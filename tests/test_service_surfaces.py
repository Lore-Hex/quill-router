from __future__ import annotations

from collections.abc import Iterable

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from tests.route_inventory import effective_routes
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import synthetic as synthetic_routes


@pytest.fixture(autouse=True)
def _reset_synthetic_operation_limits() -> None:
    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001


def _app(surface: str, **overrides: object) -> FastAPI:
    return create_app(
        Settings(
            environment="test",
            service_surface=surface,
            **overrides,
        ),
        configure_store_arg=False,
        init_observability=False,
    )


def _signatures(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, route in effective_routes(app)
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def _paths(app: FastAPI) -> set[str]:
    return {path for path, route in effective_routes(app) if isinstance(route, APIRoute)}


def _endpoints(app: FastAPI) -> dict[object, set[str]]:
    result: dict[object, set[str]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            result.setdefault(route.endpoint, set()).add(route.path)
    return result


def _without_shared_health(routes: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    return {
        signature
        for signature in routes
        if signature[1] not in {"/health", "/v1/health", "/ready", "/v1/ready"}
    }


def test_deployed_combined_surface_requires_the_explicit_migration_bridge() -> None:
    with pytest.raises(ValidationError, match="TR_ALLOW_DEPLOYED_COMBINED_SURFACE"):
        Settings(environment="canary", service_surface="combined")

    # Defense in depth: even a Settings object constructed without validation
    # cannot make create_app mount every authority in a deployed process.
    unvalidated = Settings.model_construct(
        environment="canary",
        service_surface="combined",
        allow_deployed_combined_surface=False,
    )
    with pytest.raises(ValueError, match="TR_ALLOW_DEPLOYED_COMBINED_SURFACE"):
        create_app(
            unvalidated,
            configure_store_arg=False,
            init_observability=False,
        )


def test_combined_migration_bridge_cannot_be_enabled_on_a_split_surface() -> None:
    with pytest.raises(ValidationError, match="may only be set"):
        Settings(
            environment="test",
            service_surface="control",
            allow_deployed_combined_surface=True,
        )


def test_split_surface_route_inventory_is_total_and_has_one_owner() -> None:
    combined = _signatures(_app("combined"))
    public = _signatures(_app("public"))
    actions = _signatures(_app("actions"))
    control = _signatures(_app("control"))
    internal = _signatures(_app("internal"))

    # During this cutover the existing broadcast worker remains on the legacy
    # combined control backend because decrypt authority must not move to the
    # internal surface. Exact URL-map ownership is locked by
    # test_broadcast_drain_is_control_worker_owned_not_internal_mounted.
    combined_control_bridge = {
        ("POST", "/internal/broadcast/drain"),
        ("POST", "/v1/internal/broadcast/drain"),
    }
    split = public | actions | control | internal
    assert combined == split | combined_control_bridge
    assert combined_control_bridge.isdisjoint(split)
    owned = [public, actions, control, internal]
    for index, left in enumerate(owned):
        for right in owned[index + 1 :]:
            assert _without_shared_health(left).isdisjoint(_without_shared_health(right))


def test_public_surface_has_no_authenticated_or_internal_routes() -> None:
    paths = _paths(_app("public"))

    assert "/" in paths
    assert {"/models", "/v1/models", "/ready", "/v1/ready"} <= paths
    assert {
        "/.well-known/mcp.json",
        "/.well-known/mcp/server-card.json",
    } <= paths
    assert {"/bedrock-group-buy", "/bedrock-group-buy/", "/v1/bedrock-group-buy"} <= paths
    assert "/bedrock-group-buy/manage" not in paths
    assert "/bedrock-group-buy/pledge" not in paths
    assert "/v1/bedrock-group-buy/me" not in paths
    assert "/console" not in paths
    assert "/auth/google/login" not in paths
    assert "/v1/auth/google/login" not in paths
    assert "/google_oauth_callback" not in paths
    assert "/signup" not in paths
    assert "/v1/signup" not in paths
    assert "/models/user" not in paths
    assert "/v1/models/user" not in paths
    assert "/support/inquiry" not in paths
    assert "/trustedos/inquiry" not in paths
    assert not any(path.startswith(("/internal/", "/v1/internal/")) for path in paths)


@pytest.mark.parametrize("host", ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"])
def test_public_surface_renders_non_secret_oauth_availability_flags(host: str) -> None:
    from fastapi.testclient import TestClient

    settings = Settings(
        environment="canary",
        service_surface="public",
        attribution_cookie_secret="public-attribution-" + "a" * 32,
        google_oauth_login_available=True,
        github_oauth_login_available=False,
    )
    response = TestClient(
        create_app(
            settings,
            configure_store_arg=False,
            init_observability=False,
        )
    ).get("/", headers={"Host": host})

    assert response.status_code == 200
    assert 'href="/auth/google/login"' in response.text
    assert 'href="/auth/github/login"' not in response.text
    assert settings.google_client_id is None
    assert settings.google_client_secret is None


def test_deployed_public_surface_requires_explicit_oauth_availability() -> None:
    with pytest.raises(ValueError, match="TR_GOOGLE_OAUTH_LOGIN_AVAILABLE"):
        Settings(
            environment="canary",
            service_surface="public",
            attribution_cookie_secret="public-attribution-" + "a" * 32,
        )


def test_public_openapi_keeps_customer_routes_from_split_services() -> None:
    from fastapi.testclient import TestClient

    response = TestClient(_app("public")).get(
        "/openapi.json",
        headers={"Accept-Encoding": "identity"},
    )
    paths = response.json()["paths"]

    assert response.status_code == 200
    assert "/v1/models" in paths
    assert "/v1/keys" in paths
    assert "/mcp" in paths
    assert "/v1/chat/completions" in paths
    assert "/v1/internal/gateway/authorize" not in paths


@pytest.mark.parametrize("surface", ["actions", "control", "internal", "observer"])
def test_non_public_split_surfaces_do_not_serve_openapi(surface: str) -> None:
    from fastapi.testclient import TestClient

    assert TestClient(_app(surface)).get("/openapi.json").status_code == 404


def test_observer_api_reference_redirects_to_public_docs() -> None:
    from fastapi.testclient import TestClient

    response = TestClient(_app("observer")).get(
        "/api/reference",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://trustedrouter.com/api/reference"


def test_actions_surface_owns_only_anonymous_form_submissions() -> None:
    paths = _paths(_app("actions"))

    assert paths == {
        "/health",
        "/v1/health",
        "/ready",
        "/v1/ready",
        "/support/inquiry",
        "/trustedos/inquiry",
    }


def test_control_surface_owns_login_console_and_signed_webhooks_only() -> None:
    paths = _paths(_app("control"))

    assert {
        "/auth/google/login",
        "/v1/auth/google/login",
        "/google_oauth_callback",
        "/console",
        "/signup",
        "/v1/signup",
        "/internal/stripe/webhook",
        "/v1/internal/stripe/webhook",
        "/internal/ses/notifications",
        "/v1/internal/ses/notifications",
        "/internal/chat/issue-browser-key",
        "/v1/internal/chat/issue-browser-key",
        "/bedrock-group-buy/manage",
        "/bedrock-group-buy/pledge",
        "/v1/bedrock-group-buy/me",
    } <= paths
    assert "/bedrock-group-buy" not in paths
    assert "/bedrock-group-buy/" not in paths
    assert "/v1/bedrock-group-buy" not in paths
    assert "/" not in paths
    assert "/internal/gateway/authorize" not in paths
    assert "/v1/internal/gateway/authorize" not in paths
    assert "/internal/synthetic/run" not in paths


def test_internal_surface_owns_gateway_federation_and_workers_only() -> None:
    paths = _paths(_app("internal"))

    assert {
        "/internal/gateway/authorize",
        "/v1/internal/gateway/authorize",
        "/internal/gateway/settle",
        "/v1/internal/gateway/settle",
        "/internal/federation/apply-usage",
        "/v1/internal/federation/apply-usage",
        "/internal/synthetic/run",
        "/v1/internal/synthetic/run",
    } <= paths
    assert "/" not in paths
    assert "/console" not in paths
    assert "/signup" not in paths
    assert "/internal/stripe/webhook" not in paths
    assert "/internal/chat/issue-browser-key" not in paths
    assert all(
        path in {"/health", "/v1/health", "/ready", "/v1/ready"}
        or path.startswith(("/internal/", "/v1/internal/"))
        for path in paths
    )


def test_regional_observer_has_status_and_synthetic_but_no_account_or_money_routes() -> None:
    paths = _paths(_app("observer"))

    assert {"/", "/status.json", "/internal/synthetic/run", "/v1/internal/synthetic/run"} <= paths
    assert "/signup" not in paths
    assert "/v1/signup" not in paths
    assert "/console" not in paths
    assert "/auth/google/login" not in paths
    assert "/internal/gateway/authorize" not in paths
    assert "/internal/gateway/settle" not in paths
    assert "/internal/federation/apply-usage" not in paths
    assert "/support/inquiry" not in paths
    assert "/trustedos/inquiry" not in paths
    assert "/models/user-provided" not in paths
    assert "/v1/models/user-provided" not in paths


def test_versioned_api_pairs_never_split_across_surface_owners() -> None:
    combined = _app("combined")
    owners = {
        surface: _paths(_app(surface)) for surface in ("public", "actions", "control", "internal")
    }

    for paths in _endpoints(combined).values():
        unprefixed = {path for path in paths if not path.startswith("/v1/")}
        versioned = {path for path in paths if path.startswith("/v1/")}
        if not unprefixed or not versioned:
            continue
        assert versioned == {f"/v1{path}" for path in unprefixed}
        for path in unprefixed:
            pair = f"/v1{path}"
            assert [surface for surface, owned in owners.items() if path in owned] == [
                surface for surface, owned in owners.items() if pair in owned
            ]


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("public", set()),
        ("actions", set()),
        ("control", {"_start_activation_reminder_loop"}),
        (
            "internal",
            {
                "_start_home_settlement_loop",
                "_start_synthetic_loop",
                "_start_remediator_loop",
            },
        ),
        ("observer", {"_start_synthetic_loop", "_start_remediator_loop"}),
    ],
)
def test_background_worker_ownership(surface: str, expected: set[str]) -> None:
    app = _app(
        surface,
        federation_deferred_settlement_enabled=True,
        federation_settlement_home_token="settlement-home-" + "token-for-worker-test",
        federation_home_base_url="https://trustedrouter.com",
        activation_reminder_interval_seconds=60,
        synthetic_scheduler_interval_seconds=60,
        remediator_mode="observe",
        remediator_in_process_enabled=True,
    )
    workers = {handler.__name__ for handler in app.router.on_startup}
    assert workers == expected


@pytest.mark.parametrize("surface", ["public", "actions"])
def test_anonymous_surface_ready_does_not_touch_the_billing_store(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    def forbidden(_self: object) -> None:
        raise AssertionError("public readiness touched the billing store")

    monkeypatch.setattr(
        "trusted_router.storage.InMemoryStore.readiness_check",
        forbidden,
    )
    from fastapi.testclient import TestClient

    response = TestClient(_app(surface)).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"http_surface": "ready"},
    }


def test_observer_ready_fails_when_its_status_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    def unavailable(_self: object) -> None:
        raise RuntimeError("database unavailable")

    # Patch the TYPE OF THE LIVE SINGLETON, not just InMemoryStore: /ready
    # calls module-level STORE.readiness_check, and a neighbouring test in the
    # same xdist worker may have left a different store class configured --
    # under the post-cutover matrix this intermittently made the class patch
    # miss and /ready answer 200 (observed on PR #840's post-cutover shard).
    import trusted_router.storage as storage_module

    monkeypatch.setattr(
        "trusted_router.storage.InMemoryStore.readiness_check",
        unavailable,
    )
    monkeypatch.setattr(type(storage_module.STORE), "readiness_check", unavailable, raising=False)
    response = TestClient(_app("observer")).get("/ready")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "status": "not_ready",
        "checks": {"billing_store": "unavailable"},
    }


def test_actions_rate_limiter_never_calls_durable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    durable_calls = 0

    def forbidden(_self: object, **_: object) -> None:
        nonlocal durable_calls
        durable_calls += 1
        raise AssertionError("public request touched the durable rate limiter")

    monkeypatch.setattr(
        "trusted_router.storage.InMemoryStore.hit_rate_limit",
        forbidden,
    )
    from fastapi.testclient import TestClient

    client = TestClient(_app("actions"))
    assert client.post("/support/inquiry", json={}).status_code != 500
    assert durable_calls == 0


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong-token"}])
def test_internal_token_is_rejected_before_body_or_store_work(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    store_calls = 0

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal store_calls
        store_calls += 1
        raise AssertionError("unauthenticated gateway request reached Store work")

    monkeypatch.setattr(
        "trusted_router.storage.InMemoryStore.get_key_by_hash",
        forbidden,
    )
    from fastapi.testclient import TestClient

    client = TestClient(
        _app(
            "internal",
            internal_gateway_token="correct-internal-token",  # noqa: S106 - test token.
        )
    )
    response = client.post(
        "/internal/gateway/authorize",
        headers={**headers, "content-type": "application/json"},
        content=b'{"not": "finished"',
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"
    assert store_calls == 0


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_observer_and_billing_tokens_are_scoped_by_internal_route(
    prefix: str,
) -> None:
    from fastapi.testclient import TestClient

    billing_token = "billing-route-only-token"  # noqa: S105 - test token.
    observer_token = "observer-route-only-token"  # noqa: S105 - test token.
    client = TestClient(
        _app(
            "internal",
            internal_gateway_token=billing_token,
            observer_internal_token=observer_token,
        )
    )
    malformed = b'{"not": "finished"'

    observer_denied_on_billing = client.post(
        f"{prefix}/internal/gateway/authorize",
        headers={
            "x-trustedrouter-internal-token": observer_token,
            "content-type": "application/json",
        },
        content=malformed,
    )
    billing_admitted_to_billing = client.post(
        f"{prefix}/internal/gateway/authorize",
        headers={
            "x-trustedrouter-internal-token": billing_token,
            "content-type": "application/json",
        },
        content=malformed,
    )
    billing_denied_on_observer = client.post(
        f"{prefix}/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": billing_token,
            "content-type": "application/json",
        },
        content=malformed,
    )
    observer_admitted_to_observer = client.post(
        f"{prefix}/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": observer_token,
            "content-type": "application/json",
        },
        content=malformed,
    )

    assert observer_denied_on_billing.status_code == 401
    assert billing_admitted_to_billing.status_code == 400
    assert billing_denied_on_observer.status_code == 401
    assert observer_admitted_to_observer.status_code == 400


def test_observer_surface_accepts_only_its_own_internal_token() -> None:
    from fastapi.testclient import TestClient

    observer_token = "observer-surface-token"  # noqa: S105 - test token.
    client = TestClient(
        _app(
            "observer",
            observer_internal_token=observer_token,
            # A legacy value present in process memory must never authenticate
            # the observer route.
            internal_gateway_token="legacy-billing-token",  # noqa: S106
        )
    )
    malformed = b'{"not": "finished"'

    denied = client.post(
        "/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": "legacy-billing-token",
            "content-type": "application/json",
        },
        content=malformed,
    )
    admitted = client.post(
        "/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": observer_token,
            "content-type": "application/json",
        },
        content=malformed,
    )

    assert denied.status_code == 401
    assert admitted.status_code == 400


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_combined_bridge_preserves_gateway_auth_for_synthetic_and_sentry(
    prefix: str,
) -> None:
    from fastapi.testclient import TestClient

    gateway_token = "legacy-combined-gateway-token"  # noqa: S105 - test token.
    observer_token = "future-split-observer-token"  # noqa: S105 - test token.
    settings = Settings.model_construct(
        environment="canary",
        service_surface="combined",
        allow_deployed_combined_surface=True,
        rate_limit_enabled=False,
        enable_sentry_test_route=True,
        internal_gateway_token=gateway_token,
        observer_internal_token=observer_token,
    )
    client = TestClient(
        create_app(
            settings,
            configure_store_arg=False,
            init_observability=False,
        ),
        raise_server_exceptions=False,
    )
    malformed = b'{"not": "finished"'

    synthetic_denied = client.post(
        f"{prefix}/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": observer_token,
            "content-type": "application/json",
        },
        content=malformed,
    )
    synthetic_admitted = client.post(
        f"{prefix}/internal/synthetic/run",
        headers={
            "x-trustedrouter-internal-token": gateway_token,
            "content-type": "application/json",
        },
        content=malformed,
    )
    sentry_denied = client.get(
        f"{prefix}/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": observer_token},
    )
    sentry_admitted = client.get(
        f"{prefix}/internal/sentry-test",
        headers={"x-trustedrouter-internal-token": gateway_token},
    )

    assert synthetic_denied.status_code == 401
    assert synthetic_admitted.status_code == 400
    assert sentry_denied.status_code == 401
    assert sentry_admitted.status_code == 500


@pytest.mark.parametrize(
    "path",
    (
        "/internal/synthetic/run",
        "/v1/internal/synthetic/samples",
        "/internal/sentry-test",
        "/v1/internal/sentry-test",
    ),
)
def test_observer_credential_selection_changes_only_for_the_combined_bridge(
    path: str,
) -> None:
    from trusted_router.routes.internal._shared import internal_service_credential

    gateway_token = "legacy-combined-gateway-token"  # noqa: S105 - test token.
    observer_token = "future-split-observer-token"  # noqa: S105 - test token.
    common = {
        "environment": "test",
        "internal_gateway_token": gateway_token,
        "observer_internal_token": observer_token,
    }
    bridged = Settings(
        **common,
        service_surface="combined",
        allow_deployed_combined_surface=True,
    )
    unbridged = Settings(**common, service_surface="combined")
    internal = Settings(**common, service_surface="internal")
    observer = Settings(**common, service_surface="observer")

    assert internal_service_credential(bridged, path) == ("gateway", gateway_token)
    for settings in (unbridged, internal, observer):
        assert internal_service_credential(settings, path) == (
            "observer",
            observer_token,
        )


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize(
    ("path", "header", "valid_token", "wrong_domain_token"),
    [
        (
            "/internal/federation/resolve-key",
            "x-trustedrouter-federation-token",
            "peer-token-for-malformed-body-test",
            "settlement-token-for-malformed-body-test",
        ),
        (
            "/internal/federation/apply-usage",
            "x-trustedrouter-federation-settlement-token",
            "settlement-token-for-malformed-body-test",
            "credit-token-for-malformed-body-test",
        ),
        (
            "/internal/federation/credit-transfer",
            "x-trustedrouter-federation-credit-token",
            "credit-token-for-malformed-body-test",
            "peer-token-for-malformed-body-test",
        ),
        (
            "/internal/federation/credit-transfers",
            "x-trustedrouter-internal-token",
            "internal-token-for-malformed-body-test",
            "peer-token-for-malformed-body-test",
        ),
        (
            "/internal/federation/credit-transfers/recover",
            "x-trustedrouter-internal-token",
            "internal-token-for-malformed-body-test",
            "credit-token-for-malformed-body-test",
        ),
    ],
)
def test_federation_token_domain_is_checked_before_malformed_body(
    prefix: str,
    path: str,
    header: str,
    valid_token: str,
    wrong_domain_token: str,
) -> None:
    """Each federation power authenticates before JSON parsing.

    The wrong value is deliberately a *valid credential for a different
    federation power*.  A 401 therefore proves both early authentication and
    credential-domain separation; the same malformed body reaches the JSON
    validator only after the route's own credential succeeds.
    """
    from fastapi.testclient import TestClient

    client = TestClient(
        _app(
            "internal",
            internal_gateway_token="internal-token-for-malformed-body-test",  # noqa: S106
            federation_peer_token="peer-token-for-malformed-body-test",  # noqa: S106
            federation_credit_inbound_token=(  # noqa: S106
                "credit-token-for-malformed-body-test"
            ),
            federation_settlement_inbound_tokens=(
                "aws-eu=settlement-token-for-malformed-body-test"
            ),
            federation_credit_peer_base_url="https://peer.example",
            federation_credit_peer_token=(  # noqa: S106
                "outbound-credit-token-for-malformed-body-test"
            ),
        )
    )
    malformed = b'{"not": "finished"'

    denied = client.post(
        f"{prefix}{path}",
        headers={header: wrong_domain_token, "content-type": "application/json"},
        content=malformed,
    )
    parsed_after_auth = client.post(
        f"{prefix}{path}",
        headers={header: valid_token, "content-type": "application/json"},
        content=malformed,
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["type"] == "unauthorized"
    assert parsed_after_auth.status_code == 400
    assert parsed_after_auth.json()["error"]["type"] == "bad_request"
