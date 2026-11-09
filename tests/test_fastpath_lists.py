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
ROLLOUT = ROOT / "scripts" / "deploy" / "rollout.sh"
JOB = ROOT / "scripts" / "deploy" / "fastpath_enabler_job.sh"
# The variables and the Settings field each one sets.
FIELDS = {
    "TR_FASTPATH_PILOT_WORKSPACES": "fastpath_pilot_workspaces",
    "TR_FASTPATH_PILOT_EXCLUDE": "fastpath_pilot_exclude",
    "TR_FASTPATH_ENABLER_INCLUDE": "fastpath_enabler_include",
}
# Every script that sources the lists, and the variables each renders.
SOURCING = {
    ROLLOUT: ("TR_FASTPATH_PILOT_WORKSPACES", "TR_FASTPATH_PILOT_EXCLUDE"),
    JOB: ("TR_FASTPATH_PILOT_EXCLUDE", "TR_FASTPATH_ENABLER_INCLUDE"),
}
ASSIGNMENT = re.compile(r'^([A-Z][A-Z0-9_]*)="([^"$`\\]*)"$')
# A reference to one variable assigned above it, whole.
REFERENCE = re.compile(r'^([A-Z][A-Z0-9_]*)="\$\{([A-Z][A-Z0-9_]*)\}"$')


def _assignments() -> dict[str, str]:
    """The file's variables, resolved: nothing but comments, blank lines,
    literal double-quoted assignments and whole references to a variable
    assigned above, so sourcing it runs nothing."""
    values: dict[str, str] = {}
    for line in LISTS.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        reference = REFERENCE.fullmatch(line)
        assert match or reference, f"fastpath_lists.sh may hold only literal assignments or references: {line!r}"
        name = (match or reference).group(1)  # type: ignore[union-attr]
        assert name not in values, f"{name} assigned twice"
        if match:
            values[name] = match.group(2)
        else:
            assert reference is not None
            assert reference.group(2) in values, f"{name} refers to {reference.group(2)}, not assigned above it"
            values[name] = values[reference.group(2)]
    return values


def resolved_lists() -> dict[str, str]:
    """The lists' values as a sourcing script sees them."""
    return _assignments()


def test_the_lists_file_holds_exactly_the_lists() -> None:
    assert set(_assignments()) == set(FIELDS)


def test_sourcing_the_file_in_bash_sets_the_same_values() -> None:
    names = sorted(FIELDS)
    script = f'set -euo pipefail; source "{LISTS}"; ' + "; ".join(f'printf "%s\\n" "${{{n}}}"' for n in names)
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout  # noqa: S603,S607
    assert out.splitlines() == [_assignments()[n] for n in names]


def test_all_mode_empties_the_pilot_set_and_the_include_list_keeps_its_own() -> None:
    """WS2 (section 8.2): the explicit set is empty, as the settings require
    in all mode; the include list is its own literal, so emptying the set
    did not empty it; the exclude list names each probe's workspace (section
    4(c)); and no workspace is on both lists, an overlap the settings accept
    and the enabler acts on, disabling the workspace's row as excluded."""
    values = _assignments()
    assert values["TR_FASTPATH_PILOT_WORKSPACES"] == ""
    assert 'TR_FASTPATH_PILOT_WORKSPACES=""' in LISTS.read_text().splitlines()
    include = values["TR_FASTPATH_ENABLER_INCLUDE"].split(",")
    exclude = values["TR_FASTPATH_PILOT_EXCLUDE"].split(",")
    assert len(include) == len(set(include)) == 3 and all(include)
    assert len(exclude) == len(set(exclude)) and all(exclude)
    # Each probe's workspace by name: one dropped is one all mode covers.
    assert set(exclude) == {
        "91d7810e-93b2-4c37-b1bd-ba9227585416",  # the Stage D probe's
        "d385c399-b245-4147-a528-0a4f6f170c71",  # the synthetic monitor's
        "162e441a-5d13-414f-92bd-7c6cd805faa3",  # the async settle pilot probe's
    }
    assert not set(include) & set(exclude)
    # A literal of its own: no reference left to a list WS2 emptied.
    assert not any(REFERENCE.fullmatch(line) for line in LISTS.read_text().splitlines())


def test_each_list_is_one_the_settings_accept() -> None:
    for name, value in _assignments().items():
        Settings(_env_file=None, environment="test", **{FIELDS[name]: value})  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize("script", sorted(SOURCING), ids=lambda path: path.name)
def test_every_script_sources_the_lists_and_renders_each_from_its_variable(script: Path) -> None:
    text = script.read_text()
    assert text.count('source "${SCRIPT_DIR}/fastpath_lists.sh"') == 1
    assert "# shellcheck source=scripts/deploy/fastpath_lists.sh" in text
    for name in FIELDS:
        if name not in SOURCING[script]:
            # A list the script does not read: neither rendered nor assigned.
            assert f'"{name}=' not in text and not re.search(rf"^\s*{name}=", text, flags=re.MULTILINE), name
            continue
        # Rendered exactly once, from the variable: never a literal list.
        rendered = re.findall(rf'"{name}=([^"]*)"', text)
        assert rendered == [f"${{{name}}}"], (script.name, name, rendered)
        # And never assigned here, so the file is the only source.
        assert not re.search(rf"^\s*{name}=", text, flags=re.MULTILINE), (script.name, name)
    # Sourced before the environment array that renders it.
    array = "ENV_VARS=(" if script == ROLLOUT else "env_vars=("
    assert text.index('source "${SCRIPT_DIR}/fastpath_lists.sh"') < text.index(array)


def test_rollout_renders_all_mode_as_a_literal_true() -> None:
    """TR_FASTPATH_PILOT_ALL is a literal in rollout.sh, true since WS2
    (section 8.2), so no revision inherits it; and the deployed lists are
    ones the settings start with in all mode."""
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text()
    assert re.findall(r'"TR_FASTPATH_PILOT_ALL=[^"]*"', rollout) == ['"TR_FASTPATH_PILOT_ALL=true"']
    values = _assignments()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, environment="test", storage_backend="spanner-clickhouse", fastpath_pilot_all=True,
        fastpath_pilot_workspaces=values["TR_FASTPATH_PILOT_WORKSPACES"],
        fastpath_pilot_exclude=values["TR_FASTPATH_PILOT_EXCLUDE"],
    )
    exclude = set(values["TR_FASTPATH_PILOT_EXCLUDE"].split(","))
    assert not any(settings.fastpath_covers(ws) for ws in exclude)
    assert all(settings.fastpath_covers(ws) for ws in values["TR_FASTPATH_ENABLER_INCLUDE"].split(","))
