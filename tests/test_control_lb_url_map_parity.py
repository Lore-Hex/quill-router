"""The Terraform URL map and the deploy-time renderer must route the same action paths.

The Terraform file is what `terraform apply` of infra/ writes to the live load balancer; the
Python module is what the edge scripts render. When they disagree, whichever runs last wins and
the other's paths silently fall through to the public default service (a 404 for POST-only
action routes). /token-exchange/events was missing from Terraform from 2026-10-14 to 2026-10-27.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

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


BASE_COMMIT = "5f6d67c3b0f42aa5081237669c08cc0af11f341e"
# Exact bytes from BASE_COMMIT; .txt keeps frozen code out of lint/collection.
BASE_FIXTURES = {
    "scripts/deploy/service_surface_url_map.py": (
        "base_service_surface_url_map.py.txt",
        "9ecf8df52b2b967c5f9d5bd962934dd481d5154b9740b9a497cf792642d150ed",
    ),
    "infra/control_lb_url_map.tf": (
        "base_control_lb_url_map.tf.txt",
        "beeac18f205da50d1d93fa2fc3f9b30c2fc489dce6af051581621c4b77475389",
    ),
}
TERMINAL_PATHS = {
    "/internal/gateway/settle", "/internal/gateway/heartbeat", "/internal/gateway/refund",
    "/v1/internal/gateway/settle", "/v1/internal/gateway/heartbeat", "/v1/internal/gateway/refund",
}
BASE_LINK = "projects/quill-cloud-proxy/global/backendServices/"
CONTROL = BASE_LINK + "trusted-router-control-backend"


def _base_source(path: str) -> str:
    filename, checksum = BASE_FIXTURES[path]
    data = (ROOT / "tests/fixtures/terminal-route" / filename).read_bytes()
    assert hashlib.sha256(data).hexdigest() == checksum, f"base fixture checksum mismatch: {BASE_COMMIT}:{path}"
    return data.decode("utf-8")


@pytest.mark.parametrize("path", BASE_FIXTURES)
def test_frozen_base_fixture_checksum(path: str) -> None:
    _base_source(path)


def _switch(module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    config = tmp_path / "terminal.tf"
    config.write_text("locals {\n  terminal_c1_route_enabled = " + str(enabled).lower() + "\n}\n")
    monkeypatch.setattr(module, "TERMINAL_CONFIG", config)


def _render(module: ModuleType, existing=None, internal=CONTROL, gateway=None):
    return module.rewrite_url_map(
        existing or {"defaultService": CONTROL}, BASE_LINK + "trusted-router-public-backend",
        CONTROL, CONTROL, internal, ["trustedrouter.com"], gateway_backend=gateway,
    )


def test_terminal_route_switch_is_on() -> None:
    # Independent raw-source pin: do not trust the renderer to verify itself.
    # Switched on 2026-11-03 (Joseph approved routing terminals to us-central1).
    source = (ROOT / "infra/control_terminal_backend.tf").read_text()
    assert re.findall(r"\blocals\s*\{[^{}]*\}", source) == [
        "locals {\n  terminal_c1_route_enabled = true\n}",
    ]
    assert _module().terminal_route_enabled() is True


@pytest.mark.parametrize("assignment", ["", "1", '"false"', "False", "true + false",
    "false\nterminal_c1_route_enabled = true", "false\nterminal_c1_route_enabled = 1"])
def test_terminal_switch_rejects_malformed_or_duplicate_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assignment: str,
) -> None:
    module = _module()
    config = tmp_path / "bad.tf"
    config.write_text("locals {\n  terminal_c1_route_enabled = " + assignment + "\n}\n")
    monkeypatch.setattr(module, "TERMINAL_CONFIG", config)
    with pytest.raises(ValueError, match="exactly one"):
        _render(module)


@pytest.mark.parametrize(("source", "expected"), [
    pytest.param("/*\nterminal_c1_route_enabled = false\n*/\n"
                 "locals { terminal_c1_route_enabled = true }", True, id="block-comment"),
    pytest.param("locals { terminal_c1_route_enabled = true }", True, id="inline-true"),
    pytest.param("# terminal_c1_route_enabled = true\n"
                 "locals { terminal_c1_route_enabled = false // active\n}", False, id="line-comments"),
    pytest.param("locals { /* disabled example */ terminal_c1_route_enabled = true # active\n}",
                 True, id="inline-comments"),
    pytest.param("locals { terminal_c1_route_enabled = false }\n"
                 'resource "example" "example" { url = "https://example.com/#/*escaped\\\"*/" }',
                 False, id="comment-markers-in-string"),
])
def test_terminal_switch_reads_active_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected: bool,
) -> None:
    module = _module()
    config = tmp_path / "active.tf"
    config.write_text(source)
    monkeypatch.setattr(module, "TERMINAL_CONFIG", config)
    assert module.terminal_route_enabled() is expected


@pytest.mark.parametrize("source", [
    pytest.param("/* terminal_c1_route_enabled = false */", id="only-block-comment"),
    pytest.param("# terminal_c1_route_enabled = false\n// terminal_c1_route_enabled = true", id="only-line-comments"),
    pytest.param("terminal_c1_route_enabled = false", id="outside-locals"),
    pytest.param("locals { terminal_c1_route_enabled = false }\nlocals { other = true }", id="two-locals"),
    pytest.param("locals { terminal_c1_route_enabled = false other = true }", id="other-local"),
    pytest.param('resource "example" "example" { locals { terminal_c1_route_enabled = true } }', id="nested-locals"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = "terminal_c1_route_enabled"', id="string-name"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = "// terminal_c1_route_enabled"', id="string-line-comment"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = "/* terminal_c1_route_enabled */"', id="string-block-comment"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = local.terminal_c1_route_enabled', id="extra-reference"),
    pytest.param('locals { terminal_c1_route_enabled = false }\n/* unfinished', id="unclosed-comment"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = "unfinished', id="unclosed-string"),
    pytest.param('locals { terminal_c1_route_enabled = false }\nvalue = <<EOF\ntext\nEOF', id="heredoc"),
])
def test_terminal_switch_refuses_unsupported_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    module = _module()
    config = tmp_path / "unsupported.tf"
    config.write_text(source)
    monkeypatch.setattr(module, "TERMINAL_CONFIG", config)
    with pytest.raises(ValueError, match="exactly one"):
        module.terminal_route_enabled()


def _terminal_dynamic(source: str) -> re.Match[str]:
    match = re.search(r'^    dynamic "path_rule" \{\n(.*?)^    \}', source, re.MULTILINE | re.DOTALL)
    assert match is not None
    assert 'for_each = local.terminal_c1_route_enabled ? [1] : []' in match[1]
    return match


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("surface", ["production", "internal", "gateway"])
def test_terminal_rule_parity_paths_backends_and_plain_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, surface: str,
) -> None:
    module = _module()
    _switch(module, tmp_path, monkeypatch, enabled)
    source = (ROOT / "infra/control_lb_url_map.tf").read_text()
    dynamic = _terminal_dynamic(source)
    # Evaluate this deliberately small HCL block's only two content attributes;
    # additional route actions or policy attributes fail the complete match.
    content = re.fullmatch(
        r'\s*for_each = local\.terminal_c1_route_enabled \? \[1\] : \[\]\s*content \{\s*'
        r'paths\s*=\s*\[(.*?)\]\s*service\s*=\s*google_compute_backend_service\.control_terminal_c1\.id\s*\}\s*',
        dynamic[1], re.DOTALL,
    )
    assert content is not None, "Terminal rule must have only paths and service"
    paths = set(re.findall(r'"([^"]+)"', content[1]))
    assert paths == set(module.TERMINAL_PATH_PATTERNS) == TERMINAL_PATHS
    resource = (ROOT / "infra/control_terminal_backend.tf").read_text()
    name = re.search(r'^  name\s*=\s*"([^"]+)"', resource, re.MULTILINE)
    assert name is not None
    assert name[1] == module.TERMINAL_BACKEND_NAME
    expected_link = BASE_LINK + name[1]
    assert module.terminal_backend_link(CONTROL) == expected_link
    internal = "internal-backend" if surface == "internal" else CONTROL
    gateway = "gateway-backend" if surface == "gateway" else None
    emitted = _render(module, internal=internal, gateway=gateway)
    rules = emitted["pathMatchers"][0]["pathRules"]
    terminals = [r for r in rules if set(r["paths"]) & TERMINAL_PATHS]
    assert terminals == ([{"paths": list(module.TERMINAL_PATH_PATTERNS), "service": expected_link}] if enabled else [])
    from tests.test_internal_surface_edge import _resolve_backend
    for path in TERMINAL_PATHS:
        assert _resolve_backend(emitted, "trustedrouter.com", path) == (
            expected_link if enabled else gateway or internal
        )


def test_disabled_terraform_matcher_is_base_except_empty_dynamic_block() -> None:
    source = (ROOT / "infra/control_lb_url_map.tf").read_text()
    dynamic = _terminal_dynamic(source)
    without = source[:dynamic.start()] + source[dynamic.end():]
    # Creator retirement adds only these two earnings aliases; all prior
    # routing, including disabled terminal behavior, must stay identical.
    assert without.count('"/earnings", "/earnings/*", ') == 1
    without = without.replace('"/earnings", "/earnings/*", ', "")
    def uncomment(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)).strip()
    assert uncomment(without) == uncomment(_base_source("infra/control_lb_url_map.tf"))


@pytest.mark.parametrize("shape", ["production", "internal", "gateway", "authorize"])
def test_disabled_renderer_differential_against_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    path = tmp_path / "base_renderer.py"
    path.write_text(_base_source("scripts/deploy/service_surface_url_map.py"))
    spec = importlib.util.spec_from_file_location("base_renderer", path)
    assert spec is not None and spec.loader is not None
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    module = _module()
    _switch(module, tmp_path, monkeypatch, False)
    internal = "internal-backend" if shape == "internal" else CONTROL
    gateway = "gateway-backend" if shape == "gateway" else None
    live = _render(base, internal=internal, gateway=gateway)
    authorize = {"paths": ["/internal/gateway/authorize"], "routeAction": {
        "timeout": {"seconds": 24}, "weightedBackendServices": [
            {"backendService": CONTROL, "weight": 0},
            {"backendService": BASE_LINK + "tr-fastpath-edge-backend", "weight": 100},
        ],
    }}
    if shape == "authorize":
        live["pathMatchers"][0]["pathRules"].append(authorize)
    expected = _render(base, deepcopy(live), internal, gateway)
    actual = _render(module, deepcopy(live), internal, gateway)
    expected_control = next(r for r in expected["pathMatchers"][0]["pathRules"] if "/v1" in r["paths"])
    assert "/earnings" not in expected_control["paths"]
    index = expected_control["paths"].index("/user-models/*") + 1
    expected_control["paths"][index:index] = ["/earnings", "/earnings/*"]
    if shape == "authorize":
        # Base 5f6d67c3b drops authorize. Raw equality here conflicts with the
        # explicit ruling to retain the PR's authorize preservation. Expose and
        # constrain this exception, rather than disguising a patched base.
        assert authorize not in expected["pathMatchers"][0]["pathRules"]
        assert actual != expected
        expected["pathMatchers"][0]["pathRules"].append(authorize)
    assert actual == expected


def test_earnings_aliases_route_to_control_in_both_renderers() -> None:
    from tests.test_internal_surface_edge import _resolve_backend

    rendered = _render(_module())
    for path in ("/earnings", "/earnings/transfer"):
        assert _resolve_backend(rendered, "trustedrouter.com", path) == CONTROL
    assert any({"/earnings", "/earnings/*"} <= rule
               for rule in _terraform_rule_paths("trusted-router-control-backend"))


@pytest.mark.parametrize("enabled", [False, True])
def test_renderer_preserves_live_authorize_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    module = _module()
    _switch(module, tmp_path, monkeypatch, enabled)
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


@pytest.mark.parametrize("enabled", [False, True])
def test_terminal_override_with_extra_key_is_never_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    module = _module()
    # Test the unknown rule itself, not an accidental duplicate of a rule
    # emitted while constructing the live fixture with the committed switch.
    _switch(module, tmp_path, monkeypatch, False)
    live = _render(module)
    _switch(module, tmp_path, monkeypatch, enabled)
    live["pathMatchers"][0]["pathRules"].append({
        "paths": list(module.TERMINAL_PATH_PATTERNS),
        "service": module.terminal_backend_link(CONTROL),
        "description": "operator-owned",
    })
    with pytest.raises(ValueError, match="ambiguous billing override"):
        _render(module, live)
