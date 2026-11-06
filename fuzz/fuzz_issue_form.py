"""Fuzz the submission and report parsers, and the SPDX scanner.

FormError is the parsers' way of saying no, so it is expected. Any other
exception is a crash: the bot would die on a hostile issue body.
"""

import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with atheris.instrument_imports(include=["scripts"]):
    from scripts.devstream import license_detect
    from scripts.devstream.entries import CATEGORIES
    from scripts.devstream.issue_form import REASONS, FormError, parse_report, parse_submission

HEADINGS = [
    "Repository", "Category", "Description", "Checklist",
    "Entry", "Reason", "Replaced by (owner/name)", "Details",
]
# Real answers let the fuzzer get past the "incomplete form" checks.
ANSWERS = [
    *CATEGORIES.values(),
    *REASONS,
    "- [x] I read the quality bar\n- [x] This repo is open source",
    "https://github.com/octo/hello",
    "_No response_",
]


def body(fdp: atheris.FuzzedDataProvider) -> str:
    if fdp.ConsumeBool():
        return fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    parts = []
    for _ in range(fdp.ConsumeIntInRange(0, 8)):
        answer = fdp.PickValueInList(ANSWERS) if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(200)
        parts.append(f"### {fdp.PickValueInList(HEADINGS)}\n\n{answer}")
    return "\n\n".join(parts)


def TestOneInput(data: bytes) -> None:
    text = body(atheris.FuzzedDataProvider(data))
    for parse in (parse_submission, parse_report):
        try:
            parse(text)
        except FormError:
            pass
    license_detect._detect_text(text)
    license_detect._spdx_scan("SPDX-License-Identifier: " + text)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
