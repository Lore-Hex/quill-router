"""Proof by EXECUTION that a bring-up script runs the completeness gate.

This file replaces a regex. The regex asked whether the string
``verify_cloud_complete.sh <cloud>`` appeared in a script's last N lines, and
three independent reviews killed it for the same reason: a heredoc body, a
printed instruction and a commented-out line all satisfy it. That is verbatim
the bug the whole change exists to prevent — printing the step counted as doing
the step — reproduced inside the check written to end it.

So every bound script is RUN, in ``tests/deploy_script_harness.py``'s hermetic
harness, and two things are asserted about what it did:

  1. it CALLED the gate, for its own cloud;
  2. when the gate FAILS, it exits non-zero.

Both are properties of the process, not of the text. A printed instruction
fails (1). A call whose status is swallowed — ``verify ... || true``, a call
inside ``if`` with an empty else, a call followed by ``exit 0`` — fails (2).

A third assertion covers what "must be in the last N lines" was really reaching
for: no cloud CLI runs AFTER the gate answered, except cleanup a fixture names.
A gate that passes and is then followed by more provisioning checked a cloud
that did not exist yet.

WHAT IS NOT PROVEN HERE, SAID PLAINLY
-------------------------------------
``scripts/deploy/aws_eu_clickhouse_drain_install.sh`` is recorded as
``NOT_PROVEN`` and this file does not run it. Its reason lives next to it in
``ROLLOUT_REGISTRY``; the short version is that its middle ships a payload over
SSM and reads the drain's own journal back, so a stub that answers
``Status=Success`` to everything would be the harness asserting its own answer.
:func:`test_unproven_scripts_are_declared_and_not_silently_skipped` fails if
that list ever grows without a reason, or if the docs stop saying so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trusted_router import cloud_rollout_completeness as crc

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    summarise,
)

ROOT = Path(__file__).resolve().parents[1]

PROVEN = crc.scripts_proven_by_execution()
UNPROVEN = crc.scripts_not_proven()


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    """One mirrored checkout and one stub PATH for the whole module."""
    return DeployScriptHarness(tmp_path_factory.mktemp("deploy-harness"))


def test_the_registry_actually_binds_something() -> None:
    """If nothing is proven by execution, this file is decorative."""
    assert PROVEN, "no deploy script is proven by execution — the mechanism is disconnected"


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_the_script_calls_the_gate_for_its_own_cloud(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """(1) It RAN the gate. Not "the file mentions it" — it ran it.

    The script executes end to end against recording stubs, so this assertion
    reads the gate's own call log. A `Next: ...` echo, a heredoc quoting the
    command, and a commented-out invocation all produce an empty log.
    """
    run = harness.run(script, verifier_rc=0)
    assert run.gate_ran_for(cloud), (
        f"{script} ran to completion without ever calling verify_cloud_complete.sh "
        f"for {cloud}.\n{summarise(run)}"
    )
    assert run.returncode == 0, (
        f"{script} called the gate, the gate passed, and the script still failed.\n"
        f"{summarise(run)}"
    )


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_a_failing_gate_makes_the_script_fail(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """(2) It cannot report success over a failing gate.

    This is the assertion the old text check could not make at all, and the one
    that catches the likelier regression: not deleting the call, but keeping it
    and losing its exit status — `|| true`, a bare `if`, an `exit 0` after it.
    """
    run = harness.run(script, verifier_rc=1)
    assert run.gate_ran_for(cloud), summarise(run)
    assert run.returncode != 0, (
        f"{script} exited 0 with the completeness gate FAILING. That is the outage's "
        f"shape: a finished script and a working cloud are different things.\n"
        f"{summarise(run)}"
    )


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_every_gate_exit_code_survives_the_script(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """All bound scripts understand the gate's codes, or none of them do.

    Exit 5 (NOT YET OBSERVABLE) used to be taught to exactly one of five: the
    other four reported today's real state — no deployed control plane publishes
    the `analytics` section — as INCOMPLETE ROLLOUT with a fix that would not
    have fixed it, which is how an operator learns to stop reading exit codes.
    The mapping is one shared file now, and this asserts the consequence rather
    than the mechanism: each distinct code comes out the far end unchanged.
    """
    for rc in (1, 5, 6, 7):
        run = harness.run(script, verifier_rc=rc)
        assert run.returncode == rc, (
            f"{script} turned gate exit {rc} into {run.returncode}. The codes mean "
            f"different things to an operator; collapsing them is the defect.\n"
            f"{summarise(run)}"
        )


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_nothing_provisions_after_the_gate_has_answered(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """The measured form of "the check must be the LAST thing it does".

    A gate in the middle, followed by twenty more steps that mutate the cloud,
    is a check of a cloud that did not exist yet. The old rule approximated this
    by counting lines from the end of the file; this counts commands from the
    call in an execution trace.
    """
    fixture = SCRIPT_FIXTURES.get(script)
    allowed = fixture.cleanup_after_gate if fixture else ()
    run = harness.run(script, verifier_rc=0)
    stragglers = run.cloud_cli_calls_after_the_gate(allowed)
    assert stragglers == [], (
        f"{script} runs the gate for {cloud} and then keeps provisioning: "
        f"{[' '.join(call[:4]) for call in stragglers]}. Either move the gate to the end "
        "or, if these are cleanup, name them in cleanup_after_gate in "
        "tests/deploy_script_harness.py."
    )


def test_the_shared_gate_library_returns_the_verifier_status_unaltered(
    harness: DeployScriptHarness, tmp_path: Path
) -> None:
    """The one function every bound script funnels through, exercised directly.

    Every non-zero code the verifier can produce has to come back out. This is
    what makes "all five scripts understand exit 5" a property of one file
    rather than five copies of a `case` statement, one of which had it.
    """
    import subprocess

    caller = tmp_path / "caller.sh"
    caller.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'. "{harness.mirror}/scripts/deploy/cloud_complete_gate.sh"\n'
        'require_cloud_complete "$1" "next steps for the operator"\n'
    )
    for rc in (0, 1, 5, 6, 7):
        result = subprocess.run(  # noqa: S603
            ["bash", str(caller), "aws"],  # noqa: S607
            capture_output=True,
            text=True,
            env={
                "PATH": str(harness.bin),
                "HOME": str(tmp_path),
                "HARNESS_ARGV_LOG": str(tmp_path / "argv.log"),
                "HARNESS_VERIFIER_RC": str(rc),
                "CLOUD_COMPLETE_GATE_DIR": str(harness.mirror / "scripts" / "deploy"),
            },
        )
        assert result.returncode == rc, (rc, result.returncode, result.stderr)
        if rc != 0:
            assert "next steps for the operator" in result.stderr

    # ...and each non-zero code gets its own words, so an operator is not told
    # to fix an install that did not fail.
    def stderr_for(rc: int) -> str:
        return subprocess.run(  # noqa: S603
            ["bash", str(caller), "aws"],  # noqa: S607
            capture_output=True,
            text=True,
            env={
                "PATH": str(harness.bin),
                "HOME": str(tmp_path),
                "HARNESS_ARGV_LOG": str(tmp_path / "argv.log"),
                "HARNESS_VERIFIER_RC": str(rc),
                "CLOUD_COMPLETE_GATE_DIR": str(harness.mirror / "scripts" / "deploy"),
            },
        ).stderr

    assert "NOT YET OBSERVABLE" in stderr_for(5)
    assert "NOT VERIFIED" in stderr_for(6)
    assert "UNREADABLE STATUS PAGE" in stderr_for(7)


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_each_gate_outcome_gets_the_same_words_from_every_script(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """The consequence of sharing the library, read off the scripts' own output.

    Exit 5 used to be taught to exactly one of five bound scripts: the other
    four reported today's real state — no control plane publishes the analytics
    section yet — as "INCOMPLETE ROLLOUT" with a fix that would not have fixed
    it. So this does not check that a file sources a file; it runs the script
    under each outcome and reads what the operator would have been told.
    """
    expected = {
        5: "NOT YET OBSERVABLE",
        6: "NOT VERIFIED",
        7: "UNREADABLE STATUS PAGE",
    }
    for rc, phrase in expected.items():
        run = harness.run(script, verifier_rc=rc)
        assert phrase in run.stderr, (
            f"{script} exited {run.returncode} on gate code {rc} without telling the "
            f"operator {phrase!r}, so it has its own idea of what that code means.\n"
            f"{summarise(run)}"
        )


#: The three shapes the old text check accepted, written as scripts. Each one
#: contains the exact string ``verify_cloud_complete.sh aws`` in its last lines
#: and each one is a lie; the regex passed all three.
_SABOTEURS = {
    "printed_instruction": """#!/usr/bin/env bash
set -euo pipefail
echo "provisioned everything"
cat <<'NEXT'
Next: bash scripts/deploy/verify_cloud_complete.sh aws
NEXT
exit 0
""",
    "commented_out": """#!/usr/bin/env bash
set -euo pipefail
echo "provisioned everything"
# bash "${SCRIPT_DIR}/verify_cloud_complete.sh" aws
exit 0
""",
    "swallowed_status": """#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "${SCRIPT_DIR}/verify_cloud_complete.sh" aws || true
exit 0
""",
}


@pytest.mark.parametrize("shape", sorted(_SABOTEURS))
def test_the_shapes_the_old_regex_accepted_now_fail(
    harness: DeployScriptHarness, shape: str
) -> None:
    """Demonstrate the fix rather than assert it.

    Each of these satisfies "the string `verify_cloud_complete.sh aws` appears
    in the last N lines", which is what the previous binding checked. Under
    execution the first two never call the gate at all and the third calls it
    and throws its answer away — so each fails one of the two properties, which
    is the whole point of moving from text to behaviour.
    """
    path = harness.write_script(f"scripts/deploy/_saboteur_{shape}.sh", _SABOTEURS[shape])
    passing = harness.run(path, verifier_rc=0)
    failing = harness.run(path, verifier_rc=1)

    called = passing.gate_ran_for("aws")
    survived_a_failing_gate = failing.returncode == 0
    assert not called or survived_a_failing_gate, (
        f"the {shape} saboteur should fail one of the two properties, and did not"
    )
    if shape == "swallowed_status":
        assert called, "this one does call the gate; that was never the problem"
        assert survived_a_failing_gate, "and it reports success over a failing gate"
    else:
        assert not called, f"the {shape} saboteur must never reach the gate"


def test_unproven_scripts_are_declared_and_not_silently_skipped() -> None:
    """A script this harness cannot run honestly must SAY so, in code and docs.

    The permitted answer to "the harness cannot run this one" is a written
    reason, not a quiet omission — an omission is exactly the shape of the
    original defect. Today there is one, and this pins that the docs name the
    same file, so a reader of the runbook and a reader of the registry get the
    same list.
    """
    for script, cloud, reason in UNPROVEN:
        assert reason.strip(), f"{cloud}: {script} is NOT_PROVEN with no reason"
        assert len(reason) > 120, f"{cloud}: {script}'s unproven_reason is a shrug"
        assert (ROOT / script).is_file()

    doc = (ROOT / "docs" / "storage-portability" / "multi-cloud-separation.md").read_text()
    for script, _cloud, _reason in UNPROVEN:
        assert Path(script).name in doc, (
            f"{script} is not proven by execution and the docs do not say so. "
            "A smaller true claim beats a larger false one, but only if it is written down."
        )


def test_the_unprovable_script_really_is_unprovable(harness: DeployScriptHarness) -> None:
    """Show the failure rather than asserting it in prose.

    ``aws_eu_clickhouse_drain_install.sh`` is claimed to be unrunnable under
    stubs. That claim is itself checkable: run it and watch it stop before the
    gate. If somebody later makes it runnable, this fails and the registry entry
    should become PROVEN_BY_EXECUTION — which is the right way round.
    """
    unproven_paths = [script for script, _cloud, _reason in UNPROVEN]
    if "scripts/deploy/aws_eu_clickhouse_drain_install.sh" not in unproven_paths:
        pytest.skip("the drain installer is no longer claimed to be unprovable")
    run = harness.run("scripts/deploy/aws_eu_clickhouse_drain_install.sh", verifier_rc=0)
    assert not run.gate_ran_for("aws"), (
        "the drain installer now reaches the gate under stubs — promote it to "
        "PROVEN_BY_EXECUTION in ROLLOUT_REGISTRY and delete this test's premise"
    )
    assert run.returncode != 0
