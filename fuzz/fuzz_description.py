"""Fuzz the description filter, and the SPDX scanner on the same text.

clean_description never raises; it returns (text, None) or (None, reason).
A result it accepts must also pass it again unchanged, because
validate_data re-checks every stored description that way.
"""

import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with atheris.instrument_imports(include=["scripts"]):
    from scripts.devstream import license_detect
    from scripts.devstream.description import FORBIDDEN, MAX_LEN, MIN_LEN, clean_description


def TestOneInput(data: bytes) -> None:
    text = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(len(data))
    final, why = clean_description(text)
    assert (final is None) != (why is None), (final, why)
    if final is not None:
        assert MIN_LEN <= len(final) <= MAX_LEN, final
        assert not FORBIDDEN & set(final), final
        assert clean_description(final) == (final, None), final
    license_detect._spdx_scan("SPDX-License-Identifier: " + text)
    license_detect._readme_license("## License\n\n" + text)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
