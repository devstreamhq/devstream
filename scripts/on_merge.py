import os
import logging
import time

from scripts.devstream import entries as entries_mod
from scripts.devstream.discussions import ensure_discussion, likes_category, list_discussions
from scripts.devstream.gh import GitHub
from scripts.devstream.entries import normalize_repo
from scripts.devstream.stats import ensure_branch, read_json, update_json

CAP = 40
VISIBLE = {"live", "warned", "flagged"}


def _follow(mapping: dict, pending: dict, listed: set[str]) -> dict:
    """Finish a rename whose key move failed in rot's finish, so the entry keeps its thread and its Likes.

    pending holds only renames.json records rot marked pending; a completed move is never replayed.
    Overrides a stored new key: that is the dropped entry's thread after a rename collision.
    An old name listed again is someone else's entry and keeps its own key.
    """
    for old, r in sorted(pending.items(), key=lambda kv: kv[1]["at"]):
        if old in mapping and old not in listed:
            mapping[normalize_repo(r["to"])] = mapping.pop(old)
    return mapping


def run(gh, entries, sleep=time.sleep):
    ensure_branch(gh)
    cur = read_json(gh, "discussions.json")[0]
    mapping = {normalize_repo(k): v for k, v in (cur or {}).items()}
    before = dict(mapping)
    repo_id, category_id = likes_category(gh)
    # A failed stats write leaves threads the mapping doesn't know; adopt them, never re-create.
    # setdefault: a stored mapping always wins, so a thread retitled onto a listed name can't take it over.
    for k, v in list_discussions(gh, category_id).items():
        mapping.setdefault(k, v)
    # After adopting, so a thread still titled with the old name can't put the old key back.
    pending = {k: r for k, r in (read_json(gh, "renames.json")[0] or {}).items()
               if isinstance(r, dict) and r.get("pending")}
    listed = {normalize_repo(e["repo"]) for e in entries}
    _follow(mapping, pending, listed)
    todo = {}
    for e in entries:
        if e["status"] in VISIBLE and normalize_repo(e["repo"]) not in mapping:
            todo.setdefault(normalize_repo(e["repo"]), e)
    err = None
    try:
        for i, e in enumerate(list(todo.values())[:CAP]):
            if i:
                sleep(2)
            mapping[normalize_repo(e["repo"])] = ensure_discussion(e, gh, repo_id, category_id, mapping)
    except BaseException as exc:
        err = exc
        raise
    finally:
        # Persist what exists even when a create failed, so no Discussion is orphaned.
        added = {k: v for k, v in mapping.items() if before.get(k) != v}

        def merge(stored):
            # Re-applied onto a fresh read after a 409; a stored mapping still wins, once renames are followed.
            return {**added, **_follow({normalize_repo(k): v for k, v in stored.items()}, pending, listed)}

        def done(stored):
            # Replayed once: a later relist and unlist of the old name must never pull its thread back.
            return {k: {f: x for f, x in v.items() if f != "pending"} if pending.get(k) == v else v
                    for k, v in stored.items()}
        try:
            if mapping != before:
                update_json(gh, "discussions.json", merge, "chore(stats): map entry discussions")
            if pending:
                update_json(gh, "renames.json", done, "chore(stats): finish renamed repos")
        except Exception:
            if err is None:
                raise
            logging.exception("could not write discussions.json or renames.json")


def main():
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load())


if __name__ == "__main__":
    main()
