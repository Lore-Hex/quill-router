from __future__ import annotations

import importlib.util
import json
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
PRESERVED_HOSTS = [
    "api.trustedrouter.com",
    "api.allyrouter.com",
    "api.uptimerouter.com",
    "api-aws.trustedrouter.com",
    "api-azure.trustedrouter.com",
    "api-azure-nz.trustedrouter.com",
    "api-azure-sea.trustedrouter.com",
    "api-eu-west-1.trustedrouter.com",
]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample", path)


@pytest.mark.parametrize(
    "surface", ["public", "actions", "console", "chat", "webhooks", "internal"]
)
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
    ],
)
def test_webhook_exceptions_beat_the_internal_wildcard(prefix: str, path: str) -> None:
    unversioned = path.removeprefix("/internal")
    candidate = f"{prefix}/internal{unversioned}"
    assert URL_MAP.route_surface(candidate) == "webhooks"
    assert URL_MAP.route_surface(f"{prefix}/internal/gateway/authorize") == "internal"


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_console_browser_key_exception_beats_the_internal_wildcard(prefix: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}/internal/chat/issue-browser-key") == "console"
    assert URL_MAP.route_surface(f"{prefix}/internal/gateway/authorize") == "internal"


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_catalog_public_and_authenticated_paths_have_distinct_owners(prefix: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}/models/count") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/picker") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/author/slug/endpoints") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/user") == "console"
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided") == "public"
    assert URL_MAP.route_surface(f"{prefix}/models/user-provided/sample") == "public"


@pytest.mark.parametrize("path", ["/support/inquiry", "/trustedos/inquiry"])
def test_anonymous_actions_leave_the_public_renderer(path: str) -> None:
    assert URL_MAP.route_surface(path) == "actions"
    assert URL_MAP.route_surface(path.removesuffix("/inquiry")) == "public"


def test_group_buy_public_reads_and_private_state_have_distinct_owners() -> None:
    assert URL_MAP.route_surface("/bedrock-group-buy") == "public"
    assert URL_MAP.route_surface("/bedrock-group-buy/") == "public"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy") == "public"
    assert URL_MAP.route_surface("/bedrock-group-buy/manage") == "console"
    assert URL_MAP.route_surface("/bedrock-group-buy/pledge") == "console"
    assert URL_MAP.route_surface("/bedrock-group-buy/withdraw") == "console"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy/me") == "console"
    assert URL_MAP.route_surface("/v1/bedrock-group-buy/pledge") == "console"


@pytest.mark.parametrize("prefix", ["", "/v1"])
def test_carrier_texml_is_public_but_notification_mutations_are_console(prefix: str) -> None:
    assert URL_MAP.route_surface(f"{prefix}/notify/texml") == "public"
    assert URL_MAP.route_surface(f"{prefix}/notify") == "console"
    assert URL_MAP.route_surface(f"{prefix}/notify/phone/start") == "console"


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
                    *PRESERVED_HOSTS,
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
        "console-backend",
        "chat-backend",
        "webhooks-backend",
        "internal-backend",
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
        PRESERVED_HOSTS,
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
    assert preserved_hosts == set(PRESERVED_HOSTS) | {
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
        "console-backend",
        "chat-backend",
        "webhooks-backend",
        "internal-backend",
    }
    assert result["tests"][0]["service"] == "public-backend"
    assert result["tests"][1]["service"] == "other-backend"


def test_rewrite_refuses_an_existing_catch_all_host_rule() -> None:
    with pytest.raises(ValueError, match="catch-all"):
        URL_MAP.rewrite_url_map(
            {
                "defaultService": "legacy",
                "hostRules": [{"hosts": ["*"], "pathMatcher": "old"}],
            },
            "public",
            "actions",
            "console",
            "chat",
            "webhooks",
            "internal",
            ["trustedrouter.com"],
            PRESERVED_HOSTS,
        )


def test_rewrite_refuses_missing_unrelated_default_service() -> None:
    with pytest.raises(ValueError, match="without an existing default service"):
        URL_MAP.rewrite_url_map(
            {"hostRules": []},
            "public",
            "actions",
            "console",
            "chat",
            "webhooks",
            "internal",
            ["trustedrouter.com"],
            PRESERVED_HOSTS,
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
            "console",
            "chat",
            "webhooks",
            "internal",
            ["trustedrouter.com"],
            PRESERVED_HOSTS,
        )


def test_rewrite_refuses_reserved_matcher_owned_by_unrelated_host() -> None:
    existing = {
        "defaultService": "legacy",
        "hostRules": [
            {
                "hosts": ["unrelated.example"],
                "pathMatcher": "trusted-router-service-surfaces",
            },
            {"hosts": PRESERVED_HOSTS, "pathMatcher": "regional"},
        ],
        "pathMatchers": [
            {
                "name": "trusted-router-service-surfaces",
                "defaultService": "unrelated-backend",
            },
            {"name": "regional", "defaultService": "regional-backend"},
        ],
    }

    with pytest.raises(ValueError, match="unrelated host still references"):
        URL_MAP.rewrite_url_map(
            existing,
            "public-backend",
            "actions-backend",
            "console-backend",
            "chat-backend",
            "webhooks-backend",
            "internal-backend",
            ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
            PRESERVED_HOSTS,
        )


def test_rewrite_refuses_preserved_host_that_relied_on_old_default() -> None:
    missing = "api-azure-sea.trustedrouter.com"
    explicitly_routed = [host for host in PRESERVED_HOSTS if host != missing]

    with pytest.raises(ValueError, match="top-level default"):
        URL_MAP.rewrite_url_map(
            {
                "defaultService": "legacy-attested-default",
                "hostRules": [
                    {"hosts": explicitly_routed, "pathMatcher": "attested"}
                ],
                "pathMatchers": [
                    {"name": "attested", "defaultService": "attested-backend"}
                ],
            },
            "public-backend",
            "actions-backend",
            "console-backend",
            "chat-backend",
            "webhooks-backend",
            "internal-backend",
            ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
            PRESERVED_HOSTS,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "headerAction",
            {
                "requestHeadersToAdd": [
                    {"headerName": "Authorization", "headerValue": "Bearer stale"}
                ]
            },
            "header action",
        ),
        ("defaultRouteAction", {"weightedBackendServices": []}, "default route action"),
    ],
)
def test_rewrite_refuses_global_actions_that_can_affect_managed_hosts(
    field: str,
    value: object,
    message: str,
) -> None:
    existing = {
        "defaultService": "legacy",
        "hostRules": [{"hosts": PRESERVED_HOSTS, "pathMatcher": "preserved"}],
        "pathMatchers": [{"name": "preserved", "defaultService": "legacy"}],
        field: value,
    }

    with pytest.raises(ValueError, match=message):
        URL_MAP.rewrite_url_map(
            existing,
            "public",
            "actions",
            "console",
            "chat",
            "webhooks",
            "internal",
            ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
            PRESERVED_HOSTS,
        )


def test_atomic_url_map_output_is_private_and_complete(tmp_path: Path) -> None:
    output = tmp_path / "candidate-url-map.json"
    output.write_text('{"stale":true}\n', encoding="utf-8")
    output.chmod(0o644)

    expected = {"name": "candidate", "hostRules": [{"hosts": ["example.com"]}]}
    URL_MAP._atomic_write_json(output, expected)

    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []


def test_atomic_url_map_replace_failure_preserves_prior_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "candidate-url-map.json"
    prior = b'{"name":"known-good"}\n'
    output.write_bytes(prior)
    output.chmod(0o640)

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("injected os.replace failure")

    monkeypatch.setattr(URL_MAP.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected os.replace failure"):
        URL_MAP._atomic_write_json(output, {"name": "candidate"})

    assert output.read_bytes() == prior
    assert output.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []
