from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from tests.route_inventory import effective_route_objects, route_paths
from trusted_router.config import SERVICE_SURFACE_SECRET_OWNERS, Settings
from trusted_router.main import create_app


def _routes_of(app):
    """Leaf routes, each with ``.path`` set to the path actually served.

    A raw child of an include reports its path relative to that include, so
    reading ``route.path`` straight off one yields ``/gateway/validate`` where
    this test means ``/internal/gateway/validate``.
    """
    return effective_route_objects(app)


ROOT = Path(__file__).resolve().parents[1]


def _load_url_map_module() -> ModuleType:
    path = ROOT / "scripts" / "deploy" / "service_surface_url_map.py"
    spec = importlib.util.spec_from_file_location("service_surface_url_map", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


URL_MAP = _load_url_map_module()

PUBLIC_BACKEND = "public-backend"
LEGACY_BACKEND = "legacy-backend"


def _emitted_map() -> dict[str, object]:
    return URL_MAP.rewrite_url_map(
        {"name": "trusted-router-control-map", "defaultService": LEGACY_BACKEND},
        PUBLIC_BACKEND,
        LEGACY_BACKEND,
        LEGACY_BACKEND,
        LEGACY_BACKEND,
        ["trustedrouter.com"],
    )


def _emitted_backend(url_map: dict[str, object], path: str) -> str:
    """Select a pathRule using Google URL-map matching, not the simulator."""
    matchers = url_map["pathMatchers"]
    assert isinstance(matchers, list)
    matcher = next(
        item
        for item in matchers
        if item["name"] == "trusted-router-service-surfaces"
    )
    exact: list[tuple[int, str]] = []
    wildcard: list[tuple[int, str]] = []
    for rule in matcher["pathRules"]:
        service = rule["service"]
        for pattern in rule["paths"]:
            if pattern.endswith("/*"):
                prefix = pattern.removesuffix("/*")
                if path.startswith(f"{prefix}/"):
                    wildcard.append((len(prefix), service))
            elif path == pattern:
                exact.append((len(pattern), service))
    if exact:
        return max(exact)[1]
    if wildcard:
        return max(wildcard)[1]
    return matcher["defaultService"]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample", path)


def test_every_combined_route_sent_to_public_is_mounted_by_public() -> None:
    """The T1 URL map must never send public traffic to a missing handler.

    This supersedes the parked four-process contract that every route mounted
    by a surface had to route back to that same surface.  The production split
    has only public and legacy processes: T2--T4 routes may still be mounted by
    the public app, but the availability-tier contract deliberately keeps them
    on the combined legacy service.  The reverse dependency remains forbidden,
    so every combined route selected for public must exist on the public app.
    """
    combined = create_app(
        Settings(environment="test", service_surface="combined"),
        configure_store_arg=False,
        init_observability=False,
    )
    public = create_app(
        Settings(environment="test", service_surface="public"),
        configure_store_arg=False,
        init_observability=False,
    )
    public_paths = route_paths(public)
    emitted = _emitted_map()
    violations = {
        route.path
        for route in _routes_of(combined)
        if _emitted_backend(emitted, _concrete(route.path)) == PUBLIC_BACKEND
        and route.path not in public_paths
    }
    assert violations == set()


def test_broadcast_drain_is_control_worker_owned_not_internal_mounted() -> None:
    internal = create_app(
        Settings(environment="test", service_surface="internal"),
        configure_store_arg=False,
        init_observability=False,
    )
    combined = create_app(
        Settings(
            environment="test",
            service_surface="combined",
            settle_outbox_enabled=True,
        ),
        configure_store_arg=False,
        init_observability=False,
    )
    control = create_app(
        Settings(
            environment="test",
            service_surface="control",
            settle_outbox_enabled=True,
        ),
        configure_store_arg=False,
        init_observability=False,
    )

    internal_paths = route_paths(internal)
    combined_paths = route_paths(combined)
    assert "/internal/broadcast/drain" not in internal_paths
    assert "/v1/internal/broadcast/drain" not in internal_paths
    assert "/internal/broadcast/drain" in combined_paths
    assert URL_MAP.route_surface("/internal/broadcast/drain") == "control"
    assert URL_MAP.route_surface("/v1/internal/broadcast/drain") == "control"
    assert any(
        handler.__name__ == "_start_auto_refill_outbox_loop"
        for handler in control.router.on_startup
    )
    assert any(
        handler.__name__ == "_start_auto_refill_outbox_loop"
        for handler in combined.router.on_startup
    )


def test_internal_surface_route_inventory_matches_capability_audit() -> None:
    internal = create_app(
        Settings(environment="test", service_surface="internal"),
        configure_store_arg=False,
        init_observability=False,
    )
    expected = {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("POST", "/internal/gateway/validate"),
        ("POST", "/internal/gateway/key"),
        ("POST", "/internal/gateway/resolve-custom-model"),
        ("POST", "/internal/gateway/authorize"),
        ("POST", "/internal/gateway/heartbeat"),
        ("POST", "/internal/gateway/settle"),
        ("POST", "/internal/gateway/refund"),
        ("POST", "/internal/gateway/settle-outbox/drain"),
        ("POST", "/internal/gateway/receipt-keys/collect"),
        ("POST", "/internal/gateway/spend-lease/register-boot"),
        ("POST", "/internal/gateway/regional-quota/reconcile"),
        ("POST", "/internal/gateway/home-settlement/drain"),
        ("POST", "/internal/gateway/deferred/reap"),
        ("POST", "/internal/gateway/video/jobs/prepare"),
        ("POST", "/internal/gateway/video/jobs/{job_id}/queued"),
        ("POST", "/internal/gateway/video/jobs/{job_id}/lookup"),
        ("POST", "/internal/gateway/video/jobs/claim"),
        ("POST", "/internal/gateway/video/jobs/{job_id}/update"),
        ("POST", "/internal/gateway/video/jobs/{job_id}/cleaned"),
        ("POST", "/internal/gateway/fetch-image"),
        ("POST", "/internal/reconcile/generation-activity"),
        ("POST", "/internal/federation/resolve-key"),
        ("POST", "/internal/federation/apply-usage"),
        ("POST", "/internal/federation/credit-transfer"),
        ("POST", "/internal/federation/credit-transfers"),
        ("POST", "/internal/federation/credit-transfers/recover"),
        ("GET", "/internal/synthetic/health"),
        ("POST", "/internal/synthetic/samples"),
        ("POST", "/internal/synthetic/benchmark"),
        ("POST", "/internal/synthetic/route-health"),
        ("POST", "/internal/synthetic/remediate"),
        ("POST", "/internal/synthetic/run"),
        ("GET", "/internal/sentry-test"),
    }
    actual = {
        (method, route.path)
        for route in _routes_of(internal)
        if not route.path.startswith("/v1")
        for method in (route.methods or set())
    }

    assert actual == expected
    assert {
        (method, route.path.removeprefix("/v1"))
        for route in _routes_of(internal)
        if route.path.startswith("/v1")
        for method in (route.methods or set())
    } == expected


def test_byok_decrypt_credentials_are_control_owned_only() -> None:
    assert SERVICE_SURFACE_SECRET_OWNERS["byok_kms_key_name"] == frozenset({"control"})
    assert SERVICE_SURFACE_SECRET_OWNERS["byok_envelope_key_b64"] == frozenset({"control"})


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/pricing",
        "/blog",
        "/status",
        "/status.json",
        "/catalog",
        "/leaderboard",
        "/static/charter.css",
        "/robots.txt",
        "/.well-known/oauth-authorization-server",
        "/health",
        "/ready",
    ],
)
def test_t1_marketing_static_status_and_catalog_paths_are_public(path: str) -> None:
    assert URL_MAP.route_surface(path) == "public"


@pytest.mark.parametrize(
    "path",
    [
        "/console",
        "/auth/session",
        "/oauth/apps",
        "/signup",
        "/billing/checkout",
        "/internal/stripe/webhook",
        "/internal/gateway/authorize",
        "/v1/chat/completions",
    ],
)
def test_t2_through_t4_and_legacy_alias_paths_are_not_public(path: str) -> None:
    assert URL_MAP.route_surface(path) != "public"


@pytest.mark.parametrize(
    "path",
    [
        "/oauth/apps",
        "/oauth/apps/verified-app",
        "/v1/oauth/apps",
        "/v1/oauth/apps/verified-app",
        "/oauth/authorized-apps",
        "/oauth/authorized-apps/verified-app",
        "/v1/oauth/authorized-apps",
        "/v1/oauth/authorized-apps/verified-app",
    ],
)
def test_oauth_app_registry_paths_route_to_control(path: str) -> None:
    assert URL_MAP.route_surface(path) == "control"


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize("path", ["/oauth/authorize", "/oauth/token"])
def test_conformant_oauth_protocol_paths_route_to_control(prefix: str, path: str) -> None:
    """Consent and token exchange belong beside their legacy twins.

    /oauth/authorize renders the consent page from the user's session cookie
    and /oauth/token mints an API key and can touch Stripe -- control-plane
    capabilities. Serving them from public would hand the anonymous-read
    surface store writes and key minting. The unversioned aliases matter
    most: unmatched paths default to public, so without an explicit control
    rule they would silently land on a surface that does not mount them.
    """
    full_path = f"{prefix}{path}"
    assert URL_MAP.route_surface(full_path) == "control"
    assert _emitted_backend(_emitted_map(), full_path) == LEGACY_BACKEND


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize(
    "path",
    [
        "/internal/stripe/webhook",
        "/internal/paypal/webhook",
        "/internal/adyen/webhook",
        "/internal/veriff/webhook",
        "/internal/routable/webhook",
        "/internal/ses/notifications",
        "/internal/chat/issue-browser-key",
    ],
)
def test_control_exceptions_beat_the_internal_wildcard(prefix: str, path: str) -> None:
    unversioned = path.removeprefix("/internal")
    candidate = f"{prefix}/internal{unversioned}"
    assert URL_MAP.route_surface(candidate) == "control"
    assert URL_MAP.route_surface(f"{prefix}/internal/gateway/authorize") == "internal"


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize("path", ["/payouts", "/payouts/sample"])
def test_creator_payout_paths_route_to_control(prefix: str, path: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}{path}") == "control"


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_catalog_public_and_authenticated_paths_have_distinct_owners(prefix: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}/models/count") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/picker") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/author/slug/endpoints") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/user") == "control"
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided/sample") == "public"


@pytest.mark.parametrize("path", ["/support/inquiry", "/trustedos/inquiry"])
def test_anonymous_actions_leave_the_public_renderer(path: str) -> None:
    assert URL_MAP.route_surface(path) == "actions"
    assert URL_MAP.route_surface(path.removesuffix("/inquiry")) == "public"


def test_entire_group_buy_stays_on_the_t2_legacy_control_slot() -> None:
    assert URL_MAP.route_surface("/bedrock-group-buy") == "control"
    assert URL_MAP.route_surface("/bedrock-group-buy/") == "control"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy") == "control"
    assert URL_MAP.route_surface("/bedrock-group-buy/manage") == "control"
    assert URL_MAP.route_surface("/bedrock-group-buy/pledge") == "control"
    assert URL_MAP.route_surface("/bedrock-group-buy/withdraw") == "control"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy/me") == "control"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy/pledge") == "control"


def test_every_emitted_nonpublic_wildcard_has_an_exact_bare_twin() -> None:
    emitted = _emitted_map()
    matchers = emitted["pathMatchers"]
    matcher = next(
        item
        for item in matchers
        if item["name"] == "trusted-router-service-surfaces"
    )
    owners = {
        pattern: rule["service"]
        for rule in matcher["pathRules"]
        for pattern in rule["paths"]
    }
    missing_or_wrong = {
        pattern: owners.get(pattern.removesuffix("/*"))
        for pattern, service in owners.items()
        if pattern.endswith("/*")
        and service != PUBLIC_BACKEND
        and owners.get(pattern.removesuffix("/*")) != service
    }
    assert missing_or_wrong == {}


@pytest.mark.parametrize(
    ("path", "backend"),
    (
        ("/bedrock-group-buy", LEGACY_BACKEND),
        ("/bedrock-group-buy/", LEGACY_BACKEND),
        ("/bedrock-group-buy/manage", LEGACY_BACKEND),
        ("/v1/bedrock-group-buy", LEGACY_BACKEND),
        ("/console", LEGACY_BACKEND),
        ("/auth/session", LEGACY_BACKEND),
        ("/oauth/apps/verified-app", LEGACY_BACKEND),
        ("/signup", LEGACY_BACKEND),
        ("/internal/gateway/settle", LEGACY_BACKEND),
        ("/v1/chat/completions", LEGACY_BACKEND),
        ("/google_oauth_callback", LEGACY_BACKEND),
        ("/internal/stripe/webhook", LEGACY_BACKEND),
        ("/", PUBLIC_BACKEND),
        ("/status.json", PUBLIC_BACKEND),
        ("/static/app.css", PUBLIC_BACKEND),
        ("/og.png", PUBLIC_BACKEND),
        ("/robots.txt", PUBLIC_BACKEND),
        ("/sitemap.xml", PUBLIC_BACKEND),
        ("/.well-known/mcp/server-card.json", PUBLIC_BACKEND),
        ("/leaderboard", PUBLIC_BACKEND),
        ("/trust", PUBLIC_BACKEND),
        ("/health", PUBLIC_BACKEND),
        ("/v1/health", PUBLIC_BACKEND),
    ),
)
def test_emitted_map_routes_representative_paths(path: str, backend: str) -> None:
    assert _emitted_backend(_emitted_map(), path) == backend


@pytest.mark.parametrize(
    ("path", "surface"),
    (
        ("/", "public"),
        ("/status.json", "public"),
        ("/static/a.css", "public"),
        ("/.well-known/mcp/server-card.json", "public"),
        ("/console", "control"),
        ("/oauth/apps/verified-app", "control"),
        ("/signup", "control"),
        ("/internal/gateway/settle", "internal"),
        ("/v1/chat/completions", "control"),
        ("/bedrock-group-buy", "control"),
        ("/bedrock-group-buy/manage", "control"),
        ("/google_oauth_callback", "control"),
        ("/internal/stripe/webhook", "control"),
    ),
)
def test_representative_concrete_path_ownership_is_unchanged(
    path: str,
    surface: str,
) -> None:
    assert URL_MAP.route_surface(path) == surface


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_carrier_texml_is_public_but_notification_mutations_are_control(prefix: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}/notify/texml") == "public"
    assert URL_MAP.route_surface(f"{prefix}/notify") == "control"
    assert URL_MAP.route_surface(f"{prefix}/notify/phone/start") == "control"


def test_rewrite_preserves_explicit_unrelated_hosts_but_defaults_unknown_to_public() -> None:
    existing = {
        "name": "trusted-router-control",
        "id": "123",
        "selfLink": "output-only",
        "defaultService": "old-default",
        "hostRules": [
            {"hosts": ["trustedrouter.com", "www.trustedrouter.com"], "pathMatcher": "old"},
            {
                "hosts": [
                    "api.trustedrouter.com",
                    "aws.trustedrouter.com",
                    "azure.trustedrouter.com",
                    "b.trustedrouter.com",
                    "a.uptimerouter.com",
                    "c.allyrouter.com",
                    "alerts.trustedrouter.com",
                ],
                "pathMatcher": "regional-and-attested",
            },
            {"hosts": ["unrelated.example"], "pathMatcher": "unrelated"},
        ],
        "pathMatchers": [
            {"name": "old", "defaultService": "old-default"},
            {
                "name": "regional-and-attested",
                "defaultService": "regional-backend",
            },
            {"name": "unrelated", "defaultService": "other-backend"},
        ],
        "tests": [
            {"host": "trustedrouter.com", "path": "/v1/models", "service": "old-default"},
            {"host": "unrelated.example", "path": "/", "service": "other-backend"},
        ],
    }

    result = URL_MAP.rewrite_url_map(
        existing,
        "public-backend",
        "actions-backend",
        "control-backend",
        "internal-backend",
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
    )

    assert result["defaultService"] == "public-backend"
    assert "id" not in result and "selfLink" not in result
    first_party = next(
        rule for rule in result["hostRules"] if rule["pathMatcher"] == "trusted-router-service-surfaces"
    )
    assert set(first_party["hosts"]) == {
        "trustedrouter.com",
        "www.trustedrouter.com",
        "status.trustedrouter.com",
        "trust.trustedrouter.com",
        "eu.trustedrouter.com",
        "status-us.trustedrouter.com",
        "status-eu.trustedrouter.com",
        "allyrouter.com",
        "www.allyrouter.com",
        "status.allyrouter.com",
        "trust.allyrouter.com",
        "uptimerouter.com",
        "www.uptimerouter.com",
        "status.uptimerouter.com",
        "trust.uptimerouter.com",
    }
    preserved_hosts = {
        host
        for rule in result["hostRules"]
        if rule is not first_party
        for host in rule["hosts"]
    }
    assert preserved_hosts == {
        "api.trustedrouter.com",
        "aws.trustedrouter.com",
        "azure.trustedrouter.com",
        "b.trustedrouter.com",
        "a.uptimerouter.com",
        "c.allyrouter.com",
        "alerts.trustedrouter.com",
        "unrelated.example",
    }
    matcher = next(
        item for item in result["pathMatchers"] if item["name"] == "trusted-router-service-surfaces"
    )
    assert matcher["defaultService"] == "public-backend"
    assert {rule["service"] for rule in matcher["pathRules"]} == {
        "public-backend",
        "actions-backend",
        "control-backend",
        "internal-backend",
    }
    assert result["tests"][0]["service"] == "public-backend"
    assert result["tests"][1]["service"] == "other-backend"


def test_one_service_cutover_keeps_every_nonpublic_pattern_on_legacy() -> None:
    legacy = "projects/p/global/backendServices/trusted-router-control-backend"
    public = "projects/p/global/backendServices/trusted-router-public-backend"
    result = URL_MAP.rewrite_url_map(
        {"defaultService": legacy},
        public,
        legacy,
        legacy,
        legacy,
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
    )
    matcher = next(
        item
        for item in result["pathMatchers"]
        if item["name"] == "trusted-router-service-surfaces"
    )
    rule_by_paths = {tuple(rule["paths"]): rule["service"] for rule in matcher["pathRules"]}
    for patterns in (
        URL_MAP.ACTIONS_PATH_PATTERNS,
        URL_MAP.CONTROL_PATH_PATTERNS,
        URL_MAP.INTERNAL_PATH_PATTERNS,
    ):
        assert rule_by_paths[patterns] == legacy


def test_rewrite_refuses_an_existing_catch_all_host_rule() -> None:
    with pytest.raises(ValueError, match="catch-all"):
        URL_MAP.rewrite_url_map(
            {
                "defaultService": "legacy",
                "hostRules": [{"hosts": ["*"], "pathMatcher": "old"}],
            },
            "public",
            "actions",
            "control",
            "internal",
            ["trustedrouter.com"],
        )


def test_rewrite_refuses_missing_unrelated_default_service() -> None:
    with pytest.raises(ValueError, match="without an existing default service"):
        URL_MAP.rewrite_url_map(
            {"hostRules": []},
            "public",
            "actions",
            "control",
            "internal",
            ["trustedrouter.com"],
        )


def test_rewrite_refuses_first_party_wildcard_instead_of_stealing_subdomains() -> None:
    with pytest.raises(ValueError, match="wildcard first-party"):
        URL_MAP.rewrite_url_map(
            {
                "defaultService": "legacy",
                "hostRules": [
                    {"hosts": ["*.trustedrouter.com"], "pathMatcher": "old"}
                ],
            },
            "public",
            "actions",
            "control",
            "internal",
            ["trustedrouter.com"],
        )


def test_rewrite_refuses_reserved_matcher_owned_by_unrelated_host() -> None:
    existing = {
        "defaultService": "legacy",
        "hostRules": [
            {
                "hosts": ["unrelated.example"],
                "pathMatcher": "trusted-router-service-surfaces",
            }
        ],
        "pathMatchers": [
            {
                "name": "trusted-router-service-surfaces",
                "defaultService": "unrelated-backend",
            }
        ],
    }

    with pytest.raises(ValueError, match="unrelated host still references"):
        URL_MAP.rewrite_url_map(
            existing,
            "public-backend",
            "actions-backend",
            "control-backend",
            "internal-backend",
            ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
        )
