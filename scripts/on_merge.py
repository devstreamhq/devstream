import os
import logging
import time

from scripts.devstream import entries as entries_mod
from scripts.devstream.discussions import ensure_discussion, likes_category, list_discussions
from scripts.devstream.gh import GitHub
from scripts.devstream.entries import normalize_repo
from scripts.devstream.stats import ensure_branch, read_json, write_json

CAP = 40
VISIBLE = {"live", "warned", "flagged"}


def run(gh, entries, sleep=time.sleep):
    ensure_branch(gh)
    cur, sha = read_json(gh, "discussions.json")
    mapping = {normalize_repo(k): v for k, v in (cur or {}).items()}
    before = dict(mapping)
    repo_id, category_id = likes_category(gh)
    # A failed stats write leaves threads the mapping doesn't know; adopt them, never re-create.
    # setdefault: a stored mapping always wins, so a thread retitled onto a listed name can't take it over.
    for k, v in list_discussions(gh, category_id).items():
        mapping.setdefault(k, v)
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
        if mapping != before:
            try:
                write_json(gh, "discussions.json", mapping, sha, "chore(stats): map entry discussions")
            except Exception:
                if err is None:
                    raise
                logging.exception("could not write discussions.json")


def main():
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load())


if __name__ == "__main__":
    main()
