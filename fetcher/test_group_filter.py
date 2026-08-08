"""The catch-all group filter (filter_patterns).

Pins the fix for the defect where `lynk` borrowed CelesTrak's "other-comm"
catch-all and published all 32 of its members — only 4 of them Lynk — so 26
SES O3b/O3b-mPOWER and Telesat LEO 1 were filed under the wrong constellation
downstream and oscillated as groups re-ingested.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "fetch_celestrak", Path(__file__).with_name("fetch_celestrak.py"))
fc = importlib.util.module_from_spec(_SPEC)
sys.modules["fetch_celestrak"] = fc
_SPEC.loader.exec_module(fc)

GROUP_RECORDS = [
    {"NORAD_CAT_ID": 1, "OBJECT_NAME": "LYNK TOWER 1"},
    {"NORAD_CAT_ID": 2, "OBJECT_NAME": "O3B FM5"},
    {"NORAD_CAT_ID": 3, "OBJECT_NAME": "O3B MPOWER 1"},
    {"NORAD_CAT_ID": 4, "OBJECT_NAME": "TELESAT LEO 1"},
    {"NORAD_CAT_ID": 5, "OBJECT_NAME": "LYNK TOWER 2"},
]


class GroupFilterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        fc._request = lambda params: (
            self.calls.append(params) or list(GROUP_RECORDS))
        fc.fetch_supgp = lambda slug: []
        fc.THROTTLE_SEC = 0

    def test_catchall_group_is_filtered_to_the_slug(self):
        out = fc.fetch_group("lynk", {"group": "other-comm",
                                      "patterns": ["LYNK"],
                                      "filter_patterns": True})
        names = sorted(r["OBJECT_NAME"] for r in out)
        self.assertEqual(names, ["LYNK TOWER 1", "LYNK TOWER 2"])

    def test_specific_group_is_untouched_without_the_flag(self):
        """A real operator group must NOT be filtered — that would drop
        satellites whose names do not happen to contain the slug string."""
        out = fc.fetch_group("ses", {"group": "ses", "patterns": ["SES"]})
        self.assertEqual(len(out), len(GROUP_RECORDS))

    def test_patterns_still_work_as_the_zero_result_fallback(self):
        fc._request = lambda params: ([] if "GROUP" in params
                                      else list(GROUP_RECORDS))
        out = fc.fetch_group("lynk", {"group": "nonexistent",
                                      "patterns": ["LYNK"]})
        self.assertEqual(len(out), len(GROUP_RECORDS))


if __name__ == "__main__":
    unittest.main()
