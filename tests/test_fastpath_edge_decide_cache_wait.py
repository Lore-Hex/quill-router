"""The decide path's read-through as the node is started with it: the
startup script passes -edge-decide-cache-wait from the instance attribute
fastpath-edge-decide-cache-wait alone, absent by default, only beside the
decide path, and refuses a value the release would not take before it writes
the unit."""

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


def _run_wait_block(value: str | None, decide: str = "true") -> subprocess.CompletedProcess[str]:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('decide_cache_wait="$(attribute fastpath-edge-decide-cache-wait)"')
    block = script[start:script.index("\nfi\n", start) + len("\nfi\n")]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = (
        'attribute() { printf "%s" "${A_CACHE_WAIT:-}"; }\nsystemd-cat() { cat >&2; }\n'
        f'decide="{decide}"\n'
    )
    env = {"PATH": "/usr/bin:/bin", **({"A_CACHE_WAIT": value} if value is not None else {})}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$cache_wait_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def test_absent_the_unit_passes_no_wait_flag() -> None:
    for value in (None, ""):
        for decide in ("true", "false", ""):
            ran = _run_wait_block(value, decide)
            assert ran.returncode == 0 and ran.stdout == "", (value, decide, ran.stderr)


def test_zero_or_milliseconds_up_to_the_bound_pass_as_the_flag() -> None:
    for value in ("0", "1ms", "9ms", "10ms", "99ms", "100ms", "199ms", "200ms", "249ms", "250ms"):
        ran = _run_wait_block(value)
        assert ran.returncode == 0, (value, ran.stderr)
        assert ran.stdout.split() == ["-edge-decide-cache-wait", value]


def test_a_value_the_release_would_refuse_stops_the_script() -> None:
    for value in ("100", "1s", "-1ms", "0ms", "00", "01ms", "251ms", "260ms", "300ms", "1000ms", "100ms -roles auditor",
                  " 100ms", "100ms\n-roles x", "x\n100ms", "off", "0.1s"):
        ran = _run_wait_block(value)
        assert ran.returncode == 1, (value, ran.stdout)
        assert "fastpath-edge-decide-cache-wait is 0, 1ms to 250ms, or absent" in ran.stderr, (value, ran.stderr)


def test_the_wait_needs_the_decide_path() -> None:
    for decide in ("false", ""):
        ran = _run_wait_block("100ms", decide)
        assert ran.returncode == 1, (decide, ran.stdout)
        assert "needs fastpath-edge-decide true" in ran.stderr, (decide, ran.stderr)


def test_the_bound_is_the_releases() -> None:
    """The script's bound is the edge's MaxCacheWait, which service.go and
    edge.New hold the flag to."""
    decide_path = (ROOT / "fastpath" / "internal" / "edge" / "decide_path.go").read_text(encoding="utf-8")
    assert re.search(r"^\tMaxCacheWait = 250 \* time\.Millisecond$", decide_path, re.M)


def test_the_flag_waits_for_the_attribute_and_a_release_that_takes_it() -> None:
    """The unit passes the wait's flag only from instance metadata; HEAD's
    command takes it, and once Terraform sets the attribute the pinned
    release must take it too: a flag it does not take stops the node before
    either role starts."""
    script = SCRIPT.read_text(encoding="utf-8")
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$", script, re.M)
    assert unit_line and "$cache_wait_flags" in unit_line.group(0).split(), unit_line
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert "edge-decide-cache-wait" in head
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    local = re.findall(r'^\s*fastpath_edge_decide_cache_wait\s*=\s*"([^"]*)"$', tf, re.M)
    assert len(local) == 1, local
    assert "local.fastpath_edge_decide_cache_wait_metadata," in tf
    if local[0]:
        assert re.fullmatch(r"0|[1-9]ms|[1-9][0-9]ms|1[0-9][0-9]ms|2[0-4][0-9]ms|250ms", local[0]), local[0]
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert "edge-decide-cache-wait" in taken


def test_the_runbook_names_the_switch_and_its_counts() -> None:
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = deploy[deploy.index("## The decide cache's wait"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    for needed in ('fastpath_edge_decide_cache_wait = "100ms"', 'fastpath_edge_decide_cache_wait = "0"',
                   "-edge-decide-cache-wait", "cache_miss", "cache_wait", "stale.idle", "cold.evicted",
                   "decide-cache-wait"):
        assert needed in section, needed
