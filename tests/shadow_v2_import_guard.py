"""Exact import occurrence accounting for the dormant contract boundary."""
import ast


def validate_live_imports(source: str, permitted: dict[str, list[str]], new: set[str]) -> None:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    seen: dict[str, set[str]] = {}
    lines = source.splitlines(keepends=True)
    spans = []
    forbidden = {"__import__", "eval", "exec", "__builtins__", "importlib", "builtins",
                 "globals", "locals", "vars"}
    for node in ast.walk(tree):
        if permitted and isinstance(node, ast.Name):
            assert node.id not in forbidden
            if node.id == "getattr":
                parent = parents.get(node)
                assert isinstance(parent, ast.Call) and parent.func is node, "indirect getattr alias"
        if permitted and isinstance(node, ast.ImportFrom) and node.module == "sys":
            assert not any(n.name in {"modules", "*"} for n in node.names)
        if permitted and isinstance(node, ast.Attribute):
            assert node.attr not in forbidden | {"import_module", "modules", "__dict__"}
        if permitted and isinstance(node, ast.ImportFrom) and node.module == "builtins":
            raise AssertionError("indirect builtin acquisition")
        if permitted and isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n in {"builtins", "importlib"} or n.startswith("importlib.") for n in imported)
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
                seen.setdefault(node.module, set()).add(alias.name)
            start = sum(map(len, lines[:node.lineno-1])) + node.col_offset
            end = sum(map(len, lines[:node.end_lineno-1])) + node.end_col_offset
            spans.append((start, end))
    assert {k: sorted(v) for k, v in seen.items()} == permitted
    for start, end in sorted(spans, reverse=True):
        source = source[:start] + " "*(end-start) + source[end:]
    assert not any(name in source for name in new)
