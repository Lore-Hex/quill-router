"""Execution and security contracts for the Azure ClickHouse provisioner."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    ScriptFixture,
    summarise,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/deploy/azure_clickhouse.sh"
EXPECTED_IDENTITY = "/subscriptions/harness/resourceGroups/tr-azure/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tr-azure-clickhouse-identity"
EXPECTED_SUBNET = "/subscriptions/harness/resourceGroups/tr-azure/providers/Microsoft.Network/virtualNetworks/vnet-prod/subnets/snet-clickhouse"
EXPECTED_NIC = "/subscriptions/harness/resourceGroups/tr-azure/providers/Microsoft.Network/networkInterfaces/tr-clickhouse-nic"
ROLE_ID = "/subscriptions/harness/providers/Microsoft.Authorization/roleDefinitions/key-vault-secrets-user"


def _new_node_fixture() -> ScriptFixture:
    return ScriptFixture(
        responses=(
            (r"vm list-skus", '[{"name":"Standard_D2as_v7","family":"standardDASv7Family"}]'),
            (r"vm list-sizes", '[{"name":"Standard_D2as_v7","numberOfCores":2}]'),
            (
                r"vm list-usage",
                '[{"name":{"value":"standardDASv7Family"},"currentValue":0,"limit":10}]',
            ),
            (r"openssl rand", "harness-password-that-must-not-reach-argv"),
            (r"identity show.*--query id", EXPECTED_IDENTITY),
            (r"identity show.*--query principalId", "harness-principal"),
            (r"identity show.*--query clientId", "harness-client"),
            (r"keyvault show.*--query id", "/subscriptions/harness/vaults/trquillkv"),
            (r"role definition list", ROLE_ID),
            (
                r"az rest|rest --method get",
                '{"value":[{"properties":{"roleDefinitionId":"' + ROLE_ID + '"}}]}',
            ),
            (r"vm list-ip-addresses", "10.61.3.4"),
        ),
        failures=(r"vm show", r"keyvault secret show", r"nsg rule show"),
    )


def _existing_node_fixture(*, subnet: str = EXPECTED_SUBNET) -> ScriptFixture:
    return ScriptFixture(
        responses=(
            (
                r"vm show.*networkProfile.networkInterfaces",
                '{"nic":"' + EXPECTED_NIC + '","identities":["' + EXPECTED_IDENTITY + '"]}',
            ),
            (r"vnet subnet show.*--query id", EXPECTED_SUBNET),
            (r"network nic show.*--query.*subnet.id", subnet),
            (r"identity show.*--query id", EXPECTED_IDENTITY),
            (r"identity show.*--query clientId", "harness-client"),
            (r"vm run-command invoke", "__TR_RUNCMD_OK__"),
            (r"vm list-ip-addresses", "10.61.3.4"),
        ),
        failures=(r"keyvault secret show",),
    )


def _harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: ScriptFixture,
) -> DeployScriptHarness:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, fixture)
    return DeployScriptHarness(tmp_path / "harness")


def _joined_calls(run: object) -> list[str]:
    return [" ".join(call) for call in run.calls]  # type: ignore[attr-defined]


def test_new_node_applies_the_directory_derived_single_node_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, monkeypatch, _new_node_fixture())
    captured = tmp_path / "cloud-init.yaml"

    run = harness.run(SCRIPT, extra_env={"HARNESS_CAPTURE_CUSTOM_DATA": str(captured)})

    assert run.returncode == 0, summarise(run)
    expected = sorted(path.name for path in (ROOT / "clickhouse").glob("[0-9][0-9][0-9]_*_single_node.sql"))
    assert expected, "the directory must contain at least one standalone migration"
    cloud_init = captured.read_text()
    applied = re.findall(r"^\s*-- migration: (\S+)$", cloud_init, re.MULTILINE)
    assert applied == expected
    assert "--multiquery < /root/operational_schema.sql" in cloud_init
    assert '--query "SELECT 1"' in cloud_init
    assert "path: /root/bootstrap.sh" in cloud_init
    assert "runcmd:\n  - /root/bootstrap.sh" in cloud_init
    assert "chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml" in cloud_init
    assert "chmod 640 /etc/clickhouse-server/users.d/default-password.xml" in cloud_init


def test_secrets_never_enter_xtrace_or_process_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, monkeypatch, _new_node_fixture())
    captured = tmp_path / "cloud-init.yaml"

    run = harness.run(SCRIPT, extra_env={"HARNESS_CAPTURE_CUSTOM_DATA": str(captured)})

    assert run.returncode == 0, summarise(run)
    cloud_init = captured.read_text()
    assert not re.search(r"(?m)^\s*set\s+-[^\n]*x", cloud_init)
    assert "TOKEN=$(" in cloud_init and "CH_PW=$(" in cloud_init
    assert "CLICKHOUSE_PASSWORD=\"$CH_PW\" clickhouse-client" in cloud_init
    assert not re.search(r"clickhouse-client[^\n]*--password(?:=|\s)", cloud_init)

    calls = _joined_calls(run)
    secret_sets = [call for call in calls if "keyvault secret set" in call]
    assert len(secret_sets) == 1
    assert " --file " in secret_sets[0]
    assert " --value " not in secret_sets[0]
    assert "harness-password-that-must-not-reach-argv" not in "\n".join(calls)


def test_existing_node_is_validated_before_any_mutation_and_never_rotates_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, monkeypatch, _existing_node_fixture())

    run = harness.run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    calls = _joined_calls(run)
    assert not any("keyvault secret set" in call for call in calls)
    assert not any("network nsg create" in call for call in calls)
    assert not any("network vnet subnet create" in call for call in calls)
    assert not any("vm create" in call for call in calls)
    remote = "\n".join(call for call in calls if "vm run-command invoke" in call)
    assert "systemctl is-active clickhouse-server" in remote
    assert "CLICKHOUSE_PASSWORD=" in remote
    assert "system.columns" in remote
    assert "workspace_id" in remote
    assert "__TR_RUNCMD_OK__" in remote


def test_existing_node_with_wrong_subnet_refuses_and_prints_inspection_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        tmp_path,
        monkeypatch,
        _existing_node_fixture(subnet="/subscriptions/harness/subnets/wrong"),
    )

    run = harness.run(SCRIPT)

    assert run.returncode != 0
    assert "az vm show -g tr-azure -n tr-azure-clickhouse-1" in run.stderr
    assert not any("keyvault secret set" in call for call in _joined_calls(run))


def test_network_commands_enforce_private_vnet_only_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, monkeypatch, _new_node_fixture())

    run = harness.run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    calls = _joined_calls(run)
    vm_create = next(call for call in calls if "vm create" in call)
    assert '--public-ip-address ' in vm_create
    rule = next(call for call in calls if "nsg rule create" in call)
    assert "--source-address-prefixes 10.61.0.0/16" in rule
    assert "--destination-port-ranges 8123 9000" in rule

    quota = next(i for i, call in enumerate(calls) if "vm list-usage" in call)
    mutations = [
        i
        for i, call in enumerate(calls)
        if any(
            command in call
            for command in (
                "keyvault secret set",
                "nsg rule create",
                "vnet subnet create",
                "vm create",
            )
        )
    ]
    assert mutations and all(quota < mutation for mutation in mutations)
    assert any("rest --method get" in call for call in calls)
    assert not any("role assignment list" in call for call in calls)
    assert not any("role assignment create" in call for call in calls)
