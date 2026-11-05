import re

from .entries import CATEGORIES, normalize_repo

MAX_BODY = 20_000
HEADINGS = {
    "repository": "repository",
    "category": "category",
    "description": "description",
    "checklist": "checklist",
}
CHECKS = ("i read the quality bar", "this repo is open source")
BAD_REPO = "That doesn't look like a GitHub repo link."
INCOMPLETE = "The form looks incomplete. Please open a new submission and fill in every field."
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.I)


class FormError(Exception):
    """The message is shown publicly to the submitter."""


def _repo(raw: str) -> str:
    s = re.split(r"[#?]", raw.strip(), maxsplit=1)[0]
    if "://" in s and not HOST_RE.match(s):
        raise FormError(BAD_REPO)
    s = HOST_RE.sub("", s)
    parts = s.rstrip("/").split("/")
    if len(parts) > 2 and parts[2].lower() not in ("tree", "blob"):
        raise FormError(BAD_REPO)
    if len(parts) < 2:
        raise FormError(BAD_REPO)
    owner, name = parts[0], parts[1].removesuffix(".git")
    cand = f"{owner}/{name}"
    if not REPO_RE.match(cand) or not name.strip("."):
        raise FormError(BAD_REPO)
    return normalize_repo(cand)


def parse_submission(body: str) -> dict[str, str]:
    if len(body) > MAX_BODY:
        raise FormError("That submission is too long. Please keep each answer short.")
    sections: dict[str, str] = {}
    key = None
    for line in body.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^### (.+?)\s*$", line)
        if m:
            key = HEADINGS.get(m.group(1).strip().lower())
            if key in sections:  # first duplicate heading wins
                key = None
            elif key:
                sections[key] = ""
        elif key:
            sections[key] += line + "\n"
    sections = {k: v.strip() for k, v in sections.items()}
    for k in HEADINGS.values():
        if not sections.get(k) or sections[k] == "_No response_":
            raise FormError(INCOMPLETE)

    checked = {
        m.group(1).strip().lower()
        for m in re.finditer(r"^- \[[xX]\] (.+)$", sections["checklist"], re.M)
    }
    if not all(c in checked for c in CHECKS):
        raise FormError("Please tick both boxes in the checklist, then submit again.")

    by_name = {n.lower(): slug for slug, n in CATEGORIES.items()}
    slug = by_name.get(sections["category"].lower())
    if slug is None:
        raise FormError("We don't recognise that category. Please pick one from the list.")
    return {
        "repository": _repo(sections["repository"]),
        "category": slug,
        "description": sections["description"],
    }
