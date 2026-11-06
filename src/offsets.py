"""Every memory offset the tools rely on — the ONE file to edit after a game update.

Two layers (see docs/OFFSETS.md):
  * stat block   — f32 cache the game recomputes; offsets are relative to its start
  * player object — the run-state object; the stat block sits inside it at
                    PLAYER_STAT_BASE
"""

# Game build these offsets were last confirmed on.
GAME_VERSION = "v0.2.0-1118"

# --- stat block -------------------------------------------------------------
# field -> byte offset. Names match the keys of the game's own stats line
# ("hp=...;dmg=...;luck=..."), which verify_layout.py compares against.
# Newest first: verify_layout.py tries them in this order.
STAT_LAYOUTS = {
    # 1118: every field OK against a live run; the exe moved the block another
    # +0x18 (same store pattern), again invisible to us.
    "v0.2.0-1118": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    # 1115: every field OK against a live run. In the exe the whole block moved
    # +0x78 inside its owner, but we anchor on the block itself, so no change.
    "v0.2.0-1115": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    # 1039: verify_layout.py against a live run, every field OK (same as 1036)
    "v0.2.0-1039": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
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

# Tail fields. v1118: confirmed by pinning marker values and reading the
# character pane (目标数 +7, 回响 22%, 击杀治疗 33, 增益强度 +44%, 持续伤害急速 +77%).
# v1118, from the code that prints the stats line (RVA 0x5F5E950): it reads
# lives at +0x60 and shows DoT stacks as min([+0x48], 3) + 1 — confirmed on the
# pane with markers (生命数 3, 持续伤害层数 3).
STAT_TAIL = {"targets": 56, "echo": 60, "heal": 64, "bp": 68, "dots": 72, "doth": 76,
             "lives": 96}
STAT_INT = {"targets", "dots", "lives"}     # i32, not f32
DOTS_MAX_EXTRA = 3          # the game caps extra DoT stacks at 3 (4 stacks shown)

# Effects of gear and cards: a Vec of 8-byte entries (u8 tag, 3 pad, f32 value)
# the game sums per tag each time. Its pointer and length sit in the player
# object. Thorns is tag 0x16 (pane 荆棘 30% -> 77% with a marker). Lifesteal
# mixes this list (tag 0x0e), class base and cards: not handled.
EFFECT_LIST = (0xADC, 0xAE4)        # player-object offsets of the Vec ptr and len
EFFECT_TAGS = {"thorns": 0x16}

# Never written by the tools: x36 holds 0.6. v1118: markers 0.07 and 3.0 left the
# pane's 生命偷取 at 0.6%, so despite the match it is not lifesteal. Meaning unknown.
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
# v1118: every field below re-checked on a live run (HUD; banishes 3 -> 2 after one).
# Found on v1115 (banishes / chest by diff), unchanged on v1118.
PLAYER = {
    "level":    (0x58C, "<i"),
    "xp":       (0x590, "<i"),   # current experience, resets on level up
    "hp":       (0x594, "<f"),   # current health
    "gold":     (0x5A4, "<i"),   # coin amount
    "rerolls":  (0x658, "<i"),   # watched 5 -> 4 on a real reroll
    "kills":    (0xA3C, "<i"),   # v1039 (was 0x974 up to 1036): matched HUD 222 -> 268
    "banishes": (0x1050, "<i"),  # v1115: watched 3 -> 2 on a real banish, only slot
                                 # that dropped by 1 (1036 had it at 0xF5C)
    "chest":    (0x1024, "<i"),  # v1115: gold price of the next chest (HUD chestCost);
                                 # 20 -> 31 on opening one; wrote 1, HUD showed 1
}

# Fields whose offset is not confirmed on GAME_VERSION: shown as "?", never written.
# Empty on v1118. After an update, put a field here until it is re-identified.
PLAYER_UNVERIFIED = set()

# Stat fields used as the player-object fingerprint (all must be in range at once).
PLAYER_FINGERPRINT = ("cm", "spd", "pick", "xp", "arm", "reg", "area")


def player_stat(name):
    """Byte offset of a stat-block field, relative to the player-object base."""
    return PLAYER_STAT_BASE + STAT[name]
