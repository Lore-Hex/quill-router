"""Lexical guard, not a general Python loader resolver.

Static imports of v2 modules are allowlisted by exact file and symbol. Named
loader primitives (importlib and submodules, pkgutil, runpy, zipimport, imp,
builtins.__import__, __import__, eval, exec, compile and sys.modules access)
are forbidden outright in guarded runtime files, including aliases. This does
not prove absence of code that computes a loader through other unnamed means.
The authoritative backstop is the self-relative runtime dormancy differential:
it compares outputs with the package importable and blocked, with policy empty.
"""
import ast


def validate_live_imports(source: str, permitted: dict[str, list[str]], new: set[str], *, guarded: bool = False) -> None:
    guarded = guarded or bool(permitted)
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    seen: dict[str, set[str]] = {}
    lines = source.splitlines(keepends=True)
    spans = []
    forbidden = {"__import__", "eval", "exec", "__builtins__", "importlib", "builtins",
                 "globals", "locals", "vars", "compile", "pkgutil", "runpy", "zipimport", "imp"}
    for node in ast.walk(tree):
        if guarded and isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in forbidden | {"modules", "import_module"}
        if guarded and isinstance(node, ast.Name):
            assert node.id not in forbidden
        if guarded and isinstance(node, ast.Attribute):
            assert node.attr not in forbidden | {"modules", "__dict__"}
        if guarded and isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            assert node.slice.value not in forbidden | {"modules", "import_module"}
        if permitted and isinstance(node, ast.Name):
            assert node.id not in forbidden
            if node.id == "getattr":
                parent = parents.get(node)
                assert isinstance(parent, ast.Call) and parent.func is node, "indirect getattr alias"
        if guarded and isinstance(node, ast.ImportFrom) and node.module == "sys":
            assert not any(n.name in {"modules", "*"} for n in node.names)
        if permitted and isinstance(node, ast.Attribute):
            assert node.attr not in forbidden | {"import_module", "modules", "__dict__"}
        if guarded and isinstance(node, ast.ImportFrom) and node.module == "builtins":
            raise AssertionError("indirect builtin acquisition")
        if guarded and isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n.split(".")[0] in {"builtins", "importlib", "pkgutil", "runpy", "zipimport", "imp"} for n in imported)
        if permitted and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            assert len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
            assert isinstance(node.args[1].value, str)
            assert node.args[1].value not in forbidden | {"import_module", "modules", "__dict__"}
        if permitted and isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            assert node.slice.value not in forbidden | {"modules", "import_module"}
        if isinstance(node, ast.ImportFrom) and node.module and node.module.rsplit(".", 1)[-1] in new:
            assert node.level == 0 and node.module in permitted
            for alias in node.names:
                assert alias.asname is None and alias.name in permitted[node.module]
                assert alias.name not in seen.setdefault(node.module, set()), "duplicate import occurrence"
                seen[node.module].add(alias.name)
            start = sum(map(len, lines[:node.lineno-1])) + node.col_offset
            end = sum(map(len, lines[:node.end_lineno-1])) + node.end_col_offset
            spans.append((start, end))
    assert {k: sorted(v) for k, v in seen.items()} == permitted
    for start, end in sorted(spans, reverse=True):
        source = source[:start] + " "*(end-start) + source[end:]
    assert not any(name in source for name in new)
