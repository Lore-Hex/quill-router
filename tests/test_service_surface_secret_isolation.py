from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from trusted_router.acquisition import (
    AttributionContext,
    decode_attribution_cookie,
    encode_attribution_cookie,
)
from trusted_router.config import SERVICE_SURFACE_SECRET_OWNERS, Settings

_ATTRIBUTION_SECRET = "attribution-only-" + "a" * 32
_GATEWAY_SECRET = "gateway-only-" + "g" * 32
_PRODUCTION_STORAGE = {
    "environment": "production",
    "storage_backend": "spanner-bigtable",
    "spanner_instance_id": "trusted-router",
    "spanner_database_id": "trusted-router",
    "bigtable_instance_id": "trusted-router-logs",
}
_CONSOLE_SECRETS = {
    "attribution_cookie_secret": _ATTRIBUTION_SECRET,
    "stripe_secret_key": "sk-test",
    "paypal_checkout_enabled": False,
    "google_oauth_login_available": False,
    "github_oauth_login_available": False,
    "sentry_dsn": "https://example@example.ingest.sentry.io/1",
    "aws_access_key_id": "ses-access",
    "aws_secret_access_key": "ses-secret",
    "ses_from_email": "noreply@example.com",
    "byok_kms_key_name": (
        "projects/test/locations/global/keyRings/trusted-router/cryptoKeys/byok-envelope"
    ),
}
_INTERNAL_BILLING_SECRETS = {
    "internal_gateway_token": _GATEWAY_SECRET,
    "observer_internal_token": "observer-only-" + "o" * 32,
    # These values model independently provisioned, restricted credentials;
    # they deliberately do not reuse the console/actions secret resources.
    "stripe_secret_key": "rk-test-payment-intents",
    "sentry_dsn": "https://example@example.ingest.sentry.io/1",
    "aws_access_key_id": "internal-ses-access",
    "aws_secret_access_key": "internal-ses-secret",
    "ses_from_email": "noreply@example.com",
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

_SENSITIVE_TEST_VALUES: dict[str, object] = {
    "ops_chat_webhook_secret": "o" * 40,
    "postgres_dsn": "postgresql://surface:secret@db.example/surface",
    "postgres_iam_auth": "aws-dsql",
    "clickhouse_url": "https://clickhouse.example",
    "clickhouse_password": "clickhouse-secret",
    "provider_analytics_clickhouse_url": "https://provider-clickhouse.example",
    "provider_analytics_clickhouse_password": "provider-clickhouse-secret",
    "operational_analytics_clickhouse_url": "https://ops-clickhouse.example",
    "operational_analytics_clickhouse_password": "ops-clickhouse-secret",
    "sentry_dsn": "https://example@example.ingest.sentry.io/1",
    "google_data_manager_enabled": True,
    "google_data_manager_kms_key_name": "projects/test/locations/global/keyRings/gdm/keys/k",
    "attribution_cookie_secret": _ATTRIBUTION_SECRET,
    "internal_gateway_token": _GATEWAY_SECRET,
    "observer_internal_token": "observer-only-" + "o" * 32,
    "stripe_webhook_secret": "whsec-test",
    "stripe_secret_key": "sk-test",
    "paypal_checkout_enabled": True,
    "paypal_client_id": "paypal-client",
    "paypal_client_secret": "paypal-secret",
    "paypal_webhook_id": "paypal-webhook",
    "adyen_enabled": True,
    "adyen_api_key": "adyen-api",
    "adyen_client_key": "adyen-client",
    "adyen_hmac_key": "ab" * 32,
    "adyen_reference_key": "r" * 32,
    "byok_kms_key_name": "projects/test/locations/global/keyRings/byok/keys/k",
    "byok_envelope_key_b64": "ZW52ZWxvcGUta2V5",
    "google_client_id": "google-client",
    "google_client_secret": "google-secret",
    "google_alias_credentials_json": '{"allyrouter.com":{"client_id":"id","client_secret":"secret"}}',
    "github_client_id": "github-client",
    "github_client_secret": "github-secret",
    "github_alias_credentials_json": '{"allyrouter.com":{"client_id":"id","client_secret":"secret"}}',
    "x402_enabled": True,
    "notify_enabled": True,
    "veriff_enabled": True,
    "veriff_api_key": "veriff-api",
    "veriff_shared_secret_key": "veriff-secret",
    "telnyx_api_key": "telnyx-secret",
    "twilio_account_sid": "twilio-account",
    "twilio_auth_token": "twilio-secret",
    "twilio_api_key_secret": "twilio-api-secret",
    "aws_access_key_id": "ses-access",
    "aws_secret_access_key": "ses-secret",
    "synthetic_monitor_api_key": "synthetic-secret",
    "federation_peer_token": "f" * 40,
    "federation_home_token": "h" * 40,
    "federation_credit_inbound_token": "i" * 40,
    "federation_credit_peer_token": "p" * 40,
    "federation_settlement_inbound_tokens": "aws=" + "s" * 40,
    "federation_settlement_home_token": "t" * 40,
}

_EXPECTED_OWNER_GROUPS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset({"actions"}), ("ops_chat_webhook_secret",)),
    (
        frozenset(
            {"public", "console", "chat", "webhooks", "internal", "observer"}
        ),
        (
            "postgres_dsn",
            "postgres_iam_auth",
        ),
    ),
    (
        frozenset({"public", "console", "internal", "observer"}),
        (
            "operational_analytics_clickhouse_url",
            "operational_analytics_clickhouse_password",
        ),
    ),
    (
        frozenset({"console", "internal"}),
        (
            "clickhouse_url",
            "clickhouse_password",
            "byok_kms_key_name",
            "byok_envelope_key_b64",
        ),
    ),
    (
        frozenset({"console"}),
        (
            "provider_analytics_clickhouse_url",
            "provider_analytics_clickhouse_password",
            "google_data_manager_enabled",
            "google_data_manager_kms_key_name",
            "adyen_api_key",
            "adyen_client_key",
            "google_client_id",
            "google_client_secret",
            "google_alias_credentials_json",
            "github_client_id",
            "github_client_secret",
            "github_alias_credentials_json",
            "x402_enabled",
            "notify_enabled",
            "veriff_api_key",
            "telnyx_api_key",
            "twilio_account_sid",
            "twilio_auth_token",
            "twilio_api_key_secret",
        ),
    ),
    (frozenset({"console", "internal"}), ("stripe_secret_key",)),
    (
        frozenset({"console", "webhooks"}),
        (
            "paypal_checkout_enabled",
            "paypal_client_id",
            "paypal_client_secret",
            "adyen_enabled",
            "veriff_enabled",
        ),
    ),
    (frozenset({"webhooks"}), ("paypal_webhook_id", "adyen_hmac_key")),
    (frozenset({"console", "webhooks"}), ("adyen_reference_key",)),
    (frozenset({"webhooks"}), ("stripe_webhook_secret", "veriff_shared_secret_key")),
    (
        frozenset({"console", "chat", "webhooks", "internal", "observer"}),
        ("sentry_dsn",),
    ),
    (frozenset({"public", "console"}), ("attribution_cookie_secret",)),
    (
        frozenset({"internal"}),
        ("internal_gateway_token",),
    ),
    (
        frozenset({"internal", "observer"}),
        ("observer_internal_token",),
    ),
    (
        frozenset({"internal", "observer"}),
        ("synthetic_monitor_api_key",),
    ),
    (
        frozenset({"console", "actions", "internal"}),
        ("aws_access_key_id", "aws_secret_access_key"),
    ),
    (
        frozenset({"internal"}),
        (
            "federation_peer_token",
            "federation_home_token",
            "federation_credit_inbound_token",
            "federation_credit_peer_token",
            "federation_settlement_inbound_tokens",
            "federation_settlement_home_token",
        ),
    ),
)
_EXPECTED_SENSITIVE_SETTING_OWNERS = {
    field_name: owners
    for owners, field_names in _EXPECTED_OWNER_GROUPS
    for field_name in field_names
}
_DEPLOYED_WEB_SURFACES = (
    "public",
    "actions",
    "console",
    "chat",
    "webhooks",
    "internal",
    "observer",
)
_UNAUTHORIZED_SENSITIVE_CASES = tuple(
    (field_name, surface)
    for field_name, owners in sorted(_EXPECTED_SENSITIVE_SETTING_OWNERS.items())
    for surface in _DEPLOYED_WEB_SURFACES
    if surface not in owners
)


def _production(surface: str, **overrides: object) -> Settings:
    values: dict[str, object] = {**_PRODUCTION_STORAGE, "service_surface": surface}
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(("field_name", "surface"), _UNAUTHORIZED_SENSITIVE_CASES)
def test_every_sensitive_setting_rejects_an_unauthorized_deployed_surface(
    field_name: str,
    surface: str,
) -> None:
    assert set(_SENSITIVE_TEST_VALUES) == set(_EXPECTED_SENSITIVE_SETTING_OWNERS)
    assert SERVICE_SURFACE_SECRET_OWNERS == _EXPECTED_SENSITIVE_SETTING_OWNERS
    overrides: dict[str, object] = {field_name: _SENSITIVE_TEST_VALUES[field_name]}
    if surface in {"public", "console"}:
        overrides.setdefault("attribution_cookie_secret", _ATTRIBUTION_SECRET)
    if surface in {"internal", "observer"}:
        overrides.setdefault(
            "observer_internal_token",
            _SENSITIVE_TEST_VALUES["observer_internal_token"],
        )
        if surface == "internal":
            overrides.setdefault("internal_gateway_token", _GATEWAY_SECRET)
            overrides.setdefault("stripe_secret_key", "rk-test-payment-intents")
            overrides.setdefault("aws_access_key_id", "internal-ses-access")
            overrides.setdefault("aws_secret_access_key", "internal-ses-secret")
            overrides.setdefault("ses_from_email", "noreply@example.com")
    if field_name == "ops_chat_webhook_secret":
        overrides["ops_chat_webhook_urls"] = "https://ops.example/hook"
    if field_name == "postgres_iam_auth":
        overrides["postgres_dsn"] = "postgresql://surface:secret@db.example/surface"
    if field_name == "google_data_manager_enabled":
        overrides.update(
            google_data_manager_account_id="account",
            google_data_manager_signup_action_id="signup",
            google_data_manager_activated_action_id="activation",
            google_data_manager_purchase_action_id="purchase",
            google_data_manager_kms_key_name=(
                "projects/test/locations/global/keyRings/gdm/keys/k"
            ),
        )
    if field_name == "adyen_enabled":
        overrides.update(
            adyen_api_key="adyen-api",
            adyen_client_key="adyen-client",
            adyen_hmac_key="ab" * 32,
            adyen_reference_key="r" * 32,
            adyen_merchant_account="merchant",
        )
    if field_name == "x402_enabled":
        overrides.update(
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        )
    if field_name == "veriff_enabled":
        overrides.update(
            veriff_api_key="veriff-api",
            veriff_shared_secret_key="veriff-secret",  # noqa: S106 - test credential.
        )
    if field_name == "federation_settlement_home_token":
        overrides["federation_home_base_url"] = "https://home.example"

    with pytest.raises(
        ValidationError,
        match=f"unset TR_{field_name.upper()} for TR_SERVICE_SURFACE={surface}",
    ):
        Settings(
            environment="canary",
            service_surface=surface,
            **overrides,
        )


def test_public_production_needs_only_attribution_and_non_secret_login_flags() -> None:
    settings = _production(
        "public",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
        google_oauth_login_available=True,
        github_oauth_login_available=False,
    )

    assert settings.attribution_cookie_secret == _ATTRIBUTION_SECRET
    assert settings.internal_gateway_token is None
    assert settings.stripe_secret_key is None
    assert settings.sentry_dsn is None
    assert settings.aws_access_key_id is None
    assert settings.byok_kms_key_name is None
    assert settings.google_oauth_enabled is True
    assert settings.github_oauth_enabled is False


def test_console_rejects_oauth_presentation_capability_drift() -> None:
    with pytest.raises(ValidationError, match="must match the console service"):
        Settings(
            environment="canary",
            service_surface="console",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
            paypal_checkout_enabled=False,
            google_oauth_login_available=True,
            github_oauth_login_available=False,
        )

    settings = Settings(
        environment="canary",
        service_surface="console",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
        stripe_secret_key="sk-test",  # noqa: S106 - test credential.
        paypal_checkout_enabled=False,
        google_oauth_login_available=False,
        github_oauth_login_available=False,
    )
    assert settings.google_oauth_enabled is False


@pytest.mark.parametrize(
    "missing_field",
    ("google_oauth_login_available", "github_oauth_login_available"),
)
def test_console_requires_explicit_oauth_presentation_capabilities(
    missing_field: str,
) -> None:
    capabilities = {
        "google_oauth_login_available": False,
        "github_oauth_login_available": False,
    }
    capabilities.pop(missing_field)

    with pytest.raises(ValidationError, match=f"TR_{missing_field.upper()}=true or false"):
        Settings(
            environment="canary",
            service_surface="console",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
            paypal_checkout_enabled=False,
            **capabilities,
        )


def test_console_accepts_oauth_capabilities_that_exactly_match_credentials() -> None:
    settings = Settings(
        environment="canary",
        service_surface="console",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
        stripe_secret_key="sk-test",  # noqa: S106 - test credential.
        paypal_checkout_enabled=False,
        google_client_id="google-client",
        google_client_secret="google-secret",  # noqa: S106 - test credential.
        google_oauth_login_available=True,
        github_oauth_login_available=False,
    )

    assert settings.google_oauth_enabled is True
    assert settings.github_oauth_enabled is False


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


def test_internal_requires_restricted_billing_and_email_credentials() -> None:
    settings = _production("internal", **_INTERNAL_BILLING_SECRETS)

    assert settings.stripe_secret_key == "rk-test-payment-intents"  # noqa: S105
    assert settings.aws_access_key_id == "internal-ses-access"
    assert settings.aws_secret_access_key == "internal-ses-secret"  # noqa: S105
    assert settings.ses_from_email == "noreply@example.com"
    assert settings.stripe_webhook_secret is None


@pytest.mark.parametrize(
    "missing_field",
    ("stripe_secret_key", "aws_access_key_id", "aws_secret_access_key", "ses_from_email"),
)
def test_internal_fails_closed_when_a_billing_side_effect_credential_is_missing(
    missing_field: str,
) -> None:
    values = dict(_INTERNAL_BILLING_SECRETS)
    values.pop(missing_field)

    with pytest.raises(ValidationError, match=f"TR_{missing_field.upper()}"):
        _production("internal", **values)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("attribution_cookie_secret", _ATTRIBUTION_SECRET),
        ("stripe_webhook_secret", "whsec-test"),
        ("stripe_secret_key", "sk-test"),
        ("aws_access_key_id", "ses-access"),
        ("aws_secret_access_key", "ses-secret"),
    ),
)
def test_observer_rejects_billing_and_account_secrets(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        _production(
            "observer",
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            observer_internal_token=_SENSITIVE_TEST_VALUES["observer_internal_token"],
            **{name: value},
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("attribution_cookie_secret", _ATTRIBUTION_SECRET),
        ("stripe_webhook_secret", "whsec-test"),
    ),
)
def test_internal_rejects_console_and_webhook_only_secrets(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        _production("internal", **_INTERNAL_BILLING_SECRETS, **{name: value})


def test_console_requires_dedicated_attribution_secret() -> None:
    with pytest.raises(ValidationError, match="TR_ATTRIBUTION_COOKIE_SECRET"):
        _production(
            "console",
            **{
                k: v
                for k, v in _CONSOLE_SECRETS.items()
                if k != "attribution_cookie_secret"
            },
        )

    settings = _production("console", **_CONSOLE_SECRETS)
    assert settings.attribution_cookie_secret == _ATTRIBUTION_SECRET
    assert settings.internal_gateway_token is None


def test_console_rejects_the_internal_gateway_credential() -> None:
    with pytest.raises(ValidationError, match="unset TR_INTERNAL_GATEWAY_TOKEN"):
        _production(
            "console",
            **_CONSOLE_SECRETS,
            internal_gateway_token=_GATEWAY_SECRET,
        )


def test_console_x402_needs_checkout_key_but_rejects_webhook_secret() -> None:
    settings = _production("console", **_CONSOLE_SECRETS, x402_enabled=True)

    assert settings.x402_enabled is True
    assert settings.stripe_secret_key == "sk-test"  # noqa: S105 - test credential.
    assert settings.stripe_webhook_secret is None

    with pytest.raises(ValidationError, match="unset TR_STRIPE_WEBHOOK_SECRET"):
        _production(
            "console",
            **_CONSOLE_SECRETS,
            x402_enabled=True,
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        )


def test_paypal_credentials_are_split_between_console_and_webhooks() -> None:
    console = _production(
        "console",
        **{**_CONSOLE_SECRETS, "paypal_checkout_enabled": True},
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106 - test credential.
    )
    assert console.paypal_enabled is True
    assert console.paypal_webhook_id is None

    webhooks = _production(
        "webhooks",
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        paypal_checkout_enabled=True,
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106 - test credential.
        paypal_webhook_id="paypal-webhook",
    )
    assert webhooks.paypal_webhook_ready is True

    with pytest.raises(ValidationError, match="must all be set on the webhooks surface"):
        _production(
            "webhooks",
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
            paypal_checkout_enabled=True,
            paypal_client_id="paypal-client",
            paypal_client_secret="paypal-secret",  # noqa: S106 - test credential.
        )


def test_adyen_checkout_and_webhook_credentials_are_disjoint() -> None:
    console = _production(
        "console",
        **_CONSOLE_SECRETS,
        adyen_enabled=True,
        adyen_api_key="adyen-api",
        adyen_client_key="adyen-client",
        adyen_reference_key="r" * 32,
        adyen_merchant_account="merchant",
    )
    assert console.adyen_checkout_ready is True
    assert console.adyen_hmac_key is None

    webhooks = _production(
        "webhooks",
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        paypal_checkout_enabled=False,
        adyen_hmac_key="ab" * 32,
        adyen_reference_key="r" * 32,
        adyen_merchant_account="merchant",
    )
    assert webhooks.adyen_webhook_ready is True
    assert webhooks.adyen_api_key is None
    assert webhooks.adyen_client_key is None


def test_veriff_session_and_callback_credentials_are_disjoint() -> None:
    console = _production(
        "console",
        **_CONSOLE_SECRETS,
        veriff_enabled=True,
        veriff_api_key="veriff-api",
    )
    assert console.veriff_configured is True
    assert console.veriff_shared_secret_key is None

    webhooks = _production(
        "webhooks",
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        paypal_checkout_enabled=False,
        veriff_shared_secret_key="veriff-shared",  # noqa: S106 - test credential.
    )
    assert webhooks.veriff_api_key is None

    with pytest.raises(ValidationError, match="unset TR_VERIFF_SHARED_SECRET_KEY"):
        _production(
            "console",
            **_CONSOLE_SECRETS,
            veriff_enabled=True,
            veriff_api_key="veriff-api",
            veriff_shared_secret_key="veriff-shared",  # noqa: S106 - test credential.
        )
    with pytest.raises(ValidationError, match="unset TR_VERIFF_API_KEY"):
        _production(
            "webhooks",
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
            paypal_checkout_enabled=False,
            veriff_shared_secret_key="veriff-shared",  # noqa: S106 - test credential.
            veriff_api_key="veriff-api",
        )


@pytest.mark.parametrize(
    ("surface", "surface_secrets"),
    [
        ("chat", {}),
        (
            "webhooks",
            {
                "stripe_webhook_secret": "whsec-test",
                "paypal_checkout_enabled": False,
            },
        ),
    ],
)
def test_narrow_store_surfaces_do_not_receive_clickhouse_credentials(
    surface: str,
    surface_secrets: dict[str, object],
) -> None:
    settings = _production(
        surface,
        storage_backend="spanner-clickhouse",
        analytics_read_mode="clickhouse-only",
        generation_records_enabled=True,
        operational_analytics_outbox_enabled=True,
        analytics_outbox_enabled=True,
        bigtable_mirror_writes_enabled=False,
        request_record_write_mode="typed",
        settle_outbox_enabled=True,
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        **surface_secrets,
    )

    assert settings.operational_analytics_clickhouse_url == ""
    assert settings.operational_analytics_clickhouse_password == ""


def test_deployed_canary_surfaces_enforce_secret_ownership_too() -> None:
    with pytest.raises(ValidationError, match="TR_STRIPE_SECRET_KEY"):
        Settings(
            environment="canary",
            service_surface="console",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            paypal_checkout_enabled=False,
        )

    with pytest.raises(ValidationError, match="unset TR_INTERNAL_GATEWAY_TOKEN"):
        Settings(
            environment="canary",
            service_surface="console",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
            paypal_checkout_enabled=False,
            internal_gateway_token=_GATEWAY_SECRET,
        )

    with pytest.raises(ValidationError, match="TR_OBSERVER_INTERNAL_TOKEN"):
        Settings(
            environment="canary",
            service_surface="observer",
            storage_backend="postgres",
            postgres_dsn="postgresql://observer:secret@db/observer",
        )

    with pytest.raises(ValidationError, match="TR_STORAGE_BACKEND=memory"):
        Settings(
            environment="canary",
            service_surface="actions",
            storage_backend="postgres",
            postgres_dsn="postgresql://actions:secret@db/actions",
        )


def test_attribution_and_gateway_credentials_cannot_be_reused() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        _production(
            "combined",
            **{
                **_CONSOLE_SECRETS,
                "internal_gateway_token": _GATEWAY_SECRET,
                "attribution_cookie_secret": _GATEWAY_SECRET,
            },
        )


@pytest.mark.parametrize(
    "other_field",
    ("internal_gateway_token", "synthetic_monitor_api_key"),
)
def test_observer_credential_cannot_reuse_another_security_domain(
    other_field: str,
) -> None:
    reused = "reused-secret-" + "r" * 32
    values: dict[str, object] = {
        "environment": "canary",
        "service_surface": "observer",
        "observer_internal_token": reused,
        other_field: reused,
    }
    if other_field == "internal_gateway_token":
        # The ownership violation is also intentional; the equality reason
        # must remain present and must not expose the value in its traceback.
        expected = "must differ from TR_INTERNAL_GATEWAY_TOKEN"
    else:
        expected = "must differ from TR_SYNTHETIC_MONITOR_API_KEY"

    with pytest.raises(ValidationError, match=expected):
        Settings(**values)


def test_deployed_validation_errors_hide_misrouted_secret_values() -> None:
    canary = "SUPERSECRET_CANARY_123"

    with pytest.raises(ValidationError) as captured:
        Settings(
            environment="canary",
            service_surface="public",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            internal_gateway_token=canary,
        )

    message = str(captured.value)
    assert "TR_INTERNAL_GATEWAY_TOKEN" in message
    assert canary not in message


def test_public_cookie_survives_console_handoff_without_sharing_gateway_secret() -> None:
    public = Settings(
        service_surface="public",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
    )
    console = Settings(
        service_surface="console",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
    )
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    touch = {"landing_path": "/", "captured_at": now}
    context = AttributionContext("a" * 32, touch, touch, now)

    encoded = encode_attribution_cookie(context, public)

    assert decode_attribution_cookie(encoded, console) == context
    assert (
        decode_attribution_cookie(
            encoded,
            Settings(
                service_surface="console",
                attribution_cookie_secret="rotated-" + "r" * 32,
            ),
        )
        is None
    )
