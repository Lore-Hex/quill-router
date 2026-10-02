"""The Azure deploy renews its login after the mutex wait, and says why a lookup failed.

On 2026-10-02 an Azure deploy waited 17 minutes for the deploy mutex. azure/login
had left az holding the job's GitHub OIDC ID token, which expires about five
minutes after login, so the first Key Vault call afterwards could not get a
token. The lookup discarded its error and the deploy refused with "secret: not
found" although the secret existed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from .deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, summarise

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/deploy/azure_control_plane.sh"
ACTIONS_ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/request?api-version=2.0",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "harness-request-token",
    "AZURE_CLIENT_ID": "harness-client",
    "AZURE_TENANT_ID": "harness-tenant",
    "AZURE_SUBSCRIPTION_ID": "harness-subscription",
}
OIDC_RESPONSE = (r"curl .*audience=api://AzureADTokenExchange", '{"value":"harness-oidc-token"}')


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, env=None, responses=(), failures=()):
    base = SCRIPT_FIXTURES[SCRIPT]
    fixture = dataclasses.replace(
        base,
        responses=tuple(responses) + base.responses,
        failures=tuple(failures) + base.failures,
    )
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, fixture)
    return DeployScriptHarness(tmp_path / "harness").run(SCRIPT, extra_env=env)


def _joined(run) -> list[str]:
    return [" ".join(call) for call in run.calls]


def _first(calls: list[str], prefix: str) -> int:
    return next(index for index, call in enumerate(calls) if call.startswith(prefix))


def test_in_actions_the_login_is_renewed_once_the_lease_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path, monkeypatch, env=ACTIONS_ENV, responses=(OIDC_RESPONSE,))

    assert run.returncode == 0, summarise(run)
    calls = _joined(run)
    lease = _first(calls, "gcloud storage cp ")
    login = calls.index(
        "az login --service-principal --username harness-client --tenant harness-tenant "
        "--federated-token harness-oidc-token --allow-no-subscriptions --output none"
    )
    account = calls.index("az account set --subscription harness-subscription")
    first_other_az = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("az ") and not call.startswith(("az login ", "az account set "))
    )
    # The renewal comes after the lease is written and before every other az
    # call, so no Azure read runs on the stale credential.
    assert lease < login < account < first_other_az
    token_requests = [call for call in calls if call.startswith("curl ")]
    assert any("audience=api://AzureADTokenExchange" in call for call in token_requests)
    # The request token travels in a header, never in the URL.
    assert all("harness-request-token" not in call.split()[-1] for call in token_requests)


def test_outside_actions_the_operator_login_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path, monkeypatch)

    assert run.returncode == 0, summarise(run)
    assert not any(call.startswith("az login") for call in _joined(run))


def test_in_actions_a_missing_identity_stops_before_any_azure_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {key: value for key, value in ACTIONS_ENV.items() if key != "AZURE_CLIENT_ID"}
    run = _run(tmp_path, monkeypatch, env=env, responses=(OIDC_RESPONSE,))

    assert run.returncode != 0
    assert "AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_SUBSCRIPTION_ID are required" in run.stderr
    assert not any(call.startswith("az ") for call in _joined(run))


def test_a_failed_key_vault_lookup_reports_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "ERROR: AADSTS700024: Client assertion is not within its valid time range."
    run = _run(tmp_path, monkeypatch, failures=(f"keyvault secret show\t{message}",))

    assert run.returncode != 0
    assert "Azure analytics discovery was incomplete" in run.stderr
    assert f"tr-azure-clickhouse-password: not found (az: {message}" in run.stderr
    # The lookups that worked say nothing extra.
    assert "private IP   VM tr-azure-clickhouse-1 in resource group tr-azure: 10.61.3.4\n" in run.stderr


def test_the_workflow_hands_the_script_the_login_identity() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-azure-control-plane.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    login = next(step for step in steps if step.get("uses", "").startswith("azure/login@"))
    deploy = next(step for step in steps if step.get("name") == "Deploy")
    assert deploy["env"]["AZURE_CLIENT_ID"] == login["with"]["client-id"]
    assert deploy["env"]["AZURE_TENANT_ID"] == login["with"]["tenant-id"]
    assert deploy["env"]["AZURE_SUBSCRIPTION_ID"] == login["with"]["subscription-id"]
    assert workflow["permissions"]["id-token"] == "write"
