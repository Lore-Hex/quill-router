"""Exact import occurrence accounting for the dormant contract boundary."""
import ast


def validate_live_imports(source: str, permitted: dict[str, list[str]], new: set[str]) -> None:
    tree = ast.parse(source)
    seen: dict[str, set[str]] = {}
    lines = source.splitlines(keepends=True)
    spans = []
    for node in ast.walk(tree):
        if permitted and isinstance(node, ast.Name):
            assert node.id not in {"__import__", "eval", "exec"}
        if permitted and isinstance(node, ast.Attribute):
            assert node.attr not in {"import_module", "__import__", "eval", "exec"}
        if permitted and isinstance(node, ast.ImportFrom) and node.module == "builtins":
            assert not any(n.name in {"__import__", "eval", "exec"} for n in node.names)
        if permitted and isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n == "importlib" or n.startswith("importlib.") for n in imported)
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
