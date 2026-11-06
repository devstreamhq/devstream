"""Find an OSI license in a repo when GitHub's own detection comes up empty."""

import base64
import binascii
import json
import re
from pathlib import Path

from scripts.devstream.gh import GitHubError

OSI = frozenset(json.loads((Path(__file__).parents[2] / "data" / "osi-licenses.json").read_text()))
# Content licenses, for categories whose entries are text rather than code.
CONTENT_LICENSES = frozenset({"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0"})
LICENSE_RE = re.compile(r"^(licen[cs]e|copying|unlicense)([-._][\w.-]+)?(\.(md|txt|rst))?$", re.I)
# Narrower than LICENSE_RE: these stems also name source files (copyright.py, gpl.js).
OTHER_RE = re.compile(r"^(copyright|artistic|bsdl|gpl)(\.(md|txt|rst)|-[\w-]+|-([\w-]*-)?\d+(\.\d+)*(\.(md|txt|rst))?)?$", re.I)


class _FileRe:
    @staticmethod
    def match(name):
        return LICENSE_RE.match(name) or OTHER_RE.match(name)


FILE_RE = _FileRe()
MAX_FILES = 5
MAX_BYTES = 100_000
# The blob comes down whole before any cap, so nothing larger is fetched at all.
MAX_BLOB = 5_000_000

NON_OSI = [
    ("Server Side Public License", "Server Side Public License"),
    ("Business Source License", "Business Source License"),
    ("Elastic License", "Elastic License"),
    ("Redis Source Available License", "Redis Source Available License"),
    ("Commons Clause", "Commons Clause"),
    ("source-available", "source-available"),
    ("source available", "source-available"),
    ("PolyForm", "PolyForm"),
    ("Functional Source License", "Functional Source License"),
    ("Fair Source", "Fair Source"),
    ("good, not evil", "JSON License"),
]

# Restrictions people tack onto a standard license text ("MIT, but you may not use this commercially").
# Kept to wording that restricts; plenty of OSI texts talk about commercial use without limiting it.
_MAY_NOT = r"\b(?:(?:may|must|shall|can|will)(?: not|not| never)|can't|cannot|won't|mustn't|shan't)"
_USE = r"(?:use|used|usage|purposes?|projects?|products?)"
RESTRICTION_RE = re.compile("|".join([
    rf"{_MAY_NOT} (?:be )?(?:used?|sold|sell|resold|resell|redistributed?|distributed?)\b[^.;]{{0,40}}\bcommercial",
    rf"{_MAY_NOT} (?:be )?(?:sold|sell|resold|resell)\b",
    r"\bcommercial (?:use|usage|purposes?|distribution|exploitation)\b[^.;]{0,40}\b(?:is|are) "
    r"(?:not (?:permitted|allowed)|prohibited|forbidden)",
    r"\b(?:no|without) commercial (?:use|usage|purposes?)\b",
    rf"\b(?:personal|private|non-?commercial|educational|academic) {_USE} only\b",
    rf"\bonly (?:for|be used for|be used in) (?:personal|private|non-?commercial|educational|academic) {_USE}",
    r"\bcommercial (?:use|usage|purposes?)\b[^.;]{0,40}\brequires?\b",
]))

# (SPDX ID, phrases that must all appear, phrases that must not). The first phrase is the
# license's opening, the rest are standard text from deeper in the same license, so a
# look-alike that borrows only the opening doesn't match. First match wins, so more
# specific entries come before the ones they contain.
PHRASES = [
    ("curl", ["copyright and permission notice", "permission to use, copy, modify, and distribute this software for any purpose with or without fee", "the name of a copyright holder shall not be used in advertising"], []),
    ("PostgreSQL", ["permission to use, copy, modify, and distribute this software and its documentation for any purpose, without fee, and without a written agreement", "has no obligations to provide maintenance, support, updates, enhancements, or modifications"], []),
    ("Artistic-2.0", ["this license establishes the terms under which a given free software package may be copied, modified, distributed, and/or redistributed", "copyright holder means the individual(s) or organization(s) named in the copyright notice for the entire package"], []),
    ("Artistic-1.0", ["the intent of this document is to state the conditions under which a package may be copied", "reasonable copying fee"], []),
    ("BSL-1.0", ["boost software license", "accompanying documentation covered by this license"], []),
    ("MIT", ["permission is hereby granted, free of charge", "the above copyright notice and this permission notice shall be included"], ["good, not evil"]),
    ("Apache-2.0", ["apache license", "version 2.0", "terms and conditions for use, reproduction, and distribution"], []),
    ("BSD-3-Clause", ["redistribution and use in source and binary forms", "neither the name", "this software is provided by the copyright holders and contributors"], []),
    ("BSD-2-Clause", ["redistribution and use in source and binary forms", "this software is provided by the copyright holders and contributors"], ["neither the name"]),
    ("AGPL-3.0", ["gnu affero general public license", "version 3", "network server software"], []),
    ("LGPL-2.1", ["gnu lesser general public license", "2.1", "this license, the lesser general public license, applies to some specially designated software"], []),
    ("LGPL-3.0", ["gnu lesser general public license", "version 3", "this version of the gnu lesser general public license incorporates the terms and conditions of version 3"], []),
    ("GPL-2.0", ["gnu general public license", "version 2", "the licenses for most software are designed to take away your freedom"], []),
    ("GPL-3.0", ["gnu general public license", "version 3", "the gnu general public license is a free, copyleft license for software"], []),
    ("MPL-2.0", ["mozilla public license version 2.0", "this source code form is subject to the terms of the mozilla public license"], []),
    ("ISC", ["permission to use, copy, modify, and/or distribute this software for any purpose", "disclaims all warranties"], []),
    ("Unlicense", ["this is free and unencumbered software released into the public domain", "unlicense.org"], []),
    ("Zlib", ["this software is provided 'as-is'", "altered source versions must be plainly marked", "this notice may not be removed or altered from any source distribution"], []),
    ("EPL-2.0", ["eclipse public license", "2.0", "accompanying program is provided under the terms of this eclipse public license"], []),
    # Only counted when allowed. NC and ND texts carry neither opening, so they never match.
    ("CC0-1.0", ["cc0 1.0 universal", "statement of purpose"], []),
    ("CC-BY-SA-4.0", ["attribution-sharealike 4.0 international", "sharealike"], []),
    ("CC-BY-4.0", ["attribution 4.0 international", "creative commons corporation"], []),
]

# README license names, lowercase.
NAMES = {
    "mit license": "MIT", "mit": "MIT",
    "apache license 2.0": "Apache-2.0", "apache license, version 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0",
    "bsd 3-clause": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause", "bsd-2-clause": "BSD-2-Clause",
    "gplv3": "GPL-3.0", "gpl-3.0": "GPL-3.0", "gplv2": "GPL-2.0", "gpl-2.0": "GPL-2.0",
    "lgpl-3.0": "LGPL-3.0", "lgpl-2.1": "LGPL-2.1", "agpl-3.0": "AGPL-3.0",
    "mpl 2.0": "MPL-2.0", "mpl-2.0": "MPL-2.0", "isc": "ISC",
    "unlicense": "Unlicense", "the unlicense": "Unlicense",
    "cc0": "CC0-1.0", "cc0-1.0": "CC0-1.0", "cc by 4.0": "CC-BY-4.0", "cc-by-4.0": "CC-BY-4.0",
    "creative commons attribution 4.0": "CC-BY-4.0", "cc by-sa 4.0": "CC-BY-SA-4.0",
}
NAME_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(n) for n in sorted(NAMES, key=len, reverse=True)) + r")(?![\w-])"
)
# A CC name next to one of these is a restricted variant (or a mix), so it is not a match.
NC_ND = frozenset({"nc", "noncommercial", "non", "nd", "noderivatives"})
NEGATION_RE = re.compile(r"\bnot\b|isn't|\bno longer\b|\bformerly\b")
HEADING_RE = re.compile(r"\s{0,3}(#{1,6})\s+(.*)")
SPDX_MAX_LINES = 10
SPDX_MAX_CHARS = 500
SPDX_MAX_DEPTH = 50
ID_NAME_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}$")
GENERIC = "a non-open-source license"


def extra_licenses_for(category: str) -> frozenset:
    return CONTENT_LICENSES if category == "learning-resources" else frozenset()


class Undecided(GitHubError):
    pass


class TooLarge(Undecided):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub("[\"\u201c\u201d]", "", text)).lower()


def _non_osi(text: str, markers=NON_OSI) -> str | None:
    t = _norm(text)
    return next((name for marker, name in markers if marker.lower() in t), None)


def _id_ok(i: str, allowed: frozenset = frozenset()) -> bool:
    return i in OSI or i in allowed or (i.endswith("+") and i[:-1] in OSI)


class _Bad(Exception):
    pass


def _spdx_eval(expr: str, allowed: frozenset = frozenset()) -> tuple[bool, str | None]:
    """(is OSI, first non-OSI ID). Raises _Bad on anything unparseable."""
    toks = expr.replace("(", " ( ").replace(")", " ) ").split()
    pos = 0
    bad = None

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    depth = 0

    def atom():
        nonlocal bad, depth
        t = peek()
        if t is None or t in ("AND", "OR", "WITH", ")"):
            raise _Bad
        take()
        if t == "(":
            depth += 1
            if depth > SPDX_MAX_DEPTH:
                raise _Bad
            v = or_()
            if peek() != ")":
                raise _Bad
            take()
            depth -= 1
            return v
        if peek() == "WITH":
            take()
            if peek() in (None, "AND", "OR", "WITH", "(", ")"):
                raise _Bad
            take()
        ok = _id_ok(t, allowed)
        if not ok and bad is None:
            bad = t
        return ok

    def and_():
        v = atom()
        while peek() == "AND":
            take()
            v = atom() and v
        return v

    def or_():
        v = and_()
        while peek() == "OR":
            take()
            v = and_() or v
        return v

    result = or_()
    if pos != len(toks):
        raise _Bad
    return result, bad


def _spdx_scan(text: str, allowed: frozenset = frozenset()) -> tuple[str | None, str | None]:
    """(osi expression, non-OSI name) from SPDX-License-Identifier lines."""
    found = bad = None
    seen = 0
    for line in text.split("\n"):
        i = line.find("SPDX-License-Identifier:")
        if i < 0:
            continue
        seen += 1
        if seen > SPDX_MAX_LINES:
            break
        expr = " ".join(line[i + 24:].split("*/")[0].split("-->")[0].replace("|", " ").split()).strip(" .,;:*#")
        try:
            if len(expr) > SPDX_MAX_CHARS:
                raise _Bad
            ok, name = _spdx_eval(expr, allowed)
        except (_Bad, RecursionError):
            bad = bad or GENERIC
            continue
        if ok:
            found = found or expr
        else:
            bad = bad or (name if ID_NAME_RE.match(name or "") else GENERIC)
    return found, bad


def _restricted(text: str) -> bool:
    return bool(RESTRICTION_RE.search(_norm(text).replace("\u2019", "'")))


def _detect_text(text: str, allowed: frozenset = frozenset()) -> tuple[str | None, str | None]:
    """(osi_id, non_osi_name) for one license file."""
    found, bad = _detect_known(text, allowed)
    if found and _restricted(text):
        # A fixed name, never the file's own words: this reason is posted publicly.
        return None, f"{found} with an added restriction"
    return found, bad


def _detect_known(text: str, allowed: frozenset = frozenset()) -> tuple[str | None, str | None]:
    bad = _non_osi(text)
    if bad:
        return None, bad
    found, bad = _spdx_scan(text, allowed)
    if bad:
        return None, bad
    if found:
        return found, None
    t = _norm(text).strip()
    if t in ("mit", "mit license"):
        return "MIT", None
    for spdx, need, forbid in PHRASES:
        if (spdx in OSI or spdx in allowed) and all(p in t for p in need) and not any(p in t for p in forbid):
            return spdx, None
    return None, None


def _readme_license(readme: str, allowed: frozenset = frozenset()) -> tuple[str | None, str | None]:
    """(osi_id, non_osi_name) from the README's License section."""
    lines = readme.splitlines()
    for n, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m or m.group(2).strip(" #:").lower() not in ("license", "licence", "licenses", "licences"):
            continue
        level = len(m.group(1))
        body = []
        for k in range(n + 1, min(len(lines), n + 200)):
            h = HEADING_RE.match(lines[k])
            if h and len(h.group(1)) <= level:
                break
            body.append(lines[k])
        section = "\n".join(body)[:5000]
        bad = _non_osi(section)
        if bad:
            return None, bad
        first = [ln for ln in body if ln.strip()][:2]
        for ln in first:
            low = _norm(ln)
            m = NAME_RE.search(low)
            if m and not NEGATION_RE.search(low) and not NC_ND.intersection(re.findall(r"[a-z0-9]+", low)) \
                    and _id_ok(NAMES[m.group(1)], allowed):
                if _restricted(section):
                    return None, f"{NAMES[m.group(1)]} with an added restriction"
                return NAMES[m.group(1)], None
    return None, None


def file_bytes(gh, r, data: dict, what: str) -> bytes:
    """The bytes of a contents-API file. Undecided only when the content can't be decoded."""
    if data.get("encoding") == "none" and data.get("sha"):
        if (data.get("size") or 0) > MAX_BLOB:
            raise TooLarge(0, f"{what} of {r} is over {MAX_BLOB // 1_000_000} MB")
        # Over 1 MB the contents API leaves content empty; the blobs API serves up to 100 MB.
        data = gh.get(f"/repos/{r}/git/blobs/{data['sha']}")
        if data is None:
            raise GitHubError(404, f"blob for {what} of {r} not found")
    if data.get("encoding") != "base64":
        raise Undecided(0, f"{what} of {r} has no readable content")
    try:
        # validate=True: otherwise stray characters are dropped and garbage decodes as a short file.
        return base64.b64decode("".join((data.get("content") or "").split()), validate=True)
    except (binascii.Error, ValueError) as e:
        raise Undecided(0, f"{what} of {r} could not be decoded: {e}") from e


def _read_file(gh, r, path) -> str:
    data = gh.get(f"/repos/{r}/contents/{path}")
    if not data:
        return ""
    try:
        raw = file_bytes(gh, r, data, path)
    except TooLarge:
        # No license text, so the rule fails closed; rot's second sighting still guards a removal.
        return ""
    return raw[:MAX_BYTES].decode("utf-8", errors="replace")


def find_license(gh, r, readme_text: str | None, allowed: frozenset = frozenset()) -> tuple[str | None, str | None, bool]:
    """(osi_id, non_osi_name, cc_only); (None, None, False) when nothing was found. API errors raise.

    cc_only: nothing acceptable here, but a content license is present that another category accepts.
    """
    listing = gh.get(f"/repos/{r}/contents/")
    if not isinstance(listing, list):
        listing = []
    entries = sorted(
        ((e["name"], e.get("type")) for e in listing
         if e.get("type") in ("file", "symlink") and FILE_RE.match(e.get("name") or "")),
        # license-like names first so the real license is inside the cap
        key=lambda e: (not LICENSE_RE.match(e[0]), e[0].lower(), e[0]),
    )[:MAX_FILES]
    if not entries:
        found, bad = _readme_license(readme_text or "", allowed)
        cc_only = not (found or bad or allowed) and bool(_readme_license(readme_text or "", CONTENT_LICENSES)[0])
        return found, bad, cc_only
    # One read per file, in order, stopping at the first rejection so a later unreadable file can't hide it.
    texts = []
    found = None
    for name, kind in entries:
        if kind != "file":
            continue  # a symlink counts as a license file but isn't followed
        text = _read_file(gh, r, name)
        texts.append(text)
        osi, bad = _detect_text(text, allowed)
        if bad:
            return None, bad, False
        found = found or osi
    if found or allowed:
        return found, None, False
    # Nothing here, so see whether a content license (already read) is what's present.
    cc_only = any(_detect_text(t, CONTENT_LICENSES)[0] for t in texts)
    return None, None, cc_only
