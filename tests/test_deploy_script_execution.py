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
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from trusted_router import cloud_rollout_completeness as crc
from trusted_router.config import Settings

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
    kwargs = {
        env_name.removeprefix("TR_").lower(): value
        for env_name, value in raw_env.items()
    }
    # Container Apps resolves these references before starting the process.
    kwargs["attribution_cookie_secret"] = "a" * 64
    kwargs["postgres_dsn"] = "postgresql://canary.invalid/trustedrouter"
    return Settings(**kwargs)


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
        call
        for call in run.calls
        if call[:3] == ["aws", "apprunner", "update-service"]
    ]
    assert len(service_updates) == 1
    service_config = service_updates[0][service_updates[0].index("--source-configuration") + 1]
    assert '"TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS": "0"' in service_config
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
    assert scheduler_rule[scheduler_rule.index("--schedule-expression") + 1] == (
        "rate(2 minutes)"
    )
    scheduler_targets = [
        call
        for call in run.calls
        if call[:3] == ["aws", "events", "put-targets"]
    ]
    assert len(scheduler_targets) == 1
    targets = json.loads(scheduler_targets[0][scheduler_targets[0].index("--targets") + 1])
    assert len(targets) == 1
    scheduled_body = json.loads(targets[0]["Input"])
    assert scheduled_body == {
        "monitor_region": "eu-west-3",
        "rotation_count": 8,
        "run_remediator": True,
        "detach": True,
    }
    assert waf_attach_index < scheduler_index


def test_aws_observer_initial_create_reaches_running_postconditions_waf_and_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "scripts/deploy/aws_eu_control_plane.sh"
    fixture = SCRIPT_FIXTURES[script]
    service_arn = (
        "arn:aws:apprunner:eu-west-3:330422590279:"
        "service/tr-eu/harness-service-id"
    )
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
    assert not any(
        call[:3] == ["aws", "apprunner", "update-service"] for call in run.calls
    )
    assert create[create.index("--auto-scaling-configuration-arn") + 1].startswith(
        "arn:aws:apprunner:eu-west-3:330422590279:"
        "autoscalingconfiguration/tr-eu-observer-bounded/"
    )
    assert create[create.index("--health-check-configuration") + 1].startswith(
        "Protocol=TCP"
    )

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
        index
        for index, call in enumerate(run.calls)
        if call[:3] == ["aws", "events", "put-rule"]
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
        *(
            response
            for response in fixture.responses
            if "Service\\.Status" not in response[0]
        ),
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
    assert not any(call[:2] == ["aws", "apprunner"] for call in run.calls)
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
    assert not any(call[:2] == ["aws", "apprunner"] for call in run.calls)
    assert not run.verifier_calls


@pytest.mark.parametrize(
    ("fixture_pattern", "drift_value", "query_fragment"),
    [
        (
            r"apprunner describe-service.*AutoScalingConfigurationSummary",
            "arn:aws:apprunner:eu-west-3:330422590279:"
            "autoscalingconfiguration/unbounded/1/drift",
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
        *(
            response
            for response in fixture.responses
            if query_fragment not in response[0]
        ),
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
    assert not any(
        call[:3] == ["aws", "wafv2", "associate-web-acl"] for call in run.calls
    )
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
        if call[:3] in (
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
        assert (
            "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret"
            in mutation_text
        )
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
    ("script", "app_name", "password_file", "expected_max"),
    [
        (
            "scripts/deploy/azure_control_plane.sh",
            "tr-azure",
            ".config/tr-azure/pgpw",
            "1",
        ),
        (
            "scripts/deploy/azure_canary_app.sh",
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
            failures=(
                rf"^az containerapp show -g {app_name} -n {app_name}$",
            ),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / f"azure-create-{expected_max}")

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    creates = [
        call
        for call in run.calls
        if call[:3] == ["az", "containerapp", "create"]
    ]
    assert len(creates) == 1
    assert not any(
        call[:3] == ["az", "containerapp", "update"] for call in run.calls
    )
    create = creates[0]
    create_index = run.calls.index(create)
    assert create[create.index("--min-replicas") + 1] == "1"
    assert create[create.index("--max-replicas") + 1] == expected_max
    assert create[create.index("--scale-rule-http-concurrency") + 1] == "10"
    if script.endswith("azure_canary_app.sh"):
        create_text = " ".join(create)
        assert "TR_SERVICE_SURFACE=public" in create_text
        assert (
            "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret"
            in create_text
        )
        attribution_secrets = [
            argument
            for argument in create
            if argument.startswith("attribution-cookie-secret=")
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
        assert any(
            argument.startswith("observer-token=") for argument in create
        )
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
        and any(
            argument.startswith("attribution-cookie-secret=")
            for argument in call
        )
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
    assert (
        "TR_ATTRIBUTION_COOKIE_SECRET=secretref:attribution-cookie-secret"
        in update_text
    )
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
    assert (
        "public canary retains forbidden OAuth env TR_GOOGLE_CLIENT_SECRET"
        in run.stderr
    )
    update = next(
        call for call in run.calls if call[:3] == ["az", "containerapp", "update"]
    )
    remove_index = update.index("--remove-env-vars")
    assert "TR_GOOGLE_CLIENT_SECRET" in update[
        remove_index + 1 : update.index("--min-replicas")
    ]
    assert not any(
        call[:4] == ["az", "containerapp", "revision", "set-mode"]
        for call in run.calls
    )
    assert not any(
        call[:3] == ["az", "containerapp", "show"]
        and "ingress.fqdn" in " ".join(call)
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
                *(
                    response
                    for response in fixture.responses
                    if provider not in response[0]
                ),
            ),
        ),
    )
    isolated = DeployScriptHarness(
        tmp_path / f"azure-canary-{provider.lower()}-capability-drift"
    )

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "public canary OAuth capability verification failed" in run.stderr
    assert f"{provider.lower()}=true" in run.stderr
    assert not any(
        call[:4] == ["az", "containerapp", "revision", "set-mode"]
        for call in run.calls
    )
    assert not any(
        call[:3] == ["az", "containerapp", "show"]
        and "ingress.fqdn" in " ".join(call)
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
        call[:3] in (
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
        *(
            response
            for response in fixture.responses
            if fixture_fragment not in response[0]
        ),
    )
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        replace(fixture, responses=responses),
    )
    isolated = DeployScriptHarness(
        tmp_path / f"azure-drift-{Path(script).stem}-{reported_value}"
    )

    run = isolated.run(script, verifier_rc=0)

    assert run.returncode != 0
    assert "observer scale verification failed" in run.stderr
    assert reported_value in run.stderr
    assert not run.verifier_calls
    assert not any(
        call[:3] == ["az", "containerapp", "show"]
        and "ingress.fqdn" in " ".join(call)
        for call in run.calls
    )


def test_synthetic_jobs_execute_private_ingress_preflight_in_their_own_region(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "synthetic-private")

    run = isolated.run("scripts/deploy/synthetic.sh", verifier_rc=0)

    assert run.returncode == 0, summarise(run)
    deploys = [
        (index, call)
        for index, call in enumerate(run.calls)
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["jobs", "deploy"]
    ]
    assert len(deploys) == 5
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
    assert len(subnet_updates) == 5
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
    assert len(service_contract_reads) == 5
    previous_deploy_index = -1
    for deploy_index, deploy in deploys:
        region = deploy[deploy.index("--region") + 1]
        deploy_text = " ".join(deploy)
        assert "TR_SERVICE_SURFACE=observer" in deploy_text
        assert (
            "TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:latest"
            in deploy_text
        )
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
        ("trusted-router-throughput-us-central1", "us-central1"),
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
        region = deploy[deploy.index("--region") + 1]
        assert f"https://trusted-router-stub-output.{region}.run.app" in deploy_text
        assert (
            "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
            in deploy_text
        )
        assert "TR_OBSERVER_INTERNAL_TOKEN" not in deploy_text
        assert "TR_SERVICE_SURFACE=observer" not in deploy_text
        assert "TR_BYOK_KMS_KEY_NAME=" in deploy_text
        assert "--update-secrets" in deploy
        assert "--set-secrets" not in deploy
        assert "--network" not in deploy
        assert "--subnet" not in deploy
        assert "--vpc-egress" not in deploy


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
        '] }]}}}}',
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
