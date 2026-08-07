#!/usr/bin/env python3
"""AWS Ground Station locations -> slim JSON (plan §3.2 P4).

Source of truth (2026-08-07): the marketing URL 302-redirects to the docs
page ``aws-ground-station-antenna-locations.html``, whose station list is a
plain HTML table — Ground Station Name | Location | AWS Region Name |
AWS Region Code | Notes. The first workflow run proved the old regex parsed
0 rows off the redirect target; this parser targets the real table.

The page carries NO coordinates and this project never geocodes (plan
§3.3), so rows are CITY-LEVEL name records whose only use is the
change-review diff against the curated kuiper/AWS slots (which have
coordinates from other sources). provenance stays "aws_ground_station".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

LOCATIONS_URL = ("https://docs.aws.amazon.com/ground-station/latest/ug/"
                 "aws-ground-station-antenna-locations.html")


class _TableParser(HTMLParser):
    """Collect every <table> as a list of rows of stripped cell texts."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_locations(html: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(html)
    for table in parser.tables:
        if not table:
            continue
        header = [c.lower() for c in table[0]]
        if not any("ground station name" in c for c in header):
            continue
        rows = []
        for r in table[1:]:
            if len(r) < 4 or not r[0]:
                continue
            rows.append({
                "name": f"AWS GS {r[0]}",
                "station": r[0],
                "city": r[1],
                "aws_region_name": r[2],
                "aws_region_code": r[3],
                "notes": r[4] if len(r) > 4 else "",
                "provenance": "aws_ground_station",
                "source_url": LOCATIONS_URL,
            })
        if rows:
            return rows
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", required=True, type=Path)
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
    print(f"[aws] wrote {args.out} ({len(rows)} stations: "
          f"{[r['station'] for r in rows]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
