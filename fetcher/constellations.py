"""
Constellation → CelesTrak group + name patterns mapping.

Mirror of `backend/config.py:Config.CONSTELLATIONS` (only the fields we need:
slug, group, name_patterns). Keeping it isolated here lets the GitHub
Actions workflow run with zero project dependencies.

If the China-side `backend/config.py` adds a constellation, append the same
slug/group/patterns triple here. The data-puller compares the manifest to
its own list and silently skips groups it doesn't recognise.
"""

CONSTELLATIONS = {
    "starlink":   {"group": "starlink",     "patterns": ["STARLINK", "TINTIN"]},
    "oneweb":     {"group": "oneweb",       "patterns": ["ONEWEB"]},
    "kuiper":     {"group": "kuiper",       "patterns": ["KUIPER"]},
    "iridium":    {"group": "iridium-next", "patterns": ["IRIDIUM"]},
    "globalstar": {"group": "globalstar",   "patterns": ["GLOBALSTAR"]},
    "orbcomm":    {"group": "orbcomm",      "patterns": ["ORBCOMM"]},
    "gps":        {"group": "gps-ops",      "patterns": ["NAVSTAR"]},
    "glonass":    {"group": "glo-ops",      "patterns": ["GLONASS"]},
    "galileo":    {"group": "galileo",      "patterns": ["GALILEO"]},
    "beidou":     {"group": "beidou",       "patterns": ["BEIDOU"]},
    "planet":     {"group": "planet",       "patterns": ["FLOCK", "DOVE", "SKYSAT"]},
    "spire":      {"group": "spire",        "patterns": ["SPIRE", "LEMUR"]},
    "intelsat":   {"group": "intelsat",     "patterns": ["INTELSAT"]},
    "ses":        {"group": "ses",          "patterns": ["SES-", "ASTRA"]},
    "stations":   {"group": "stations",     "patterns": ["ISS", "TIANGONG", "CSS"]},
    "swarm":      {"group": "swarm",        "patterns": ["SWARM"]},
    "qianfan":    {"group": "qianfan",      "patterns": ["QIANFAN"]},
    "xingwang":   {"group": "xingwang",     "patterns": [
        "GUOWANG", "SATNET", "CHINA SATNET", "GW-", "HULIANWANG", "JISHU",
    ]},
    # yinhe / lynk: CelesTrak GROUP entry exists but is incomplete (newer
    # launches missing for weeks). force_spacetrack=True asks the workflow's
    # ST fallback step to *always* re-query for these slugs, regardless of
    # whether CelesTrak returned >0 records, so the union is published.
    "yinhe":      {"group": "yinhe",        "patterns": ["GALAXYSPACE", "YINHE"],
                   "force_spacetrack": True},
    "jilin":      {"group": "jilin-1",      "patterns": ["JILIN"]},
    "tianqi":     {"group": "tianqi",       "patterns": ["TIANQI"]},
    "yaogan":     {"group": "yaogan",       "patterns": ["YAOGAN"]},
    "bluewalker": {"group": "ast",          "patterns": ["BLUEWALKER"]},
    "lynk":       {"group": "other-comm",   "patterns": ["LYNK"],
                   "force_spacetrack": True},
    "telesat":    {"group": "telesat",      "patterns": ["TELESAT"]},
    # Newly added — parity with satellitemap.space menu. CelesTrak does not
    # publish a dedicated group for any of these yet, so they fall through
    # to the OBJECT_NAME pattern fallback in fetch_celestrak.py and (when
    # patterns yield 0) the Space-Track gp fetch in refresh.yml.
    "e-space":    {"group": "e-space",      "patterns": ["ESPACE"]},
    "geespace":   {"group": "geespace",     "patterns": ["GEESAT", "GEESPACE"]},
    "satelog":    {"group": "satelog",      "patterns": ["SATELOG"]},
    "parus":      {"group": "parus",        "patterns": ["PARUS"]},
    "strela-1m":  {"group": "strela-1m",    "patterns": ["STRELA-1M"]},
    "strela-3":   {"group": "strela-3",     "patterns": ["STRELA-3"]},
}

ALL_SLUGS = sorted(CONSTELLATIONS.keys())

# Slugs whose CelesTrak feed is known-incomplete; the workflow always queries
# Space-Track for them and *unions* the results into the on-disk JSON.
FORCE_SPACETRACK_SLUGS = sorted(
    slug for slug, cfg in CONSTELLATIONS.items() if cfg.get("force_spacetrack")
)
