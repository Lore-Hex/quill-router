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

ROOT = Path(__file__).resolve().parents[1]


def _module():
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
