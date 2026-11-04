"""Azeroth Survivors — save-file tool. EDITS THE SAVE: run it with the game closed.

  python save.py --show                    decrypt and print the save
  python save.py --resets 0                perk resets used -> 0 (page shows 2/2 left)
  python save.py --marks 9999.9            marks of the current hero's class
  python save.py --marks 500 --cls 5       ... of another class (WoW ids: 2 paladin, 5 priest)
  python save.py --export plain.txt        decrypted text, to edit by hand
  python save.py --import plain.txt        encrypt an edited text back into the save
  add --dry-run to see the change without writing it

The save (%APPDATA%\\wenilla-rogue\\characters.txt) is "RSE1:" + base64 of
  nonce(16) | ciphertext | tag(20)
  tag       = HMAC-SHA1(key, "tag" | nonce | ciphertext)
  keystream = HMAC-SHA1(key, "ks" | nonce | block index as u64 LE), 20 bytes a block
The plaintext is text, one field per line (`marks|2|1440`, `perk|banish:3,...`).
The key is not stored in this repo: it is found in the game exe, as the HMAC
ipad/opad blocks the exe precomputes, and accepted only if it verifies the save.

Every write: refuses while the game runs, backs the save up first, then re-reads
what it wrote and checks that only the intended lines changed.
"""
import argparse
import base64
import datetime
import difflib
import hashlib
import hmac
import os
import shutil
import sys

import numpy as np

import game
from group_stores import pe_sections
from scan_stores import DEFAULT_EXE

SAVE = os.path.join(os.environ.get("APPDATA", ""), "wenilla-rogue", "characters.txt")
BACKUP_DIR = "trainer-backups"          # next to the save
MAGIC = "RSE1:"
NONCE, TAG, BLOCK = 16, 20, 20
MARKS_SCALE = 10                        # stored in tenths: 1440 = 144.0 marks


class SaveError(Exception):
    pass


# --- codec ------------------------------------------------------------------
def _mac(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha1).digest()


def _xor_stream(key, nonce, data):
    out = bytearray(data)
    for i in range(0, len(out), BLOCK):
        ks = _mac(key, b"ks", nonce, (i // BLOCK).to_bytes(8, "little"))
        for j in range(min(BLOCK, len(out) - i)):
            out[i + j] ^= ks[j]
    return bytes(out)


def split(text):
    text = text.strip()
    if not text.startswith(MAGIC):
        raise SaveError(f"not an {MAGIC} save")
    raw = base64.b64decode(text[len(MAGIC):], validate=True)
    if len(raw) < NONCE + TAG:
        raise SaveError("save too short")
    return raw[:NONCE], raw[NONCE:-TAG], raw[-TAG:]


def verifies(key, text):
    nonce, ct, tag = split(text)
    return hmac.compare_digest(_mac(key, b"tag", nonce, ct), tag)


def decode(key, text):
    """Encrypted save text -> (nonce, plaintext)."""
    nonce, ct, tag = split(text)
    if not hmac.compare_digest(_mac(key, b"tag", nonce, ct), tag):
        raise SaveError("tag mismatch: wrong key, or the save is damaged")
    return nonce, _xor_stream(key, nonce, ct).decode("utf-8")


def encode(key, nonce, plain):
    ct = _xor_stream(key, nonce, plain.encode("utf-8"))
    return MAGIC + base64.b64encode(nonce + ct + _mac(key, b"tag", nonce, ct)).decode("ascii")


# --- key ----------------------------------------------------------------------
def key_candidates(blob, width=48):
    """HMAC keys whose precomputed ipad block is followed by its opad block.

    HMAC-SHA1 pads the key to 64 bytes; the exe stores key^0x36 then key^0x5c,
    16-byte aligned, with the all-pad tail elsewhere. Their XOR is 0x6a
    throughout, which ordinary data almost never is.
    """
    a = np.frombuffer(blob, dtype=np.uint8)
    n = len(a) - 2 * width
    if n <= 0:
        return []
    first = np.nonzero((a[:n] ^ a[width:width + n]) == 0x6A)[0]
    first = first[first % 16 == 0]
    out = []
    for i in first:
        i = int(i)
        if np.all((a[i:i + width] ^ a[i + width:i + 2 * width]) == 0x6A):
            key = bytes(a[i:i + width] ^ 0x36)
            if any(key) and key not in out:
                out.append(key)
    return out


def find_key(exe, save_text):
    """The key from the exe that verifies this save."""
    try:
        d = open(exe, "rb").read()
    except OSError as e:
        raise SaveError(f"cannot read the game exe ({exe}): {e}") from None
    secs, _exc_rva, _exc_size = pe_sections(d)
    for name, _va, _vsz, rp, rsz in secs:
        if name != ".rdata":
            continue
        for key in key_candidates(d[rp:rp + rsz]):
            if verifies(key, save_text):
                return key
    raise SaveError("no key in the exe verifies this save — the game changed its "
                    "save format; do not write until save.py is updated")


# --- edits on the plaintext ---------------------------------------------------
def lines_of(plain):
    return plain.split("\n")


def field(lines, name):
    """Index of the line `name|...`, or None."""
    hits = [i for i, l in enumerate(lines) if l.split("|", 1)[0] == name]
    if len(hits) > 1:
        raise SaveError(f"{len(hits)} '{name}' lines — unexpected layout, not editing")
    return hits[0] if hits else None


def current_class(lines):
    i = field(lines, "pick")
    if i is None:
        raise SaveError("no 'pick' line: pass --cls")
    return int(lines[i].split("|")[1])


def set_resets(lines, used):
    """Perk resets USED on this save (the page shows total - used)."""
    if used < 0:
        raise SaveError("resets must be >= 0")
    out = list(lines)
    i = field(out, "resets")
    if used == 0:                       # the game itself omits the line at 0
        if i is not None:
            del out[i]
        return out
    line = f"resets|{used}"
    if i is None:
        j = field(out, "pets")
        out.insert(j + 1 if j is not None else len(out), line)
    else:
        out[i] = line
    return out


def marks_of(lines, cls):
    for l in lines:
        p = l.split("|")
        if p[0] == "marks" and len(p) == 3 and p[1] == str(cls):
            return int(p[2]) / MARKS_SCALE
    return None


def set_marks(lines, cls, marks):
    """Marks of one class, in game units (one decimal)."""
    tenths = round(marks * MARKS_SCALE)
    if not 0 <= tenths < 2**31:
        raise SaveError("marks out of range")
    out = list(lines)
    hits = [i for i, l in enumerate(out)
            if l.split("|")[0] == "marks" and l.split("|")[1:2] == [str(cls)]]
    if len(hits) > 1:
        raise SaveError(f"{len(hits)} marks lines for class {cls} — not editing")
    line = f"marks|{cls}|{tenths}"
    if hits:
        out[hits[0]] = line
    else:
        out.append(line)
    return out


def changes(old_plain, new_plain):
    return [d for d in difflib.unified_diff(lines_of(old_plain), lines_of(new_plain),
                                            lineterm="", n=0)
            if d[:1] in "+-" and d[:3] not in ("+++", "---")]


# --- file handling ------------------------------------------------------------
def read_text(path, encoding="ascii"):
    with open(path, encoding=encoding) as f:
        return f.read()


def backup(path):
    folder = os.path.join(os.path.dirname(path), BACKUP_DIR)
    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"   # sorts by time
    # never overwrite an earlier backup: two edits within one second are easy in the GUI
    for n in range(1000):
        dest = os.path.join(folder, f"characters-{stamp}{'' if n == 0 else f'-{n}'}.txt")
        try:
            with open(path, "rb") as src, open(dest, "xb") as out:
                shutil.copyfileobj(src, out)
        except FileExistsError:
            continue
        shutil.copystat(path, dest)
        return dest
    raise SaveError("could not pick a free backup name")


def write_checked(key, path, nonce, old_plain, new_plain):
    """Back up, write atomically, read back and compare. Returns the backup path."""
    expected = changes(old_plain, new_plain)
    text = encode(key, nonce, new_plain)
    _n, again = decode(key, text)
    if again != new_plain:
        raise SaveError("encode/decode mismatch — not writing")
    saved = backup(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    _n, final = decode(key, read_text(path))
    if changes(old_plain, final) != expected:
        shutil.copy2(saved, path)
        raise SaveError("read-back differs from what was written — restored the backup")
    return saved


CLASS_NAMES = {1: "战士", 2: "圣骑士", 3: "猎人", 4: "盗贼", 5: "牧师",
               7: "萨满", 8: "法师", 9: "术士", 11: "德鲁伊"}
GAME_RUNNING = ("游戏正在运行：它退出时会重写存档、把改动覆盖掉。先完全退出游戏。"
                " (the game is running — close it completely first)")


def resets_used(lines):
    i = field(lines, "resets")
    return 0 if i is None else int(lines[i].split("|")[1])


def all_marks(lines):
    """{class id: marks in game units} for every marks line."""
    out = {}
    for l in lines:
        p = l.split("|")
        if p[0] == "marks" and len(p) == 3:
            out[int(p[1])] = int(p[2]) / MARKS_SCALE
    return out


class Save:
    """One read of the save, decrypted. commit() writes an edited copy back."""

    def __init__(self, path=SAVE, exe=DEFAULT_EXE):
        self.path = path
        try:
            self.text = read_text(path)
        except OSError as e:
            raise SaveError(f"cannot read the save: {e}") from None
        self.key = find_key(exe, self.text)
        self.nonce, self.plain = decode(self.key, self.text)
        self.lines = lines_of(self.plain)

    def commit(self, new_lines):
        """Write `new_lines` (an edit of self.lines). Returns the backup path, or
        None when nothing changed."""
        new_plain = "\n".join(new_lines)
        if new_plain == self.plain:
            return None
        if game.find_pid():
            raise SaveError(GAME_RUNNING)
        if read_text(self.path) != self.text:
            raise SaveError("存档在读取之后被改过（游戏刚存过档？），重新读取后再改"
                            " (the save changed since it was read: reload first)")
        return write_checked(self.key, self.path, self.nonce, self.plain, new_plain)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", default=SAVE, help="save file (default: %(default)s)")
    ap.add_argument("--exe", default=DEFAULT_EXE, help="game exe, where the key is")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--resets", type=int, metavar="USED",
                    help="perk resets used on this save (0 = all left)")
    ap.add_argument("--marks", type=float, help="marks, in game units (one decimal)")
    ap.add_argument("--cls", type=int, help="class id for --marks (default: current hero)")
    ap.add_argument("--export", metavar="FILE", help="write the decrypted text to FILE")
    ap.add_argument("--import", dest="import_", metavar="FILE",
                    help="encrypt FILE (edited text) into the save")
    ap.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    args = ap.parse_args()

    try:
        sv = Save(args.file, args.exe)
        plain, lines = sv.plain, sv.lines

        if args.show or not any(v is not None for v in (args.resets, args.marks, args.import_, args.export)):
            print(plain)
        if args.export:
            with open(args.export, "w", encoding="utf-8", newline="") as f:
                f.write(plain)
            print(f"decrypted text -> {args.export}")

        new = lines
        if args.import_:
            new = lines_of(read_text(args.import_, "utf-8").replace("\r\n", "\n"))
        if args.resets is not None:
            new = set_resets(new, args.resets)
        if args.marks is not None:
            cls = args.cls if args.cls is not None else current_class(new)
            print(f"marks (class {cls}): {marks_of(new, cls)} -> {args.marks:.1f}")
            new = set_marks(new, cls, args.marks)
        new_plain = "\n".join(new)
        if new_plain == plain:
            return
        diff = changes(plain, new_plain)
        print("change:\n  " + "\n  ".join(diff))
        if args.dry_run:
            print("dry run: nothing written")
            return
        saved = sv.commit(new)
        print(f"written and verified. backup: {saved}")
    except SaveError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
