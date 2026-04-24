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
    "yinhe":      {"group": "yinhe",        "patterns": ["GALAXYSPACE", "YINHE"]},
    "jilin":      {"group": "jilin-1",      "patterns": ["JILIN"]},
    "tianqi":     {"group": "tianqi",       "patterns": ["TIANQI"]},
    "yaogan":     {"group": "yaogan",       "patterns": ["YAOGAN"]},
    "bluewalker": {"group": "ast",          "patterns": ["BLUEWALKER"]},
    "lynk":       {"group": "other-comm",   "patterns": ["LYNK"]},
    "telesat":    {"group": "telesat",      "patterns": ["TELESAT"]},
}

ALL_SLUGS = sorted(CONSTELLATIONS.keys())
