"""Every memory offset the tools rely on — the ONE file to edit after a game update.

Two layers (see docs/OFFSETS.md):
  * stat block   — f32 cache the game recomputes; offsets are relative to its start
  * player object — the run-state object; the stat block sits inside it at
                    PLAYER_STAT_BASE
"""

# Game build these offsets were last confirmed on. The updater calls 1127
# "0.142.0"; both strings are in the exe.
GAME_VERSION = "v0.2.0-1127"

# --- stat block -------------------------------------------------------------
# field -> byte offset. Names match the keys of the game's own stats line
# ("hp=...;dmg=...;luck=..."), which verify_layout.py compares against.
# Newest first: verify_layout.py tries them in this order.
STAT_LAYOUTS = {
    # 1127 (0.142.0): every field OK against a live run; the exe writes the block
    # at the same +0x680 as 1118 (xp now from xmm13, a REX-prefixed movss).
    "v0.2.0-1127": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
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

# Stats the game sums from lists each time instead of storing a total.
#
# Effect list: the run's talent stats (armory picks, one entry per stat kind,
# same-kind talents summed; tags in docs/OFFSETS.md: 0 dmg, 1 cooldown, 2 max hp,
# 3 crit, 4 crit dmg, 5 damage taken, 6 regen, 7 speed, 0x0e lifesteal), a Vec
# of 8-byte entries (u8 tag, 3 pad, f32 value), capacity / pointer / length in
# the player object. Built when the run starts by pushing (capacity 4, 8, 16...),
# cards don't touch it. Thorns = tag 0x16 (pane 荆棘 30% -> 77% with a marker).
# v1118, 7 talents: capacity 8, so one spare slot; appending (tag 0x16, 0.5) and
# bumping the length showed 荆棘 50% with no thorns talent at all.
# v1127: read on a live run, same shape (capacity 8, 7 talent tags, sane values).
EFFECT_LIST = (0xADC, 0xAE4)        # player-object offsets of the Vec ptr and len
EFFECT_CAP = 0xAD4                  # ... and its capacity (Rust Vec: cap, ptr, len)
# With no spare slot (no talent stats at all: capacity 0; or exactly 4 / 8 kinds)
# the tools point the Vec at a page of their own holding a copy plus their entry,
# with capacity 0: Rust frees a Vec's buffer only when its capacity isn't 0, so
# the game never hands our page to its allocator. Unlocking points it back.
EFFECT_PAGE_ENTRIES = 512           # 8-byte entries in that 4 KiB page
# Item list (jewelry and the like): a Vec of 24-byte entries, +0x00 pointer to
# the item's definition (kind byte at +0x44), +0x10 f32 value.
ITEM_LIST = (0x43C, 0x444)
ITEM_STRIDE, ITEM_DEF_KIND, ITEM_VALUE = 24, 0x44, 0x10
# Lifesteal (RVA 0x5F809D0) = timed stacking buffs + class base (a constant
# table in the exe) + effect tag 0x0e + item kind 0x12. We touch only the two
# lists, so the panel shows that part; the pane adds the class base (圣骑士 0.6%).
# v1118: a legendary 饮血者指环 read kind 0x12 / 0.03 (3.0%); writing 0.25 moved
# the pane's 生命偷取 to 25% + class base and stayed.
# v1127: read on a live run, three items; one (kind 0x09, 0.288) equals the
# block's echo (+60 = 0.288), so the list and the tail still line up.
EFFECT_SOURCES = {
    "thorns": (("effect", 0x16),),
    "lifesteal": (("effect", 0x0e), ("item", 0x12)),
    "luck": (("effect", 0x0c),),        # backs the luck stat lock: STAT_BACKING
}

# Stats whose lock also goes into the talent list, not only into the stat block.
#
# The game rebuilds the stat block on every level-up and every card pick, and in
# the same frame deals the next hand of a stacked level-up. A stat-block lock
# writes luck back ~15 ms later (v1127, timeline 2026-11-09: hands dealt 13-18 ms
# after a pick read 普普普 / 优优优 with luck locked at +1000%; the first hand of
# each level-up, 300 ms after the rebuild, read 传史史). An entry in the talent
# list is part of what the rebuild sums, so luck never drops to 0 at that moment.
# Tags found 2026-11-09 (v1127) by appending (tag, 3.0) and diffing a rebuild:
# 0x08 范围 +3, 0x09 拾取 x4, 0x0a 经验值 +3, 0x0b 金币 +3, 0x0c 幸运 +3 (0 -> 3),
# 0x0d 回响 +3, 0x0f 击杀治疗 +3.
STAT_BACKING = {"luck"}

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
# v1127: unchanged. verify_layout.py: level / gold / kills / rerolls / chest / hp
# match the HUD; banishes read 90 after the preset wrote 100 at run start, so the
# game is counting that slot down.
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

# Values the game itself never goes past. v1118: the level cap is 120 (seen in
# game); a higher level could send its experience-table lookup out of bounds.
# 极速模式: the run timer (HUD "time", what boss arrivals go by), f32 seconds in the
# player object. v1127: found by turbo.locate (HUD match + probe), then seen at
# player + 0xFC4 in two runs (the second run's chest price sat at timer + 0x60).
# Not a FIELDS entry: turbo.Turbo stretches it, nobody types a value into it.
RUN_TIME = 0xFC4

PLAYER_LIMITS = {"level": (1, 120)}

# Fields whose offset is not confirmed on GAME_VERSION: shown as "?", never written.
# Empty on v1127. After an update, put a field here until it is re-identified.
PLAYER_UNVERIFIED = set()

# Stat fields used as the player-object fingerprint (all must be in range at once).
PLAYER_FINGERPRINT = ("cm", "spd", "pick", "xp", "arm", "reg", "area")


def player_stat(name):
    """Byte offset of a stat-block field, relative to the player-object base."""
    return PLAYER_STAT_BASE + STAT[name]
