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
  local attempt=1
  while true; do
    if gh release view "$TAG" >/dev/null 2>&1; then
      # Metadata-only refresh (title/notes/target). GitHub intermittently
      # answers this PATCH with 403 "Resource not accessible by integration"
      # even though the token has contents:write (observed 2026-07-31, run
      # 30621946641). The release and its assets are unaffected, so after the
      # retries degrade to a warning instead of failing the whole refresh.
      if gh release edit "$TAG" --title "$TITLE" --notes "$NOTES" --target "$TARGET_SHA"; then
        return 0
      fi
      if [[ "$attempt" -ge 3 ]]; then
        echo "WARN: 'gh release edit $TAG' failed ${attempt} times; keeping existing release metadata and continuing with asset upload" >&2
        return 0
      fi
    else
      if gh release create "$TAG" --title "$TITLE" --notes "$NOTES" --target "$TARGET_SHA"; then
        return 0
      fi
      # Without a release the asset uploads cannot succeed, so creation
      # failures stay fatal -- but give transient API errors a chance first.
      if [[ "$attempt" -ge 3 ]]; then
        echo "failed to create release ${TAG} after ${attempt} attempts" >&2
        return 1
      fi
    fi
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
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

# Day files are immutable once published (a given EPOCH day's gp_history does
# not change), yet every run used to re-download all of them in the restore
# step and re-upload them here with --clobber: ~2,000 API calls per run, and
# -- because --clobber deletes and re-creates the asset -- it reset every
# asset's created_at, which made age-based retention impossible. It is also
# how the rolling release was driven straight into GitHub's 1000-asset cap.
# Skip a *.jsonl.gz whose name is already on the release with the same size;
# a repair run (re-fetched day) yields a different size and still uploads.
# Cursor / manifest / other .json are small and change every run: always upload.
declare -A REMOTE_SIZE
while IFS=$'\t' read -r name size; do
  [[ -n "$name" ]] && REMOTE_SIZE["$name"]="$size"
done < <(gh api "repos/${GITHUB_REPOSITORY:-${REPO:-}}/releases/tags/${TAG}" \
           --jq '.assets[] | "\(.name)\t\(.size)"' 2>/dev/null || true)
echo "remote assets on ${TAG}: ${#REMOTE_SIZE[@]}"

mapfile -t ASSETS < <(
  find "$DATA_DIR" -maxdepth 1 -type f \( \
    -name "*.jsonl.gz" -o \
    \( -name "*.json" ! -name "manifest.json" \) \
  \) | sort
)
if [[ "${#ASSETS[@]}" -eq 0 ]]; then
  echo "no backfill assets (*.jsonl.gz / *.json) found in ${DATA_DIR}" >&2
  exit 68
fi

# Upload data assets first (jsonl.gz day files + cursor). Only publish the
# manifest after every referenced asset has uploaded successfully, so clients
# never consume a half-new release.
SKIPPED=0
for asset in "${ASSETS[@]}"; do
  base="$(basename "$asset")"
  if [[ "$base" == *.jsonl.gz && -n "${REMOTE_SIZE[$base]:-}" \
        && "${REMOTE_SIZE[$base]}" == "$(stat -c %s "$asset")" ]]; then
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  upload_one "$asset"
done
echo "skipped ${SKIPPED} already-published day file(s)"
upload_one "$MANIFEST"
