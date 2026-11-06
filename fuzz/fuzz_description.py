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
    from scripts.devstream.description import EMOJI_ZWJ, FORBIDDEN, IGNORABLE, MAX_LEN, MIN_LEN, VARIATION, clean_description


# Invisible characters people use to slip a blocked word past the filter.
SNEAKY = [
    "\u00ad", "\u034f", "\u061c", "\u115f", "\u1160", "\u17b4", "\u180b", "\u180e", "\u200b",
    "\u200d", "\u202e", "\u2060", "\u2062", "\u3164", "\ufe08", "\ufe0f", "\ufeff", "\uffa0",
    "\ufff0", "\U0001bca0", "\U0001d173", "\U000e0001", "\U000e0100", "\u2728",
]


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        text = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    else:
        text = "".join(
            fdp.PickValueInList(SNEAKY) if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(8)
            for _ in range(fdp.ConsumeIntInRange(0, 40))
        )
    final, why = clean_description(text)
    assert (final is None) != (why is None), (final, why)
    if final is not None:
        assert MIN_LEN <= len(final) <= MAX_LEN, final
        assert not FORBIDDEN & set(final), final
        # The one invisible character kept is a joiner between two emoji, which builds a single emoji.
        assert not IGNORABLE.search(EMOJI_ZWJ.sub("", final)) and not VARIATION.search(final), final
        assert clean_description(final) == (final, None), final
    license_detect._spdx_scan("SPDX-License-Identifier: " + text)
    license_detect._readme_license("## License\n\n" + text)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
