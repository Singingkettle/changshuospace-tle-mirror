"""Regression tests for the gap-repair queue.

The frontier and the queue are two independent pointers sharing one state
file, and the interesting failure is them contaminating each other. The
frontier carries a hard monotonic guard (it may only move forward, because a
rewind silently re-queries and can strand days); a repair day sits in 2001 or
2017 and must therefore never be written into it.

Also pins the rolling end. A hard-coded BACKFILL_END turns this job into a
permanently successful no-op the day the cursor reaches it — the run exits 0
and publishes status=complete while coverage quietly stops growing. The
previous value was about four days from doing exactly that.

Hermetic: no network, no account, temp dirs only.
"""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_gp_history", Path(__file__).with_name("backfill_gp_history.py"))
bf = importlib.util.module_from_spec(_SPEC)
sys.modules["backfill_gp_history"] = bf
_SPEC.loader.exec_module(bf)


def _start_day(url: str) -> str:
    m = re.search(r"EPOCH/(?:%3E)?(\d{4}-\d{2}-\d{2})", url)
    return m.group(1) if m else "?"


class _OkSession:
    """Every window returns one record; records which days were asked for."""
    headers: dict = {}

    def __init__(self):
        self.days = []

    def get(self, url, timeout=None):
        self.days.append(_start_day(url))

        class R:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return [{"NORAD_CAT_ID": 1, "EPOCH": "2001-05-01T00:00:00"}]
        return R()


class RollingEndTests(unittest.TestCase):
    def test_end_tracks_today_and_is_never_fixed(self):
        end = bf._rolling_end()
        expected = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertEqual(end, expected)
        # The specific value that was about to strand the job.
        self.assertNotEqual(end, "2026-07-31")


class QueuePlanTests(unittest.TestCase):
    SEGS = [
        {"label": "a", "start": "2017-03-01", "end": "2017-03-05"},
        {"label": "b", "start": "2001-05-01", "end": "2001-05-03"},
    ]

    def test_starts_at_the_first_segment(self):
        idx, day, seg_end = bf._queue_plan(self.SEGS, 0, None)
        self.assertEqual((idx, day.date().isoformat()), (0, "2017-03-01"))
        self.assertEqual(seg_end.date().isoformat(), "2017-03-05")

    def test_rolls_into_the_next_segment_when_one_is_exhausted(self):
        done = datetime.fromisoformat("2017-03-05T00:00:00")
        idx, day, _ = bf._queue_plan(self.SEGS, 0, done)
        self.assertEqual((idx, day.date().isoformat()), (1, "2001-05-01"))

    def test_returns_none_when_everything_is_done(self):
        done = datetime.fromisoformat("2001-05-03T00:00:00")
        self.assertIsNone(bf._queue_plan(self.SEGS, 1, done))

    def test_skips_a_malformed_segment_instead_of_dying(self):
        segs = [{"label": "bad", "start": "not-a-date", "end": "x"}] + self.SEGS
        idx, day, _ = bf._queue_plan(segs, 0, None)
        self.assertEqual((idx, day.date().isoformat()), (1, "2017-03-01"))


class QueueDoesNotMoveTheFrontierTests(unittest.TestCase):
    """The property that protects the archive."""

    def _run_with_queue(self, segments, frontier_day):
        tmp = Path(tempfile.mkdtemp())
        bf.DATA_DIR = tmp
        bf.BACKFILL_DIR = tmp / "backfill"
        bf.CURSOR_PATH = tmp / "backfill_cursor.json"
        bf.QUEUE_PATH = tmp / "backfill_queue.json"
        bf.QUEUE_PATH.write_text(json.dumps({"segments": segments}))
        # Frontier already at the end -> queue mode.
        bf.CURSOR_PATH.write_text(json.dumps({"next_epoch": frontier_day}))
        bf.DEFAULT_END = frontier_day[:10]
        bf.DAYS_PER_RUN = 2
        bf.BETWEEN_S = 0
        bf.RETRY_BACKOFF_S = 0
        bf._CURSOR_FLOOR = None

        session = _OkSession()
        bf._login = lambda: session
        bf._logout = lambda s: None
        rc = bf.main()
        cursor = json.loads(bf.CURSOR_PATH.read_text())
        return rc, cursor, session.days

    def test_repair_days_advance_the_queue_not_the_frontier(self):
        frontier = "2026-08-01T00:00:00"
        rc, cursor, days = self._run_with_queue(
            [{"label": "old", "start": "2001-05-01", "end": "2001-06-01"}],
            frontier)

        self.assertEqual(rc, 0)
        # It really did fetch 2001, which is the whole point...
        self.assertEqual(days[:2], ["2001-05-01", "2001-05-02"])
        # ...and the frontier did NOT follow it back in time.
        self.assertEqual(cursor["next_epoch"], frontier)
        self.assertEqual(cursor["queue_index"], 0)
        self.assertEqual(cursor["queue_next"], "2001-05-03T00:00:00")

    def test_empty_queue_is_a_clean_no_op(self):
        rc, cursor, days = self._run_with_queue([], "2026-08-01T00:00:00")
        self.assertEqual(rc, 0)
        self.assertEqual(days, [], "must not query anything with no work")
        self.assertEqual(cursor["status"], "complete")


if __name__ == "__main__":
    unittest.main()
