"""
Pull GP records (TLE + orbital elements) from CelesTrak per constellation
group, with name-pattern fallback. Output: data/<slug>.json (an array of GP
records compatible with Space-Track's gp class JSON schema).

Run from a GitHub Actions runner (US/EU egress IP) — CelesTrak is rock
solid from there. Throttle 5s between requests to be polite.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from constellations import CONSTELLATIONS, ALL_SLUGS
from tle_synthesizer import fill_missing_tle_lines

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"
# Supplemental GP (operator-derived ephemerides) — fresher than catalog GP for
# Starlink / OneWeb. Unioned on top of the group feed by newer EPOCH.
CELESTRAK_SUPGP_URL = "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php"
SUPGP_FILES = {
    "starlink": "starlink",
    "oneweb": "oneweb",
}
THROTTLE_SEC = 5
# Fail fast: when CelesTrak is degraded, long timeouts here can eat the
# whole hourly job (observed 2026-07-30: 31 groups x 3x60s retries ran past
# the 60-min job timeout, so the Space-Track fallback never fired). A group
# that fails still gets an empty data/<slug>.json, which routes it to the
# Space-Track fallback step.
TIMEOUT = 15
RETRIES = 2
USER_AGENT = "changshuospace-tle-mirror/1 (+https://github.com)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _epoch_key(rec: Dict) -> str:
    return str(rec.get("EPOCH") or "")


def fetch_supgp(slug: str) -> List[Dict]:
    """Optional Supplemental GP overlay (Starlink / OneWeb)."""
    file_id = SUPGP_FILES.get(slug)
    if not file_id:
        return []
    recs = _request_url(CELESTRAK_SUPGP_URL, {"FILE": file_id, "FORMAT": "json"}) or []
    print(f"[celestrak] {slug}: sup-gp FILE={file_id} -> {len(recs)} records")
    return recs


def _request_url(url: str, params: Dict[str, str]) -> Optional[List[Dict]]:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(
                url,
                params=params,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if r.status_code == 200:
                text = r.text.strip()
                if not text or text.lower().startswith("no gp data"):
                    return []
                try:
                    return r.json()
                except json.JSONDecodeError:
                    return []
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(2 ** attempt)
    print(f"[celestrak] url={url} params={params} failed after {RETRIES} retries: {last_err}",
          file=sys.stderr)
    return None


def _request(params: Dict[str, str]) -> Optional[List[Dict]]:
    return _request_url(CELESTRAK_GP_URL, params)


def fetch_group(slug: str, conf: Dict) -> List[Dict]:
    """Fetch by group slug, then patterns, then SupGP overlay for freshness."""
    by_norad: Dict[int, Dict] = {}
    group = conf.get("group")
    if group:
        recs = _request({"GROUP": group, "FORMAT": "json"}) or []
        # A CATCH-ALL group (e.g. "other-comm") carries satellites that belong
        # to other slugs entirely. When filter_patterns is set, `patterns` acts
        # as a NAME filter on the group result instead of only as the
        # zero-result fallback below — without it, borrowing a catch-all group
        # publishes every one of its members under this slug (measured: lynk
        # published 32 records of which 4 were Lynk).
        if conf.get("filter_patterns"):
            pats = [p.upper() for p in (conf.get("patterns") or [])]
            before = len(recs)
            recs = [r for r in recs
                    if any(p in (r.get("OBJECT_NAME") or "").upper()
                           for p in pats)]
            print(f"[celestrak] {slug}: group={group} filtered "
                  f"{before} -> {len(recs)} by {pats}")
        for r in recs:
            nid = r.get("NORAD_CAT_ID")
            if nid:
                by_norad[int(nid)] = r
        if by_norad:
            print(f"[celestrak] {slug}: group={group} -> {len(by_norad)} records")

    if not by_norad:
        for pattern in conf.get("patterns", []) or []:
            time.sleep(THROTTLE_SEC)
            recs = _request({"NAME": pattern, "FORMAT": "json"}) or []
            for r in recs:
                nid = r.get("NORAD_CAT_ID")
                if nid and int(nid) not in by_norad:
                    by_norad[int(nid)] = r
        if by_norad:
            print(f"[celestrak] {slug}: patterns -> {len(by_norad)} records")

    # Overlay Supplemental GP: prefer newer EPOCH for the same NORAD.
    if slug in SUPGP_FILES:
        time.sleep(THROTTLE_SEC)
        replaced = 0
        added = 0
        for r in fetch_supgp(slug):
            nid = r.get("NORAD_CAT_ID")
            if not nid:
                continue
            nid = int(nid)
            prev = by_norad.get(nid)
            if prev is None:
                by_norad[nid] = r
                added += 1
            elif _epoch_key(r) > _epoch_key(prev):
                by_norad[nid] = r
                replaced += 1
        if replaced or added:
            print(f"[celestrak] {slug}: sup-gp overlay replaced={replaced} added={added}")

    if not by_norad:
        print(f"[celestrak] {slug}: NO DATA")

    return list(by_norad.values())


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slugs = sys.argv[1:] or ALL_SLUGS
    summary = {}
    for slug in slugs:
        if slug not in CONSTELLATIONS:
            print(f"[celestrak] skip unknown slug {slug}", file=sys.stderr)
            continue
        records = fetch_group(slug, CONSTELLATIONS[slug])
        # Some recent launches arrive as OMM-only (no plain TLE strings).
        # Synthesize line1/line2 from the OMM elements before publishing
        # so downstream consumers (puller, satellite.js, sgp4 in any
        # language) all see a TLE-complete payload.
        records, filled = fill_missing_tle_lines(records)
        if filled:
            print(f"[celestrak] {slug}: synthesised TLE lines for "
                  f"{filled} OMM-only records")
        out = DATA_DIR / f"{slug}.json"
        out.write_text(json.dumps(records, ensure_ascii=False, separators=(",", ":")))
        summary[slug] = len(records)
        time.sleep(THROTTLE_SEC)

    print("[celestrak] summary:", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
