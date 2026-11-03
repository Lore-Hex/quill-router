"""The Terraform URL map and the deploy-time renderer must route the same action paths.

The Terraform file is what `terraform apply` of infra/ writes to the live load balancer; the
Python module is what the edge scripts render. When they disagree, whichever runs last wins and
the other's paths silently fall through to the public default service (a 404 for POST-only
action routes). /token-exchange/events was missing from Terraform from 2026-10-14 to 2026-10-27.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    path = ROOT / "scripts/deploy/service_surface_url_map.py"
    spec = importlib.util.spec_from_file_location("service_surface_url_map", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _terraform_rule_paths(service_suffix: str) -> list[set[str]]:
    source = (ROOT / "infra/control_lb_url_map.tf").read_text()
    matcher = source.split('name            = "trusted-router-service-surfaces"', 1)[1]
    rules = re.findall(r"path_rule \{\n(.*?)\n    \}", matcher, re.DOTALL)
    found = []
    for rule in rules:
        if f"backendServices/{service_suffix}" not in rule:
            continue
        paths_block = re.search(r"paths\s*=\s*\[(.*?)\]", rule, re.DOTALL)
        assert paths_block is not None, rule
        found.append(set(re.findall(r'"([^"]+)"', paths_block.group(1))))
    return found


def test_terraform_routes_every_action_path_the_renderer_routes() -> None:
    module = _module()
    expected = set(module.ACTIONS_PATH_PATTERNS)
    control_rules = _terraform_rule_paths("trusted-router-control-backend")
    assert control_rules, "no control-backend path rules found in infra/control_lb_url_map.tf"
    terraform_actions = [rule for rule in control_rules if "/token-exchange/brief" in rule]
    assert len(terraform_actions) == 1, control_rules
    assert terraform_actions[0] == expected, (
        f"Terraform actions rule {sorted(terraform_actions[0])} != renderer {sorted(expected)}"
    )


def test_terraform_routes_every_public_path_the_renderer_routes() -> None:
    module = _module()
    expected = set(module.PUBLIC_PATH_PATTERNS)
    public_rules = _terraform_rule_paths("trusted-router-public-backend")
    assert any(rule == expected for rule in public_rules), (
        f"no Terraform public rule equals the renderer list {sorted(expected)}; found {public_rules}"
    )


def test_terminal_route_default_is_off() -> None:
    source = (ROOT / "infra/control_terminal_backend.tf").read_text()
    weights = re.findall(r"^\s*terminal_c1_route_weight\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    assert weights == ["0"], "Terminal centralization must land switched off"


# POST paths are ready; disposition remains explicitly blocked on matching mode.
TERMINAL_PATHS = {
    "/internal/gateway/settle", "/internal/gateway/heartbeat", "/internal/gateway/refund",
    "/v1/internal/gateway/settle", "/v1/internal/gateway/heartbeat", "/v1/internal/gateway/refund",
}


def test_terminal_rule_parity_paths_weights_backends_and_deadline() -> None:
    module = _module()
    source = (ROOT / "infra/control_lb_url_map.tf").read_text()
    rules = re.findall(r"^    path_rule \{\n(.*?)^    \}", source, re.MULTILINE | re.DOTALL)
    terminal = [rule for rule in rules if "local.terminal_c1_route_weight" in rule]
    assert len(terminal) == 1, "Terraform must have exactly one terminal rule"
    rule = terminal[0]
    paths = re.search(r"paths\s*=\s*\[(.*?)\]", rule, re.DOTALL)
    assert paths is not None
    assert set(re.findall(r'"([^"]+)"', paths[1])) == TERMINAL_PATHS
    assert set(module.TERMINAL_PATH_PATTERNS) == TERMINAL_PATHS
    weight_source = (ROOT / "infra/control_terminal_backend.tf").read_text()
    weight = re.search(r"terminal_c1_route_weight\s*=\s*(\d+)", weight_source)
    assert weight is not None
    w = int(weight[1])
    assert re.search(r"seconds\s*=\s*30\b", rule)
    services = re.findall(r"weighted_backend_services\s*\{(.*?)\}", rule, re.DOTALL)
    # Interpolation contains braces, so inspect the complete block text instead.
    assert len(services) == 2
    assert re.search(r'backendServices/trusted-router-control-backend"\s+weight\s*=\s*100 - local.terminal_c1_route_weight', rule)
    assert re.search(r'backend_service\s*=\s*google_compute_backend_service.control_terminal_c1.id\s+weight\s*=\s*local.terminal_c1_route_weight', rule)
    base = "projects/quill-cloud-proxy/global/backendServices/"
    control = base + "trusted-router-control-backend"
    emitted = module.rewrite_url_map(
        {"defaultService": control}, base + "trusted-router-public-backend",
        control, control, "internal-backend", ["trustedrouter.com"],
        gateway_backend="gateway-backend",
    )
    emitted_rules = emitted["pathMatchers"][0]["pathRules"]
    actual = [r for r in emitted_rules if set(r["paths"]) & TERMINAL_PATHS]
    assert len(actual) == 1, "Renderer must have exactly one terminal rule"
    assert actual[0] == {
        "paths": list(module.TERMINAL_PATH_PATTERNS),
        "routeAction": {
            "timeout": {"seconds": 30},
            "weightedBackendServices": [
                {"backendService": control, "weight": 100 - w},
                {"backendService": base + "trusted-router-control-terminal-c1-backend", "weight": w},
            ],
        },
    }


def test_renderer_preserves_live_authorize_override() -> None:
    module = _module()
    authorize = {
        "paths": ["/internal/gateway/authorize"],
        "routeAction": {
            "timeout": {"seconds": 24},
            "weightedBackendServices": [
                {"backendService": "control", "weight": 0},
                {"backendService": "edge", "weight": 100},
            ],
        },
    }
    live = {
        "defaultService": "control",
        "pathMatchers": [{"name": module._MATCHER_NAME, "pathRules": [authorize]}],
    }
    rendered = module.rewrite_url_map(live, "public", "control", "control", "internal", ["trustedrouter.com"])
    assert authorize in rendered["pathMatchers"][0]["pathRules"]
    assert module.existing_gateway_backend(rendered) is None
