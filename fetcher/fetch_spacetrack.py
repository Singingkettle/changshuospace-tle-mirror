"""
Pull from Space-Track.org as fallback / SATCAT primary.

Three modes:
  * `python fetch_spacetrack.py satcat` — full SATCAT JSON (1×/day)
  * `python fetch_spacetrack.py decays` — SATCAT rows for objects decayed
    within the last DECAY_WINDOW_DAYS (small payload, safe to run hourly).
    This is the ONLY feed that carries authoritative decay dates: the GP
    queries below intentionally filter `decay_date/null-val` (per the
    Space-Track "propagable ephemerides" best practice), so without this
    asset decayed objects silently vanish from the mirror and downstream
    DBs never learn they re-entered.
  * `python fetch_spacetrack.py gp <slug> [<slug> ...]` — query GP class for
    constellations CelesTrak couldn't cover (rare; only used when
    fetch_celestrak.py wrote a 0-record file).

Credentials come from `SPACETRACK_USER` / `SPACETRACK_PASS` env vars
(supplied as GitHub Actions repository secrets). Single account, used at
most a few times per cycle, well under the 30 req/min limit.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from constellations import CONSTELLATIONS
from tle_synthesizer import fill_missing_tle_lines

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"
TIMEOUT = 180
USER_AGENT = "changshuospace-tle-mirror/1 (+https://github.com)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Look-back window for the `decays` mode. 45 days overlaps generously with
# the hourly cadence, so a few missed cycles never lose a re-entry.
DECAY_WINDOW_DAYS = 45


def _login() -> Optional[requests.Session]:
    user = os.environ.get("SPACETRACK_USER")
    pwd = os.environ.get("SPACETRACK_PASS")
    if not user or not pwd:
        print("[spacetrack] SPACETRACK_USER/PASS not set; skipping",
              file=sys.stderr)
        return None
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    try:
        r = s.post(LOGIN_URL, data={"identity": user, "password": pwd},
                   timeout=60)
        if r.status_code == 200 and "error" not in r.text.lower():
            print(f"[spacetrack] login OK as {user[:3]}***")
            return s
        print(f"[spacetrack] login failed status={r.status_code}",
              file=sys.stderr)
    except requests.RequestException as exc:
        print(f"[spacetrack] login error: {exc}", file=sys.stderr)
    return None


def _logout(session: requests.Session) -> None:
    try:
        session.get(LOGOUT_URL, timeout=30)
    except requests.RequestException:
        pass


def _query(session: requests.Session, path: str) -> Optional[List[Dict]]:
    url = f"{BASE_URL}{path}"
    try:
        r = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"[spacetrack] query error {path}: {exc}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[spacetrack] HTTP {r.status_code} for {path}: "
              f"{r.text[:200]}", file=sys.stderr)
        return None
    try:
        return r.json()
    except json.JSONDecodeError as exc:
        print(f"[spacetrack] non-JSON response for {path}: {exc}",
              file=sys.stderr)
        return None


def fetch_satcat(session: requests.Session) -> List[Dict]:
    path = ("/basicspacedata/query/class/satcat/orderby/NORAD_CAT_ID%20asc/"
            "format/json")
    print("[spacetrack] fetching full SATCAT...")
    data = _query(session, path) or []
    print(f"[spacetrack] satcat -> {len(data)} records")
    return data


def fetch_recent_decays(session: requests.Session) -> List[Dict]:
    """SATCAT records of objects that decayed in the last N days.

    A single cheap request (typically a few hundred rows), so running it
    on every hourly cycle stays far below the 30 req/min / 300 req/hr
    limits. The DECAY field of the satcat class is the authoritative
    re-entry date per https://www.space-track.org/documentation#api.
    """
    path = (f"/basicspacedata/query/class/satcat/"
            f"DECAY/%3Enow-{DECAY_WINDOW_DAYS}/"
            f"orderby/DECAY%20desc/format/json")
    print(f"[spacetrack] fetching decays (last {DECAY_WINDOW_DAYS} days)...")
    data = _query(session, path) or []
    print(f"[spacetrack] decays -> {len(data)} records")
    return data


def fetch_gp_for_slug(session: requests.Session, slug: str) -> List[Dict]:
    conf = CONSTELLATIONS.get(slug)
    if not conf:
        print(f"[spacetrack] unknown slug {slug}", file=sys.stderr)
        return []
    patterns = conf.get("patterns") or [slug.upper()]
    by_norad: Dict[int, Dict] = {}
    for pattern in patterns:
        # latest GP per object: orderby EPOCH desc + group by NORAD_CAT_ID.
        # decay_date/null-val + epoch/>now-30 follow the documented best
        # practice ("only retrieve propagable ephemerides for on-orbit
        # objects") and keep the query cheap for the server.
        path = (
            f"/basicspacedata/query/class/gp/OBJECT_NAME/~~{pattern}/"
            f"decay_date/null-val/epoch/%3Enow-30/"
            f"orderby/EPOCH%20desc/format/json"
        )
        recs = _query(session, path) or []
        for r in recs:
            nid = r.get("NORAD_CAT_ID")
            if nid and int(nid) not in by_norad:
                by_norad[int(nid)] = r
        time.sleep(2)  # be polite, well under 30/min
    print(f"[spacetrack] gp {slug} -> {len(by_norad)} records")
    return list(by_norad.values())


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fetch_spacetrack.py satcat | gp <slug> [<slug> ...]",
              file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1]
    session = _login()
    if not session:
        return 0  # soft-fail: a missed ST cycle is fine, mirror still ships
                  # whatever CelesTrak gave us

    try:
        if mode == "satcat":
            records = fetch_satcat(session)
            (DATA_DIR / "satcat.json").write_text(
                json.dumps(records, ensure_ascii=False, separators=(",", ":"))
            )
        elif mode == "decays":
            records = fetch_recent_decays(session)
            # Only publish a non-empty asset: an empty list would usually
            # mean the query failed, and clobbering a good previous asset
            # with [] would hide real decays from the puller for an hour.
            if records:
                (DATA_DIR / "decays.json").write_text(
                    json.dumps(records, ensure_ascii=False,
                               separators=(",", ":"))
                )
            else:
                print("[spacetrack] decays returned 0 records; "
                      "not writing decays.json", file=sys.stderr)
        elif mode == "gp":
            slugs = sys.argv[2:]
            if not slugs:
                print("[spacetrack] no slugs given to gp mode", file=sys.stderr)
                return 1
            for slug in slugs:
                records = fetch_gp_for_slug(session, slug)
                if not records:
                    continue
                # Merge with whatever CelesTrak already wrote — Space-Track
                # adds satellites missing from CelesTrak (e.g. very recent
                # yinhe / lynk launches). Dedup by NORAD_CAT_ID, ST wins
                # because its TLEs are usually more recent.
                target = DATA_DIR / f"{slug}.json"
                merged: Dict[int, Dict] = {}
                if target.exists():
                    try:
                        existing = json.loads(target.read_text() or "[]")
                        for r in existing:
                            nid = r.get("NORAD_CAT_ID")
                            if nid:
                                merged[int(nid)] = r
                    except (OSError, json.JSONDecodeError):
                        pass
                for r in records:
                    nid = r.get("NORAD_CAT_ID")
                    if nid:
                        merged[int(nid)] = r
                merged_records = list(merged.values())
                # Fill in TLE_LINE1/2 for OMM-only records (very recent
                # launches whose plain TLE strings haven't been published).
                merged_records, filled = fill_missing_tle_lines(merged_records)
                if filled:
                    print(f"[spacetrack] {slug}: synthesised TLE lines for "
                          f"{filled} OMM-only records")
                target.write_text(
                    json.dumps(merged_records, ensure_ascii=False,
                               separators=(",", ":"))
                )
                print(f"[spacetrack] merged {slug}: {len(merged_records)} total")
        else:
            print(f"[spacetrack] unknown mode {mode}", file=sys.stderr)
            return 1
    finally:
        _logout(session)

    return 0


if __name__ == "__main__":
    sys.exit(main())
