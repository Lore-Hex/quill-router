"""The settle log's hedge as the node is started with it (#2671): the
startup script passes -settle-hedge from the instance attribute
fastpath-settle-hedge alone, absent by default, and refuses a value the
release would not take before it writes the unit."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_fastpath_p1 import _command_flags, _source_at

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "fastpath_node_startup.sh.tftpl"

# The pinned-source check may fetch into the same shallow checkout.
pytestmark = pytest.mark.xdist_group("fastpath-pinned-source")


def _run_hedge_block(value: str | None) -> subprocess.CompletedProcess[str]:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('settle_hedge="$(attribute fastpath-settle-hedge)"')
    block = script[start:script.index("\nfi\n", start) + len("\nfi\n")]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = 'attribute() { printf "%s" "${A_SETTLE_HEDGE:-}"; }\nsystemd-cat() { cat >&2; }\n'
    env = {"PATH": "/usr/bin:/bin", **({"A_SETTLE_HEDGE": value} if value is not None else {})}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$hedge_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def test_absent_the_unit_passes_no_hedge_flag() -> None:
    for value in (None, ""):
        ran = _run_hedge_block(value)
        assert ran.returncode == 0 and ran.stdout == "", (value, ran.stderr)


def test_zero_or_milliseconds_pass_as_the_flag() -> None:
    for value in ("0", "1ms", "250ms", "9999ms"):
        ran = _run_hedge_block(value)
        assert ran.returncode == 0, (value, ran.stderr)
        assert ran.stdout.split() == ["-settle-hedge", value]


def test_a_value_the_release_would_refuse_stops_the_script() -> None:
    for value in ("250", "1s", "-1ms", "0ms", "00", "10000ms", "250ms -roles auditor", " 250ms", "250ms\n-roles x",
                  "x\n250ms", "off"):
        ran = _run_hedge_block(value)
        assert ran.returncode == 1, (value, ran.stdout)
        assert "fastpath-settle-hedge is 0, 1ms to 9999ms, or absent" in ran.stderr, (value, ran.stderr)


def test_the_flag_waits_for_the_attribute_and_a_release_that_takes_it() -> None:
    """The unit passes the hedge flag only from instance metadata, after every
    other flag; HEAD's command takes it, and once Terraform sets the
    attribute the pinned release must take it too: a flag it does not take
    stops the node before either role starts."""
    script = SCRIPT.read_text(encoding="utf-8")
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$", script, re.M)
    assert unit_line and unit_line.group(0).split()[-1] == "$hedge_flags", unit_line
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert "settle-hedge" in head
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    local = re.findall(r'^\s*fastpath_settle_hedge\s*=\s*"([^"]*)"$', tf, re.M)
    assert len(local) == 1, local
    assert re.search(r"local\.fastpath_settle_hedge_metadata[,)]", tf)
    if local[0]:
        assert re.fullmatch(r"0|[1-9][0-9]{0,3}ms", local[0]), local[0]
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert "settle-hedge" in taken


def test_the_runbook_names_the_off_switch() -> None:
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = deploy[deploy.index("## The settle log's hedge"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    for needed in ('fastpath_settle_hedge = "0"', "sudo google_metadata_script_runner startup",
                   "-settle-hedge 0", "publish_calls", "hedges_withheld", "Taking a pinned release"):
        assert needed in section, needed
