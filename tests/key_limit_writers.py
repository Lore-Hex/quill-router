"""A structural scanner of every writer of an API key's spend configuration.

What can change a key's limits, their ``tr_key_limit`` mirrors, or whether
the mirror row exists, in this repository:

- an in-process change of an ``ApiKey``'s limit fields: an attribute
  assignment, a ``setattr`` (a literal, an f-string, or a variable in a
  function that spells a limit field), or ``replace(...)`` with one;
- a write to the ``tr_entities`` table (where ``api_key`` bodies live) or to
  ``tr_key_limit``, by any means:
  - a Spanner mutation (``insert``, ``update``, ``insert_or_update``,
    ``replace``, ``delete`` and their ``_map`` forms), its table a literal,
    a constant, a local variable, an attribute such as ``entity_table``, or
    unresolved;
  - a mutation payload: a dict literal whose ``table`` is one of them;
  - SQL DML in any case (``INSERT``, ``INSERT OR UPDATE``, ``UPDATE``,
    ``MERGE``, ``UPSERT``, ``REPLACE``, ``DELETE``) in a string, an
    f-string, a shell script, a SQL file or Go source, and DML on a table an
    f-string fills in;
- a call of an entity writer (a function that holds such a write, or calls
  one, and takes a ``kind`` parameter) whose kind is ``"api_key"`` or not a
  literal, and any write-named call given ``"api_key"``.

Each finding is (style, path, qualified name). tests/test_key_limit_writer_inventory.py
holds every one to a reviewed list.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LIMIT_FIELDS = frozenset(
    {"limit_microdollars", "limit_daily_microdollars", "limit_weekly_microdollars", "limit_monthly_microdollars"}
)
TABLES = {"tr_entities": "entities", "tr_key_limit": "key_limit"}
# Attributes the stores hold their table names in.
TABLE_ATTRIBUTES = {"entity_table": "tr_entities", "ENTITY_TABLE": "tr_entities", "KEY_LIMIT_TABLE": "tr_key_limit"}
MUTATION_VERBS = frozenset(
    {"insert", "update", "insert_or_update", "replace", "delete", "insert_map", "update_map", "replace_map",
     "insert_or_update_map"}
)
MUTATION_KEYWORDS = frozenset({"table", "columns", "values", "keyset"})
WRITE_WORDS = ("write", "insert", "upsert", "update", "replace", "put", "save", "store", "apply")

_DML = (
    r"(?:insert\s+or\s+update(?:\s+into)?|insert\s+(?:or\s+ignore\s+)?into|update|merge(?:\s+into)?|"
    r"upsert(?:\s+into)?|replace\s+into|delete\s+from|delete)"
)
SQL_WRITE = re.compile(_DML + r"\s+(?:@\{[^}]*\}\s*)?(tr_entities|tr_key_limit)\b", re.IGNORECASE)
SQL_DYNAMIC = re.compile(_DML + r"\s+(?:@\{[^}]*\}\s*)?\{\}", re.IGNORECASE)
GO_MUTATION = re.compile(r"spanner\.(?:Insert|Update|InsertOrUpdate|Replace|Delete)\w*\(\s*\"(tr_entities|tr_key_limit)\"")

PY_TREES = ("src/trusted_router", "scripts")
TEXT_TREES = ("src/trusted_router", "scripts", "infra")
TEXT_SUFFIXES = (".sh", ".sql")


def _qualnames(tree: ast.AST) -> tuple[dict[int, str], dict[int, ast.AST], dict[int, str]]:
    """Each node's enclosing qualified name, its innermost function, and its
    innermost class's name."""
    names: dict[int, str] = {}
    scopes: dict[int, ast.AST] = {}
    classes: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                for sub in ast.walk(child):
                    names[id(sub)] = inner
                    if isinstance(child, ast.ClassDef):
                        classes[id(sub)] = child.name
                    else:
                        scopes[id(sub)] = child
                visit(child, inner)
            else:
                visit(child, prefix)

    visit(tree, "")
    return names, scopes, classes


def _module_constants(trees: Iterable[ast.AST]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tree in trees:
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out.setdefault(target.id, node.value.value)
    return out


def _local_constants(scope: ast.AST | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if scope is None:
        return out
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _is_limit_attr_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in LIMIT_FIELDS
    if isinstance(node, ast.JoinedStr):
        text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return text.startswith("limit_") and text.endswith("_microdollars")
    return False


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


class _Module:
    def __init__(self, path: Path, constants: dict[str, str]) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.names, self.scopes, self.classes = _qualnames(self.tree)
        self.constants = constants
        self._locals: dict[int, dict[str, str]] = {}

    def where(self, node: ast.AST) -> str:
        return self.names.get(id(node), "<module>")

    def resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            scope = self.scopes.get(id(node))
            if id(scope) not in self._locals:
                self._locals[id(scope)] = _local_constants(scope)
            return self._locals[id(scope)].get(node.id, self.constants.get(node.id))
        if isinstance(node, ast.Attribute):
            return TABLE_ATTRIBUTES.get(node.attr, self.constants.get(node.attr))
        return None


def _table_writes(m: _Module) -> Iterator[tuple[str, ast.AST]]:
    """The module's writes to tr_entities or tr_key_limit: (style, node).
    A style is mutation.<table> or sql.<table>, with _delete for a delete;
    mutation.unresolved for a mutation whose table is not a literal, a
    constant or a known attribute; sql.dynamic for DML on a table an
    f-string fills in."""
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Call) and _callee(node) in MUTATION_VERBS:
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            table = keywords.get("table", node.args[0] if node.args else None)
            value = m.resolve(table) if table is not None else None
            delete = "_delete" if _callee(node) == "delete" else ""
            if value in TABLES:
                yield "mutation." + TABLES[value] + delete, node
            elif MUTATION_KEYWORDS & keywords.keys() and value is None and "table" in keywords:
                yield "mutation.unresolved", node
        elif isinstance(node, ast.Dict):
            # A mutation payload carries values; a read request carries a
            # key set and is no write.
            keys = {m.resolve(key) for key in node.keys if key is not None}
            for key, value in zip(node.keys, node.values, strict=True):
                table = m.resolve(value) if key is not None and m.resolve(key) == "table" else None
                if table in TABLES and "values" in keys:
                    yield "mutation." + TABLES[table], node
        text = _literal_text(node)
        if text is not None:
            for match in SQL_WRITE.finditer(text):
                delete = "_delete" if match.group(0).lower().startswith("delete") else ""
                yield "sql." + TABLES[match.group(1).lower()] + delete, node
            if isinstance(node, ast.JoinedStr) and SQL_DYNAMIC.search(text):
                yield "sql.dynamic", node


Function = ast.FunctionDef | ast.AsyncFunctionDef


def _kind_position(fn: Function) -> int | None:
    params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    if "kind" in params:
        return params.index("kind")
    if any(a.arg == "kind" for a in fn.args.kwonlyargs):
        return -1
    return None


def _kind_arguments(call: ast.Call, positions: set[int]) -> list[ast.AST]:
    out = [kw.value for kw in call.keywords if kw.arg == "kind"]
    out.extend(call.args[p] for p in positions if 0 <= p < len(call.args))
    return out


# Method names an I/O object also has (a file's write, a dict's update): an
# attribute call of one matches an entity writer only through self or cls,
# in the writer's own class.
AMBIGUOUS_METHODS = frozenset({"write", "read", "get", "update", "delete", "insert", "add", "put", "set", "run",
                               "execute", "append", "extend", "replace", "close", "flush"})

Writer = tuple[str, str | None]


def _call_targets(m: _Module, call: ast.Call, writers: dict[Writer, set[int]]) -> list[Writer]:
    """The entity writers a call may reach: a bare name a plain function, an
    attribute through self or cls a method of the caller's class, any other
    attribute any writer method of that name but an ambiguous one."""
    name = _callee(call)
    if not isinstance(call.func, ast.Attribute):
        return [(name, None)] if (name, None) in writers else []
    receiver = call.func.value
    if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
        own = (name, m.classes.get(id(call)))
        return [own] if own in writers else []
    if name in AMBIGUOUS_METHODS:
        return []
    return [w for w in writers if w[0] == name and w[1] is not None]


def _entity_writers(modules: list[_Module]) -> dict[Writer, set[int]]:
    """(function name, its class or None) -> the positions its kind
    parameter takes, for every function that takes a kind parameter and
    writes tr_entities itself (a delete aside), or passes its own kind on to
    such a function; to a fixed point."""
    functions: list[tuple[_Module, Function, Writer, list[ast.Call]]] = []
    for m in modules:
        for fn in ast.walk(m.tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and m.scopes.get(id(c)) is fn]
                cls = m.classes.get(id(fn.body[0])) if fn.body else None
                in_class = cls is not None and m.classes.get(id(fn)) == cls
                functions.append((m, fn, (fn.name, cls if in_class else None), calls))
    writing = {
        id(m.scopes.get(id(node))) for m in modules for style, node in _table_writes(m)
        if style in ("mutation.entities", "sql.entities")
    }
    writers: dict[Writer, set[int]] = {}
    changed = True
    while changed:
        changed = False
        for m, fn, writer, calls in functions:
            position = _kind_position(fn)
            if position is None or position in writers.get(writer, set()):
                continue
            passes_kind = any(
                isinstance(arg, ast.Name) and arg.id == "kind"
                for call in calls
                for target in _call_targets(m, call, writers)
                for arg in _kind_arguments(call, writers[target])
            )
            if id(fn) in writing or passes_kind:
                writers.setdefault(writer, set()).add(position)
                changed = True
    return writers


def _python_findings(modules: list[_Module]) -> Iterator[tuple[str, str, str]]:
    writers = _entity_writers(modules)
    for m in modules:
        relative = m.path.relative_to(ROOT).as_posix() if m.path.is_relative_to(ROOT) else m.path.name
        for style, node in _table_writes(m):
            yield style, relative, m.where(node)
        for node in ast.walk(m.tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(sub, ast.Attribute) and sub.attr in LIMIT_FIELDS
                       for target in targets for sub in ast.walk(target)):
                    yield "assign", relative, m.where(node)
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name == "setattr" and len(node.args) >= 2:
                scope = m.scopes.get(id(node))
                spelled = scope is not None and any(_is_limit_attr_name(sub) for sub in ast.walk(scope))
                if _is_limit_attr_name(node.args[1]) or (not isinstance(node.args[1], ast.Constant) and spelled):
                    yield "setattr", relative, m.where(node)
            if name == "replace" and any(kw.arg in LIMIT_FIELDS for kw in node.keywords):
                yield "replace", relative, m.where(node)
            targets = _call_targets(m, node, writers)
            if targets:
                positions = set().union(*(writers[target] for target in targets))
                scope = m.scopes.get(id(node))
                own_kind = isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and _kind_position(
                    scope) is not None
                for argument in _kind_arguments(node, positions):
                    kind = m.resolve(argument)
                    if kind == "api_key":
                        yield "entity.api_key", relative, m.where(node)
                    elif kind is None and not (own_kind and isinstance(argument, ast.Name)
                                               and argument.id == "kind"):
                        yield "entity.unresolved", relative, m.where(node)
            elif any(word in name.lower() for word in WRITE_WORDS) and any(
                    m.resolve(a) == "api_key" for a in [*node.args, *(kw.value for kw in node.keywords)]):
                yield "entity.api_key", relative, m.where(node)


def _text_findings(path: Path) -> Iterator[tuple[str, str, str]]:
    relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in SQL_WRITE.finditer(text):
        delete = "_delete" if match.group(0).lower().startswith("delete") else ""
        yield "sql." + TABLES[match.group(1).lower()] + delete, relative, "<file>"
    if path.suffix == ".go":
        for match in GO_MUTATION.finditer(text):
            yield "mutation." + TABLES[match.group(1)], relative, "<file>"


def scan_paths(python: Iterable[Path], text: Iterable[Path]) -> set[tuple[str, str, str]]:
    python = list(python)
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in python]
    constants = _module_constants(trees)
    modules = [_Module(p, constants) for p in python]
    found = set(_python_findings(modules))
    for path in text:
        found.update(_text_findings(path))
    return found


def repository_paths() -> tuple[list[Path], list[Path]]:
    python = [p for top in PY_TREES for p in sorted((ROOT / top).rglob("*.py"))]
    text = [p for top in TEXT_TREES for p in sorted((ROOT / top).rglob("*")) if p.suffix in TEXT_SUFFIXES]
    text += [p for p in sorted((ROOT / "fastpath").rglob("*.go")) if not p.name.endswith("_test.go")]
    return python, text


def scan_repository() -> set[tuple[str, str, str]]:
    return scan_paths(*repository_paths())


if __name__ == "__main__":
    for finding in sorted(scan_repository()):
        print(finding)
