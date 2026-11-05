import json
from pathlib import Path
from typing import Literal, TypedDict

DATA = Path(__file__).resolve().parents[2] / "data"
DEFAULT_PATH = str(DATA / "entries.json")

STATUSES = ("live", "warned", "flagged", "hidden", "archived", "superseded")


class Entry(TypedDict):
    repo: str
    category: str
    description: str
    submitted_by: str
    submitted_at: str
    issue: int
    status: Literal["live", "warned", "flagged", "hidden", "archived", "superseded"]
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
    Path(path).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def find(entries: list[Entry], repo: str) -> Entry | None:
    want = normalize_repo(repo)
    return next((e for e in entries if normalize_repo(e["repo"]) == want), None)
