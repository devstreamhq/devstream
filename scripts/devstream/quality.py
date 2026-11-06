import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from scripts.devstream.gh import GitHubError
from scripts.devstream.license_detect import OSI, Undecided, find_license

NO_LICENSE = {None, "NOASSERTION", "NONE"}
DEPRECATED = re.compile(r"\b(deprecated|unmaintained|no longer maintained)\b", re.I)
# Project-level README statements only: a marker opening a line (after up to 10 symbols,
# emoji or one-level HTML tags), a "Status:" line, "this/the <project> is <marker>", or a
# marker after a dash, colon or bracket on the first line. Every repeat is capped, so no ReDoS.
_MARK = r"deprecated|unmaintained|no\s{1,3}longer\s{1,3}(?:actively\s{1,3})?maintained"
_END = r"(?![A-Za-z0-9])"
README_DEPRECATED = re.compile(
    r"^(?:<[^<>\n]{1,100}>|[^A-Za-z0-9\n<]){0,10}(?P<a>" + _MARK + r")" + _END
    + r"|^[^A-Za-z0-9\n<]{0,10}status[ \t]{0,3}:[^A-Za-z0-9\n]{0,10}(?P<c>" + _MARK + r")" + _END
    + r"|\b(?:this|the)(?:[ \t]{1,5}(?:project|repo|repository|library|package|tool|crate|module|app|plugin))?"
    r"[ \t]{1,5}(?:is|was|has[ \t]{1,5}been)[ \t]{1,5}(?:(?:now|officially|currently)[ \t]{1,5}){0,2}"
    r"(?P<b>deprecated|unmaintained|archived|no[ \t]{1,5}longer[ \t]{1,5}(?:actively[ \t]{1,5})?maintained)" + _END,
    re.I | re.M,
)
# Title lines like "foo - DEPRECATED" or "# foo (deprecated)"; first non-blank line only.
README_TITLE_DEPRECATED = re.compile(r"[-\u2013\u2014:|(\[][ \t]{0,3}(?P<d>" + _MARK + r")" + _END, re.I)
README_MIN = 300
README_SCAN = 200_000

MSG_GONE = "This repo no longer exists."
MSG_LICENSE = "We need an open-source license GitHub can recognise (like MIT or Apache-2.0)."
MSG_NON_OSI = "This repo's license ({}) isn't an open-source license, so it can't be listed."
MSG_NOT_FOUND = (
    "We couldn't find an open-source license. If this is your repo, add a standard LICENSE file "
    "(choosealicense.com has them) so GitHub can detect it, then submit again."
)
MSG_NO_README = "Add a README so people know what this is."
MSG_SHORT_README = "Your README needs a bit more detail (at least a short paragraph about what it does)."
MSG_FORK = "Forks need their own commits to be listed."
MSG_DEPRECATED = "This repo says it's \"{}\". If that's not right, update the description or README and submit again."
MSG_NO_DESC = "Add a short description to the repo so people know what it is."
MSG_STALE = "This repo hasn't had a push in the last 90 days."
MSG_ARCHIVED = "This repo is archived. Unarchive it to be listed."
MSG_NEW = (
    "New repos need to be at least 30 days old before they can be listed. "
    "Come back once yours is a month old."
)


@dataclass
class RepoResult:
    exists: bool
    canonical: str
    failures: list[str] = field(default_factory=list)
    rot: Literal["ok", "warn", "remove"] = "ok"
    meta: dict = field(default_factory=dict)


def _readme_text_length(text: str) -> int:
    text = text[:README_SCAN]  # also bounds the regex work below
    text = re.sub(r"```.*?(```|\Z)", " ", text, flags=re.S)
    text = re.sub(r"^\s{0,3}#.*$", " ", text, flags=re.M)
    text = re.sub(r"<[^>]{0,2000}>", " ", text)
    text = re.sub(r"!\[[^\]]{0,500}\]\([^)]{0,2000}\)", " ", text)
    text = re.sub(r"\[([^\]]{0,500})\]\([^)]{0,2000}\)", r"\1", text)
    return len(re.sub(r"\s+", " ", text).strip())


def _license(repo, gh, r, readme_text) -> str | None:
    """None when fine, else the failure message."""
    spdx = (repo.get("license") or {}).get("spdx_id")
    if spdx == "NOASSERTION":
        spdx = ((gh.get(f"/repos/{r}/license") or {}).get("license") or {}).get("spdx_id")
    if spdx not in NO_LICENSE:
        return None if spdx in OSI else MSG_LICENSE
    if spdx == "NONE":
        return MSG_LICENSE
    found, bad = find_license(gh, r, readme_text)
    if found:
        return None
    return MSG_NON_OSI.format(bad) if bad else MSG_NOT_FOUND


def _readme(gh, r) -> tuple[str | None, int] | None:
    """None for no README, else (text, length). text is None when GitHub withholds it (over 1 MB)."""
    data = gh.get(f"/repos/{r}/readme")
    if data is None:
        return None
    if data.get("encoding") == "none":
        return None, data.get("size") or 0
    try:
        raw = base64.b64decode("".join((data.get("content") or "").split()), validate=False)
    except (binascii.Error, ValueError) as e:
        raise Undecided(0, f"README of {r} could not be decoded: {e}") from e
    text = raw.decode("utf-8", errors="replace")[:README_SCAN]
    return text, _readme_text_length(text)


def _fork_diverged(repo, gh) -> bool:
    parent = repo["parent"]
    spec = f"{parent['default_branch']}...{repo['owner']['login']}:{repo['default_branch']}"
    cmp_ = gh.get(f"/repos/{parent['full_name']}/compare/{spec}")
    return cmp_ is not None and cmp_.get("ahead_by", 0) > 0


def _extras_missing(repo, gh, r) -> list[str]:
    """Empty list when 2+ extras exist. Cheap checks first; stops at 2."""
    found = 0
    missing = []

    def tally(ok, label):
        nonlocal found
        if ok:
            found += 1
        else:
            missing.append(label)
        return found >= 2

    if tally(len(repo.get("topics") or []) >= 1, "a topic or two"):
        return []
    files = (gh.get(f"/repos/{r}/community/profile") or {}).get("files") or {}
    checks = [
        (files.get("contributing") is not None, "a CONTRIBUTING guide"),
        (files.get("code_of_conduct") is not None, "a code of conduct"),
        (files.get("issue_template") is not None or files.get("pull_request_template") is not None,
         "an issue or pull request template"),
    ]
    for ok, label in checks:
        if tally(ok, label):
            return []
    sec = any(gh.get(f"/repos/{r}/contents/{p}") is not None
              for p in ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md"))
    if tally(sec, "a SECURITY policy"):
        return []
    if tally(bool(gh.get(f"/repos/{r}/contents/.github/workflows")), "GitHub Actions workflows"):
        return []
    if tally(bool(gh.get(f"/repos/{r}/releases?per_page=1")), "a release"):
        return []
    return missing


def _old_enough(created_at, now) -> bool:
    try:
        return now - datetime.fromisoformat(created_at) >= timedelta(days=30)
    except (TypeError, ValueError):
        return False


def check_repo(full: str, gh, now: datetime, mode: Literal["submit", "rot"]) -> RepoResult:
    repo = gh.get(f"/repos/{full}")
    if repo is None:
        return RepoResult(False, full, [MSG_GONE], "remove")
    # A redirect's final path is often /repositories/{id}, so the name comes from the body.
    r = repo["full_name"]
    meta = {
        "stars": repo.get("stargazers_count"),
        "language": repo.get("language"),
        "pushed_at": repo.get("pushed_at"),
        "description": repo.get("description"),
        "avatar_url": (repo.get("owner") or {}).get("avatar_url"),
        "default_branch": repo.get("default_branch"),
        "html_url": repo.get("html_url"),
    }
    pushed = repo.get("pushed_at")
    if pushed is None and mode == "rot":
        raise Undecided(0, f"{r} has no pushed_at")
    age = None if pushed is None else now - datetime.fromisoformat(pushed)
    readme = _readme(gh, r)
    license_msg = _license(repo, gh, r, readme[0] if readme else None)
    license_ok = license_msg is None
    readme_ok = readme is not None and readme[1] >= README_MIN
    text = (readme[0] if readme else None) or ""
    m = DEPRECATED.search(repo.get("description") or "")
    rm = README_DEPRECATED.search(text[:500])
    if not rm:
        title = next((ln for ln in text[:500].splitlines() if ln.strip()), "")
        rm = README_TITLE_DEPRECATED.search(title)
    deprecated = m.group(0) if m else (rm.group(rm.lastgroup) if rm else None)

    failures = []
    if not license_ok:
        failures.append(license_msg)
    if readme is None:
        failures.append(MSG_NO_README)
    elif not readme_ok:
        failures.append(MSG_SHORT_README)

    if mode == "rot":
        if repo.get("archived"):
            failures.append(MSG_ARCHIVED)
        if deprecated:
            failures.append(MSG_DEPRECATED.format(deprecated))
        if age > timedelta(days=365):
            failures.append("This repo hasn't had a push in over a year.")
        if not (license_ok and readme_ok):
            rot = "remove"
        elif len(failures):
            rot = "warn"
        else:
            rot = "ok"
        return RepoResult(True, r, failures, rot, meta)

    if age is None or age > timedelta(days=90):
        failures.append(MSG_STALE)
    if repo.get("archived"):
        failures.append(MSG_ARCHIVED)
    if not _old_enough(repo.get("created_at"), now):
        failures.append(MSG_NEW)
    if repo.get("fork") and not _fork_diverged(repo, gh):
        failures.append(MSG_FORK)
    if not (repo.get("description") or "").strip():
        failures.append(MSG_NO_DESC)
    if deprecated:
        failures.append(MSG_DEPRECATED.format(deprecated))
    missing = _extras_missing(repo, gh, r)
    if missing:
        failures.append("Add at least two of these to the repo: " + ", ".join(missing) + ".")
    return RepoResult(True, r, failures, "ok", meta)
