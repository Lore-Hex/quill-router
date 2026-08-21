from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
import sentry_sdk
import sentry_sdk.integrations.logging as sentry_logging
from fastapi.testclient import TestClient

import trusted_router.sentry_config as sentry_config
from trusted_router.auth import bootstrap_management_key
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.sentry_config import (
    SENSITIVE_STRING_FRAGMENTS,
    SENSITIVE_STRING_PREFIXES,
    SENTRY_FAILED_REQUEST_STATUS_CODES,
    before_breadcrumb,
    before_send,
    before_send_log,
    init_sentry,
    reset_sentry_floodgate_for_tests,
    sentry_should_init,
)
from trusted_router.storage import STORE

TEST_BYOK_KMS_KEY_NAME = (
    "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
)


def test_bootstrap_management_key_is_opt_in_and_idempotent() -> None:
    assert bootstrap_management_key(Settings(environment="test")) is None

    bootstrap_key = "sk-tr-v1-" + "bootstrap-test"
    settings = Settings(environment="test", bootstrap_management_key=bootstrap_key)
    first = bootstrap_management_key(settings)
    second = bootstrap_management_key(settings)

    assert first is not None
    assert second is first
    assert first.management is True
    assert STORE.get_key_by_raw(bootstrap_key) is first


def test_configured_internal_gateway_token_is_required_even_in_test(user_headers: dict[str, str]) -> None:
    internal_token = "internal" + "-test-token"
    app = create_app(Settings(environment="test", internal_gateway_token=internal_token))
    client = TestClient(app)
    key = client.post("/v1/keys", headers=user_headers, json={"name": "gateway"}).json()["data"]
    body = {
        "api_key_hash": key["hash"],
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 1,
        "max_output_tokens": 1,
    }

    missing = client.post("/v1/internal/gateway/authorize", json=body)
    wrong = client.post(
        "/v1/internal/gateway/authorize",
        headers={"x-trustedrouter-internal-token": "wrong"},
        json=body,
    )
    correct = client.post(
        "/v1/internal/gateway/authorize",
        headers={"x-trustedrouter-internal-token": internal_token},
        json=body,
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"]["type"] == "unauthorized"
    assert wrong.json()["error"]["type"] == "unauthorized"
    assert correct.status_code == 200, correct.text


def test_sentry_test_route_requires_internal_token_not_management_auth() -> None:
    internal_token = "internal" + "-sentry-test"
    app = create_app(Settings(environment="test", observer_internal_token=internal_token))
    client = TestClient(app, raise_server_exceptions=False)

    missing = client.get("/v1/internal/sentry-test")
    management = client.get(
        "/v1/internal/sentry-test",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    correct = client.get(
        "/v1/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": internal_token},
    )

    assert missing.status_code == 401
    assert management.status_code == 401
    assert correct.status_code == 500


def test_sentry_test_route_is_disabled_in_production_unless_explicitly_enabled() -> None:
    base_settings = dict(
        environment="production",
        service_surface="internal",
        internal_gateway_token="internal-prod-sentry-test",  # noqa: S106 - test config.
        observer_internal_token="observer-prod-sentry-test",  # noqa: S106 - test config.
        stripe_secret_key="rk_test_payment_intents",  # noqa: S106 - test config.
        aws_access_key_id="internal-ses-access",
        aws_secret_access_key="internal-ses-secret",  # noqa: S106 - test config.
        ses_from_email="noreply@example.com",
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="trusted-router",
        spanner_database_id="trusted-router",
        bigtable_instance_id="trusted-router-logs",
    )
    disabled = TestClient(
        create_app(
            Settings(**base_settings),
            configure_store_arg=False,
            init_observability=False,
        ),
        raise_server_exceptions=False,
    )
    enabled = TestClient(
        create_app(
            Settings(**base_settings, enable_sentry_test_route=True),
            configure_store_arg=False,
            init_observability=False,
        ),
        raise_server_exceptions=False,
    )

    disabled_resp = disabled.get(
        "/v1/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": "observer-prod-sentry-test"},
    )
    enabled_resp = enabled.get(
        "/v1/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": "observer-prod-sentry-test"},
    )

    assert disabled_resp.status_code == 404
    assert enabled_resp.status_code == 500


def test_production_rejects_spoofable_user_header_auth() -> None:
    stripe_key = "sk_" + "test_secret"
    prod_client = TestClient(
        create_app(
            Settings(
                environment="production",
                service_surface="console",
                attribution_cookie_secret="attribution-cookie-" + "a" * 32,
                stripe_secret_key=stripe_key,
                google_oauth_login_available=False,
                github_oauth_login_available=False,
                paypal_checkout_enabled=False,
                sentry_dsn="https://example@example.ingest.sentry.io/1",
                aws_access_key_id="test-access-key",
                aws_secret_access_key="test-secret-key",  # noqa: S106 - test fixture.
                ses_from_email="noreply@example.com",
                storage_backend="spanner-bigtable",
                spanner_instance_id="trusted-router",
                spanner_database_id="trusted-router",
                bigtable_instance_id="trusted-router-logs",
                byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
            ),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = prod_client.get("/v1/keys", headers={"x-trustedrouter-user": "alice@example.com"})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_stripe_webhook_signature_is_required_when_secret_configured(monkeypatch) -> None:
    webhook_secret = "whsec_" + "test"
    app = create_app(Settings(environment="test", stripe_webhook_secret=webhook_secret))
    client = TestClient(app)
    workspace_id = client.get("/v1/workspaces", headers={"x-trustedrouter-user": "alice@example.com"}).json()[
        "data"
    ][0]["id"]
    event = {
        "id": "evt_signed",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 321,
                "payment_status": "paid",
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }
    raw_event = json.dumps(event, separators=(",", ":")).encode()
    captured: dict[str, object] = {}

    def construct_event(raw: bytes, signature: str | None, secret: str):
        captured["raw"] = raw
        captured["signature"] = signature
        captured["secret"] = secret
        return event

    monkeypatch.setattr(
        "trusted_router.routes.internal.webhook.stripe.Webhook.construct_event",
        construct_event,
    )

    signed = client.post(
        "/v1/internal/stripe/webhook",
        content=raw_event,
        headers={"stripe-signature": "signed-header", "content-type": "application/json"},
    )

    assert signed.status_code == 200, signed.text
    assert captured == {"raw": raw_event, "signature": "signed-header", "secret": webhook_secret}
    assert signed.json()["data"]["credited"] is True


def test_stripe_webhook_rejects_bad_signature_when_secret_configured(monkeypatch) -> None:
    webhook_secret = "whsec_" + "test"
    app = create_app(Settings(environment="test", stripe_webhook_secret=webhook_secret))
    client = TestClient(app)

    def construct_event(_raw: bytes, _signature: str | None, _secret: str):
        raise ValueError("bad signature")

    monkeypatch.setattr(
        "trusted_router.routes.internal.webhook.stripe.Webhook.construct_event",
        construct_event,
    )

    rejected = client.post(
        "/v1/internal/stripe/webhook",
        json={"id": "evt_bad", "type": "checkout.session.completed"},
        headers={"stripe-signature": "bad"},
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["type"] == "bad_request"


def test_deployed_stripe_webhook_fails_closed_without_verification_secret() -> None:
    # Bypass Settings' startup validation to independently pin the route's
    # defense in depth. A future construction-path regression still must not
    # make an Internet-deployed webhook trust raw JSON.
    settings = Settings(environment="test", service_surface="webhooks")
    settings.environment = "canary"
    client = TestClient(create_app(settings, init_observability=False))

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={"id": "evt-forged", "type": "forged"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"


def test_sentry_scrubber_redacts_every_declared_prefix_and_fragment() -> None:
    """Sentry's `_scrub_string` is a hand-rolled blocklist that quietly rots
    when a new secret format ships. We iterate every declared fragment +
    prefix from SENSITIVE_STRING_FRAGMENTS / SENSITIVE_STRING_PREFIXES so
    adding an entry there automatically gets a regression test, and removing
    one accidentally is caught by the test failing."""
    leaked: dict[str, str] = {}
    for fragment in SENSITIVE_STRING_FRAGMENTS:
        # Embed in a longer string so we exercise the substring match path.
        leaked[f"frag_{fragment}"] = f"prefix-{fragment}-suffix"
    for prefix in SENSITIVE_STRING_PREFIXES:
        leaked[f"pref_{prefix}"] = f"{prefix}REDACTME-{prefix}"

    event = {
        "extra": leaked,
        "breadcrumbs": [{"message": f"saw {value}"} for value in leaked.values()],
        "tags": {"safe": "kept"},
    }
    scrubbed = before_send(event, {})
    assert scrubbed is not None
    text = json.dumps(scrubbed, sort_keys=True)
    for value in leaked.values():
        assert value not in text, f"scrubber leaked {value}"
    assert "kept" in text  # benign tags survive the walk


def test_sentry_hooks_scrub_logs_breadcrumbs_and_request_bodies_without_mutating_original() -> None:
    event = {
        "request": {
            "headers": {"Authorization": "Bearer sk-tr-v1-secret", "Cookie": "session=secret"},
            "data": {"messages": [{"role": "user", "content": "private prompt"}]},
            "cookies": {"session": "secret"},
        },
        "extra": {"safe": "ok", "output_text": "private answer"},
    }
    original = json.loads(json.dumps(event))

    scrubbed = before_send(event, {})
    assert scrubbed is not None
    text = json.dumps(scrubbed, sort_keys=True)
    assert "sk-tr-v1-secret" not in text
    assert "private prompt" not in text
    assert "private answer" not in text
    assert "session=secret" not in text
    assert "ok" in text
    assert event == original

    log = before_send_log(
        {"message": "provider failed with sk-tr-v1-secret", "attributes": {"api_key": "raw"}},
        {},
    )
    crumb = before_breadcrumb(
        {"message": "request failed with sk-or-v1-secret", "data": {"prompt": "private prompt"}},
        {},
    )
    assert log is not None
    assert crumb is not None
    assert "sk-tr-v1-secret" not in json.dumps(log)
    assert "sk-or-v1-secret" not in json.dumps(crumb)
    assert "private prompt" not in json.dumps(crumb)


def test_sentry_drops_spanner_client_metrics_export_noise() -> None:
    noisy = {
        "level": "error",
        "message": (
            "Failed to export metrics to Cloud Monitoring: 400 One or more TimeSeries "
            "could not be written: metric.type=\"spanner.googleapis.com/internal/client/"
            "operation_latencies\", resource.type=\"spanner_instance_client\": "
            "the set of resource labels is incomplete, missing (instance_id)"
        ),
    }

    assert before_send(noisy, {}) is None
    assert before_send_log(noisy, {}) is None
    assert before_breadcrumb(noisy, {}) is None


def test_sentry_failed_request_statuses_exclude_expected_compatibility_501() -> None:
    assert 405 in SENTRY_FAILED_REQUEST_STATUS_CODES
    assert 500 in SENTRY_FAILED_REQUEST_STATUS_CODES
    assert 501 not in SENTRY_FAILED_REQUEST_STATUS_CODES
    assert 502 in SENTRY_FAILED_REQUEST_STATUS_CODES
    assert 599 in SENTRY_FAILED_REQUEST_STATUS_CODES


def _method_not_allowed_event(
    url: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | list[list[str]] | None = None,
) -> dict[str, object]:
    return {
        "level": "error",
        "request": {"method": method, "url": url, "headers": headers or {}},
        "exception": {
            "values": [
                {
                    "type": "HTTPException",
                    "value": "Method Not Allowed",
                }
            ]
        },
    }


@pytest.mark.parametrize(
    ("method", "url", "headers"),
    [
        (
            "GET",
            "https://trustedrouter.com/v1/internal/synthetic/route-health",
            {"User-Agent": "meta-externalagent/1.1"},
        ),
        ("POST", "https://35.241.14.18/", {"User-Agent": "scanner"}),
        (
            "POST",
            "https://trustedrouter.com/?rest_route=%2Fbatch%2Fv1",
            {"User-Agent": "wp2shell"},
        ),
        (
            "POST",
            "https://trustedrouter.com/console/credits",
            {"Origin": "https://attacker.example"},
        ),
    ],
)
def test_sentry_drops_untrusted_method_not_allowed_noise(
    method: str,
    url: str,
    headers: dict[str, str],
) -> None:
    reset_sentry_floodgate_for_tests()
    assert before_send(
        _method_not_allowed_event(url, method=method, headers=headers),
        {},
    ) is None


@pytest.mark.parametrize(
    "headers",
    [
        {"Referer": "https://trustedrouter.com/console/settings"},
        [["Origin", "https://trustedrouter.com"]],
        {"X-TrustedRouter-Internal-Token": "filtered-secret"},
    ],
)
def test_sentry_drops_405_with_spoofable_same_origin_or_unverified_internal_headers(
    headers: dict[str, str] | list[list[str]],
) -> None:
    reset_sentry_floodgate_for_tests()
    event = _method_not_allowed_event(
        "https://trustedrouter.com/console/settings?source=console",
        headers=headers,
    )

    original = json.loads(json.dumps(event))

    assert before_send(event, {}) is None
    assert event == original


def test_sentry_keeps_authenticated_internal_405_and_uses_server_route_identity() -> None:
    internal_token = "observer" + "-sentry-token"
    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            observer_internal_token=internal_token,
        )
    )
    event = _method_not_allowed_event(
        "https://trustedrouter.com/attacker-controlled/raw/path?source=probe",
        headers={"X-TrustedRouter-Internal-Token": internal_token},
    )
    event["transaction"] = "trusted_router.routes.internal.synthetic.route_health"
    event["transaction_info"] = {"source": "component"}

    captured = before_send(event, {})

    assert captured is not None
    assert captured["fingerprint"] == [
        "http-405",
        "POST",
        "trusted_router.routes.internal.synthetic.route_health",
    ]
    assert "fingerprint" not in event
    reset_sentry_floodgate_for_tests()


@pytest.mark.parametrize(
    ("url", "referer", "user_agent"),
    [
        (
            "https://uptimerouter.com/support/inquiry",
            "https://uptimerouter.com/support",
            (
                "Mozilla/5.0 (compatible; heritrix/3.14.2-SNAPSHOT "
                "+https://www.image-meta.com)"
            ),
        ),
        (
            "https://uptimerouter.com/analytics/events",
            "https://uptimerouter.com/static/dashboard.js",
            (
                "Mozilla/5.0 (compatible; heritrix/3.14.2-SNAPSHOT "
                "+https://www.image-meta.com)"
            ),
        ),
        (
            "https://uptimerouter.com/",
            "https://uptimerouter.com/docs",
            "Mozilla/5.0 (compatible; Amzn-SearchBot/1.0)",
        ),
    ],
)
def test_sentry_drops_crawler_405_even_with_same_origin_referer(
    url: str,
    referer: str,
    user_agent: str,
) -> None:
    reset_sentry_floodgate_for_tests()
    event = _method_not_allowed_event(
        url,
        method="GET",
        headers={
            "Referer": referer,
            "User-Agent": user_agent,
        },
    )

    assert before_send(event, {}) is None


def test_sentry_keeps_internal_worker_405_with_crawler_shaped_user_agent() -> None:
    internal_token = "private" + "-token"
    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            observer_internal_token=internal_token,
        )
    )
    event = _method_not_allowed_event(
        "https://trustedrouter.com/v1/internal/synthetic/route-health",
        method="GET",
        headers={
            "User-Agent": "trustedrouter-synthetic-bot/1.0",
            "X-TrustedRouter-Internal-Token": internal_token,
        },
    )

    assert before_send(event, {}) is not None
    reset_sentry_floodgate_for_tests()


def test_sentry_keeps_crawler_originated_server_error() -> None:
    reset_sentry_floodgate_for_tests()
    event = {
        "level": "error",
        "request": {
            "method": "GET",
            "url": "https://uptimerouter.com/support",
            "headers": {
                "Referer": "https://uptimerouter.com/",
                "User-Agent": "heritrix/3.14.2",
            },
        },
        "exception": {
            "values": [{"type": "RuntimeError", "value": "database failed"}]
        },
    }

    assert before_send(event, {}) is not None


def test_sentry_recognizes_context_only_405_as_untrusted() -> None:
    reset_sentry_floodgate_for_tests()
    event = {
        "request": {
            "method": "GET",
            "url": "https://trustedrouter.com/mcp",
            "headers": {"User-Agent": "Infrawatch/1.0"},
        },
        "contexts": {"response": {"status_code": 405}},
    }

    assert before_send(event, {}) is None


def test_sentry_keeps_non_405_server_errors_without_origin() -> None:
    reset_sentry_floodgate_for_tests()
    event = {
        "level": "error",
        "request": {"method": "POST", "url": "https://trustedrouter.com/v1/keys"},
        "exception": {"values": [{"type": "RuntimeError", "value": "database failed"}]},
    }

    assert before_send(event, {}) is not None


def test_sentry_does_not_mutate_dropped_scanner_event() -> None:
    event = _method_not_allowed_event(
        "https://trustedrouter.com/",
        headers={"User-Agent": "scanner"},
    )
    original = json.loads(json.dumps(event))

    assert before_send(event, {}) is None
    assert event == original


def test_sentry_floodgate_drops_repeated_issue_after_per_fingerprint_limit() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            sentry_floodgate_window_seconds=60,
            sentry_floodgate_max_events_per_fingerprint=2,
            sentry_floodgate_max_events_per_window=100,
        ),
        clock=clock,
    )
    event = {
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "provider unavailable",
                    "stacktrace": {"frames": [{"filename": "providers.py", "function": "chat", "lineno": 42}]},
                }
            ]
        },
    }

    assert before_send(event, {}) is not None
    assert before_send(event, {}) is not None
    assert before_send(event, {}) is None

    now = 1061.0
    assert before_send(event, {}) is not None
    reset_sentry_floodgate_for_tests()


def test_sentry_floodgate_global_window_is_hard_cap_for_new_and_evicted_issues() -> None:
    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            sentry_floodgate_window_seconds=60,
            sentry_floodgate_max_events_per_fingerprint=10,
            sentry_floodgate_max_events_per_window=2,
            sentry_floodgate_max_fingerprints=1,
        ),
        clock=lambda: 2000.0,
    )

    assert before_send({"level": "error", "message": "issue a"}, {}) is not None
    assert before_send({"level": "error", "message": "issue b"}, {}) is not None
    assert len(sentry_config._floodgate._fingerprints) == 1
    assert before_send({"level": "error", "message": "issue a"}, {}) is None
    assert before_send({"level": "error", "message": "issue c"}, {}) is None
    assert len(sentry_config._floodgate._fingerprints) == 1
    assert sentry_config._floodgate._global.count == 2
    reset_sentry_floodgate_for_tests()


def test_sentry_floodgate_caps_unique_authenticated_405_paths_and_cardinality() -> None:
    internal_token = "observer" + "-unique-path-token"
    hard_max = 3
    max_fingerprints = 2
    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            observer_internal_token=internal_token,
            sentry_floodgate_window_seconds=60,
            sentry_floodgate_max_events_per_fingerprint=100,
            sentry_floodgate_max_events_per_window=hard_max,
            sentry_floodgate_max_fingerprints=max_fingerprints,
        ),
        clock=lambda: 2500.0,
    )

    accepted = []
    for index in range(hard_max + 20):
        event = _method_not_allowed_event(
            f"https://trustedrouter.com/probe/{index}/attacker-selected",
            headers={"X-TrustedRouter-Internal-Token": internal_token},
        )
        captured = before_send(event, {})
        if captured is not None:
            accepted.append(captured)

    assert len(accepted) == hard_max
    assert {tuple(event["fingerprint"]) for event in accepted} == {
        ("http-405", "POST", "unresolved-route")
    }
    assert len(sentry_config._floodgate._fingerprints) <= max_fingerprints
    assert sentry_config._floodgate._global.count == hard_max
    reset_sentry_floodgate_for_tests()


def test_sentry_floodgate_groups_logs_after_scrubbing_secret_values() -> None:
    reset_sentry_floodgate_for_tests(
        settings=Settings(
            environment="test",
            sentry_floodgate_window_seconds=60,
            sentry_floodgate_max_events_per_fingerprint=1,
            sentry_floodgate_max_events_per_window=100,
        ),
        clock=lambda: 3000.0,
    )

    first = before_send_log(
        {"level": "error", "message": "provider failed", "extra": {"api_key": "sk-tr-v1-one"}},
        {},
    )
    second = before_send_log(
        {"level": "error", "message": "provider failed", "extra": {"api_key": "sk-tr-v1-two"}},
        {},
    )

    assert first is not None
    assert json.dumps(first).count("[Filtered]") == 1
    assert second is None
    reset_sentry_floodgate_for_tests()


def test_sentry_init_is_noop_under_pytest_even_with_local_dsn(monkeypatch) -> None:
    """Importing trusted_router.main creates the module-level ASGI app.

    Local Settings can read SENTRY_DSN from ~/.quill_cloud_keys.private.
    Under pytest, that must remain inert or synthetic route failures page
    the real project.
    """
    calls: list[dict] = []

    class FakeSentry:
        @staticmethod
        def init(**kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "sentry_sdk", FakeSentry)

    init_sentry(
        Settings(
            environment="local",
            sentry_dsn="https://example@example.ingest.sentry.io/1",
        )
    )

    assert calls == []


def test_sentry_init_gates_logs_product_at_warning(monkeypatch) -> None:
    init_calls: list[dict] = []
    logging_integration_kwargs: list[dict] = []

    class FakeLoggingIntegration:
        def __init__(self, **kwargs) -> None:
            logging_integration_kwargs.append(kwargs)

    monkeypatch.setattr(sentry_config, "_running_under_pytest", lambda _settings: False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(sentry_logging, "LoggingIntegration", FakeLoggingIntegration)

    init_sentry(
            Settings(
                environment="staging",
                service_surface="observer",
                observer_internal_token="observer-staging-token",  # noqa: S106
                sentry_dsn="https://example@example.ingest.sentry.io/1",
            )
    )

    assert len(init_calls) == 1
    assert logging_integration_kwargs == [
        {"level": None, "event_level": None, "sentry_logs_level": logging.WARNING}
    ]


def test_sentry_init_is_noop_for_local_scripts_unless_explicitly_enabled() -> None:
    dsn = "https://example@example.ingest.sentry.io/1"

    assert (
        sentry_should_init(
            Settings(environment="local", sentry_dsn=dsn),
            running_under_pytest=False,
        )
        is False
    )
    assert (
        sentry_should_init(
            Settings(environment="local", sentry_dsn=dsn, sentry_local_enabled=True),
            running_under_pytest=False,
        )
        is True
    )
    assert (
        sentry_should_init(
            Settings(
                environment="staging",
                service_surface="observer",
                observer_internal_token="observer-staging-token",  # noqa: S106
                sentry_dsn=dsn,
            ),
            running_under_pytest=False,
        )
        is True
    )


def test_test_settings_override_process_env_for_default_client(monkeypatch) -> None:
    monkeypatch.setenv("TR_STRIPE_SECRET_KEY", "sk_test_from_shell")
    monkeypatch.setenv("TR_STRIPE_WEBHOOK_SECRET", "whsec_from_shell")
    monkeypatch.setenv("TR_GOOGLE_CLIENT_ID", "google-client-from-shell")
    monkeypatch.setenv("TR_GOOGLE_CLIENT_SECRET", "google-secret-from-shell")
    settings = Settings(
        environment="test",
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
    )

    assert settings.stripe_secret_key is None
    assert settings.stripe_webhook_secret is None
    assert settings.google_oauth_enabled is False


def test_local_key_file_is_not_loaded_under_pytest(monkeypatch) -> None:
    """Tests often construct `Settings(environment="local")` for browser-like
    flows. That must never silently import live Stripe/Sentry/OAuth secrets
    from the developer machine."""
    for key in (
        "TR_ALLOW_LOCAL_KEY_FILE_IN_TESTS",
        "TR_STRIPE_SECRET_KEY",
        "TR_STRIPE_WEBHOOK_SECRET",
        "TR_SENTRY_DSN",
        "TR_GOOGLE_CLIENT_ID",
        "TR_GOOGLE_CLIENT_SECRET",
        "TR_GITHUB_CLIENT_ID",
        "TR_GITHUB_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(environment="local")

    assert settings.stripe_secret_key is None
    assert settings.stripe_webhook_secret is None
    assert settings.sentry_dsn is None
    assert settings.google_oauth_enabled is False
    assert settings.github_oauth_enabled is False


def test_playwright_server_runs_with_test_observability_disabled() -> None:
    repository = Path(__file__).resolve().parents[1]
    config = (repository / "playwright.config.js").read_text(encoding="utf-8")
    server = (repository / "tests/browser/six_surface_server.py").read_text(
        encoding="utf-8"
    )

    assert "tests.browser.six_surface_server:app" in config
    assert "reuseExistingServer: false" in config
    assert '"environment": "test"' in server
    assert '"storage_backend": "memory"' in server
    assert '"sentry_dsn": None' in server
    assert "_without_ambient_credentials()" in server
    assert "_without_outbound_network()" in server


def test_inference_key_labels_are_partial_and_raw_key_is_one_time_only(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "label"}).json()
    raw_key = created["key"]
    key_hash = created["data"]["hash"]
    label = created["data"]["label"]

    assert raw_key.startswith("sk-tr-v1-")
    assert label.startswith(raw_key[:10])
    assert label.endswith(raw_key[-4:])
    assert raw_key not in json.dumps(created["data"])

    fetched = client.get(f"/v1/keys/{key_hash}", headers=user_headers).json()["data"]
    listed = client.get("/v1/keys", headers=user_headers).json()["data"]
    assert "key" not in fetched
    assert raw_key not in json.dumps(fetched)
    assert raw_key not in json.dumps(listed)


@pytest.mark.parametrize("method,path", [("GET", "/v1/keys"), ("POST", "/v1/billing/checkout")])
def test_management_endpoints_require_authentication(method: str, path: str, client: TestClient) -> None:
    response = client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_plain_workspace_member_cannot_manage_org_resources(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    org = client.post("/v1/workspaces", headers=user_headers, json={"name": "Org"}).json()["data"]
    owner_org_headers = {**user_headers, "x-trustedrouter-workspace": org["id"]}
    add = client.post(
        f"/v1/workspaces/{org['id']}/members/add",
        headers=owner_org_headers,
        json={"emails": ["bob@example.com"], "role": "member"},
    )
    assert add.status_code == 200
    bob_org_headers = {"x-trustedrouter-user": "bob@example.com", "x-trustedrouter-workspace": org["id"]}

    cases = [
        ("GET", "/v1/keys", None),
        ("POST", "/v1/keys", {"name": "member should not create"}),
        ("POST", "/v1/billing/checkout", {"amount": 25}),
        ("GET", "/v1/byok/providers", None),
        ("GET", "/v1/organization/members", None),
    ]
    for method, path, body in cases:
        response = client.request(method, path, headers=bob_org_headers, json=body)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["type"] == "forbidden"


def test_workspace_admin_can_manage_org_resources(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    org = client.post("/v1/workspaces", headers=user_headers, json={"name": "Org"}).json()["data"]
    owner_org_headers = {**user_headers, "x-trustedrouter-workspace": org["id"]}
    add = client.post(
        f"/v1/workspaces/{org['id']}/members/add",
        headers=owner_org_headers,
        json={"emails": ["admin@example.com"], "role": "admin"},
    )
    assert add.status_code == 200
    admin_org_headers = {
        "x-trustedrouter-user": "admin@example.com",
        "x-trustedrouter-workspace": org["id"],
    }

    listed = client.get("/v1/keys", headers=admin_org_headers)
    created = client.post("/v1/keys", headers=admin_org_headers, json={"name": "admin key"})
    members = client.get("/v1/organization/members", headers=admin_org_headers)

    assert listed.status_code == 200
    assert created.status_code == 201
    assert created.json()["data"]["workspace_id"] == org["id"]
    assert members.status_code == 200
