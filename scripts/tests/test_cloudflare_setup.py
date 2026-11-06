import importlib.util
import io
import json
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloudflare_setup", ROOT / "scripts/admin/cloudflare_setup.py")
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)


class FakeApi:
    """Answers the handful of Cloudflare calls the setup makes, and records every write."""

    def __init__(self, project=True, domain=True, record=None, subdomain="devstream-2lv.pages.dev"):
        self.project, self.domain, self.record, self.subdomain = project, domain, record, subdomain
        self.writes = []

    def __call__(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, body))
        if path == "/accounts/acct/pages/projects/devstream":
            if not self.project:
                raise cf.ApiError(404, "not found")
            result = {"name": "devstream"}
            if self.subdomain is not None:
                result["subdomain"] = self.subdomain
            return result
        if path == "/accounts/acct/pages/projects" and method == "POST":
            self.project = True
            return {"name": "devstream"}
        if path == "/accounts/acct/pages/projects/devstream/domains/devstream.jamesconway.dev":
            return {"name": "devstream.jamesconway.dev", "status": "pending"}
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


GOOD = {"id": "r1", "type": "CNAME", "name": "devstream.jamesconway.dev", "content": "devstream-2lv.pages.dev", "proxied": True}


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
                    {"type": "CNAME", "name": "devstream", "content": "devstream-2lv.pages.dev", "proxied": True, "ttl": 1},
                ),
            ],
        )

    def test_fixes_only_the_devstream_record(self):
        api = FakeApi(record=dict(GOOD, proxied=False, content="old.example.com"))
        cf.run(api, "acct")
        self.assertEqual(
            api.writes,
            [("PATCH", "/zones/zone1/dns_records/r1", {"content": "devstream-2lv.pages.dev", "proxied": True})],
        )

    def test_cname_targets_project_subdomain(self):
        api = FakeApi()
        cf.run(api, "acct")
        self.assertEqual(
            api.writes,
            [("POST", "/zones/zone1/dns_records", {"type": "CNAME", "name": "devstream", "content": "devstream-2lv.pages.dev", "proxied": True, "ttl": 1})],
        )

    def test_wrong_cname_is_corrected(self):
        api = FakeApi(record=dict(GOOD, content="devstream.pages.dev"))
        cf.run(api, "acct")
        self.assertEqual(
            api.writes,
            [("PATCH", "/zones/zone1/dns_records/r1", {"content": "devstream-2lv.pages.dev", "proxied": True})],
        )

    def test_missing_subdomain_raises(self):
        for sub in (None, ""):
            with self.subTest(sub=sub):
                api = FakeApi(subdomain=sub)
                with self.assertRaises(cf.ApiError):
                    cf.run(api, "acct")
                self.assertEqual([w for w in api.writes if "dns_records" in w[1]], [])

    def test_prints_custom_domain_status(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cf.run(FakeApi(record=dict(GOOD)), "acct")
        self.assertIn("pending", out.getvalue())

    def test_refuses_to_replace_a_non_cname_record(self):
        api = FakeApi(record=dict(GOOD, type="A", content="192.0.2.1"))
        with self.assertRaises(SystemExit):
            cf.run(api, "acct")
        self.assertEqual(api.writes, [])


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def respond(body):
    return mock.patch("urllib.request.urlopen", return_value=Response(json.dumps(body).encode()))


class HttpApi(unittest.TestCase):
    def test_returns_result_on_success(self):
        with respond({"success": True, "result": [{"id": "z"}]}):
            self.assertEqual(cf.http_api("t")("GET", "/zones"), [{"id": "z"}])

    def test_raises_when_success_is_not_true(self):
        for body in ({"success": False, "errors": [{"message": "nope"}], "result": None}, {"result": []}):
            with self.subTest(body=body), respond(body), self.assertRaises(cf.ApiError):
                cf.http_api("t")("GET", "/zones")


if __name__ == "__main__":
    unittest.main()
