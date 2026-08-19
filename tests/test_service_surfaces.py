from __future__ import annotations

from collections.abc import Iterable

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from trusted_router.config import Settings
from trusted_router.main import create_app


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
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def _paths(app: FastAPI) -> set[str]:
    return {route.path for route in app.routes if isinstance(route, APIRoute)}


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


def test_combined_surface_is_rejected_outside_local_and_test() -> None:
    with pytest.raises(ValueError, match="TR_SERVICE_SURFACE"):
        create_app(
            Settings(environment="canary", service_surface="combined"),
            configure_store_arg=False,
            init_observability=False,
        )


def test_split_surface_route_inventory_is_total_and_has_one_owner() -> None:
    combined = _signatures(_app("combined"))
    public = _signatures(_app("public"))
    actions = _signatures(_app("actions"))
    control = _signatures(_app("control"))
    internal = _signatures(_app("internal"))

    assert combined == public | actions | control | internal
    owned = [public, actions, control, internal]
    for index, left in enumerate(owned):
        for right in owned[index + 1 :]:
            assert _without_shared_health(left).isdisjoint(_without_shared_health(right))


def test_public_surface_has_no_authenticated_or_internal_routes() -> None:
    paths = _paths(_app("public"))

    assert "/" in paths
    assert {"/models", "/v1/models", "/ready", "/v1/ready"} <= paths
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
    } <= paths
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


def test_versioned_api_pairs_never_split_across_surface_owners() -> None:
    combined = _app("combined")
    owners = {
        surface: _paths(_app(surface))
        for surface in ("public", "actions", "control", "internal")
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


@pytest.mark.parametrize("surface", ["public", "actions", "observer"])
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
