#!/usr/bin/env python3
"""ACMA RRL -> slim satellite-gateway extract (aux-data-refresh-plan §3.2 P1).

Downloads the daily full Register of Radiocommunications Licences package
(``spectra_rrl.zip``, ~hundreds of MB) on the GitHub Actions runner, joins
client -> licence -> device_details -> site, filters to the satellite
operators and bands we model, and emits a SLIM derived JSON. The raw zip is
NEVER republished (size + ACMA Licence Usage Conditions).

Publication gate (licence terms, §3.4): the ACMA register download is
subject to "Licence Usage Conditions" whose redistribution clauses could not
be reviewed from the deployment network (acma.gov.au is unreachable there —
403/timeout, measured 2026-08-07). Until a human has read the conditions ON
THE RUNNER and set the repository variable ``ACMA_PUBLISH_OK=1``, this
script still runs — proving the parser against live data — but writes only
``gateways_acma.preview.json`` (row COUNTS and the terms text sha256, zero
coordinate rows). The workflow uploads the fetched terms page as a build
artifact for that review. Coordinates here are the register's own licensed
site coordinates (site.csv LATITUDE/LONGITUDE), never geocoding.

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
        preview["publication"] = (
            "WITHHELD — ACMA Licence Usage Conditions not yet reviewed; "
            "set repo variable ACMA_PUBLISH_OK=1 after reading the terms "
            "artifact to publish derived rows.")
        out = args.out.with_suffix(".preview.json")
        out.write_text(json.dumps(preview, indent=2, ensure_ascii=False))
        print(f"[acma] PREVIEW ONLY -> {out} ({doc['n_rows']} rows withheld)")
        return 0

    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    print(f"[acma] wrote {args.out} ({doc['n_rows']} rows: "
          f"{doc['n_rows_by_operator']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
