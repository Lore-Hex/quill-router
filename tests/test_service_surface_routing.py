from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.routing import APIRoute

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


@pytest.mark.parametrize("surface", ["public", "actions", "control", "internal"])
def test_every_registered_route_maps_to_its_service_surface(surface: str) -> None:
    app = create_app(
        Settings(environment="test", service_surface=surface),
        configure_store_arg=False,
        init_observability=False,
    )
    shared = {"/health", "/v1/health", "/ready", "/v1/ready"}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path not in shared:
            assert URL_MAP.route_surface(_concrete(route.path)) == surface, route.path


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
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided") == "control"
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided/sample") == "control"


@pytest.mark.parametrize("path", ["/support/inquiry", "/trustedos/inquiry"])
def test_anonymous_actions_leave_the_public_renderer(path: str) -> None:
    assert URL_MAP.route_surface(path) == "actions"
    assert URL_MAP.route_surface(path.removesuffix("/inquiry")) == "public"


def test_rewrite_preserves_unrelated_hosts_and_installs_all_three_backends() -> None:
    existing = {
        "name": "trusted-router-control",
        "id": "123",
        "selfLink": "output-only",
        "defaultService": "old-default",
        "hostRules": [
            {"hosts": ["trustedrouter.com", "www.trustedrouter.com"], "pathMatcher": "old"},
            {"hosts": ["unrelated.example"], "pathMatcher": "unrelated"},
        ],
        "pathMatchers": [
            {"name": "old", "defaultService": "old-default"},
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
        "*.trustedrouter.com",
        "allyrouter.com",
        "*.allyrouter.com",
        "uptimerouter.com",
        "*.uptimerouter.com",
    }
    assert {rule["hosts"][0] for rule in result["hostRules"] if rule is not first_party} == {
        "unrelated.example"
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


def test_rewrite_refuses_an_existing_catch_all_host_rule() -> None:
    with pytest.raises(ValueError, match="catch-all"):
        URL_MAP.rewrite_url_map(
            {"hostRules": [{"hosts": ["*"], "pathMatcher": "old"}]},
            "public",
            "actions",
            "control",
            "internal",
            ["trustedrouter.com"],
        )
