"""Fail-closed source inventory and typed, executable GoogleSQL acceptance cases.

The checked-in manifest registers each SQL expression and its enclosing function.
Line numbers are diagnostic only. Dynamic bindings are explicit test scenarios;
SQL is evaluated from the current adapter, never a frozen copy of production SQL.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/trusted_router"
MANIFEST = Path(__file__).with_name("spanner_sql_manifest.json")
SQL_START = re.compile(r"^\s*(SELECT|INSERT(?: OR UPDATE)? INTO|UPDATE|DELETE FROM|WITH)\s", re.I)


def fingerprint(node: ast.AST) -> str:
    """Stable across Python 3.11–3.14's optional AST fields/dump defaults."""
    def normalize(value: Any) -> Any:
        if isinstance(value, ast.AST):
            return {
                "node": type(value).__name__,
                "fields": {name: normalize(field) for name, field in ast.iter_fields(value)
                           if field is not None and field != []},
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)  # bytes/Ellipsis constants in enclosing Python code

    encoded = json.dumps(normalize(node), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class SourceSQL:
    key: str
    module: str
    expression: ast.expr
    scope: ast.AST
    tree: ast.Module
    line: int

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.scope)


def discover(src: Path = SRC) -> dict[str, SourceSQL]:
    found = {}
    for path in sorted(src.glob("storage_gcp*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        candidates: dict[int, ast.expr] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(n.value for n in node.values if isinstance(n, ast.Constant))
            else:
                continue
            if isinstance(parents.get(node), (ast.JoinedStr, ast.Expr)) or not SQL_START.match(text):
                continue
            while isinstance(parents.get(node), (ast.BinOp, ast.JoinedStr)):
                node = parents[node]
            candidates[node.lineno] = node
        counters: dict[str, int] = {}
        for line, node in sorted(candidates.items()):
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            if isinstance(scope, ast.Module):
                scope = parents[node]  # constant assignment, not the whole module
            name = getattr(scope, "name", "constants")
            counters[name] = counters.get(name, 0) + 1
            key = f"{path.stem}:{name}:{counters[name]}"
            found[key] = SourceSQL(key, path.stem, node, scope, tree, line)
    assert found, "no native GoogleSQL expressions discovered"
    return found


def builders(src: Path = SRC) -> dict[str, str]:
    result = {}
    for path in sorted(src.glob("storage_gcp*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and (node.name.endswith(("_statement", "_statements", "_sql"))):
                result[f"{path.stem}:{node.name}"] = fingerprint(node)
    return result


def sql_sinks(src: Path = SRC) -> dict[str, str]:
    """Track dispatch scopes too: new indirect SQL/batch calls cannot hide behind names."""
    result = {}
    methods = {"execute_sql", "execute_update", "execute_partitioned_dml", "batch_update", "execute_batch_dml"}
    for path in sorted(src.glob("storage_gcp*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in methods:
                continue
            ancestors, scope = [], None
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    ancestors.append(node.name)
                    if scope is None:
                        scope = node
            key = path.stem + ":" + ".".join(reversed(ancestors))
            result[key] = fingerprint(scope or tree)
    return result


def evaluate(source: SourceSQL, bindings: dict[str, Any]) -> str:
    module = importlib.import_module("trusted_router." + source.module)
    namespace = dict(vars(module))
    # Bindings use Python expressions only for module-owned column/SQL constants.
    for key, value in bindings.items():
        namespace[key] = eval(value[1:], namespace) if isinstance(value, str) and value.startswith("=") else value  # noqa: S307 - checked-in test scenarios
    sql = eval(compile(ast.Expression(source.expression), source.module, "eval"), namespace)  # noqa: S307 - repository source
    assert isinstance(sql, str) and SQL_START.match(sql), source.key
    return sql


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text())


def assert_complete(src: Path = SRC) -> None:
    actual = discover(src)
    manifest = load_manifest()
    expected = manifest["expressions"]
    assert actual.keys() == expected.keys(), (
        f"Unregistered SQL: {sorted(actual.keys() - expected.keys())}; "
        f"stale SQL registrations: {sorted(expected.keys() - actual.keys())}"
    )
    for key, source in actual.items():
        assert source.fingerprint == expected[key]["fingerprint"], f"SQL scope changed: {key}; review acceptance cases and register it"
        assert expected[key]["scenarios"], f"No executable scenarios for {key}"
    assert sql_sinks(src) == manifest["sinks"], "SQL dispatch inventory changed; review indirect SQL and batch shapes"
    assert builders(src) == manifest["builders"], "SQL builder inventory changed; register and exercise its output"


def parameter_types(source: SourceSQL) -> dict[str, str]:
    """Read literal SDK type dictionaries in the closest scope first.

    Used only when authoring the manifest; CI consumes the reviewed, explicit map.
    Unknown parameters fail generation instead of silently becoming STRING.
    """
    result = {}
    for scope in (source.tree, source.scope):
        for node in ast.walk(scope):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = list(zip(node.keys, node.values, strict=True))
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                pairs = [(node.targets[0].slice, node.value)]
            for key, value in pairs:
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if isinstance(value, ast.Attribute) and value.attr in {"STRING", "INT64", "BOOL", "TIMESTAMP", "FLOAT64", "BYTES", "JSON"}:
                    result[key.value] = value.attr
                elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "Array":
                    result[key.value] = f"ARRAY<{value.args[0].attr}>"
    return result


def typed_parameters(types: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    from google.cloud.spanner_v1 import param_types

    values = {"STRING": "test", "INT64": 1, "BOOL": False,
              "TIMESTAMP": "2026-01-01T00:00:00Z", "FLOAT64": 1.0, "BYTES": b"acceptance"}
    overrides = {"payload": "{}", "body": "{}", "data": "{}", "body_json": "{}", "causes_json": "[]",
                 "kind": "payment", "provider": "stripe", "lifecycle_status": "pending",
                 "debit_status": "debited", "status": "done", "sut": "credits",
                 "phase": "open", "registration_kind": "BOUND", "provisional_id": None}
    params, sdk_types = {}, {}
    for name, kind in types.items():
        if kind.startswith("ARRAY<"):
            element = kind[6:-1]
            params[name] = [values[element]]
            sdk_types[name] = param_types.Array(getattr(param_types, element))
        else:
            params[name] = overrides.get(name, values[kind]) if kind == "STRING" else values[kind]
            sdk_types[name] = getattr(param_types, kind)
    return params, sdk_types
