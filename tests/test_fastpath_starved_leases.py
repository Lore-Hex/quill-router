"""What an owner does with a starved shard's leases as the node is started
with it (docs/design/billing-503-reduction-2026-11.md, option A): the startup
script passes -starved-leases from the instance attribute
fastpath-starved-leases alone, absent by default so the release's default
(log) holds, and refuses a value the release would not take before it writes
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


def _run_starved_block(value: str | None) -> subprocess.CompletedProcess[str]:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('starved_leases="$(attribute fastpath-starved-leases)"')
    block = script[start:script.index("\nfi\n", start) + len("\nfi\n")]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = 'attribute() { printf "%s" "${A_STARVED:-}"; }\nsystemd-cat() { cat >&2; }\n'
    env = {"PATH": "/usr/bin:/bin", **({"A_STARVED": value} if value is not None else {})}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$starved_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def test_absent_the_unit_passes_no_starved_flag() -> None:
    for value in (None, ""):
        ran = _run_starved_block(value)
        assert ran.returncode == 0 and ran.stdout == "", (value, ran.stderr)


def test_off_log_or_close_pass_as_the_flag() -> None:
    for value in ("off", "log", "close"):
        ran = _run_starved_block(value)
        assert ran.returncode == 0, (value, ran.stderr)
        assert ran.stdout.split() == ["-starved-leases", value]


def test_a_value_the_release_would_refuse_stops_the_script() -> None:
    for value in ("on", "Close", "true", "1", " close", "close ", "close -roles auditor", "close\n-roles x",
                  "x\nclose"):
        ran = _run_starved_block(value)
        assert ran.returncode == 1, (value, ran.stdout)
        assert "fastpath-starved-leases is off, log, close, or absent" in ran.stderr, (value, ran.stderr)


def test_the_flag_waits_for_the_attribute_and_a_release_that_takes_it() -> None:
    """The unit passes the flag only from instance metadata; HEAD's command
    takes it, and once Terraform sets the attribute the pinned release must
    take it too: a flag it does not take stops the node before either role
    starts."""
    script = SCRIPT.read_text(encoding="utf-8")
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$", script, re.M)
    assert unit_line and "$starved_flags" in unit_line.group(0).split(), unit_line
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert "starved-leases" in head
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    local = re.findall(r'^\s*fastpath_starved_leases\s*=\s*"([^"]*)"$', tf, re.M)
    assert len(local) == 1, local
    assert "local.fastpath_starved_leases_metadata," in tf
    if local[0]:
        assert local[0] in {"off", "log", "close"}, local[0]
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert "starved-leases" in taken


def test_the_runbook_names_the_switch_and_the_evidence() -> None:
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = deploy[deploy.index("## Starved leases"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    for needed in ('fastpath_starved_leases = "close"', "owner starved shard", "billing.lease_headroom_unavailable",
                   "sudo google_metadata_script_runner startup", "-starved-leases close", "Taking a pinned release"):
        assert needed in section, needed
