from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "deploy" / "cloud_bake_gate.sh"
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"

_CLOUD_STUB = r'''#!/usr/bin/env bash
printf '%s\t%s\n' "${0##*/}" "$*" >>"${BAKE_STUB_CALLS}"

case "${0##*/}" in
  gcloud)
    [ "${BAKE_GCP_FAIL:-0}" = "0" ] || exit 1
    if [ "$1 $2 $3" = "run services describe" ]; then
      printf '{"spec":{"template":{"spec":{"containers":[{"image":"repo:%s"}]}}},"status":{"traffic":[{"revisionName":"gcp-traffic-revision","percent":100}]}}\n' \
        "${BAKE_GCP_TEMPLATE_SHA}"
    elif [ "$1 $2 $3" = "run revisions describe" ]; then
      printf 'us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router:%s\n' \
        "${BAKE_GCP_SHA}"
    else
      exit 2
    fi
    ;;
  az)
    [ "${BAKE_AZURE_FAIL:-0}" = "0" ] || exit 1
    if [ "$1 $2 $3" = "containerapp revision list" ]; then
      printf '[{"name":"active-rollback","properties":{"createdTime":"2026-08-23T00:00:00Z","trafficWeight":100,"healthState":"Healthy","template":{"containers":[{"image":"trazureuaenorthacr.azurecr.io/trusted-router@sha256:%064d","env":[{"name":"TR_RELEASE","value":"%s"}]}]}}},{"name":"new-template","properties":{"createdTime":"2026-08-24T00:00:00Z","trafficWeight":0,"healthState":"Healthy","template":{"containers":[{"image":"repo:%s","env":[{"name":"TR_RELEASE","value":"%s"}]}]}}},{"name":"unhealthy-traffic","properties":{"createdTime":"2026-08-25T00:00:00Z","trafficWeight":100,"healthState":"Unhealthy","template":{"containers":[{"image":"repo:%s","env":[{"name":"TR_RELEASE","value":"%s"}]}]}}}]\n' \
        0 "${BAKE_AZURE_SHA}" "${BAKE_AZURE_TEMPLATE_SHA}" "${BAKE_AZURE_TEMPLATE_SHA}" \
        "${BAKE_AZURE_TEMPLATE_SHA}" "${BAKE_AZURE_TEMPLATE_SHA}"
    else
      exit 2
    fi
    ;;
  aws)
    [ "${BAKE_AWS_FAIL:-0}" = "0" ] || exit 1
    if [ "$1 $2" = "apprunner list-services" ]; then
      printf '%s\n' \
        'arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu/test-service-id'
    elif [ "$1 $2" = "apprunner describe-service" ]; then
      if [[ " $* " == *"Service.Status"* ]]; then
        printf '%s\n' "${BAKE_AWS_SERVICE_STATUS:-RUNNING}"
      elif [[ " $* " == *"ImageIdentifier"* ]]; then
        printf '330422590279.dkr.ecr.eu-west-3.amazonaws.com/trusted-router@sha256:%064d\n' 0
      else
        printf '%s\n' "${BAKE_AWS_SHA}"
      fi
    elif [ "$1 $2" = "apprunner list-operations" ]; then
      printf '%s\n' "${BAKE_AWS_OPERATION_STATUS:-SUCCEEDED}"
    else
      exit 2
    fi
    ;;
  curl)
    [ "${BAKE_STATUS_FAIL:-0}" = "0" ] || exit 22
    printf '{"data":{"overall_status":"%s"}}\n' "${BAKE_STATUS:-up}"
    ;;
  *) exit 2 ;;
esac
'''


@dataclass
class BakeRepo:
    path: Path
    bin_dir: Path
    calls: Path
    base_sha: str
    candidate_sha: str

    def short(self, sha: str) -> str:
        return self.git("rev-parse", "--short", sha).stdout.strip()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed git operation in a temp repo
            [GIT, "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def commit(self, subject: str, *, hours_ago: int) -> str:
        marker = self.path / "history.txt"
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(subject + "\n")
        self.git("add", "history.txt")
        timestamp = int(time.time()) - hours_ago * 3600
        commit_env = {
            **os.environ,
            "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
        }
        subprocess.run(  # noqa: S603 - fixed git operation in a temp repo
            [GIT, "-C", str(self.path), "commit", "-m", subject],
            check=True,
            capture_output=True,
            text=True,
            env=commit_env,
        )
        return self.git("rev-parse", "HEAD").stdout.strip()

    def push_main(self) -> None:
        self.git("push", "--force", "origin", "main")

    def run(
        self,
        *,
        mode: str,
        target: str = "azure",
        gcp: str | None = None,
        azure: str | None = None,
        aws: str | None = None,
        status: str = "up",
        override: str | None = None,
        bake_hours: str = "24",
        fail_gcp: bool = False,
        fail_azure: bool = False,
        fail_aws: bool = False,
        gcp_template: str | None = None,
        azure_template: str | None = None,
        aws_service_status: str = "RUNNING",
        aws_operation_status: str = "SUCCEEDED",
    ) -> subprocess.CompletedProcess[str]:
        values = {
            "gcp": self.candidate_sha if gcp is None else gcp,
            "azure": self.candidate_sha if azure is None else azure,
            "aws": self.candidate_sha if aws is None else aws,
        }
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "BAKE_STUB_CALLS": str(self.calls),
            "BAKE_GCP_SHA": self.short(values["gcp"]),
            "BAKE_GCP_TEMPLATE_SHA": self.short(
                values["gcp"] if gcp_template is None else gcp_template
            ),
            "BAKE_AZURE_SHA": self.short(values["azure"]),
            "BAKE_AZURE_TEMPLATE_SHA": self.short(
                values["azure"] if azure_template is None else azure_template
            ),
            "BAKE_AWS_SHA": self.short(values["aws"]),
            "BAKE_GCP_FAIL": str(int(fail_gcp)),
            "BAKE_AZURE_FAIL": str(int(fail_azure)),
            "BAKE_AWS_FAIL": str(int(fail_aws)),
            "BAKE_AWS_SERVICE_STATUS": aws_service_status,
            "BAKE_AWS_OPERATION_STATUS": aws_operation_status,
            "BAKE_STATUS": status,
            "TR_CLOUD_DEPLOY_MODE": mode,
            "TR_CLOUD_BAKE_HOURS": bake_hours,
        }
        if override is not None:
            env["TR_CLOUD_BAKE_OVERRIDE"] = override
        return subprocess.run(  # noqa: S603 - repo-local script with stubbed network CLIs
            [BASH, str(self.path / "scripts/deploy/cloud_bake_gate.sh"), target],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.path,
            env=env,
        )


@pytest.fixture
def bake_repo(tmp_path: Path) -> BakeRepo:
    repo = tmp_path / "repo"
    remote = tmp_path / "origin.git"
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "calls.tsv"
    (repo / "scripts/deploy").mkdir(parents=True)
    bin_dir.mkdir()
    calls.write_text("", encoding="utf-8")
    shutil.copy2(GATE, repo / "scripts/deploy/cloud_bake_gate.sh")
    shutil.copy2(
        ROOT / "scripts/deploy/resolve_active_revision.py",
        repo / "scripts/deploy/resolve_active_revision.py",
    )

    subprocess.run(  # noqa: S603,S607 - isolated test repository
        [GIT, "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603,S607 - isolated bare test remote
        [GIT, "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    harness = BakeRepo(repo, bin_dir, calls, "", "")
    harness.git("config", "user.email", "bake-gate@example.test")
    harness.git("config", "user.name", "Bake Gate Test")
    base = harness.commit("old production base", hours_ago=100)
    candidate = harness.commit("candidate under test", hours_ago=1)
    harness.base_sha = base
    harness.candidate_sha = candidate
    harness.git("remote", "add", "origin", str(remote))
    harness.git("push", "-u", "origin", "main")

    stub = bin_dir / "cloud-stub"
    stub.write_text(_CLOUD_STUB, encoding="utf-8")
    stub.chmod(0o755)
    for name in ("gcloud", "az", "aws", "curl"):
        (bin_dir / name).symlink_to(stub)
    return harness


def test_promote_fresh_candidate_is_refused_with_wait_remaining(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(mode="promote")

    assert result.returncode == 1
    assert "candidate commit age: INFO" in result.stderr
    assert "candidate merged age: FAIL" in result.stderr
    assert "wait_remaining_hours=" in result.stderr
    assert "production lineage: PASS" in result.stderr
    assert "cloud bake gate: REFUSED" in result.stderr


def test_promote_baked_ancestor_passes(bake_repo: BakeRepo) -> None:
    baked = bake_repo.commit("baked candidate", hours_ago=73)
    descendant = bake_repo.commit("currently serving descendant", hours_ago=1)
    bake_repo.push_main()
    bake_repo.candidate_sha = baked
    bake_repo.git("checkout", "--detach", baked)

    result = bake_repo.run(mode="promote", gcp=descendant)

    assert result.returncode == 0, result.stderr
    assert "candidate merged age: PASS" in result.stderr
    assert "production lineage: PASS" in result.stderr
    assert "fleet health: PASS" in result.stderr


def test_promote_baked_non_ancestor_is_refused(bake_repo: BakeRepo) -> None:
    baked = bake_repo.commit("old candidate", hours_ago=73)
    bake_repo.push_main()
    bake_repo.candidate_sha = baked

    result = bake_repo.run(
        mode="promote",
        gcp=bake_repo.base_sha,
        azure=bake_repo.base_sha,
        aws=bake_repo.base_sha,
    )

    assert result.returncode == 1
    assert "candidate merged age: PASS" in result.stderr
    assert "production lineage: FAIL" in result.stderr


def test_backdated_branch_tip_merged_now_is_not_treated_as_baked(
    bake_repo: BakeRepo,
) -> None:
    bake_repo.git("checkout", "-b", "backdated-branch")
    backdated = bake_repo.commit("backdated branch tip", hours_ago=73)
    bake_repo.git("checkout", "main")
    subprocess.run(  # noqa: S603 - fixed git operation in a temp repo
        [GIT, "-C", str(bake_repo.path), "merge", "--no-ff", "backdated-branch", "-m", "merge now"],
        check=True,
        capture_output=True,
        text=True,
    )
    serving = bake_repo.git("rev-parse", "HEAD").stdout.strip()
    bake_repo.push_main()
    bake_repo.candidate_sha = backdated
    bake_repo.git("checkout", "--detach", backdated)

    result = bake_repo.run(mode="promote", gcp=serving)

    assert result.returncode == 1
    assert "candidate commit age: INFO" in result.stderr
    assert "candidate merged age: FAIL first contained by origin/main" in result.stderr
    assert "wait_remaining_hours=" in result.stderr
    assert "production lineage: PASS" in result.stderr


def test_promote_fails_closed_when_main_has_no_threshold_old_commit(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(mode="promote", bake_hours="720")

    assert result.returncode == 1
    assert "origin/main has no commit at least 720h old" in result.stderr


def test_canary_fresh_candidate_passes_with_baked_other_cloud(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="azure",
        aws=bake_repo.base_sha,
    )

    assert result.returncode == 0, result.stderr
    assert f"aws  {bake_repo.short(bake_repo.base_sha)}" in result.stderr
    assert "LIFEBOAT" in result.stderr
    assert "lifeboat: PASS aws" in result.stderr
    assert "CANARY DEPLOY: azure will serve FRESH commit" in result.stderr


def test_canary_refuses_gcp_as_the_only_baked_lifeboat(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="azure",
        gcp=bake_repo.base_sha,
    )

    assert result.returncode == 1
    assert "fresh-exempt (gcp auto-deploys; ineligible as lifeboat)" in result.stderr
    assert "lifeboat: FAIL" in result.stderr


def test_canary_accepts_baked_azure_as_a_gated_lifeboat(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="aws",
        azure=bake_repo.base_sha,
    )

    assert result.returncode == 0, result.stderr
    assert "lifeboat: PASS azure" in result.stderr


def test_canary_is_refused_when_all_other_clouds_are_fresh(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(mode="canary", target="azure")

    assert result.returncode == 1
    assert "gcp" in result.stderr and "fresh" in result.stderr
    assert "lifeboat: FAIL" in result.stderr


def test_canary_is_refused_when_other_clouds_are_unknown(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="azure",
        fail_gcp=True,
        fail_aws=True,
    )

    assert result.returncode == 1
    assert "gcp serving commit: UNKNOWN" in result.stderr
    assert "aws serving commit: UNKNOWN" in result.stderr
    assert "lifeboat: FAIL" in result.stderr


def test_ref_named_like_another_commit_sha_cannot_shadow_resolution(
    bake_repo: BakeRepo,
) -> None:
    advertised = bake_repo.short(bake_repo.base_sha)
    bake_repo.git("update-ref", f"refs/heads/{advertised}", bake_repo.candidate_sha)

    result = bake_repo.run(
        mode="canary",
        target="azure",
        aws=bake_repo.base_sha,
    )

    assert result.returncode == 1
    assert f"aws serving commit: UNKNOWN tag={advertised}" in result.stderr
    assert "possible ref shadowing" in result.stderr
    assert "lifeboat: FAIL" in result.stderr


@pytest.mark.parametrize("mode", ["promote", "canary"])
def test_status_down_refuses_both_modes(bake_repo: BakeRepo, mode: str) -> None:
    baked = bake_repo.commit("healthy age candidate", hours_ago=73)
    bake_repo.push_main()
    bake_repo.candidate_sha = baked
    result = bake_repo.run(
        mode=mode,
        target="azure",
        gcp=baked,
        aws=baked,
        status="down",
    )

    assert result.returncode == 1
    assert result.stderr.count("overall_status=down") == 2
    assert "fleet health: FAIL" in result.stderr
    curl_calls = [
        line for line in bake_repo.calls.read_text().splitlines() if line.startswith("curl\t")
    ]
    assert len(curl_calls) == 2
    assert all("--max-time 10" in call for call in curl_calls)


def test_override_logs_reason_after_real_results_and_proceeds(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="promote",
        status="down",
        override="INC-742 restore service",
    )

    assert result.returncode == 0, result.stderr
    age = result.stderr.index("candidate merged age: FAIL")
    health = result.stderr.index("fleet health: FAIL")
    override = result.stderr.index("CLOUD BAKE OVERRIDE")
    assert age < health < override
    assert "reason: INC-742 restore service" in result.stderr


def test_empty_override_does_not_bypass_a_failure(bake_repo: BakeRepo) -> None:
    result = bake_repo.run(mode="promote", override="")

    assert result.returncode == 1
    assert "CLOUD BAKE OVERRIDE" not in result.stderr
    assert "requires a non-empty reason" in result.stderr


@pytest.mark.parametrize("value", ["0", "721", "not-a-number"])
def test_bake_hour_bounds_are_enforced(bake_repo: BakeRepo, value: str) -> None:
    result = bake_repo.run(mode="canary", bake_hours=value)

    assert result.returncode == 2
    assert "invalid_bake_hours" in result.stderr


@pytest.mark.parametrize("value", ["1", "720"])
def test_bake_hour_boundaries_are_accepted(bake_repo: BakeRepo, value: str) -> None:
    result = bake_repo.run(
        mode="canary",
        bake_hours=value,
        gcp=bake_repo.base_sha,
    )

    assert result.returncode != 2
    assert "invalid_bake_hours" not in result.stderr


def test_discovery_reads_the_deploy_scripts_production_resources(
    bake_repo: BakeRepo,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="azure",
        gcp=bake_repo.base_sha,
        aws=bake_repo.base_sha,
    )

    assert result.returncode == 0, result.stderr
    calls = bake_repo.calls.read_text(encoding="utf-8")
    assert (
        "gcloud\trun services describe trusted-router --region us-central1 "
        "--project quill-cloud-proxy "
        "--format=json"
    ) in calls
    assert (
        "gcloud\trun revisions describe gcp-traffic-revision "
        "--region us-central1 --project quill-cloud-proxy "
        "--format=value(spec.containers[0].image)"
    ) in calls
    assert (
        "az\tcontainerapp revision list --resource-group tr-azure "
        "--name tr-azure-vnet --output json"
    ) in calls
    assert "az\tcontainerapp show" not in calls
    assert "aws\tapprunner list-services --region eu-west-3" in calls
    assert "ServiceName=='tr-eu'" in calls
    assert "Service.Status" in calls
    assert "aws\tapprunner list-operations --region eu-west-3" in calls
    assert "OperationSummaryList[0].Status" in calls
    assert "ImageIdentifier" in calls
    assert "RuntimeEnvironmentVariables.TR_RELEASE" in calls


def test_gcp_rollback_reports_the_revision_carrying_traffic_not_template(
    bake_repo: BakeRepo,
) -> None:
    candidate = bake_repo.commit("old promoted candidate", hours_ago=73)
    bake_repo.push_main()
    bake_repo.candidate_sha = candidate

    result = bake_repo.run(
        mode="promote",
        gcp=bake_repo.base_sha,
        azure=bake_repo.base_sha,
        aws=bake_repo.base_sha,
        gcp_template=candidate,
    )

    assert result.returncode == 1
    assert "candidate merged age: PASS" in result.stderr
    assert "production lineage: FAIL" in result.stderr
    assert f"gcp  {bake_repo.short(bake_repo.base_sha)}" in result.stderr
    calls = bake_repo.calls.read_text(encoding="utf-8")
    assert "run revisions describe gcp-traffic-revision" in calls
    assert "spec.template.spec.containers[0].image" not in calls


def test_azure_rollback_reports_healthy_traffic_revision_not_app_template(
    bake_repo: BakeRepo,
) -> None:
    candidate = bake_repo.commit("old azure candidate", hours_ago=73)
    bake_repo.push_main()
    bake_repo.candidate_sha = candidate

    result = bake_repo.run(
        mode="promote",
        gcp=bake_repo.base_sha,
        azure=bake_repo.base_sha,
        aws=bake_repo.base_sha,
        azure_template=candidate,
    )

    assert result.returncode == 1
    assert "candidate merged age: PASS" in result.stderr
    assert "production lineage: FAIL" in result.stderr
    assert f"azure  {bake_repo.short(bake_repo.base_sha)}" in result.stderr
    calls = bake_repo.calls.read_text(encoding="utf-8")
    assert "az\tcontainerapp revision list" in calls
    assert "az\tcontainerapp show" not in calls


@pytest.mark.parametrize(
    ("service_status", "operation_status"),
    (("OPERATION_IN_PROGRESS", "SUCCEEDED"), ("RUNNING", "IN_PROGRESS")),
)
def test_aws_discovery_requires_running_service_and_succeeded_latest_operation(
    bake_repo: BakeRepo,
    service_status: str,
    operation_status: str,
) -> None:
    result = bake_repo.run(
        mode="canary",
        target="azure",
        aws=bake_repo.base_sha,
        aws_service_status=service_status,
        aws_operation_status=operation_status,
    )

    assert result.returncode == 1
    assert "aws serving commit: UNKNOWN" in result.stderr
    assert "lifeboat: FAIL" in result.stderr


@pytest.mark.parametrize(
    ("relative", "cloud", "first_mutation"),
    (
        (
            "scripts/deploy/aws_eu_control_plane.sh",
            "aws",
            "docker buildx build",
        ),
        (
            "scripts/deploy/azure_control_plane.sh",
            "azure",
            'az acr build --registry "$ACR"',
        ),
    ),
)
def test_operator_deploy_calls_bake_gate_after_mutex_before_first_mutation(
    relative: str,
    cloud: str,
    first_mutation: str,
) -> None:
    script = (ROOT / relative).read_text(encoding="utf-8")

    source = script.index('source "${SCRIPT_DIR}/cloud_bake_gate.sh"')
    mutex = script.index("deploy_mutex_acquire")
    gate = script.index(f"cloud_bake_gate {cloud}")
    mutation = script.index(first_mutation)

    assert source < mutex < gate < mutation
    assert re.search(rf"(?m)^cloud_bake_gate {cloud}$", script)


def test_azure_canary_app_refuses_production_names_without_bake_override() -> None:
    script = (ROOT / "scripts/deploy/azure_canary_app.sh").read_text(encoding="utf-8")

    guard = script.index('if [ "$RG" = "tr-azure" ] || [[ "$APP" == *-vnet ]]')
    first_cloud_read = script.index("PG_HOST=")
    assert guard < first_cloud_read
    assert "scripts/deploy/azure_control_plane.sh" in script[guard:first_cloud_read]
    assert "TR_CLOUD_BAKE_OVERRIDE" in script[guard:first_cloud_read]


def test_gate_is_sourceable_without_executing_on_source(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603,S607 - source-only shell assertion
        [BASH, "-c", f'source "{GATE}"; declare -F cloud_bake_gate'],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert "cloud_bake_gate" in result.stdout
