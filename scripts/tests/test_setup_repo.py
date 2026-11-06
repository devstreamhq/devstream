import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/admin/setup_repo.sh"

# Stands in for gh: logs each call, and answers the ruleset lookup and the org setting from the environment.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if "--input" in a:
    sys.stdin.read()
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
path = next((x for x in a if x.startswith(("repos/", "orgs/"))), "")
if "rulesets?includes_parents=false" in path:
    ids = os.environ.get("IDS_" + ("release" if "release-tags" in a[-1] else "main"), "")
    if ids:  # like gh --jq: nothing at all when nothing matches
        print(ids.replace(",", "\\n"))
elif path.startswith("orgs/"):
    print(os.environ.get("ORG_APPROVE", "true"))
elif "can_approve_pull_request_reviews=true" in a and (
        os.environ.get("ORG_APPROVE") == "false" or os.environ.get("FAIL_WORKFLOW")):
    sys.exit(1)
"""


class SetupRepoTest(unittest.TestCase):
    def run_script(self, **env):
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d) / "gh"
            gh.write_text(FAKE_GH)
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
            log = Path(d) / "log"
            log.touch()
            p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "GH_LOG": str(log), **env})
            return p, [json.loads(line) for line in log.read_text().splitlines()]

    @staticmethod
    def ruleset_writes(calls):
        return [(c[2], c[3]) for c in calls if c[:2] == ["api", "-X"] and "/rulesets" in c[3]]

    def test_clean_run(self):
        p, calls = self.run_script(IDS_main="11")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "done\n", ""))
        self.assertEqual(self.ruleset_writes(calls), [("PUT", "repos/devstreamhq/devstream/rulesets/11"),
                                                      ("POST", "repos/devstreamhq/devstream/rulesets")])

    def test_duplicate_rulesets_update_the_oldest_and_warn(self):
        p, calls = self.run_script(IDS_main="14,11", IDS_release="20")
        self.assertEqual(p.returncode, 0)
        self.assertIn("2 rulesets are named main-protection (ids 11 14); updating 11", p.stderr)
        self.assertEqual(self.ruleset_writes(calls), [("PUT", "repos/devstreamhq/devstream/rulesets/11"),
                                                      ("PUT", "repos/devstreamhq/devstream/rulesets/20")])

    def test_org_blocking_approvals_is_a_note_not_a_failure(self):
        p, calls = self.run_script(ORG_APPROVE="false")
        self.assertEqual((p.returncode, p.stdout), (0, "done\n"))
        self.assertIn("doesn't let GitHub Actions approve pull requests", p.stderr)
        # The read-only default token is still set, without the approval part.
        self.assertEqual(calls[-1], ["api", "-X", "PUT", "repos/devstreamhq/devstream/actions/permissions/workflow",
                                     "-f", "default_workflow_permissions=read"])

    def test_other_permission_failures_still_fail(self):
        p, _ = self.run_script(FAIL_WORKFLOW="1")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("could not set the repo's Actions workflow permissions", p.stderr)


if __name__ == "__main__":
    unittest.main()
