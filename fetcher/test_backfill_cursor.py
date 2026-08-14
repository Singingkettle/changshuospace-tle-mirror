"""Regression tests for the backfill cursor semantics.

Pins the fix for the defect that permanently lost five EPOCH-days
(2026-03-28, 04-14, 04-19, 04-29, 05-03): ``_query_window`` returned ``[]``
for every failure mode, which was indistinguishable from a genuinely empty
day, and ``main()`` advanced the cursor either way — so a transient
Space-Track error skipped that day forever inside a range the cursor then
reported as done.

The two semantics that must hold, and that this file keeps apart:
  * query FAILURE  -> cursor does not move, run exits non-zero, retried next run
  * genuinely EMPTY day -> cursor advances normally, run is a success

Hermetic: no network, no Space-Track account, temp dirs only.
"""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_gp_history", Path(__file__).with_name("backfill_gp_history.py"))
bf = importlib.util.module_from_spec(_SPEC)
sys.modules["backfill_gp_history"] = bf
_SPEC.loader.exec_module(bf)

FAIL_DAY = "2026-03-02"


def _start_day(url: str) -> str:
    """The window START date encoded in either predicate form.

    Matching on a bare substring is wrong: the ``alt`` form spells the range
    as ``EPOCH/<start>--<end>``, so the END date also appears in the URL and
    a naive fake fails the PREVIOUS window too.
    """
    m = re.search(r"EPOCH/(?:%3E)?(\d{4}-\d{2}-\d{2})", url)
    return m.group(1) if m else "?"


class _Resp:
    def __init__(self, ok: bool):
        self.status_code = 200 if ok else 500
        self.text = "upstream error"

    def json(self):
        return [{"NORAD_CAT_ID": 1, "EPOCH": "2026-03-01T00:00:00"}]


class _FailOnDay:
    """Fails BOTH predicate forms for one window; succeeds elsewhere."""
    headers: dict = {}

    def get(self, url, timeout=None):
        return _Resp(_start_day(url) != FAIL_DAY)


class _AllEmpty:
    """Every window is a real, successful, empty day."""
    headers: dict = {}

    def get(self, url, timeout=None):
        r = _Resp(True)
        r.json = lambda: []
        return r


class BackfillCursorTests(unittest.TestCase):
    def _run(self, session):
        tmp = Path(tempfile.mkdtemp())
        bf.DATA_DIR = tmp
        bf.BACKFILL_DIR = tmp / "backfill"
        bf.CURSOR_PATH = tmp / "cur.json"
        bf.DEFAULT_START = "2026-03-01"
        bf.DEFAULT_END = "2026-03-05"
        bf.DAYS_PER_RUN = 3
        bf.BETWEEN_S = 0
        bf._login = lambda: session
        bf._logout = lambda s: None
        rc = bf.main()
        cursor = (json.loads((tmp / "cur.json").read_text())["next_epoch"]
                  if (tmp / "cur.json").exists() else None)
        files = sorted(p.name for p in (tmp / "backfill").glob("*.jsonl.gz"))
        manifest = json.loads((tmp / "backfill" / "manifest.json").read_text())
        return rc, cursor, files, manifest

    def test_query_failure_does_not_advance_the_cursor(self):
        rc, cursor, files, manifest = self._run(_FailOnDay())
        self.assertEqual(rc, 3, "a failed window must exit non-zero")
        self.assertTrue(cursor.startswith(FAIL_DAY),
                        f"cursor must stay on the failed day, got {cursor}")
        self.assertEqual(files, ["2026-03-01.jsonl.gz"],
                         "only the successful window may be written")
        self.assertEqual(manifest["query_failed_day"], FAIL_DAY)

    def test_genuinely_empty_days_still_advance(self):
        rc, cursor, files, manifest = self._run(_AllEmpty())
        self.assertEqual(rc, 0, "empty is not failure")
        self.assertTrue(cursor.startswith("2026-03-04"),
                        f"cursor must advance past empty days, got {cursor}")
        self.assertEqual(files, [])
        self.assertIsNone(manifest["query_failed_day"])
        self.assertEqual(manifest["per_window_records"],
                         {"2026-03-01": 0, "2026-03-02": 0, "2026-03-03": 0},
                         "per-window counts make a truncated window auditable")

    def test_manifest_survives_a_first_window_failure(self):
        """The run must report the failure, not crash writing its manifest.

        _write_window is the only other thing that creates BACKFILL_DIR, so
        before the fix a run whose first window failed died with
        FileNotFoundError and the real cause never reached the log.
        """
        global FAIL_DAY
        original, FAIL_DAY = FAIL_DAY, "2026-03-01"
        try:
            rc, cursor, files, manifest = self._run(_FailOnDay())
        finally:
            FAIL_DAY = original
        self.assertEqual(rc, 3)
        self.assertEqual(files, [])
        self.assertEqual(manifest["query_failed_day"], "2026-03-01")
        self.assertIsNone(cursor, "no window succeeded, so no cursor is saved")




class CursorRewindGuardTests(unittest.TestCase):
    """The frontier must never move backward.

    Measured 2026-08-08: `_load_cursor` fell back to DEFAULT_START on ANY
    failure, and the workflow's restore step swallowed download failures with
    `|| true`. One failed `gh release download` therefore resumed the sweep at
    2026-02-27, re-queried ~100 finished EPOCH-days, and PUBLISHED that
    rewound cursor — the repair-run guard does not apply because a scheduled
    run passes no `start` override. Reachable with no human action.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bf.DATA_DIR = self.tmp
        bf.BACKFILL_DIR = self.tmp / "backfill"
        bf.CURSOR_PATH = self.tmp / "cur.json"
        bf.DEFAULT_START = "2026-02-27"
        bf._CURSOR_FLOOR = None
        self._env = dict(bf.os.environ)

    def tearDown(self):
        bf.os.environ.clear()
        bf.os.environ.update(self._env)
        bf._CURSOR_FLOOR = None

    def test_corrupt_cursor_refuses_instead_of_rewinding(self):
        bf.CURSOR_PATH.write_text("{ this is not json")
        with self.assertRaises(bf.CursorUnavailable):
            bf._load_cursor()

    def test_failed_restore_refuses_instead_of_rewinding(self):
        """No cursor file AND the workflow says one exists upstream."""
        bf.os.environ["BACKFILL_CURSOR_RESTORED"] = "0"
        with self.assertRaises(bf.CursorUnavailable):
            bf._load_cursor()

    def test_genuine_first_run_still_starts_at_default(self):
        bf.os.environ["BACKFILL_CURSOR_RESTORED"] = "1"
        self.assertEqual(bf._load_cursor(),
                         datetime.fromisoformat("2026-02-27T00:00:00"))

    def test_save_cursor_refuses_to_move_backward(self):
        bf._CURSOR_FLOOR = datetime.fromisoformat("2026-06-11T00:00:00")
        with self.assertRaises(bf.CursorUnavailable):
            bf._save_cursor(datetime.fromisoformat("2026-02-27T00:00:00"))
        self.assertFalse(bf.CURSOR_PATH.exists(),
                         "a refused save must not write anything")

    def test_save_cursor_allows_forward_and_equal(self):
        bf._CURSOR_FLOOR = datetime.fromisoformat("2026-06-11T00:00:00")
        bf._save_cursor(datetime.fromisoformat("2026-06-11T00:00:00"))
        bf._save_cursor(datetime.fromisoformat("2026-06-14T00:00:00"))
        self.assertTrue(
            json.loads(bf.CURSOR_PATH.read_text())["next_epoch"]
            .startswith("2026-06-14"))

    def test_main_exits_4_and_issues_no_queries_when_cursor_unavailable(self):
        bf.CURSOR_PATH.write_text("{ corrupt")
        calls = []

        class _Sess:
            headers: dict = {}

            def get(self, url, timeout=None):
                calls.append(url)
                raise AssertionError("no query may be issued")

        bf._login = lambda: _Sess()
        bf._logout = lambda s: None
        self.assertEqual(bf.main(), 4)
        self.assertEqual(calls, [], "a refused run must not touch Space-Track")


if __name__ == "__main__":
    unittest.main()

class _AllOk:
    """Every window succeeds with one record."""
    headers: dict = {}

    def get(self, url, timeout=None):
        return _Resp(True)


class FrontierHandoffTests(unittest.TestCase):
    """The run in which the frontier reaches the rolling end must hand its
    REMAINING windows to the repair queue, not throw them away.

    `frontier_done` used to be computed once, before the window loop, so the
    handoff run broke out with (DAYS_PER_RUN - 1) windows unused — measured
    on the live job as 26.4 published files/day against the designed 32,
    a 21% throughput loss on the resource that bounds the whole backfill.
    """

    def test_midrun_handoff_uses_remaining_windows_for_repairs(self):
        tmp = Path(tempfile.mkdtemp())
        bf.DATA_DIR = tmp
        bf.BACKFILL_DIR = tmp / "backfill"
        bf.CURSOR_PATH = tmp / "cur.json"
        bf.QUEUE_PATH = tmp / "queue.json"
        bf.DEFAULT_START = "2026-03-01"
        bf.DEFAULT_END = "2026-03-02"      # frontier: exactly one window left
        bf.DAYS_PER_RUN = 3
        bf.BETWEEN_S = 0
        bf._login = lambda: _AllOk()
        bf._logout = lambda s: None
        bf.QUEUE_PATH.write_text(json.dumps({"segments": [
            {"label": "hole-2010", "start": "2010-01-01", "end": "2010-01-05"},
        ]}))

        rc = bf.main()
        self.assertEqual(rc, 0)
        files = sorted(p.name for p in (tmp / "backfill").glob("*.jsonl.gz"))
        self.assertEqual(
            files,
            ["2010-01-01.jsonl.gz", "2010-01-02.jsonl.gz",
             "2026-03-01.jsonl.gz"],
            "windows 2-3 must be repair work, not discarded",
        )
        cur = json.loads((tmp / "cur.json").read_text())
        self.assertTrue(cur["next_epoch"].startswith("2026-03-02"),
                        "the frontier itself must not move on repair windows")
        self.assertEqual(cur["queue_next"][:10], "2010-01-03")
