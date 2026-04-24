"""
Parse Jonathan McDowell's JCAT (General Catalog of Artificial Space Objects)
TSV into a compact JSON map of NORAD_CAT_ID -> operational status.

Source: https://planet4589.org/space/gcat/tsv/cat/satcat.tsv
Header: tab-separated, with a leading '#JCAT\\tSatcat\\t...' line.

Output: data/jcat_status.json
    {
      "<NORAD>": {
          "status": "O",          # raw JCAT code (O / R / AO / AR / D / DSO / N / E / OX / DK / L / ...)
          "op_orbit": "LEO/I"     # JCAT OpOrbit class (informational)
      },
      ...
    }

Status code meanings (most common):
    O   operational, in Earth orbit                 (counts as "operational")
    OX  auxiliary operational (e.g. cubesats from ISS)  (counts as "operational")
    AO  operational, atmospheric reentry expected   (counts as "operational")
    R   in orbit, retired / reserved
    R?  uncertain reserved
    AR  atmospheric reentry / decayed
    D   decayed (deep space disposal)
    DSO deep space operational (probes, lunar, etc.)
    DK  deep space, dock / docked
    DSA deep space, attached
    N   non-functional / failed
    E   exotic / planetary explorer (out of Earth orbit)
    L   lost (no current data)
    TFR transferred (escape trajectory)
    ATT attached to another object
    GRP grouped (constellation marker)
    C   cataloged, no other info

Run from a GitHub Actions runner; outputs ~3 MB JSON for 68k objects.
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import requests

JCAT_URL = "https://planet4589.org/space/gcat/tsv/cat/satcat.tsv"
TIMEOUT = 180
RETRIES = 3
USER_AGENT = "changshuospace-tle-mirror/1 (+https://github.com)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TSV_PATH = DATA_DIR / "jcat_satcat.tsv"
OUT_PATH = DATA_DIR / "jcat_status.json"

# JCAT status codes considered "currently operational" (Earth orbit).
# Does NOT include DSO/DK/DSA — those are deep-space probes that satellite.js
# can't render anyway, and satellitemap.space excludes them too.
OPERATIONAL_STATUSES = {"O", "OX", "AO"}

# Statuses worth keeping in jcat_status.json. Empty / "-" / unknown skipped.
KEEP_STATUSES = {
    "O", "OX", "AO", "R", "R?", "L", "L?", "AR", "D", "DK", "DSO", "DSA",
    "N", "E", "TFR", "ATT", "GRP", "C", "ERR", "REL", "EVA DP",
}


def _download_tsv() -> bool:
    """Stream the JCAT TSV to disk. Returns True on success."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with requests.get(
                JCAT_URL,
                stream=True,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            ) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    print(f"[jcat] attempt {attempt}: {last_err}",
                          file=sys.stderr)
                    time.sleep(2 ** attempt)
                    continue
                with TSV_PATH.open("wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
            size_mb = TSV_PATH.stat().st_size / (1024 * 1024)
            print(f"[jcat] downloaded {size_mb:.1f} MB to {TSV_PATH}")
            return True
        except requests.RequestException as exc:
            last_err = str(exc)
            print(f"[jcat] attempt {attempt} error: {exc}", file=sys.stderr)
            time.sleep(2 ** attempt)
    print(f"[jcat] failed after {RETRIES} retries: {last_err}",
          file=sys.stderr)
    return False


def _parse_header(row: list) -> Optional[Dict[str, int]]:
    """Map header column name -> index. Strips leading '#'."""
    cleaned = [(c or "").lstrip("#").strip() for c in row]
    idx = {name: i for i, name in enumerate(cleaned) if name}
    if "Satcat" in idx and "Status" in idx:
        return idx
    return None


# Status precedence when a NORAD has multiple JCAT entries (e.g. ISS as GRP
# alongside its operational module). Lower index wins; "real" operational
# states override administrative markers like GRP / ATT / C.
_STATUS_PRIORITY = [
    "O", "OX", "AO",          # currently operational (Earth orbit)
    "DSO",                    # operational beyond Earth orbit
    "R", "R?",                # in orbit, retired
    "N",                      # failed in orbit
    "AR", "D", "DK", "DSA",   # decayed / deep-space disposal
    "L", "L?", "TFR", "E",    # lost / transferred / exotic
    "GRP", "ATT", "C", "ERR", "REL", "EVA DP",  # admin / catalog markers
]
_STATUS_RANK = {s: i for i, s in enumerate(_STATUS_PRIORITY)}


def _better_status(existing: Dict[str, str], candidate: Dict[str, str]) -> bool:
    """True if `candidate` should overwrite `existing` for the same NORAD."""
    new_rank = _STATUS_RANK.get(candidate["status"], 999)
    old_rank = _STATUS_RANK.get(existing["status"], 999)
    return new_rank < old_rank


def parse_tsv(path: Path) -> Dict[str, Dict[str, str]]:
    """Read the JCAT TSV and return {norad_str: {status, op_orbit}}."""
    out: Dict[str, Dict[str, str]] = {}
    header_idx: Optional[Dict[str, int]] = None

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        for row in reader:
            if not row:
                continue
            first = (row[0] or "").strip()
            if first.startswith("#"):
                if header_idx is None:
                    parsed = _parse_header(row)
                    if parsed is not None:
                        header_idx = parsed
                continue
            if header_idx is None:
                continue
            try:
                satcat = (row[header_idx["Satcat"]] or "").strip()
                status = (row[header_idx["Status"]] or "").strip()
            except IndexError:
                continue
            if not satcat or not satcat.isdigit():
                continue
            if not status or status == "-":
                continue
            entry: Dict[str, str] = {"status": status}
            op_idx = header_idx.get("OpOrbit")
            if op_idx is not None and op_idx < len(row):
                op = (row[op_idx] or "").strip()
                if op and op != "-":
                    entry["op_orbit"] = op
            # Cast NORAD to int(str) to drop leading zeros, matching DB schema.
            key = str(int(satcat))
            existing = out.get(key)
            if existing is None or _better_status(existing, entry):
                out[key] = entry
    return out


def main() -> int:
    if not _download_tsv():
        return 1

    status_map = parse_tsv(TSV_PATH)
    if not status_map:
        print("[jcat] parse produced 0 records — refusing to publish",
              file=sys.stderr)
        return 1

    op_count = sum(1 for v in status_map.values()
                   if v.get("status") in OPERATIONAL_STATUSES)
    OUT_PATH.write_text(
        json.dumps(status_map, ensure_ascii=False, separators=(",", ":"))
    )
    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"[jcat] wrote {OUT_PATH} ({len(status_map)} records, "
          f"{op_count} operational, {size_mb:.2f} MB)")

    # Drop the bulky source TSV — only the JSON ships in the release.
    try:
        TSV_PATH.unlink()
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
