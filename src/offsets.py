"""Every memory offset the tools rely on — the ONE file to edit after a game update.

Two layers (see docs/OFFSETS.md):
  * stat block   — f32 cache the game recomputes; offsets are relative to its start
  * player object — the run-state object; the stat block sits inside it at
                    PLAYER_STAT_BASE
"""

# Game build these offsets were last confirmed on.
GAME_VERSION = "v0.2.0-1036"

# --- stat block -------------------------------------------------------------
# field -> byte offset. Names match the keys of the game's own stats line
# ("hp=...;dmg=...;luck=..."), which verify_layout.py compares against.
# Newest first: verify_layout.py tries them in this order.
STAT_LAYOUTS = {
    "v0.2.0-1036": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    # v1033 inserted x36 and pushed everything after `arm` four bytes along.
    "v0.2.0-1033": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    "v0.2.0-977": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "reg": 36, "area": 40, "luck": 44, "gold": 48,
    },
}
STAT = STAT_LAYOUTS[GAME_VERSION]

# Tail fields: NOT re-verified after the v1033 shift, assumed +4 from the old build.
STAT_TAIL = {"echo": 60, "heal": 64, "bp": 68, "doth": 76}

# Never written by the tools: x36 holds 0.6, the panel doesn't show it and
# writing it changed nothing visible (not lifesteal). Meaning unknown.
STAT_READONLY = {"x36"}

# Plausible ranges (inclusive) a live stat block satisfies. Deliberately wide:
# level 40+ characters carry pickup radius and regen in the hundreds.
HP_RANGE = (40.0, 800000.0)
STAT_RANGES = {
    "dmg": (0.1, 100.0),
    "cd": (0.05, 1.5),      # cooldown multiplier, smaller = faster
    "crit": (0.0, 10.0),
    "cm": (1.0, 20.0),      # crit damage multiplier
    "spd": (0.5, 20.0),
    "pick": (1.0, 800.0),
    "xp": (0.5, 20.0),      # xp multiplier
    "arm": (0.0, 2.0),
    "reg": (0.0, 5000.0),
    "area": (0.2, 30.0),
    "luck": (-1.0, 100.0),
    "gold": (0.5, 40.0),    # gold multiplier
}

# --- player object ----------------------------------------------------------
PLAYER_STAT_BASE = 0x5F4    # player object + 0x5F4 = stat block start

# field -> (byte offset from the player-object base, struct format)
PLAYER = {
    "level":    (0x58C, "<i"),
    "xp":       (0x590, "<i"),   # current experience, resets on level up
    "hp":       (0x594, "<f"),   # current health
    "gold":     (0x5A4, "<i"),   # coin amount
    "rerolls":  (0x658, "<i"),   # watched 5 -> 4 on a real reroll
    "kills":    (0x974, "<i"),
    "banishes": (0xF5C, "<i"),   # watched 3 -> 2 on a real banish
}

# Stat fields used as the player-object fingerprint (all must be in range at once).
PLAYER_FINGERPRINT = ("cm", "spd", "pick", "xp", "arm", "reg", "area")


def player_stat(name):
    """Byte offset of a stat-block field, relative to the player-object base."""
    return PLAYER_STAT_BASE + STAT[name]
