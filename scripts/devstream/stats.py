"""JSON files on the bot-only `stats` branch, read and written through the contents API."""

import base64
import copy
import json

from scripts.devstream.gh import GitHubError

API = "/repos/devstreamhq/devstream"
BOT = "devstreamhq-bot[bot]"
BOT_EMAIL = "353074438+devstreamhq-bot[bot]@users.noreply.github.com"


def ensure_branch(gh) -> None:
    if gh.get(f"{API}/git/ref/heads/stats") is None:
        raise GitHubError(404, "stats branch is missing; run scripts/admin/setup_stats.sh")


def read_json(gh, path):
    """Return (data, sha), or (None, None) when the file doesn't exist."""
    cur = gh.get(f"{API}/contents/{path}", {"ref": "stats"})
    if cur is None:
        return None, None
    return json.loads(base64.b64decode(cur["content"])), cur["sha"]


def write_json(gh, path, data, sha, message) -> None:
    body = {
        "message": message,
        "content": base64.b64encode((json.dumps(data, indent=2, sort_keys=True) + "\n").encode()).decode(),
        "branch": "stats",
        "committer": {"name": BOT, "email": BOT_EMAIL},
    }
    if sha:
        body["sha"] = sha
    gh.put(f"{API}/contents/{path}", body)


def update_json(gh, path, mutate, message) -> None:
    """Read, mutate(data) -> new data, write. On a 409 (another writer got there first) re-read and retry once."""
    for attempt in (1, 2):
        data, sha = read_json(gh, path)
        new = mutate(copy.deepcopy(data) if data is not None else {})
        if new == (data or {}):
            return
        try:
            write_json(gh, path, new, sha, message)
            return
        except GitHubError as e:
            if e.status != 409 or attempt == 2:
                raise
