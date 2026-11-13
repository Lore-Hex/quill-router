"""provider_health tests must not pin a route that is about to retire.

provider-catalog-health.yml runs the provider_health tests on the real clock,
so a test that hardcodes a route breaks at that route's scheduled retirement.
Tinfoil's kimi-k3 did on 2026-11-12. This guard turns that into a warning
RETIREMENT_WARNING_DAYS ahead: it fails while a provider_health test still
names a route whose retirement is that close, so the test can be moved to a
live route before the cutover.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trusted_router import provider_lifecycle

TESTS_DIR = Path(__file__).parent
RETIREMENT_WARNING_DAYS = 14


@dataclass(frozen=True)
class PinnedRoute:
    test: str
    provider: str
    model_id: str
    effective_at: datetime


def _is_provider_health_marker(decorator: ast.expr) -> bool:
    return isinstance(decorator, ast.Attribute) and decorator.attr == "provider_health"


def _is_parametrize(decorator: ast.expr) -> bool:
    return (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize")


def _is_row(node: ast.AST) -> bool:
    """A flat literal tuple such as ("google-vertex", "google/gemini-3.6-flash")."""
    return (isinstance(node, ast.Tuple) and len(node.elts) >= 2
            and all(isinstance(e, ast.Constant) for e in node.elts))


@dataclass
class _Strings:
    loose: set[str]
    rows: list[set[str]]


def _strings(node: ast.AST, constants: dict[str, ast.expr], seen: frozenset[str] = frozenset()) -> _Strings:
    """The string literals in a node, following module-level constants it names.
    A flat literal tuple is one row: a model and a provider pair up only within
    the same row, or both outside every row."""
    found = _Strings(set(), [])
    stack: list[ast.AST] = [node]
    while stack:
        child = stack.pop()
        if _is_row(child):
            assert isinstance(child, ast.Tuple)
            found.rows.append({e.value for e in child.elts
                               if isinstance(e, ast.Constant) and isinstance(e.value, str)})
            continue
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.loose.add(child.value)
        elif isinstance(child, ast.Name) and child.id in constants and child.id not in seen:
            nested = _strings(constants[child.id], constants, seen | {child.id})
            found.loose |= nested.loose
            found.rows += nested.rows
        stack.extend(ast.iter_child_nodes(child))
    return found


def _rows(node: ast.expr, constants: dict[str, ast.expr]) -> Iterator[ast.expr]:
    """The parametrize rows of an argvalues expression: one per element."""
    if isinstance(node, ast.Name) and node.id in constants:
        yield from _rows(constants[node.id], constants)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
        "sorted", "list", "tuple",
    } and node.args:
        yield from _rows(node.args[0], constants)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        yield from node.elts
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            yield ast.Tuple(elts=[k for k in (key, value) if k is not None], ctx=ast.Load())
    else:
        yield node


def _mentions(strings: set[str], context: str, provider: str, model_id: str) -> bool:
    names_model = any(s == model_id or s.startswith(f"{model_id}@{provider}/") for s in strings)
    names_provider = (
        provider in strings
        or any(f"@{provider}/" in s for s in strings)
        or provider.replace("-", "_") in context
    )
    return names_model and names_provider


def pinned_routes(
    source: str,
    retirements: Iterable[provider_lifecycle._Retirement],
    *,
    now: datetime,
    filename: str = "<test>",
    days: int = RETIREMENT_WARNING_DAYS,
) -> list[PinnedRoute]:
    """Routes that provider_health tests in ``source`` name and that retire
    after ``now`` but within ``days`` of it."""
    soon = [r for r in retirements if now < r.effective_at <= now + timedelta(days=days)]
    if not soon:
        return []
    tree = ast.parse(source)
    constants = {
        target.id: stmt.value
        for stmt in tree.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None
        for target in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
        if isinstance(target, ast.Name)
    }
    found: list[PinnedRoute] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_provider_health_marker(d) for d in node.decorator_list):
            continue
        body = _strings(ast.Module(body=node.body, type_ignores=[]), constants)
        units = [body.loose, *body.rows]
        for decorator in node.decorator_list:
            if _is_parametrize(decorator) and len(decorator.args) >= 2:
                for row in _rows(decorator.args[1], constants):
                    found_in_row = _strings(row, constants)
                    units.append(body.loose | found_in_row.loose)
                    units += found_in_row.rows
        for retirement in soon:
            for model_id in sorted(retirement.model_ids):
                if any(_mentions(unit, node.name, retirement.provider, model_id) for unit in units):
                    found.append(PinnedRoute(f"{filename}::{node.name}", retirement.provider,
                                             model_id, retirement.effective_at))
    return found


@pytest.mark.provider_health
def test_no_provider_health_test_pins_a_route_retiring_soon() -> None:
    now = provider_lifecycle._utc_now()
    pinned = [
        route
        for path in sorted(TESTS_DIR.rglob("test_*.py"))
        for route in pinned_routes(path.read_text(), provider_lifecycle.provider_retirements(),
                                   now=now, filename=str(path.relative_to(TESTS_DIR.parent)))
    ]
    assert not pinned, (
        f"provider_health tests name routes that retire within {RETIREMENT_WARNING_DAYS} days; "
        "move them to a live route (or derive it from the catalog) before the cutover:\n"
        + "\n".join(f"  {p.test}: {p.model_id}@{p.provider} retires {p.effective_at.isoformat()}"
                    for p in pinned)
    )


_KIMI_K3 = provider_lifecycle._Retirement(
    provider="tinfoil",
    model_ids=frozenset({"moonshotai/kimi-k3"}),
    upstream_ids=frozenset({"kimi-k3"}),
    effective_at=provider_lifecycle.TINFOIL_KIMI_K3_RETIREMENT_AT,
)

# The test that broke the health run on 2026-11-12, before this guard.
_PINNED_SOURCE = '''
import pytest

@pytest.mark.provider_health
@pytest.mark.parametrize("model_id", ["deepseek/deepseek-v4.1-flash", "moonshotai/kimi-k3"])
def test_tinfoil_still_serves_its_confidential_route(model_id):
    assert f"{model_id}@tinfoil/prepaid" in MODEL_ENDPOINTS
'''


def test_guard_flags_a_hardcoded_route_inside_the_warning_window() -> None:
    before = _KIMI_K3.effective_at - timedelta(days=RETIREMENT_WARNING_DAYS - 1)
    [route] = pinned_routes(_PINNED_SOURCE, [_KIMI_K3], now=before)
    assert (route.provider, route.model_id) == ("tinfoil", "moonshotai/kimi-k3")
    assert route.test.endswith("::test_tinfoil_still_serves_its_confidential_route")


@pytest.mark.parametrize("shape", [
    # A module-level table of (model, provider) rows.
    '''
ROUTES = [("deepseek/deepseek-v4.1-flash", "tinfoil"), ("moonshotai/kimi-k3", "tinfoil")]

@pytest.mark.provider_health
@pytest.mark.parametrize(("model_id", "provider"), ROUTES)
def test_serves(model_id, provider):
    assert model_id
''',
    # A literal endpoint id in the body. It is split here so that
    # test_no_test_names_an_already_retired_endpoint does not read this
    # fixture as a test naming the retired route.
    '''
@pytest.mark.provider_health
def test_serves():
    assert "moonshotai/kimi-k3''' + '''@tinfoil/prepaid" in MODEL_ENDPOINTS
''',
])
def test_guard_follows_tables_and_endpoint_ids(shape: str) -> None:
    before = _KIMI_K3.effective_at - timedelta(days=1)
    assert [r.model_id for r in pinned_routes(shape, [_KIMI_K3], now=before)] == ["moonshotai/kimi-k3"]


def test_guard_ignores_routes_outside_the_window_other_hosts_and_other_tests() -> None:
    long_before = _KIMI_K3.effective_at - timedelta(days=RETIREMENT_WARNING_DAYS + 1)
    after = _KIMI_K3.effective_at
    assert pinned_routes(_PINNED_SOURCE, [_KIMI_K3], now=long_before) == []
    assert pinned_routes(_PINNED_SOURCE, [_KIMI_K3], now=after) == []
    before = _KIMI_K3.effective_at - timedelta(days=1)
    # The same model on another host is not the retiring route.
    other_host = '''
@pytest.mark.provider_health
def test_kimi_k3_is_still_served_across_its_hosts():
    assert {("kimi", "Credits")} <= served("moonshotai/kimi-k3")
'''
    assert pinned_routes(other_host, [_KIMI_K3], now=before) == []
    # Rows pair a model with its host: kimi-k3 here is Kimi's, not Tinfoil's.
    rows = '''
@pytest.mark.provider_health
def test_rows():
    for provider, model_id in (("kimi", "moonshotai/kimi-k3"), ("tinfoil", "z-ai/glm-5.3")):
        assert served(provider, model_id)
'''
    assert pinned_routes(rows, [_KIMI_K3], now=before) == []
    paired = rows.replace('("kimi", "moonshotai/kimi-k3")', '("tinfoil", "moonshotai/kimi-k3")')
    assert [r.model_id for r in pinned_routes(paired, [_KIMI_K3], now=before)] == ["moonshotai/kimi-k3"]
    # A test outside provider_health runs on a pinned clock, not the real one.
    unmarked = _PINNED_SOURCE.replace("@pytest.mark.provider_health\n", "")
    assert pinned_routes(unmarked, [_KIMI_K3], now=before) == []
