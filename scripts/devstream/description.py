import re
import unicodedata
from pathlib import Path

MIN_LEN, MAX_LEN = 10, 140
FORBIDDEN = set("<>[]()`*_#")
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf]")
VARIATION = re.compile("[\ufe00-\ufe0f]")
# Only names that are not real TLDs a spammer would use; Node.js and Vue.js rely on "js".
NOT_TLDS = {"js", "ts", "py", "md", "rb"}
DOMAIN = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+([a-z]{2,24})(?![a-z0-9])", re.I)
IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\w)")
# A path, query or port after the name means it is an address, not a product name.
ADDRESS_TAIL = re.compile(r"/|[:?]\S")


def _strip_invisible(s: str) -> str:
    # Cf covers zero-width, soft hyphen, invisible operators and U+180E; U+034F is Mn.
    return "".join(c for c in s if unicodedata.category(c) != "Cf" and c != "\u034f")


def _load_blocklist() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "blocklist.txt"
    lines = (ln.strip().casefold() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


BLOCKLIST = _load_blocklist()
def _term_pattern(term: str) -> str:
    if len(re.sub(r"[\W_]", "", term)) < 5:
        return r"(?<!\w)" + r"\s+".join(map(re.escape, term.split())) + r"(?!\w)"
    # Letters may be separated by punctuation or spaces ("c.a.s.i.n.o"), but the
    # whole run must stand alone so "specialists" does not hit "cialis".
    letters = [re.escape(c) for c in term if c.isalnum()]
    return r"(?<![a-z0-9])" + r"[\W_]*".join(letters) + r"(?![a-z0-9])"


BLOCKED = re.compile("|".join(_term_pattern(t) for t in BLOCKLIST))


def _mixed_script(t: str) -> bool:
    for w in re.findall(r"[^\W\d_]+", t):
        scripts = {unicodedata.name(c, "").split(" ")[0] for c in w}
        if "LATIN" in scripts and scripts & {"CYRILLIC", "GREEK"}:
            return True
    return False


def _has_url(t: str) -> bool:
    t = t.replace("。", ".")
    if re.search(r"https?://|www\.", t, re.I):
        return True
    if IPV4.search(t):
        return True
    return any(
        m.group(1).lower() not in NOT_TLDS or ADDRESS_TAIL.match(t[m.end() :])
        for m in DOMAIN.finditer(t)
    )


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
    # U+FE0F is kept through the emoji check, then dropped before matching.
    t = " ".join(_strip_invisible(unicodedata.normalize("NFKC", text)).split())
    if len(t) < MIN_LEN:
        return None, f"Descriptions need to be between {MIN_LEN} and {MAX_LEN} characters."
    if _emoji_run_too_long(t):
        return None, "Descriptions can't have more than 2 emoji in a row."
    t = " ".join(VARIATION.sub("", t).split())
    final = t.rstrip("!?,;: ").rstrip(".") + "."
    # The limit covers the finished text, so a 140-character input with no
    # period is rejected rather than stored at 141.
    if len(final) > MAX_LEN or len(t) < MIN_LEN:
        return None, f"Descriptions need to be between {MIN_LEN} and {MAX_LEN} characters."
    if _mixed_script(t):
        return None, "Please use one alphabet per word."
    if _has_url(t):
        return None, "Descriptions can't include links."
    if FORBIDDEN & set(t):
        return None, "Descriptions can't include these characters: < > [ ] ( ) ` * _ #"
    if any(w.isupper() and len(w) > 5 for w in re.findall(r"[^\W\d_]+", t)):
        return None, "Descriptions can't have long words in ALL CAPS."
    if BLOCKED.search(t.casefold()):
        return None, "That description includes wording we don't allow."
    return final, None
