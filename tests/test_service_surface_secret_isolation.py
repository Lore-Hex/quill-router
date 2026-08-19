from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from trusted_router.acquisition import (
    AttributionContext,
    decode_attribution_cookie,
    encode_attribution_cookie,
)
from trusted_router.config import Settings

_ATTRIBUTION_SECRET = "attribution-only-" + "a" * 32
_GATEWAY_SECRET = "gateway-only-" + "g" * 32
_PRODUCTION_STORAGE = {
    "environment": "production",
    "storage_backend": "spanner-bigtable",
    "spanner_instance_id": "trusted-router",
    "spanner_database_id": "trusted-router",
    "bigtable_instance_id": "trusted-router-logs",
}
_CONTROL_SECRETS = {
    "attribution_cookie_secret": _ATTRIBUTION_SECRET,
    "internal_gateway_token": _GATEWAY_SECRET,
    "stripe_webhook_secret": "whsec-test",
    "stripe_secret_key": "sk-test",
    "sentry_dsn": "https://example@example.ingest.sentry.io/1",
    "aws_access_key_id": "ses-access",
    "aws_secret_access_key": "ses-secret",
    "ses_from_email": "noreply@example.com",
    "byok_kms_key_name": (
        "projects/test/locations/global/keyRings/trusted-router/cryptoKeys/byok-envelope"
    ),
}

_ACTION_FORBIDDEN_CONFIG: tuple[tuple[str, object, str], ...] = (
    ("postgres_dsn", "postgresql://actions:secret@db/actions", "TR_POSTGRES_DSN"),
    ("postgres_iam_auth", "aws-dsql", "TR_POSTGRES_IAM_AUTH"),
    ("clickhouse_url", "https://clickhouse.example", "TR_CLICKHOUSE_URL"),
    ("clickhouse_password", "clickhouse-secret", "TR_CLICKHOUSE_PASSWORD"),
    (
        "provider_analytics_clickhouse_url",
        "https://provider-clickhouse.example",
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL",
    ),
    (
        "provider_analytics_clickhouse_password",
        "provider-secret",
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD",
    ),
    (
        "operational_analytics_clickhouse_url",
        "https://ops-clickhouse.example",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL",
    ),
    (
        "operational_analytics_clickhouse_password",
        "ops-clickhouse-secret",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD",
    ),
    ("google_data_manager_enabled", True, "TR_GOOGLE_DATA_MANAGER_ENABLED"),
    (
        "google_data_manager_kms_key_name",
        "projects/test/locations/global/keyRings/test/cryptoKeys/data-manager",
        "TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME",
    ),
    ("paypal_client_id", "paypal-client", "TR_PAYPAL_CLIENT_ID"),
    ("paypal_client_secret", "paypal-secret", "TR_PAYPAL_CLIENT_SECRET"),
    ("paypal_webhook_id", "paypal-webhook", "TR_PAYPAL_WEBHOOK_ID"),
    ("adyen_enabled", True, "TR_ADYEN_ENABLED"),
    ("adyen_api_key", "adyen-secret", "TR_ADYEN_API_KEY"),
    ("adyen_client_key", "adyen-client", "TR_ADYEN_CLIENT_KEY"),
    ("adyen_hmac_key", "ab" * 32, "TR_ADYEN_HMAC_KEY"),
    ("adyen_reference_key", "r" * 32, "TR_ADYEN_REFERENCE_KEY"),
    ("google_client_id", "google-client", "TR_GOOGLE_CLIENT_ID"),
    ("google_client_secret", "google-secret", "TR_GOOGLE_CLIENT_SECRET"),
    (
        "google_alias_credentials_json",
        '{"allyrouter.com":{"client_id":"id","client_secret":"secret"}}',
        "TR_GOOGLE_ALIAS_CREDENTIALS_JSON",
    ),
    ("github_client_id", "github-client", "TR_GITHUB_CLIENT_ID"),
    ("github_client_secret", "github-secret", "TR_GITHUB_CLIENT_SECRET"),
    (
        "github_alias_credentials_json",
        '{"allyrouter.com":{"client_id":"id","client_secret":"secret"}}',
        "TR_GITHUB_ALIAS_CREDENTIALS_JSON",
    ),
    ("x402_enabled", True, "TR_X402_ENABLED"),
    ("notify_enabled", True, "TR_NOTIFY_ENABLED"),
    ("veriff_enabled", True, "TR_VERIFF_ENABLED"),
    ("veriff_api_key", "veriff-secret", "TR_VERIFF_API_KEY"),
    (
        "veriff_shared_secret_key",
        "veriff-shared-secret",
        "TR_VERIFF_SHARED_SECRET_KEY",
    ),
    ("telnyx_api_key", "telnyx-secret", "TR_TELNYX_API_KEY"),
    ("twilio_account_sid", "twilio-account", "TR_TWILIO_ACCOUNT_SID"),
    ("twilio_auth_token", "twilio-secret", "TR_TWILIO_AUTH_TOKEN"),
    ("twilio_api_key_secret", "twilio-api-secret", "TR_TWILIO_API_KEY_SECRET"),
    (
        "synthetic_monitor_api_key",
        "synthetic-secret",
        "TR_SYNTHETIC_MONITOR_API_KEY",
    ),
    ("federation_peer_token", "f" * 32, "TR_FEDERATION_PEER_TOKEN"),
    ("federation_home_token", "h" * 32, "TR_FEDERATION_HOME_TOKEN"),
    (
        "federation_credit_inbound_token",
        "i" * 32,
        "TR_FEDERATION_CREDIT_INBOUND_TOKEN",
    ),
    (
        "federation_credit_peer_token",
        "p" * 32,
        "TR_FEDERATION_CREDIT_PEER_TOKEN",
    ),
    (
        "federation_settlement_inbound_tokens",
        "aws=" + "s" * 32,
        "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS",
    ),
    (
        "federation_settlement_home_token",
        "t" * 32,
        "TR_FEDERATION_SETTLEMENT_HOME_TOKEN",
    ),
    ("byok_envelope_key_b64", "ZW52ZWxvcGUta2V5", "TR_BYOK_ENVELOPE_KEY_B64"),
)


def _production(surface: str, **overrides: object) -> Settings:
    values: dict[str, object] = {**_PRODUCTION_STORAGE, "service_surface": surface}
    values.update(overrides)
    return Settings(**values)


def test_public_production_needs_only_its_narrow_attribution_secret() -> None:
    settings = _production(
        "public",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
    )

    assert settings.attribution_cookie_secret == _ATTRIBUTION_SECRET
    assert settings.internal_gateway_token is None
    assert settings.stripe_secret_key is None
    assert settings.sentry_dsn is None
    assert settings.aws_access_key_id is None
    assert settings.byok_kms_key_name is None


def test_actions_production_has_no_store_or_private_plane_credentials() -> None:
    settings = Settings(
        environment="production",
        service_surface="actions",
        aws_access_key_id="ses-access",
        aws_secret_access_key="ses-secret",  # noqa: S106 - test credential.
        ses_from_email="noreply@example.com",
    )

    assert settings.storage_backend == "memory"
    assert settings.internal_gateway_token is None
    assert settings.stripe_secret_key is None
    assert settings.sentry_dsn is None
    assert settings.byok_kms_key_name is None


@pytest.mark.parametrize("storage_backend", ("postgres", "spanner-bigtable"))
def test_actions_production_requires_memory_storage(storage_backend: str) -> None:
    values: dict[str, object] = {
        "environment": "production",
        "service_surface": "actions",
        "storage_backend": storage_backend,
        "aws_access_key_id": "ses-access",
        "aws_secret_access_key": "ses-secret",
        "ses_from_email": "noreply@example.com",
    }
    if storage_backend == "postgres":
        values["postgres_dsn"] = "postgresql://actions:secret@db/actions"
    else:
        values.update(
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
        )

    with pytest.raises(ValidationError, match="TR_STORAGE_BACKEND=memory"):
        Settings(**values)


@pytest.mark.parametrize(("name", "value", "environment_name"), _ACTION_FORBIDDEN_CONFIG)
def test_actions_production_rejects_non_form_credentials_and_clients(
    name: str,
    value: object,
    environment_name: str,
) -> None:
    overrides: dict[str, object] = {name: value}
    if name == "google_data_manager_enabled":
        overrides.update(
            google_data_manager_account_id="account",
            google_data_manager_signup_action_id="signup",
            google_data_manager_activated_action_id="activation",
            google_data_manager_purchase_action_id="purchase",
            google_data_manager_kms_key_name=(
                "projects/test/locations/global/keyRings/test/cryptoKeys/data-manager"
            ),
        )
    if name == "adyen_enabled":
        overrides.update(
            adyen_api_key="adyen-api",
            adyen_client_key="adyen-client",
            adyen_hmac_key="ab" * 32,
            adyen_reference_key="r" * 32,
            adyen_merchant_account="merchant",
        )
    if name == "veriff_enabled":
        overrides.update(
            veriff_api_key="veriff-api",
            veriff_shared_secret_key="veriff-secret",  # noqa: S106 - test value.
        )
    if name == "x402_enabled":
        overrides.update(
            stripe_secret_key="sk-test",  # noqa: S106 - test value.
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test value.
        )
    if name == "federation_settlement_home_token":
        overrides["federation_home_base_url"] = "https://home.example"

    with pytest.raises(ValidationError, match=f"unset {environment_name}"):
        Settings(
            environment="production",
            service_surface="actions",
            aws_access_key_id="ses-access",
            aws_secret_access_key="ses-secret",  # noqa: S106 - test credential.
            ses_from_email="noreply@example.com",
            **overrides,
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("attribution_cookie_secret", _ATTRIBUTION_SECRET),
        ("internal_gateway_token", _GATEWAY_SECRET),
        ("stripe_webhook_secret", "whsec-test"),
        ("stripe_secret_key", "sk-test"),
        ("sentry_dsn", "https://example@example.ingest.sentry.io/1"),
        ("byok_kms_key_name", "projects/test/cryptoKeys/byok"),
    ),
)
def test_actions_production_rejects_other_surface_secrets(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        Settings(
            environment="production",
            service_surface="actions",
            aws_access_key_id="ses-access",
            aws_secret_access_key="ses-secret",  # noqa: S106 - test credential.
            ses_from_email="noreply@example.com",
            **{name: value},
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("internal_gateway_token", _GATEWAY_SECRET),
        ("stripe_webhook_secret", "whsec-test"),
        ("stripe_secret_key", "sk-test"),
        ("sentry_dsn", "https://example@example.ingest.sentry.io/1"),
        ("aws_access_key_id", "ses-access"),
        ("aws_secret_access_key", "ses-secret"),
        ("byok_kms_key_name", "projects/test/cryptoKeys/byok"),
    ),
)
def test_public_production_rejects_private_surface_secrets(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        _production(
            "public",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            **{name: value},
        )


def test_internal_and_observer_require_gateway_observability_but_not_account_secrets() -> None:
    for surface in ("internal", "observer"):
        settings = _production(
            surface,
            internal_gateway_token=_GATEWAY_SECRET,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
        )
        assert settings.stripe_secret_key is None
        assert settings.aws_access_key_id is None
        assert settings.byok_kms_key_name is None


@pytest.mark.parametrize("surface", ("internal", "observer"))
@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("attribution_cookie_secret", _ATTRIBUTION_SECRET),
        ("stripe_webhook_secret", "whsec-test"),
        ("stripe_secret_key", "sk-test"),
        ("aws_access_key_id", "ses-access"),
        ("aws_secret_access_key", "ses-secret"),
        ("byok_kms_key_name", "projects/test/cryptoKeys/byok"),
    ),
)
def test_non_account_surfaces_reject_account_secrets(
    surface: str,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        _production(
            surface,
            internal_gateway_token=_GATEWAY_SECRET,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            **{name: value},
        )


def test_control_requires_dedicated_attribution_secret() -> None:
    with pytest.raises(ValidationError, match="TR_ATTRIBUTION_COOKIE_SECRET"):
        _production("control", **{k: v for k, v in _CONTROL_SECRETS.items() if k != "attribution_cookie_secret"})

    settings = _production("control", **_CONTROL_SECRETS)
    assert settings.attribution_cookie_secret == _ATTRIBUTION_SECRET


def test_attribution_and_gateway_credentials_cannot_be_reused() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        _production(
            "control",
            **{
                **_CONTROL_SECRETS,
                "attribution_cookie_secret": _GATEWAY_SECRET,
            },
        )


def test_public_cookie_survives_control_handoff_without_sharing_gateway_secret() -> None:
    public = Settings(
        service_surface="public",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
    )
    control = Settings(
        service_surface="control",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
        internal_gateway_token=_GATEWAY_SECRET,
    )
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    touch = {"landing_path": "/", "captured_at": now}
    context = AttributionContext("a" * 32, touch, touch, now)

    encoded = encode_attribution_cookie(context, public)

    assert decode_attribution_cookie(encoded, control) == context
    assert (
        decode_attribution_cookie(
            encoded,
            Settings(
                service_surface="control",
                attribution_cookie_secret="rotated-" + "r" * 32,
                internal_gateway_token=_GATEWAY_SECRET,
            ),
        )
        is None
    )
