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
]

# (SPDX ID, phrases that must all appear, phrases that must not). First match wins,
# so the more specific entries come before the ones they contain.
PHRASES = [
    ("curl", ["copyright and permission notice", "permission to use, copy, modify, and distribute this software for any purpose with or without fee"], []),
    ("PostgreSQL", ["permission to use, copy, modify, and distribute this software and its documentation for any purpose, without fee, and without a written agreement"], []),
    ("MIT", ["permission is hereby granted, free of charge"], []),
    ("Apache-2.0", ["apache license", "version 2.0"], []),
    ("BSD-3-Clause", ["redistribution and use in source and binary forms", "neither the name"], []),
    ("BSD-2-Clause", ["redistribution and use in source and binary forms"], ["neither the name"]),
    ("AGPL-3.0", ["gnu affero general public license"], []),
    ("LGPL-2.1", ["gnu lesser general public license", "2.1"], []),
    ("LGPL-3.0", ["gnu lesser general public license", "version 3"], []),
    ("GPL-2.0", ["gnu general public license", "version 2"], []),
    ("GPL-3.0", ["gnu general public license", "version 3"], []),
    ("MPL-2.0", ["mozilla public license version 2.0"], []),
    ("ISC", ["permission to use, copy, modify, and/or distribute this software for any purpose"], []),
    ("Unlicense", ["this is free and unencumbered software released into the public domain"], []),
    ("Zlib", ["this software is provided 'as-is'", "altered source versions must be plainly marked"], []),
    ("BSL-1.0", ["boost software license"], []),
    ("EPL-2.0", ["eclipse public license", "2.0"], []),
]

# README license names, lowercase. Longest names are tried first.
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


class Undecided(GitHubError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _non_osi(text: str) -> str | None:
    t = _norm(text)
    return next((name for marker, name in NON_OSI if marker.lower() in t), None)


def _id_ok(i: str) -> bool:
    return i in OSI or (i.endswith("+") and i[:-1] in OSI)


def _spdx_expr_ok(expr: str) -> bool:
    # shortcut: parentheses are flattened, so "A AND (B OR C)" reads as "A AND B" or "C". Upgrade if real files need it.
    expr = expr.replace("(", " ").replace(")", " ")
    for alt in re.split(r"\s+OR\s+", expr.strip()):
        ids = [re.split(r"\s+WITH\s+", part.strip())[0] for part in re.split(r"\s+AND\s+", alt)]
        if all(i and _id_ok(i) for i in ids):
            return True
    return False


def _detect_text(text: str) -> str | None:
    for m in re.finditer(r"SPDX-License-Identifier:[ \t]*([^\n\r]+)", text):
        expr = m.group(1).split("*/")[0].strip()
        if _spdx_expr_ok(expr):
            return expr
    t = _norm(text)
    for spdx, need, forbid in PHRASES:
        if all(p in t for p in need) and not any(p in t for p in forbid):
            return spdx
    return None


def _readme_license(readme: str) -> tuple[str | None, str | None]:
    """(osi_id, non_osi_name) from the README's License section."""
    lines = readme.splitlines()
    for n, line in enumerate(lines):
        m = re.match(r"\s{0,3}(#{1,6})\s*(licen[cs]e)s?\b", line, re.I)
        if not m:
            continue
        level = len(m.group(1))
        body = []
        for nxt in lines[n + 1:]:
            h = re.match(r"\s{0,3}(#{1,6})\s", nxt)
            if h and len(h.group(1)) <= level:
                break
            body.append(nxt)
        section = "\n".join(body)
        bad = _non_osi(section)
        if bad:
            return None, bad
        low = _norm(section)
        for name in sorted(NAMES, key=len, reverse=True):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", low):
                return NAMES[name], None
        for i in OSI:
            if re.search(rf"(?<![\w.-]){re.escape(i.lower())}(?![\w.-])", low):
                return i, None
    return None, None


def _read_file(gh, r, path) -> str:
    data = gh.get(f"/repos/{r}/contents/{path}")
    if not data or data.get("encoding") != "base64":
        return ""
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
    names = sorted(
        e["name"] for e in listing if e.get("type") == "file" and FILE_RE.match(e.get("name") or "")
    )[:MAX_FILES]
    if not names:
        return _readme_license(readme_text or "")
    found = None
    for name in names:
        text = _read_file(gh, r, name)
        bad = _non_osi(text)
        if bad:
            return None, bad
        found = found or _detect_text(text)
    return found, None
