#!/usr/bin/env python3
"""AWS Ground Station locations page -> slim JSON (plan §3.2 P4).

The public locations page lists a few dozen region/city entries. It carries
NO coordinates — and this project never geocodes (plan §3.3) — so rows here
are CITY-LEVEL name records whose only use is the change-review diff: the
puller compares them against the kuiper slots already curated (with
coordinates from other sources) and flags additions/removals for a human.
provenance stays "aws_ground_station".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCATIONS_URL = "https://aws.amazon.com/ground-station/locations/"


def parse_locations(html: str) -> list[dict]:
    # The page renders location entries as headings/list items of the form
    # "Region name (IATA-ish code)". Parse liberally, dedupe, sort.
    pat = re.compile(r">([A-Z][A-Za-z .'-]{2,40})\s*\(([A-Z]{3,4})\)<")
    seen = {}
    for name, code in pat.findall(html):
        name = name.strip()
        if name.lower() in ("amazon web services",):
            continue
        seen[code] = {"name": f"AWS GS {name} ({code})",
                      "location_code": code,
                      "city": name,
                      "provenance": "aws_ground_station",
                      "source_url": LOCATIONS_URL}
    return [seen[k] for k in sorted(seen)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", required=True, type=Path,
                    help="Fetched locations page (workflow curls it)")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    rows = parse_locations(args.html.read_text(encoding="utf-8",
                                               errors="replace"))
    if not rows:
        # A silent empty parse after a page redesign must FAIL the job, not
        # publish an empty asset that the diff would read as mass removal.
        print("[aws] ERROR: 0 locations parsed — page layout changed?")
        return 2
    doc = {"source": "aws_ground_station_locations",
           "source_url": LOCATIONS_URL,
           "generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "coordinates": "none — city-level names only, diff/review use",
           "n_rows": len(rows), "rows": rows}
    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    print(f"[aws] wrote {args.out} ({len(rows)} locations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
