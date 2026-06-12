#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 <tag> <title> <notes> <target-sha> <data-dir>" >&2
  exit 64
fi

TAG="$1"
TITLE="$2"
NOTES="$3"
TARGET_SHA="$4"
DATA_DIR="$5"
MANIFEST="${DATA_DIR}/manifest.json"

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "GH_TOKEN is required" >&2
  exit 65
fi
if [[ ! -d "$DATA_DIR" ]]; then
  echo "data dir not found: $DATA_DIR" >&2
  exit 66
fi
if [[ ! -s "$MANIFEST" ]]; then
  echo "manifest missing or empty: $MANIFEST" >&2
  exit 67
fi

ensure_release() {
  if gh release view "$TAG" >/dev/null 2>&1; then
    gh release edit "$TAG" --title "$TITLE" --notes "$NOTES" --target "$TARGET_SHA"
  else
    gh release create "$TAG" --title "$TITLE" --notes "$NOTES" --target "$TARGET_SHA"
  fi
}

upload_one() {
  local asset="$1"
  local attempt=1
  while true; do
    echo "upload ${TAG}: ${asset} (attempt ${attempt})"
    if gh release upload "$TAG" "$asset" --clobber; then
      return 0
    fi
    if [[ "$attempt" -ge 3 ]]; then
      echo "failed to upload ${asset} after ${attempt} attempts" >&2
      return 1
    fi
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}

ensure_release

mapfile -t ASSETS < <(find "$DATA_DIR" -maxdepth 1 -type f -name "*.json" ! -name "manifest.json" | sort)
if [[ "${#ASSETS[@]}" -eq 0 ]]; then
  echo "no JSON assets found in ${DATA_DIR}" >&2
  exit 68
fi

# Upload data assets first. Only publish the manifest after every referenced
# asset has uploaded successfully, so clients never consume a half-new release.
for asset in "${ASSETS[@]}"; do
  upload_one "$asset"
done
upload_one "$MANIFEST"
