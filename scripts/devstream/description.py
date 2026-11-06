import re
import unicodedata
from pathlib import Path

MIN_LEN, MAX_LEN = 10, 140
FORBIDDEN = set("<>[]()`*_#")
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf]")
VARIATION = re.compile("[\ufe00-\ufe0f]")
ZWJ = "\u200d"
# A zero-width joiner between two emoji builds one emoji (a family, say), so it survives the stripping.
EMOJI_ZWJ = re.compile("(?<=[\U0001f000-\U0001faff\u2600-\u27bf\ufe00-\ufe0f])\u200d(?=[\U0001f000-\U0001faff\u2600-\u27bf])")
# Only names that are not real TLDs a spammer would use; Node.js and Vue.js rely on "js".
NOT_TLDS = {"js", "ts", "py", "md", "rb"}
DOMAIN = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+([a-z]{2,24})(?![a-z0-9])", re.I)
IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\w)")
# A path, query or port after the name means it is an address, not a product name.
ADDRESS_TAIL = re.compile(r"/|[:?]\S")


# Default_Ignorable_Code_Point plus the Hangul fillers. The stdlib has no property
# regex, so this is the table. U+FE00-FE0F is left to VARIATION: the emoji-run
# check needs to see them first.
IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0000-\U000e0fff]"
)


def _strip_invisible(s: str) -> str:
    # Cf also covers format characters outside the table, such as U+0600-0605.
    return ZWJ.join(IGNORABLE.sub("", "".join(c for c in part if unicodedata.category(c) != "Cf"))
                    for part in EMOJI_ZWJ.split(s))


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
    if re.search(r"https?://|www\.(?=\w)", t, re.I):
        return True
    if IPV4.search(t):
        return True
    return any(
        m.group(1).lower() not in NOT_TLDS or ADDRESS_TAIL.match(t[m.end() :])
        for m in DOMAIN.finditer(t)
    )


def _emoji_run_too_long(t: str) -> bool:
    run, joined = 0, False
    for ch in t:
        if ch == ZWJ:  # the next emoji is part of this one
            joined = True
        elif EMOJI.match(ch):
            run += not joined
            joined = False
            if run > 2:
                return True
        elif not VARIATION.match(ch):  # variation selectors ride along with their emoji
            run = 0
    return False


def clean_description(text: str) -> tuple[str | None, str | None]:
    # U+FE0F is kept through the emoji check, then dropped before matching.
    # Strip on both sides of NFKC: it maps U+3164 and U+FFA0 onto the U+1160 filler.
    t = " ".join(_strip_invisible(unicodedata.normalize("NFKC", _strip_invisible(text))).split())
    if len(t) < MIN_LEN:
        return None, f"Descriptions need to be between {MIN_LEN} and {MAX_LEN} characters."
    if _emoji_run_too_long(t):
        return None, "Descriptions can't have more than 2 emoji in a row."
    t = " ".join(VARIATION.sub("", t).split())
    body = t.rstrip("!?,;: ")
    # A question keeps one question mark; anything else ends in a full stop.
    final = body.rstrip(".") + ("?" if "?" in t[len(body):] else ".")
    # Both limits cover the finished text: a 140-character input with no period
    # would be stored at 141, and "Toolkit!!!" shrinks to an 8-character "Toolkit.".
    if not MIN_LEN <= len(final) <= MAX_LEN:
        return None, f"Descriptions need to be between {MIN_LEN} and {MAX_LEN} characters."
    # Check the text we store, so validate_data's re-check reaches the same answer.
    t = final
    if _mixed_script(t):
        return None, "Please use one alphabet per word."
    if _has_url(t):
        return None, "Descriptions can't include links."
    if "@" in t:
        return None, "Descriptions can't mention people or teams."
    if FORBIDDEN & set(t):
        return None, "Descriptions can't include these characters: < > [ ] ( ) ` * _ #"
    if any(w.isupper() and len(w) > 5 for w in re.findall(r"[^\W\d_]+", t)):
        return None, "Descriptions can't have long words in ALL CAPS."
    if BLOCKED.search(t.casefold()):
        return None, "That description includes wording we don't allow."
    return final, None
