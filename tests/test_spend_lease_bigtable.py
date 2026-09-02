from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    summarise,
)

ROOT = Path(__file__).parents[1]
SCRIPT = "scripts/deploy/spend_lease_ledger.sh"
CLUSTER_MAP = "us-central1=trusted-router-logs-c1,europe-west4=trusted-router-logs-eu"
VALID_TABLE_SCHEMA = json.dumps(
    {"columnFamilies": {"lease": {"gcRule": {"maxNumVersions": 1}}}},
    separators=(",", ":"),
)


def _fixture(
    *,
    table_schema: str | None,
    profiles: dict[str, tuple[str, bool]] | None = None,
    cluster_map: str = CLUSTER_MAP,
) -> ScriptFixture:
    profile_config = profiles or {}
    responses: list[tuple[str, str]] = []
    failures: list[str] = []
    if table_schema is None:
        failures.append(r"bigtable instances tables describe trustedrouter-spend-lease")
        failures.append(r"bigtable app-profiles describe tr-spend-")
    else:
        responses.append(
            (
                r"bigtable instances tables describe trustedrouter-spend-lease "
                r".*--format=json",
                table_schema,
            )
        )
        for region, (cluster, transactional) in profile_config.items():
            responses.append(
                (
                    rf"bigtable app-profiles describe tr-spend-{re.escape(region)} "
                    r".*--format=value\(",
                    f"{cluster} {'True' if transactional else 'False'}",
                )
            )
    return ScriptFixture(
        env={
            "GCP_PROJECT_ID": "harness-project",
            "TR_BIGTABLE_INSTANCE_ID": "harness-instance",
            "TR_SPEND_LEASE_CLUSTER_MAP": cluster_map,
        },
        responses=tuple(responses),
        failures=tuple(failures),
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    table_schema: str | None,
    profiles: dict[str, tuple[str, bool]] | None = None,
    cluster_map: str = CLUSTER_MAP,
) -> HarnessRun:
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        _fixture(
            table_schema=table_schema,
            profiles=profiles,
            cluster_map=cluster_map,
        ),
    )
    return DeployScriptHarness(tmp_path / "harness").run(SCRIPT)


def _gcloud_calls(run: HarnessRun, *prefix: str) -> list[list[str]]:
    expected = ["gcloud", *prefix]
    return [call for call in run.calls if call[: len(expected)] == expected]


def _assert_workflow_order(workflow: str) -> None:
    migrate_schema = workflow.split("\n  migrate-schema:\n", 1)[1].split(
        "\n  sync-runtime-secrets:\n", 1
    )[0]
    ledger_scripts = re.findall(
        r"^        run: bash scripts/deploy/([a-z_]+_ledger\.sh)$",
        migrate_schema,
        re.MULTILINE,
    )
    regional = ledger_scripts.index("regional_quota_ledger.sh")
    assert ledger_scripts[regional + 1] == "spend_lease_ledger.sh"


def _assert_orchestrator_order(orchestrator: str) -> None:
    deploy_scripts = re.findall(r'bash "\$\{SCRIPT_DIR\}/deploy/([^" ]+\.sh)"', orchestrator)
    regional = deploy_scripts.index("regional_quota_ledger.sh")
    assert deploy_scripts[regional + 1] == "spend_lease_ledger.sh"


def test_fresh_run_creates_latest_version_only_family_without_maxage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, monkeypatch, table_schema=None)

    assert run.returncode == 0, summarise(run)
    assert os.access(ROOT / SCRIPT, os.X_OK)
    assert _gcloud_calls(run, "bigtable", "instances", "tables", "create") == [
        [
            "gcloud",
            "bigtable",
            "instances",
            "tables",
            "create",
            "trustedrouter-spend-lease",
            "--project=harness-project",
            "--instance=harness-instance",
            "--column-families=lease:maxversions=1",
        ]
    ]


def test_fresh_run_creates_transactional_profile_for_each_mapped_cluster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, monkeypatch, table_schema=None)

    assert run.returncode == 0, summarise(run)
    creates = _gcloud_calls(run, "bigtable", "app-profiles", "create")
    assert creates == [
        [
            "gcloud",
            "bigtable",
            "app-profiles",
            "create",
            "tr-spend-us-central1",
            "--project=harness-project",
            "--instance=harness-instance",
            "--route-to=trusted-router-logs-c1",
            "--transactional-writes",
            "--description=TrustedRouter spend-lease escrow for us-central1",
        ],
        [
            "gcloud",
            "bigtable",
            "app-profiles",
            "create",
            "tr-spend-europe-west4",
            "--project=harness-project",
            "--instance=harness-instance",
            "--route-to=trusted-router-logs-eu",
            "--transactional-writes",
            "--description=TrustedRouter spend-lease escrow for europe-west4",
        ],
    ]


def test_existing_profile_routed_to_another_cluster_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(
        tmp_path,
        monkeypatch,
        table_schema=VALID_TABLE_SCHEMA,
        profiles={"us-central1": ("wrong-cluster", True)},
        cluster_map="us-central1=trusted-router-logs-c1",
    )

    assert run.returncode == 1, summarise(run)
    assert "refusing spend-lease profile drift" in run.stdout
    assert not _gcloud_calls(run, "bigtable", "app-profiles", "create")


def test_existing_profile_without_transactional_writes_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(
        tmp_path,
        monkeypatch,
        table_schema=VALID_TABLE_SCHEMA,
        profiles={"us-central1": ("trusted-router-logs-c1", False)},
        cluster_map="us-central1=trusted-router-logs-c1",
    )

    assert run.returncode == 1, summarise(run)
    assert "refusing spend-lease profile drift" in run.stdout
    assert not _gcloud_calls(run, "bigtable", "app-profiles", "create")


def test_existing_table_with_age_based_gc_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = json.dumps(
        {
            "columnFamilies": {
                "lease": {
                    "gcRule": {
                        "union": {
                            "rules": [
                                {"maxNumVersions": 1},
                                {"maxAge": "604800s"},
                            ]
                        }
                    }
                }
            }
        },
        separators=(",", ":"),
    )
    run = _run(tmp_path, monkeypatch, table_schema=schema)

    assert run.returncode == 1, summarise(run)
    assert "lease must use maxversions=1 with no maxage" in run.stdout
    assert not _gcloud_calls(run, "bigtable", "app-profiles")


def test_malformed_cluster_map_entry_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(
        tmp_path,
        monkeypatch,
        table_schema=VALID_TABLE_SCHEMA,
        cluster_map="malformed",
    )

    assert run.returncode == 2, summarise(run)
    assert "invalid cluster-map entry: malformed" in run.stdout


def test_second_run_creates_nothing_and_prints_profile_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(
        tmp_path,
        monkeypatch,
        table_schema=VALID_TABLE_SCHEMA,
        profiles={
            "us-central1": ("trusted-router-logs-c1", True),
            "europe-west4": ("trusted-router-logs-eu", True),
        },
    )

    assert run.returncode == 0, summarise(run)
    assert not _gcloud_calls(run, "bigtable", "instances", "tables", "create")
    assert not _gcloud_calls(run, "bigtable", "app-profiles", "create")
    assert (
        "set TR_SPEND_LEASE_BIGTABLE_APP_PROFILES="
        "us-central1=tr-spend-us-central1,"
        "europe-west4=tr-spend-europe-west4"
    ) in run.stdout


def test_deploy_workflow_does_not_run_spend_lease_provisioning_until_iam_granted() -> None:
    """2026-09-02: the migrate-schema service account lacks bigtable.tables.create, so the
    workflow must not invoke spend_lease_ledger.sh until that grant lands (see the comment
    beside the regional quota step). deploy-gcp.sh remains the manual provisioning path."""

    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    run_lines = [line for line in workflow.splitlines() if line.strip().startswith("run:")]
    assert not any("spend_lease_ledger.sh" in line for line in run_lines)
    assert "bigtable.tables.create" in workflow


def test_deploy_gcp_invokes_spend_lease_immediately_after_regional_quota() -> None:
    orchestrator = (ROOT / "scripts/deploy-gcp.sh").read_text()

    _assert_orchestrator_order(orchestrator)
    assert "TR_SPEND_LEASE_CLUSTER_MAP=" not in orchestrator


def test_deploy_lib_defaults_spend_lease_cluster_map_and_honours_override() -> None:
    command = (
        'gcloud() { printf "123456\\n"; }; '
        "source scripts/deploy/_lib.sh; "
        'printf "%s\\n%s\\n" '
        '"$TR_REGIONAL_QUOTA_CLUSTER_MAP" "$TR_SPEND_LEASE_CLUSTER_MAP"'
    )

    defaulted = subprocess.run(  # noqa: S603 - fixed shell and repo-owned script
        ["/bin/bash", "-c", command],
        cwd=ROOT,
        env={
            **os.environ,
            "TR_REGIONAL_QUOTA_CLUSTER_MAP": "us-test1=quota-cluster",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    overridden = subprocess.run(  # noqa: S603 - fixed shell and repo-owned script
        ["/bin/bash", "-c", command],
        cwd=ROOT,
        env={
            **os.environ,
            "TR_REGIONAL_QUOTA_CLUSTER_MAP": "us-test1=quota-cluster",
            "TR_SPEND_LEASE_CLUSTER_MAP": "eu-test1=spend-cluster",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert defaulted.returncode == 0, defaulted.stderr
    assert defaulted.stdout.splitlines() == [
        "us-test1=quota-cluster",
        "us-test1=quota-cluster",
    ]
    assert overridden.returncode == 0, overridden.stderr
    assert overridden.stdout.splitlines() == [
        "us-test1=quota-cluster",
        "eu-test1=spend-cluster",
    ]
