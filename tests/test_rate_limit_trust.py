from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from trusted_router import storage_rate_limits
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import public as public_routes
from trusted_router.services.email import EmailMessage
from trusted_router.services.ops_chat import OpsChatFanoutResult
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_rate_limits import InMemoryRateLimits

_TEST_BYOK_KMS_KEY_NAME = (
    "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
)
_FEDERATION_PEER_TOKEN = "test-federation-peer-token"  # noqa: S105
_FEDERATION_CREDIT_TOKEN = "test-federation-credit-token"  # noqa: S105
_FEDERATION_SETTLEMENT_TOKEN = "s" * 40  # noqa: S105


@pytest.fixture(autouse=True)
def fixed_rate_limit_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: dt.datetime(2026, 8, 19, 12, 0, 30, tzinfo=dt.UTC),
    )


def _production_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "service_surface": "control",
        "attribution_cookie_secret": "test-attribution-cookie-secret-32-bytes",
        "stripe_webhook_secret": "whsec_test",
        "stripe_secret_key": "sk_test_secret",
        "sentry_dsn": "https://example@example.ingest.sentry.io/1",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
        "ses_from_email": "noreply@example.com",
        "storage_backend": "spanner-bigtable",
        "spanner_instance_id": "trusted-router",
        "spanner_database_id": "trusted-router",
        "bigtable_instance_id": "trusted-router-logs",
        "byok_kms_key_name": _TEST_BYOK_KMS_KEY_NAME,
        "rate_limit_enabled": True,
        "rate_limit_client_ip_mode": "edge_header",
    }
    values.update(updates)
    return Settings(**values)


def _management_key(email: str) -> str:
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, _key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="rate-limit trust",
        creator_user_id=user.id,
        management=True,
    )
    return raw


def _active_session(email: str) -> str:
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label=email,
        ttl_seconds=3600,
        state="active",
        workspace_id=workspace.id,
    )
    return raw


@pytest.mark.parametrize("environment", ["production", "canary"])
def test_deployed_env_uses_only_normalized_lb_client_ip_for_identity(
    environment: str,
) -> None:
    app = create_app(
        _production_settings(environment=environment, rate_limit_ip_per_window=2),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)

    hosts = ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"]
    responses = [
        client.post(
            "/v1/signup",
            headers={
                "host": hosts[index],
                "x-trustedrouter-client-ip": "2001:0db8:0:0:0:0:0:1",
                "x-forwarded-for": f"198.51.100.{index}",
                "cf-connecting-ip": f"203.0.113.{index}",
                "x-trustedrouter-user": f"spoof-{index}@example.com",
            },
            json={},
        )
        for index in range(3)
    ]

    assert [response.status_code for response in responses[:2]] == [400, 400]
    assert responses[2].status_code == 429
    assert responses[2].json()["error"]["type"] == "rate_limited"


@pytest.mark.parametrize("environment", ["production", "canary"])
def test_missing_or_malformed_deployed_lb_header_collapses_to_one_subject(
    environment: str,
) -> None:
    app = create_app(
        _production_settings(environment=environment, rate_limit_ip_per_window=3),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)
    headers = [
        {"x-trustedrouter-client-ip": "not-an-ip", "x-forwarded-for": "198.51.100.1"},
        {"x-trustedrouter-client-ip": "", "cf-connecting-ip": "203.0.113.2"},
        {"x-trustedrouter-client-ip": "fe80::1%caller-chosen-zone"},
        {"x-forwarded-for": "192.0.2.3", "cf-connecting-ip": "192.0.2.4"},
    ]

    responses = [client.post("/v1/signup", headers=item, json={}) for item in headers]

    assert [response.status_code for response in responses[:3]] == [400, 400, 400]
    assert responses[3].status_code == 429


def test_duplicate_deployed_lb_headers_collapse_to_untrusted_subject() -> None:
    app = create_app(
        _production_settings(rate_limit_ip_per_window=1),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)

    first = client.post(
        "/v1/signup",
        headers=[
            ("x-trustedrouter-client-ip", "198.51.100.1"),
            ("x-trustedrouter-client-ip", "203.0.113.2"),
            ("x-forwarded-for", "192.0.2.3"),
        ],
        json={},
    )
    second = client.post("/v1/signup", json={})

    assert first.status_code == 400
    assert second.status_code == 429


def test_rotating_invalid_bearers_share_source_bucket_and_never_touch_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(
            environment="test",
            rate_limit_ip_per_window=2,
            rate_limit_key_per_window=100,
        ),
        init_observability=False,
    )
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(
        InMemoryStore,
        "hit_rate_limit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted bearer reached durable limiter")
        ),
    )
    monkeypatch.setattr(
        InMemoryStore,
        "api_key_auth_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public route authenticated an arbitrary bearer")
        ),
    )

    responses = [
        client.get(
            "/v1/models",
            headers={"authorization": f"Bearer sk-tr-attacker-rotation-{index}"},
        )
        for index in range(3)
    ]

    assert [response.status_code for response in responses[:2]] == [200, 200]
    assert responses[2].status_code == 429


@pytest.mark.parametrize("transport", ["api_key", "bearer_session", "cookie_session"])
def test_authenticated_credential_limit_is_local_post_auth_and_keeps_429_shape(
    transport: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(
            environment="test",
            rate_limit_ip_per_window=100,
            rate_limit_key_per_window=1,
        ),
        init_observability=False,
    )
    client = TestClient(app, raise_server_exceptions=False)
    if transport == "api_key":
        raw = _management_key("limit-api-key@example.com")
        headers = {"authorization": f"Bearer {raw}"}
        cookies = None
    else:
        raw = _active_session(f"limit-{transport}@example.com")
        headers = (
            {"authorization": f"Bearer {raw}"}
            if transport == "bearer_session"
            else None
        )
        cookies = {"tr_session": raw} if transport == "cookie_session" else None

    durable_calls = 0

    def reject_durable(self: InMemoryStore, *_args: object, **_kwargs: object) -> None:
        del self
        nonlocal durable_calls
        durable_calls += 1
        raise AssertionError("authenticated limiter touched durable storage")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", reject_durable)
    responses = [
        client.get("/v1/keys", headers=headers, cookies=cookies)
        for _ in range(2)
    ]

    assert responses[0].status_code == 200, responses[0].text
    limited = responses[1]
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"
    assert limited.headers["retry-after"]
    assert limited.headers["x-ratelimit-limit"] == "1"
    assert limited.headers["x-ratelimit-remaining"] == "0"
    assert limited.headers["x-ratelimit-reset"]
    assert durable_calls == 0


def test_authenticated_limiter_is_applied_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = InMemoryRateLimits.hit

    def track(self: InMemoryRateLimits, **kwargs: object):
        if str(kwargs["namespace"]).startswith("authenticated_"):
            calls.append(str(kwargs["namespace"]))
        return original(self, **kwargs)

    monkeypatch.setattr(InMemoryRateLimits, "hit", track)
    app = create_app(
        Settings(
            environment="test",
            rate_limit_ip_per_window=100,
            rate_limit_key_per_window=100,
        ),
        init_observability=False,
    )
    raw = _management_key("dedupe@example.com")
    client = TestClient(app)

    response = client.get("/v1/keys", headers={"authorization": f"Bearer {raw}"})

    assert response.status_code == 200
    assert calls == ["authenticated_api_key"]


def test_http_limiter_never_calls_durable_store_when_store_limiter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def broken_store_limiter(self: InMemoryStore, *_args: object, **_kwargs: object) -> None:
        del self
        nonlocal calls
        calls += 1
        raise RuntimeError("durable limiter unavailable")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", broken_store_limiter)
    app = create_app(
        Settings(environment="test", rate_limit_ip_per_window=100),
        init_observability=False,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/v1/signup", json={})

    assert response.status_code == 400
    assert calls == 0


def test_ingress_limiter_failure_fails_closed_for_external_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InMemoryRateLimits,
        "hit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("limiter broken")),
    )
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/v1/signup", json={})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    assert response.headers["retry-after"] == "1"


def test_valid_internal_token_fails_open_if_local_limiter_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "valid-internal-token"  # noqa: S105 - test fixture.
    app = create_app(
        Settings(environment="test", internal_gateway_token=token),
        init_observability=False,
    )
    user = STORE.ensure_user("internal-throughput@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="internal throughput",
        creator_user_id=user.id,
    )
    monkeypatch.setattr(
        InMemoryRateLimits,
        "hit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("limiter broken")),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/internal/gateway/key",
        headers={"x-trustedrouter-internal-token": token},
        json={"api_key_lookup_hash": key.lookup_hash},
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("path", "header", "setting", "configured"),
    [
        (
            "/v1/internal/federation/resolve-key",
            "x-trustedrouter-federation-token",
            "federation_peer_token",
            _FEDERATION_PEER_TOKEN,
        ),
        (
            "/v1/internal/federation/apply-usage",
            "x-trustedrouter-federation-settlement-token",
            "federation_settlement_inbound_tokens",
            f"aws={_FEDERATION_SETTLEMENT_TOKEN}",
        ),
        (
            "/v1/internal/federation/credit-transfer",
            "x-trustedrouter-federation-credit-token",
            "federation_credit_inbound_token",
            _FEDERATION_CREDIT_TOKEN,
        ),
    ],
)
def test_valid_route_scoped_federation_token_gets_internal_allowance(
    path: str,
    header: str,
    setting: str,
    configured: str,
) -> None:
    supplied = (
        _FEDERATION_SETTLEMENT_TOKEN
        if setting == "federation_settlement_inbound_tokens"
        else configured
    )
    settings = Settings.model_validate(
        {
            "environment": "test",
            "rate_limit_ip_per_window": 1,
            "rate_limit_internal_per_window": 2,
            setting: configured,
        }
    )
    client = TestClient(create_app(settings, init_observability=False))

    responses = [client.post(path, headers={header: supplied}, json={}) for _ in range(3)]

    assert [response.status_code for response in responses] == [400, 400, 429]


@pytest.mark.parametrize(
    ("path", "header", "setting", "configured"),
    [
        (
            "/v1/internal/federation/resolve-key",
            "x-trustedrouter-federation-token",
            "federation_peer_token",
            _FEDERATION_PEER_TOKEN,
        ),
        (
            "/v1/internal/federation/apply-usage",
            "x-trustedrouter-federation-settlement-token",
            "federation_settlement_inbound_tokens",
            f"aws={_FEDERATION_SETTLEMENT_TOKEN}",
        ),
        (
            "/v1/internal/federation/credit-transfer",
            "x-trustedrouter-federation-credit-token",
            "federation_credit_inbound_token",
            _FEDERATION_CREDIT_TOKEN,
        ),
    ],
)
def test_invalid_federation_tokens_share_bounded_source_bucket(
    path: str,
    header: str,
    setting: str,
    configured: str,
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "rate_limit_ip_per_window": 1,
            "rate_limit_internal_per_window": 100,
            setting: configured,
        }
    )
    client = TestClient(create_app(settings, init_observability=False))

    first = client.post(path, headers={header: "rotating-invalid-1"}, json={})
    second = client.post(path, headers={header: "rotating-invalid-2"}, json={})

    assert first.status_code == 401
    assert second.status_code == 429


def test_generic_gateway_token_does_not_raise_federation_route_allowance() -> None:
    settings = Settings(
        environment="test",
        internal_gateway_token="generic-internal-token",  # noqa: S106
        federation_peer_token=_FEDERATION_PEER_TOKEN,
        rate_limit_ip_per_window=1,
        rate_limit_internal_per_window=100,
    )
    client = TestClient(create_app(settings, init_observability=False))
    headers = {
        "x-trustedrouter-internal-token": "generic-internal-token",
        "x-trustedrouter-federation-token": "wrong-peer-token",
    }

    first = client.post("/v1/internal/federation/resolve-key", headers=headers, json={})
    second = client.post("/v1/internal/federation/resolve-key", headers=headers, json={})

    assert first.status_code == 401
    assert second.status_code == 429


def test_observer_token_does_not_raise_billing_route_allowance() -> None:
    billing_token = "billing-internal-token"  # noqa: S105 - test token.
    observer_token = "observer-internal-token"  # noqa: S105 - test token.
    settings = Settings(
        environment="test",
        service_surface="internal",
        internal_gateway_token=billing_token,
        observer_internal_token=observer_token,
        rate_limit_ip_per_window=1,
        rate_limit_internal_per_window=100,
    )
    client = TestClient(create_app(settings, init_observability=False))
    headers = {"x-trustedrouter-internal-token": observer_token}

    first = client.post("/v1/internal/gateway/authorize", headers=headers, json={})
    second = client.post("/v1/internal/gateway/authorize", headers=headers, json={})

    assert first.status_code == 401
    assert second.status_code == 429


def test_valid_route_scoped_federation_token_fails_open_if_local_limiter_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(environment="test", federation_peer_token=_FEDERATION_PEER_TOKEN),
        init_observability=False,
    )
    monkeypatch.setattr(
        InMemoryRateLimits,
        "hit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("limiter broken")),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/v1/internal/federation/resolve-key",
        headers={"x-trustedrouter-federation-token": _FEDERATION_PEER_TOKEN},
        json={},
    )

    assert response.status_code == 400


def test_authenticated_limits_are_per_instance_defense_in_depth() -> None:
    settings = Settings(
        environment="test",
        rate_limit_ip_per_window=100,
        rate_limit_key_per_window=1,
    )
    first_app = create_app(
        settings,
        configure_store_arg=False,
        init_observability=False,
    )
    second_app = create_app(
        settings,
        configure_store_arg=False,
        init_observability=False,
    )
    raw = _management_key("multi-instance@example.com")
    headers = {"authorization": f"Bearer {raw}"}

    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get("/v1/keys", headers=headers).status_code == 200
        assert second.get("/v1/keys", headers=headers).status_code == 200
        assert first.get("/v1/keys", headers=headers).status_code == 429
        assert second.get("/v1/keys", headers=headers).status_code == 429


def test_read_only_mode_keeps_storage_free_authenticated_defense() -> None:
    app = create_app(
        Settings(
            environment="test",
            read_only=True,
            rate_limit_ip_per_window=100,
            rate_limit_key_per_window=1,
        ),
        init_observability=False,
    )
    raw = _management_key("read-only-limit@example.com")
    client = TestClient(app)
    headers = {"authorization": f"Bearer {raw}"}

    assert client.get("/v1/keys", headers=headers).status_code == 200
    assert client.get("/v1/keys", headers=headers).status_code == 429


@pytest.mark.parametrize(
    "path",
    ["/ready", "/docs", "/openapi.json", "/docs/llms-full.txt"],
)
def test_former_expensive_exemptions_use_ingress_bucket(path: str) -> None:
    app = create_app(
        Settings(environment="test", rate_limit_ip_per_window=1),
        init_observability=False,
    )
    client = TestClient(app)

    assert client.get(path).status_code != 429
    limited = client.get(path)

    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"


def test_health_route_uses_the_same_source_admission_bucket() -> None:
    app = create_app(
        Settings(environment="test", rate_limit_ip_per_window=1),
        init_observability=False,
    )
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    limited = client.get("/health")
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"


@pytest.mark.parametrize("environment", ["production", "canary", "staging"])
def test_deployed_untrusted_mode_ignores_even_a_well_formed_client_ip_header(
    environment: str,
) -> None:
    app = create_app(
        _production_settings(
            environment=environment,
            rate_limit_client_ip_mode="untrusted",
            rate_limit_ip_per_window=2,
        ),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)

    responses = [
        client.post(
            "/v1/signup",
            headers={"x-trustedrouter-client-ip": f"198.51.100.{index}"},
            json={},
        )
        for index in range(1, 4)
    ]

    assert [response.status_code for response in responses[:2]] == [400, 400]
    assert responses[2].status_code == 429


def test_client_ip_identity_mode_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="TR_RATE_LIMIT_CLIENT_IP_MODE"):
        Settings(environment="test", rate_limit_client_ip_mode="forwarded")


@pytest.mark.parametrize(
    ("setting", "error_name"),
    [
        ({"rate_limit_window_seconds": 0}, "TR_RATE_LIMIT_WINDOW_SECONDS"),
        ({"rate_limit_ip_per_window": 0}, "TR_RATE_LIMIT_IP_PER_WINDOW"),
        ({"rate_limit_key_per_window": -1}, "TR_RATE_LIMIT_KEY_PER_WINDOW"),
        ({"rate_limit_internal_per_window": 0}, "TR_RATE_LIMIT_INTERNAL_PER_WINDOW"),
    ],
)
def test_rate_limit_settings_must_be_positive(
    setting: dict[str, int], error_name: str
) -> None:
    with pytest.raises(ValueError, match=error_name):
        Settings(environment="test", **setting)


def test_public_inquiry_limiters_and_display_use_canonical_production_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_rate_subjects: list[str] = []
    sent_messages: list[EmailMessage] = []

    class FakeEmailService:
        def send(self, message: EmailMessage) -> bool:
            sent_messages.append(message)
            return True

    async def fake_fanout(*_args: object, **_kwargs: object) -> OpsChatFanoutResult:
        return OpsChatFanoutResult(configured=0, accepted=0)

    monkeypatch.setattr(
        public_routes,
        "_inquiry_rate_ok",
        lambda subject: seen_rate_subjects.append(subject) or True,
    )
    monkeypatch.setattr(
        public_routes,
        "get_email_service",
        lambda _settings: FakeEmailService(),
    )
    monkeypatch.setattr(public_routes, "fanout_support_message", fake_fanout)
    app = create_app(
        Settings(
            environment="production",
            service_surface="actions",
            storage_backend="memory",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",  # noqa: S106
            ses_from_email="noreply@example.com",
            rate_limit_enabled=True,
            rate_limit_client_ip_mode="edge_header",
            rate_limit_ip_per_window=100,
            partner_inquiry_email="leads@example.com",
        ),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)
    source_headers = {
        "x-trustedrouter-client-ip": "2001:0db8:0:0:0:0:0:1",
        "x-forwarded-for": "198.51.100.200",
        "cf-connecting-ip": "203.0.113.200",
    }

    support = client.post(
        "/support/inquiry",
        headers=source_headers,
        json={
            "name": "Ada",
            "email": "ada@example.com",
            "category": "api",
            "subject": "Help",
            "message": "Please help",
            "website": "",
        },
    )
    partner = client.post(
        "/trustedos/inquiry",
        headers=source_headers,
        json={
            "name": "Ada",
            "email": "ada@example.com",
            "company": "Analytical Engines",
            "message": "Partnership request",
            "website": "",
        },
    )

    assert support.status_code == 200, support.text
    assert partner.status_code == 200, partner.text
    assert seen_rate_subjects == ["support:2001:db8::1", "2001:db8::1"]
    assert "IP:      2001:db8::1" in sent_messages[-1].text_body
