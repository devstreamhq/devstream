import base64
import time
import unittest
from pathlib import Path

from scripts.devstream.gh import GitHubError
from scripts.devstream.license_detect import CONTENT_LICENSES, FILE_RE, extra_licenses_for
from scripts.tests.fake_gh import FakeGitHub
from scripts.devstream.quality import MSG_LICENSE, MSG_NON_OSI, MSG_NOT_FOUND, MSG_NOT_FOUND_CC, Undecided, check_repo
from scripts.tests.test_quality import NOW, README, R, gh, run

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


FIXTURES = Path(__file__).parent / "fixtures" / "licenses"


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
        for text in (JSON_LIC, "PolyForm Noncommercial 1.0.0"):
            self.assertIn("isn't an open-source license", run(lic_gh({"LICENSE": text})).failures[0], text)

    def test_all_rights_reserved_with_osi_text_passes(self):
        bsd3 = (
            "Copyright (c) 2020 X. All rights reserved.\nRedistribution and use in source and binary forms, "
            "with or without modification, are permitted. Neither the name of X nor the names of contributors "
            "may be used. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS AS IS."
        )
        self.assertEqual(run(lic_gh({"LICENSE": bsd3})).failures, [])
        self.assertEqual(run(lic_gh({"LICENSE": "Copyright (c) 2020 X. All rights reserved.\n" + MIT}), "rot").rot, "ok")

    def test_canonical_texts_are_detected(self):
        files = [f for f in sorted(FIXTURES.glob("*.txt")) if not f.stem.startswith("CC")]
        self.assertEqual(len(files), 19)
        for f in files:
            with self.subTest(f.stem):
                text = "Copyright (c) 2024 Example. All rights reserved.\n\n" + f.read_text()
                self.assertEqual(run(lic_gh({"LICENSE": text})).failures, [])

    def test_postgres_copyright_file(self):
        self.passes(lic_gh({"COPYRIGHT": (FIXTURES / "PostgreSQL.txt").read_text()}))

    def test_perl_artistic_plus_gpl1_copying(self):
        gpl1 = (
            "GNU GENERAL PUBLIC LICENSE\nVersion 1, February 1989\nThe licenses for most software are "
            "designed to take away your freedom to share and change it."
        )
        self.passes(lic_gh({"Artistic": (FIXTURES / "Artistic-1.0.txt").read_text().replace("The Artistic License", 'The "Artistic License"', 1), "Copying": gpl1}))

    def test_ruby_style_layout(self):
        ruby = (
            "Ruby is copyrighted free software by Yukihiro Matsumoto <matz@netlab.jp>.\n"
            "You can redistribute it and/or modify it under either the terms of the\n"
            "2-clause BSDL (see the file BSDL), or the conditions below:\n"
        )
        self.passes(lic_gh({
            "COPYING": ruby,
            "BSDL": (FIXTURES / "BSD-2-Clause.txt").read_text(),
            "GPL": (FIXTURES / "GPL-2.0-only.txt").read_text(),
        }))

    def test_unknown_copyright_file_not_found(self):
        r = run(lic_gh({"COPYRIGHT": "Copyright 2020 X. All rights reserved."}))
        self.assertEqual(r.failures, [MSG_NOT_FOUND])

    def test_artistic_lookalikes_are_not_found(self):
        for text in (
            "Proprietary software. Not licensed under the Artistic License 2.0. The Copyright Holder reserves all rights. No redistribution.",
            "This software is proprietary. Unlike the Artistic License, you may not redistribute this Package. Standard Version only.",
            "Commercial license. See the Artistic License for details about Package use in the Standard Version.",
        ):
            self.assertEqual(run(lic_gh({"LICENSE": text})).failures, [MSG_NOT_FOUND], text)

    def test_artistic_curly_quotes(self):
        t = (FIXTURES / "Artistic-1.0.txt").read_text().replace("Reasonable copying fee", "\u201cReasonable copying fee\u201d")
        self.passes(lic_gh({"Artistic": t}))

    def test_filename_stems(self):
        for n in ("COPYRIGHT", "Artistic", "BSDL", "GPL", "COPYRIGHT.txt", "GPL-2", "GPL-3.0", "gpl-3.0.txt", "Artistic-2.0", "COPYRIGHT-2024", "LICENSE", "LICENSE.py", "COPYING.md"):
            self.assertTrue(FILE_RE.match(n), n)
        for n in ("copyright.py", "gpl.js", "Copyright.h", "copyright-header.txt", "artistic.c", "bsdl.rb"):
            self.assertFalse(FILE_RE.match(n), n)

    def test_license_file_read_before_cap(self):
        files = {n: "x" for n in ("COPYRIGHT", "Artistic", "BSDL", "GPL", "GPL-2", "COPYING-extra")}
        files["license"] = MIT
        self.passes(lic_gh(files))

    def test_weak_markers_alone_are_not_found(self):
        for text in ("Copyright 2020 X. All rights reserved.", "This software is proprietary.", "For noncommercial use only."):
            self.assertEqual(run(lic_gh({"LICENSE": text})).failures, [MSG_NOT_FOUND], text)

    def test_readme_weak_marker_is_not_found(self):
        r = run(lic_gh({}, readme=README + "\n## License\n\nProprietary.\n"))
        self.assertEqual(r.failures, [MSG_NOT_FOUND])

    def test_deep_parens_fail_closed(self):
        for depth in (499, 60):
            r = self.spdx("(" * depth + "MIT" + ")" * depth) if depth == 60 else self.spdx("(" * depth)
            self.assertIn("isn't an open-source license", r.failures[0])
        self.assertEqual(self.spdx("(" * 40 + "MIT" + ")" * 40).failures, [])

    def test_spdx_table_pipes(self):
        g = lic_gh({"LICENSE": "| SPDX-License-Identifier: | BSD-3-Clause WITH PCRE2-exception |"})
        self.assertEqual(run(g).failures, [])

    def test_readme_formerly(self):
        r = run(lic_gh({}, readme=README + "\n## License\n\nFormerly MIT, now relicensed.\n"))
        self.assertEqual(r.failures, [MSG_NOT_FOUND])

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

    def oversize(self, text):
        g = lic_gh({"LICENSE": MIT})
        g.routes[f"GET {R}/contents/LICENSE"] = {"type": "file", "encoding": "none", "size": len(text), "sha": "big"}
        g.routes[f"GET {R}/git/blobs/big"] = {"encoding": "base64", "sha": "big",
                                              "content": base64.encodebytes(text.encode()).decode()}
        return g

    def test_oversize_file_is_read_through_the_blob(self):
        busl = "Business Source License 1.1\n\n" + "Licensed Work terms. " * 100_000
        self.assertGreater(len(busl), 2_000_000)
        g = self.oversize(busl)
        self.assertEqual(run(g).failures, [MSG_NON_OSI.format("Business Source License")])
        self.assertEqual(run(g, "rot").rot, "remove")
        self.assertIn(("GET", f"{R}/git/blobs/big", None), g.calls)

    def test_oversize_file_is_capped_at_the_scan_limit(self):
        # Only the first 100 KB is scanned, the same as a file under 1 MB.
        self.assertEqual(run(self.oversize("x" * 150_000 + MIT)).failures, [MSG_NOT_FOUND])
        self.assertEqual(run(self.oversize(MIT + "\n" + "x" * 2_000_000)).failures, [])

    def test_oversize_file_without_a_blob_sha_is_undecided(self):
        g = lic_gh({"LICENSE": MIT})
        g.routes[f"GET {R}/contents/LICENSE"] = {"type": "file", "encoding": "none", "size": 2_000_000}
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

        for spdx in [p[0] for p in ld.PHRASES] + list(ld.NAMES.values()):
            self.assertIn(spdx, ld.OSI | ld.CONTENT_LICENSES)


if __name__ == "__main__":
    unittest.main()


def run_cc(g, mode="submit", allowed=CONTENT_LICENSES):
    return check_repo("alice/tool", g, NOW, mode, allowed_extra=allowed)


def cc_spdx(i):
    return {"license": {"spdx_id": i}}


class ContentLicenseTest(unittest.TestCase):
    TEXTS = ["CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0"]

    def text(self, name):
        return (FIXTURES / f"{name}.txt").read_text()

    def test_helper(self):
        self.assertEqual(CONTENT_LICENSES, {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0"})
        self.assertEqual(extra_licenses_for("learning-resources"), CONTENT_LICENSES)
        self.assertEqual(extra_licenses_for("developer-tools"), frozenset())

    def test_canonical_texts_detected_when_allowed(self):
        for name in self.TEXTS:
            with self.subTest(name):
                self.assertEqual(run_cc(lic_gh({"LICENSE": self.text(name)})).failures, [])

    def test_canonical_texts_not_detected_when_not_allowed(self):
        for name in self.TEXTS:
            with self.subTest(name):
                got = run_cc(lic_gh({"LICENSE": self.text(name)}), allowed=frozenset()).failures
                self.assertEqual(got, [MSG_NOT_FOUND_CC])

    def test_hint_only_for_cc(self):
        self.assertIn("Learning & Resources only", MSG_NOT_FOUND_CC)
        self.assertNotIn("Learning & Resources", MSG_NOT_FOUND)
        self.assertEqual(run_cc(lic_gh({"LICENSE": "all rights reserved"}), allowed=frozenset()).failures, [MSG_NOT_FOUND])

    def test_noncommercial_text_never_accepted(self):
        text = self.text("CC-BY-NC-4.0")
        for allowed in (CONTENT_LICENSES, frozenset()):
            with self.subTest(allowed=bool(allowed)):
                self.assertEqual(run_cc(lic_gh({"LICENSE": text}), allowed=allowed).failures, [MSG_NOT_FOUND])

    def test_spdx_id_from_github(self):
        self.assertEqual(run_cc(gh(cc_spdx("CC-BY-4.0"))).failures, [])
        self.assertEqual(run_cc(gh(cc_spdx("CC-BY-4.0")), allowed=frozenset()).failures, [MSG_LICENSE])
        for bad in ("CC-BY-NC-4.0", "CC-BY-ND-4.0", "CC-BY-NC-SA-4.0"):
            with self.subTest(bad):
                self.assertEqual(run_cc(gh(cc_spdx(bad))).failures, [MSG_LICENSE])

    def test_license_endpoint_fallback(self):
        g = gh(cc_spdx("NOASSERTION"), extra={f"GET {R}/license": {"license": {"spdx_id": "CC0-1.0"}}})
        self.assertEqual(run_cc(g).failures, [])
        g = gh(cc_spdx("NOASSERTION"), extra={f"GET {R}/license": {"license": {"spdx_id": "CC0-1.0"}}})
        self.assertEqual(run_cc(g, allowed=frozenset()).failures, [MSG_LICENSE])

    def test_spdx_expressions(self):
        ok = lic_gh({"LICENSE": "SPDX-License-Identifier: CC-BY-4.0 AND MIT"})
        self.assertEqual(run_cc(ok).failures, [])
        ok = lic_gh({"LICENSE": "SPDX-License-Identifier: CC-BY-4.0 AND MIT"})
        self.assertEqual(run_cc(ok, allowed=frozenset()).failures, [MSG_NON_OSI.format("CC-BY-4.0")])
        for bad in ("CC-BY-NC-4.0", "CC-BY-ND-4.0", "CC-BY-NC-SA-4.0", "CC-BY-4.0 AND CC-BY-NC-4.0"):
            with self.subTest(bad):
                g = lic_gh({"LICENSE": f"SPDX-License-Identifier: {bad}"})
                self.assertEqual(run_cc(g).failures, [MSG_NON_OSI.format(bad.split()[-1])])

    def readme(self, name):
        return README + f"\n\n## License\n\n{name}\n"

    def test_readme_names(self):
        for name in ("CC0", "CC BY 4.0", "CC-BY-4.0", "Creative Commons Attribution 4.0", "CC BY-SA 4.0"):
            with self.subTest(name):
                self.assertEqual(run_cc(gh(cc_spdx("NONE"), readme=self.readme(name))).failures, [MSG_LICENSE])
                g = lic_gh({}, readme=self.readme(name))
                self.assertEqual(run_cc(g).failures, [])
                g = lic_gh({}, readme=self.readme(name))
                self.assertEqual(run_cc(g, allowed=frozenset()).failures, [MSG_NOT_FOUND_CC])

    def test_readme_nc_nd_phrasings_not_found(self):
        for name in ("CC BY 4.0 NC", "Creative Commons Attribution 4.0 NonCommercial",
                     "CC BY 4.0 / CC BY-NC 4.0", "CC BY 4.0 ND", "CC BY 4.0 Non-Commercial"):
            with self.subTest(name):
                self.assertEqual(run_cc(lic_gh({}, readme=self.readme(name))).failures, [MSG_NOT_FOUND])

    def test_readme_mit_and_cc0_still_pass(self):
        self.assertEqual(run_cc(lic_gh({}, readme=self.readme("MIT"))).failures, [])
        self.assertEqual(run_cc(lic_gh({}, readme=self.readme("CC0"))).failures, [])

    def test_cc_hint_is_submit_only(self):
        g = lic_gh({"LICENSE": self.text("CC0-1.0")})
        r = run_cc(g, mode="rot", allowed=frozenset())
        self.assertIn(MSG_NOT_FOUND, r.failures)
        self.assertNotIn(MSG_NOT_FOUND_CC, r.failures)

    def test_early_rejection_survives_a_later_oversized_file(self):
        g = lic_gh({"LICENSE": SSPL.replace("Server Side Public License", "Business Source License"), "LICENSE.md": ""})
        g.routes[f"GET {R}/contents/LICENSE.md"] = {"type": "file", "encoding": "none"}
        self.assertEqual(run(g).failures, [MSG_NON_OSI.format("Business Source License")])
        self.assertEqual(run(g, "rot").rot, "remove")

    def test_readme_noncommercial_names_rejected(self):
        for name in ("CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-NC-SA 4.0", "Creative Commons Attribution-NonCommercial 4.0"):
            with self.subTest(name):
                self.assertEqual(run_cc(lic_gh({}, readme=self.readme(name))).failures, [MSG_NOT_FOUND])
