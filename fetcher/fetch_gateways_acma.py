#!/usr/bin/env python3
"""ACMA RRL -> slim satellite-gateway extract (aux-data-refresh-plan §3.2 P1).

Downloads the daily full Register of Radiocommunications Licences package
(``spectra_rrl.zip``, ~hundreds of MB) on the GitHub Actions runner, joins
client -> licence -> device_details -> site, filters to the satellite
operators and bands we model, and emits a SLIM derived JSON. The raw zip is
NEVER republished (size + ACMA Licence Usage Conditions).

PUBLICATION IS WITHHELD BY DESIGN (licence determination 2026-08-08)
--------------------------------------------------------------------
The ACMA Licence Usage Conditions could NOT be read first-hand from any
channel available to this project: www.acma.gov.au blocks both the
deployment network (403/timeout) and the GitHub runner (its WAF resets
HTTP/2 and, with HTTP/1.1 + a browser UA, still refused — the 2026-08-07
terms artifact contains only the fallback marker). Secondary sources quote
the conditions as reserving all rights in the Register and permitting no
copying "except as expressly provided in the Licence", with the permitted
purpose tied to spectrum management under the Radiocommunications Act 1992.
That is (B)-grade evidence, not a first-hand reading.

The mirror repository is PUBLIC, so publishing extracted register rows there
would be public redistribution. Under unread terms that reserve all rights,
the conservative option is correct under BOTH hypotheses: if the terms allow
redistribution we lose almost nothing (see below); if they forbid it we stay
compliant. So the default is permanent: publish a CHANGE-DETECTION
FINGERPRINT and aggregate counts — facts ABOUT the register, not a copy of
it — and never the rows.

What the fingerprint buys: it changes if and only if the set of licensed
(operator, site) pairs changes, so it tells a human exactly WHEN to go look.
The follow-up is the project's existing precedent — the 13 Oceania rows
already in starlink_oceania.geojson were obtained by looking individual
sites up through ACMA's own public RRL web interface and transcribing them
with an RRL citation. Detection is automated; transcription stays human and
per-site.

``--publish-ok`` (repo variable ACMA_PUBLISH_OK=1) is retained ONLY for the
case where someone obtains a first-hand licence determination that permits
redistribution. Absent that, leave it unset. Coordinates, when extracted at
all, are the register's own licensed site coordinates (site.csv
LATITUDE/LONGITUDE) — never geocoding.

Row schema (one per licensed earth-station site per licensee):
  name, lat, lon, state, site_id, site_precision, licensee, licences
  (list of licence_no), n_tx_devices, n_rx_devices, freq_ranges_mhz,
  provenance="regulator_register",
  source_url=RRL licence search URL for the licence number.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RRL_ZIP_URL = "https://web.acma.gov.au/rrl-updates/spectra_rrl.zip"
TERMS_URL = "https://www.acma.gov.au/radiocomms-licence-data"

# Licensee match patterns (client.csv LICENCEE / TRADING_NAME, uppercased).
# Deliberately explicit — a substring like "SPACE" alone would sweep in
# unrelated licensees; every pattern names one operator we actually model.
OPERATOR_PATTERNS = {
    "starlink": re.compile(r"SPACE\s*EXPLORATION|STARLINK", re.I),
    "kuiper": re.compile(r"KUIPER|AMAZON\s+.*(KUIPER|SAT)", re.I),
    "oneweb": re.compile(r"ONEWEB|ONE\s*WEB|EUTELSAT\s+ONEWEB", re.I),
    "globalstar": re.compile(r"GLOBALSTAR", re.I),
    "iridium": re.compile(r"IRIDIUM", re.I),
    "orbcomm": re.compile(r"ORBCOMM", re.I),
    "telesat": re.compile(r"TELESAT", re.I),
}

# Frequency window (MHz) that keeps a device: anything plausibly a LEO
# gateway feeder or TT&C link. device_details.FREQUENCY is in Hz in the RRL
# extract; values are normalised to MHz below and sanity-checked.
FREQ_MIN_MHZ = 1_000.0     # L-band TT&C and up
FREQ_MAX_MHZ = 60_000.0    # through Ka/V gateway bands


def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def _freq_to_mhz(raw: str) -> float | None:
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    # The extract stores Hz; tolerate feeds that already publish MHz.
    if f > 1e8:
        f = f / 1e6
    return f if FREQ_MIN_MHZ <= f <= FREQ_MAX_MHZ else None


def _read_csv(zf: zipfile.ZipFile, name: str):
    """Stream a CSV member as dict rows (uppercased keys)."""
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        reader = csv.DictReader(text)
        for row in reader:
            yield {(k or "").strip().upper(): (v or "").strip()
                   for k, v in row.items()}


def extract(zip_path: Path) -> dict:
    zf = zipfile.ZipFile(zip_path)
    members = {Path(n).name.lower(): n for n in zf.namelist()}

    def member(base: str) -> str:
        for cand in (f"{base}.csv", f"{base}.txt"):
            if cand in members:
                return members[cand]
        raise KeyError(f"{base}.csv not found in RRL zip "
                       f"(members: {sorted(members)[:12]}...)")

    # 1. clients -> operator slug
    client_op: dict[str, str] = {}
    client_name: dict[str, str] = {}
    for row in _read_csv(zf, member("client")):
        label = f"{row.get('LICENCEE','')} {row.get('TRADING_NAME','')}"
        for slug, pat in OPERATOR_PATTERNS.items():
            if pat.search(label):
                client_op[row["CLIENT_NO"]] = slug
                client_name[row["CLIENT_NO"]] = row.get("LICENCEE") or label.strip()
                break

    # 2. licences of those clients (any status recorded; status kept per row
    #    so the reviewer sees expired vs granted rather than us silently
    #    dropping history)
    lic_client: dict[str, str] = {}
    lic_status: dict[str, str] = {}
    for row in _read_csv(zf, member("licence")):
        cno = row.get("CLIENT_NO", "")
        if cno in client_op:
            lic_client[row["LICENCE_NO"]] = cno
            lic_status[row["LICENCE_NO"]] = row.get("STATUS_TEXT") or row.get("STATUS", "")

    # 3. devices on those licences -> per (site, client) aggregation
    per_site: dict[tuple, dict] = {}
    for row in _read_csv(zf, member("device_details")):
        lic = row.get("LICENCE_NO", "")
        cno = lic_client.get(lic)
        if cno is None:
            continue
        site_id = row.get("SITE_ID", "")
        if not site_id:
            continue
        mhz = _freq_to_mhz(row.get("FREQUENCY") or row.get("CARRIER_FREQ"))
        if mhz is None:
            continue
        key = (site_id, cno)
        agg = per_site.setdefault(key, {
            "licences": set(), "tx": 0, "rx": 0, "freqs": []})
        agg["licences"].add(lic)
        dtype = (row.get("DEVICE_TYPE") or "").upper()
        if dtype == "T":
            agg["tx"] += 1
        elif dtype == "R":
            agg["rx"] += 1
        agg["freqs"].append(mhz)

    # 4. site coordinates (register's own licensed coordinates)
    sites: dict[str, dict] = {}
    wanted = {sid for sid, _ in per_site}
    for row in _read_csv(zf, member("site")):
        sid = row.get("SITE_ID", "")
        if sid in wanted:
            sites[sid] = row

    rows = []
    for (sid, cno), agg in sorted(per_site.items()):
        site = sites.get(sid)
        if site is None:
            continue
        try:
            lat = float(site["LATITUDE"])
            lon = float(site["LONGITUDE"])
        except (KeyError, TypeError, ValueError):
            # No licensed coordinate -> no row. Geocoding a site name is
            # fabrication under this project's provenance rules (§3.3).
            continue
        freqs = sorted(agg["freqs"])
        lic_sorted = sorted(agg["licences"])
        rows.append({
            "operator": client_op[cno],
            "licensee": client_name[cno],
            "name": site.get("NAME") or f"ACMA site {sid}",
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "state": site.get("STATE", ""),
            "site_id": sid,
            "site_precision": site.get("SITE_PRECISION", ""),
            "elevation_m": site.get("ELEVATION") or None,
            "licences": lic_sorted,
            "licence_status": sorted({lic_status.get(l, "") for l in lic_sorted}),
            "n_tx_devices": agg["tx"],
            "n_rx_devices": agg["rx"],
            "freq_range_mhz": [round(freqs[0], 3), round(freqs[-1], 3)],
            "provenance": "regulator_register",
            "source_url": ("https://web.acma.gov.au/rrl/"
                           f"site_search.site_lookup?pSITE_ID={sid}"),
        })

    return {
        "source": "acma_rrl",
        "source_zip_url": RRL_ZIP_URL,
        "terms_url": TERMS_URL,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(rows),
        "n_rows_by_operator": dict(sorted(
            (op, sum(1 for r in rows if r["operator"] == op))
            for op in {r["operator"] for r in rows})),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", required=True, type=Path,
                    help="Path to an already-downloaded spectra_rrl.zip "
                         "(the workflow curls it; this script never fetches)")
    ap.add_argument("--terms-html", type=Path, default=None,
                    help="Fetched Licence Usage Conditions page, hashed into "
                         "the output so a terms change is detectable")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--publish-ok", action="store_true",
                    help="Set ONLY after a human has read the ACMA Licence "
                         "Usage Conditions (repo var ACMA_PUBLISH_OK=1). "
                         "Without it, output is a coordinate-free preview.")
    args = ap.parse_args()

    doc = extract(args.zip)
    if args.terms_html and args.terms_html.exists():
        doc["terms_sha256"] = _sha256_bytes(args.terms_html.read_bytes())

    if not args.publish_ok:
        preview = {k: v for k, v in doc.items() if k != "rows"}
        rows = doc.get("rows", [])
        # Change-detection fingerprint: a hash over the sorted set of
        # (operator, site_id) pairs. It is one-way — the register content is
        # not recoverable from it — but it changes if and only if the set of
        # licensed sites changes, which is exactly the signal a human needs
        # to know when to look something up in ACMA's own public RRL UI.
        key = "\n".join(sorted(f"{r['operator']}|{r['site_id']}" for r in rows))
        preview["content_fingerprint"] = _sha256_bytes(key.encode())
        # Aggregate facts ABOUT the register (counts), never its content.
        by_state = {}
        for r in rows:
            st = r.get("state") or "?"
            by_state[st] = by_state.get(st, 0) + 1
        preview["n_rows_by_state"] = dict(sorted(by_state.items()))
        preview["publication"] = (
            "WITHHELD BY DESIGN — the ACMA Licence Usage Conditions reserve "
            "all rights in the Register and could not be read first-hand "
            "(WAF blocks both this project's network and the runner). This "
            "repository is public, so extracted rows are never published. "
            "Watch content_fingerprint: when it changes, look the new sites "
            "up in ACMA's public RRL interface and transcribe them into "
            "curated_gateways.json with an RRL citation, as the existing "
            "Oceania rows were. See the module docstring.")
        out = args.out.with_suffix(".preview.json")
        out.write_text(json.dumps(preview, indent=2, ensure_ascii=False))
        print(f"[acma] fingerprint {preview['content_fingerprint'][:16]} over "
              f"{doc['n_rows']} sites {doc['n_rows_by_operator']} "
              f"-> {out} (rows withheld by design)")
        return 0

    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    print(f"[acma] wrote {args.out} ({doc['n_rows']} rows: "
          f"{doc['n_rows_by_operator']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
