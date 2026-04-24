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
THROTTLE_SEC = 5
TIMEOUT = 60
RETRIES = 3
USER_AGENT = "changshuospace-tle-mirror/1 (+https://github.com)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _request(params: Dict[str, str]) -> Optional[List[Dict]]:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(
                CELESTRAK_GP_URL,
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
    print(f"[celestrak] params={params} failed after {RETRIES} retries: {last_err}",
          file=sys.stderr)
    return None


def fetch_group(slug: str, conf: Dict) -> List[Dict]:
    """Fetch by group slug, then patterns, then return whatever we got."""
    by_norad: Dict[int, Dict] = {}
    group = conf.get("group")
    if group:
        recs = _request({"GROUP": group, "FORMAT": "json"}) or []
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
