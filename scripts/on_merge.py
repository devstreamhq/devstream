import base64
import json
import os
import time

from scripts.devstream import entries as entries_mod
from scripts.devstream.discussions import ensure_discussion, likes_category
from scripts.devstream.gh import GitHub

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
    cur = gh.get(FILE, {"ref": "stats"})
    sha = cur["sha"] if cur else None
    mapping = json.loads(base64.b64decode(cur["content"])) if cur else {}
    todo = [e for e in entries if e["status"] in VISIBLE and e["repo"] not in mapping][:CAP]
    if not todo:
        return
    repo_id, category_id = likes_category(gh)
    before = dict(mapping)
    try:
        for i, e in enumerate(todo):
            if i:
                sleep(2)
            mapping[e["repo"]] = ensure_discussion(e, gh, repo_id, category_id, mapping)
    finally:
        # Persist what exists even when a create failed, so no Discussion is orphaned.
        if mapping != before:
            _write(gh, mapping, sha)


def main():
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load())


if __name__ == "__main__":
    main()
