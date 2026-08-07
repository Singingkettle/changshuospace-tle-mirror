#!/usr/bin/env python3
"""Build the gateways release manifest (plan §3.1 — own tag, own manifest).

Mirrors build_release.py's contract exactly so the China-side puller can
reuse the same verification discipline: every asset gets a sha256 or the
build FAILS (exit 2); the manifest carries url/sha256/size/record_count per
asset under a ``sources`` map keyed by asset stem. Separate tag
(``gateways-latest``) so weekly gateway assets never mix into the hourly TLE
``latest`` upload stream and the two manifests' key spaces cannot collide.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

DATA_DIR = Path(__file__).resolve().parent.parent / "gateway_data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
SCHEMA_VERSION = 1
URL_TEMPLATE = ("https://github.com/{owner}/{repo}/releases/download/"
                "{tag}/{asset}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    owner = os.environ.get("GH_OWNER", "Singingkettle")
    repo = os.environ.get("GH_REPO", "changshuospace-tle-mirror")
    tag = os.environ.get("GH_TAG", "gateways-latest")

    if not DATA_DIR.exists():
        print(f"[gateways-release] {DATA_DIR} missing — no fetcher output")
        return 2

    sources: Dict[str, Dict] = {}
    failed = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            sha = _sha256(path)
        except Exception as e:
            failed.append(f"{path.name}: {e}")
            continue
        try:
            payload = json.loads(path.read_text())
            n = payload.get("n_rows", -1)
        except Exception:
            n = -1
        sources[path.stem] = {
            "url": URL_TEMPLATE.format(owner=owner, repo=repo, tag=tag,
                                       asset=path.name),
            "sha256": sha,
            "size_bytes": path.stat().st_size,
            "record_count": n,
            "fetched_at_utc": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    if failed:
        # Same policy as build_release.py 2026-04-24: never publish an asset
        # the puller would have to trust without a hash.
        print(f"[gateways-release] sha256 FAILED for: {failed}")
        return 2
    if not sources:
        print("[gateways-release] no assets found — refusing to publish an "
              "empty manifest (a diff would read it as mass removal)")
        return 2

    MANIFEST_PATH.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "owner": owner, "repo": repo, "tag": tag,
        "sources": sources,
    }, ensure_ascii=False, indent=2))
    print(f"[gateways-release] manifest with {len(sources)} assets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
