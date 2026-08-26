from __future__ import annotations

import base64
import datetime as dt

import pytest
from pydantic import ValidationError

from tests.route_inventory import route_paths
from trusted_router.acquisition import (
    AttributionContext,
    decode_attribution_cookie,
    encode_attribution_cookie,
)
from trusted_router.config import SERVICE_SURFACE_SECRET_OWNERS, Settings
from trusted_router.main import create_app

_ATTRIBUTION_SECRET = "attribution-only-" + "a" * 32
_ATTRIBUTION_KEY = "aDMnBV9nDwwAD1tr4MpooFMj7i8Kv6lB5Q9LTmrjTfc="
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
    (
        "operational_analytics_clickhouse_write_password",
        "ops-clickhouse-write-secret",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_WRITE_PASSWORD",
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
    "operational_analytics_clickhouse_write_password": "ops-clickhouse-write-secret",
    "sentry_dsn": "https://example@example.ingest.sentry.io/1",
    "google_data_manager_enabled": True,
    "google_data_manager_kms_key_name": "projects/test/locations/global/keyRings/gdm/keys/k",
    "attribution_cookie_key": _ATTRIBUTION_KEY,
    "attribution_cookie_secret": _ATTRIBUTION_SECRET,
    "internal_gateway_token": _GATEWAY_SECRET,
    "observer_internal_token": "observer-only-" + "o" * 32,
    "stripe_webhook_secret": "whsec-test",
    "stripe_secret_key": "sk-test",
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
        frozenset({"public", "control", "internal", "observer"}),
        (
            "postgres_dsn",
            "postgres_iam_auth",
            "operational_analytics_clickhouse_url",
            "operational_analytics_clickhouse_password",
        ),
    ),
    (
        frozenset({"control", "internal"}),
        (
            "clickhouse_url",
            "clickhouse_password",
            "operational_analytics_clickhouse_write_password",
        ),
    ),
    (
        frozenset({"control"}),
        (
            "provider_analytics_clickhouse_url",
            "provider_analytics_clickhouse_password",
            "byok_kms_key_name",
            "byok_envelope_key_b64",
            "google_data_manager_enabled",
            "google_data_manager_kms_key_name",
            "stripe_webhook_secret",
            "stripe_secret_key",
            "paypal_client_id",
            "paypal_client_secret",
            "paypal_webhook_id",
            "adyen_enabled",
            "adyen_api_key",
            "adyen_client_key",
            "adyen_hmac_key",
            "adyen_reference_key",
            "google_client_id",
            "google_client_secret",
            "google_alias_credentials_json",
            "github_client_id",
            "github_client_secret",
            "github_alias_credentials_json",
            "x402_enabled",
            "notify_enabled",
            "veriff_enabled",
            "veriff_api_key",
            "veriff_shared_secret_key",
            "telnyx_api_key",
            "twilio_account_sid",
            "twilio_auth_token",
            "twilio_api_key_secret",
        ),
    ),
    (frozenset({"public", "control", "internal", "observer"}), ("sentry_dsn",)),
    (
        frozenset({"public", "control"}),
        ("attribution_cookie_key", "attribution_cookie_secret"),
    ),
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
        frozenset({"control", "actions"}),
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
_DEPLOYED_WEB_SURFACES = ("public", "actions", "control", "internal", "observer")
_UNAUTHORIZED_SENSITIVE_CASES = tuple(
    (field_name, surface)
    for field_name, owners in sorted(_EXPECTED_SENSITIVE_SETTING_OWNERS.items())
    for surface in _DEPLOYED_WEB_SURFACES
    if surface not in owners
)


def _production(surface: str, **overrides: object) -> Settings:
    values: dict[str, object] = {**_PRODUCTION_STORAGE, "service_surface": surface}
    if surface == "internal":
        values["settle_outbox_enabled"] = True
    values.update(overrides)
    return Settings(**values)


def _full_combined_bridge_production() -> dict[str, object]:
    """Legacy GCP bindings spanning every future split-service owner."""
    return {
        **_PRODUCTION_STORAGE,
        **_SENSITIVE_TEST_VALUES,
        # The legacy bridge uses the original root. The newer mutual-exclusion
        # contract is asserted by test_cookie_key_and_secret_are_ambiguous.
        "attribution_cookie_key": None,
        "service_surface": "combined",
        "allow_deployed_combined_surface": True,
        "rate_limit_enabled": False,
        "ses_from_email": "noreply@example.com",
        "ops_chat_webhook_urls": "https://ops.example/hook",
        "google_data_manager_account_id": "account",
        "google_data_manager_signup_action_id": "signup",
        "google_data_manager_activated_action_id": "activated",
        "google_data_manager_purchase_action_id": "purchase",
        "adyen_merchant_account": "merchant",
        "federation_home_base_url": "https://home.example",
        # The test credential fixtures contain exactly this alias.
        "trusted_domain_aliases": "allyrouter.com",
    }


def test_production_internal_surface_requires_durable_settle_outbox() -> None:
    with pytest.raises(ValidationError, match="TR_SETTLE_OUTBOX_ENABLED=true"):
        Settings(
            **_PRODUCTION_STORAGE,
            service_surface="internal",
            internal_gateway_token=_GATEWAY_SECRET,
            observer_internal_token=_SENSITIVE_TEST_VALUES["observer_internal_token"],
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            settle_outbox_enabled=False,
        )


def test_production_combined_rejects_full_bindings_without_the_bridge() -> None:
    values = _full_combined_bridge_production()
    values.pop("allow_deployed_combined_surface")

    with pytest.raises(
        ValidationError,
        match="TR_ALLOW_DEPLOYED_COMBINED_SURFACE",
    ):
        Settings(**values)


@pytest.mark.parametrize(
    "field_name",
    ("internal_gateway_token", "stripe_webhook_secret", "stripe_secret_key"),
)
def test_combined_bridge_preserves_legacy_required_credentials(field_name: str) -> None:
    values = _full_combined_bridge_production()
    values[field_name] = None

    with pytest.raises(ValidationError, match=f"TR_{field_name.upper()}"):
        Settings(**values)


def test_combined_bridge_requires_the_legacy_rate_limiter_to_stay_off() -> None:
    values = _full_combined_bridge_production()
    values["rate_limit_enabled"] = True

    with pytest.raises(ValidationError, match="TR_RATE_LIMIT_ENABLED=false"):
        Settings(**values)


@pytest.mark.parametrize(
    ("surface", "overrides"),
    (
        (
            "public",
            {
                "attribution_cookie_secret": _ATTRIBUTION_SECRET,
                "google_oauth_login_available": False,
                "github_oauth_login_available": False,
            },
        ),
        ("actions", {}),
        (
            "control",
            {
                "attribution_cookie_secret": _ATTRIBUTION_SECRET,
                "stripe_webhook_secret": "whsec-test",
                "stripe_secret_key": "sk-test",
            },
        ),
        (
            "internal",
            {
                "internal_gateway_token": _GATEWAY_SECRET,
                "observer_internal_token": _SENSITIVE_TEST_VALUES[
                    "observer_internal_token"
                ],
            },
        ),
        (
            "observer",
            {
                "observer_internal_token": _SENSITIVE_TEST_VALUES[
                    "observer_internal_token"
                ],
            },
        ),
    ),
)
def test_split_deployed_surfaces_keep_their_rate_limiter_default(
    surface: str,
    overrides: dict[str, object],
) -> None:
    settings = Settings(environment="canary", service_surface=surface, **overrides)

    assert settings.rate_limit_enabled is True


def test_combined_bridge_accepts_full_legacy_bindings_and_mounts_all_routes() -> None:
    settings = Settings(**_full_combined_bridge_production())

    for field_name, expected in _SENSITIVE_TEST_VALUES.items():
        if field_name == "attribution_cookie_key":
            expected = None
        assert getattr(settings, field_name) == expected
    app = create_app(
        settings,
        configure_store_arg=False,
        init_observability=False,
    )
    paths = route_paths(app)
    assert {"/", "/console", "/internal/gateway/authorize"} <= paths


@pytest.mark.parametrize(("field_name", "surface"), _UNAUTHORIZED_SENSITIVE_CASES)
def test_every_sensitive_setting_rejects_an_unauthorized_deployed_surface(
    field_name: str,
    surface: str,
) -> None:
    assert set(_SENSITIVE_TEST_VALUES) == set(_EXPECTED_SENSITIVE_SETTING_OWNERS)
    assert SERVICE_SURFACE_SECRET_OWNERS == _EXPECTED_SENSITIVE_SETTING_OWNERS
    overrides: dict[str, object] = {field_name: _SENSITIVE_TEST_VALUES[field_name]}
    if surface in {"public", "control"}:
        overrides.setdefault("attribution_cookie_secret", _ATTRIBUTION_SECRET)
    if surface in {"internal", "observer"}:
        overrides.setdefault(
            "observer_internal_token",
            _SENSITIVE_TEST_VALUES["observer_internal_token"],
        )
        if surface == "internal":
            overrides.setdefault("internal_gateway_token", _GATEWAY_SECRET)
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


def test_public_production_accepts_only_t1_owned_secrets_and_login_flags() -> None:
    settings = _production(
        "public",
        attribution_cookie_key=_ATTRIBUTION_KEY,
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        google_oauth_login_available=True,
        github_oauth_login_available=False,
    )

    assert settings.attribution_cookie_key == _ATTRIBUTION_KEY
    assert settings.attribution_cookie_secret is None
    assert settings.internal_gateway_token is None
    assert settings.observer_internal_token is None
    assert settings.stripe_secret_key is None
    assert settings.sentry_dsn == "https://example@example.ingest.sentry.io/1"
    assert settings.aws_access_key_id is None
    assert settings.byok_kms_key_name is None
    assert settings.google_oauth_enabled is True
    assert settings.github_oauth_enabled is False


def test_control_rejects_oauth_presentation_capability_drift() -> None:
    with pytest.raises(ValidationError, match="must match the control service"):
        Settings(
            environment="canary",
            service_surface="control",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
            google_oauth_login_available=True,
            github_oauth_login_available=False,
        )

    settings = Settings(
        environment="canary",
        service_surface="control",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
        stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
        stripe_secret_key="sk-test",  # noqa: S106 - test credential.
        google_oauth_login_available=False,
        github_oauth_login_available=False,
    )
    assert settings.google_oauth_enabled is False


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
        ("observer_internal_token", "observer-only-" + "o" * 32),
        ("stripe_webhook_secret", "whsec-test"),
        ("stripe_secret_key", "sk-test"),
        # Sentry is intentionally absent: the newer T1 error-reporting
        # contract permits it on public and is exercised by
        # test_public_production_accepts_only_t1_owned_secrets_and_login_flags
        # plus the exact deploy allowlist test in test_public_surface_deploy.py.
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
        auth = {
            "observer_internal_token": _SENSITIVE_TEST_VALUES["observer_internal_token"]
        }
        if surface == "internal":
            auth["internal_gateway_token"] = _GATEWAY_SECRET
        settings = _production(
            surface,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            **auth,
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
    ),
)
def test_non_account_surfaces_reject_account_secrets(
    surface: str,
    name: str,
    value: str,
) -> None:
    auth = {
        "observer_internal_token": _SENSITIVE_TEST_VALUES["observer_internal_token"]
    }
    if surface == "internal":
        auth["internal_gateway_token"] = _GATEWAY_SECRET
    with pytest.raises(ValidationError, match=f"unset TR_{name.upper()}"):
        _production(
            surface,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            **auth,
            **{name: value},
        )


def test_control_requires_exactly_one_attribution_key_source() -> None:
    without_attribution = {
        key: value
        for key, value in _CONTROL_SECRETS.items()
        if key != "attribution_cookie_secret"
    }
    with pytest.raises(
        ValidationError,
        match="TR_ATTRIBUTION_COOKIE_KEY or TR_ATTRIBUTION_COOKIE_SECRET",
    ):
        _production("control", **without_attribution)

    secret_settings = _production("control", **_CONTROL_SECRETS)
    assert secret_settings.attribution_cookie_secret == _ATTRIBUTION_SECRET
    assert secret_settings.attribution_cookie_key is None
    assert secret_settings.internal_gateway_token is None

    key_settings = _production(
        "control",
        **without_attribution,
        attribution_cookie_key=_ATTRIBUTION_KEY,
    )
    assert key_settings.attribution_cookie_key == _ATTRIBUTION_KEY
    assert key_settings.attribution_cookie_secret is None


def test_cookie_key_must_decode_to_exactly_32_bytes() -> None:
    short_key = base64.b64encode(b"too short").decode("ascii")

    with pytest.raises(
        ValidationError,
        match="TR_ATTRIBUTION_COOKIE_KEY must decode to exactly 32 bytes",
    ):
        Settings(attribution_cookie_key=short_key)


def test_cookie_key_must_be_valid_base64() -> None:
    with pytest.raises(
        ValidationError,
        match="TR_ATTRIBUTION_COOKIE_KEY must be valid base64",
    ):
        Settings(attribution_cookie_key="not+valid/base64===garbage")


def test_cookie_key_and_secret_are_ambiguous() -> None:
    with pytest.raises(
        ValidationError,
        match=(
            "TR_ATTRIBUTION_COOKIE_KEY and TR_ATTRIBUTION_COOKIE_SECRET "
            "must not both be set"
        ),
    ):
        Settings(
            attribution_cookie_key=_ATTRIBUTION_KEY,
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
        )


def test_cookie_key_cannot_equal_raw_gateway_token_bytes() -> None:
    gateway_token = "g" * 32
    encoded_gateway_token = base64.b64encode(gateway_token.encode()).decode("ascii")

    with pytest.raises(
        ValidationError,
        match="TR_ATTRIBUTION_COOKIE_KEY must differ from TR_INTERNAL_GATEWAY_TOKEN",
    ):
        Settings(
            environment="canary",
            service_surface="public",
            attribution_cookie_key=encoded_gateway_token,
            internal_gateway_token=gateway_token,
            google_oauth_login_available=False,
            github_oauth_login_available=False,
        )


def test_control_rejects_the_internal_gateway_credential() -> None:
    with pytest.raises(ValidationError, match="unset TR_INTERNAL_GATEWAY_TOKEN"):
        _production(
            "control",
            **_CONTROL_SECRETS,
            internal_gateway_token=_GATEWAY_SECRET,
        )


def test_deployed_canary_surfaces_enforce_secret_ownership_too() -> None:
    with pytest.raises(ValidationError, match="TR_STRIPE_WEBHOOK_SECRET"):
        Settings(
            environment="canary",
            service_surface="control",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
        )

    with pytest.raises(ValidationError, match="unset TR_INTERNAL_GATEWAY_TOKEN"):
        Settings(
            environment="canary",
            service_surface="control",
            attribution_cookie_secret=_ATTRIBUTION_SECRET,
            stripe_webhook_secret="whsec-test",  # noqa: S106 - test credential.
            stripe_secret_key="sk-test",  # noqa: S106 - test credential.
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
                **_CONTROL_SECRETS,
                "allow_deployed_combined_surface": True,
                "rate_limit_enabled": False,
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


def test_public_cookie_survives_control_handoff_without_sharing_gateway_secret() -> None:
    public = Settings(
        service_surface="public",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
    )
    control = Settings(
        service_surface="control",
        attribution_cookie_secret=_ATTRIBUTION_SECRET,
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
            ),
        )
        is None
    )
