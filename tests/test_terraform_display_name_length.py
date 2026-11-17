"""GCP refuses a display_name longer than 100 characters at apply time.

Monitoring alert policies, their conditions, service accounts and custom roles
all cap display_name at 100 characters. A plan does not check it, so a long name
passes review and fails infra-apply, and the failed apply blocks every later
change under infra/ until it is fixed.
"""

import re
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent / "infra"
LIMIT = 100
DISPLAY_NAME = re.compile(r'^\s*display_name\s*=\s*"((?:[^"\\]|\\.)*)"', re.M)


def long_display_names(text: str) -> list[str]:
    return [m.group(1) for m in DISPLAY_NAME.finditer(text) if len(m.group(1)) > LIMIT]


def test_every_display_name_fits_gcps_limit() -> None:
    offenders = {
        f"{path.name}: {name}"
        for path in sorted(INFRA.glob("*.tf"))
        for name in long_display_names(path.read_text())
    }
    assert offenders == set()


def test_the_check_finds_a_long_name() -> None:
    text = '  conditions {\n    display_name = "' + "x" * (LIMIT + 1) + '"\n  }\n'
    assert long_display_names(text) == ["x" * (LIMIT + 1)]
    assert long_display_names(text.replace("x" * (LIMIT + 1), "x" * LIMIT)) == []
