from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trusted_router.auth import bootstrap_management_key
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.sentry_config import (
    SENSITIVE_STRING_FRAGMENTS,
    SENSITIVE_STRING_PREFIXES,
    before_breadcrumb,
    before_send,
    before_send_log,
    init_sentry,
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
    app = create_app(Settings(environment="test", internal_gateway_token=internal_token))
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
        internal_gateway_token="internal-prod-sentry-test",  # noqa: S106 - test config.
        stripe_webhook_secret="whsec_test",  # noqa: S106 - test config.
        stripe_secret_key="sk_test_secret",  # noqa: S106 - test config.
        sentry_dsn="https://example@example.ingest.sentry.io/1",
        storage_backend="spanner-bigtable",
        spanner_instance_id="trusted-router",
        spanner_database_id="trusted-router",
        bigtable_instance_id="trusted-router-logs",
        byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
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
        headers={"x-trustedrouter-internal-token": "internal-prod-sentry-test"},
    )
    enabled_resp = enabled.get(
        "/v1/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": "internal-prod-sentry-test"},
    )

    assert disabled_resp.status_code == 404
    assert enabled_resp.status_code == 500


def test_production_rejects_spoofable_user_header_auth() -> None:
    internal_token = "internal" + "-prod-token"
    webhook_secret = "whsec_" + "test"
    stripe_key = "sk_" + "test_secret"
    prod_client = TestClient(
        create_app(
            Settings(
                environment="production",
                internal_gateway_token=internal_token,
                stripe_webhook_secret=webhook_secret,
                stripe_secret_key=stripe_key,
                sentry_dsn="https://example@example.ingest.sentry.io/1",
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
        "data": {"object": {"amount_total": 321, "metadata": {"workspace_id": workspace_id}}},
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
    config = (Path(__file__).resolve().parents[1] / "playwright.config.js").read_text(encoding="utf-8")

    assert "TR_ENVIRONMENT=test" in config
    assert "TR_SENTRY_DSN=" in config


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
