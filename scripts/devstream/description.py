import re
import unicodedata
from pathlib import Path

MIN_LEN, MAX_LEN = 10, 140
ZERO_WIDTH = re.compile("[​-‏⁠﻿]")
FORBIDDEN = set("<>[]()`*_#")
EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿]")
# Extension-like TLDs: "Node.js" is a name, not a domain. shortcut: also lets
# "x.py"-style hosts through; extend only if spam starts using them.
NOT_TLDS = {"js", "ts", "py", "rs", "rb", "md", "sh", "kt", "cs"}
DOMAIN = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+([a-z]{2,24})(?![a-z0-9])", re.I)


def _load_blocklist() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "blocklist.txt"
    lines = (ln.strip().casefold() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


BLOCKLIST = _load_blocklist()
WHOLE = re.compile(
    r"(?<!\w)(?:" + "|".join(r"\s+".join(map(re.escape, t.split())) for t in BLOCKLIST) + r")(?!\w)"
)
SQUASHED = [s for s in (re.sub(r"[\W_]", "", t) for t in BLOCKLIST) if len(s) >= 5]


def _squash(s: str) -> str:
    return re.sub(r"[\W_]", "", s)


def _has_url(t: str) -> bool:
    t = t.replace("。", ".")
    if re.search(r"https?://|www\.", t, re.I):
        return True
    return any(m.group(1).lower() not in NOT_TLDS for m in DOMAIN.finditer(t))


def _emoji_run_too_long(t: str) -> bool:
    run = 0
    for ch in t:
        if EMOJI.match(ch):
            run += 1
            if run > 2:
                return True
        elif ch != "️":  # variation selector rides along with its emoji
            run = 0
    return False


def clean_description(text: str) -> tuple[str | None, str | None]:
    t = ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    t = " ".join(t.split())
    if not MIN_LEN <= len(t) <= MAX_LEN:
        return None, f"Descriptions need to be between {MIN_LEN} and {MAX_LEN} characters."
    if _has_url(t):
        return None, "Descriptions can't include links."
    if FORBIDDEN & set(t):
        return None, "Descriptions can't include these characters: < > [ ] ( ) ` * _ #"
    if _emoji_run_too_long(t):
        return None, "Descriptions can't have more than 2 emoji in a row."
    if any(w.isupper() and len(w) > 5 for w in re.findall(r"[^\W\d_]+", t)):
        return None, "Descriptions can't have long words in ALL CAPS."
    folded = t.casefold()
    if WHOLE.search(folded) or any(s in _squash(folded) for s in SQUASHED):
        return None, "That description includes wording we don't allow."
    return t.rstrip("!?,;: ").rstrip(".") + ".", None
