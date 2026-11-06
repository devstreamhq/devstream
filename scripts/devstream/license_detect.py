"""Find an OSI license in a repo when GitHub's own detection comes up empty."""

import base64
import binascii
import json
import re
from pathlib import Path

from scripts.devstream.gh import GitHubError

OSI = frozenset(json.loads((Path(__file__).parents[2] / "data" / "osi-licenses.json").read_text()))
FILE_RE = re.compile(r"^(licen[cs]e|copying|unlicense)([-._][\w.-]+)?(\.(md|txt|rst))?$", re.I)
MAX_FILES = 5
MAX_BYTES = 100_000

NON_OSI = [
    ("Server Side Public License", "Server Side Public License"),
    ("Business Source License", "Business Source License"),
    ("Elastic License", "Elastic License"),
    ("Redis Source Available License", "Redis Source Available License"),
    ("Commons Clause", "Commons Clause"),
    ("source-available", "source-available"),
    ("source available", "source-available"),
    ("non-commercial", "non-commercial"),
    ("noncommercial", "non-commercial"),
    ("PolyForm", "PolyForm"),
    ("Functional Source License", "Functional Source License"),
    ("Fair Source", "Fair Source"),
    ("good, not evil", "JSON License"),
]
# Standard BSD and MIT headers say "All rights reserved", so these only count when no OSI license is found.
WEAK_NON_OSI = [
    ("proprietary", "proprietary"),
    ("all rights reserved", "all rights reserved"),
]

# (SPDX ID, phrases that must all appear, phrases that must not). The first phrase is the
# license's opening, the rest are standard text from deeper in the same license, so a
# look-alike that borrows only the opening doesn't match. First match wins, so more
# specific entries come before the ones they contain.
PHRASES = [
    ("curl", ["copyright and permission notice", "permission to use, copy, modify, and distribute this software for any purpose with or without fee", "the name of a copyright holder shall not be used in advertising"], []),
    ("PostgreSQL", ["permission to use, copy, modify, and distribute this software and its documentation for any purpose, without fee, and without a written agreement", "has no obligations to provide maintenance, support, updates, enhancements, or modifications"], []),
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
}
NAME_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(n) for n in sorted(NAMES, key=len, reverse=True)) + r")(?![\w-])"
)
NEGATION_RE = re.compile(r"\bnot\b|isn't|\bno longer\b")
HEADING_RE = re.compile(r"\s{0,3}(#{1,6})\s+(.*)")
SPDX_MAX_LINES = 10
SPDX_MAX_CHARS = 500
ID_NAME_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}$")
GENERIC = "a non-open-source license"


class Undecided(GitHubError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _non_osi(text: str, markers=NON_OSI) -> str | None:
    t = _norm(text)
    return next((name for marker, name in markers if marker.lower() in t), None)


def _id_ok(i: str) -> bool:
    return i in OSI or (i.endswith("+") and i[:-1] in OSI)


class _Bad(Exception):
    pass


def _spdx_eval(expr: str) -> tuple[bool, str | None]:
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

    def atom():
        nonlocal bad
        t = peek()
        if t is None or t in ("AND", "OR", "WITH", ")"):
            raise _Bad
        take()
        if t == "(":
            v = or_()
            if peek() != ")":
                raise _Bad
            take()
            return v
        if peek() == "WITH":
            take()
            if peek() in (None, "AND", "OR", "WITH", "(", ")"):
                raise _Bad
            take()
        ok = _id_ok(t)
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


def _spdx_scan(text: str) -> tuple[str | None, str | None]:
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
        expr = " ".join(line[i + 24:].split("*/")[0].split("-->")[0].split())
        try:
            if len(expr) > SPDX_MAX_CHARS:
                raise _Bad
            ok, name = _spdx_eval(expr)
        except _Bad:
            bad = bad or GENERIC
            continue
        if ok:
            found = found or expr
        else:
            bad = bad or (name if ID_NAME_RE.match(name or "") else GENERIC)
    return found, bad


def _detect_text(text: str) -> tuple[str | None, str | None]:
    """(osi_id, non_osi_name) for one license file."""
    bad = _non_osi(text)
    if bad:
        return None, bad
    found, bad = _spdx_scan(text)
    if bad:
        return None, bad
    if found:
        return found, None
    t = _norm(text).strip()
    if t in ("mit", "mit license"):
        return "MIT", None
    for spdx, need, forbid in PHRASES:
        if all(p in t for p in need) and not any(p in t for p in forbid):
            return spdx, None
    return None, _non_osi(text, WEAK_NON_OSI)


def _readme_license(readme: str) -> tuple[str | None, str | None]:
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
            if m and not NEGATION_RE.search(low):
                return NAMES[m.group(1)], None
        bad = _non_osi(section, WEAK_NON_OSI)
        if bad:
            return None, bad
    return None, None


def _read_file(gh, r, path) -> str:
    data = gh.get(f"/repos/{r}/contents/{path}")
    if not data:
        return ""
    if data.get("encoding") != "base64":
        raise Undecided(0, f"{path} of {r} is too large to read")
    try:
        raw = base64.b64decode("".join((data.get("content") or "").split()), validate=False)
    except (binascii.Error, ValueError) as e:
        raise Undecided(0, f"{path} of {r} could not be decoded: {e}") from e
    return raw[:MAX_BYTES].decode("utf-8", errors="replace")


def find_license(gh, r, readme_text: str | None) -> tuple[str | None, str | None]:
    """(osi_id, non_osi_name); (None, None) when nothing was found. API errors raise."""
    listing = gh.get(f"/repos/{r}/contents/")
    if not isinstance(listing, list):
        listing = []
    entries = sorted(
        (e["name"], e.get("type")) for e in listing
        if e.get("type") in ("file", "symlink") and FILE_RE.match(e.get("name") or "")
    )[:MAX_FILES]
    if not entries:
        return _readme_license(readme_text or "")
    found = None
    for name, kind in entries:
        if kind != "file":
            continue  # a symlink counts as a license file but isn't followed
        osi, bad = _detect_text(_read_file(gh, r, name))
        if bad:
            return None, bad
        found = found or osi
    return found, None
