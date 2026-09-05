from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from trusted_router.config import Settings

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    summarise,
)

SCRIPT = "scripts/deploy/public_surface.sh"
REGIONS = {
    "us-central1",
    "us-east4",
    "europe-west4",
    "southamerica-east1",
}
PUBLIC_SA = "tr-public@quill-cloud-proxy.iam.gserviceaccount.com"
IMAGE = (
    "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/"
    "trusted-router@sha256:" + "1" * 64
)
BASE_ENV = {
    "TR_ENVIRONMENT": "production",
    "TR_SERVICE_SURFACE": "public",
    "TR_RELEASE": "cb16dcc",
    "TR_TRUSTED_DOMAIN": "trustedrouter.com",
    "TR_TRUSTED_DOMAIN_ALIASES": "allyrouter.com,uptimerouter.com",
    "TR_API_BASE_URL": "https://api.trustedrouter.com/v1",
    "TR_SUPPORT_EMAIL": "help@trustedrouter.com",
    "TR_GCP_PROJECT_ID": "quill-cloud-proxy",
    "TR_REGIONS": "us-central1,us-east4,europe-west4,southamerica-east1",
    "TR_PRIMARY_REGION": "us-central1",
    "TR_STORAGE_BACKEND": "spanner-bigtable",
    "TR_SPANNER_INSTANCE_ID": "trusted-router-nam6",
    "TR_SPANNER_DATABASE_ID": "trusted-router",
    "TR_SPANNER_POOL_SIZE": "8",
    "TR_BIGTABLE_INSTANCE_ID": "trusted-router-logs",
    "TR_BIGTABLE_GENERATION_TABLE": "trustedrouter-generations",
    "TR_BIGTABLE_MIRROR_WRITES_ENABLED": "true",
    "TR_ANALYTICS_READ_MODE": "clickhouse",
    "GOOGLE_CLOUD_SPANNER_MULTIPLEXED_SESSIONS_FOR_RW": "true",
    # The status page this surface serves reports outbox freshness; without
    # this its store has no outbox object and publishes not_configured,
    # failing verify-cloud-complete stage (c) on every deploy.
    "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "true",
    "TR_ENABLE_LIVE_PROVIDERS": "false",
    "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE": "true",
    "TR_GITHUB_OAUTH_LOGIN_AVAILABLE": "true",
    "TR_TRUST_GCP_SOURCE_COMMIT": "source-commit",
    "TR_TRUST_GCP_IMAGE_REFERENCE": "gcp-image-reference",
    "TR_TRUST_GCP_IMAGE_DIGEST": "sha256:" + "2" * 64,
    "TR_TRUST_GCP_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/gcp-release.json"
    ),
    "TR_TRUST_GCP_RELEASE_FALLBACK_URLS": (
        "https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/"
        "main/trust-page/gcp-release.json"
    ),
    "TR_TRUST_AWS_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/aws-release.json"
    ),
    "TR_TRUST_AZURE_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/azure-release.json"
    ),
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL": "http://10.128.15.10:8123",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "tr_control_read",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "tr",
}
EXPECTED_SECRETS = {
    "TR_ATTRIBUTION_COOKIE_KEY": "trustedrouter-attribution-cookie-key:latest",
    "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD": (
        "trustedrouter-clickhouse-control-read-password:latest"
    ),
}
HARNESS_SECRET_VALUES = {
    "TR_ATTRIBUTION_COOKIE_KEY": "aDMnBV9nDwwAD1tr4MpooFMj7i8Kv6lB5Q9LTmrjTfc=",
}
FORBIDDEN_SECRET_FRAGMENTS = {
    "gateway",
    "stripe",
    "paypal",
    "observer",
    "ses",
    "byok",
    "oauth",
    "google-client",
    "github-client",
}


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    return DeployScriptHarness(tmp_path_factory.mktemp("public-deploy-harness"))


def _deploy_calls(run) -> list[list[str]]:
    return [
        call
        for call in run.calls
        if call[:4] == ["gcloud", "--project", "quill-cloud-proxy", "run"]
        and call[4:6] == ["deploy", "trusted-router-public"]
    ]


def _serialized_mapping(call: list[str], flag: str, delimiter: str) -> dict[str, str]:
    raw = call[call.index(flag) + 1]
    if flag == "--set-env-vars":
        assert raw.startswith("^|^")
        raw = raw.removeprefix("^|^")
    return {
        item.partition("=")[0]: item.partition("=")[2]
        for item in raw.split(delimiter)
    }


def _settings_kwargs(call: list[str]) -> dict[str, object]:
    kwargs: dict[str, object] = {
        name.removeprefix("TR_").lower(): value
        for name, value in _serialized_mapping(call, "--set-env-vars", "|").items()
    }
    for name, reference in _serialized_mapping(call, "--set-secrets", ",").items():
        secret_name = reference.partition(":")[0]
        kwargs[name.removeprefix("TR_").lower()] = HARNESS_SECRET_VALUES.get(
            name,
            f"harness-{secret_name}",
        )
    return kwargs


@pytest.mark.parametrize(
    ("stage", "ingress", "client_ip_mode"),
    [
        ("companion", "all", "untrusted"),
        ("routed", "all", "edge_header"),
    ],
)
def test_public_deploy_pins_every_region_and_stage_contract(
    harness: DeployScriptHarness,
    stage: str,
    ingress: str,
    client_ip_mode: str,
) -> None:
    run = harness.run(SCRIPT, args=(stage,))
    assert run.returncode == 0, summarise(run)
    calls = _deploy_calls(run)
    assert len(calls) == 4
    assert {call[call.index("--region") + 1] for call in calls} == REGIONS

    expected_env = {**BASE_ENV, "TR_RATE_LIMIT_CLIENT_IP_MODE": client_ip_mode}
    for call in calls:
        assert _serialized_mapping(call, "--set-env-vars", "|") == expected_env
        secrets = _serialized_mapping(call, "--set-secrets", ",")
        assert secrets == EXPECTED_SECRETS
        assert "TR_SENTRY_DSN" in secrets
        lowered = " ".join(f"{name}={value}" for name, value in secrets.items()).lower()
        assert all(fragment not in lowered for fragment in FORBIDDEN_SECRET_FRAGMENTS)
        assert call[call.index("--ingress") + 1] == ingress
        assert call[call.index("--service-account") + 1] == PUBLIC_SA
        assert call[call.index("--image") + 1] == IMAGE
        if stage == "routed":
            assert "--no-traffic" in call
            assert call[call.index("--format") + 1] == (
                "value(status.latestCreatedRevisionName)"
            )
        else:
            assert "--no-traffic" not in call
        for flag, value in (
            ("--concurrency", "16"),
            ("--cpu", "1"),
            ("--memory", "2Gi"),
            ("--timeout", "60"),
            ("--max-instances", "20"),
            ("--min-instances", "2"),
            ("--network", "default"),
            ("--subnet", "default"),
            ("--vpc-egress", "private-ranges-only"),
        ):
            assert call[call.index(flag) + 1] == value


@pytest.mark.parametrize("stage", ["companion", "routed"])
def test_exact_emitted_public_settings_validate_and_reject_control_secrets(
    harness: DeployScriptHarness,
    stage: str,
) -> None:
    run = harness.run(SCRIPT, args=(stage,))
    assert run.returncode == 0, summarise(run)
    for call in _deploy_calls(run):
        kwargs = _settings_kwargs(call)
        settings = Settings(**kwargs)
        assert settings.service_surface == "public"
        for forbidden_name, forbidden_value in (
            ("internal_gateway_token", "gateway-" + "g" * 32),
            ("stripe_secret_key", "sk_live_forbidden"),
            ("observer_internal_token", "observer-" + "o" * 32),
        ):
            with pytest.raises(
                ValidationError,
                match=f"unset TR_{forbidden_name.upper()} for TR_SERVICE_SURFACE=public",
            ):
                Settings(**kwargs, **{forbidden_name: forbidden_value})


def test_missing_runtime_service_account_fails_before_cloud_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(
            original,
            failures=(*original.failures, r"iam service-accounts describe"),
        ),
    )
    isolated = DeployScriptHarness(tmp_path / "missing-public-sa")

    run = isolated.run(SCRIPT, args=("companion",))

    assert run.returncode != 0
    assert "required public runtime service account" in run.stderr
    assert "roles/spanner.databaseReader" in run.stderr
    assert "roles/bigtable.reader" in run.stderr
    assert "roles/serviceusage.serviceUsageConsumer" in run.stderr
    mutating = ("create", "update", "deploy", "add-backend", "import")
    assert not any(any(part in mutating for part in call[1:]) for call in run.calls)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TR_PUBLIC_CONCURRENCY", "0"),
        ("TR_PUBLIC_CONCURRENCY", "many"),
        ("TR_PUBLIC_MIN_INSTANCES", "-1"),
        ("TR_PUBLIC_MIN_INSTANCES", "1.5"),
    ],
)
def test_invalid_public_capacity_fails_before_cloud_mutation(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    run = DeployScriptHarness(tmp_path / f"invalid-{name}-{value}").run(
        SCRIPT,
        args=("routed",),
        extra_env={name: value},
    )

    assert run.returncode != 0
    assert "must be a positive integer" in run.stderr
    assert not _deploy_calls(run)


def test_bigtable_mode_omits_clickhouse_secret_env_and_vpc_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    responses: list[tuple[str, str]] = []
    for pattern, response in original.responses:
        if "revisions describe trusted-router-active" not in pattern:
            responses.append((pattern, response))
            continue
        revision = json.loads(response)
        env = revision["spec"]["containers"][0]["env"]
        for item in env:
            if item.get("name") == "TR_ANALYTICS_READ_MODE":
                item["value"] = "bigtable"
        revision["spec"]["containers"][0]["env"] = [
            item
            for item in env
            if not item.get("name", "").startswith(
                "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_"
            )
        ]
        responses.append((pattern, json.dumps(revision, separators=(",", ":"))))
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(original, responses=tuple(responses)),
    )
    harness = DeployScriptHarness(tmp_path / "bigtable-public")

    run = harness.run(SCRIPT, args=("companion",))

    assert run.returncode == 0, summarise(run)
    for call in _deploy_calls(run):
        env = _serialized_mapping(call, "--set-env-vars", "|")
        assert env["TR_ANALYTICS_READ_MODE"] == "bigtable"
        assert not any("CLICKHOUSE" in name for name in env)
        assert _serialized_mapping(call, "--set-secrets", ",") == {
            "TR_ATTRIBUTION_COOKIE_KEY": (
                "trustedrouter-attribution-cookie-key:latest"
            ),
            "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
        }
        assert "--network" not in call
        assert "--subnet" not in call
        assert "--vpc-egress" not in call
        assert Settings(**_settings_kwargs(call)).service_surface == "public"


def _traffic_calls(run) -> list[list[str]]:
    return [
        call
        for call in run.calls
        if "run" in call and "services" in call and "update-traffic" in call
    ]


def _region_arg(call: list[str]) -> str:
    if "--region" in call:
        return call[call.index("--region") + 1]
    return next(item.removeprefix("--region=") for item in call if item.startswith("--region="))


def test_routed_healthy_smoke_promotes_each_region(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "healthy-routed-public")

    run = isolated.run(SCRIPT, args=("routed",))

    assert run.returncode == 0, summarise(run)
    promotes = [
        call
        for call in _traffic_calls(run)
        if any(item.startswith("--to-revisions=") for item in call)
    ]
    assert len(promotes) == 4
    assert {_region_arg(call) for call in promotes} == REGIONS
    for call in promotes:
        region = _region_arg(call)
        assert f"--to-revisions=trusted-router-public-candidate-{region}=100" in call
    assert run.public_ingress_state == {
        region: "internal-and-cloud-load-balancing" for region in REGIONS
    }


def test_routed_accepts_deliberate_companion_cloud_state(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "companion-to-routed-public")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_PUBLIC_INITIAL_INGRESS": "all"},
    )

    assert run.returncode == 0, summarise(run)
    assert run.public_ingress_state == {
        region: "internal-and-cloud-load-balancing" for region in REGIONS
    }


def test_routed_smoke_is_reachable_before_each_region_restricts_ingress(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "ingress-aware-routed-public")

    run = isolated.run(SCRIPT, args=("routed",))

    assert run.returncode == 0, summarise(run)
    for region in REGIONS:
        deploy_index = next(
            index
            for index, call in enumerate(run.calls)
            if call[0] == "gcloud"
            and "deploy" in call
            and _region_arg(call) == region
        )
        curl_indexes = [
            index
            for index, call in enumerate(run.calls)
            if call[0] == "curl" and region in call[-1]
        ]
        restrict_index = next(
            index
            for index, call in enumerate(run.calls)
            if call[0] == "gcloud"
            and "update" in call
            and "trusted-router-public" in call
            and _region_arg(call) == region
            and "internal-and-cloud-load-balancing" in call
        )
        assert curl_indexes
        assert deploy_index < min(curl_indexes) < max(curl_indexes) < restrict_index
    assert run.public_ingress_state == {
        region: "internal-and-cloud-load-balancing" for region in REGIONS
    }


def test_term_during_promotion_reports_and_restores_the_in_flight_region(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "interrupted-promotion")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PUBLIC_INITIAL_INGRESS": "internal-and-cloud-load-balancing",
            "HARNESS_PUBLIC_TERM_DURING_PROMOTE_REGION": "us-central1",
        },
    )

    assert run.returncode == 143
    assert "interrupted during promotion" in run.stderr
    assert "us-central1" in run.stderr
    assert "trusted-router-public-active" in run.stderr
    assert "trusted-router-public-candidate-us-central1" in run.stderr
    assert any(
        "--to-revisions=trusted-router-public-active=100" in call
        for call in _traffic_calls(run)
    )
    assert run.public_ingress_state["us-central1"] == (
        "internal-and-cloud-load-balancing"
    )


def test_term_during_no_traffic_smoke_restores_the_original_ingress(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "interrupted-smoke")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PUBLIC_INITIAL_INGRESS": (
                "internal-and-cloud-load-balancing"
            ),
            "HARNESS_PUBLIC_TERM_DURING_PROBE_TAG_REGION": "us-central1",
        },
    )

    assert run.returncode == 143
    assert "interrupted before traffic promotion" in run.stderr
    assert run.public_ingress_state["us-central1"] == (
        "internal-and-cloud-load-balancing"
    )
    assert not any(
        "--to-revisions=trusted-router-public-active=100" in call
        for call in _traffic_calls(run)
    )


def test_cloud_only_mid_promotion_state_is_reported_without_a_local_marker(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "cloud-only-mid-promotion")
    state_dir = tmp_path / "empty-local-state"

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "TR_PUBLIC_DEPLOY_STATE_DIR": str(state_dir),
            "HARNESS_PUBLIC_INITIAL_INGRESS": "all",
            "HARNESS_PUBLIC_INITIAL_PROBE_TAG_REGION": "us-central1",
        },
    )

    assert not (state_dir / "trusted-router-public.promotion-in-flight").exists()
    assert run.returncode != 0
    assert "cloud recovery state detected" in run.stderr
    assert "serving=trusted-router-public-active" in run.stderr
    assert "ingress=all" in run.stderr
    assert (
        "probe tag public-revision-probe="
        "trusted-router-public-candidate-us-central1"
    ) in run.stderr
    assert "--remove-tags=public-revision-probe" in run.stderr
    assert "--ingress internal-and-cloud-load-balancing" in run.stderr
    assert not _deploy_calls(run)


def test_probe_tag_cleanup_retries_a_transient_failure(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "transient-tag-cleanup")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PROBE_TAG_REMOVE_FAILURES": "1",
            "TR_PROBE_TAG_REMOVE_RETRY_SECONDS": "0",
        },
    )

    assert run.returncode == 0, summarise(run)
    remove_calls = [
        call
        for call in _traffic_calls(run)
        if "--remove-tags=public-revision-probe" in call
    ]
    assert len(remove_calls) == 5


def test_probe_tag_cleanup_permanent_failure_is_loud_and_nonzero(
    tmp_path: Path,
) -> None:
    isolated = DeployScriptHarness(tmp_path / "permanent-tag-cleanup")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PROBE_TAG_REMOVE_ALWAYS_FAIL": "1",
            "TR_PROBE_TAG_REMOVE_RETRY_SECONDS": "0",
        },
    )

    assert run.returncode != 0
    assert "probe tag public-revision-probe may still be addressable" in run.stderr
    assert (
        "gcloud run services update-traffic trusted-router-public "
        "--region=us-central1 --project=quill-cloud-proxy "
        "--remove-tags=public-revision-probe --quiet"
    ) in run.stderr
    remove_calls = [
        call
        for call in _traffic_calls(run)
        if "--remove-tags=public-revision-probe" in call
    ]
    assert len(remove_calls) == 6


def test_routed_failing_region_does_not_promote_it(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "failing-routed-public")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PUBLIC_SMOKE_FAIL_REGION": "us-central1",
            "HARNESS_PUBLIC_SMOKE_FAIL_PATH": "/robots.txt",
        },
    )

    assert run.returncode != 0
    assert "us-central1" in run.stderr
    assert "/robots.txt" in run.stderr
    traffic = _traffic_calls(run)
    assert any("--to-revisions=trusted-router-public-active=100" in call for call in traffic)
    assert not any("candidate-us-central1=100" in " ".join(call) for call in traffic)


def test_region_three_failure_restores_every_earlier_public_promotion(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "public-region-three-failure").run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PUBLIC_SMOKE_FAIL_REGION": "europe-west4",
            "HARNESS_PUBLIC_SMOKE_FAIL_PATH": "/status.json",
        },
    )

    assert run.returncode != 0
    traffic = _traffic_calls(run)
    for region in ("us-central1", "us-east4"):
        assert any(
            _region_arg(call) == region
            and "--to-revisions=trusted-router-public-active=100" in call
            for call in traffic
        )


def test_restart_restores_every_public_region_from_durable_promotion_history(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "public-promotion-state"
    state_dir.mkdir()
    (state_dir / "trusted-router-public.promotion-history").write_text(
        "us-central1\ttrusted-router-public-active\t"
        "trusted-router-public-candidate-us-central1\t"
        "internal-and-cloud-load-balancing\n"
        "us-east4\ttrusted-router-public-active\t"
        "trusted-router-public-candidate-us-east4\t"
        "internal-and-cloud-load-balancing\n"
    )

    run = DeployScriptHarness(tmp_path / "public-restart-restore").run(
        SCRIPT,
        args=("routed",),
        extra_env={"TR_PUBLIC_DEPLOY_STATE_DIR": str(state_dir)},
    )

    assert run.returncode == 0, summarise(run)
    traffic = _traffic_calls(run)
    for region in ("us-central1", "us-east4"):
        assert any(
            _region_arg(call) == region
            and "--to-revisions=trusted-router-public-active=100" in call
            for call in traffic
        )
    assert not (state_dir / "trusted-router-public.promotion-history").exists()


def test_failed_public_fleet_restore_reports_every_exact_command(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "public-restore-failure").run(
        SCRIPT,
        args=("routed",),
        extra_env={
            "HARNESS_PUBLIC_SMOKE_FAIL_REGION": "europe-west4",
            "HARNESS_PUBLIC_SMOKE_FAIL_PATH": "/status.json",
            "HARNESS_PUBLIC_RESTORE_FAIL_REGION": "us-east4",
        },
    )

    assert run.returncode != 0
    assert "FLEET IS SPLIT" in run.stderr
    for region in ("us-central1", "us-east4", "europe-west4"):
        assert (
            "gcloud --project quill-cloud-proxy run services update-traffic "
            f"trusted-router-public --region {region} "
            "--to-revisions=trusted-router-public-active=100 --quiet"
        ) in run.stderr
        assert (
            "gcloud --project quill-cloud-proxy run services update "
            f"trusted-router-public --region {region} "
            "--ingress internal-and-cloud-load-balancing --quiet"
        ) in run.stderr


def test_routed_transport_failure_retries_and_fails_safe(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path / "transport-routed-public")

    run = isolated.run(
        SCRIPT,
        args=("routed",),
        extra_env={"HARNESS_PUBLIC_SMOKE_TRANSPORT_PATH": "/status.json"},
    )

    assert run.returncode != 0
    status_curls = [
        call
        for call in run.calls
        if call[0] == "curl" and call[-1].endswith("/status.json")
    ]
    assert len(status_curls) == 3
    assert "inconclusive" in run.stderr
    assert not any(
        "candidate-us-central1=100" in " ".join(call)
        for call in _traffic_calls(run)
    )
