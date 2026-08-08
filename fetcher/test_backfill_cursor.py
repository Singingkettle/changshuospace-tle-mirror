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


if __name__ == "__main__":
    unittest.main()
