"""Values the agent applies by itself at the start of every run.

A plain UTF-8 text file passed with --preset (AZS-Launcher.bat passes
AZS-preset.txt next to itself, in the game folder):

    # comment
    伤害 = 100          (+100%, typed the way the pane shows it)
    幸运 = 1000
    宝箱价格 = 1

Names are the panel's labels (or the internal names: dmg, luck, player.gold).
Every field that can be locked is locked, so the game can't take it back; the
rest (金币数量, 等级, 当前经验, 击杀, 重抽次数, 放逐次数) is written once. The file is read again at every
run, so an edit takes effect from the next run without restarting the agent.
"""
import os

import cardpick
from session import FIELDS, SessionError, coerce, parse, show

FILENAME = "AZS-preset.txt"

# (kind, name) for each name a line may use: the label, kind.name, and the bare
# name where it is unambiguous (gold is both a stat and a player field).
_BY_NAME = {}
for _kind, _name, _label, *_ in FIELDS:
    _BY_NAME[_label] = (_kind, _name)
    _BY_NAME[f"{_kind}.{_name}"] = (_kind, _name)
for _kind, _name, *_ in FIELDS:
    if sum(1 for f in FIELDS if f[1] == _name) == 1:
        _BY_NAME[_name] = (_kind, _name)
_SPEC = {(f[0], f[1]): f for f in FIELDS}


CARD_TEMPLATE = [
    "",
    "# --- 自动选卡（要用 AZS-Launcher.bat 开游戏）---",
    "# 写了名单就自动选：越靠前越优先，用 > 隔开；写稀有度（普通 优秀 精良 史诗 传说）表示这个稀有度及以上的任意一张。",
    "# 同一项有几张都符合时选更稀有的。名字照卡上写（后台窗口会把每手牌列出来，可以照抄）。",
    "# 只在游戏窗口在最前面时按键（1 2 3 选卡、R 重抽）；每手牌都重新读这个文件，改完马上生效。",
    "# 技能卡 = 神圣愤怒 > 传说 > 审判",
    "# 装备卡 = 天选者印记 > 史诗",
    "# 都没有时 = 停下              # 停下（留给你选）/ 重抽（只用免费次数）/ 稀有度最高 / 第一张，可以用 > 连着写",
]


class PresetError(ValueError):
    pass


def parse_text(text):
    """[(kind, name, raw value)] from the file's text; PresetError names the bad line."""
    out, seen = [], {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for sep in ("=", "＝", ":", "："):
            if sep in line:
                key, val = (s.strip() for s in line.split(sep, 1))
                break
        else:
            raise PresetError(f"第 {no} 行：要写成“名字 = 数值”：{line}")
        if key in cardpick.KEYS:
            continue                    # 技能卡 / 装备卡 / 都没有时: cardpick.py reads those
        if not val:
            continue                    # "伤害 =" with nothing after it: left as is
        field = _BY_NAME.get(key)
        if not field:
            raise PresetError(f"第 {no} 行：没有叫“{key}”的属性（用面板上的名字）")
        kind, name, label, _fmt, _lockable, unit, _group = _SPEC[field]
        try:
            raw = coerce(kind, name, parse(unit, val))
        except ValueError as e:
            raise PresetError(f"第 {no} 行：{label} 的数值“{val}”不对（{e}）") from None
        if field in seen:
            raise PresetError(f"第 {no} 行：{label} 在第 {seen[field]} 行已经写过了")
        seen[field] = no
        out.append((kind, name, raw))
    return out


def load(path):
    """The file's entries, [] if there is no file."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return parse_text(f.read())
    except FileNotFoundError:
        return []


def apply(session, entries):
    """Lock (or, if it can't be locked, write) every entry. Returns log lines;
    one field failing (no slot for 荆棘, say) doesn't stop the others."""
    lines = []
    for kind, name, raw in entries:
        _k, _n, label, _fmt, lockable, unit, _g = _SPEC[(kind, name)]
        try:
            if lockable:
                v = session.pin(kind, name, raw)
                lines.append(f"lock {label} = {show(unit, v)}")
            else:
                v = session.write(kind, name, raw)
                lines.append(f"set {label} = {show(unit, v)}")
        except (ValueError, SessionError) as e:
            lines.append(f"{label}: failed: {e}")
    return lines


def template():
    """A file listing every field, all commented out: nothing happens until edited."""
    rows = [
        "# AZS 每局自动设置的数值。后台（remote.bat）每局定位到角色后按这里锁定。",
        "# 用法：删掉行首的 #，在 = 后面填数，保存；下一局开始生效，不用重启后台。= 后面空着的行不管。",
        "# 写法和游戏面板、AZS 面板一样：伤害 100 就是 +100%，急速 30 就是 +30%，暴击 50 就是 50%。",
        "# 能锁定的都会锁定（游戏改不回去）；金币数量、等级、当前经验、击杀、重抽次数、放逐次数只在开局写一次。",
        "# 想临时取消，在 AZS 面板里去掉那一项的“锁”就行。",
        "",
    ]
    hints = {"+%1": "例：100 就是 +100%", "haste": "例：30 就是 +30%",
             "%.1": "例：50 就是 50%", "%": "例：50 就是 50%", "+%": "例：1000 就是 +1000%",
             "+int": "整数，例：5 就是 +5", "int": "整数", "num": "数值", "dots": "1～4 层"}
    group = None
    for kind, name, label, _fmt, lockable, unit, grp in FIELDS:
        if grp != group:
            rows += ["", f"# --- {grp} ---"]
            group = grp
        hint = hints.get(unit, "")
        if not lockable:
            hint += "（只在开局写一次）"
        rows.append(f"# {label} = {' ' * max(1, 14 - 2 * len(label))}# {hint}")
    rows += CARD_TEMPLATE
    return "\n".join(rows) + "\n"


def ensure_file(path):
    """Write the commented-out template if there is no file yet. True if written."""
    if os.path.exists(path):
        return False
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(template())
        return True
    except OSError:
        return False
