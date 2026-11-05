import json
import os
import re
import tempfile
from datetime import datetime, timedelta

ORG = "devstreamhq"
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TTL = timedelta(days=7)
MIN_AGE = timedelta(days=30)

BLOCKED = "This account can't submit to DevStream."
BOT = "Bot accounts can't submit or Like."
NOT_FOUND = "We couldn't find that GitHub account."
TOO_NEW = "Your account needs to be at least 30 days old."
NO_ACTIVITY = "We couldn't find public activity on your account in the last year."

ACTIVITY_QUERY = """
query UserActivity($login: String!, $from: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from) {
      contributionCalendar { totalContributions }
    }
  }
}
"""


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _check(login, gh, now, excluded):
    """Return (reason or None, created_at or None). API errors propagate."""
    if login.lower() in {e.lower() for e in excluded}:
        return BLOCKED, None
    if login.lower().endswith("[bot]"):
        return BOT, None
    if not LOGIN_RE.match(login):
        return NOT_FOUND, None
    if gh.status(f"/orgs/{ORG}/blocks/{login}") == 204:
        return BLOCKED, None
    user = gh.get(f"/users/{login}")
    if user is None:
        return NOT_FOUND, None
    created = user["created_at"]
    if user.get("type") == "Bot":
        return BOT, created
    if now - _parse(created) < MIN_AGE:
        return TOO_NEW, created
    if user["public_repos"] == 0:
        data = gh.graphql(ACTIVITY_QUERY, {"login": login, "from": _iso(now - timedelta(days=365))})
        total = data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
        if total < 1:
            return NO_ACTIVITY, created
    return None, created


def check_user(login: str, gh, now: datetime, excluded: set[str]) -> str | None:
    return _check(login, gh, now, excluded)[0]


class TrustCache:
    def __init__(self, path):
        self.path = str(path)
        try:
            with open(self.path) as f:
                self.entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # A bad cache only costs re-checks.
            self.entries = {}

    def get(self, login, now):
        e = self.entries.get(login.lower())
        if e is None or now - _parse(e["checked_at"]) > TTL:
            return None
        return e

    def put(self, login, result, now):
        self.entries[login.lower()] = {
            "trusted": result["trusted"],
            "reason": result.get("reason"),
            "checked_at": _iso(now),
            "created_at": result.get("created_at"),
        }

    def save(self):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.entries, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    def trusted(self, login, gh, now, excluded) -> bool:
        # Exclusions can change between runs, so they never read the cache.
        if login.lower() not in {e.lower() for e in excluded}:
            e = self.get(login, now)
            if e is not None:
                return e["trusted"]
        reason, created = _check(login, gh, now, excluded)
        self.put(login, {"trusted": reason is None, "reason": reason, "created_at": created}, now)
        return reason is None

    def account_age_days(self, login, now) -> int | None:
        e = self.get(login, now)
        if e is None or not e["created_at"]:
            return None
        return (now - _parse(e["created_at"])).days
