from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from trusted_router.config import Settings

from .deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, summarise

SCRIPT = "scripts/deploy/internal_surface.sh"
REGIONS = {
    "us-central1",
    "us-east4",
    "europe-west4",
    "southamerica-east1",
}
EXPECTED_SECRETS = {
    "TR_INTERNAL_GATEWAY_TOKEN": "trustedrouter-internal-gateway-token:latest",
    "TR_OBSERVER_INTERNAL_TOKEN": "trustedrouter-observer-internal-token:latest",
    "TR_SYNTHETIC_MONITOR_API_KEY": (
        "trustedrouter-synthetic-monitor-api-key:latest"
    ),
    "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
    "TR_FEDERATION_PEER_TOKEN": "trustedrouter-federation-peer-token:latest",
    "TR_FEDERATION_HOME_TOKEN": "trustedrouter-federation-home-token:latest",
    "TR_FEDERATION_CREDIT_INBOUND_TOKEN": (
        "trustedrouter-federation-credit-inbound-token:latest"
    ),
    "TR_FEDERATION_CREDIT_PEER_TOKEN": (
        "trustedrouter-federation-credit-peer-token:latest"
    ),
    "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS": (
        "trustedrouter-federation-settlement-inbound-tokens:latest"
    ),
    "TR_FEDERATION_SETTLEMENT_HOME_TOKEN": (
        "trustedrouter-federation-settlement-home-token:latest"
    ),
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD": (
        "trustedrouter-clickhouse-control-read-password:latest"
    ),
}
SECRET_VALUES = {
    "TR_INTERNAL_GATEWAY_TOKEN": "gateway-" + "g" * 40,
    "TR_OBSERVER_INTERNAL_TOKEN": "observer-" + "o" * 40,
    "TR_SYNTHETIC_MONITOR_API_KEY": "monitor-" + "m" * 40,
    "TR_SENTRY_DSN": "https://example@example.ingest.sentry.io/1",
    "TR_FEDERATION_PEER_TOKEN": "f" * 40,
    "TR_FEDERATION_HOME_TOKEN": "h" * 40,
    "TR_FEDERATION_CREDIT_INBOUND_TOKEN": "i" * 40,
    "TR_FEDERATION_CREDIT_PEER_TOKEN": "p" * 40,
    "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS": "aws=" + "s" * 40,
    "TR_FEDERATION_SETTLEMENT_HOME_TOKEN": "t" * 40,
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD": "clickhouse-password",
}
EXPECTED_ENV_NAMES = {
    "TR_ENVIRONMENT",
    "TR_SERVICE_SURFACE",
    "TR_RELEASE",
    "TR_TRUSTED_DOMAIN",
    "TR_TRUSTED_DOMAIN_ALIASES",
    "TR_API_BASE_URL",
    "TR_SUPPORT_EMAIL",
    "TR_GCP_PROJECT_ID",
    "TR_REGIONS",
    "TR_PRIMARY_REGION",
    "TR_STORAGE_BACKEND",
    "TR_SPANNER_INSTANCE_ID",
    "TR_SPANNER_DATABASE_ID",
    "TR_SPANNER_POOL_SIZE",
    "TR_BIGTABLE_INSTANCE_ID",
    "TR_BIGTABLE_GENERATION_TABLE",
    "TR_BIGTABLE_MIRROR_WRITES_ENABLED",
    "TR_GENERATION_RECORDS_ENABLED",
    "TR_ANALYTICS_READ_MODE",
    "TR_REQUEST_RECORD_WRITE_MODE",
    "TR_SETTLE_OUTBOX_ENABLED",
    "TR_ANALYTICS_OUTBOX_ENABLED",
    "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED",
    "TR_USER_MODELS_DISPATCH_ENABLED",
    "TR_ENABLE_LIVE_PROVIDERS",
    "TR_REMEDIATOR_IN_PROCESS_ENABLED",
    "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS",
    "TR_RATE_LIMIT_CLIENT_IP_MODE",
    "TR_TRUST_GCP_SOURCE_COMMIT",
    "TR_TRUST_GCP_IMAGE_REFERENCE",
    "TR_TRUST_GCP_IMAGE_DIGEST",
    "TR_TRUST_GCP_RELEASE_URL",
    "TR_TRUST_GCP_RELEASE_FALLBACK_URLS",
    "TR_TRUST_AWS_RELEASE_URL",
    "TR_TRUST_AZURE_RELEASE_URL",
    "TR_REGIONAL_QUOTA_LEASES_ENABLED",
    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED",
    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS",
    "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS",
    "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS",
    "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS",
    "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT",
    "TR_REGIONAL_QUOTA_BIGTABLE_TABLE",
    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES",
    "TR_FEDERATION_HOME_BASE_URL",
    "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE",
}


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    return DeployScriptHarness(tmp_path_factory.mktemp("internal-deploy-harness"))


def _deploy_calls(run) -> list[list[str]]:
    return [
        call
        for call in run.calls
        if call[:6]
        == [
            "gcloud",
            "--project",
            "quill-cloud-proxy",
            "run",
            "deploy",
            "trusted-router-internal",
        ]
    ]


def _mapping(call: list[str], flag: str, delimiter: str) -> dict[str, str]:
    raw = call[call.index(flag) + 1]
    if flag == "--set-env-vars":
        raw = raw.removeprefix("^|^")
    return dict(item.split("=", 1) for item in raw.split(delimiter))


def _settings_kwargs(call: list[str]) -> dict[str, object]:
    values: dict[str, object] = {
        name.removeprefix("TR_").lower(): value
        for name, value in _mapping(call, "--set-env-vars", "|").items()
    }
    for name in _mapping(call, "--set-secrets", ","):
        values[name.removeprefix("TR_").lower()] = SECRET_VALUES[name]
    return values


@pytest.mark.parametrize(
    ("stage", "client_ip_mode"),
    (("companion", "untrusted"), ("routed", "edge_header")),
)
def test_internal_deploy_pins_each_region_stage_and_exact_secret_allowlist(
    harness: DeployScriptHarness,
    stage: str,
    client_ip_mode: str,
) -> None:
    run = harness.run(SCRIPT, args=(stage,))
    assert run.returncode == 0, summarise(run)
    calls = _deploy_calls(run)
    assert len(calls) == 4
    assert {call[call.index("--region") + 1] for call in calls} == REGIONS
    for call in calls:
        env = _mapping(call, "--set-env-vars", "|")
        assert set(env) == EXPECTED_ENV_NAMES
        assert env["TR_SERVICE_SURFACE"] == "internal"
        assert env["TR_RATE_LIMIT_CLIENT_IP_MODE"] == client_ip_mode
        assert _mapping(call, "--set-secrets", ",") == EXPECTED_SECRETS
        assert call[call.index("--ingress") + 1] == "all"
        assert call[call.index("--service-account") + 1] == (
            "tr-internal@quill-cloud-proxy.iam.gserviceaccount.com"
        )
        for flag, expected in (
            ("--concurrency", "8"),
            ("--memory", "2Gi"),
            ("--timeout", "60"),
            ("--max-instances", "40"),
            ("--min-instances", "2"),
        ):
            assert call[call.index(flag) + 1] == expected
        assert ("--no-traffic" in call) is (stage == "routed")


@pytest.mark.parametrize("stage", ("companion", "routed"))
def test_settings_from_exact_emitted_internal_env_fail_closed(
    harness: DeployScriptHarness,
    stage: str,
) -> None:
    run = harness.run(SCRIPT, args=(stage,))
    assert run.returncode == 0, summarise(run)
    for call in _deploy_calls(run):
        kwargs = _settings_kwargs(call)
        assert Settings(**kwargs).service_surface == "internal"
        for name, value in (
            ("stripe_secret_key", "sk_live_forbidden"),
            ("paypal_client_id", "paypal-forbidden"),
            ("adyen_api_key", "adyen-forbidden"),
            ("aws_secret_access_key", "ses-forbidden"),
            ("attribution_cookie_secret", "a" * 40),
            (
                "byok_kms_key_name",
                "projects/p/locations/l/keyRings/r/cryptoKeys/k",
            ),
        ):
            with pytest.raises(
                ValidationError,
                match=f"unset TR_{name.upper()} for TR_SERVICE_SURFACE=internal",
            ):
                Settings(**kwargs, **{name: value})


def test_routed_authenticated_validate_smoke_precedes_promotion_and_restricts_ingress(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run(SCRIPT, args=("routed",))
    assert run.returncode == 0, summarise(run)
    smoke_indexes = [index for index, call in enumerate(run.calls) if call[0] == "curl"]
    assert len(smoke_indexes) == 4
    assert not any(
        "status.traffic[?" in argument
        for call in run.calls
        for argument in call
    )
    assert any(
        "run" in call
        and "services" in call
        and "describe" in call
        and "--format=json" in call
        for call in run.calls
    )
    for smoke_index in smoke_indexes:
        call = run.calls[smoke_index]
        assert call[-1].endswith("/internal/gateway/validate")
        assert any(item.startswith("@") for item in call if "headers" in item)
        assert not any("harness-internal-gateway-token" in item for item in call)
        region = next(region for region in REGIONS if region in call[-1])
        promote_index = next(
            index
            for index, candidate in enumerate(run.calls)
            if any(
                item
                == f"--to-revisions=trusted-router-internal-candidate-{region}=100"
                for item in candidate
            )
        )
        restrict_index = next(
            index
            for index, candidate in enumerate(run.calls)
            if "update" in candidate
            and region in candidate
            and "internal-and-cloud-load-balancing" in candidate
        )
        assert smoke_index < promote_index < restrict_index


def test_routed_validate_smoke_rejects_wrong_internal_token(tmp_path: Path) -> None:
    run = DeployScriptHarness(tmp_path / "wrong-internal-smoke-token").run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_EXPECTED_TOKEN": "different-token"},
    )

    assert run.returncode != 0
    assert "authenticated validate smoke expected the dummy-key 401" in run.stderr
    assert not any("candidate-us-central1=100" in " ".join(call) for call in run.calls)


@pytest.mark.parametrize("status", ("200", "500"))
def test_routed_validate_smoke_rejects_non_401_status(
    tmp_path: Path,
    status: str,
) -> None:
    run = DeployScriptHarness(tmp_path / f"bad-internal-smoke-{status}").run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_SMOKE_HTTP_CODE": status},
    )

    assert run.returncode != 0
    assert "authenticated validate smoke expected the dummy-key 401" in run.stderr
    assert not any("candidate-us-central1=100" in " ".join(call) for call in run.calls)


def test_routed_validate_smoke_rejects_unexpected_401_body(tmp_path: Path) -> None:
    run = DeployScriptHarness(tmp_path / "bad-internal-smoke-body").run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_SMOKE_BODY": "unexpected"},
    )

    assert run.returncode != 0
    assert "authenticated validate smoke expected the dummy-key 401" in run.stderr


def test_routed_validate_smoke_rejects_401_body_with_expected_phrase_only(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "substring-internal-smoke-body").run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_SMOKE_BODY": "substring"},
    )

    assert run.returncode != 0
    assert "authenticated validate smoke expected the dummy-key 401" in run.stderr


def test_internal_settings_explicitly_reject_stripe_secret_key(
    harness: DeployScriptHarness,
) -> None:
    run = harness.run(SCRIPT, args=("routed",))
    assert run.returncode == 0, summarise(run)
    kwargs = _settings_kwargs(_deploy_calls(run)[0])

    with pytest.raises(
        ValidationError,
        match="unset TR_STRIPE_SECRET_KEY for TR_SERVICE_SURFACE=internal",
    ):
        Settings(**kwargs, stripe_secret_key="sk_live_forbidden")  # noqa: S106


def test_region_three_failure_restores_every_earlier_internal_promotion(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "internal-region-three-failure").run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_SMOKE_FAIL_REGION": "europe-west4"},
    )

    assert run.returncode != 0
    for region in ("us-central1", "us-east4"):
        assert any(
            "update-traffic" in call
            and region in call
            and "--to-revisions=trusted-router-internal-active=100" in call
            for call in run.calls
        )


def test_restart_restores_every_region_from_durable_promotion_history(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "internal-promotion-state"
    state_dir.mkdir()
    (state_dir / "trusted-router-internal.promotion-history").write_text(
        "us-central1\ttrusted-router-internal-active\t"
        "trusted-router-internal-candidate-us-central1\t"
        "internal-and-cloud-load-balancing\n"
        "us-east4\ttrusted-router-internal-active\t"
        "trusted-router-internal-candidate-us-east4\t"
        "internal-and-cloud-load-balancing\n"
    )

    run = DeployScriptHarness(tmp_path / "internal-restart-restore").run(
        SCRIPT,
        args=("routed",),
        extra_env={"TR_INTERNAL_DEPLOY_STATE_DIR": str(state_dir)},
    )

    assert run.returncode == 0, summarise(run)
    for region in ("us-central1", "us-east4"):
        assert any(
            "update-traffic" in call
            and region in call
            and "--to-revisions=trusted-router-internal-active=100" in call
            for call in run.calls
        )
    assert not (state_dir / "trusted-router-internal.promotion-history").exists()


def test_failed_internal_fleet_restore_reports_every_exact_command(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "internal-restore-failure").run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_INTERNAL_SMOKE_FAIL_REGION": "europe-west4",
            "HARNESS_INTERNAL_RESTORE_FAIL_REGION": "us-east4",
        },
    )

    assert run.returncode != 0
    assert "FLEET IS SPLIT" in run.stderr
    for region in ("us-central1", "us-east4", "europe-west4"):
        assert (
            "gcloud --project quill-cloud-proxy run services update-traffic "
            f"trusted-router-internal --region {region} "
            "--to-revisions=trusted-router-internal-active=100 --quiet"
        ) in run.stderr
        assert (
            "gcloud --project quill-cloud-proxy run services update "
            f"trusted-router-internal --region {region} "
            "--ingress internal-and-cloud-load-balancing --quiet"
        ) in run.stderr


def test_companion_cloud_state_is_a_legitimate_routed_start(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "companion-to-routed")
    run = harness.run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_INTERNAL_INITIAL_INGRESS": "all"},
    )
    assert run.returncode == 0, summarise(run)


def test_missing_internal_runtime_sa_refuses_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES.get(SCRIPT)
    if original is not None:
        monkeypatch.setitem(
            SCRIPT_FIXTURES,
            SCRIPT,
            replace(
                original,
                failures=(*original.failures, r"iam service-accounts describe"),
            ),
        )
    harness = DeployScriptHarness(tmp_path / "missing-internal-sa")

    run = harness.run(SCRIPT, args=("companion",))

    assert run.returncode != 0
    assert "required internal runtime service account" in run.stderr
    mutating = ("create", "update", "deploy", "add-backend", "import")
    assert not any(any(part in mutating for part in call[1:]) for call in run.calls)


@pytest.mark.parametrize(
    ("failed_preflight", "message"),
    (
        (r"spanner databases get-iam-policy", "roles/spanner.databaseUser"),
        (r"bigtable instances get-iam-policy", "roles/bigtable.user"),
        (r"secrets get-iam-policy", "roles/secretmanager.secretAccessor"),
    ),
)
def test_missing_runtime_grant_refuses_before_cloud_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_preflight: str,
    message: str,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(original, failures=(*original.failures, failed_preflight)),
    )
    run = DeployScriptHarness(tmp_path / message.rsplit(".", 1)[-1]).run(
        SCRIPT, args=("companion",)
    )
    assert run.returncode != 0
    assert message in run.stderr
    mutating = ("create", "update", "deploy", "add-backend", "import")
    assert not any(any(part in mutating for part in call[1:]) for call in run.calls)
