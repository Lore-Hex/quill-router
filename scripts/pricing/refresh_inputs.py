"""List the repository paths that determine the hourly price refresh's output.

The refresh workflow (.github/workflows/refresh-prices.yml) computes a
catalog snapshot from a base commit of main, then waits for the exact-SHA
ci.yml run of that snapshot. When main advances meanwhile, the snapshot may
be carried onto the new main without recomputing it only if no commit main
gained touched anything that could change what the refresh would have
written. This module prints that set, one git pathspec per line, for the
publication step to compare with `git diff --quiet BASE NEW -- <pathspecs>`.

The set is the static import closure of every refresh entry point the
workflow runs before publication (found by reading the workflow's `run`
scripts, so a new refresh step is covered without editing this file), plus
the inputs that are not Python imports: every file the refresh reads or
writes as data, the self-healed parsers and dynamically imported provider
adapters, the dependency lock and the workflow itself.

The closure is computed on the base tree. A commit on main can bring a new
module into the closure only by adding an import to a module already in it,
which is itself a listed path, so the comparison cannot miss a new edge.

Every import statement anywhere in a module counts, including imports inside
functions and `if TYPE_CHECKING:` blocks: over-inclusion only costs a
recomputation, never a stale publication.

Stdlib only. Usage: python -m scripts.pricing.refresh_inputs [--workflow PATH]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = Path(".github/workflows/refresh-prices.yml")
PUBLICATION_STEP = "- name: Commit, verify, and push if changed"

# Python import roots, relative to the repository root: `scripts.*` imports
# from the root, `trusted_router.*` from src/ (pyproject's wheel package).
IMPORT_ROOTS: tuple[str, ...] = ("", "src")

# Inputs that are not static imports. Directory entries end in "/".
#   * scripts/pricing/: provider adapters are imported by name at runtime
#     (refresh.py's importlib.import_module), parsers are rewritten and
#     reloaded by self-heal, and this file and its rules live here.
#   * src/trusted_router/data/: every catalog file the refresh reads,
#     carries forward or rewrites (snapshot, manifests, model changes,
#     routing evidence, lifecycle data).
#   * src/trusted_router/static/: social cards and the logos and fonts
#     scripts/generate_provider_og.py renders them from.
#   * fastpath/testdata/model_alias_vectors_v1.json: a refresh output.
#   * pyproject.toml, uv.lock: the interpreter environment the scripts ran in.
#   * The workflow: the steps, their order and arguments.
STATIC_INPUTS: tuple[str, ...] = (
    "scripts/pricing/",
    "src/trusted_router/data/",
    "src/trusted_router/static/",
    "fastpath/testdata/model_alias_vectors_v1.json",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/refresh-prices.yml",
)

# A repository script named by a string literal in a closure module, such as
# scripts/update_model_changes.py running scripts/model_change_snapshot.py in
# a subprocess, is an entry point too.
SCRIPT_LITERAL = re.compile(r"^(?:scripts|src)/(?:[\w.-]+/)*[\w.-]+\.py$")
MODULE_ENTRY = re.compile(r"\bpython3?\s+-m\s+([A-Za-z_][\w.]*)")
SCRIPT_ENTRY = re.compile(r"\bpython3?\s+((?:[\w.-]+/)*[\w.-]+\.py)\b")


def entry_points(workflow_text: str) -> list[Path]:
    """Files the workflow executes before its publication step."""
    if PUBLICATION_STEP not in workflow_text:
        raise ValueError("refresh workflow has no publication step")
    before = workflow_text[: workflow_text.index(PUBLICATION_STEP)]
    found: list[Path] = []
    for module in MODULE_ENTRY.findall(before):
        path = resolve_module(module)
        if path is None:
            raise ValueError(f"refresh entry module not found: {module}")
        found.append(path)
    for script in SCRIPT_ENTRY.findall(before):
        path = Path(script)
        if not (ROOT / path).is_file():
            raise ValueError(f"refresh entry script not found: {script}")
        found.append(path)
    if not found:
        raise ValueError("refresh workflow runs no Python entry point")
    return sorted(set(found))


def resolve_module(name: str) -> Path | None:
    """Repository-relative file for a dotted module, or None for a non-repo module."""
    parts = name.split(".")
    for root in IMPORT_ROOTS:
        base = ROOT / root if root else ROOT
        candidate = base.joinpath(*parts)
        if (candidate / "__init__.py").is_file():
            return (candidate / "__init__.py").relative_to(ROOT)
        module = candidate.with_suffix(".py")
        if module.is_file():
            return module.relative_to(ROOT)
    return None


def module_name(path: Path) -> str:
    """Dotted module name of a repository-relative Python file."""
    parts = list(path.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def package_inits(name: str) -> Iterable[Path]:
    """Every package __init__ that importing `name` executes."""
    parts = name.split(".")
    for end in range(1, len(parts) + 1):
        path = resolve_module(".".join(parts[:end]))
        if path is not None and path.name == "__init__.py":
            yield path


def script_literals(path: Path) -> set[Path]:
    """Existing repository scripts a module names in a string literal."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=str(path))
    return {
        Path(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and SCRIPT_LITERAL.match(node.value) and (ROOT / node.value).is_file()
    }


def imported_modules(path: Path) -> set[str]:
    """Dotted names every import statement in the file may load."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    package = module_name(path) if is_package else module_name(path).rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".") if package else []
                if node.level > 1:
                    anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            if not base:
                continue
            names.add(base)
            # `from pkg import name` may import the submodule pkg.name.
            names.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def closure(entries: Iterable[Path]) -> set[Path]:
    seen: set[Path] = set()
    pending = list(entries)
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in imported_modules(path):
            resolved = resolve_module(name)
            if resolved is None:
                continue
            pending.extend(dep for dep in (*package_inits(name), resolved) if dep not in seen)
        pending.extend(script for script in script_literals(path) if script not in seen)
    return seen


def covered(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == prefix or (prefix.endswith("/") and path.startswith(prefix))
               for prefix in prefixes)


def refresh_inputs(workflow: Path = WORKFLOW) -> list[str]:
    """Sorted pathspecs: the static inputs, then closure files they do not cover."""
    text = (ROOT / workflow).read_text(encoding="utf-8")
    modules = sorted(path.as_posix() for path in closure(entry_points(text)))
    extra = [path for path in modules if not covered(path, STATIC_INPUTS)]
    return [*STATIC_INPUTS, *extra]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    args = parser.parse_args(argv)
    sys.stdout.write("".join(f"{path}\n" for path in refresh_inputs(args.workflow)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
