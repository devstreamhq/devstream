import json
import os
from pathlib import Path
from typing import Literal, TypedDict, get_args

DATA = Path(__file__).resolve().parents[2] / "data"
DEFAULT_PATH = str(DATA / "entries.json")

Status = Literal["live", "warned", "flagged", "hidden", "archived", "superseded"]
STATUSES: tuple[str, ...] = get_args(Status)


class Entry(TypedDict):
    repo: str
    repo_id: int
    category: str
    description: str
    submitted_by: str
    submitted_at: str
    issue: int
    status: Status
    warned_at: str | None
    superseded_by: str | None
    added_by_seed: bool


CATEGORIES: dict[str, str] = {
    c["slug"]: c["name"]
    for c in json.loads((DATA / "categories.json").read_text(encoding="utf-8"))
}


def normalize_repo(s: str) -> str:
    s = s.strip().lower().rstrip("/")
    s = s.removeprefix("https://github.com/").rstrip("/")
    return s.removesuffix(".git")


def sort_key(e: dict) -> str:
    return e["repo"].lower()


def load(path: str = DEFAULT_PATH) -> list[Entry]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["entries"]


def save(entries: list[Entry], path: str = DEFAULT_PATH) -> None:
    doc = {"version": 1, "entries": sorted(entries, key=sort_key)}
    # Written beside the target and renamed over it, so a crash mid-write never leaves half a file.
    tmp = Path(f"{path}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def find(entries: list[Entry], repo: str) -> Entry | None:
    want = normalize_repo(repo)
    return next((e for e in entries if normalize_repo(e["repo"]) == want), None)
