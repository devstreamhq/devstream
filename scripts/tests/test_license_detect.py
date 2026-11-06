import base64
import unittest

from scripts.devstream.gh import GitHubError
from scripts.tests.fake_gh import FakeGitHub
from scripts.tests.test_quality import NOW, README, R, gh, run

MSG_NOT_FOUND = (
    "We couldn't find an open-source license. If this is your repo, add a standard LICENSE file "
    "(choosealicense.com has them) so GitHub can detect it, then submit again."
)
MIT = "Permission is hereby granted, free of charge, to any person obtaining a copy"
CURL = (
    "COPYRIGHT AND PERMISSION NOTICE\n\nPermission to use, copy, modify, and distribute this\n"
    "software for any purpose with or without fee is hereby granted."
)
APACHE = "Apache License\n   Version 2.0, January 2004"
SSPL = "Server Side Public License\nVERSION 1, OCTOBER 16, 2018"
NOASSERT = {"license": {"spdx_id": "NOASSERTION"}}


def file(content):
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(content.encode()).decode()}


def lic_gh(files, readme=README, repo_kw=NOASSERT):
    extra = {f"GET {R}/contents/": [{"name": n, "type": "file"} for n in files]}
    extra.update({f"GET {R}/contents/{n}": file(c) for n, c in files.items()})
    return gh(repo_kw, readme=readme, extra=extra)


class LicenseSearchTest(unittest.TestCase):
    def passes(self, g, mode="submit"):
        self.assertEqual(run(g, mode).failures, [])

    def test_curl_style(self):
        self.passes(lic_gh({"COPYING": CURL}))

    def test_null_spdx_also_searches(self):
        self.passes(lic_gh({"LICENSE": MIT}, repo_kw={"license": None}))

    def test_dual_files(self):
        self.passes(lic_gh({"LICENSE-MIT": MIT, "LICENSE-APACHE": APACHE}))

    def test_spdx_with_exception(self):
        self.passes(lic_gh({"LICENSE": "SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception"}))

    def test_spdx_or(self):
        self.passes(lic_gh({"LICENSE": "// SPDX-License-Identifier: GPL-2.0-or-later OR MIT"}))

    def test_spdx_and_needs_all(self):
        g = lic_gh({"LICENSE": "SPDX-License-Identifier: MIT AND Proprietary"})
        self.assertEqual(run(g).failures, [MSG_NOT_FOUND])

    def test_sspl_rejected_by_name(self):
        r = run(lic_gh({"LICENSE": SSPL + MIT}))
        self.assertEqual(
            r.failures,
            ["This repo's license (Server Side Public License) isn't an open-source license, so it can't be listed."],
        )

    def test_readme_section_without_file(self):
        self.passes(lic_gh({}, readme=README + "\n## License\n\nMIT License\n\n## Credits\n"))

    def test_readme_mit_but_sspl_file(self):
        r = run(lic_gh({"LICENSE": SSPL}, readme=README + "\n## License\n\nMIT\n"))
        self.assertIn("Server Side Public License", r.failures[0])

    def test_unknown_custom_license(self):
        self.assertEqual(run(lic_gh({"LICENSE": "Do what you like, mostly."})).failures, [MSG_NOT_FOUND])

    def test_file_present_but_undetected_ignores_readme(self):
        g = lic_gh({"LICENSE": "custom words"}, readme=README + "\n# License\n\nMIT\n")
        self.assertEqual(run(g).failures, [MSG_NOT_FOUND])

    def test_readme_non_osi_section(self):
        r = run(lic_gh({}, readme=README + "\n### Licence\n\nCommons Clause + MIT\n"))
        self.assertIn("Commons Clause", r.failures[0])

    def test_nothing_anywhere(self):
        self.assertEqual(run(gh(NOASSERT)).failures, [MSG_NOT_FOUND])

    def test_api_500_on_listing_raises(self):
        def boom(_):
            raise GitHubError(500, "boom")

        g = gh(NOASSERT, extra={f"GET {R}/contents/": boom})
        for mode in ("submit", "rot"):
            with self.assertRaises(GitHubError):
                run(g, mode)

    def test_at_most_five_files_read(self):
        g = lic_gh({f"LICENSE-{i}": "x" for i in range(8)})
        run(g)
        reads = [c for c in g.calls if c[1].startswith(f"{R}/contents/LICENSE")]
        self.assertEqual(len(reads), 5)

    def test_rot_found_is_ok_and_not_found_removes(self):
        self.assertEqual(run(lic_gh({"LICENSE": MIT}), "rot").rot, "ok")
        self.assertEqual(run(gh(NOASSERT), "rot").rot, "remove")

    def test_phrase_table_ids_are_osi(self):
        from scripts.devstream import license_detect as ld

        for spdx, _, _ in ld.PHRASES:
            self.assertIn(spdx, ld.OSI)
        for spdx in ld.NAMES.values():
            self.assertIn(spdx, ld.OSI)


if __name__ == "__main__":
    unittest.main()
