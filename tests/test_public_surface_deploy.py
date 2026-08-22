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
    "TR_ATTRIBUTION_COOKIE_SECRET": (
        "trustedrouter-attribution-cookie-secret:latest"
    ),
    "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD": (
        "trustedrouter-clickhouse-control-read-password:latest"
    ),
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
        kwargs[name.removeprefix("TR_").lower()] = f"harness-{secret_name}"
    return kwargs


@pytest.mark.parametrize(
    ("stage", "ingress", "client_ip_mode"),
    [
        ("companion", "all", "untrusted"),
        ("routed", "internal-and-cloud-load-balancing", "edge_header"),
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
        for flag, value in (
            ("--concurrency", "8"),
            ("--cpu", "1"),
            ("--memory", "2Gi"),
            ("--timeout", "60"),
            ("--max-instances", "20"),
            ("--min-instances", "1"),
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
            "TR_ATTRIBUTION_COOKIE_SECRET": (
                "trustedrouter-attribution-cookie-secret:latest"
            ),
            "TR_SENTRY_DSN": "trustedrouter-sentry-dsn:latest",
        }
        assert "--network" not in call
        assert "--subnet" not in call
        assert "--vpc-egress" not in call
        assert Settings(**_settings_kwargs(call)).service_surface == "public"
