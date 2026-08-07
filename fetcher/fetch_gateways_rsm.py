#!/usr/bin/env python3
"""RSM NZ per-station poller — CONFIG-DRIVEN SKELETON (plan §3.2 P3).

Honest scope note (2026-08-07): the plan said "poll the 6 known
rrf.rsm.govt.nz station IDs", but the six curated NZ gateway rows all cite
the RRL SEARCH ENTRY POINT, not per-station IDs — there are no known IDs to
poll. Bulk Data Extracts need an RSM-approved account, and the SMART portal
is a stateful WDK app that polite automation should not screen-scrape.

So this fetcher is a skeleton: it reads ``rsm_stations.json`` (licence_id ->
expected site) and polls ONLY entries a human has added after looking the
licences up manually in the RRF. With the default empty config it no-ops
with a clear message and exit 0 — the workflow stays green and honest
instead of pretending coverage.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(__file__).with_name("rsm_stations.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    stations = []
    if CONFIG.exists():
        stations = json.loads(CONFIG.read_text()).get("stations", [])
    if not stations:
        print("[rsm] no station IDs configured (rsm_stations.json empty) — "
              "nothing to poll; see the module docstring. Skipping cleanly.")
        args.out.write_text(json.dumps({
            "source": "rsm_nz_rrf",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "n_rows": 0, "rows": [],
            "note": "skeleton — no per-station licence IDs known yet",
        }, indent=2))
        return 0

    # Per-station polling lands here once IDs exist (portal API per plan
    # §2.1 P3: portal.api.business.govt.nz/api/radiospectrum-management).
    print(f"[rsm] {len(stations)} configured stations — polling not yet "
          f"implemented; refusing to guess an API contract.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
