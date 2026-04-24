# changshuospace-tle-mirror

GitHub Actions–driven mirror of CelesTrak + Space-Track GP/SATCAT data,
re-published every 6 hours as a rolling **`latest`** Release plus a daily
date-stamped tag for rollback.

The China-side production server (`backend/services/data_puller.py`) pulls
JSON assets from this Release through several CDNs (raw.githubusercontent,
ghproxy, jsdelivr, OSS), so it never has to talk to CelesTrak / Space-Track
directly. Any one Action failure is just a missed cycle — the puller falls
back to the previous Release.

## Layout

```
.github/workflows/refresh.yml      # cron: 0 */6 * * *
fetcher/
  constellations.py                # group → CelesTrak slug + name patterns
  fetch_celestrak.py               # GP JSON per group via gp.php?FORMAT=json
  fetch_spacetrack.py              # SATCAT (1×/day) + GP fallback for misses
  build_release.py                 # write data/<group>.json + manifest.json
  requirements.txt
data/                              # generated (gitignored, only present in releases)
```

## How to deploy

1. Create an empty repo on GitHub, e.g. `your-org/changshuospace-tle-mirror`
2. `git init && git add . && git commit -m "init"` in this folder, then
   `git remote add origin git@github.com:your-org/changshuospace-tle-mirror.git`
   and `git push -u origin main`
3. In GitHub repo Settings → Secrets and variables → Actions, add:
   - `SPACETRACK_USER` — Space-Track account #1 (mirror-only, separate from
     the China server's emergency pool)
   - `SPACETRACK_PASS`
4. Settings → Actions → General → Workflow permissions → "Read and write"
   so the workflow can publish Releases.
5. The first cron run (or `Run workflow` from the Actions tab) will publish
   release `latest` with `manifest.json` + per-group JSON assets.

## Output schema

`manifest.json`

```json
{
  "generated_at_utc": "2026-04-23T03:14:00Z",
  "schema_version": 1,
  "groups": {
    "starlink": {
      "url": "https://github.com/<owner>/<repo>/releases/download/latest/starlink.json",
      "sha256": "...",
      "record_count": 5832,
      "fetched_at_utc": "2026-04-23T03:13:51Z",
      "source": "celestrak"
    },
    ...
  },
  "satcat": {
    "url": "...",
    "sha256": "...",
    "record_count": 60123,
    "fetched_at_utc": "...",
    "source": "spacetrack"
  }
}
```

`<group>.json` — array of GP records compatible with Space-Track's
`/basicspacedata/query/class/gp/...` JSON output, i.e. the same shape that
`backend/services/tle_service.py:_store_single_gp_record` already consumes:

```json
[
  {
    "NORAD_CAT_ID": "25544",
    "OBJECT_NAME": "ISS (ZARYA)",
    "TLE_LINE1": "1 25544U ...",
    "TLE_LINE2": "2 25544 ...",
    "EPOCH": "2026-04-23T01:23:45.678901",
    "MEAN_MOTION": "15.49...",
    "INCLINATION": "51.6...",
    "APOAPSIS": "...",
    "PERIAPSIS": "...",
    "ECCENTRICITY": "...",
    "SEMIMAJOR_AXIS": "...",
    "INTLDES": "..."
  }
]
```
