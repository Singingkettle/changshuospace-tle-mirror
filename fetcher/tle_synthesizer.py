"""Synthesize TLE_LINE1 / TLE_LINE2 from OMM-only records.

Space-Track / CelesTrak occasionally publish brand-new launches as
OMM-only records: NORAD_CAT_ID, OBJECT_NAME, EPOCH, MEAN_MOTION,
ECCENTRICITY, INCLINATION, RA_OF_ASC_NODE, ARG_OF_PERICENTER,
MEAN_ANOMALY, BSTAR are all set, but TLE_LINE1 / TLE_LINE2 are absent
because the plain TLE strings haven't been generated yet.

Downstream consumers of the mirror that propagate orbits with SGP4
(satellite.js in the browser, sgp4 in Python) need the TLE strings.
OMM elements are mathematically equivalent to a TLE pair, so we can
losslessly export the strings via ``sgp4.exporter.export_tle``. A
round-trip through ``Satrec.twoline2rv`` reproduces the original
position to sub-millimetre precision.

Run this on every batch of records before writing ``data/<slug>.json``
so all downstream consumers receive TLE-complete payloads.
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional, Tuple

try:
    from sgp4.api import Satrec
    from sgp4 import omm as sgp4_omm
    from sgp4 import exporter as sgp4_exporter
    _SGP4_AVAILABLE = True
except ImportError:
    _SGP4_AVAILABLE = False
    Satrec = None  # type: ignore
    sgp4_omm = None  # type: ignore
    sgp4_exporter = None  # type: ignore

# Minimum OMM keys required for sgp4.omm.initialize.
_REQUIRED_KEYS = (
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "NORAD_CAT_ID",
)


def _has_required_keys(record: Dict) -> bool:
    for key in _REQUIRED_KEYS:
        if record.get(key) in (None, "", 0):
            return False
    return True


def synthesize_tle(record: Dict) -> Optional[Tuple[str, str]]:
    """Return ``(line1, line2)`` for an OMM-only record, or None if
    the OMM is incomplete or sgp4 rejects it."""
    if not _SGP4_AVAILABLE or not _has_required_keys(record):
        return None
    try:
        sat = Satrec()
        sgp4_omm.initialize(sat, record)
        line1, line2 = sgp4_exporter.export_tle(sat)
    except Exception as exc:
        print(f"[tle_synth] NORAD {record.get('NORAD_CAT_ID')} failed: {exc}",
              file=sys.stderr)
        return None
    if not line1 or not line2 or len(line1) < 60 or len(line2) < 60:
        return None
    return line1, line2


def fill_missing_tle_lines(records: List[Dict]) -> Tuple[List[Dict], int]:
    """In-place: for every record missing TLE_LINE1/TLE_LINE2, synthesise
    them from OMM. Returns (records, number_filled)."""
    if not _SGP4_AVAILABLE:
        print("[tle_synth] sgp4 not installed; skipping synthesis",
              file=sys.stderr)
        return records, 0
    filled = 0
    for r in records:
        if r.get("TLE_LINE1") and r.get("TLE_LINE2"):
            continue
        synth = synthesize_tle(r)
        if synth:
            r["TLE_LINE1"], r["TLE_LINE2"] = synth
            filled += 1
    return records, filled
