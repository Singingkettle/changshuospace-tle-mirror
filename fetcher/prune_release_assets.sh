#!/usr/bin/env bash
# Keep the rolling backfill release under GitHub's hard cap of 1000 assets
# per release. Once full, every new day-file upload is rejected, the publish
# step fails, the cursor never advances, and the next run re-fetches the same
# window from Space-Track -- burning quota for nothing (11 wasted runs,
# 2026-09-07..10). Run this BEFORE the fetch so headroom is guaranteed before
# any Space-Track query is spent.
#
# Retention is by asset created_at (oldest published first). That is only a
# sound age signal because upload_release_assets.sh no longer re-clobbers
# unchanged day files. The local ingester drains new files within ~6h (and
# the watchdog kicks it every 8h), so anything published more than a day ago
# is already in tle_history; keeping the newest KEEP files leaves a ~2-week
# buffer on top. Never touches non-day assets (cursor, manifest, reset).
#
# usage: prune_release_assets.sh <tag> <keep> <trigger>   (DRY_RUN=1 lists only)
set -euo pipefail
TAG="${1:?tag}"; KEEP="${2:?keep}"; TRIGGER="${3:?trigger}"
REPO_SLUG="${GITHUB_REPOSITORY:-${REPO:?set GITHUB_REPOSITORY or REPO}}"

# Resolve the numeric release id, then page through its assets. The
# release-by-tag object does embed the full asset list today (verified at
# 1000), but the dedicated assets endpoint is the documented paginated path
# and the only one guaranteed not to truncate. Asset ids here are the numeric
# REST ids the DELETE endpoint needs (gh release view --json gives node ids,
# which DELETE answers with 404 -- bitten by that on 2026-09-10).
RELEASE_ID="$(gh api "repos/${REPO_SLUG}/releases/tags/${TAG}" --jq '.id')"
mapfile -t ROWS < <(gh api "repos/${REPO_SLUG}/releases/${RELEASE_ID}/assets?per_page=100" --paginate \
  --jq '.[] | select(.name | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}\\.jsonl\\.gz$"))
        | [.created_at, .id, .name] | @tsv' | sort)
COUNT="${#ROWS[@]}"
echo "prune ${TAG}: ${COUNT} day file(s) on release (keep=${KEEP}, trigger=${TRIGGER})"
if (( COUNT <= TRIGGER )); then
  echo "under trigger -- nothing to prune"
  exit 0
fi
DELETE_N=$(( COUNT - KEEP ))
echo "deleting the ${DELETE_N} oldest-published day file(s)"
for row in "${ROWS[@]:0:DELETE_N}"; do
  IFS=$'\t' read -r created id name <<< "$row"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  [dry-run] would delete ${name} (published ${created})"
  else
    gh api -X DELETE "repos/${REPO_SLUG}/releases/assets/${id}" >/dev/null
    echo "  deleted ${name} (published ${created})"
  fi
done
