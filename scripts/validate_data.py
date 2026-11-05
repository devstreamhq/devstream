import json
import re
import sys
from datetime import datetime

from scripts.devstream.description import clean_description
from scripts.devstream.entries import (
    CATEGORIES,
    DEFAULT_PATH,
    STATUSES,
    normalize_repo,
    sort_key,
)

REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")  # fullmatch: "$" would let a trailing newline through
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# name -> (type, nullable)
FIELDS = {
    "repo": (str, False),
    "category": (str, False),
    "description": (str, False),
    "submitted_by": (str, False),
    "submitted_at": (str, False),
    "issue": (int, False),
    "status": (str, False),
    "warned_at": (str, True),
    "superseded_by": (str, True),
    "added_by_seed": (bool, False),
}


def _ts_ok(v) -> bool:
    if not isinstance(v, str) or not TS_RE.match(v):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate(entries: list[dict]) -> list[str]:
    errors: list[str] = []
    seen: dict[str, int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            errors.append(f"entry {i}: not an object")
            continue
        at = f"entry {i} ({e.get('repo')})"
        for name, (typ, nullable) in FIELDS.items():
            if name not in e:
                errors.append(f"{at}: missing {name}")
                continue
            v = e[name]
            if v is None and nullable:
                continue
            # bool is an int subclass; don't let True pass as an issue number
            if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
                errors.append(f"{at}: {name} must be {typ.__name__}")
        for name in e.keys() - FIELDS.keys():
            errors.append(f"{at}: unknown field {name}")
        repo = e.get("repo")
        if isinstance(repo, str):
            if not REPO_RE.fullmatch(repo):
                errors.append(f"{at}: bad repo format")
            key = normalize_repo(repo)
            if key in seen:
                errors.append(f"{at}: duplicate of entry {seen[key]}")
            seen.setdefault(key, i)
        desc = e.get("description")
        # Backstop for anything that reaches main without passing the submission filter.
        if isinstance(desc, str) and clean_description(desc) != (desc, None):
            errors.append(f"{at}: description fails the description filter")
        if e.get("status") not in STATUSES:
            errors.append(f"{at}: unknown status {e.get('status')!r}")
        if e.get("category") not in CATEGORIES:
            errors.append(f"{at}: unknown category {e.get('category')!r}")
        if "submitted_at" in e and not _ts_ok(e["submitted_at"]):
            errors.append(f"{at}: submitted_at must be ISO-8601 UTC (…Z)")
        if e.get("warned_at") is not None and not _ts_ok(e["warned_at"]):
            errors.append(f"{at}: warned_at must be ISO-8601 UTC (…Z)")
    if all(isinstance(e, dict) and isinstance(e.get("repo"), str) for e in entries):
        keys = [sort_key(e) for e in entries]
        if keys != sorted(keys):
            errors.append("entries are not sorted case-insensitively by repo")
    return errors


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        entries = doc["entries"]
        if doc.get("version") != 1 or not isinstance(entries, list):
            raise ValueError("expected {version: 1, entries: [...]}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    errors = validate(entries)
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
