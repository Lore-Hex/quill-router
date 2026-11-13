from __future__ import annotations

import datetime as dt
import threading

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.fakes.production_storage import PRODUCTION_SPANNER_STORAGE
from tests.route_inventory import effective_routes
from trusted_router import storage_rate_limits
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.middleware import (
    CONTROL_PLANE_EVIDENCE_PATH,
    CONTROL_PLANE_EVIDENCE_SURFACES,
    _rate_limit_request,
)
from trusted_router.routes import public as public_routes
from trusted_router.services.email import EmailMessage
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
        **PRODUCTION_SPANNER_STORAGE,
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
@pytest.mark.parametrize(
    "forwarded_for",
    [
        "{spoof}",
        "{spoof}, 203.0.113.10, 192.0.2.10",
        "{spoof}, 192.0.2.99, 203.0.113.10, 192.0.2.10",
    ],
    ids=["direct-origin-shape", "gclb-shape", "extra-untrusted-hop"],
)
def test_deployed_env_uses_only_normalized_lb_client_ip_for_identity(
    environment: str,
    forwarded_for: str,
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
                "x-forwarded-for": forwarded_for.format(spoof=f"198.51.100.{index}"),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["edge_header", "untrusted"])
@pytest.mark.parametrize("path", ["/", "/v1/signup", "/internal/gateway/authorize"])
async def test_rotating_xff_prefix_cannot_mint_new_ingress_buckets(
    mode: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _production_settings(rate_limit_client_ip_mode=mode, rate_limit_ip_per_window=2)
    limiter = InMemoryRateLimits(lock=threading.RLock())
    monkeypatch.setattr(
        InMemoryStore,
        "hit_rate_limit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted source identity reached durable rate limits")
        ),
    )
    responses = []
    for index in range(3):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [
                (b"x-forwarded-for", f"198.51.100.{index}, 203.0.113.7, 192.0.2.1".encode()),
                (b"x-trustedrouter-client-ip", b"203.0.113.7"),
            ],
            "client": (f"198.51.100.{index}", 12345),
        })
        responses.append(await _rate_limit_request(
            request,
            settings,
            ingress_rate_limits=limiter,
            federation_settlement_tokens=(),
        ))
    assert responses[:2] == [None, None]
    assert responses[2] is not None and responses[2].status_code == 429


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


def test_health_probes_bypass_an_exhausted_untrusted_source_bucket() -> None:
    # Behind the AWS NLB every request shares one `untrusted` source bucket,
    # so a client burst must not 429 the balancer's own GET /health probe.
    app = create_app(
        _production_settings(rate_limit_client_ip_mode="untrusted", rate_limit_ip_per_window=2),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)

    for _ in range(2):
        assert client.post("/v1/chat/completions", json={}).status_code != 429
    limited = client.post("/v1/chat/completions", json={})
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"

    assert client.get("/health").status_code == 200
    assert client.get("/v1/health").status_code == 200
    # Still exhausted for everything else, including the look-alike that is
    # not a route: an exempt /healthz would be an unmetered 404-render path.
    assert client.get("/healthz").status_code == 429
    assert client.post("/v1/chat/completions", json={}).status_code == 429


# Paths that request.url.path reads as a liveness route and the router does
# not. Starlette builds request.url through urlsplit, which drops tab, CR and
# LF, so each of these has request.url.path "/health" or "/v1/health". The
# router matches the path as sent: a tab or CR is no route (a 404, and on a
# page path a rendered 404 page), and only a trailing LF still reaches the
# handler, because the route pattern's `$` matches before it.
_NOT_THE_PROBE_PATH = [
    "/hea\tlth",
    "/\thealth",
    "/health\t",
    "/health\r",
    "/hea\nlth",
    "/health\n",
    "/v1/hea\tlth",
    "/v1/health\n",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _NOT_THE_PROBE_PATH)
async def test_health_exemption_compares_the_path_the_router_matches(path: str) -> None:
    settings = _production_settings(
        rate_limit_client_ip_mode="untrusted", rate_limit_ip_per_window=1
    )
    limiter = InMemoryRateLimits(lock=threading.RLock())

    async def admit(sent: str) -> object:
        request = Request({
            "type": "http",
            "method": "GET",
            "path": sent,
            "headers": [(b"host", b"aws.trustedrouter.com"), (b"accept", b"text/html")],
            "client": ("198.51.100.7", 12345),
        })
        # The premise: request.url.path alone cannot tell these apart.
        assert request.url.path in {"/health", "/v1/health"}
        return await _rate_limit_request(
            request,
            settings,
            ingress_rate_limits=limiter,
            federation_settlement_tokens=(),
        )

    # Counted, then refused: neither half of the exemption applies.
    assert await admit(path) is None
    refused = await admit(path)
    assert isinstance(refused, JSONResponse) and refused.status_code == 429
    # The probe itself is still admitted on the exhausted bucket.
    assert await admit("/health") is None
    assert await admit("/v1/health") is None


@pytest.mark.parametrize(
    "path",
    ["/hea%09lth", "/%09health", "/health%09", "/health%0D", "/health%0A", "/v1/hea%09lth"],
)
@pytest.mark.parametrize("accept", ["text/html", "application/json", "*/*"])
def test_an_exhausted_bucket_refuses_a_probe_look_alike_before_any_404_render(
    path: str, accept: str
) -> None:
    app = create_app(
        _production_settings(rate_limit_client_ip_mode="untrusted", rate_limit_ip_per_window=1),
        configure_store_arg=False,
        init_observability=False,
    )
    client = TestClient(app)
    assert client.get("/ready").status_code != 429
    assert client.get("/ready").status_code == 429

    limited = client.get(path, headers={"Accept": accept})

    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"
    assert client.get("/health").status_code == 200


# The release coordinator's read of a cloud's control-plane evidence
# (scripts/deploy/cloud_rollout.py `fetch`): a GET with a unique query.
_COORDINATOR_HEADERS = {
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "User-Agent": "TrustedRouter-Release-Coordinator/2",
}
# Every surface that is a web app. `settle-drain` is a separate worker app
# with no ingress limiter and none of these routes.
_WEB_SURFACES = ("combined", "public", "actions", "control", "internal", "observer")
# Every other route that serves trust evidence. Each reads receipt keys or
# resolves a release record, which may fetch it, so each stays metered.
_OTHER_TRUST_ROUTES = (
    "/trust",
    "/trust/",
    "/trust/receipt-keys.json",
    "/.well-known/inference-receipt-keys",
    "/trust/gcp-release.json",
    "/trust/aws-release.json",
    "/trust/azure-release.json",
    "/trust/pcr0-aws.txt",
    "/trust/hostdata-azure.txt",
    "/trust/image-digest-gcp.txt",
    "/trust/image-reference-gcp.txt",
)
# Paths that are not the evidence route. The last four have request.url.path
# equal to it (urlsplit drops tab, CR and LF); the router reads them as sent.
_NOT_THE_EVIDENCE_PATH = (
    "/trust/control-plane.json/",
    "/trust/control-plane.json/x",
    "/trust/control-plane",
    "/trust/control-plane.jsonx",
    "/trust/Control-Plane.json",
    "/v1/trust/control-plane.json",
    "//trust/control-plane.json",
    "/trust/control-\tplane.json",
    "/\ttrust/control-plane.json",
    "/trust/control-plane.json\r",
    "/trust/control-plane.json\n",
)


def _surface_settings(surface: str, **updates: object) -> Settings:
    # The test environment keys the source bucket on the ASGI peer, which is
    # one address in these tests: one bucket, as in deployed `untrusted` mode.
    return Settings(environment="test", service_surface=surface, **updates)  # type: ignore[arg-type]


async def _admission(
    settings: Settings, limiter: InMemoryRateLimits, method: str, path: str
) -> JSONResponse | None:
    request = Request({
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"rollout_check=0f6c1d7e2b9a4c53",
        "headers": [
            (b"host", b"aws.trustedrouter.com"),
            *((name.lower().encode(), value.encode()) for name, value in _COORDINATOR_HEADERS.items()),
        ],
        "client": ("198.51.100.7", 12345),
    })
    return await _rate_limit_request(
        request,
        settings,
        ingress_rate_limits=limiter,
        federation_settlement_tokens=(),
    )


def _mounts(app: object, method: str, path: str) -> bool:
    return any(
        served == path and isinstance(route, APIRoute) and method in route.methods
        for served, route in effective_routes(app)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(CONTROL_PLANE_EVIDENCE_SURFACES))
async def test_the_release_evidence_read_is_neither_counted_nor_refused(surface: str) -> None:
    settings = _surface_settings(surface, rate_limit_ip_per_window=2)
    limiter = InMemoryRateLimits(lock=threading.RLock())

    # Not counted: reads beyond the allowance leave all of it for other paths.
    for _ in range(5):
        assert await _admission(settings, limiter, "GET", CONTROL_PLANE_EVIDENCE_PATH) is None
    assert await _admission(settings, limiter, "GET", "/trust") is None
    assert await _admission(settings, limiter, "GET", "/trust") is None
    refused = await _admission(settings, limiter, "GET", "/trust")
    assert refused is not None and refused.status_code == 429

    # Not refused: the bucket is exhausted and the read is still admitted.
    assert await _admission(settings, limiter, "GET", CONTROL_PLANE_EVIDENCE_PATH) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "method", "path"),
    [
        # Where the route is not mounted the path is a 404 render.
        *(
            (surface, "GET", CONTROL_PLANE_EVIDENCE_PATH)
            for surface in _WEB_SURFACES
            if surface not in CONTROL_PLANE_EVIDENCE_SURFACES
        ),
        # GET is the route's only method.
        *(
            ("observer", method, CONTROL_PLANE_EVIDENCE_PATH)
            for method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
        ),
        *(("observer", "GET", path) for path in _OTHER_TRUST_ROUTES),
        *(("observer", "GET", path) for path in _NOT_THE_EVIDENCE_PATH),
    ],
)
async def test_nothing_but_the_mounted_evidence_get_is_exempt(
    surface: str, method: str, path: str
) -> None:
    settings = _surface_settings(surface, rate_limit_ip_per_window=1)
    limiter = InMemoryRateLimits(lock=threading.RLock())

    # Counted, then refused: neither half of the exemption applies.
    assert await _admission(settings, limiter, method, path) is None
    refused = await _admission(settings, limiter, method, path)

    assert refused is not None and refused.status_code == 429


def test_the_not_exempt_cases_cover_three_unmounted_surfaces() -> None:
    # The parametrization above derives its unmounted surfaces from the
    # constant; an exemption widened to every surface would empty that list.
    assert set(_WEB_SURFACES) - CONTROL_PLANE_EVIDENCE_SURFACES == {
        "actions",
        "control",
        "internal",
    }
    assert CONTROL_PLANE_EVIDENCE_SURFACES <= set(_WEB_SURFACES)


@pytest.mark.parametrize("surface", _WEB_SURFACES)
def test_the_evidence_read_is_exempt_exactly_where_the_route_is_mounted(surface: str) -> None:
    app = create_app(
        _surface_settings(surface, rate_limit_ip_per_window=1, release="release-under-test"),
        configure_store_arg=False,
        init_observability=False,
    )
    mounted = _mounts(app, "GET", CONTROL_PLANE_EVIDENCE_PATH)
    # The route table decides, so a surface that gains or loses the route
    # fails here until the exemption follows it.
    assert mounted == (surface in CONTROL_PLANE_EVIDENCE_SURFACES)
    client = TestClient(app)
    assert client.get("/ready").status_code != 429
    assert client.get("/ready").status_code == 429

    for accept in ("application/json", "text/html", "*/*"):
        response = client.get(
            CONTROL_PLANE_EVIDENCE_PATH,
            params={"rollout_check": "0f6c1d7e2b9a4c53"},
            headers={**_COORDINATOR_HEADERS, "Accept": accept},
        )
        if mounted:
            assert response.status_code == 200
            assert response.json()["release"] == "release-under-test"
        else:
            # Refused before the 404 page for this path is rendered.
            assert response.status_code == 429
            assert response.json()["error"]["type"] == "rate_limited"


def test_a_healthy_holdback_serves_its_release_evidence_on_an_exhausted_shared_bucket() -> None:
    # The AWS and Azure control planes: the observer surface, deployed, behind
    # no edge that overwrites the client address, so `untrusted` mode and one
    # bucket for every caller.
    settings = Settings(
        environment="canary",
        service_surface="observer",
        observer_internal_token="observer-only-" + "o" * 32,
        release="4f0c2d9e1b7a4c5386d0e2f1a9b8c7d6e5f40312",
        trusted_domain="aws.trustedrouter.com",
        api_base_url="https://api-aws.trustedrouter.com/v1",
        rate_limit_enabled=True,
        rate_limit_client_ip_mode="untrusted",
        rate_limit_ip_per_window=2,
    )
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    client = TestClient(app)

    # Third-party traffic from two addresses spends the whole allowance.
    for index in range(2):
        spent = client.get("/ready", headers={"X-Forwarded-For": f"198.51.100.{index}"})
        assert spent.status_code != 429
    limited = client.get("/ready", headers={"X-Forwarded-For": "198.51.100.9"})
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"

    # The coordinator reads the evidence three times per fetch, twice per check.
    for attempt in range(6):
        evidence = client.get(
            CONTROL_PLANE_EVIDENCE_PATH,
            params={"rollout_check": f"{attempt:032x}"},
            headers=_COORDINATOR_HEADERS,
        )
        assert evidence.status_code == 200
        assert evidence.json() == {
            "plane": "aws.trustedrouter.com",
            "release": "4f0c2d9e1b7a4c5386d0e2f1a9b8c7d6e5f40312",
            "api_base_url": "https://api-aws.trustedrouter.com/v1",
        }

    # Everything else is still refused, before its handler runs.
    for path in _OTHER_TRUST_ROUTES:
        refused = client.get(path, headers=_COORDINATOR_HEADERS)
        assert refused.status_code == 429, path
        assert refused.json()["error"]["type"] == "rate_limited"
    for method in ("HEAD", "POST", "OPTIONS"):
        refused = client.request(method, CONTROL_PLANE_EVIDENCE_PATH)
        assert refused.status_code == 429, method
    for path in ("/trust/control-%09plane.json", "/trust/control-plane.json%0A", "/trust/control-plane.json/"):
        refused = client.get(path, headers={"Accept": "text/html"})
        assert refused.status_code == 429, path
        assert refused.json()["error"]["type"] == "rate_limited"
    assert client.get("/ready").status_code == 429


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


@pytest.mark.parametrize(
    ("path", "payload", "subject_prefix"),
    (
        (
            "/support/inquiry",
            {
                "name": "Ada",
                "email": "ada@example.com",
                "category": "api",
                "subject": "Help",
                "message": "Please help",
                "website": "",
            },
            "support:",
        ),
        (
            "/trustedos/inquiry",
            {
                "name": "Ada",
                "email": "ada@example.com",
                "company": "Analytical Engines",
                "message": "Partnership request",
                "website": "",
            },
            "",
        ),
    ),
)
def test_combined_bridge_forms_keep_legacy_client_identity_without_weakening_split(
    path: str,
    payload: dict[str, str],
    subject_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_rate_subjects: list[str] = []

    class FakeEmailService:
        def send(self, _message: EmailMessage) -> bool:
            return True

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

    bridged = Settings.model_construct(
        environment="production",
        service_surface="combined",
        allow_deployed_combined_surface=True,
        rate_limit_enabled=False,
        partner_inquiry_email="leads@example.com",
    )
    bridge_app = create_app(
        bridged,
        configure_store_arg=False,
        init_observability=False,
    )
    for address in ("198.51.100.10", "203.0.113.20"):
        response = TestClient(bridge_app, client=(address, 50000)).post(
            path,
            json=payload,
        )
        assert response.status_code == 200, response.text
    assert seen_rate_subjects == [
        f"{subject_prefix}198.51.100.10",
        f"{subject_prefix}203.0.113.20",
    ]

    seen_rate_subjects.clear()
    split = Settings.model_construct(
        environment="production",
        service_surface="actions",
        allow_deployed_combined_surface=False,
        rate_limit_enabled=False,
        rate_limit_client_ip_mode="untrusted",
        partner_inquiry_email="leads@example.com",
    )
    split_app = create_app(
        split,
        configure_store_arg=False,
        init_observability=False,
    )
    for address in ("198.51.100.10", "203.0.113.20"):
        response = TestClient(split_app, client=(address, 50000)).post(
            path,
            json=payload,
        )
        assert response.status_code == 200, response.text
    assert seen_rate_subjects == [
        f"{subject_prefix}untrusted_lb",
        f"{subject_prefix}untrusted_lb",
    ]
