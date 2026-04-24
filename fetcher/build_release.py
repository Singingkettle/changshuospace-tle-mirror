"""
Walk the data/ folder produced by fetch_celestrak.py + fetch_spacetrack.py,
write `data/manifest.json` describing every asset (sha256 + size + record
count), and print the list of files to upload.

The actual `gh release create / upload` happens in the GitHub Actions
workflow (refresh.yml); this script just produces deterministic metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
SCHEMA_VERSION = 1

# Public download URL pattern for assets attached to a Release tag.
# Owner / repo / tag are filled at runtime from GH context.
URL_TEMPLATE = (
    "https://github.com/{owner}/{repo}/releases/download/{tag}/{asset}"
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return len(payload) if isinstance(payload, list) else -1
    except Exception:
        return -1


def main() -> int:
    if not DATA_DIR.exists():
        print(f"[build_release] {DATA_DIR} missing — nothing to publish")
        return 1

    owner = os.environ.get("GH_OWNER", "OWNER")
    repo = os.environ.get("GH_REPO", "REPO")
    tag = os.environ.get("GH_TAG", "latest")

    groups: Dict[str, Dict] = {}
    satcat_meta = None
    jcat_meta = None
    assets: List[str] = []

    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        meta = {
            "url": URL_TEMPLATE.format(
                owner=owner, repo=repo, tag=tag, asset=path.name
            ),
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "record_count": _record_count(path),
            "fetched_at_utc": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        slug = path.stem
        if slug == "satcat":
            meta["source"] = "spacetrack"
            satcat_meta = meta
        elif slug == "jcat_status":
            meta["source"] = "planet4589"
            # record_count for dict-shaped payloads
            try:
                with path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                meta["record_count"] = (
                    len(payload) if isinstance(payload, dict) else -1
                )
            except Exception:
                meta["record_count"] = -1
            jcat_meta = meta
        else:
            meta["source"] = "celestrak"
            groups[slug] = meta
        assets.append(str(path))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "owner": owner,
        "repo": repo,
        "tag": tag,
        "groups": groups,
    }
    if satcat_meta is not None:
        manifest["satcat"] = satcat_meta
    if jcat_meta is not None:
        manifest["jcat_status"] = jcat_meta

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    assets.append(str(MANIFEST_PATH))

    print(f"[build_release] manifest -> {MANIFEST_PATH}")
    print(f"[build_release] {len(groups)} groups, "
          f"satcat={'yes' if satcat_meta else 'no'}")
    # Emit list for the workflow step to pick up via $GITHUB_OUTPUT.
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"asset_list<<EOF\n" + "\n".join(assets) + "\nEOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
