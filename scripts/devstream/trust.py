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
    """Return (reason or None, created_at or None). API errors propagate.

    excluded must already hold the org's blocked logins (blocked_logins), which read as BLOCKED.
    """
    if login.lower() in {e.lower() for e in excluded}:
        return BLOCKED, None
    if login.lower().endswith("[bot]"):
        return BOT, None
    if not LOGIN_RE.match(login):
        return NOT_FOUND, None
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
        except (FileNotFoundError, ValueError):
            # A bad cache only costs re-checks (ValueError covers bad JSON and bad UTF-8).
            self.entries = {}
        if not isinstance(self.entries, dict):
            self.entries = {}

    def get(self, login, now):
        e = self.entries.get(login.lower())
        try:
            age = now - _parse(e["checked_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return e if timedelta(0) <= age <= TTL else None

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
        if login.lower() not in {e.lower() for e in excluded} and LOGIN_RE.match(login):
            e = self.get(login, now)
            if e is not None:
                # A block added since caching is in excluded, so it took effect above.
                return e.get("trusted") is True
        reason, created = _check(login, gh, now, excluded)
        if reason == BLOCKED:
            # Not cached, so an unblock applies on the next run.
            self.entries.pop(login.lower(), None)
        else:
            self.put(login, {"trusted": reason is None, "reason": reason, "created_at": created}, now)
        return reason is None

    def account_age_days(self, login, now) -> int | None:
        e = self.get(login, now)
        try:
            return (now - _parse(e["created_at"])).days
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def blocked_logins(gh) -> set[str]:
    """The org's blocked logins, lowercased: one listing per run instead of one lookup per account."""
    # A token that can't read blocks gets 404, which must never read as "nobody blocked".
    return {u["login"].lower() for u in gh.paginate(f"/orgs/{ORG}/blocks?per_page=100", must_exist=True)}
