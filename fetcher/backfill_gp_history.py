"""Backfill historical GP (TLE) records via Space-Track gp_history.

Designed for GitHub Actions: each run advances a persistent cursor a few
EPOCH-day windows, writing data/backfill/<YYYY-MM-DD>.jsonl.gz assets that
downstream consumers (ChangShuoSpace puller) can ingest with INSERT OR IGNORE.

Rate limits (protect the account):
  * At most BACKFILL_DAYS_PER_RUN windows per invocation (default 2).
  * BACKFILL_BETWEEN_SECONDS sleep between windows (default 25).
  * Single account login reused for the whole run; logout at the end.

Cursor file: data/backfill_cursor.json  (committed back by the workflow, or
stored as a Release asset named backfill_cursor.json).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
LOGOUT_URL = f"{BASE_URL}/ajaxauth/logout"
TIMEOUT = 300
USER_AGENT = "changshuospace-tle-mirror-backfill/1 (+https://github.com)"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BACKFILL_DIR = DATA_DIR / "backfill"
CURSOR_PATH = DATA_DIR / "backfill_cursor.json"

DEFAULT_START = os.environ.get("BACKFILL_START", "2026-02-27")
DEFAULT_END = os.environ.get("BACKFILL_END", "2026-06-15")
DAYS_PER_RUN = int(os.environ.get("BACKFILL_DAYS_PER_RUN", "2"))
BETWEEN_S = int(os.environ.get("BACKFILL_BETWEEN_SECONDS", "25"))
WINDOW_HOURS = int(os.environ.get("BACKFILL_WINDOW_HOURS", "24"))


def _login() -> Optional[requests.Session]:
    user = os.environ.get("SPACETRACK_USER")
    pwd = os.environ.get("SPACETRACK_PASS")
    if not user or not pwd:
        print("[backfill] SPACETRACK_USER/PASS not set; aborting", file=sys.stderr)
        return None
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    try:
        r = s.post(LOGIN_URL, data={"identity": user, "password": pwd}, timeout=60)
        if r.status_code == 200 and "error" not in r.text.lower():
            print(f"[backfill] login OK as {user[:3]}***")
            return s
        print(f"[backfill] login failed status={r.status_code}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"[backfill] login error: {exc}", file=sys.stderr)
    return None


def _logout(session: requests.Session) -> None:
    try:
        session.get(LOGOUT_URL, timeout=30)
    except requests.RequestException:
        pass


def _load_cursor() -> datetime:
    if CURSOR_PATH.exists():
        try:
            d = json.loads(CURSOR_PATH.read_text())
            return datetime.fromisoformat(d["next_epoch"])
        except Exception as exc:
            print(f"[backfill] cursor parse failed ({exc}); using start", file=sys.stderr)
    return datetime.fromisoformat(DEFAULT_START + "T00:00:00")


def _save_cursor(dt: datetime, extra: dict | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "next_epoch": dt.isoformat(),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "end": DEFAULT_END,
    }
    if extra:
        payload.update(extra)
    CURSOR_PATH.write_text(json.dumps(payload, indent=2))


def _query_window(session: requests.Session, start: datetime, end: datetime) -> List[Dict]:
    """Fetch ALL objects whose EPOCH falls in [start, end)."""
    # Space-Track GFE requires encoded comparison operators.
    start_s = start.strftime("%Y-%m-%d%%20%H:%M:%S")
    end_s = end.strftime("%Y-%m-%d%%20%H:%M:%S")
    # Use predicate form EPOCH/<start>--<end> which Space-Track accepts as a
    # closed-open range, or two filters with %3E / %3C.
    path = (
        f"/basicspacedata/query/class/gp_history/"
        f"EPOCH/%3E{quote(start.strftime('%Y-%m-%dT%H:%M:%S'), safe='')}"
        f"/%3C{quote(end.strftime('%Y-%m-%dT%H:%M:%S'), safe='')}/"
        f"orderby/EPOCH%20asc/format/json"
    )
    # Prefer the simpler predicate form first (smaller URL, often faster).
    alt = (
        f"/basicspacedata/query/class/gp_history/"
        f"EPOCH/{start.strftime('%Y-%m-%d')}--{end.strftime('%Y-%m-%d')}/"
        f"orderby/EPOCH%20asc/format/json"
    )
    for candidate in (alt, path):
        url = f"{BASE_URL}{candidate}"
        try:
            r = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            print(f"[backfill] query error: {exc}", file=sys.stderr)
            continue
        if r.status_code != 200:
            print(f"[backfill] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            continue
        try:
            data = r.json()
            if isinstance(data, list):
                return data
        except json.JSONDecodeError as exc:
            print(f"[backfill] JSON decode: {exc}", file=sys.stderr)
    return []


def _write_window(day_tag: str, records: List[Dict]) -> Path:
    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKFILL_DIR / f"{day_tag}.jsonl.gz"
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


def main() -> int:
    end = datetime.fromisoformat(DEFAULT_END + "T00:00:00")
    cursor = _load_cursor()
    if cursor >= end:
        print(f"[backfill] cursor {cursor.isoformat()} already past end {end.isoformat()}; done")
        _save_cursor(cursor, {"status": "complete"})
        return 0

    session = _login()
    if session is None:
        return 2

    window = timedelta(hours=WINDOW_HOURS)
    saved_total = 0
    try:
        for i in range(DAYS_PER_RUN):
            if cursor >= end:
                break
            win_end = min(cursor + window, end)
            day_tag = cursor.strftime("%Y-%m-%d")
            print(f"[backfill] window {i+1}/{DAYS_PER_RUN}: "
                  f"[{cursor.isoformat()} → {win_end.isoformat()})")
            records = _query_window(session, cursor, win_end)
            print(f"[backfill]   got {len(records)} records")
            if records:
                path = _write_window(day_tag, records)
                print(f"[backfill]   wrote {path} ({path.stat().st_size} bytes)")
                saved_total += len(records)
            cursor = win_end
            _save_cursor(cursor, {"last_window_records": len(records)})
            if i + 1 < DAYS_PER_RUN and cursor < end:
                time.sleep(BETWEEN_S)
    finally:
        _logout(session)

    # Manifest for this run
    manifest = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "next_epoch": cursor.isoformat(),
        "records_this_run": saved_total,
        "files": sorted(p.name for p in BACKFILL_DIR.glob("*.jsonl.gz")),
    }
    (BACKFILL_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[backfill] DONE records={saved_total} next={cursor.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
