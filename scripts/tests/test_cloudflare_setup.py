import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloudflare_setup", ROOT / "scripts/admin/cloudflare_setup.py")
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)


class FakeApi:
    """Answers the handful of Cloudflare calls the setup makes, and records every write."""

    def __init__(self, project=True, domain=True, record=None):
        self.project, self.domain, self.record = project, domain, record
        self.writes = []

    def __call__(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, body))
        if path == "/accounts/acct/pages/projects/devstream":
            if not self.project:
                raise cf.ApiError(404, "not found")
            return {"name": "devstream"}
        if path == "/accounts/acct/pages/projects" and method == "POST":
            return {"name": "devstream"}
        if path == "/accounts/acct/pages/projects/devstream/domains":
            if method == "GET":
                return [{"name": "devstream.jamesconway.dev"}] if self.domain else [{"name": "other.example.com"}]
            return {"name": body["name"]}
        if path == "/zones?name=jamesconway.dev":
            return [{"id": "zone1", "name": "jamesconway.dev"}]
        if path == "/zones/zone1/dns_records?name=devstream.jamesconway.dev":
            return [self.record] if self.record else []
        if path.startswith("/zones/zone1/dns_records"):
            return {"id": "r1"}
        raise AssertionError(f"unexpected call {method} {path}")


GOOD = {"id": "r1", "type": "CNAME", "name": "devstream.jamesconway.dev", "content": "devstream.pages.dev", "proxied": True}


class CloudflareSetup(unittest.TestCase):
    def test_everything_present_writes_nothing(self):
        api = FakeApi(record=dict(GOOD))
        cf.run(api, "acct")
        self.assertEqual(api.writes, [])

    def test_creates_whatever_is_missing(self):
        api = FakeApi(project=False, domain=False)
        cf.run(api, "acct")
        self.assertEqual(
            api.writes,
            [
                ("POST", "/accounts/acct/pages/projects", {"name": "devstream", "production_branch": "main"}),
                ("POST", "/accounts/acct/pages/projects/devstream/domains", {"name": "devstream.jamesconway.dev"}),
                (
                    "POST",
                    "/zones/zone1/dns_records",
                    {"type": "CNAME", "name": "devstream", "content": "devstream.pages.dev", "proxied": True, "ttl": 1},
                ),
            ],
        )

    def test_fixes_only_the_devstream_record(self):
        api = FakeApi(record=dict(GOOD, proxied=False, content="old.example.com"))
        cf.run(api, "acct")
        self.assertEqual(
            api.writes,
            [("PATCH", "/zones/zone1/dns_records/r1", {"content": "devstream.pages.dev", "proxied": True})],
        )

    def test_refuses_to_replace_a_non_cname_record(self):
        api = FakeApi(record=dict(GOOD, type="A", content="192.0.2.1"))
        with self.assertRaises(SystemExit):
            cf.run(api, "acct")
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
