from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from trusted_router.config import Settings
from trusted_router.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def _load_url_map_module() -> ModuleType:
    path = ROOT / "scripts" / "deploy" / "service_surface_url_map.py"
    spec = importlib.util.spec_from_file_location("service_surface_url_map", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


URL_MAP = _load_url_map_module()


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
    public_paths = {route.path for route in public.routes}
    violations = {
        route.path
        for route in combined.routes
        if URL_MAP.route_surface(_concrete(route.path)) == "public"
        and route.path not in public_paths
    }
    assert violations == set()


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
        "/signup",
        "/billing/checkout",
        "/internal/stripe/webhook",
        "/internal/gateway/authorize",
        "/v1/chat/completions",
    ],
)
def test_t2_through_t4_and_legacy_alias_paths_are_not_public(path: str) -> None:
    assert URL_MAP.route_surface(path) != "public"


@pytest.mark.parametrize("prefix", ["", "/v1"])
@pytest.mark.parametrize(
    "path",
    [
        "/internal/stripe/webhook",
        "/internal/paypal/webhook",
        "/internal/adyen/webhook",
        "/internal/veriff/webhook",
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


def test_every_nonpublic_wildcard_also_owns_its_bare_prefix() -> None:
    for patterns in (
        URL_MAP.CONTROL_PATH_PATTERNS,
        URL_MAP.ACTIONS_PATH_PATTERNS,
        URL_MAP.INTERNAL_PATH_PATTERNS,
    ):
        for pattern in patterns:
            if pattern.endswith("/*"):
                assert URL_MAP.route_surface(pattern.removesuffix("/*")) != "public"


@pytest.mark.parametrize(
    ("path", "surface"),
    (
        ("/", "public"),
        ("/status.json", "public"),
        ("/static/a.css", "public"),
        ("/console", "control"),
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
