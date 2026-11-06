import base64
import time
import unittest

from scripts.devstream.gh import GitHubError
from scripts.tests.fake_gh import FakeGitHub
from scripts.devstream.quality import Undecided
from scripts.tests.test_quality import NOW, README, R, gh, run

MSG_NOT_FOUND = (
    "We couldn't find an open-source license. If this is your repo, add a standard LICENSE file "
    "(choosealicense.com has them) so GitHub can detect it, then submit again."
)
MIT = (
    "Permission is hereby granted, free of charge, to any person obtaining a copy. "
    "The above copyright notice and this permission notice shall be included in all copies."
)
JSON_LIC = MIT + " The Software shall be used for Good, not Evil."
CURL = (
    "COPYRIGHT AND PERMISSION NOTICE\n\nPermission to use, copy, modify, and distribute this\n"
    "software for any purpose with or without fee is hereby granted. "
    "Except as contained in this notice, the name of a copyright holder shall not be used in advertising."
)
APACHE = "Apache License\n   Version 2.0, January 2004\n TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION"
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
        self.assertIn("isn't an open-source license", run(g).failures[0])

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

    def spdx(self, expr):
        return run(lic_gh({"LICENSE": "SPDX-License-Identifier: " + expr}))

    def test_spdx_precedence_and_parens(self):
        bad = self.spdx("(MIT OR Apache-2.0) AND SSPL-1.0")
        self.assertIn("SSPL-1.0", bad.failures[0])
        self.assertEqual(self.spdx("(MIT AND SSPL-1.0) OR Apache-2.0").failures, [])
        self.assertEqual(self.spdx("MIT AND (Apache-2.0 OR GPL-2.0-only)").failures, [])
        self.assertEqual(self.spdx("MIT OR SSPL-1.0 AND Foo").failures, [])
        for junk in ("(MIT OR Apache-2.0", "MIT)", "MIT AND", "OR MIT", "MIT Apache-2.0"):
            self.assertIn("isn't an open-source license", self.spdx(junk).failures[0], junk)

    def test_spdx_hostile_is_fast(self):
        for text in ("SPDX-License-Identifier: MIT" + " " * 100_000 + "x", " " * 100_000 + "MIT" + " " * 100_000):
            t = time.monotonic()
            run(lic_gh({"LICENSE": text}))
            self.assertLess(time.monotonic() - t, 1)

    def test_readme_hostile_is_fast(self):
        t = time.monotonic()
        run(lic_gh({}, readme="## License\n" * 55_000))
        self.assertLess(time.monotonic() - t, 1)

    def test_loose_phrases_rejected(self):
        for text in (JSON_LIC, "This is non-commercial only. " + MIT, "PolyForm Noncommercial 1.0.0", "Copyright 2020 X. All rights reserved.", "This software is proprietary."):
            self.assertIn("isn't an open-source license", run(lic_gh({"LICENSE": text})).failures[0], text)

    def test_all_rights_reserved_with_osi_text_passes(self):
        bsd3 = (
            "Copyright (c) 2020 X. All rights reserved.\nRedistribution and use in source and binary forms, "
            "with or without modification, are permitted. Neither the name of X nor the names of contributors "
            "may be used. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS AS IS."
        )
        self.assertEqual(run(lic_gh({"LICENSE": bsd3})).failures, [])
        self.assertEqual(run(lic_gh({"LICENSE": "Copyright (c) 2020 X. All rights reserved.\n" + MIT}), "rot").rot, "ok")

    def test_opener_alone_is_not_mit(self):
        g = lic_gh({"LICENSE": "Permission is hereby granted, free of charge, to anyone."})
        self.assertEqual(run(g).failures, [MSG_NOT_FOUND])

    def test_readme_negation_and_fair_source(self):
        for body in ("not MIT", "MIT is no longer used here", "Fair Source"):
            r = run(lic_gh({}, readme=README + "\n## License\n\n" + body + "\n"))
            self.assertTrue(r.failures and r.failures[0] != "", body)
            self.assertNotEqual(r.failures, [], body)

    def test_readme_bare_id_beyond_two_lines_not_used(self):
        r = run(lic_gh({}, readme=README + "\n## License\n\nSee below.\n\nMore words.\n\nMIT\n"))
        self.assertEqual(r.failures, [MSG_NOT_FOUND])

    def test_readme_bare_osi_id_not_in_names_not_used(self):
        r = run(lic_gh({}, readme=README + "\n## License\n\nArtistic-2.0\n"))
        self.assertEqual(r.failures, [MSG_NOT_FOUND])

    def test_mit_plus_licenseref_in_other_file_rejected(self):
        r = run(lic_gh({"LICENSE-MIT": MIT, "LICENSE-OTHER": "SPDX-License-Identifier: LicenseRef-Custom"}))
        self.assertIn("isn't an open-source license", r.failures[0])

    def test_one_line_mit(self):
        for t in ("MIT", " MIT License \n"):
            self.assertEqual(run(lic_gh({"LICENSE": t})).failures, [], t)

    def test_symlink_counts_as_present_and_is_not_read(self):
        g = lic_gh({}, readme=README + "\n## License\n\nMIT License\n")
        g.routes[f"GET {R}/contents/"] = [{"name": "LICENSE", "type": "symlink"}]
        self.assertEqual(run(g).failures, [MSG_NOT_FOUND])
        self.assertFalse([c for c in g.calls if c[1] == f"{R}/contents/LICENSE"])

    def test_oversize_file_is_undecided(self):
        g = lic_gh({"LICENSE": MIT})
        g.routes[f"GET {R}/contents/LICENSE"] = {"encoding": "none", "size": 2_000_000}
        with self.assertRaises(Undecided):
            run(g)

    def test_5xx_on_file_read_raises(self):
        def boom(_):
            raise GitHubError(502, "bad gateway")

        g = lic_gh({"LICENSE": MIT})
        g.routes[f"GET {R}/contents/LICENSE"] = boom
        with self.assertRaises(GitHubError):
            run(g)

    def test_listing_once(self):
        g = lic_gh({f"LICENSE-{i}": "x" for i in range(8)})
        run(g)
        self.assertEqual(len([c for c in g.calls if c[1] == f"{R}/contents/"]), 1)

    def test_rot_non_osi_file_removes(self):
        self.assertEqual(run(lic_gh({"LICENSE": SSPL}), "rot").rot, "remove")

    def test_phrase_table_ids_are_osi(self):
        from scripts.devstream import license_detect as ld

        for spdx, _, _ in ld.PHRASES:
            self.assertIn(spdx, ld.OSI)
        for spdx in ld.NAMES.values():
            self.assertIn(spdx, ld.OSI)


if __name__ == "__main__":
    unittest.main()
