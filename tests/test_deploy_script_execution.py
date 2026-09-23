"""Proof by EXECUTION that a bring-up script runs the completeness gate.

This file replaces a regex. The regex asked whether the string
``verify_cloud_complete.sh <cloud>`` appeared in a script's last N lines, and
three independent reviews killed it for the same reason: a heredoc body, a
printed instruction and a commented-out line all satisfy it. That is verbatim
the bug the whole change exists to prevent — printing the step counted as doing
the step — reproduced inside the check written to end it.

So every bound script is RUN, in ``tests/deploy_script_harness.py``'s stub-PATH
harness — isolation by NAME rather than a sandbox, which that module's own
header spells out — and two things are asserted about what it did:

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

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from trusted_router import cloud_rollout_completeness as crc
from trusted_router.config import Settings

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    live_schedule_targets,
    summarise,
)

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or "/usr/bin/git"

PROVEN = crc.scripts_proven_by_execution()
UNPROVEN = crc.scripts_not_proven()


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    """One mirrored checkout and one stub PATH for the whole module."""
    return DeployScriptHarness(tmp_path_factory.mktemp("deploy-harness"))


def _settings_from_containerapp_mutation(call: list[str]) -> Settings:
    if "--set-env-vars" in call:
        start = call.index("--set-env-vars") + 1
        end = call.index("--remove-env-vars")
    else:
        start = call.index("--env-vars") + 1
        end = call.index("--target-port")
    raw_env = {
        argument.partition("=")[0]: argument.partition("=")[2]
        for argument in call[start:end]
        if argument.startswith("TR_") and "=" in argument
    }
    kwargs = {env_name.removeprefix("TR_").lower(): value for env_name, value in raw_env.items()}
    # Container Apps resolves these references before starting the process.
    kwargs["attribution_cookie_secret"] = "a" * 64
    kwargs["postgres_dsn"] = "postgresql://canary.invalid/trustedrouter"
    return Settings(**kwargs)


def _cloud_run_job_env(call: list[str]) -> dict[str, str]:
    serialized = call[call.index("--set-env-vars") + 1]
    assert serialized.startswith("^|^")
    return {
        assignment.partition("=")[0]: assignment.partition("=")[2]
        for assignment in serialized.removeprefix("^|^").split("|")
    }


def _settings_kwargs_from_cloud_run_job(call: list[str]) -> dict[str, object]:
    kwargs: dict[str, object] = {
        name.removeprefix("TR_").lower(): value
        for name, value in _cloud_run_job_env(call).items()
        if name.startswith("TR_")
    }
    secret_flag = next(flag for flag in ("--update-secrets", "--set-secrets") if flag in call)
    for binding in call[call.index(secret_flag) + 1].split(","):
        name, separator, reference = binding.partition("=")
        if not separator or not name.startswith("TR_"):
            continue
        secret_name = reference.partition(":")[0]
        kwargs[name.removeprefix("TR_").lower()] = f"harness-{secret_name}"
    return kwargs


_TYPED_COUNTERS = "scripts/deploy/migrate_typed_counters.sh"
_RESERVATION_INDEX = "tr_reservation_by_authorization"
_TYPED_COUNTERS_GCLOUD_STUB = r"""#!/usr/bin/env bash
record=gcloud
for argument in "$@"; do
  recorded="${argument//$'\n'/\\n}"
  recorded="${recorded//$'\t'/\\t}"
  record="$record"$'\t'"$recorded"
done
printf '%s\n' "$record" >> "$HARNESS_ARGV_LOG"

state=$(cat "$HARNESS_INDEX_STATE")
case "$*" in
  *"spanner databases ddl update"*)
    if [[ "$*" == *"CREATE NULL_FILTERED INDEX tr_reservation_by_authorization"* ]]; then
      [ "$state" = MISSING ] || exit 1
      printf 'WRITE_ONLY\n' > "$HARNESS_INDEX_STATE"
    fi
    ;;
  *"SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES"*"index_name='tr_reservation_by_authorization'"*)
    if [ "$state" = MISSING ]; then echo 0; else echo 1; fi
    ;;
  *"SELECT INDEX_STATE"*"index_name='tr_reservation_by_authorization'"*)
    polls=0
    [ ! -f "$HARNESS_INDEX_STATE.polls" ] || polls=$(cat "$HARNESS_INDEX_STATE.polls")
    if [ "$state" = WRITE_ONLY ] && [ "$polls" -ge 2 ] && \
       [ "$HARNESS_FINISH_BACKFILL" = true ]; then
      state=READ_WRITE
      printf '%s\n' "$state" > "$HARNESS_INDEX_STATE"
    fi
    printf '%s\n' "$((polls + 1))" > "$HARNESS_INDEX_STATE.polls"
    printf 'index-state\t%s\n' "$state" >> "$HARNESS_ARGV_LOG"
    [ "$state" = MISSING ] || printf '%s\n' "$state"
    ;;
  *"SELECT INDEX_STATE"*) echo READ_WRITE ;;
  *"SELECT SPANNER_STATE"*) echo COMMITTED ;;
  *"SELECT COUNT(*) FROM INFORMATION_SCHEMA."*) echo 1 ;;
  *) exit 1 ;;
esac
"""


@pytest.mark.parametrize(
    ("initial_state", "finish_backfill"),
    [
        pytest.param("MISSING", True, id="fresh-index"),
        pytest.param("WRITE_ONLY", True, id="resume-backfill"),
        pytest.param("READ_WRITE", True, id="already-ready"),
        pytest.param("WRITE_ONLY", False, id="unfinished-backfill"),
    ],
)
def test_typed_counters_reservation_authorization_index(
    tmp_path: Path, initial_state: str, finish_backfill: bool
) -> None:
    # Other schema objects already exist. Only this index's presence/readiness
    # varies; creating it starts a backfill, not an immediately usable index.
    isolated = DeployScriptHarness(tmp_path / "typed-counters")
    gcloud = isolated.bin / "gcloud"
    gcloud.write_text(_TYPED_COUNTERS_GCLOUD_STUB)
    state_file = tmp_path / "index-state"
    state_file.write_text(initial_state + "\n")

    run = isolated.run(
        _TYPED_COUNTERS,
        extra_env={
            "SPANNER_INSTANCE_ID": "harness-instance",
            "SPANNER_DATABASE_ID": "harness-database",
            "GCP_PROJECT_ID": "harness-project",
            "HARNESS_INDEX_STATE": str(state_file),
            "HARNESS_FINISH_BACKFILL": str(finish_backfill).lower(),
        },
    )

    ddl_calls = [
        call for call in run.calls
        if call[:5] == ["gcloud", "spanner", "databases", "ddl", "update"]
    ]
    if initial_state == "MISSING":
        assert len(ddl_calls) == 1, summarise(run)
        ddl = next(arg.removeprefix("--ddl=") for arg in ddl_calls[0] if arg.startswith("--ddl="))
        assert " ".join(ddl.replace("\\n", " ").split()) == (
            f"CREATE NULL_FILTERED INDEX {_RESERVATION_INDEX} "
            "ON tr_reservation (authorization_id)"
        )
        assert run.calls.index(ddl_calls[0]) < run.calls.index(["index-state", "WRITE_ONLY"])
    else:
        assert ddl_calls == [], summarise(run)

    states = [call[1] for call in run.calls if call[0] == "index-state"]
    sleeps = [call for call in run.calls if call[0] == "sleep"]
    if not finish_backfill:
        assert states and set(states) == {"WRITE_ONLY"}, summarise(run)
        assert run.returncode != 0, summarise(run)
        assert f"timed out waiting for {_RESERVATION_INDEX}" in run.stdout
        assert f"{_RESERVATION_INDEX} is read-write" not in run.stdout
        assert "[migrate_typed_counters] done" not in run.stdout
        return

    assert run.returncode == 0, summarise(run)
    expected_states = ["READ_WRITE"] if initial_state == "READ_WRITE" else [
        "WRITE_ONLY", "WRITE_ONLY", "READ_WRITE"
    ]
    assert states == expected_states, summarise(run)
    assert sleeps == [["sleep", "5"]] * (len(expected_states) - 1)
    ready = run.stdout.index(f"{_RESERVATION_INDEX} is read-write")
    done = run.stdout.index("[migrate_typed_counters] done")
    assert ready < done
    if initial_state != "READ_WRITE":
        waiting = run.stdout.index(f"waiting for {_RESERVATION_INDEX} backfill (state=WRITE_ONLY)")
        assert waiting < ready
    assert state_file.read_text().strip() == "READ_WRITE"


_REGIONAL_QUOTA_RECONCILER = "scripts/deploy/regional_quota_reconciler.sh"
_RECONCILER_GCLOUD_STUB = r"""#!/usr/bin/env bash
{ printf '%s' "${0##*/}"; for argument in "$@"; do
    recorded="${argument//$'\n'/\\n}"
    recorded="${recorded//$'\t'/\\t}"
    printf '\t%s' "$recorded"
  done
  printf '\n'
} >> "$HARNESS_ARGV_LOG"

if [[ " $* " == *" scheduler jobs describe "* ]]; then
  printf '%s' "${HARNESS_SCHEDULER_DESCRIBE_STDERR:-}" >&2
  if [ -n "${HARNESS_SCHEDULER_STATE:-}" ]; then
    printf '%s\n' "$HARNESS_SCHEDULER_STATE"
  fi
  exit "${HARNESS_SCHEDULER_DESCRIBE_RC:-0}"
fi

if [[ " $* " == *" run jobs list "* ]] && \
   [ "${HARNESS_VERSIONED_JOB_EXISTS:-false}" = "true" ]; then
  printf '%s\n' "$HARNESS_VERSIONED_JOB_NAME"
fi

if [[ " $* " == *" run jobs list "* ]] && \
   [[ " $* " == *" --sort-by="* ]] && \
   [ -n "${HARNESS_STALE_JOB_NAMES:-}" ]; then
  printf '%s\n' "$HARNESS_STALE_JOB_NAMES"
fi

if [[ " $* " == *" projects describe "* ]]; then
  printf '%s\n' '123456789'
fi
exit 0
"""


def _run_regional_quota_reconciler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str = "",
    describe_rc: int = 0,
    describe_stderr: str = "",
    versioned_job_exists: bool = False,
    stale_job_names: str = "",
    extra_env: dict[str, str] | None = None,
) -> HarnessRun:
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        _REGIONAL_QUOTA_RECONCILER,
        ScriptFixture(
            env={
                "HARNESS_SCHEDULER_STATE": state,
                "HARNESS_SCHEDULER_DESCRIBE_RC": str(describe_rc),
                "HARNESS_SCHEDULER_DESCRIBE_STDERR": describe_stderr,
                "HARNESS_VERSIONED_JOB_EXISTS": str(versioned_job_exists).lower(),
                "HARNESS_VERSIONED_JOB_NAME": (
                    "trusted-router-regional-quota-reconciler-existing"
                ),
                "HARNESS_STALE_JOB_NAMES": stale_job_names,
                "TR_REGIONAL_QUOTA_RECONCILER_JOB": (
                    "trusted-router-regional-quota-reconciler-existing"
                    if versioned_job_exists
                    else ""
                ),
            }
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "regional-quota-reconciler")
    gcloud = isolated.bin / "gcloud"
    gcloud.write_text(_RECONCILER_GCLOUD_STUB)
    gcloud.chmod(0o755)
    return isolated.run(_REGIONAL_QUOTA_RECONCILER, extra_env=extra_env)


def _gcloud_calls(run: HarnessRun, *command: str) -> list[list[str]]:
    return [
        call
        for call in run.calls
        if call[0] == "gcloud" and call[3 : 3 + len(command)] == list(command)
    ]


_RAMP_SECONDARIES = "scripts/deploy/ramp_secondaries.sh"


def _run_secondary_ramp(
    tmp_path: Path,
    *,
    traffic: str,
    holds: str = "",
    overrides: dict[str, str] | None = None,
) -> HarnessRun:
    isolated = DeployScriptHarness(tmp_path / "secondary-ramp")
    isolated.write_script(
        "scripts/deploy/watchdog.py",
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

with open(os.environ["HARNESS_ARGV_LOG"], "a", encoding="utf-8") as log:
    log.write("watchdog.py\\t" + "\\t".join(sys.argv[1:]) + "\\n")
if "--baseline-output" in sys.argv:
    output = pathlib.Path(sys.argv[sys.argv.index("--baseline-output") + 1])
    output.write_text(json.dumps({"harness": "up"}) + "\\n")
""",
    )
    for relative, command_name in (
        ("scripts/deploy/assert_no_billing_5xx.sh", "assert_no_billing_5xx.sh"),
        ("scripts/deploy/regional_quota_reconciler.sh", "regional_quota_reconciler.sh"),
        ("scripts/deploy/spend_lease_reconciler.sh", "spend_lease_reconciler.sh"),
    ):
        # The reconcilers launch concurrently, so append each invocation with
        # one printf; separate name/newline writes can merge into one record.
        isolated.write_script(
            relative,
            "#!/usr/bin/env bash\n"
            "record='"
            + command_name
            + "'\n"
            "for argument in \"$@\"; do "
            "printf -v record '%s\\t%s' \"$record\" \"$argument\"; done\n"
            "printf '%s\\n' \"$record\" >>\"$HARNESS_ARGV_LOG\"\n",
        )

    env = {
        "PREV_EU": "trusted-router-01300-euold",
        "PREV_US_EAST4": "trusted-router-01300-useold",
        "PREV_SOUTHAMERICA_EAST1": "trusted-router-01300-saold",
        "NEW_EU": "trusted-router-01302-eunew",
        "NEW_US_EAST4": "trusted-router-01302-usenew",
        "NEW_SOUTHAMERICA_EAST1": "trusted-router-01302-sanew",
        "TR_DEPLOY_HOLD_REGIONS": holds,
        "TR_DEPLOY_MUTEX_OPERATION": "harness-secondary-operation",
        "TR_DEPLOY_MUTEX_GENERATION": "1",
        "HARNESS_CLOUD_RUN_TRAFFIC": traffic,
        **(overrides or {}),
    }
    return isolated.run(_RAMP_SECONDARIES, extra_env=env, timeout=30)


def _regional_traffic_updates(run: HarnessRun, region: str) -> list[str]:
    updates: list[str] = []
    for call in run.calls:
        if call[:5] != [
            "gcloud",
            "run",
            "services",
            "update-traffic",
            "trusted-router",
        ]:
            continue
        if f"--region={region}" not in call:
            continue
        updates.extend(
            argument.removeprefix("--to-revisions=")
            for argument in call
            if argument.startswith("--to-revisions=")
        )
    return updates


def _regional_update_traffic_calls(run: HarnessRun, region: str) -> list[list[str]]:
    return [
        call
        for call in run.calls
        if call[:5]
        == ["gcloud", "run", "services", "update-traffic", "trusted-router"]
        and f"--region={region}" in call
    ]


def _default_secondary_traffic() -> str:
    return (
        "europe-west4=trusted-router-01300-euold:100;"
        "us-east4=trusted-router-01300-useold:100;"
        "southamerica-east1=trusted-router-01300-saold:100"
    )


def test_secondary_hold_never_updates_held_region_while_siblings_ramp(
    tmp_path: Path,
) -> None:
    run = _run_secondary_ramp(
        tmp_path,
        traffic=_default_secondary_traffic(),
        holds="europe-west4",
    )

    assert run.returncode == 0, summarise(run)
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert _regional_traffic_updates(run, "us-east4") == [
        "trusted-router-01302-usenew=10,trusted-router-01300-useold=90",
        "trusted-router-01302-usenew=50,trusted-router-01300-useold=50",
        "trusted-router-01302-usenew=100",
    ]
    assert _regional_traffic_updates(run, "southamerica-east1") == [
        "trusted-router-01302-sanew=10,trusted-router-01300-saold=90",
        "trusted-router-01302-sanew=50,trusted-router-01300-saold=50",
        "trusted-router-01302-sanew=100",
    ]


def test_secondary_ramp_never_routes_to_job_start_snapshot_after_pin(
    tmp_path: Path,
) -> None:
    traffic = (
        "europe-west4=trusted-router-01302-eunew:100;"
        "us-east4=trusted-router-01301-usehot:100;"
        "southamerica-east1=trusted-router-01301-sahot:100"
    )
    run = _run_secondary_ramp(
        tmp_path,
        traffic=traffic,
        overrides={
            "PREV_US_EAST4": "trusted-router-01301-usehot",
            "PREV_SOUTHAMERICA_EAST1": "trusted-router-01301-sahot",
        },
    )

    assert run.returncode == 0, summarise(run)
    all_updates = [
        assignment
        for region in ("europe-west4", "us-east4", "southamerica-east1")
        for assignment in _regional_traffic_updates(run, region)
    ]
    assert not any("trusted-router-01300-euold" in update for update in all_updates)
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert _regional_traffic_updates(run, "us-east4") == [
        "trusted-router-01302-usenew=10,trusted-router-01301-usehot=90",
        "trusted-router-01302-usenew=50,trusted-router-01301-usehot=50",
        "trusted-router-01302-usenew=100",
    ]
    assert _regional_traffic_updates(run, "southamerica-east1") == [
        "trusted-router-01302-sanew=10,trusted-router-01301-sahot=90",
        "trusted-router-01302-sanew=50,trusted-router-01301-sahot=50",
        "trusted-router-01302-sanew=100",
    ]


def test_secondary_ramp_refuses_older_new_revision_without_traffic_mutation(
    tmp_path: Path,
) -> None:
    run = _run_secondary_ramp(
        tmp_path,
        traffic=_default_secondary_traffic(),
        overrides={"NEW_EU": "trusted-router-01299-eunew"},
    )

    assert run.returncode != 0
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert _regional_traffic_updates(run, "us-east4") == []
    assert _regional_traffic_updates(run, "southamerica-east1") == []
    assert "held= ramped= refused=europe-west4" in run.stdout


def test_secondary_ramp_refuses_ambiguous_serving_split_without_mutation(
    tmp_path: Path,
) -> None:
    traffic = _default_secondary_traffic().replace(
        "europe-west4=trusted-router-01300-euold:100",
        "europe-west4=trusted-router-01300-euold:50,"
        "trusted-router-01301-euhot:50",
    )
    run = _run_secondary_ramp(tmp_path, traffic=traffic)

    assert run.returncode != 0
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert "split=trusted-router-01300-euold:50,trusted-router-01301-euhot:50" in (
        run.stderr
    )
    assert "held= ramped= refused=europe-west4" in run.stdout


def test_secondary_operator_intervention_is_held_and_siblings_continue(
    tmp_path: Path,
) -> None:
    traffic = _default_secondary_traffic().replace(
        "trusted-router-01300-euold", "trusted-router-01301-euhot"
    )
    run = _run_secondary_ramp(tmp_path, traffic=traffic)

    assert run.returncode == 0, summarise(run)
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert _regional_traffic_updates(run, "us-east4")
    assert _regional_traffic_updates(run, "southamerica-east1")
    assert "held=europe-west4" in run.stdout


def test_secondary_already_on_new_still_runs_watchdog_and_billing_gate(
    tmp_path: Path,
) -> None:
    traffic = _default_secondary_traffic().replace(
        "trusted-router-01300-euold", "trusted-router-01302-eunew"
    )
    run = _run_secondary_ramp(
        tmp_path,
        traffic=traffic,
        holds="us-east4,southamerica-east1",
    )

    assert run.returncode == 0, summarise(run)
    assert _regional_update_traffic_calls(run, "europe-west4") == []
    assert any(
        call[0] == "watchdog.py" and "europe-west4" in call for call in run.calls
    )
    assert any(
        call[0] == "assert_no_billing_5xx.sh" and call[1] == "europe-west4"
        for call in run.calls
    )


def test_secondary_reconcilers_launch_when_every_region_is_held(
    tmp_path: Path,
) -> None:
    run = _run_secondary_ramp(
        tmp_path,
        traffic=_default_secondary_traffic(),
        holds="all",
    )

    assert run.returncode == 0, summarise(run)
    assert [call for call in run.calls if call[0] == "regional_quota_reconciler.sh"]
    assert [call for call in run.calls if call[0] == "spend_lease_reconciler.sh"]
    assert all(
        not _regional_update_traffic_calls(run, region)
        for region in ("europe-west4", "us-east4", "southamerica-east1")
    )
    assert (
        "held=europe-west4,us-east4,southamerica-east1 ramped= refused="
        in run.stdout
    )


def _initialize_bake_harness_repo(harness: DeployScriptHarness) -> str:
    def run_git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed git operations in a temp repo
            [GIT, *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    run_git("init", "--initial-branch=main", str(harness.mirror))
    for key, value in (
        ("user.email", "deploy-harness@example.test"),
        ("user.name", "Deploy Harness"),
    ):
        run_git("-C", str(harness.mirror), "config", key, value)
    run_git("-C", str(harness.mirror), "add", ".")
    timestamp = int(time.time()) - 2 * 3600
    run_git(
        "-C",
        str(harness.mirror),
        "commit",
        "-m",
        "harness candidate",
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
        },
    )
    origin = harness.root / "origin.git"
    run_git("init", "--bare", str(origin))
    run_git("-C", str(harness.mirror), "remote", "add", "origin", str(origin))
    run_git("-C", str(harness.mirror), "push", "-u", "origin", "main")
    return run_git(
        "-C", str(harness.mirror), "rev-parse", "--short", "HEAD"
    ).stdout.strip()


def test_gcp_no_traffic_warm_preprovisions_and_validates_private_candidate(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run("scripts/deploy/rollout.sh")
    assert run.returncode == 0, summarise(run)

    deploy = next(
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:7] == ["deploy", "trusted-router", "--region"]
    )
    # The primary must absorb a burst without waiting for new instances.
    # Keep the staged revision primer small while retaining the service floor.
    assert deploy[deploy.index("--min") + 1] == "8"
    assert deploy[deploy.index("--min-instances") + 1] == "2"
    assert "--no-traffic" in deploy
    assert any(
        call[0:4] == ["gcloud", "run", "revisions", "describe"]
        and "trusted-router-candidate" in call
        for call in run.calls
    )
    assert not any(
        call[0] == "curl" and "staged-probe---" in call[-1]
        for call in run.calls
    )


_QUOTA_REGIONS = ("us-central1", "us-east4", "europe-west4", "us-west1", "southamerica-east1")
_QUOTA_CLUSTER_MAP = ",".join(f"{region}=trusted-router-logs-c1" for region in _QUOTA_REGIONS)
_QUOTA_PROFILES = ",".join(f"{region}=tr-quota-{region}" for region in _QUOTA_REGIONS)
_REGIONAL_QUOTA_PINS = {
    "TR_REGIONAL_QUOTA_CLUSTER_MAP": _QUOTA_CLUSTER_MAP,
    "TR_SPEND_LEASE_CLUSTER_MAP": "us-central1=trusted-router-logs-c1",
    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": _QUOTA_PROFILES,
    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS": (
        "358d80a4-2c9a-4479-92ea-a681f187477d,f46bf618-4c7c-4a35-afa0-8d48891bf7a5,"
        "1fa994e7-15b1-4e36-9c1c-51ba072d3060,c4ba9257-d212-4d7e-a5a1-989bceb7a1d8,"
        "45819281-0ce9-4811-a0cd-c660ab3a116d"
    ),
    "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS": "300",
    "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS": "10000000",
    "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS": "1000",
    "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT": "16",
    "TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS": "4",
    "TR_REGIONAL_QUOTA_BIGTABLE_TABLE": "trustedrouter-regional-quota",
}


_LIVE_REGIONAL_QUOTA_ENV = {
    "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true",
    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": "true",
    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS": "workspace-pilot,workspace-canary",
    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-central1=tr-quota-us-central1",
    "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS": "60",
    "TR_REGIONAL_QUOTA_BIGTABLE_TABLE": "live-old-table",
    "TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS": "2",
    "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS": "5000000",
    "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS": "2000",
    "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT": "8",
}


def _regional_quota_rollout_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_env: dict[str, str],
) -> DeployScriptHarness:
    script = "scripts/deploy/rollout.sh"
    fixture = SCRIPT_FIXTURES[script]
    revision_env = [{"name": name, "value": value} for name, value in live_env.items()]
    active_revision = json.dumps(
        {"spec": {"containers": [{"env": revision_env}]}},
        separators=(",", ":"),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            env={
                key: value for key, value in fixture.env.items()
                if not key.startswith("TR_REGIONAL_QUOTA_")
            },
            responses=(
                (r"run revisions describe trusted-router-active .*--format=json", active_revision),
                *fixture.responses,
            ),
        ),
    )
    return DeployScriptHarness(tmp_path / "regional-quota-rollout")


@pytest.mark.parametrize(
    ("control", "live", "expected"),
    [
        pytest.param(None, "true", "false", id="absent-pins-live-true-off"),
        pytest.param("", "true", "false", id="empty-pins-live-true-off"),
        pytest.param("preserve", "true", "true", id="dispatch-preserve-live-true"),
        pytest.param("true", "false", "true", id="dispatch-enables-live-false"),
        pytest.param("false", "true", "false", id="dispatch-disables-live-true"),
    ],
)
def test_rollout_regional_quota_issuance_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str | None,
    live: str,
    expected: str,
) -> None:
    live_env = {**_LIVE_REGIONAL_QUOTA_ENV, "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": live}
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, live_env)
    run = isolated.run(
        "scripts/deploy/rollout.sh",
        extra_env={} if control is None else {"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": control},
    )

    assert run.returncode == 0, summarise(run)
    deploy = next(
        call for call in run.calls
        if call[0:6] == ["gcloud", "--project", "quill-cloud-proxy", "run", "deploy", "trusted-router"]
    )
    rendered_env = _cloud_run_job_env(deploy)
    assert rendered_env["TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"] == expected
    assert rendered_env["TR_REGIONAL_QUOTA_LEASES_ENABLED"] == "true"
    for name, value in _REGIONAL_QUOTA_PINS.items():
        assert rendered_env[name] == value
    # Enabling (including preserve=true) must preflight every serving region
    # before it creates a candidate; pausing does not need that preflight.
    before_deploy = run.calls[:run.calls.index(deploy)]
    for region in ("us-central1", "us-east4", "europe-west4", "southamerica-east1"):
        fleet_checked = (
            f"regional quota issuance compatibility: {region}=capable, marker={live}"
            in run.stderr
        )
        assert fleet_checked is (expected == "true")
        if expected == "true":
            assert any(
                "revisions" in call and "trusted-router-active" in call
                and f"--region={region}" in call for call in before_deploy
            )


@pytest.mark.parametrize("missing", [
    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS",
    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES",
])
def test_rollout_regional_quota_dispatch_true_requires_pilot_and_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    isolated = _regional_quota_rollout_harness(
        tmp_path, monkeypatch, {**_LIVE_REGIONAL_QUOTA_ENV, missing: ""},
    )
    run = isolated.run(
        "scripts/deploy/rollout.sh",
        extra_env={"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": "true", missing: ""},
    )

    assert run.returncode != 0, summarise(run)
    expected = (
        "refusing empty regional quota setting: TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"
        if missing == "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"
        else "issuance requires pilot workspaces and fixed Bigtable app profiles"
    )
    assert expected in run.stderr
    assert not any("run" in call and "deploy" in call for call in run.calls)


def test_rollout_regional_quota_dispatch_true_refuses_incompatible_fleet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_env = dict(_LIVE_REGIONAL_QUOTA_ENV)
    del live_env["TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"]
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, live_env)
    run = isolated.run(
        "scripts/deploy/rollout.sh",
        extra_env={"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": "true"},
    )

    assert run.returncode != 0, summarise(run)
    assert "lacks TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" in run.stderr
    assert not any("run" in call and "deploy" in call for call in run.calls)


@pytest.mark.parametrize("live_env", [{}, _LIVE_REGIONAL_QUOTA_ENV], ids=["no-live-settings", "stale-live-settings"])
def test_rollout_renders_every_regional_quota_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_env: dict[str, str],
) -> None:
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, live_env)
    run = isolated.run("scripts/deploy/rollout.sh")
    assert run.returncode == 0, summarise(run)
    deploy = next(call for call in run.calls if call[3:5] == ["run", "deploy"])
    rendered = _cloud_run_job_env(deploy)
    for name, value in _REGIONAL_QUOTA_PINS.items():
        assert rendered[name] == value, name
    assert rendered["TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"] == "false"


@pytest.mark.parametrize("script", [
    "scripts/deploy/rollout.sh",
    "scripts/deploy/spend_lease_ledger.sh",
    "scripts/deploy/spend_lease_reconciler.sh",
])
def test_empty_spend_lease_cluster_map_falls_back_to_independent_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
) -> None:
    if script.endswith("/spend_lease_ledger.sh"):
        monkeypatch.setitem(SCRIPT_FIXTURES, script, ScriptFixture(
            failures=(r"bigtable instances tables describe ", r"bigtable app-profiles describe "),
        ))
    elif script.endswith("/spend_lease_reconciler.sh"):
        monkeypatch.setitem(SCRIPT_FIXTURES, script, ScriptFixture(
            responses=((r"scheduler jobs describe ", "ENABLED"),),
        ))
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, _LIVE_REGIONAL_QUOTA_ENV)
    run = isolated.run(script, extra_env={"TR_SPEND_LEASE_CLUSTER_MAP": ""})
    assert run.returncode == 0, summarise(run)
    expected_profiles = "us-central1=tr-spend-us-central1"
    if script.endswith("/spend_lease_ledger.sh"):
        creates = [call for call in run.calls if call[1:4] == ["bigtable", "app-profiles", "create"]]
        assert len(creates) == 1
        assert creates[0][4] == "tr-spend-us-central1"
        assert "--route-to=trusted-router-logs-c1" in creates[0]
        assert "--transactional-writes" in creates[0]
        assert f"set TR_SPEND_LEASE_BIGTABLE_APP_PROFILES={expected_profiles}\n" in run.stdout
    else:
        mutations = [call for call in run.calls if "--set-env-vars" in call]
        assert len(mutations) == 1
        rendered = _cloud_run_job_env(mutations[0])
        assert rendered["TR_SPEND_LEASE_BIGTABLE_APP_PROFILES"] == expected_profiles
        if script.endswith("/rollout.sh"):
            assert rendered["TR_SPEND_LEASE_CLUSTER_MAP"] == "us-central1=trusted-router-logs-c1"


@pytest.mark.parametrize(("name", "value"), [
    ("TR_REGIONAL_QUOTA_CLUSTER_MAP", "us-east4=trusted-router-logs-c1"),
    ("TR_SPEND_LEASE_CLUSTER_MAP", "us-east4=trusted-router-logs-c1"),
    ("TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES", "us-east4=tr-quota-us-east4"),
    ("TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS", "workspace-override"),
    ("TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS", "90"),
    ("TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS", "7000000"),
    ("TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS", "1500"),
    ("TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT", "4"),
    ("TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS", "3"),
    ("TR_REGIONAL_QUOTA_BIGTABLE_TABLE", "override-quota-table"),
])
def test_rollout_explicit_env_overrides_each_regional_quota_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, _LIVE_REGIONAL_QUOTA_ENV)
    overrides = {name: value}
    if name in {"TR_REGIONAL_QUOTA_CLUSTER_MAP", "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"}:
        overrides.update({
            "TR_REGIONAL_QUOTA_CLUSTER_MAP": "us-east4=trusted-router-logs-c1",
            "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-east4=tr-quota-us-east4",
        })
    run = isolated.run("scripts/deploy/rollout.sh", extra_env=overrides)
    assert run.returncode == 0, summarise(run)
    deploy = next(call for call in run.calls if call[3:5] == ["run", "deploy"])
    rendered = _cloud_run_job_env(deploy)
    for key, expected in {**_REGIONAL_QUOTA_PINS, **overrides}.items():
        assert rendered[key] == expected, key


@pytest.mark.parametrize("script", ["scripts/deploy/rollout.sh", "scripts/deploy/regional_quota_ledger.sh"])
def test_regional_quota_profile_map_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
) -> None:
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, _LIVE_REGIONAL_QUOTA_ENV)
    run = isolated.run(script, extra_env={
        "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-central1=tr-quota-us-central1",
    })
    assert run.returncode != 0, summarise(run)
    assert "profile list mismatch" in run.stdout + run.stderr
    assert not any("create" in call or "deploy" in call for call in run.calls)


_QUOTA_LEDGER = "scripts/deploy/regional_quota_ledger.sh"


@pytest.mark.parametrize("missing", [(), _QUOTA_REGIONS], ids=["idempotent", "create-missing"])
def test_regional_quota_ledger_provisions_all_five_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...],
) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, _QUOTA_LEDGER, ScriptFixture(
        responses=((r"bigtable app-profiles describe .*--format=", "trusted-router-logs-c1\tTrue"),),
        failures=tuple(rf"bigtable app-profiles describe tr-quota-{region} " for region in missing),
    ))
    run = DeployScriptHarness(tmp_path / "ledger").run(_QUOTA_LEDGER)
    assert run.returncode == 0, summarise(run)
    assert f"set TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES={_QUOTA_PROFILES}\n" in run.stdout
    creates = [call for call in run.calls if call[1:4] == ["bigtable", "app-profiles", "create"]]
    assert len(creates) == len(missing)
    assert {call[4] for call in creates} == {f"tr-quota-{region}" for region in missing}
    for call in creates:
        assert "--route-to=trusted-router-logs-c1" in call
        assert "--transactional-writes" in call
    for region in _QUOTA_REGIONS:
        describes = [call for call in run.calls if call[1:5] == [
            "bigtable", "app-profiles", "describe", f"tr-quota-{region}",
        ]]
        assert describes
        if region not in missing:
            assert any("--format=value(singleClusterRouting.clusterId,singleClusterRouting.allowTransactionalWrites)" in call for call in describes)


@pytest.mark.parametrize("config", ["trusted-router-logs-eu\tTrue", "trusted-router-logs-c1\tFalse"])
def test_regional_quota_ledger_refuses_profile_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str,
) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, _QUOTA_LEDGER, ScriptFixture(
        responses=((r"bigtable app-profiles describe .*--format=", config),),
    ))
    run = DeployScriptHarness(tmp_path / "ledger").run(_QUOTA_LEDGER)
    assert run.returncode != 0, summarise(run)
    assert "refusing regional quota profile drift" in run.stdout
    assert not any("create" in call for call in run.calls)


def test_regional_quota_ledger_refuses_unknown_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, _QUOTA_LEDGER, ScriptFixture(
        failures=(r"bigtable clusters describe unknown-cluster ",),
    ))
    run = DeployScriptHarness(tmp_path / "ledger").run(_QUOTA_LEDGER, extra_env={
        "TR_REGIONAL_QUOTA_CLUSTER_MAP": _QUOTA_CLUSTER_MAP.replace("trusted-router-logs-c1", "unknown-cluster"),
    })
    assert run.returncode != 0, summarise(run)
    assert "unknown or unreadable cluster: unknown-cluster" in run.stdout
    assert not any("app-profiles" in call for call in run.calls)


# These are shared inputs even when a phase (the provisioner, for example)
# only consumes a subset. Resolve them once, and reject empty values uniformly.
_SHARED_QUOTA_OVERRIDES = {
    "TR_REGIONAL_QUOTA_CLUSTER_MAP": "us-east4=trusted-router-logs-c1",
    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-east4=tr-quota-us-east4",
    "TR_REGIONAL_QUOTA_BIGTABLE_TABLE": "override-quota-table",
    "TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS": "3",
    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS": "workspace-override",
    "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS": "90",
    "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS": "7000000",
    "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS": "1500",
    "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT": "4",
}
_QUOTA_SCRIPTS = (
    "scripts/deploy/rollout.sh", _QUOTA_LEDGER, _REGIONAL_QUOTA_RECONCILER,
)


@pytest.mark.parametrize("script", _QUOTA_SCRIPTS)
def test_regional_quota_consumers_do_not_resolve_shared_settings_again(script: str) -> None:
    # Early rejection can mask a reintroduced local fallback at runtime (q6).
    # Enforce the single-resolution contract as well as testing execution below.
    source = (ROOT / script).read_text()
    for name in _SHARED_QUOTA_OVERRIDES:
        assert not re.search(r"\$\{" + name + r"(?::?[-+=?])", source), name
        assert not re.search(r"^" + name + r"=", source, re.MULTILINE), name


@pytest.mark.parametrize("script", _QUOTA_SCRIPTS)
@pytest.mark.parametrize("mode", ["absent", "empty", "nonempty"])
@pytest.mark.parametrize("name", _SHARED_QUOTA_OVERRIDES)
def test_regional_quota_shared_settings_agree_across_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, mode: str, name: str,
) -> None:
    isolated = _regional_quota_rollout_harness(tmp_path, monkeypatch, _LIVE_REGIONAL_QUOTA_ENV)
    monkeypatch.setitem(SCRIPT_FIXTURES, _QUOTA_LEDGER, ScriptFixture(
        responses=((r"bigtable app-profiles describe .*--format=", "trusted-router-logs-c1\tTrue"),),
    ))
    monkeypatch.setitem(SCRIPT_FIXTURES, _REGIONAL_QUOTA_RECONCILER, ScriptFixture(
        responses=((r"scheduler jobs describe .*--format=value\(state\)", "ENABLED"),),
    ))
    overrides = {} if mode == "absent" else {
        name: "" if mode == "empty" else _SHARED_QUOTA_OVERRIDES[name],
    }
    if mode == "nonempty" and name in {
        "TR_REGIONAL_QUOTA_CLUSTER_MAP", "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES",
    }:
        overrides.update({key: _SHARED_QUOTA_OVERRIDES[key] for key in (
            "TR_REGIONAL_QUOTA_CLUSTER_MAP", "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES",
        )})
    expected = {**_REGIONAL_QUOTA_PINS, **overrides}
    # The provisioner has no revision env. Observe its resolved inputs after
    # successful execution, in addition to checking actual Bigtable argv below.
    if script == _QUOTA_LEDGER:
        ledger = isolated.mirror / script
        with ledger.open("a") as out:
            for key in _SHARED_QUOTA_OVERRIDES:
                out.write(f'printf "RESOLVED_{key}=%s\\n" "${key}"\n')
    run = isolated.run(script, extra_env=overrides)
    if mode == "empty" and name != "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS":
        assert run.returncode != 0, summarise(run)
        assert f"refusing empty regional quota setting: {name}" in run.stdout + run.stderr
        # Only _lib.sh's read-only project-number lookup may precede refusal.
        # In particular, no mutex write, bucket update, table or revision write.
        assert all(call[0] == "gcloud" and "projects" in call and "describe" in call
                   for call in run.calls), run.calls
        return
    assert run.returncode == 0, summarise(run)
    if script == _QUOTA_LEDGER:
        resolved = dict(line.removeprefix("RESOLVED_").split("=", 1)
                        for line in run.stdout.splitlines() if line.startswith("RESOLVED_"))
        table_calls = [call for call in run.calls if call[1:5] == [
            "bigtable", "instances", "tables", "describe",
        ]]
        assert len(table_calls) == 1
        assert table_calls[0][5] == expected["TR_REGIONAL_QUOTA_BIGTABLE_TABLE"]
        assert f"set TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES={expected['TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES']}\n" in run.stdout
        cluster_calls = [call for call in run.calls if call[1:4] == [
            "bigtable", "clusters", "describe",
        ]]
        assert [call[4] for call in cluster_calls] == [
            entry.split("=")[1] for entry in expected["TR_REGIONAL_QUOTA_CLUSTER_MAP"].split(",")
        ]
    else:
        command = ["run", "deploy"] if script.endswith("/rollout.sh") else ["run", "jobs", "create"]
        deploy = next(call for call in run.calls if call[3:3 + len(command)] == command)
        resolved = _cloud_run_job_env(deploy)
    for key in _SHARED_QUOTA_OVERRIDES:
        assert resolved[key] == expected[key], key


def test_regional_quota_ledger_final_provisioned_list_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, _QUOTA_LEDGER, ScriptFixture(
        responses=((r"bigtable app-profiles describe .*--format=", "trusted-router-logs-c1\tTrue"),),
    ))
    isolated = DeployScriptHarness(tmp_path / "ledger-final-guard")
    ledger = isolated.mirror / _QUOTA_LEDGER
    source = ledger.read_text()
    # Fault injection after pre-validation and the complete provisioning loop:
    # simulate a bug dropping one successfully provisioned profile from output.
    marker = 'profile_csv="$(IFS=\',\'; printf \'%s\' "${profiles[*]}")"'
    assert source.count(marker) == 1
    ledger.write_text(source.replace(marker, 'unset \'profiles[4]\'\n' + marker))
    run = isolated.run(_QUOTA_LEDGER)
    assert run.returncode != 0, summarise(run)
    assert "refusing regional quota provisioned profile list mismatch" in run.stdout
    assert "set TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=" not in run.stdout
    for region in _QUOTA_REGIONS:
        assert any(call[1:5] == [
            "bigtable", "app-profiles", "describe", f"tr-quota-{region}",
        ] for call in run.calls)


@pytest.mark.parametrize("profiles", [None, "us-east4=tr-quota-us-east4"])
def test_reconciler_receives_same_regional_quota_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiles: str | None,
) -> None:
    run = _run_regional_quota_reconciler(
        tmp_path, monkeypatch, state="ENABLED",
        extra_env={} if profiles is None else {
            "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": profiles,
            "TR_REGIONAL_QUOTA_CLUSTER_MAP": "us-east4=trusted-router-logs-c1",
        },
    )
    assert run.returncode == 0, summarise(run)
    deploy = next(call for call in run.calls if call[3:6] == ["run", "jobs", "create"])
    assert _cloud_run_job_env(deploy)["TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"] == (profiles or _QUOTA_PROFILES)


def test_rollout_binding_unit_4_fence_passes_with_settle_clamp(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run("scripts/deploy/rollout.sh")
    assert run.returncode == 0, summarise(run)

    deploy = next(
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["deploy", "trusted-router"]
    )
    serialized_env = deploy[deploy.index("--set-env-vars") + 1]
    assert "TR_SPEND_LEASE_BINDING_ENABLED=true" in serialized_env.split("|")
    assert "TR_SPEND_LEASE_BIGTABLE_TABLE=trustedrouter-spend-lease" in serialized_env.split(
        "|"
    )
    assert (
        "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES=us-central1=tr-spend-us-central1"
        in serialized_env.split("|")
    )


def test_rollout_binding_refuses_empty_spend_lease_app_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/rollout.sh"
    fixture = SCRIPT_FIXTURES[script]
    active_revision = json.dumps(
        {
            "spec": {
                "containers": [
                    {
                        "env": [
                            {
                                "name": "TR_REGIONAL_QUOTA_LEASES_ENABLED",
                                "value": "false",
                            },
                            {
                                "name": "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED",
                                "value": "false",
                            },
                            {
                                "name": "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES",
                                "value": "",
                            },
                        ]
                    }
                ]
            }
        },
        separators=(",", ":"),
    )
    responses = (
        (
            r"run revisions describe trusted-router-active .*--format=json",
            active_revision,
        ),
        *(
            response
            for response in fixture.responses
            if "run revisions describe trusted-router-active" not in response[0]
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "spend-lease-profiles-empty")

    run = isolated.run(script)

    assert run.returncode != 0
    assert (
        "TR_SPEND_LEASE_BINDING_ENABLED=true requires non-empty "
        "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES"
        in run.stderr
    )
    assert not any(
        call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["deploy", "trusted-router"]
        for call in run.calls
    )


def test_rollout_binding_unit_4_fence_refuses_missing_settle_clamp(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "spend-lease-unit-4-missing")
    settlement = (
        isolated.mirror
        / "src/trusted_router/services/spend_lease_settlement.py"
    )
    settlement.write_text(
        settlement.read_text().replace(
            "def clamp_spend_lease_charge(",
            "def removed_spend_lease_charge_clamp(",
            1,
        )
    )

    run = isolated.run("scripts/deploy/rollout.sh")

    assert run.returncode != 0
    assert (
        "TR_SPEND_LEASE_BINDING_ENABLED=true requires spend-lease unit 4 "
        "(missing clamp_spend_lease_charge)" in run.stderr
    )
    assert not any(
        call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["deploy", "trusted-router"]
        for call in run.calls
    )


def test_rollout_lists_optional_secrets_once_without_missing_secret_probes(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run("scripts/deploy/rollout.sh")
    assert run.returncode == 0, summarise(run)

    inventory_calls = [
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "secrets"]
        and call[4] == "list"
    ]
    assert len(inventory_calls) == 1
    assert not any(
        call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "secrets"]
        and call[4] == "describe"
        for call in run.calls
    )


def test_rollout_fails_closed_when_optional_secret_inventory_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/rollout.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, failures=(r"secrets list --format=value\(name\)",)),
    )
    isolated = DeployScriptHarness(tmp_path / "secret-inventory-failure")

    run = isolated.run(script)

    assert run.returncode != 0
    assert "cannot list optional secret inventory" in run.stderr
    assert not any(
        call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["deploy", "trusted-router"]
        for call in run.calls
    )


def test_warm_primer_caps_below_a_high_service_minimum(
    harness: DeployScriptHarness,
) -> None:
    """The pre-warm regression, pinned (measured 2026-08-25).

    Priming the candidate with the FULL service minimum made the no-traffic
    deploy wait for that many instances to go Ready — us-east4 pins 8, and
    the parallel warm step ran 7m37 instead of ~2m. The revision minimum must
    be the small primer, capped by the service minimum, never the service
    minimum itself.
    """
    run = harness.run(
        "scripts/deploy/rollout.sh",
        extra_env={"TR_CLOUD_RUN_MIN_INSTANCES": "8"},
    )
    assert run.returncode == 0, summarise(run)
    deploy = next(
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:7] == ["deploy", "trusted-router", "--region"]
    )
    assert deploy[deploy.index("--min") + 1] == "8"
    assert deploy[deploy.index("--min-instances") + 1] == "2"


def test_warm_primer_never_exceeds_a_cold_service_minimum(
    harness: DeployScriptHarness,
) -> None:
    """A cold region (service min below the primer) must not pay for primer
    instances its steady state never runs."""
    run = harness.run(
        "scripts/deploy/rollout.sh",
        extra_env={"TR_CLOUD_RUN_MIN_INSTANCES": "1"},
    )
    assert run.returncode == 0, summarise(run)
    deploy = next(
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:7] == ["deploy", "trusted-router", "--region"]
    )
    assert deploy[deploy.index("--min") + 1] == "1"
    assert deploy[deploy.index("--min-instances") + 1] == "1"

    tag_index = next(
        index
        for index, call in enumerate(run.calls)
        if "--update-tags=staged-probe=trusted-router-candidate" in call
    )
    warm_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[0:4] == ["gcloud", "run", "revisions", "describe"]
        and "trusted-router-candidate" in call
    )
    assert tag_index < warm_index
    assert not any("--remove-tags=staged-probe" in call for call in run.calls)


def test_gcp_failed_candidate_warm_restores_zero_traffic_capacity(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run(
        "scripts/deploy/rollout.sh",
        extra_env={"HARNESS_ROLLOUT_CANDIDATE_READY": "0"},
    )
    assert run.returncode != 0

    deploy = next(
        call
        for call in run.calls
        if call[0:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:7] == ["deploy", "trusted-router", "--region"]
    )
    assert deploy[deploy.index("--min-instances") + 1] == "2"
    tag_index = next(
        index
        for index, call in enumerate(run.calls)
        if "--update-tags=staged-probe=trusted-router-candidate" in call
    )
    restore_index = next(
        index
        for index, call in enumerate(run.calls)
        if "--remove-tags=staged-probe" in call
    )
    assert tag_index < restore_index


@pytest.mark.parametrize(
    ("script", "cloud", "mutation_prefix"),
    (
        (
            "scripts/deploy/aws_eu_control_plane.sh",
            "aws",
            ("docker", "buildx", "build"),
        ),
        (
            "scripts/deploy/azure_control_plane.sh",
            "azure",
            ("az", "acr", "build"),
        ),
    ),
)
def test_operator_control_plane_holds_fleet_mutex_through_its_deploy(
    harness: DeployScriptHarness,
    script: str,
    cloud: str,
    mutation_prefix: tuple[str, ...],
) -> None:
    run = harness.run(script, verifier_rc=0)
    assert run.returncode == 0, summarise(run)

    create = next(
        index
        for index, call in enumerate(run.calls)
        if call[0:3] == ["gcloud", "storage", "cp"]
        and "--if-generation-match=0" in call
    )
    mutation = next(
        index
        for index, call in enumerate(run.calls)
        if tuple(call[: len(mutation_prefix)]) == mutation_prefix
    )
    gate = next(
        index
        for index, call in enumerate(run.calls)
        if call == ["verify_cloud_complete.sh", cloud]
    )
    release = next(
        index
        for index, call in enumerate(run.calls)
        if call[0:3] == ["gcloud", "storage", "rm"]
    )

    assert create < mutation < gate < release
    assert f"deploy_mutex.acquired cloud={cloud}" in run.stderr


@pytest.mark.parametrize(
    ("script", "mutation_prefix"),
    (
        ("scripts/deploy/aws_eu_control_plane.sh", ("docker", "buildx", "build")),
        ("scripts/deploy/azure_control_plane.sh", ("az", "acr", "build")),
    ),
)
def test_operator_bake_gate_refusal_aborts_and_releases_mutex(
    harness: DeployScriptHarness,
    script: str,
    mutation_prefix: tuple[str, ...],
) -> None:
    run = harness.run(script, omit_env=("TR_CLOUD_BAKE_OVERRIDE",))

    assert run.returncode != 0
    assert "serving commit: UNKNOWN" in run.stderr
    assert "fleet health: FAIL" in run.stderr
    assert not any(
        tuple(call[: len(mutation_prefix)]) == mutation_prefix for call in run.calls
    )
    assert not any(
        call[:3]
        in (
            ["aws", "apprunner", "create-service"],
            ["aws", "apprunner", "update-service"],
            ["az", "containerapp", "create"],
            ["az", "containerapp", "update"],
        )
        for call in run.calls
    )
    assert any(call[:3] == ["gcloud", "storage", "rm"] for call in run.calls)


def test_harness_git_discovery_stops_at_the_harness_root(tmp_path: Path) -> None:
    """A harness root INSIDE a git checkout behaves exactly like one in /tmp.

    ``pytest --basetemp=.pytest-tmp`` from the repo root puts every mirror
    under the developer's checkout, and the mirror has no ``.git``. Git's
    repository discovery walks upward, so ``cloud_bake_gate.sh``'s
    ``git -C "$repo_root" fetch --quiet origin main`` found the REAL
    repository and fetched from its origin: over SSH it spawned the harness's
    stub ``ssh``, which does not speak the git protocol, and the two
    deadlocked until the subprocess timeout, leaving the fetch orphaned.

    The enclosing checkout here has one commit and an SSH-style origin at an
    unresolvable host, so a regression reproduces that hang rather than a
    quiet pass; the hang is bounded by ``timeout`` and reported with the
    calls recorded up to it.
    """

    def run_git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed git operations in a temp repo
            [GIT, *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    enclosing = tmp_path / "checkout"
    run_git("init", "--initial-branch=main", str(enclosing))
    run_git("-C", str(enclosing), "config", "user.email", "enclosing@example.test")
    run_git("-C", str(enclosing), "config", "user.name", "Enclosing Checkout")
    (enclosing / "README").write_text("the developer's real checkout\n")
    run_git("-C", str(enclosing), "add", "README")
    run_git("-C", str(enclosing), "commit", "-q", "-m", "enclosing checkout commit")
    enclosing_head = run_git("-C", str(enclosing), "rev-parse", "--short", "HEAD")
    run_git(
        "-C", str(enclosing), "remote", "add", "origin",
        "git@harness.invalid:escape/never.git",
    )

    harness = DeployScriptHarness(enclosing / ".pytest-tmp" / "deploy-harness0")

    # Positive control for the setup: from the mirror, with no ceiling, git
    # does discover the enclosing checkout. Without this the assertions below
    # would also pass for a mirror that was never inside a repository.
    discovered = subprocess.run(  # noqa: S603 - fixed git operation in a temp repo
        [GIT, "-C", str(harness.mirror), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    ).stdout.strip()
    assert Path(discovered) == enclosing.resolve()

    try:
        run = harness.run(
            "scripts/deploy/aws_eu_control_plane.sh",
            omit_env=("TR_CLOUD_BAKE_OVERRIDE",),
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        recorded = "".join(
            log.read_text() for log in sorted(harness.root.glob("run-*/argv.log"))
        )
        pytest.fail(
            "git discovery escaped the harness into the enclosing checkout and the"
            f" script hung on its fetch: {exc}\nrecorded calls tail:\n{recorded[-2000:]}"
        )

    # The mirror is not a repository, so HEAD does not resolve ...
    assert "candidate: FAIL unable to resolve HEAD and commit metadata" in run.stderr
    assert f"CANDIDATE {enclosing_head}" not in run.stderr
    # ... the fetch fails at once for the documented reason ...
    assert "fatal: not a git repository" in run.stderr
    assert "git fetch origin main failed" in run.stderr
    # ... no transport was ever spawned ...
    assert not any(call[0] == "ssh" for call in run.calls), run.calls
    assert not any("git-upload-pack" in " ".join(call) for call in run.calls), run.calls
    # ... and the script went on to the gate's verdict, exactly as under /tmp.
    assert run.returncode != 0
    assert "serving commit: UNKNOWN" in run.stderr
    assert "fleet health: FAIL" in run.stderr


@pytest.mark.parametrize(
    ("script", "tag_name", "mutation_prefix"),
    (
        (
            "scripts/deploy/aws_eu_control_plane.sh",
            "TAG",
            ("docker", "buildx", "build"),
        ),
        (
            "scripts/deploy/azure_control_plane.sh",
            "IMAGE_TAG",
            ("az", "acr", "build"),
        ),
    ),
)
def test_operator_deploy_refuses_dirty_tree_after_gate(
    tmp_path: Path,
    script: str,
    tag_name: str,
    mutation_prefix: tuple[str, ...],
) -> None:
    isolated = DeployScriptHarness(tmp_path / f"dirty-{tag_name.lower()}")
    short_head = _initialize_bake_harness_repo(isolated)
    dirty = isolated.mirror / "dirty-after-bake-gate.txt"
    dirty.write_text("unvalidated\n", encoding="utf-8")

    run = isolated.run(
        script,
        omit_env=("TR_CLOUD_BAKE_OVERRIDE",),
        extra_env={
            "HARNESS_CLOUD_BAKE_SHA": short_head,
            "TR_CLOUD_BAKE_HOURS": "1",
            tag_name: short_head,
        },
    )

    assert run.returncode != 0
    assert "the gate validated HEAD; a dirty tree deploys unvalidated code" in run.stderr
    assert not any(
        tuple(call[: len(mutation_prefix)]) == mutation_prefix for call in run.calls
    )
    assert any(call[:3] == ["gcloud", "storage", "rm"] for call in run.calls)


@pytest.mark.parametrize(
    ("script", "tag_name", "mutation_prefix"),
    (
        (
            "scripts/deploy/aws_eu_control_plane.sh",
            "TAG",
            ("docker", "buildx", "build"),
        ),
        (
            "scripts/deploy/azure_control_plane.sh",
            "IMAGE_TAG",
            ("az", "acr", "build"),
        ),
    ),
)
def test_operator_deploy_refuses_tag_that_does_not_match_validated_head(
    tmp_path: Path,
    script: str,
    tag_name: str,
    mutation_prefix: tuple[str, ...],
) -> None:
    isolated = DeployScriptHarness(tmp_path / f"tag-mismatch-{tag_name.lower()}")
    short_head = _initialize_bake_harness_repo(isolated)

    run = isolated.run(
        script,
        omit_env=("TR_CLOUD_BAKE_OVERRIDE",),
        extra_env={
            "HARNESS_CLOUD_BAKE_SHA": short_head,
            "TR_CLOUD_BAKE_HOURS": "1",
            tag_name: "deadbee",
        },
    )

    assert run.returncode != 0
    assert "does not match the validated short HEAD" in run.stderr
    assert not any(
        tuple(call[: len(mutation_prefix)]) == mutation_prefix for call in run.calls
    )
    assert any(call[:3] == ["gcloud", "storage", "rm"] for call in run.calls)


@pytest.mark.parametrize(
    ("script", "tag_name", "message"),
    (
        (
            "scripts/deploy/aws_eu_control_plane.sh",
            "TAG",
            "not a git checkout: set TAG=<short-sha>",
        ),
        (
            "scripts/deploy/azure_control_plane.sh",
            "IMAGE_TAG",
            "not a git checkout: set IMAGE_TAG=<short-sha>",
        ),
    ),
)
def test_operator_deploy_tag_fallback_explains_non_git_checkout(
    harness: DeployScriptHarness,
    script: str,
    tag_name: str,
    message: str,
) -> None:
    run = harness.run(script, omit_env=(tag_name,))

    assert run.returncode != 0
    assert message in run.stderr


def test_azure_canary_app_cannot_target_production_side_door(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "azure-canary-production-guard")

    run = isolated.run(
        "scripts/deploy/azure_canary_app.sh",
        extra_env={"RG": "tr-azure", "APP": "tr-azure-vnet"},
    )

    assert run.returncode != 0
    assert "use scripts/deploy/azure_control_plane.sh" in run.stderr
    assert not any(call[0] == "az" for call in run.calls)


def test_regional_quota_reconciler_preserves_a_paused_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_regional_quota_reconciler(tmp_path, monkeypatch, state="PAUSED")

    assert run.returncode == 0, summarise(run)
    assert not _gcloud_calls(run, "run", "jobs", "execute")
    assert len(_gcloud_calls(run, "scheduler", "jobs", "update")) == 1
    assert not _gcloud_calls(run, "scheduler", "jobs", "create")
    assert not _gcloud_calls(run, "scheduler", "jobs", "resume")
    assert "preserving intentional regional quota reconciler pause" in run.stderr


def test_regional_quota_reconciler_verifies_updates_and_resumes_enabled_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_regional_quota_reconciler(tmp_path, monkeypatch, state="ENABLED")

    assert run.returncode == 0, summarise(run)
    assert len(_gcloud_calls(run, "run", "jobs", "execute")) == 1
    assert len(_gcloud_calls(run, "scheduler", "jobs", "update")) == 1
    assert not _gcloud_calls(run, "scheduler", "jobs", "create")
    assert len(_gcloud_calls(run, "scheduler", "jobs", "resume")) == 1
    assert len(_gcloud_calls(run, "run", "jobs", "create")) == 1
    assert not _gcloud_calls(run, "run", "jobs", "deploy")
    update = _gcloud_calls(run, "scheduler", "jobs", "update")[0]
    assert "--max-retry-attempts=3" in update
    assert "--max-retry-duration=45s" in update
    assert "--min-backoff=5s" in update
    assert "--max-backoff=15s" in update
    assert "--max-doublings=1" in update


def test_regional_quota_reconciler_updates_existing_version_without_get_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_regional_quota_reconciler(
        tmp_path,
        monkeypatch,
        state="ENABLED",
        versioned_job_exists=True,
    )

    assert run.returncode == 0, summarise(run)
    assert len(_gcloud_calls(run, "run", "jobs", "list")) >= 1
    assert len(_gcloud_calls(run, "run", "jobs", "update")) == 1
    assert not _gcloud_calls(run, "run", "jobs", "create")
    assert not _gcloud_calls(run, "run", "jobs", "deploy")


def test_regional_quota_reconciler_deletes_stale_jobs_without_polling_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "trusted-router-regional-quota-reconciler-existing"
    previous = "trusted-router-regional-quota-reconciler-previous"
    stale = "trusted-router-regional-quota-reconciler-stale"
    run = _run_regional_quota_reconciler(
        tmp_path,
        monkeypatch,
        state="ENABLED",
        versioned_job_exists=True,
        stale_job_names=f"{current}\n{previous}\n{stale}",
    )

    assert run.returncode == 0, summarise(run)
    deletes = _gcloud_calls(run, "run", "jobs", "delete")
    assert len(deletes) == 1
    assert stale in deletes[0]
    assert "--async" in deletes[0]
    assert previous not in deletes[0]


def test_regional_quota_reconciler_creates_only_when_scheduler_is_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_regional_quota_reconciler(
        tmp_path,
        monkeypatch,
        describe_rc=1,
        describe_stderr=("ERROR: (gcloud.scheduler.jobs.describe) NOT_FOUND: Job not found.\n"),
    )

    assert run.returncode == 0, summarise(run)
    assert len(_gcloud_calls(run, "scheduler", "jobs", "describe")) == 1
    assert len(_gcloud_calls(run, "run", "jobs", "execute")) == 1
    assert len(_gcloud_calls(run, "scheduler", "jobs", "create")) == 1
    assert not _gcloud_calls(run, "scheduler", "jobs", "update")
    assert not _gcloud_calls(run, "scheduler", "jobs", "resume")


def test_regional_quota_reconciler_fails_closed_when_scheduler_state_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = "ERROR: (gcloud.scheduler.jobs.describe) UNAVAILABLE: credential blip\n"
    run = _run_regional_quota_reconciler(
        tmp_path,
        monkeypatch,
        describe_rc=1,
        describe_stderr=error,
    )

    assert run.returncode != 0
    assert "cannot determine state of Cloud Scheduler job" in run.stderr
    assert error.strip() in run.stderr
    assert len(_gcloud_calls(run, "scheduler", "jobs", "describe")) == 1
    assert not _gcloud_calls(run, "run", "jobs", "execute")
    assert not _gcloud_calls(run, "scheduler", "jobs", "create")
    assert not _gcloud_calls(run, "scheduler", "jobs", "update")
    assert not _gcloud_calls(run, "scheduler", "jobs", "resume")


def test_every_deploy_script_parses_in_this_machine_s_shell() -> None:
    """`bash -n` over every deploy script, with whatever bash is here.

    Cheap, and it caught a real one: `aws_eu_clickhouse_drain_install.sh` built
    its next-steps text as `"$(cat <<'NEXT' ... )"`, and a heredoc nested inside
    a command substitution is a syntax error in bash 3.2 — /bin/bash on every
    macOS — the moment the BODY contains an apostrophe. The body said "step 9's
    output". So on a Mac the whole file failed to parse and did nothing at all,
    gate included, while CI on Linux bash 5 parsed it happily.

    What this proves is therefore shell-specific, and saying so is the point: on
    a modern bash it proves the scripts are well-formed there, and on an old one
    it proves they are runnable by the operator sitting in front of it. The
    class of defect only shows up on the second kind of machine, which is why
    the check runs the LOCAL shell rather than a pinned one.
    """
    for script in sorted((ROOT / "scripts").rglob("*.sh")):
        result = subprocess.run(  # noqa: S603
            ["bash", "-n", str(script)],  # noqa: S607
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{script.relative_to(ROOT)} does not parse under "
            f"{subprocess.run(['bash', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}"  # noqa: E501,S603,S607
            f":\n{result.stderr}"
        )


def test_the_registry_actually_binds_something() -> None:
    """If nothing is proven by execution, this file is decorative."""
    assert PROVEN, "no deploy script is proven by execution — the mechanism is disconnected"


def test_aws_observer_executes_bounded_capacity_tcp_health_and_waf_before_schedule(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run("scripts/deploy/aws_eu_control_plane.sh", verifier_rc=0)
    assert run.returncode == 0, summarise(run)

    describe_scaling = [
        call
        for call in run.calls
        if call[:3] == ["aws", "apprunner", "describe-auto-scaling-configuration"]
    ]
    assert len(describe_scaling) == 1
    postcondition_queries = [
        call
        for call in run.calls
        if call[:3] == ["aws", "apprunner", "describe-service"]
        and any(
            field in " ".join(call)
            for field in (
                "AutoScalingConfigurationSummary",
                "HealthCheckConfiguration.Protocol",
            )
        )
    ]
    assert len(postcondition_queries) == 2
    service_updates = [
        call for call in run.calls if call[:3] == ["aws", "apprunner", "update-service"]
    ]
    assert len(service_updates) == 1
    service_config = service_updates[0][service_updates[0].index("--source-configuration") + 1]
    assert '"TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS": "0"' in service_config
    assert '"TR_SYNTHETIC_RUN_DEADLINE_SECONDS": "240"' in service_config
    assert '"TR_REMEDIATOR_IN_PROCESS_ENABLED": "false"' in service_config
    observer_secret_reads = [
        call
        for call in run.calls
        if call[:3] == ["aws", "secretsmanager", "get-secret-value"]
        and "quill/trustedrouter-observer-internal-token" in call
    ]
    assert len(observer_secret_reads) == 1
    legacy_secret_reads = [
        call
        for call in run.calls
        if call[:3] == ["aws", "secretsmanager", "get-secret-value"]
        and "quill/trustedrouter-internal-gateway-token" in call
    ]
    assert len(legacy_secret_reads) == 1
    assert not any(
        call[:3] == ["aws", "secretsmanager", "get-secret-value"]
        and "SecretString" not in " ".join(call)
        for call in run.calls
    )

    waf_attach_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "wafv2", "associate-web-acl"]
    )
    scheduler_rules = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "events", "put-rule"]
    ]
    assert len(scheduler_rules) == 1
    scheduler_index, scheduler_rule = scheduler_rules[0]
    assert scheduler_rule[scheduler_rule.index("--schedule-expression") + 1] == ("rate(2 minutes)")
    scheduler_targets = [call for call in run.calls if call[:3] == ["aws", "events", "put-targets"]]
    assert len(scheduler_targets) == 1
    targets = json.loads(scheduler_targets[0][scheduler_targets[0].index("--targets") + 1])
    assert len(targets) == 1
    assert targets[0]["DeadLetterConfig"] == {
        "Arn": "arn:aws:sqs:eu-west-3:330422590279:tr-eu-synthetic-dlq"
    }
    # The Input is owned by infra/aws_synthetic_monitoring.tf. This script has
    # no opinion about it: it reads the live value and passes the same bytes
    # back, after the rule exists and before it rewrites the target.
    input_reads = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "events", "list-targets-by-rule"]
    ]
    assert len(input_reads) == 1
    assert scheduler_index < input_reads[0][0] < run.calls.index(scheduler_targets[0])
    assert targets[0]["Input"] == '{"detach":true,"monitor_region":"eu-west-3","rotation_count":8}'
    (queue_policy_call,) = [
        call for call in run.calls if call[:3] == ["aws", "sqs", "set-queue-attributes"]
    ]
    queue_attributes = json.loads(
        queue_policy_call[queue_policy_call.index("--attributes") + 1]
    )
    queue_policy = json.loads(queue_attributes["Policy"])
    assert queue_policy["Statement"][0]["Principal"] == {
        "Service": "events.amazonaws.com"
    }
    assert queue_policy["Statement"][0]["Condition"] == {
        "ArnEquals": {
            "aws:SourceArn": "arn:aws:events:eu-west-3:330422590279:rule/tr-eu-synthetic-1min"
        }
    }
    dlq_role_policies = [
        call
        for call in run.calls
        if call[:3] == ["aws", "iam", "put-role-policy"]
        and "tr-eu-synthetic-dlq-send" in call
    ]
    assert len(dlq_role_policies) == 1
    role_policy = json.loads(
        dlq_role_policies[0][dlq_role_policies[0].index("--policy-document") + 1]
    )
    assert role_policy["Statement"][0]["Action"] == "sqs:SendMessage"
    assert role_policy["Statement"][0]["Resource"] == (
        "arn:aws:sqs:eu-west-3:330422590279:tr-eu-synthetic-dlq"
    )
    alarms = [
        call
        for call in run.calls
        if call[:3] == ["aws", "cloudwatch", "put-metric-alarm"]
    ]
    assert {call[call.index("--alarm-name") + 1] for call in alarms} == {
        "tr-eu-synthetic-failed-invocations",
        "tr-eu-synthetic-dlq-messages-visible",
    }
    failed_alarm = next(
        call for call in alarms if "tr-eu-synthetic-failed-invocations" in call
    )
    assert failed_alarm[failed_alarm.index("--metric-name") + 1] == "FailedInvocations"
    assert failed_alarm[failed_alarm.index("--evaluation-periods") + 1] == "3"
    assert failed_alarm[failed_alarm.index("--period") + 1] == "300"
    dlq_alarm = next(call for call in alarms if "tr-eu-synthetic-dlq-messages-visible" in call)
    assert (
        dlq_alarm[dlq_alarm.index("--metric-name") + 1]
        == "ApproximateNumberOfMessagesVisible"
    )
    assert all("--actions-enabled" in call for call in alarms)
    assert all(
        call[call.index("--alarm-actions") + 1]
        == "arn:aws:sns:eu-west-3:330422590279:tr-eu-synthetic-alarms"
        for call in alarms
    )
    assert waf_attach_index < scheduler_index


@pytest.mark.parametrize(
    "live_input",
    [
        # What is live today: the operator's state, mirrored by Terraform.
        '{"detach":true,"monitor_region":"eu-west-3","rotation_count":8}',
        # What will be live after a reviewed Terraform change re-enables the
        # pass. The script must carry THAT forward too: it is not the one that
        # decides, in either direction. Deliberately odd spacing and key order,
        # because a re-serialised value is a diff in the next Terraform plan.
        '{"rotation_count": 8, "run_remediator":true, "monitor_region":"eu-west-3","detach":true}',
        # Trailing newlines are what a text-mode read through $(...) loses.
        '{"detach":true,"monitor_region":"eu-west-3","rotation_count":8}\n\n',
    ],
)
def test_aws_observer_carries_the_terraform_owned_schedule_input_forward_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_input: str,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"events list-targets-by-rule", live_schedule_targets(live_input)),
        *(r for r in fixture.responses if "list-targets-by-rule" not in r[0]),
    )
    monkeypatch.setitem(SCRIPT_FIXTURES, script, replace(fixture, responses=responses))
    isolated = DeployScriptHarness(tmp_path / "aws-live-input")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    (put_targets,) = [c for c in run.calls if c[:3] == ["aws", "events", "put-targets"]]
    (target,) = json.loads(put_targets[put_targets.index("--targets") + 1])
    assert target["Input"] == live_input
    # The script itself names no remediator flag any more.
    source = (ROOT / script).read_text(encoding="utf-8")
    assert '"run_remediator"' not in source


@pytest.mark.parametrize(
    ("live_targets", "expected_error"),
    [
        # The rule has no targets at all (the Terraform root was never applied).
        (live_schedule_targets(), "apply the Terraform root"),
        # It has a target, but not the observer's.
        (live_schedule_targets("{}", target_id="something-else"), "apply the Terraform root"),
        # The observer's target has no Input (an InputTransformer, say).
        (live_schedule_targets(None), "apply the Terraform root"),
        # Two targets claim the id: there is no single value to carry forward.
        (live_schedule_targets("{}", "{}"), "apply the Terraform root"),
        # Not something the observer could parse: refuse to re-assert it.
        (live_schedule_targets("[1, 2]"), "not a JSON object"),
        (live_schedule_targets("run_remediator=true"), "not a JSON object"),
    ],
)
def test_aws_observer_never_invents_a_schedule_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_targets: str,
    expected_error: str,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"events list-targets-by-rule", live_targets),
        *(r for r in fixture.responses if "list-targets-by-rule" not in r[0]),
    )
    monkeypatch.setitem(SCRIPT_FIXTURES, script, replace(fixture, responses=responses))
    isolated = DeployScriptHarness(tmp_path / "aws-no-input")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert expected_error in run.stderr
    assert not any(c[:3] == ["aws", "events", "put-targets"] for c in run.calls)


def test_aws_observer_initial_create_reaches_running_postconditions_waf_and_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    service_arn = "arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu/harness-service-id"
    responses = (
        (r"apprunner list-services", ""),
        (r"apprunner create-service", service_arn),
        *(
            response
            for response in fixture.responses
            if "apprunner list-services" not in response[0]
            and "apprunner create-service" not in response[0]
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "aws-initial-create")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    creates = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "apprunner", "create-service"]
    ]
    assert len(creates) == 1
    create_index, create = creates[0]
    assert not any(call[:3] == ["aws", "apprunner", "update-service"] for call in run.calls)
    assert create[create.index("--auto-scaling-configuration-arn") + 1].startswith(
        "arn:aws:apprunner:eu-west-3:330422590279:autoscalingconfiguration/tr-eu-observer-bounded/"
    )
    assert create[create.index("--health-check-configuration") + 1].startswith("Protocol=TCP")

    running_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "apprunner", "describe-service"]
        and "Service.Status" in " ".join(call)
    )
    scaling_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "apprunner", "describe-service"]
        and "AutoScalingConfigurationSummary" in " ".join(call)
    )
    health_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "apprunner", "describe-service"]
        and "HealthCheckConfiguration.Protocol" in " ".join(call)
    )
    waf_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "wafv2", "associate-web-acl"]
    )
    schedule_index = next(
        index for index, call in enumerate(run.calls) if call[:3] == ["aws", "events", "put-rule"]
    )
    assert create_index < running_index < scaling_index < health_index < waf_index
    assert waf_index < schedule_index
    assert run.gate_ran_for("aws")


@pytest.mark.parametrize(
    ("reported_status", "expected_error"),
    [
        ("OPERATION_IN_PROGRESS", "did not reach RUNNING"),
        ("UPDATE_FAILED", "FAILED: UPDATE_FAILED"),
    ],
)
def test_aws_observer_never_secures_or_schedules_a_non_running_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_status: str,
    expected_error: str,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"apprunner describe-service.*Service\.Status", reported_status),
        *(response for response in fixture.responses if "Service\\.Status" not in response[0]),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "aws-not-running")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert expected_error in run.stderr
    assert not any(call[:3] == ["aws", "wafv2", "associate-web-acl"] for call in run.calls)
    assert not any(call[:3] == ["aws", "events", "put-rule"] for call in run.calls)


def test_aws_observer_rejects_reused_billing_gateway_credential_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"get-secret-value.*trustedrouter-observer-internal-token", "same-token"),
        (r"get-secret-value.*trustedrouter-internal-gateway-token", "same-token"),
        *(
            response
            for response in fixture.responses
            if "secretsmanager get-secret-value" not in response[0]
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "aws-observer-token-reuse")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "must differ from the billing gateway token" in run.stderr
    assert not any(call[0] == "docker" for call in run.calls)
    assert not any(
        call[:3] in (["aws", "apprunner", "create-service"], ["aws", "apprunner", "update-service"])
        for call in run.calls
    )
    assert not run.verifier_calls


def test_aws_observer_fails_closed_when_legacy_credential_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            responses=tuple(
                response
                for response in fixture.responses
                if "trustedrouter-internal-gateway-token" not in response[0]
            ),
            failures=(
                r"^aws secretsmanager get-secret-value.*"
                r"trustedrouter-internal-gateway-token",
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "aws-legacy-token-inspection-error")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "could not inspect the legacy billing gateway token" in run.stderr
    assert not any(call[0] == "docker" for call in run.calls)
    assert not any(
        call[:3] in (["aws", "apprunner", "create-service"], ["aws", "apprunner", "update-service"])
        for call in run.calls
    )
    assert not run.verifier_calls


@pytest.mark.parametrize(
    ("fixture_pattern", "drift_value", "query_fragment"),
    [
        (
            r"apprunner describe-service.*AutoScalingConfigurationSummary",
            "arn:aws:apprunner:eu-west-3:330422590279:autoscalingconfiguration/unbounded/1/drift",
            "AutoScalingConfigurationSummary",
        ),
        (
            r"apprunner describe-service.*HealthCheckConfiguration",
            "HTTP",
            "HealthCheckConfiguration.Protocol",
        ),
    ],
)
def test_aws_observer_stops_before_waf_and_schedule_on_each_live_postcondition_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_pattern: str,
    drift_value: str,
    query_fragment: str,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (fixture_pattern, drift_value),
        *(response for response in fixture.responses if query_fragment not in response[0]),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / query_fragment.replace(".", "-"))

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "expected" in run.stderr
    assert not any(call[:3] == ["aws", "wafv2", "associate-web-acl"] for call in run.calls)
    assert not any(call[:3] == ["aws", "events", "put-rule"] for call in run.calls)
    assert not run.verifier_calls


@pytest.mark.parametrize(
    ("script", "expected_max"),
    [
        ("scripts/deploy/azure_control_plane.sh", "1"),
        ("scripts/deploy/azure_canary_app.sh", "2"),
    ],
)
def test_azure_observer_executes_single_revision_bounded_http_scaling(
    tmp_path: Path,
    script: str,
    expected_max: str,
) -> None:
    isolated = DeployScriptHarness(tmp_path / f"azure-{expected_max}")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    mutations = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:3]
        in (
            ["az", "containerapp", "update"],
            ["az", "containerapp", "create"],
        )
    ]
    assert len(mutations) == 1
    mutation_index, mutation = mutations[0]
    assert mutation[mutation.index("--min-replicas") + 1] == "1"
    assert mutation[mutation.index("--max-replicas") + 1] == expected_max
    assert mutation[mutation.index("--scale-rule-http-concurrency") + 1] == "10"
    mutation_text = " ".join(mutation)
    if script.endswith("azure_canary_app.sh"):
        assert "TR_SERVICE_SURFACE=public" in mutation_text
        assert "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret" in mutation_text
        assert "TR_INTERNAL_GATEWAY_TOKEN" not in mutation_text
        assert "TR_SYNTHETIC_MONITOR_API_KEY" not in mutation_text
        assert "TR_FEDERATION_" not in mutation_text
        assert "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=false" in mutation_text
        assert "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=false" in mutation_text
        remove_index = mutation.index("--remove-env-vars")
        retired = set(mutation[remove_index + 1 : mutation.index("--min-replicas")])
        assert {
            "TR_GOOGLE_CLIENT_ID",
            "TR_GOOGLE_CLIENT_SECRET",
            "TR_GOOGLE_OAUTH_REDIRECT_URL",
            "TR_GOOGLE_ALIAS_CREDENTIALS_JSON",
            "TR_GITHUB_CLIENT_ID",
            "TR_GITHUB_CLIENT_SECRET",
            "TR_GITHUB_OAUTH_REDIRECT_URL",
            "TR_GITHUB_ALIAS_CREDENTIALS_JSON",
        } == retired
        settings = _settings_from_containerapp_mutation(mutation)
        assert settings.service_surface == "public"
        assert settings.google_oauth_login_available is False
        assert settings.github_oauth_login_available is False
        assert settings.google_client_secret is None
        assert settings.github_client_secret is None
    else:
        assert "TR_SERVICE_SURFACE=observer" in mutation_text
        assert "TR_OBSERVER_INTERNAL_TOKEN=secretref:observer-token" in mutation_text
        assert "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=120" in mutation_text
        assert "TR_REMEDIATOR_IN_PROCESS_ENABLED=true" in mutation_text
        assert "TR_REMEDIATOR_MODE=observe" in mutation_text
        remove_index = mutation.index("--remove-env-vars")
        set_index = mutation.index("--set-env-vars")
        configured_text = " ".join(mutation[set_index + 1 : remove_index])
        assert "TR_INTERNAL_GATEWAY_TOKEN" not in configured_text
        assert "TR_FEDERATION_" not in configured_text
        retired = set(mutation[remove_index + 1 : mutation.index("--min-replicas")])
        assert {
            "TR_INTERNAL_GATEWAY_TOKEN",
            "TR_FEDERATION_HOME_TOKEN",
            "TR_FEDERATION_SETTLEMENT_HOME_TOKEN",
        } <= retired

    set_mode_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:4] == ["az", "containerapp", "revision", "set-mode"]
    )
    assert mutation_index < set_mode_index
    assert run.calls[set_mode_index][run.calls[set_mode_index].index("--mode") + 1] == "single"
    postcondition_indices = [
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["az", "containerapp", "show"]
        and any(
            field in " ".join(call)
            for field in (
                "activeRevisionsMode",
                "template.scale.maxReplicas",
                "concurrentRequests",
            )
        )
    ]
    assert len(postcondition_indices) == 3
    assert all(index > set_mode_index for index in postcondition_indices)


@pytest.mark.parametrize(
    ("script", "rg_name", "app_name", "password_file", "expected_max"),
    [
        (
            "scripts/deploy/azure_control_plane.sh",
            # Resource group and app name are no longer the same string. The
            # container app is tr-azure-vnet inside resource group tr-azure;
            # the pre-vnet name has not existed since the VNet migration, and
            # this fixture asserting `-g tr-azure -n tr-azure` is why the
            # script could target a nonexistent app with every deploy test
            # still green -- the harness faked the name the script asked for
            # rather than the one Azure has.
            "tr-azure",
            "tr-azure-vnet",
            ".config/tr-azure/pgpw",
            "1",
        ),
        (
            "scripts/deploy/azure_canary_app.sh",
            "tr-canary",
            "tr-canary",
            ".config/tr-canary/pgpw",
            "2",
        ),
    ],
)
def test_azure_observer_initial_create_is_bounded_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    rg_name: str,
    app_name: str,
    password_file: str,
    expected_max: str,
) -> None:
    fixture = SCRIPT_FIXTURES[script]
    home_files = {**fixture.home_files, password_file: "harness-db-password\n"}
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            home_files=home_files,
            responses=(
                # A genuine first run: the resource group is empty. The
                # preflight distinguishes "nothing deployed yet, create it"
                # from "something else is already the deployment here" by
                # listing the group, and the harness answers unknown commands
                # with "stub-output", which would read as a phantom app.
                (r"^az containerapp list -g \S+ --query", ""),
                *fixture.responses,
            ),
            failures=(rf"^az containerapp show -g {rg_name} -n {app_name}$",),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / f"azure-create-{expected_max}")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    creates = [call for call in run.calls if call[:3] == ["az", "containerapp", "create"]]
    assert len(creates) == 1
    assert not any(call[:3] == ["az", "containerapp", "update"] for call in run.calls)
    create = creates[0]
    create_index = run.calls.index(create)
    assert create[create.index("--min-replicas") + 1] == "1"
    assert create[create.index("--max-replicas") + 1] == expected_max
    assert create[create.index("--scale-rule-http-concurrency") + 1] == "10"
    if script.endswith("azure_canary_app.sh"):
        create_text = " ".join(create)
        assert "TR_SERVICE_SURFACE=public" in create_text
        assert "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret" in create_text
        attribution_secrets = [
            argument for argument in create if argument.startswith("attribution-cookie-secret=")
        ]
        assert len(attribution_secrets) == 1
        assert len(attribution_secrets[0].partition("=")[2]) == 64
        assert "TR_INTERNAL_GATEWAY_TOKEN" not in create_text
        assert "TR_SYNTHETIC_MONITOR_API_KEY" not in create_text
        assert "TR_FEDERATION_" not in create_text
        assert "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=false" in create_text
        assert "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=false" in create_text
        settings = _settings_from_containerapp_mutation(create)
        assert settings.service_surface == "public"
        assert settings.google_oauth_login_available is False
        assert settings.github_oauth_login_available is False
    else:
        create_text = " ".join(create)
        assert "TR_SERVICE_SURFACE=observer" in create_text
        assert "TR_OBSERVER_INTERNAL_TOKEN=secretref:observer-token" in create_text
        assert any(argument.startswith("observer-token=") for argument in create)
        assert "TR_INTERNAL_GATEWAY_TOKEN" not in create_text
        assert "TR_FEDERATION_" not in create_text
    set_mode_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:4] == ["az", "containerapp", "revision", "set-mode"]
    )
    postcondition_indices = [
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["az", "containerapp", "show"]
        and any(
            field in " ".join(call)
            for field in (
                "activeRevisionsMode",
                "template.scale.maxReplicas",
                "concurrentRequests",
            )
        )
    ]
    assert len(postcondition_indices) == 3
    assert create_index < set_mode_index
    assert all(index > set_mode_index for index in postcondition_indices)
    if script.endswith("azure_control_plane.sh"):
        assert run.gate_ran_for("azure")


def test_azure_observer_refuses_to_drop_its_only_synthetic_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            env={**fixture.env, "SYNTHETIC_INTERVAL_SECONDS": "0"},
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-no-synthetic-owner")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "no external synthetic owner" in run.stderr
    assert not run.calls
    assert not run.verifier_calls


def _azure_without_analytics_discovery(fixture: ScriptFixture) -> ScriptFixture:
    discovery_fragments = (
        "vm list-ip-addresses",
        "keyvault secret show.*tr-azure-clickhouse-password",
        "identity show.*tr-azure-clickhouse-identity",
    )
    return replace(
        fixture,
        responses=(
            (r"vm list-ip-addresses.*tr-azure-clickhouse-1", ""),
            (
                r"keyvault secret show.*tr-azure-clickhouse-password.*--query id",
                "",
            ),
            (
                r"identity show.*tr-azure-clickhouse-identity.*--query id",
                "",
            ),
            *(
                response
                for response in fixture.responses
                if not any(fragment in response[0] for fragment in discovery_fragments)
            ),
        ),
    )


def _azure_control_plane_update(run: HarnessRun) -> list[str]:
    updates = [call for call in run.calls if call[:3] == ["az", "containerapp", "update"]]
    assert len(updates) == 1, summarise(run)
    return updates[0]


def _azure_update_env(update: list[str]) -> dict[str, str]:
    start = update.index("--set-env-vars") + 1
    end = update.index("--remove-env-vars")
    return {
        argument.partition("=")[0]: argument.partition("=")[2]
        for argument in update[start:end]
        if "=" in argument
    }


def test_azure_observer_emits_the_discovered_operational_analytics_env(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "azure-analytics-discovered")

    run = isolated.run("scripts/deploy/azure_control_plane.sh", verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    update = _azure_control_plane_update(run)
    env = _azure_update_env(update)
    assert {
        name: env[name]
        for name in (
            "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD",
        )
    } == {
        "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "true",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL": "http://10.61.3.4:8123",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "default",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "default",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD": "secretref:clickhouse-password",
    }

    identity_assign = next(
        call
        for call in run.calls
        if call[:4] == ["az", "containerapp", "identity", "assign"]
    )
    secret_set = next(
        call for call in run.calls if call[:4] == ["az", "containerapp", "secret", "set"]
    )
    assert run.calls.index(identity_assign) < run.calls.index(secret_set) < run.calls.index(update)
    clickhouse_reference = next(
        argument for argument in secret_set if argument.startswith("clickhouse-password=")
    )
    assert clickhouse_reference == (
        "clickhouse-password=keyvaultref:https://trquillkv.vault.azure.net/"
        "secrets/tr-azure-clickhouse-password/harness-version,identityref:/subscriptions/"
        "harness/resourceGroups/tr-azure/providers/Microsoft.ManagedIdentity/"
        "userAssignedIdentities/tr-azure-clickhouse-identity"
    )


def test_azure_observer_refuses_missing_analytics_discovery_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        _azure_without_analytics_discovery(fixture),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-analytics-undiscoverable")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "expects_outbox=True" in run.stderr
    assert "tr-azure-clickhouse-1" in run.stderr
    assert "trquillkv" in run.stderr
    assert "az vm list-ip-addresses -g tr-azure -n tr-azure-clickhouse-1" in run.stderr
    assert "az keyvault secret show --vault-name trquillkv" in run.stderr
    mutating_prefixes = (
        ["az", "acr", "build"],
        ["az", "acr", "import"],
        ["az", "containerapp", "identity", "assign"],
        ["az", "containerapp", "secret", "set"],
        ["az", "containerapp", "update"],
        ["az", "containerapp", "create"],
        ["az", "containerapp", "revision", "set-mode"],
        ["az", "postgres", "flexible-server", "firewall-rule"],
        ["gcloud", "dns"],
        ["psql"],
    )
    assert not any(
        call[: len(prefix)] == prefix for call in run.calls for prefix in mutating_prefixes
    ), summarise(run)
    assert not run.verifier_calls


def test_azure_observer_missing_analytics_requires_an_explicit_operator_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        _azure_without_analytics_discovery(fixture),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-analytics-explicit-opt-out")

    run = isolated.run(
        script,
        verifier_rc=0,
        extra_env={"AZURE_ANALYTICS_OPERATOR_DECISION": "disable"},
    )

    assert run.returncode == 0, summarise(run)
    update = _azure_control_plane_update(run)
    env = _azure_update_env(update)
    assert env["TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"] == "false"
    assert env["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL"] == ""
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD" not in env
    removed = update[
        update.index("--remove-env-vars") + 1 : update.index("--min-replicas")
    ]
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD" in removed
    assert "operator explicitly decided to disable Azure analytics" in run.stderr


def test_azure_observer_keeps_all_retired_private_env_names_absent(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "azure-retired-env-absent")

    run = isolated.run("scripts/deploy/azure_control_plane.sh", verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    update = _azure_control_plane_update(run)
    env = _azure_update_env(update)
    retired = {
        "TR_INTERNAL_GATEWAY_TOKEN",
        "TR_FEDERATION_HOME_TOKEN",
        "TR_FEDERATION_SETTLEMENT_HOME_TOKEN",
        "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED",
        "TR_FEDERATION_HOME_BASE_URL",
    }
    assert retired.isdisjoint(env)
    removed = set(
        update[update.index("--remove-env-vars") + 1 : update.index("--min-replicas")]
    )
    assert retired <= removed


def test_azure_clickhouse_password_never_enters_argv_or_logs(tmp_path: Path) -> None:
    password = "harness-clickhouse-password-S3CR3T"  # noqa: S105 - leak canary
    isolated = DeployScriptHarness(tmp_path / "azure-analytics-password-isolation")

    run = isolated.run(
        "scripts/deploy/azure_control_plane.sh",
        verifier_rc=0,
        extra_env={"CLICKHOUSE_PASSWORD": password},
    )

    assert run.returncode == 0, summarise(run)
    observable = "\n".join((run.stdout, run.stderr, *("\t".join(call) for call in run.calls)))
    assert password not in observable
    key_vault_calls = [
        call for call in run.calls if call[:4] == ["az", "keyvault", "secret", "show"]
    ]
    assert len(key_vault_calls) == 1
    assert key_vault_calls[0][key_vault_calls[0].index("--query") + 1] == "id"


def test_azure_canary_persists_a_missing_dedicated_attribution_secret_before_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_canary_app.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"secret-name attribution-cookie-secret", ""),
        *fixture.responses,
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-canary-attribution-migration")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    secret_set_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:4] == ["az", "containerapp", "secret", "set"]
        and any(argument.startswith("attribution-cookie-secret=") for argument in call)
    )
    update_index = next(
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["az", "containerapp", "update"]
    )
    assert secret_set_index < update_index
    secret_set = run.calls[secret_set_index]
    stored = next(
        argument.partition("=")[2]
        for argument in secret_set
        if argument.startswith("attribution-cookie-secret=")
    )
    assert len(stored) == 64
    update_text = " ".join(run.calls[update_index])
    assert "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret" in update_text
    assert "TR_INTERNAL_GATEWAY_TOKEN" not in update_text
    assert "TR_FEDERATION_" not in update_text


def test_azure_canary_fails_closed_if_a_retired_oauth_credential_survives_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_canary_app.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            responses=(
                (
                    r"containers\[0\]\.env\[\]\.name",
                    "TR_SERVICE_SURFACE\tTR_GOOGLE_CLIENT_SECRET\tTR_RELEASE",
                ),
                *fixture.responses,
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-canary-stale-oauth")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "public canary retains forbidden OAuth env TR_GOOGLE_CLIENT_SECRET" in run.stderr
    update = next(call for call in run.calls if call[:3] == ["az", "containerapp", "update"])
    remove_index = update.index("--remove-env-vars")
    assert "TR_GOOGLE_CLIENT_SECRET" in update[remove_index + 1 : update.index("--min-replicas")]
    assert not any(call[:4] == ["az", "containerapp", "revision", "set-mode"] for call in run.calls)
    assert not any(
        call[:3] == ["az", "containerapp", "show"] and "ingress.fqdn" in " ".join(call)
        for call in run.calls
    )


@pytest.mark.parametrize(
    "provider",
    ["GOOGLE", "GITHUB"],
)
def test_azure_canary_fails_closed_if_an_oauth_capability_flag_drifts_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    script = "scripts/deploy/azure_canary_app.sh"
    fixture = SCRIPT_FIXTURES[script]
    fixture_fragment = rf"TR_{provider}_OAUTH_LOGIN_AVAILABLE.*value"
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            responses=(
                (fixture_fragment, "true"),
                *(response for response in fixture.responses if provider not in response[0]),
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / f"azure-canary-{provider.lower()}-capability-drift")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "public canary OAuth capability verification failed" in run.stderr
    assert f"{provider.lower()}=true" in run.stderr
    assert not any(call[:4] == ["az", "containerapp", "revision", "set-mode"] for call in run.calls)
    assert not any(
        call[:3] == ["az", "containerapp", "show"] and "ingress.fqdn" in " ".join(call)
        for call in run.calls
    )


def test_azure_observer_rejects_a_live_legacy_private_env_after_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            responses=(
                (
                    r"containers\[0\]\.env\[\]\.name",
                    "TR_SERVICE_SURFACE\nTR_INTERNAL_GATEWAY_TOKEN\nTR_RELEASE",
                ),
                *fixture.responses,
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-observer-stale-private-env")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "retains forbidden legacy env TR_INTERNAL_GATEWAY_TOKEN" in run.stderr
    assert not run.verifier_calls


def test_azure_observer_rejects_reused_billing_gateway_credential_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/azure_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    home_files = {
        **fixture.home_files,
        ".quill-secrets/trustedrouter-observer-internal-token": "same-token\n",
        ".quill-secrets/trustedrouter-internal-gateway-token": "same-token\n",
    }
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, home_files=home_files),
    )
    isolated = DeployScriptHarness(tmp_path / "azure-observer-token-reuse")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "must differ from the billing gateway token" in run.stderr
    assert not any(
        call[:3]
        in (
            ["az", "containerapp", "create"],
            ["az", "containerapp", "update"],
        )
        for call in run.calls
    )
    assert not any(call[:3] == ["az", "acr", "build"] for call in run.calls)
    assert not run.verifier_calls


@pytest.mark.parametrize(
    ("script", "fixture_fragment", "reported_value"),
    [
        (script, fixture_fragment, reported_value)
        for script in (
            "scripts/deploy/azure_control_plane.sh",
            "scripts/deploy/azure_canary_app.sh",
        )
        for fixture_fragment, reported_value in (
            ("activeRevisionsMode", "Multiple"),
            (r"template\.scale\.maxReplicas", "99"),
            ("concurrentRequests", "999"),
        )
    ],
)
def test_azure_surfaces_fail_closed_on_each_live_scaling_postcondition_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    fixture_fragment: str,
    reported_value: str,
) -> None:
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (fixture_fragment, reported_value),
        *(response for response in fixture.responses if fixture_fragment not in response[0]),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / f"azure-drift-{Path(script).stem}-{reported_value}")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "observer scale verification failed" in run.stderr
    assert reported_value in run.stderr
    assert not run.verifier_calls
    assert not any(
        call[:3] == ["az", "containerapp", "show"] and "ingress.fqdn" in " ".join(call)
        for call in run.calls
    )


def test_synthetic_jobs_execute_private_ingress_preflight_in_their_own_region(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "synthetic-private")

    run = isolated.run("scripts/deploy/synthetic.sh", verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    scheduler_pauses = _gcloud_calls(run, "scheduler", "jobs", "pause")
    assert len(scheduler_pauses) == 1
    assert "trusted-router-spend-lease-soak-us-central1-every-minute" in scheduler_pauses[0]
    deploys = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
    ]
    assert len(deploys) == 7
    subnet_updates = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:7]
        == [
            "gcloud",
            "--project",
            "quill-cloud-proxy",
            "compute",
            "networks",
            "subnets",
            "update",
        ]
    ]
    assert len(subnet_updates) == 7
    service_contract_reads = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:6]
        == [
            "gcloud",
            "--project",
            "quill-cloud-proxy",
            "run",
            "services",
            "describe",
        ]
        and call[6] == "trusted-router-billing"
    ]
    assert len(service_contract_reads) == 7
    previous_deploy_index = -1
    for deploy_index, deploy in deploys:
        region = deploy[deploy.index("--region") + 1]
        deploy_text = " ".join(deploy)
        deployed_env = _cloud_run_job_env(deploy)
        assert deployed_env["TR_SERVICE_SURFACE"] == "observer"
        assert "TR_ALLOW_DEPLOYED_COMBINED_SURFACE" not in deployed_env
        assert "TR_RATE_LIMIT_ENABLED" not in deployed_env
        assert (
            "TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:latest" in deploy_text
        )
        job_name = deploy[6]
        if job_name.startswith("trusted-router-stage-d-probe-"):
            assert (
                "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
                in deploy_text
            )
        else:
            assert "TR_INTERNAL_GATEWAY_TOKEN" not in deploy_text
        assert "--set-secrets" in deploy
        assert "--update-secrets" not in deploy
        assert deploy[deploy.index("--network") + 1] == "default"
        assert deploy[deploy.index("--subnet") + 1] == "default"
        assert deploy[deploy.index("--vpc-egress") + 1] == "private-ranges-only"
        fresh_subnet_updates = [
            call
            for index, call in enumerate(run.calls[:deploy_index])
            if index > previous_deploy_index
            if call[:7]
            == [
                "gcloud",
                "--project",
                "quill-cloud-proxy",
                "compute",
                "networks",
                "subnets",
                "update",
            ]
        ]
        assert len(fresh_subnet_updates) == 1
        fresh_contract_reads = [
            call
            for index, call in enumerate(run.calls[:deploy_index])
            if index > previous_deploy_index
            if call[:6]
            == [
                "gcloud",
                "--project",
                "quill-cloud-proxy",
                "run",
                "services",
                "describe",
            ]
        ]
        assert len(fresh_contract_reads) == 1
        contract_read = fresh_contract_reads[0]
        assert contract_read[6] == "trusted-router-billing"
        assert contract_read[contract_read.index("--region") + 1] == region
        latest_preflight = fresh_subnet_updates[0]
        preflight_region = next(
            (
                latest_preflight[index + 1]
                if argument == "--region"
                else argument.removeprefix("--region=")
            )
            for index, argument in enumerate(latest_preflight)
            if argument == "--region" or argument.startswith("--region=")
        )
        assert preflight_region == region
        previous_deploy_index = deploy_index


def test_synthetic_deploy_requires_explicit_split_billing_service_before_any_gcloud_call(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "synthetic-no-split-service")

    run = isolated.run(
        "scripts/deploy/synthetic.sh",
        verifier_rc=0,
        omit_env=("TR_BILLING_SERVICE",),
    )

    assert run.returncode != 0
    assert "TR_BILLING_SERVICE is required" in run.stderr
    assert run.calls == []


def test_combined_synthetic_refresh_preserves_security_boundaries(tmp_path: Path) -> None:
    script = "scripts/deploy/synthetic_image_refresh.sh"
    isolated = DeployScriptHarness(tmp_path / "synthetic-image-refresh")

    run = isolated.run(
        script,
        verifier_rc=0,
        omit_env=("TR_BILLING_SERVICE", "TR_ALLOW_DEPLOYED_COMBINED_SURFACE"),
    )

    assert run.returncode == 0, summarise(run)
    updates = [
        call
        for call in run.calls
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "update"]
    ]
    assert [(call[6], call[call.index("--region") + 1]) for call in updates] == [
        ("trusted-router-synthetic-us-central1", "us-central1"),
        ("trusted-router-synthetic-europe-west4", "europe-west4"),
        ("trusted-router-throughput-us-central1", "us-central1"),
        ("trusted-router-spend-lease-soak-us-central1", "us-central1"),
        ("trusted-router-image-generation-us-central1", "us-central1"),
        ("trusted-router-video-generation-us-central1", "us-central1"),
    ]
    assert not any(call[4:6] == ["jobs", "deploy"] for call in run.calls if len(call) > 5)
    for update in updates:
        update_text = " ".join(update)
        assert "--image" in update
        assert "--update-env-vars" in update
        assert "TR_RELEASE=1234567" in update_text
        env_update = update[update.index("--update-env-vars") + 1]
        assert env_update.startswith("^|^")
        assert "TR_REGIONS=us-central1,us-east4,europe-west4,us-west1" in env_update.split("|")
        assert "southamerica-east1" not in env_update
        assert "--set-secrets" not in update
        assert "--update-secrets" not in update
        assert "--service-account" not in update
        assert "--network" not in update
        assert "--subnet" not in update
        assert "--vpc-egress" not in update
    for update in updates[:2]:
        assert (
            "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=https://trustedrouter.com"
            in " ".join(update)
        )
    scheduler_pauses = _gcloud_calls(run, "scheduler", "jobs", "pause")
    assert len(scheduler_pauses) == 1
    assert "trusted-router-spend-lease-soak-us-central1-every-minute" in scheduler_pauses[0]
    assert not _gcloud_calls(run, "scheduler", "jobs", "resume")


def test_combined_synthetic_refresh_resumes_enabled_spend_lease_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/synthetic_image_refresh.sh"
    fixture = SCRIPT_FIXTURES[script]
    job_json = json.loads(fixture.responses[0][1])
    env = job_json["spec"]["template"]["spec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    next(
        item for item in env if item["name"] == "TR_SPEND_LEASE_SOAK_PROBE_ENABLED"
    )["value"] = "true"
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            responses=(
                (fixture.responses[0][0], json.dumps(job_json)),
                (fixture.responses[1][0], "PAUSED"),
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-image-refresh-enabled-soak")

    run = isolated.run(
        script,
        verifier_rc=0,
        omit_env=("TR_BILLING_SERVICE", "TR_ALLOW_DEPLOYED_COMBINED_SURFACE"),
    )

    assert run.returncode == 0, summarise(run)
    scheduler_resumes = _gcloud_calls(run, "scheduler", "jobs", "resume")
    assert len(scheduler_resumes) == 1
    assert "trusted-router-spend-lease-soak-us-central1-every-minute" in scheduler_resumes[0]
    assert not _gcloud_calls(run, "scheduler", "jobs", "pause")


def test_combined_synthetic_refresh_is_a_visible_release_gate() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]
    synthetic_step = rollout.split(
        "- name: Deploy synthetic monitor Cloud Run Job", 1
    )[1].split("- name: Enforce Google Data Manager sharing disabled", 1)[0]

    assert "synthetic_image_refresh.sh" in synthetic_step
    assert "synthetic.sh" in synthetic_step
    assert "if:" not in synthetic_step
    assert "continue-on-error" not in synthetic_step
    assert "TR_ALLOW_DEPLOYED_COMBINED_SURFACE" not in synthetic_step
    assert "deploy_synthetic_monitor" not in workflow


def test_synthetic_combined_bridge_restores_legacy_job_deploys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/synthetic.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            env={**fixture.env, "TR_ALLOW_DEPLOYED_COMBINED_SURFACE": "true"},
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-combined-bridge")

    run = isolated.run(script, verifier_rc=0, omit_env=("TR_BILLING_SERVICE",))

    assert run.returncode == 0, summarise(run)
    deploys = [
        call
        for call in run.calls
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
    ]
    assert [(call[6], call[call.index("--region") + 1]) for call in deploys] == [
        ("trusted-router-synthetic-us-central1", "us-central1"),
        ("trusted-router-synthetic-europe-west4", "europe-west4"),
        ("trusted-router-stage-d-probe-us-central1", "us-central1"),
        ("trusted-router-throughput-us-central1", "us-central1"),
        ("trusted-router-spend-lease-soak-us-central1", "us-central1"),
        ("trusted-router-image-generation-us-central1", "us-central1"),
        ("trusted-router-video-generation-us-central1", "us-central1"),
    ]
    assert not any(
        call[:6]
        == [
            "gcloud",
            "--project",
            "quill-cloud-proxy",
            "run",
            "services",
            "describe",
        ]
        for call in run.calls
    )
    assert not any(
        call[:5]
        == [
            "gcloud",
            "--project",
            "quill-cloud-proxy",
            "secrets",
            "describe",
        ]
        and call[5] == "trustedrouter-observer-internal-token"
        for call in run.calls
    )
    for deploy in deploys:
        deploy_text = " ".join(deploy)
        deployed_env = _cloud_run_job_env(deploy)
        region = deploy[deploy.index("--region") + 1]
        assert "https://trustedrouter.com/v1/internal/synthetic/" in deploy_text
        assert f"https://trusted-router-stub-output.{region}.run.app" not in deploy_text
        assert (
            "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest" in deploy_text
        )
        assert "TR_OBSERVER_INTERNAL_TOKEN" not in deploy_text
        assert deployed_env["TR_SERVICE_SURFACE"] == "combined"
        assert deployed_env["TR_ALLOW_DEPLOYED_COMBINED_SURFACE"] == "true"
        assert deployed_env["TR_RATE_LIMIT_ENABLED"] == "false"
        assert "TR_BYOK_KMS_KEY_NAME=" in deploy_text
        assert "--update-secrets" in deploy
        assert "--set-secrets" not in deploy
        assert "--network" not in deploy
        assert "--subnet" not in deploy
        assert "--vpc-egress" not in deploy
        # The combined service is private behind the load balancer. Bridge jobs
        # reach token-protected ingest routes through that load balancer and must
        # not grow a private VPC path before the split service is active.
        assert not any(
            arg == flag or arg.startswith(f"{flag}=")
            for arg in deploy
            for flag in ("--network", "--subnet", "--vpc-egress", "--vpc-connector")
        ), " ".join(deploy)


def test_synthetic_combined_bridge_job_environment_constructs_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/synthetic.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            env={**fixture.env, "TR_ALLOW_DEPLOYED_COMBINED_SURFACE": "true"},
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-combined-settings")

    run = isolated.run(script, verifier_rc=0, omit_env=("TR_BILLING_SERVICE",))

    assert run.returncode == 0, summarise(run)
    deploys = [
        call
        for call in run.calls
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
    ]
    assert len(deploys) == 7
    for deploy in deploys:
        settings_kwargs = _settings_kwargs_from_cloud_run_job(deploy)
        settings = Settings(**settings_kwargs)
        assert settings.environment == "worker"
        assert settings.service_surface == "combined"

        without_combined_opt_in = dict(settings_kwargs)
        without_combined_opt_in.pop("allow_deployed_combined_surface")
        with pytest.raises(
            ValidationError,
            match="TR_ALLOW_DEPLOYED_COMBINED_SURFACE",
        ):
            Settings(**without_combined_opt_in)

        without_gateway_token = dict(settings_kwargs)
        without_gateway_token.pop("internal_gateway_token")
        with pytest.raises(
            ValidationError,
            match="not fail-closed.*TR_INTERNAL_GATEWAY_TOKEN",
        ):
            Settings(**without_gateway_token)


@pytest.mark.parametrize(
    "service_json",
    [
        '{"metadata":{"name":"trusted-router-billing",'
        '"annotations":{"run.googleapis.com/ingress":'
        '"internal-and-cloud-load-balancing"}},'
        '"status":{"conditions":[{"type":"Ready","status":"False"}]},'
        '"spec":{"template":{"spec":{"containers":[{"env":[]}]}}}}',
        '{"metadata":{"name":"trusted-router-billing",'
        '"annotations":{"run.googleapis.com/ingress":"all"}},'
        '"status":{"conditions":[{"type":"Ready","status":"True"}]},'
        '"spec":{"template":{"spec":{"containers":[{"env":[]}]}}}}',
        '{"metadata":{"name":"trusted-router-billing",'
        '"annotations":{"run.googleapis.com/ingress":'
        '"internal-and-cloud-load-balancing"}},'
        '"status":{"conditions":[{"type":"Ready","status":"True"}]},'
        '"spec":{"template":{"spec":{"containers":[{"env":['
        '{"name":"TR_SERVICE_SURFACE","value":"public"}] }]}}}}',
        '{"metadata":{"name":"trusted-router-billing",'
        '"annotations":{"run.googleapis.com/ingress":'
        '"internal-and-cloud-load-balancing"}},'
        '"status":{"conditions":[{"type":"Ready","status":"True"}]},'
        '"spec":{"template":{"spec":{"containers":[{"env":['
        '{"name":"TR_SERVICE_SURFACE","value":"internal"},'
        '{"name":"TR_OBSERVER_INTERNAL_TOKEN","valueFrom":'
        '{"secretKeyRef":{"name":"wrong-secret"}}},'
        '{"name":"TR_INTERNAL_GATEWAY_TOKEN","valueFrom":'
        '{"secretKeyRef":{"name":"trustedrouter-internal-gateway-token"}}}'
        "] }]}}}}",
    ],
    ids=("not-ready", "public-ingress", "wrong-surface", "wrong-observer-secret"),
)
def test_synthetic_deploy_rejects_live_ingest_contract_drift_before_any_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_json: str,
) -> None:
    script = "scripts/deploy/synthetic.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (r"run services describe trusted-router-billing.*--format=json", service_json),
        *(
            response
            for response in fixture.responses
            if "run services describe trusted-router-billing" not in response[0]
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-contract-drift")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "internal synthetic ingest service contract failed" in run.stderr
    assert not any(
        call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
        for call in run.calls
    )


def test_synthetic_deploy_fails_before_jobs_without_observer_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/synthetic.sh"
    fixture = SCRIPT_FIXTURES[script]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(
            fixture,
            failures=(
                r"^gcloud --project quill-cloud-proxy secrets describe "
                r"trustedrouter-observer-internal-token$",
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-missing-observer-token")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "observer-internal-token is required" in run.stderr
    assert not any(
        call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
        for call in run.calls
    )


@pytest.mark.parametrize(
    "zone_json",
    [
        '{"dnsName":"not-run.app.","visibility":"private",'
        '"privateVisibilityConfig":{"networks":['
        '{"networkUrl":"projects/quill-cloud-proxy/global/networks/default"}]}}',
        '{"dnsName":"run.app.","visibility":"public",'
        '"privateVisibilityConfig":{"networks":['
        '{"networkUrl":"projects/quill-cloud-proxy/global/networks/default"}]}}',
        '{"dnsName":"run.app.","visibility":"private",'
        '"privateVisibilityConfig":{"networks":['
        '{"networkUrl":"projects/quill-cloud-proxy/global/networks/wrong"}]}}',
    ],
    ids=("wrong-dns-name", "public-zone", "wrong-network"),
)
def test_synthetic_private_ingress_drift_stops_before_any_job_deploy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone_json: str,
) -> None:
    script = "scripts/deploy/synthetic.sh"
    fixture = SCRIPT_FIXTURES[script]
    responses = (
        (
            r"dns managed-zones describe trusted-router-private-run-app --format=json",
            zone_json,
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(tmp_path / "synthetic-drift")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "unsafe drift" in run.stderr
    assert not any(
        call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
        for call in run.calls
    )


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
        f"{script} called the gate, the gate passed, and the script still failed.\n{summarise(run)}"
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
    """All bound scripts understand the gate's two codes, or none of them do.

    Exit 5 (NOT YET OBSERVABLE) used to be taught to exactly one of five: the
    other four reported today's real state — no deployed control plane publishes
    the `analytics` section — as a flat install failure with a fix that would not
    have fixed it, which is how an operator learns to stop reading exit codes.
    The mapping is one shared file now, and this asserts the consequence rather
    than the mechanism: each code comes out the far end unchanged.

    There are two codes because 5 is the only one that earns its own words. The
    gate used to have seven; the rest are collapsed into 1, which prints why.
    """
    for rc in (1, 5):
        run = harness.run(script, verifier_rc=rc)
        assert run.returncode == rc, (
            f"{script} turned gate exit {rc} into {run.returncode}. 5 and 1 mean "
            f"different things to an operator; collapsing them is the defect.\n"
            f"{summarise(run)}"
        )


def test_the_gate_status_survives_without_the_operator_attestation(
    harness: DeployScriptHarness,
) -> None:
    """The propagation claim, asked the way a FIRST RUN asks it.

    `aws_eu_north_clickhouse.sh` refuses to claim the Stockholm replica is wired
    until an operator says so with TR_STOCKHOLM_REPLICA_WIRED=1, and exits 3
    when they have not. That check used to run AFTER the gate and unconditionally
    overwrite its status — so on a first run, when nobody has ever set the
    variable, the gate's 5 and the gate's 1 both came out as 3. The only reason
    the parametrised test above passed for this script is that the harness
    fixture sets the variable; the operator does not have it.
    """
    script = "scripts/deploy/aws_eu_north_clickhouse.sh"
    for rc in (1, 5):
        run = harness.run(script, verifier_rc=rc, omit_env=("TR_STOCKHOLM_REPLICA_WIRED",))
        assert run.returncode == rc, (
            f"{script} turned gate exit {rc} into {run.returncode} for an operator who "
            f"has not set TR_STOCKHOLM_REPLICA_WIRED, i.e. on every first run.\n"
            f"{summarise(run)}"
        )

    # ...and with the gate passing, the unwired replica is still its own answer.
    unwired = harness.run(script, verifier_rc=0, omit_env=("TR_STOCKHOLM_REPLICA_WIRED",))
    assert unwired.returncode == 3, summarise(unwired)
    assert "STOCKHOLM NOT WIRED" in unwired.stderr


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

    Note what is NOT set here: the gate library used to read
    CLOUD_COMPLETE_GATE_DIR to decide which verifier to run, "for the test
    harness". Every bound deploy script inherits its operator's environment, so
    that variable was a redirect for the gate itself — the third appearance of
    the class of defect the verifier spends a section of its header closing. It
    is gone, and nothing was lost: the caller below sources the gate out of the
    MIRRORED checkout, and the gate resolves the verifier next to itself, which
    is the mirror's recording stub.
    """
    caller = tmp_path / "caller.sh"
    caller.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'. "{harness.mirror}/scripts/deploy/cloud_complete_gate.sh"\n'
        'require_cloud_complete "$1" "next steps for the operator"\n'
    )

    def run_with(rc: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["bash", str(caller), "aws"],  # noqa: S607
            capture_output=True,
            text=True,
            env={
                "PATH": str(harness.bin),
                "HOME": str(tmp_path),
                "HARNESS_ARGV_LOG": str(tmp_path / "argv.log"),
                "HARNESS_VERIFIER_RC": str(rc),
            },
        )

    for rc in (0, 1, 5):
        result = run_with(rc)
        assert result.returncode == rc, (rc, result.returncode, result.stderr)
        if rc != 0:
            assert "next steps for the operator" in result.stderr

    # ...and each non-zero code gets its own words, so an operator is not told
    # to fix an install that did not fail.
    assert "NOT YET OBSERVABLE" in run_with(5).stderr
    assert "NOT VERIFIED" in run_with(1).stderr


@pytest.mark.parametrize(("script", "cloud"), PROVEN, ids=[s for s, _ in PROVEN])
def test_each_gate_outcome_gets_the_same_words_from_every_script(
    harness: DeployScriptHarness, script: str, cloud: str
) -> None:
    """The consequence of sharing the library, read off the scripts' own output.

    Exit 5 used to be taught to exactly one of five bound scripts: the other
    four reported today's real state — no control plane publishes the analytics
    section yet — as a flat install failure with a fix that would not have fixed
    it. So this does not check that a file sources a file; it runs the script
    under each outcome and reads what the operator would have been told.
    """
    expected = {
        1: "NOT VERIFIED",
        5: "NOT YET OBSERVABLE",
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


#: The page that states, for a human, which scripts are proven and which are
#: only claimed. It is the other half of the registry, and CI holds the two to
#: exact agreement.
PROOF_DOC = ROOT / "docs" / "storage-portability" / "multi-cloud-separation.md"


def _documented_scripts(marker: str) -> set[str]:
    """The repo-relative paths listed between ``<!-- MARKER:begin/end -->``.

    A parser rather than a substring search, and that is the whole fix. The
    previous check asked whether each NOT_PROVEN script's BASENAME appeared
    anywhere in this document — which it does, several times, in the prose
    explaining why it is not proven. So the document could go on calling a
    script proven while the registry called it unproven, and the check was happy
    with both.
    """
    text = PROOF_DOC.read_text()
    match = re.search(
        rf"<!--\s*{marker}:begin\s*-->(.*?)<!--\s*{marker}:end\s*-->", text, re.DOTALL
    )
    assert match is not None, (
        f"{PROOF_DOC.relative_to(ROOT)} has no <!-- {marker}:begin --> block. That block "
        "is how the docs and ROLLOUT_REGISTRY are held to the same list; do not delete it "
        "to make this test pass."
    )
    return set(re.findall(r"^\s*[-*]\s*`([^`]+)`\s*$", match.group(1), re.MULTILINE))


def test_the_docs_and_the_registry_name_the_same_proven_scripts() -> None:
    """Losing behavioural coverage has to be loud, and this is the noise.

    Flipping one script from PROVEN_BY_EXECUTION to NOT_PROVEN takes five
    parametrised cases out of this module — the suite goes from 76 passing to 71
    and stays GREEN, because a test that is not collected cannot fail. For one
    revision the only thing in the way was a minimum reason length, which is 121
    characters of filler.

    So the gate is not the reason's length: it is that the registry and the
    human-readable page must name the SAME SET, exactly. A script cannot lose
    its coverage without an edit to a document somebody reviews, and the failure
    below says which script moved and in which direction.
    """
    registry_proven = {script for script, _cloud in PROVEN}
    registry_unproven = {script for script, _cloud, _reason in UNPROVEN}
    doc_proven = _documented_scripts("PROVEN_BY_EXECUTION")
    doc_unproven = _documented_scripts("NOT_PROVEN")

    assert registry_proven == doc_proven, (
        "ROLLOUT_REGISTRY and the 'Proven by execution today' list disagree.\n"
        f"  proven in the registry, absent from the docs: {sorted(registry_proven - doc_proven)}\n"
        f"  listed in the docs, not proven in the registry: {sorted(doc_proven - registry_proven)}\n"
        f"Fix both, in {PROOF_DOC.relative_to(ROOT)} and "
        "src/trusted_router/cloud_rollout_completeness.py. A script that quietly stops "
        "being executed here loses five behavioural cases and the suite stays green."
    )
    assert registry_unproven == doc_unproven, (
        "ROLLOUT_REGISTRY and the 'Not proven, and therefore only CLAIMED' list "
        "disagree.\n"
        f"  NOT_PROVEN in the registry, absent from the docs: "
        f"{sorted(registry_unproven - doc_unproven)}\n"
        f"  listed in the docs, not NOT_PROVEN in the registry: "
        f"{sorted(doc_unproven - registry_unproven)}"
    )
    assert not (registry_proven & registry_unproven)


def test_unproven_scripts_are_declared_and_not_silently_skipped() -> None:
    """A script this harness cannot run honestly must SAY so, in code and docs.

    The permitted answer to "the harness cannot run this one" is a written
    reason, not a quiet omission — an omission is exactly the shape of the
    original defect.

    Note what this no longer asserts: a minimum reason LENGTH. That was the only
    thing standing between the registry and a silent loss of five behavioural
    cases per script, and 121 characters of filler cleared it. The gate is
    :func:`test_the_docs_and_the_registry_name_the_same_proven_scripts`; a blank
    reason is separately a `script_binding_gaps` failure.
    """
    for script, cloud, reason in UNPROVEN:
        assert reason.strip(), f"{cloud}: {script} is NOT_PROVEN with no reason"
        assert (ROOT / script).is_file()


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
    # It has to have RUN and stopped, not failed to parse. Those are the same
    # two observations — non-zero, gate never reached — and for a while they
    # were the same outcome here: this file did not parse under bash 3.2 at all
    # (see test_every_deploy_script_parses_in_this_machine_s_shell), so this
    # test was passing on a Mac without the script executing a single line.
    assert run.calls, f"the script never ran a command at all.\n{summarise(run)}"
    assert not run.gate_ran_for("aws"), (
        "the drain installer now reaches the gate under stubs — promote it to "
        "PROVEN_BY_EXECUTION in ROLLOUT_REGISTRY and delete this test's premise"
    )
    assert run.returncode != 0


# --- azure_control_plane.sh: where the observer secrets come from -----------
#
# The order is a file in $SECRETS_DIR, then $KEYS_FILE, then the value the
# RUNNING app already holds. The last is what lets
# .github/workflows/deploy-azure-control-plane.yml deploy with no secret
# files; the first is how an operator rotates a value.

_AZURE = "scripts/deploy/azure_control_plane.sh"
_APP_OBSERVER = "app-held-observer-value"
_APP_MONITOR = "app-held-monitor-value"
_APP_LEGACY = "app-held-legacy-gateway-value"


_APP_SECRET_NAMES = ("observer-token", "monitor-key", "internal-token")


def _azure_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_secret_files: bool,
    app_secrets: dict[str, str],
    name: str,
    extra_responses: tuple[tuple[str, str], ...] = (),
) -> DeployScriptHarness:
    # Each of the three secrets this change reads back from the app
    # (_APP_SECRET_NAMES) is either given a value here or made to fail, which
    # is what `az containerapp secret show` does for a secret the app does not
    # hold, so none of those reads reaches the stub's default output.
    # pg-password, which the script read back before this change, is left to
    # the fixture unless a test passes it in extra_responses.
    fixture = SCRIPT_FIXTURES[_AZURE]
    responses = extra_responses + tuple(
        (rf"containerapp secret show .*--secret-name {secret} --query", app_secrets[secret])
        for secret in _APP_SECRET_NAMES
        if secret in app_secrets
    ) + fixture.responses
    failures = tuple(
        rf"containerapp secret show .*--secret-name {secret} --query"
        for secret in _APP_SECRET_NAMES
        if secret not in app_secrets
    ) + fixture.failures
    home_files = fixture.home_files if with_secret_files else {}
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        _AZURE,
        replace(fixture, responses=responses, failures=failures, home_files=home_files),
    )
    return DeployScriptHarness(tmp_path / name)


def _secret_set_values(run: HarnessRun) -> dict[str, str]:
    (secret_set,) = [c for c in run.calls if c[:4] == ["az", "containerapp", "secret", "set"]]
    values = secret_set[secret_set.index("--secrets") + 1 :]
    return dict(v.split("=", 1) for v in values if "=" in v and not v.startswith("-"))


def _app_secret_reads(run: HarnessRun) -> list[str]:
    return [
        c[c.index("--secret-name") + 1]
        for c in run.calls
        if c[:4] == ["az", "containerapp", "secret", "show"]
    ]


def test_azure_deploy_with_no_secret_files_carries_the_running_apps_values_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CI shape: no $SECRETS_DIR, no $KEYS_FILE.
    harness = _azure_harness(
        tmp_path,
        monkeypatch,
        with_secret_files=False,
        app_secrets={
            "observer-token": _APP_OBSERVER,
            "monitor-key": _APP_MONITOR,
            "internal-token": _APP_LEGACY,
        },
        name="azure-no-files",
    )

    run = harness.run(_AZURE, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    written = _secret_set_values(run)
    assert written["observer-token"] == _APP_OBSERVER
    assert written["monitor-key"] == _APP_MONITOR
    assert {"observer-token", "monitor-key", "internal-token"} <= set(_app_secret_reads(run))


def test_azure_deploy_prefers_the_operators_secret_files_over_the_running_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rotation: the operator's files hold new values, the app the old ones.
    harness = _azure_harness(
        tmp_path,
        monkeypatch,
        with_secret_files=True,
        app_secrets={"observer-token": _APP_OBSERVER, "monitor-key": _APP_MONITOR},
        name="azure-files-win",
    )

    run = harness.run(_AZURE, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    written = _secret_set_values(run)
    assert written["observer-token"] == "harness-fake-observer"
    assert written["monitor-key"] == "harness-fake-monitor"
    reads = _app_secret_reads(run)
    assert "observer-token" not in reads
    assert "monitor-key" not in reads


def test_azure_deploy_refuses_when_neither_files_nor_the_app_hold_the_observer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _azure_harness(
        tmp_path,
        monkeypatch,
        with_secret_files=False,
        app_secrets={"monitor-key": _APP_MONITOR},
        name="azure-no-observer",
    )

    run = harness.run(_AZURE, verifier_rc=0)

    assert run.returncode != 0
    assert "no observer internal token in" in run.stderr
    assert "or the running app" in run.stderr
    assert not any(c[:4] == ["az", "containerapp", "secret", "set"] for c in run.calls)
    assert not any(c[:3] == ["az", "containerapp", "update"] for c in run.calls)


def test_azure_deploy_keeps_the_observer_and_billing_tokens_apart_when_reading_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The separation check still runs when both values come from the app.
    # The positive control is the first test above: different values deploy.
    harness = _azure_harness(
        tmp_path,
        monkeypatch,
        with_secret_files=False,
        app_secrets={
            "observer-token": _APP_LEGACY,
            "monitor-key": _APP_MONITOR,
            "internal-token": _APP_LEGACY,
        },
        name="azure-same-token",
    )

    run = harness.run(_AZURE, verifier_rc=0)

    assert run.returncode != 0
    assert "observer internal token must differ from the billing gateway token" in run.stderr
    assert not any(c[:4] == ["az", "containerapp", "secret", "set"] for c in run.calls)


_APP_PG_PASSWORD = "app-held-pg-password"  # noqa: S105 - test value, not a credential


@pytest.mark.parametrize("in_github_actions", [True, False])
def test_azure_deploy_masks_every_value_it_reads_when_running_in_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_github_actions: bool
) -> None:
    # Values read at run time are not registered GitHub secrets, so Actions
    # would not mask them, and this repository's run logs are public.
    harness = _azure_harness(
        tmp_path,
        monkeypatch,
        with_secret_files=False,
        app_secrets={
            "observer-token": _APP_OBSERVER,
            "monitor-key": _APP_MONITOR,
            "internal-token": _APP_LEGACY,
        },
        extra_responses=(
            (r"containerapp secret show .*--secret-name pg-password --query", _APP_PG_PASSWORD),
        ),
        name=f"azure-mask-{in_github_actions}",
    )

    run = harness.run(
        _AZURE,
        verifier_rc=0,
        extra_env={"GITHUB_ACTIONS": "true"} if in_github_actions else {},
    )

    assert run.returncode == 0, summarise(run)
    masks = [line for line in run.stdout.splitlines() if line.startswith("::add-mask::")]
    values = (_APP_OBSERVER, _APP_MONITOR, _APP_LEGACY, _APP_PG_PASSWORD)
    if in_github_actions:
        assert masks == [f"::add-mask::{value}" for value in values]
    else:
        # Positive control: an operator's terminal gets no workflow commands.
        assert masks == []
    unmasked_output = "\n".join(
        line for line in (run.stdout + "\n" + run.stderr).splitlines()
        if not line.startswith("::add-mask::")
    )
    for value in values:
        assert value not in unmasked_output
