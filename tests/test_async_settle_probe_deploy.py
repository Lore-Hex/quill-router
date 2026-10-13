"""Real deploy scripts under the recording cloud harness; never real cloud calls."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.deploy_script_harness import (
    _SYNTHETIC_COMBINED_JOB_JSON,
    _SYNTHETIC_INGEST_SERVICE_JSON,
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    summarise,
)

COHORT = "trusted-router-async-settle-shadow-probe-"
SECRET = "trustedrouter-async-settle-shadow-probe-api-key"  # noqa: S105 - resource name
FULL = "scripts/deploy/synthetic.sh"
STANDALONE = "scripts/deploy/async_settle_shadow_probe_jobs.sh"
REFRESH = "scripts/deploy/synthetic_image_refresh.sh"


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    result = DeployScriptHarness(tmp_path_factory.mktemp("probe-deploy"))
    # Keep the recording harness and real scripts, but answer gcloud in the
    # current Bash process. The generic stub spawns awk/base64 for each lookup;
    # hundreds of subprocesses dominated these otherwise tiny contract tests.
    # No real gcloud is reachable through this function.
    result.write_script("probe-cloud.sh", r'''gcloud() {
  local record=gcloud arg
  for arg in "$@"; do record+=$'\t'"$arg"; done
  printf '%s\n' "$record" >> "$HARNESS_ARGV_LOG"
  [ "$1" != --project ] || shift 2
  case "$*" in
    "secrets describe trustedrouter-async-settle-shadow-probe-api-key"*)
      [ "$PROBE_SECRET" = present ]; return ;;
    "run jobs describe trusted-router-async-settle-shadow-probe-"*)
      [ "$PROBE_JOBS" = present ]; return ;;
    *"jobs describe trusted-router-spend-lease-soak-"*)
      printf 'ERROR: (gcloud.%s.jobs.describe) NOT_FOUND\n' "$1" >&2; return 1 ;;
    "projects describe "*) echo 123456789 ;;
    "run services describe "*) printf '%s\n' "$PROBE_SERVICE_JSON" ;;
    "run jobs describe "*) [ "${PROBE_MONITOR:-present}" = present ] || return 1; printf '%s\n' "$PROBE_JOB_JSON" ;;
    "scheduler jobs describe "*) [ "$PROBE_SCHEDULERS" = present ]; return ;;
    "dns managed-zones describe "*) printf '%s\n' '{"dnsName":"run.app.","visibility":"private","privateVisibilityConfig":{"networks":[{"networkUrl":"projects/quill-cloud-proxy/global/networks/default"}]}}' ;;
    "projects get-iam-policy "*) echo '{"bindings":[]}' ;;
    *) echo stub-output ;;
  esac
}
''')
    return result


def run_script(harness: DeployScriptHarness, script: str, **overrides: str):
    fixture = SCRIPT_FIXTURES[script]
    return harness.run(script, extra_env={
        "BASH_ENV": str(harness.mirror / "probe-cloud.sh"),
        "PROBE_SECRET": "absent" if any(SECRET in f for f in fixture.failures) else "present",
        "PROBE_JOBS": "absent" if any(COHORT in f for f in fixture.failures) else "present",
        "PROBE_SCHEDULERS": "absent" if any("scheduler" in f and "soak" not in f for f in fixture.failures) else "present",
        "PROBE_SERVICE_JSON": _SYNTHETIC_INGEST_SERVICE_JSON,
        "PROBE_JOB_JSON": _SYNTHETIC_COMBINED_JOB_JSON,
        **overrides,
    })


def cohort_mutations(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if any(COHORT in arg for arg in c)
            and any(verb in c for verb in ("deploy", "create", "update", "delete"))]


def test_standalone_creates_only_cohort_and_refresh_only_image(harness: DeployScriptHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = replace(SCRIPT_FIXTURES[FULL], env=SCRIPT_FIXTURES[FULL].env |
                      {"IMAGE": "approved-image", "RUN_SERVICE_ACCOUNT": "probe-worker"})
    failures = tuple(f for f in fixture.failures if SECRET not in f) + ("scheduler jobs describe " + COHORT,)
    monkeypatch.setitem(SCRIPT_FIXTURES, STANDALONE, replace(fixture, failures=failures))
    run = run_script(harness, STANDALONE)
    assert run.returncode == 0, summarise(run)
    mutations = [c for c in run.calls if c[0] == "gcloud" and not any(v in c for v in ("describe", "list", "get"))]
    assert len(mutations) == 4 and mutations == cohort_mutations(run.calls)
    jobs = [c for c in mutations if "run" in c]
    schedules = [c for c in mutations if "scheduler" in c]
    assert {c[c.index("deploy")+1] for c in jobs} == {COHORT+r for r in ("us-central1", "europe-west4")}
    assert len(schedules) == 2 and all("create" in c for c in schedules)
    assert all(c[c.index("--schedule")+1] == "*/4 * * * *" for c in schedules)
    for c in jobs:
        assert c[c.index("--service-account")+1] == "probe-worker"
        assert c[c.index("--tasks")+1] == c[c.index("--parallelism")+1] == "1"
        assert c[c.index("--image")+1] == fixture.env["IMAGE"]
        assert SECRET in c[c.index("--set-secrets")+1]
        assert c[c.index("--set-secrets")+1] == "/var/run/secrets/shadow-probe/api-key=" + SECRET + ":latest"
        from trusted_router.config import Settings
        env = dict(item.split("=", 1) for item in c[c.index("--set-env-vars")+1][3:].split("|"))
        cfg = Settings(**{name[3:].lower(): value for name, value in env.items() if name[3:].lower() in Settings.model_fields})
        assert not cfg.observer_internal_token and not cfg.internal_gateway_token
        assert not any("INGEST" in arg or "TR_OBSERVER_INTERNAL_TOKEN" in arg or "TR_INTERNAL_GATEWAY_TOKEN" in arg for arg in c)
        assert not any("synthetic-monitor-api-key" in arg for arg in c)
    # Same standalone command is idempotent when scheduler already exists.
    monkeypatch.setitem(SCRIPT_FIXTURES, STANDALONE, replace(fixture, failures=tuple(f for f in failures if "scheduler" not in f)))
    repeated = run_script(harness, STANDALONE)
    assert repeated.returncode == 0, summarise(repeated)
    assert len(cohort_mutations(repeated.calls)) == 4
    assert all("update" in c for c in cohort_mutations(repeated.calls) if "scheduler" in c)
    refresh_fixture = SCRIPT_FIXTURES[REFRESH]
    monkeypatch.setitem(SCRIPT_FIXTURES, REFRESH, replace(refresh_fixture, failures=tuple(
        f for f in refresh_fixture.failures if COHORT not in f)))
    refreshed = run_script(harness, REFRESH)
    assert refreshed.returncode == 0, summarise(refreshed)
    updates = cohort_mutations(refreshed.calls)
    assert len(updates) == 2
    for c in updates:
        assert c[c.index("run"):] == ["run", "jobs", "update", c[c.index("update")+1],
                                      "--region", c[c.index("--region")+1], "--image",
                                      SCRIPT_FIXTURES[REFRESH].env["IMAGE"], "--quiet"]


def test_refresh_missing_cohort_silently_skips(harness: DeployScriptHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = SCRIPT_FIXTURES[REFRESH]
    monkeypatch.setitem(SCRIPT_FIXTURES, REFRESH, replace(fixture, failures=fixture.failures+("run jobs describe "+COHORT,)))
    run = run_script(harness, REFRESH)
    assert run.returncode == 0, summarise(run)
    assert cohort_mutations(run.calls) == []
    assert COHORT not in run.stdout + run.stderr
    script = (harness.mirror / REFRESH).read_text()
    baseline = harness.write_script("scripts/deploy/refresh-baseline.sh", script.split("# Opt-in cohort:")[0])
    monkeypatch.setitem(SCRIPT_FIXTURES, baseline, fixture)
    before = run_script(harness, baseline)
    assert before.returncode == 0, summarise(before)
    assert [c for c in run.calls if not any(COHORT in arg for arg in c)] == before.calls


def test_absent_secret_preserves_main_commands(harness: DeployScriptHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    # Baseline is exactly the script before the single opt-in block. Round-2
    # report separately records byte equality against the starting HEAD (read only).
    script = (harness.mirror / FULL).read_text()
    baseline = harness.write_script("scripts/deploy/synthetic-baseline.sh", script)
    monkeypatch.setitem(SCRIPT_FIXTURES, baseline, SCRIPT_FIXTURES[FULL])
    before, after = run_script(harness, baseline), run_script(harness, FULL)
    assert before.returncode == after.returncode == 0, (summarise(before), summarise(after))
    assert [c for c in after.calls if SECRET not in c] == before.calls
    assert cohort_mutations(after.calls) == []
    monkeypatch.setitem(SCRIPT_FIXTURES, STANDALONE, SCRIPT_FIXTURES[FULL])
    absent = run_script(harness, STANDALONE)
    assert absent.returncode != 0
    assert cohort_mutations(absent.calls) == []
    assert not any("jobs" in c for c in absent.calls)



def test_missing_monitor_fails_before_any_mutation(harness, monkeypatch):
    fixture = SCRIPT_FIXTURES[FULL]
    monkeypatch.setitem(SCRIPT_FIXTURES, STANDALONE, replace(fixture, failures=tuple(f for f in fixture.failures if SECRET not in f)))
    run = run_script(harness, STANDALONE, PROBE_MONITOR="absent")
    assert run.returncode != 0
    assert all(any(verb in call for verb in ("describe", "list", "get")) for call in run.calls if call[0] == "gcloud")


@pytest.mark.parametrize("mode", ["direct", "connector"])
def test_existing_monitor_egress_is_copied_read_only(harness, monkeypatch, mode):
    import json
    fixture = SCRIPT_FIXTURES[FULL]
    monkeypatch.setitem(SCRIPT_FIXTURES, STANDALONE, replace(fixture, failures=tuple(f for f in fixture.failures if SECRET not in f)))
    job = json.loads(_SYNTHETIC_COMBINED_JOB_JSON)
    annotations = job["spec"]["template"]["spec"]["template"]["metadata"]["annotations"]
    annotations["run.googleapis.com/vpc-access-egress"] = "all-traffic"
    if mode == "direct":
        annotations["run.googleapis.com/network-interfaces"] = json.dumps([{"network": "existing-network", "subnetwork": "existing-subnet"}])
        expected = {"--network=existing-network", "--subnet=existing-subnet", "--vpc-egress=all-traffic"}
    else:
        annotations["run.googleapis.com/vpc-access-connector"] = "existing-connector"
        expected = {"--vpc-connector=existing-connector", "--vpc-egress=all-traffic"}
    run = run_script(harness, STANDALONE, PROBE_JOB_JSON=json.dumps(job))
    assert run.returncode == 0, summarise(run)
    mutations = [call for call in run.calls if call[0] == "gcloud" and not any(verb in call for verb in ("describe", "list", "get"))]
    assert len(mutations) == 4 and mutations == cohort_mutations(run.calls)
    for call in mutations:
        if "run" in call:
            assert expected <= set(call)
