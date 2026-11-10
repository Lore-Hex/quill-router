"""The enabler's deploy (docs/design/fast-admission-widening.md sections 4(d)
and 5.7): scripts/deploy/fastpath_enabler_job.sh run to completion under the
deploy-script harness's stubs, its emitted job held to the pinned literals and
to the startup validator; its step in the production workflow; and its two
alerts' filters matched against the lines the enabler writes."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
from google.cloud.spanner_v1 import COMMIT_TIMESTAMP, param_types

from tests.deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    summarise,
)
from tests.fakes.fastpath_enabler_spanner import T0, FakeDatabase, go_arm
from tests.test_fastpath_enabler import BASE, NOW, _fleet, _seed
from trusted_router import fastpath_enabler as fe
from trusted_router.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/deploy/fastpath_enabler_job.sh"
JOB = "trusted-router-fastpath-enabler"
TERRAFORM = ROOT / "infra" / "fastpath_enabler_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbooks" / "fastpath-alerts.md"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
SECRETS = "trustedrouter-sentry-dsn\ntrustedrouter-synthetic-monitor-api-key\n"


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, secrets: str = SECRETS,
         image_exists: bool = True) -> HarnessRun:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(
        responses=((r"^gcloud .*secrets list", secrets),),
        failures=() if image_exists else (r"artifacts docker images describe",),
    ))
    return DeployScriptHarness(tmp_path / str(image_exists)).run(SCRIPT)


def _calls(run: HarnessRun, *command: str) -> list[list[str]]:
    return [call for call in run.calls if call[0] == "gcloud" and call[3:3 + len(command)] == list(command)]


def _job(run: HarnessRun) -> tuple[dict[str, str], dict[str, str], list[str]]:
    (job,) = _calls(run, "run", "jobs", "update", JOB)
    env = dict(item.split("=", 1) for item in job[job.index("--set-env-vars") + 1].removeprefix("^|^").split("|"))
    secrets = next(a for a in job if a.startswith("--set-secrets="))
    bindings = dict(item.split("=", 1) for item in secrets.removeprefix("--set-secrets=").split(","))
    return env, bindings, job


def test_the_job_deploys_in_apply_with_the_lists_and_its_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_fastpath_lists import resolved_lists

    run = _run(tmp_path, monkeypatch)
    assert run.returncode == 0, summarise(run)
    env, bindings, job = _job(run)
    # WS2: apply writes the allow-list once armed; nobody joins off the list.
    assert env["TR_FASTPATH_ENABLER_MODE"] == "apply" and env["TR_FASTPATH_ENABLER_MAX_RANK"] == "0"
    lists = resolved_lists()
    # The include list is its own literal since WS2 emptied the pilot set.
    assert env["TR_FASTPATH_ENABLER_INCLUDE"] == lists["TR_FASTPATH_ENABLER_INCLUDE"] != ""
    assert lists["TR_FASTPATH_PILOT_WORKSPACES"] == "" and "TR_FASTPATH_PILOT_ALL" not in env
    assert env["TR_FASTPATH_PILOT_EXCLUDE"] == lists["TR_FASTPATH_PILOT_EXCLUDE"]
    assert env["TR_FASTPATH_ENABLER_ENABLE_BALANCE_MICRO"] == "100000000"
    assert env["TR_FASTPATH_ENABLER_DISABLE_BALANCE_MICRO"] == "40000000"
    assert env["TR_STORAGE_BACKEND"] == "spanner-clickhouse" and env["TR_SERVICE_SURFACE"] == "control"
    assert "--args=-m,trusted_router.fastpath_enabler" in job
    # The probes' keys that exist, outside the TR_ prefix.
    assert bindings == {
        "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
        "FASTPATH_ENABLER_MONITOR_KEY": "trustedrouter-synthetic-monitor-api-key:latest",
    }
    (schedule,) = _calls(run, "scheduler", "jobs", "update", "http")
    assert "--schedule=12,27,42,57 * * * *" in schedule
    tier = (ROOT / "scripts" / "deploy" / "trust_tier_job.sh").read_text()
    assert '--schedule="7,22,37,52 * * * *"' in tier  # five minutes before
    # The emitted job starts: the real validator, with dummy secret values.
    with monkeypatch.context() as isolated:
        for name in os.environ:
            if name.startswith("TR_"):
                isolated.delenv(name)
        for name, value in env.items():
            isolated.setenv(name, value)
        isolated.setenv("TR_SENTRY_DSN", "https://key@example.invalid/1")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        config = fe.EnablerConfig.from_settings(settings)
    assert config.mode == "apply" and config.max_rank == 0
    assert config.include == frozenset(lists["TR_FASTPATH_ENABLER_INCLUDE"].split(","))


def test_the_job_reads_the_async_shadow_list_the_control_plane_does() -> None:
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text()
    job = (ROOT / SCRIPT).read_text()
    pattern = r'"TR_ASYNC_SETTLE_SHADOW_WORKSPACES=([^"]*)"'
    assert re.findall(pattern, job) == re.findall(pattern, rollout) and len(re.findall(pattern, job)) == 1


def test_the_job_holds_no_settings_key_the_control_surface_may_not() -> None:
    job = (ROOT / SCRIPT).read_text()
    for name in ("TR_SYNTHETIC_MONITOR_API_KEY", "TR_STAGE_D_PROBE_API_KEY"):
        assert name not in job
    assert set(fe.PROBE_KEY_ENVIRONMENT) <= set(re.findall(r'"(FASTPATH_ENABLER_[A-Z_]+_KEY)=', job))


def test_no_image_no_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch, image_exists=False)
    assert run.returncode != 0 and not _calls(run, "run", "jobs", "update", JOB)


def test_an_unreadable_secret_inventory_stops_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(failures=(r"^gcloud .*secrets list",)))
    run = DeployScriptHarness(tmp_path).run(SCRIPT)
    assert run.returncode != 0 and not _calls(run, "run", "jobs", "update", JOB)


def test_every_control_plane_release_deploys_the_job() -> None:
    workflow = WORKFLOW.read_text()
    step = "run: bash scripts/deploy/fastpath_enabler_job.sh"
    assert workflow.count(step) == 1
    # In the rollout job, after the trust jobs' step, behind no opt-in.
    assert workflow.index("run: bash scripts/deploy/trust_jobs.sh") < workflow.index(step)
    block = workflow[workflow.rindex("- name:", 0, workflow.index(step)):workflow.index(step)]
    assert "if:" not in block and "TR_TRUST_JOBS_DEPLOY" not in block


# -- the alerts --------------------------------------------------------------------


def _events() -> dict[str, str]:
    tf = TERRAFORM.read_text()
    block = re.search(r"fastpath_enabler_lines = \{(.*?)\n  \}", tf, re.S)
    assert block
    return dict(re.findall(r'^\s+(\w+)\s+= "([^"]+)"$', block.group(1), re.M))


def _payload_match(event: str) -> str:
    template = re.search(r'"textPayload:\\"(.*?)\\""', TERRAFORM.read_text())
    assert template
    return template.group(1).replace("${event}", event)


class _Lines(logging.Handler):
    """The enabler's lines, as its job's basicConfig format renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _rendered(kind: str) -> list[str]:
    handler = _Lines()
    log = logging.getLogger("trusted_router.fastpath_enabler")
    log.addHandler(handler)
    try:
        db = FakeDatabase()
        go_arm(db)
        names = _fleet(db, 40, enabled=True)
        if kind == "mass_disable":
            for name in names[:21]:
                db.credit[name][0].tier = 1
        else:
            for name in names:
                db.credit[name][0].reconciled = None
        fe.run(db, param_types, BASE.__class__(mode="dry-run", max_rank=100), now=NOW)
    finally:
        log.removeHandler(handler)
    return handler.lines


def test_each_filter_matches_the_line_the_enabler_writes_and_no_other() -> None:
    events = _events()
    assert events == {"mass_disable": "fastpath_enabler_mass_disable", "trust_stale": "fastpath_enabler_trust_stale"}
    tf = TERRAFORM.read_text()
    assert f'fastpath_enabler_job = "{JOB}"' in tf and (ROOT / SCRIPT).read_text().count(f'JOB_NAME="{JOB}"') == 1
    assert '"resource.type=\\"cloud_run_job\\""' in tf
    assert "logging.basicConfig(level=logging.INFO, format=\"%(levelname)s %(name)s %(message)s\")" in (
        ROOT / "src" / "trusted_router" / "fastpath_enabler.py").read_text()
    for kind, event in events.items():
        match = _payload_match(event)
        hits = [line for line in _rendered(kind) if match in line]
        assert len(hits) == 1, (kind, match)
        assert match not in f"ERROR trusted_router.fastpath_enabler {event}_x n=1"
        assert match not in f"WARNING trusted_router.fastpath_enabler {event} n=1"
        assert match not in f"ERROR trusted_router.other {event} n=1"
    # A quiet run writes neither.
    db = FakeDatabase()
    go_arm(db)
    _seed(db, "ws-synthetic-quiet", reconciled=T0)
    handler = _Lines()
    log = logging.getLogger("trusted_router.fastpath_enabler")
    log.addHandler(handler)
    try:
        fe.run(db, param_types, BASE, now=NOW, commit_timestamp=COMMIT_TIMESTAMP)
    finally:
        log.removeHandler(handler)
    assert not [line for line in handler.lines for event in events.values() if _payload_match(event) in line]


def test_the_metric_and_the_policy_read_one_filter_and_page_as_the_others_do() -> None:
    tf = TERRAFORM.read_text()
    assert tf.count("filter      = local.fastpath_enabler_filters[each.key]") == 1
    assert tf.count("filter = local.fastpath_enabler_filters[each.key]") == 1
    assert tf.count("for_each = local.fastpath_enabler_lines") == 2
    assert re.search(r'notification_rate_limit \{\n\s+period = "300s"\n\s+\}\n\s+auto_close = "1800s"', tf)
    assert "notification_channels = local.fastpath_alert_channels" in tf


def test_every_kind_has_a_runbook_row() -> None:
    runbook = RUNBOOK.read_text()
    for kind in _events():
        assert re.search(rf"^\| `{kind}` \| ", runbook, re.M), kind
