import base64
import json
import os
import logging
import time

from scripts.devstream import entries as entries_mod
from scripts.devstream.discussions import ensure_discussion, likes_category, list_discussions
from scripts.devstream.gh import GitHub, GitHubError
from scripts.devstream.entries import normalize_repo

CAP = 40
VISIBLE = {"live", "warned", "flagged"}
FILE = "/repos/devstreamhq/devstream/contents/discussions.json"
BOT = "devstreamhq-bot[bot]"
BOT_EMAIL = "353074438+devstreamhq-bot[bot]@users.noreply.github.com"


def _write(gh, mapping, sha):
    body = {
        "message": "chore(stats): map entry discussions",
        "content": base64.b64encode((json.dumps(mapping, indent=2, sort_keys=True) + "\n").encode()).decode(),
        "branch": "stats",
        "committer": {"name": BOT, "email": BOT_EMAIL},
    }
    if sha:
        body["sha"] = sha
    gh.put(FILE, body)


def run(gh, entries, sleep=time.sleep):
    if gh.get("/repos/devstreamhq/devstream/git/ref/heads/stats") is None:
        raise GitHubError(404, "stats branch is missing; run scripts/admin/setup_stats.sh")
    cur = gh.get(FILE, {"ref": "stats"})
    sha = cur["sha"] if cur else None
    mapping = (
        {normalize_repo(k): v for k, v in json.loads(base64.b64decode(cur["content"])).items()}
        if cur
        else {}
    )
    before = dict(mapping)
    repo_id, category_id = likes_category(gh)
    # A failed stats write leaves threads the mapping doesn't know; adopt them, never re-create.
    mapping.update(list_discussions(gh, category_id))
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
                _write(gh, mapping, sha)
            except Exception:
                if err is None:
                    raise
                logging.exception("could not write discussions.json")


def main():
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load())


if __name__ == "__main__":
    main()
