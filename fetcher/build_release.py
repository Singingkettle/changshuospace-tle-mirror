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

# Assets that are NOT regenerated on every run. satcat.json is refreshed at
# most once per 20h; on the other ~20 runs a day it is simply not in data/.
# Because the manifest is built from a glob of data/, that made the key
# vanish from the manifest even though the asset itself survives on the
# release (`gh release upload --clobber` never deletes it). Consumers read
# the manifest, not the asset list, so satcat became unreachable for ~19 of
# every 20 hours -- and the China-side puller's per-slug state froze at
# whatever its last outcome had been. Carry the previous manifest's entry
# forward for these keys when the file is absent locally.
CARRY_FORWARD_KEYS = ("satcat", "decays", "jcat_status", "gp_catalogue")

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
    gp_catalogue_meta = None
    decays_meta = None
    jcat_meta = None
    assets: List[str] = []

    missing_sha: List[str] = []

    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        sha = _sha256(path)
        if not sha:
            # Should never happen unless _sha256 itself fails. Project policy
            # 2026-04-24: refuse to publish a release that contains an asset
            # without a sha256 -- the China-side puller will reject it anyway,
            # better to fail the workflow loudly here.
            missing_sha.append(path.name)
            continue
        meta = {
            "url": URL_TEMPLATE.format(
                owner=owner, repo=repo, tag=tag, asset=path.name
            ),
            "sha256": sha,
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
        elif slug == "decays":
            meta["source"] = "spacetrack"
            decays_meta = meta
        elif slug == "gp_catalogue":
            # A top-level asset, NOT a constellation group. Without this
            # branch the else-clause below would file it under groups[] and
            # the consumer would ingest a 24k-object superset as if it were
            # one more constellation.
            meta["source"] = "spacetrack"
            gp_catalogue_meta = meta
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
    if gp_catalogue_meta is not None:
        manifest["gp_catalogue"] = gp_catalogue_meta
    if decays_meta is not None:
        manifest["decays"] = decays_meta
    if jcat_meta is not None:
        manifest["jcat_status"] = jcat_meta

    # Carry forward optional assets that this run did not regenerate. The
    # previous manifest is fetched by the workflow ONLY after it has confirmed
    # the corresponding asset still exists on the release, so a carried entry
    # always describes bytes that are actually downloadable, with the sha256
    # they still hash to.
    prev_path = os.environ.get("CARRY_FORWARD_MANIFEST", "").strip()
    if prev_path:
        try:
            prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[build_release] carry-forward manifest unreadable "
                  f"({prev_path}): {exc}")
            prev = {}
        for key in CARRY_FORWARD_KEYS:
            if key in manifest or key not in prev:
                continue
            entry = dict(prev[key])
            entry["carried_forward"] = True
            manifest[key] = entry
            print(f"[build_release] carried forward '{key}' from the previous "
                  f"manifest (not regenerated this run, asset still on the "
                  f"release, sha256={str(entry.get('sha256'))[:12]})")

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    assets.append(str(MANIFEST_PATH))

    print(f"[build_release] manifest -> {MANIFEST_PATH}")
    # Report what the MANIFEST says, not what this run happened to
    # regenerate -- a carried-forward asset is present for consumers.
    def _state(key: str) -> str:
        entry = manifest.get(key)
        if entry is None:
            return "no"
        return "carried" if entry.get("carried_forward") else "yes"

    print(f"[build_release] {len(groups)} groups, "
          f"satcat={_state('satcat')}, decays={_state('decays')}, "
          f"gp_catalogue={_state('gp_catalogue')}")
    # Emit list for the workflow step to pick up via $GITHUB_OUTPUT.
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"asset_list<<EOF\n" + "\n".join(assets) + "\nEOF\n")

    if missing_sha:
        print(
            f"[build_release] FATAL: {len(missing_sha)} asset(s) failed sha256 "
            f"computation: {missing_sha}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
