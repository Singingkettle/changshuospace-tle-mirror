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
# Extended 2026-08-08 from 2026-06-15: measured epoch-day coverage shows
# the region 2026-06-08..2026-07-30 is SPARSE, not covered — 12k-37k rows/day
# against 54k-63k in the backfilled region and 84k-116k once the live
# pipeline densifies at 2026-07-31 (2026-07-25 holds just 18 rows). Stopping
# at 06-15 would have declared the job complete with that sparse band left
# as-is. Re-running those days is safe and idempotent: the consumer inserts
# under a UNIQUE(satellite_id, epoch) index, so existing rows are skipped and
# only the missing versions land.
DEFAULT_END = os.environ.get("BACKFILL_END", "2026-07-31")
# HARD CLAMP, not a default. BETWEEN_S=25 lets one process sustain
# 3600/25 = 144 windows/hour, and a window costs 2 GETs when the primary
# predicate form fails — 289 requests/hour against a published 300/hour
# ceiling (96.3%). That was reachable by typing a number into the
# workflow_dispatch text box, with no code change and nothing unusual
# happening. Clamped at 8: worst case 8x2 GETs + login + logout = 18
# requests per run.
_MAX_DAYS_PER_RUN = 8
DAYS_PER_RUN = max(1, min(int(os.environ.get("BACKFILL_DAYS_PER_RUN", "2")),
                          _MAX_DAYS_PER_RUN))
# Raised 25 -> 40 on 2026-08-08. The clamp above bounds ONE run, but the
# SUSTAINED hourly rate under continuous dispatch is set here, not by the
# clamp: 3600/(BETWEEN_S + RETRY_BACKOFF_S) x 2 GETs per window. At 25s+0s
# that was 288/hour against a published 300/hour ceiling — 96%, i.e. a
# structural near-miss that no amount of clamping fixes. At 40s+5s it is
# 160/hour = 53%. Cost to normal operation: 3 windows/run means 120s of
# sleeps instead of 50s, on a job that runs 4x a day.
BETWEEN_S = int(os.environ.get("BACKFILL_BETWEEN_SECONDS", "40"))
# Pause before retrying the second predicate form (see _query_window).
RETRY_BACKOFF_S = int(os.environ.get("BACKFILL_RETRY_BACKOFF_S", "5"))
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


class CursorUnavailable(RuntimeError):
    """The cursor exists somewhere but this run could not read it.

    Distinct from "there is no cursor yet". Falling back to DEFAULT_START in
    this case rewinds the frontier by however far the sweep has progressed —
    measured 2026-08-08: a single failed `gh release download` (the restore
    step swallows failures with `|| true`) left no cursor file, so the run
    would have resumed at 2026-02-27, re-queried ~100 finished EPOCH-days,
    and then PUBLISHED that rewound cursor, because it is not a repair run
    and the repair guard therefore does not apply. Reachable with no human
    action at all.
    """


def _load_cursor() -> datetime:
    if CURSOR_PATH.exists():
        try:
            d = json.loads(CURSOR_PATH.read_text())
            return datetime.fromisoformat(d["next_epoch"])
        except Exception as exc:
            # A corrupt cursor is NOT a first run. Refuse rather than rewind.
            raise CursorUnavailable(
                f"cursor file present but unreadable ({exc}); refusing to "
                f"fall back to {DEFAULT_START}") from exc
    # No cursor file at all. That is legitimate ONLY on the very first run;
    # any later run reaches this state because the restore failed. The
    # workflow signals which case it is via BACKFILL_CURSOR_RESTORED.
    if os.environ.get("BACKFILL_CURSOR_RESTORED") == "0":
        raise CursorUnavailable(
            "no cursor file and the workflow reported that a release cursor "
            "EXISTS but could not be restored; refusing to rewind to "
            f"{DEFAULT_START}")
    return datetime.fromisoformat(DEFAULT_START + "T00:00:00")


# Set by main() from the cursor this run loaded; _save_cursor refuses to go
# below it. None means "no floor known yet" (module import, tests).
_CURSOR_FLOOR: Optional[datetime] = None


def _save_cursor(dt: datetime, extra: dict | None = None) -> None:
    if _CURSOR_FLOOR is not None and dt < _CURSOR_FLOOR:
        raise CursorUnavailable(
            f"refusing to move the cursor backward: {dt.isoformat()} < "
            f"{_CURSOR_FLOOR.isoformat()}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "next_epoch": dt.isoformat(),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "end": DEFAULT_END,
    }
    if extra:
        payload.update(extra)
    CURSOR_PATH.write_text(json.dumps(payload, indent=2))


def _query_window(session: requests.Session, start: datetime,
                  end: datetime) -> Optional[List[Dict]]:
    """Fetch ALL objects whose EPOCH falls in [start, end).

    Returns the record list on success (possibly empty for a genuinely empty
    day), or None when BOTH predicate forms failed — the caller must not
    advance its cursor on None."""
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
    for attempt, candidate in enumerate((alt, path)):
        if attempt:
            # The fallback predicate form used to fire back-to-back with zero
            # delay, doubling this window's request cost at full speed. A
            # short backoff halves the achievable rate of exactly the failure
            # mode that gets closest to the hourly limit.
            time.sleep(RETRY_BACKOFF_S)
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
    # BOTH predicate forms failed. Returning [] here used to be
    # indistinguishable from "this day genuinely has no records", and the
    # caller advanced its cursor either way — so a transient Space-Track
    # error silently and PERMANENTLY skipped that EPOCH-day. Five days were
    # lost that way (2026-03-28, 04-14, 04-19, 04-29, 05-03), inside a range
    # the cursor already reports as done. None means failure; [] still means
    # a real empty window.
    return None


def _write_window(day_tag: str, records: List[Dict]) -> Path:
    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKFILL_DIR / f"{day_tag}.jsonl.gz"
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


def main() -> int:
    end = datetime.fromisoformat(DEFAULT_END + "T00:00:00")
    try:
        cursor = _load_cursor()
    except CursorUnavailable as exc:
        print(f"[backfill] REFUSING TO RUN: {exc}", file=sys.stderr)
        return 4
    # Monotonic publish guard. The frontier may only move FORWARD. Recorded
    # here (before any query) and re-checked before the cursor is written, so
    # no code path — restore failure, corrupt file, a future edit — can
    # publish a cursor earlier than the one this run started from.
    global _CURSOR_FLOOR
    _CURSOR_FLOOR = cursor
    if cursor >= end:
        print(f"[backfill] cursor {cursor.isoformat()} already past end {end.isoformat()}; done")
        _save_cursor(cursor, {"status": "complete"})
        return 0

    session = _login()
    if session is None:
        return 2

    window = timedelta(hours=WINDOW_HOURS)
    saved_total = 0
    # Per-window record counts, published in the manifest so a truncated
    # window (a 200 response that parses but carries far fewer records than
    # its neighbours) is auditable after the fact instead of being guessed
    # at from file sizes.
    per_window = {}
    query_failed = None
    try:
        for i in range(DAYS_PER_RUN):
            if cursor >= end:
                break
            win_end = min(cursor + window, end)
            day_tag = cursor.strftime("%Y-%m-%d")
            print(f"[backfill] window {i+1}/{DAYS_PER_RUN}: "
                  f"[{cursor.isoformat()} → {win_end.isoformat()})")
            records = _query_window(session, cursor, win_end)
            if records is None:
                # Query failure, NOT an empty day. Leave the cursor where it
                # is and stop this run: the next scheduled run re-attempts
                # the same window. Advancing here is what permanently lost
                # five EPOCH-days. Stopping (rather than continuing to the
                # next window) also keeps the cursor a single contiguous
                # frontier, which is the invariant _load_cursor relies on.
                print(f"[backfill]   QUERY FAILED for {day_tag} — cursor "
                      f"stays at {cursor.isoformat()}, will retry next run",
                      file=sys.stderr)
                query_failed = day_tag
                break
            print(f"[backfill]   got {len(records)} records")
            if records:
                path = _write_window(day_tag, records)
                print(f"[backfill]   wrote {path} ({path.stat().st_size} bytes)")
                saved_total += len(records)
            per_window[day_tag] = len(records)
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
        "per_window_records": per_window,
        "query_failed_day": query_failed,
        "files": sorted(p.name for p in BACKFILL_DIR.glob("*.jsonl.gz")),
    }
    # _write_window is the only other thing that creates this directory, so a
    # run whose FIRST window fails (or is genuinely empty) would otherwise
    # crash here with FileNotFoundError instead of reporting the failure.
    BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
    (BACKFILL_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[backfill] DONE records={saved_total} next={cursor.isoformat()}"
          + (f" QUERY_FAILED={query_failed}" if query_failed else ""))
    # A failed window is a real failure: surface it as a non-zero exit so the
    # run shows red instead of looking like a clean short run.
    return 3 if query_failed else 0


if __name__ == "__main__":
    sys.exit(main())
