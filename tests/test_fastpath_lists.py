"""scripts/deploy/fastpath_lists.sh holds the fast path's workspace lists for
every deploy that reads them (docs/design/fast-admission-widening.md section
5.7): each script that sources it renders each list from its variable, and
never writes a list of its own."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from trusted_router.config import Settings

ROOT = Path(__file__).resolve().parents[1]
LISTS = ROOT / "scripts" / "deploy" / "fastpath_lists.sh"
# Every script that sources the lists. The enabler job's script
# (fastpath_enabler_job.sh) joins when widening PR 6 adds it.
SOURCING = (ROOT / "scripts" / "deploy" / "rollout.sh",)
# The variables and the Settings field each one sets.
FIELDS = {"TR_FASTPATH_PILOT_EXCLUDE": "fastpath_pilot_exclude"}
ASSIGNMENT = re.compile(r'^([A-Z][A-Z0-9_]*)="([^"$`\\]*)"$')


def _assignments() -> dict[str, str]:
    """The file's variables: nothing but comments, blank lines and literal
    double-quoted assignments, so sourcing it runs nothing."""
    values: dict[str, str] = {}
    for line in LISTS.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        assert match, f"fastpath_lists.sh may hold only literal assignments: {line!r}"
        assert match.group(1) not in values, f"{match.group(1)} assigned twice"
        values[match.group(1)] = match.group(2)
    return values


def test_the_lists_file_holds_exactly_the_lists() -> None:
    assert set(_assignments()) == set(FIELDS)


def test_sourcing_the_file_in_bash_sets_the_same_values() -> None:
    names = sorted(FIELDS)
    script = f'set -euo pipefail; source "{LISTS}"; ' + "; ".join(f'printf "%s\\n" "${{{n}}}"' for n in names)
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout  # noqa: S603,S607
    assert out.splitlines() == [_assignments()[n] for n in names]


def test_each_list_is_one_the_settings_accept() -> None:
    for name, value in _assignments().items():
        Settings(_env_file=None, environment="test", **{FIELDS[name]: value})  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize("script", SOURCING, ids=lambda path: path.name)
def test_every_script_sources_the_lists_and_renders_each_from_its_variable(script: Path) -> None:
    text = script.read_text()
    assert text.count('source "${SCRIPT_DIR}/fastpath_lists.sh"') == 1
    assert "# shellcheck source=scripts/deploy/fastpath_lists.sh" in text
    for name in FIELDS:
        # Rendered exactly once, from the variable: never a literal list.
        rendered = re.findall(rf'"{name}=([^"]*)"', text)
        assert rendered == [f"${{{name}}}"], (script.name, name, rendered)
        # And never assigned here, so the file is the only source.
        assert not re.search(rf"^\s*{name}=", text, flags=re.MULTILINE), (script.name, name)
    # Sourced before the environment array that renders it.
    assert text.index('source "${SCRIPT_DIR}/fastpath_lists.sh"') < text.index("ENV_VARS=(")


def test_rollout_renders_all_mode_as_a_literal_false() -> None:
    """TR_FASTPATH_PILOT_ALL is a literal in rollout.sh, false until WS2
    (section 8.2), so no revision inherits it."""
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text()
    assert re.findall(r'"TR_FASTPATH_PILOT_ALL=[^"]*"', rollout) == ['"TR_FASTPATH_PILOT_ALL=false"']
