"""The per-test footprint map and the test selection it drives (shadow mode).

Stdlib only, like commit_scope.py, so a job can run it before `uv sync`.

  needed    the "Test map" workflow's first job: record=true unless the newest
            valid map's commit and main's head agree on everything the map is
            built from (MAP_INPUTS).
  compact   one shard's raw recorder output (scripts/ci/testmap_recorder.py,
            one JSONL file per process) -> one gzipped fragment: interned keys,
            identical footprints shared, each test's scoped fixtures resolved.
  merge     the eight fragments of a run whose shards all passed -> the map,
            stamped with the commit, event, recorder and schema versions and
            the shard count, plus the functions and Python files that existed
            at that commit.
  shadow    on a pull request (ci.yml's coverage job): find the newest valid
            map on the base's first-parent history, select the tests the
            diff can reach, write the selection (gzipped JSON) and a job
            summary. Report only: nothing reads the selection to decide what
            runs.
  compare   after the test shards: this run's failed tests against the shadow
            selection, "failures outside selection: N (IDs)".
  heavy     the map workflow, after merging a map: every repository file a
            test of commit_scope.HEAVY_TESTS ran or opened in it must be a
            change that runs the heavy lane (run_heavy); exit 1 naming each
            one that is not. --write also records the paths in
            HEAVY_SNAPSHOT, which the default suite holds the scope to.

What a footprint holds (the recorder's docstring has how it is recorded):
  f  functions that started or resumed ("path:qualname"), and calls of a
     functools cache ("path:qualname@memo": a hit runs no Python code)
  o  repository files opened for reading (an import is not a read)
  d  repository directories listed
  s  commands started ("argv0 argv1 ... @cwd=<repo-relative or ->")
A test's footprint is its own window (setup, call and teardown), its module's
collection and the setup of every module/class/package/session fixture it
requests. `cached` holds what each memoized function's misses ran (every
miss), and what every call of a function that keeps a cache by hand ran (a
module global it assigns, a private global or attribute named like a cache:
the recorder's docstring lists them and what is not covered).
`incomplete_memo` lists any function whose misses were not all recorded (past
the recorder's safety bound, or a cache tracked only after its first use);
`unattributed_memo` any cache that served a hit no window recorded. `global`
is what ran outside every window: imports, session hooks, collection hooks,
and misses at import time.

Selection rules (the full suite runs on any fail-safe; reasons are reported).
The changes are the pull request's (base..head) and, as drift, those between
the map's commit and the base: the map is up to hours old, and a call edge
added since can only come from a changed function body, so a function
changed since the map selects what ran it like one the pull request changed.

  Fail-safes (run everything):
   F1 a diff record that is not a plain add/modify/delete of a regular file;
      an empty diff; a change (pull request or drift) to what decides CI
      (commit_scope's FULL_MATRIX_PATHS, any .gitattributes) or to the
      Python toolchain; the pull request labeled ci:full.
   F2 no map; a map that is not this repository's own "Test map" run
      (MAP_WORKFLOW, a MAP_EVENTS event on main, head repository ours, green)
      at a commit on the base's first-parent history (git rev-list, not
      dates) within MAX_DISTANCE commits, uploaded within MAX_AGE_HOURS by
      the server's clock; a map that is unreadable, of another schema or
      recorder version, or not from all of its shards; a map whose commit
      is not an ancestor of the base; a map with any `unattributed_memo`
      (nothing names the tests that cache served; checked in select).
   F3 a file the pull request changed that is read outside every window (at
      import or session time), or a changed function (pull request or
      drift) that ran outside every window (at import: main.py builds its
      app at import, so create_app's callees are there). Data that changed
      only as drift does not count: the hourly price refresh rewrites catalog
      data read at import, which would run everything on every pull request;
      the refresh's commit ran the full suite itself, and the next map is at
      most hours away. A known gap.
   F4 a changed module-level statement that is not an import, a docstring,
      an assignment, a class body or a decorated definition (a call, an
      `if`, a `try` at module level); a .py file the diff cannot parse.
   F5 a changed function, or a changed Python file, the map cannot know: it
      exists at the base but neither at the map's commit nor as an addition
      among the drift changes.
   F6 the selection reaches more than FULL_SHARE of the suite's weight.
   F7 a change to any conftest.py, at any depth (pull request or drift).

  Selection (union):
   S1 a changed test file runs whole (a deleted one runs nothing).
   S2 a changed line selects the tests that ran the innermost def that
      encloses it and existed on the old side; a new nested def changes its
      enclosing def.
   S3 a class change (a new method, a class-level statement) selects what
      names the class and every method of it that ran.
   S4 a changed module-level assignment, class or decorated definition
      selects the tests that ran a function naming what it defines, followed
      through module-level assignments, class bodies and re-exports that
      name it in turn. Import lines and docstrings select nothing on their
      own (every run imports the app; an import error fails any test).
   S5 a deleted Python file selects the tests that ran any of its code.
   S6 an added file selects nothing on its own (what imports it changed
      too), except through a listing of its directory.
   S7 a changed file selects the tests that opened it; an added or deleted
      one the tests that listed its directory.
   S8 a test that started a command is selected by any change, unless the
      command is git outside the repository or one of NARROW_COMMANDS: what
      python, uv run, env, bash -c or a script reads is unknown.
   S9 a changed test helper (Python under tests/ or scripts/) also selects
      what commit_scope's reverse import walk reaches.
   S10 a memoized function whose misses a change reaches selects its users:
      the tests in whose window it missed, and those the recorder marked as
      calling it or served by its cache (a hit runs no Python code: the
      recorder marks a call, and a rise in the cache's hit count while the
      window was open). A function that keeps a cache by hand selects the
      tests that called it when a change reaches what any of its calls ran.
      A memoized function in `incomplete_memo` selects its users whatever
      changed. Module and session fixture setups count for
      every test that requests them; a module's collection counts for each of
      its tests.
   S11 a test the map does not know runs (compare counts it as selected).
"""

from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:  # run as a script (scope job) or imported as scripts.ci.testmap (tests)
    from scripts.ci import commit_scope as cs
except ImportError:  # pragma: no cover - the script path
    import commit_scope as cs  # type: ignore[no-redef]

SCHEMA_VERSION = 1
# The recorder writes this into every file it produces; a map from another
# recorder version is not used (F2). Bump it with any change to what the
# recorder records or how.
RECORDER_VERSION = "3"
SHARDS = 8
MAX_DISTANCE = 40
MAX_AGE_HOURS = 48
# The map comes from this workflow's own runs on main, nothing else (F2).
MAP_WORKFLOW = ".github/workflows/testmap.yml"
MAP_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
# What the map is built from: the map workflow records again when one changed.
MAP_INPUTS = ("src", "tests", "scripts/ci", "pyproject.toml", "uv.lock", ".python-version", "conftest.py",
              ":(glob)**/conftest.py")
FULL_SHARE = 0.7
ARTIFACT = "testmap"
MEMO = "@memo"  # the recorder's mark for a call of a functools cache
# Directories the name index and the map's universe skip: not repository code.
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".hypothesis", ".ruff_cache",
                       ".mypy_cache", ".pytest_cache"})
# Commands that read no repository file whatever their arguments (S8).
NARROW_COMMANDS = frozenset({"id", "true", "false", "echo", "printf", "sleep", "uname", "whoami", "date"})
MODULE_KINDS_SELECTING = frozenset({"assign", "class", "decorator"})
MODULE_KINDS_INERT = frozenset({"import", "docstring"})


# ---------------------------------------------------------------- footprints


class Interner:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.index: dict[str, int] = {}
        self.fps: list[list[list[int]]] = []
        self.fp_index: dict[str, int] = {}

    def key(self, text: str) -> int:
        found = self.index.get(text)
        if found is None:
            found = self.index[text] = len(self.keys)
            self.keys.append(text)
        return found

    def fp(self, parts: Iterable[Iterable[str]]) -> int:
        encoded = [sorted({self.key(x) for x in part}) for part in parts]
        token = json.dumps(encoded, separators=(",", ":"))
        found = self.fp_index.get(token)
        if found is None:
            found = self.fp_index[token] = len(self.fps)
            self.fps.append(encoded)
        return found


PARTS = ("f", "o", "d", "s")


def _empty() -> dict[str, set[str]]:
    return {k: set() for k in PARTS}


def _union(into: dict[str, set[str]], record: dict) -> None:
    for k in PARTS:
        into[k].update(record.get(k, ()))


def _instance(scope: str, baseid: str, arg: str, nodeid: str) -> str:
    module = nodeid.split("::")[0]
    anchor = {"session": "", "module": module, "class": "::".join(nodeid.split("::")[:2])}.get(scope, "*")
    return f"{scope}|{anchor}|{baseid}|{arg}"


def compact(raw_dir: Path, shard: int) -> dict:
    """One shard's recorder output as a fragment."""
    tests: dict[str, dict] = {}
    fixtures: dict[str, dict[str, set[str]]] = defaultdict(_empty)
    collect: dict[str, dict[str, set[str]]] = defaultdict(_empty)
    cached: dict[str, dict[str, set[str]]] = defaultdict(_empty)
    incomplete: set[str] = set()
    unattributed: set[str] = set()
    glob_fp = _empty()
    errors: list[str] = []
    headers: list[dict] = []
    files = sorted(raw_dir.glob("*.jsonl"))
    if not files:
        errors.append(f"no recorder output in {raw_dir}")
    for path in files:
        ended = False
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"{path.name}: a truncated record")
                    continue
                if "h" in record:
                    headers.append(record["h"])
                elif "end" in record:
                    ended = True
                elif "err" in record:
                    errors.append(f"{path.name}: {record['err']}")
                elif "t" in record:
                    entry = tests.setdefault(record["t"], {**_empty(), "x": set()})
                    _union(entry, record)
                    entry["x"].update(record.get("x", ()))
                elif "fx" in record:
                    scope, anchor, baseid, arg = record["fx"].split("|", 3)
                    _union(fixtures[record["fx"]], record)
                    _union(fixtures[f"{scope}|*|{baseid}|{arg}"], record)
                elif "c" in record:
                    _union(collect[record["c"]], record)
                elif "cf" in record:
                    _union(cached[record["cf"]], record)
                elif "mi" in record:
                    incomplete.add(record["mi"])
                elif "um" in record:
                    unattributed.add(record["um"])
                elif "g" in record:
                    _union(glob_fp, record)
        if not ended:
            errors.append(f"{path.name}: no end record (the process stopped before its session ended)")
    versions = {h.get("recorder") for h in headers}
    if headers and versions != {RECORDER_VERSION}:
        errors.append(f"recorder versions {sorted(map(str, versions))}, expected {RECORDER_VERSION}")
    if not headers:
        errors.append("no recorder header")
    if not tests:
        errors.append("no test was recorded")
    interner = Interner()
    out_tests = []
    for nodeid in sorted(tests):
        entry = tests[nodeid]
        fx_ids = set()
        for ref in sorted(entry["x"]):
            scope, baseid, arg = ref.split("|", 2)
            source = fixtures.get(_instance(scope, baseid, arg, nodeid)) or fixtures.get(f"{scope}|*|{baseid}|{arg}")
            if source is not None:
                fx_ids.add(interner.fp(source[k] for k in PARTS))
        out_tests.append([nodeid, interner.fp(entry[k] for k in PARTS), sorted(fx_ids)])
    return {
        "schema": SCHEMA_VERSION, "recorder": RECORDER_VERSION, "shard": shard,
        "valid": not errors, "errors": errors[:50],
        "keys": interner.keys, "fps": interner.fps, "tests": out_tests,
        "collect": {m: interner.fp(v[k] for k in PARTS) for m, v in sorted(collect.items())},
        "cached": {c: interner.fp(v[k] for k in PARTS) for c, v in sorted(cached.items())},
        "incomplete_memo": sorted(incomplete),
        "unattributed_memo": sorted(unattributed),
        "global": interner.fp(glob_fp[k] for k in PARTS),
    }


def merge(fragments: list[dict], *, shards: int, commit: str, ref: str, event: str, run_id: str,
          root: Path) -> dict:
    """Fragments of every shard of one green push run -> the map. Raises when
    a shard is missing, repeated or invalid."""
    seen = sorted(f.get("shard") for f in fragments)
    if seen != list(range(1, shards + 1)):
        raise ValueError(f"fragments for shards {seen}, expected 1..{shards}")
    for f in fragments:
        if f.get("schema") != SCHEMA_VERSION or f.get("recorder") != RECORDER_VERSION:
            raise ValueError(f"shard {f.get('shard')}: schema {f.get('schema')} recorder {f.get('recorder')}")
        if not f.get("valid"):
            raise ValueError(f"shard {f.get('shard')} is invalid: {f.get('errors')}")
    interner = Interner()

    def remap(fragment: dict, index: int) -> int:
        keys = fragment["keys"]
        return interner.fp([keys[i] for i in part] for part in fragment["fps"][index])

    tests: dict[str, list] = {}
    collect: dict[str, set[int]] = defaultdict(set)
    cached: dict[str, set[int]] = defaultdict(set)
    incomplete: set[str] = set()
    unattributed: set[str] = set()
    globals_: set[int] = set()
    for fragment in fragments:
        for nodeid, rt, fx in fragment["tests"]:
            if nodeid in tests:
                raise ValueError(f"{nodeid} recorded by two shards")
            tests[nodeid] = [nodeid, remap(fragment, rt), sorted({remap(fragment, i) for i in fx})]
        for module, i in fragment["collect"].items():
            collect[module].add(remap(fragment, i))
        for key, i in fragment["cached"].items():
            cached[key].add(remap(fragment, i))
        incomplete.update(fragment.get("incomplete_memo", ()))
        unattributed.update(fragment.get("unattributed_memo", ()))
        globals_.add(remap(fragment, fragment["global"]))

    def joined(ids: set[int]) -> int:
        parts: list[set[str]] = [set(), set(), set(), set()]
        for i in ids:
            for n, part in enumerate(interner.fps[i]):
                parts[n].update(interner.keys[k] for k in part)
        return interner.fp(parts)

    index = NameIndex.build(root)
    return {
        "schema": SCHEMA_VERSION, "recorder": RECORDER_VERSION, "commit": commit, "ref": ref,
        "event": event, "run_id": str(run_id), "shards": shards, "fragments": seen,
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": interner.keys, "fps": interner.fps,
        "tests": [tests[t] for t in sorted(tests)],
        "collect": {m: joined(ids) for m, ids in sorted(collect.items())},
        "cached": {c: joined(ids) for c, ids in sorted(cached.items())},
        "incomplete_memo": sorted(incomplete),
        "unattributed_memo": sorted(unattributed),
        "global": joined(globals_),
        "memoized": sorted({k[: -len(MEMO)] for k in interner.keys if k.endswith(MEMO)}),
        "universe_files": sorted(index.universe),
        "universe_funcs": sorted(k for keys in index.universe.values() for k in keys),
    }


def write_gz(obj: dict, path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(obj, handle, separators=(",", ":"))


def read_gz(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------- python changes


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _spans(tree: ast.AST) -> list[tuple[int, int, str, str]]:
    """(first line incl. decorators, last line, qualname, func|class) of every
    def and class, qualnames as code objects spell them."""
    out: list[tuple[int, int, str, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{child.name}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                kind = "class" if isinstance(child, ast.ClassDef) else "func"
                out.append((start, child.end_lineno or child.lineno, q, kind))
                visit(child, q + ("." if kind == "class" else ".<locals>."))
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _toplevel_kinds(tree: ast.Module, lines: Iterable[int]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for ln in lines:
        for node in tree.body:
            start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
            if not start <= ln <= (node.end_lineno or node.lineno):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                out.add(("import", ""))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
                out.update(("assign", n) for n in names)
                if not names:
                    out.add(("other", type(node).__name__))
            elif isinstance(node, ast.ClassDef):
                out.add(("class", node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(("decorator", node.name))
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out.add(("docstring", ""))
            else:
                out.add(("other", type(node).__name__))
    return out


@dataclass
class PyChange:
    """What a diff changes in one Python file, read from both sides."""
    funcs: set[str] = field(default_factory=set)       # existing defs whose code changed
    classes: set[str] = field(default_factory=set)     # existing classes given a new method/statement
    kinds: set[tuple[str, str]] = field(default_factory=set)  # module-level statements
    unparsable: bool = False


def _classify_side(source: str | None, lines: list[int], base_names: set[str], change: PyChange) -> None:
    if source is None or not lines:
        return
    try:
        tree = ast.parse(source)
    except SyntaxError:
        change.unparsable = True
        return
    spans = _spans(tree)
    text = source.split("\n")
    module_lines = []
    decorated: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorated[node.name] = node
    for ln in lines:
        line = text[ln - 1].strip() if 0 < ln <= len(text) else ""
        if not line or line.startswith("#"):
            continue
        chain = sorted((s for s in spans if s[0] <= ln <= s[1]), key=lambda s: s[0])
        if not chain:
            module_lines.append(ln)
            continue
        for _start, _end, q, kind in reversed(chain):
            if q in base_names:
                if kind == "func":
                    change.funcs.add(q)
                elif "<locals>" in q:
                    change.funcs.add(q.rsplit(".<locals>.", 1)[0])
                else:
                    change.classes.add(q)
                break
        else:
            # Everything around the line is new: a new top-level def or class.
            # What it registers with through decorators is a module-level use
            # of those names.
            node = decorated.get(chain[0][2])
            for deco in getattr(node, "decorator_list", []):
                change.kinds.update(("assign", n.id) for n in ast.walk(deco) if isinstance(n, ast.Name))
    change.kinds |= _toplevel_kinds(tree, module_lines)


def classify_python(old: str | None, new: str | None, old_lines: list[int], new_lines: list[int]) -> PyChange:
    """The defs, classes and module-level statements a change touches. A line
    counts for the innermost def or class around it that exists in `old`."""
    change = PyChange()
    base_names: set[str] = set()
    if old is not None:
        try:
            base_names = {s[2] for s in _spans(ast.parse(old))}
        except SyntaxError:
            change.unparsable = True
    _classify_side(old, old_lines, base_names, change)
    _classify_side(new, new_lines, base_names, change)
    return change


def hunk_lines(patch: str) -> tuple[list[int], list[int]]:
    old: list[int] = []
    new: list[int] = []
    for line in patch.split("\n"):
        m = HUNK.match(line)
        if m:
            os_, oc, ns, nc = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            old.extend(range(os_, os_ + oc))
            new.extend(range(ns, ns + nc))
    return old, new


# ---------------------------------------------------------------- name index


def _idents(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def python_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


@dataclass
class NameIndex:
    """Which functions name an identifier, and which module- or class-level
    definitions are built from one, over every Python file of the repository."""
    funcs_by_ident: dict[str, set[str]]
    defined_from: dict[str, set[str]]
    universe: dict[str, list[str]]

    @classmethod
    def build(cls, root: Path) -> NameIndex:
        funcs_by_ident: dict[str, set[str]] = defaultdict(set)
        defined_from: dict[str, set[str]] = defaultdict(set)
        universe: dict[str, list[str]] = {}
        for path in sorted(python_files(root)):
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                universe[rel] = []
                continue
            keys: list[str] = []

            def visit(node: ast.AST, prefix: str, in_func: bool, rel: str = rel, keys: list[str] = keys) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        key = f"{rel}:{prefix}{child.name}"
                        keys.append(key)
                        for ident in _idents(child):
                            funcs_by_ident[ident].add(key)
                        visit(child, f"{prefix}{child.name}.<locals>.", True)
                    elif isinstance(child, ast.ClassDef):
                        if not in_func:
                            for stmt in child.body:
                                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                    for ident in _idents(stmt):
                                        defined_from[ident].add(child.name)
                            for b in [*child.bases, *child.keywords, *child.decorator_list]:
                                for ident in _idents(b):
                                    defined_from[ident].add(child.name)
                        visit(child, f"{prefix}{child.name}.", in_func)
                    elif not in_func and isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                        names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
                        value = getattr(child, "value", None)
                        if value is not None:
                            for ident in _idents(value):
                                defined_from[ident].update(names)
                    elif not in_func and isinstance(child, ast.ImportFrom):
                        for alias in child.names:
                            defined_from[alias.name].add(alias.asname or alias.name)
                    elif not in_func and isinstance(child, (ast.If, ast.Try, ast.With)):
                        visit(child, prefix, in_func)

            visit(tree, "", False)
            universe[rel] = keys
        return cls(dict(funcs_by_ident), dict(defined_from), universe)

    def spread(self, names: Iterable[str]) -> set[str]:
        """Function keys naming any of `names`, or anything defined from them."""
        seen: set[str] = set()
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            pending.extend(self.defined_from.get(name, ()))
        out: set[str] = set()
        for name in seen:
            out |= self.funcs_by_ident.get(name, set())
        return out


# ---------------------------------------------------------------- selection


@dataclass
class Selection:
    full: bool
    reasons: list[str]
    whole_files: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    not_selected: list[str] = field(default_factory=list)
    share: float = 1.0
    map_commit: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"schema": SCHEMA_VERSION, "full": self.full, "reasons": self.reasons, "share": round(self.share, 4),
                "map_commit": self.map_commit, "whole_files": self.whole_files, "selected": self.selected,
                "not_selected": self.not_selected, "notes": self.notes}


XDIST_GROUP = re.compile(r"@[A-Za-z0-9_.:-]+$")


def membership(selection: dict) -> Callable[[str], bool]:
    """Would the selection run a test? A test the map does not know runs (S11).
    xdist's loadgroup appends "@<group>" to a grouped test's ID in a worker,
    so the recorder, .test_durations and the shard logs may or may not carry
    it: IDs are compared without it."""
    if selection.get("full"):
        return lambda test_id: True
    whole = set(selection.get("whole_files", ()))
    selected = {XDIST_GROUP.sub("", t) for t in selection.get("selected", ())}
    not_selected = {XDIST_GROUP.sub("", t) for t in selection.get("not_selected", ())}

    def runs(test_id: str) -> bool:
        test_id = XDIST_GROUP.sub("", test_id)
        if test_id.split("::")[0] in whole or test_id in selected:
            return True
        return test_id not in not_selected

    return runs


def contains(selection: dict, test_id: str) -> bool:
    return membership(selection)(test_id)


class TestMap:
    __test__ = False  # not a pytest class

    def __init__(self, data: dict) -> None:
        self.data = data
        self.keys: list[str] = data["keys"]
        self.key_id = {k: i for i, k in enumerate(self.keys)}
        self.fps: list[list[list[int]]] = data["fps"]
        self.tests: list[list] = data["tests"]
        self.collect: dict[str, int] = data["collect"]
        self.memoized: list[str] = data.get("memoized", [])
        self.cached: dict[str, int] = data["cached"]
        self.glob = self.fps[data["global"]]
        self.universe_files = set(data.get("universe_files", ()))
        self.universe_funcs = set(data.get("universe_funcs", ()))
        self.known_funcs = {self.keys[i] for fp in self.fps for i in fp[0]}

    def ids(self, texts: Iterable[str]) -> set[int]:
        return {self.key_id[t] for t in texts if t in self.key_id}

    def global_funcs(self) -> set[str]:
        return {self.keys[i] for i in self.glob[0]}

    def global_opens(self) -> set[str]:
        return {self.keys[i] for i in self.glob[1]}


def validate_map(data: dict, *, commit: str | None = None) -> str | None:
    """Why the map cannot be used, or None."""
    if data.get("schema") != SCHEMA_VERSION:
        return f"map schema {data.get('schema')} is not {SCHEMA_VERSION}"
    if data.get("recorder") != RECORDER_VERSION:
        return f"map recorder {data.get('recorder')} is not {RECORDER_VERSION}"
    if data.get("event") not in MAP_EVENTS or data.get("ref") != "refs/heads/main":
        return f"map is from a {data.get('event')} run of {data.get('ref')}, not main's"
    if data.get("fragments") != list(range(1, int(data.get("shards") or 0) + 1)) or data.get("shards") != SHARDS:
        return f"map has fragments {data.get('fragments')} of {data.get('shards')} shards, expected {SHARDS}"
    if commit is not None and data.get("commit") != commit:
        return f"map commit {data.get('commit')} is not {commit}"
    for name in ("keys", "fps", "tests", "collect", "cached", "memoized", "incomplete_memo", "unattributed_memo",
                 "global"):
        if name not in data:
            return f"map has no {name}"
    return None


def test_weights(root: Path, test_ids: Iterable[str]) -> dict[str, float]:
    """Each test's .test_durations weight; a test without one takes its
    file's mean, or the suite's median."""
    try:
        durations = json.loads((root / ".test_durations").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        durations = {}
    ids = list(test_ids)
    known = [durations[t] for t in ids if t in durations]
    median = sorted(known)[len(known) // 2] if known else 1.0
    by_file: dict[str, list[float]] = defaultdict(list)
    for t in ids:
        if t in durations:
            by_file[t.split("::")[0]].append(durations[t])
    out = {}
    for t in ids:
        if t in durations:
            out[t] = float(durations[t])
        else:
            same = by_file.get(t.split("::")[0])
            out[t] = sum(same) / len(same) if same else float(median)
    return out


def _split_command(cmd: str) -> tuple[list[str], str]:
    """The recorder writes a command as its first argv words, then
    ` @cwd=<repo-relative directory, or - outside the repository>`."""
    body, sep, cwd = cmd.rpartition(" @cwd=")
    if not sep:
        return cmd.split(), "-"
    return body.split(), cwd


def command_is_narrow(cmd: str) -> bool:
    """Can the selector tell what a recorded command reads? Only git outside
    the repository (a test's scratch repository) and a few utilities that read
    no files. Anything else (python, uv run, env, bash -c, a script that
    starts Python) may read any file: the test is selected by every change."""
    toks, cwd = _split_command(cmd)
    if not toks:
        return False
    exe = toks[0].rsplit("/", 1)[-1]
    if exe in NARROW_COMMANDS:
        return True
    if exe == "git":
        # A repository path among its words (--work-tree=src: an option's
        # value counts) means it reads the repository.
        values = [tok.split("=", 1)[-1] for tok in toks[1:]]
        return cwd == "-" and not any(v == "." or "/" in v and not v.startswith("<") for v in values)
    return False


def helper_reach(root: Path, helpers: list[str]) -> set[str]:
    """Test files that import a changed helper, directly or through other
    helpers (not through conftest.py: a change to any conftest.py runs the
    full suite), or name its file."""
    if not helpers:
        return set()
    tests = cs.test_files(root)
    edges = cs.import_edges(root, tests)
    dependents: dict[str, set[str]] = defaultdict(set)
    for path, targets in edges.items():
        for target in targets:
            dependents[target].add(path)
    found: set[str] = set()
    sources: dict[str, str] = {}
    for helper in helpers:
        reached, pending = {helper}, [helper]
        while pending:
            for dependent in dependents.get(pending.pop(), ()):
                if dependent not in reached and not dependent.endswith("/conftest.py"):
                    reached.add(dependent)
                    pending.append(dependent)
        found |= {t for t in tests if t in reached}
        name = helper.rsplit("/", 1)[-1]
        if name not in cs.GENERIC_NAMES:
            for test in tests:
                if test not in found:
                    if test not in sources:
                        sources[test] = (root / test).read_text(encoding="utf-8", errors="replace")
                    if name in sources[test]:
                        found.add(test)
    return found


@dataclass
class Drift:
    """What changed between the map's commit and the base (an ancestor of
    the base), and the defs and files that exist at the base but not at the
    map's commit."""
    changes: list[cs.Change]
    classify: Callable[[cs.Change], PyChange]
    added_files: set[str] = field(default_factory=set)
    added_keys: set[str] = field(default_factory=set)


def select(changes: list[cs.Change], tmap: TestMap, *, root: Path, classify: Callable[[cs.Change], PyChange],
           index: Callable[[], NameIndex], weights: dict[str, float], drift: Drift | None = None) -> Selection:
    """Rules S1-S10 and fail-safes F1, F3-F7 over the pull request's changes
    and, as drift, the changes between the map's commit and the base (F2, the
    map's validity, and the ci:full label are the caller's, except a map's
    `unattributed_memo`; S11 is `membership`)."""
    commit = str(tmap.data.get("commit", ""))
    reasons: list[str] = []
    drift = drift or Drift([], classify)

    def full(reason: str) -> Selection:
        return Selection(True, [reason, *reasons], map_commit=commit)

    if not changes:
        return full("the comparison is empty")
    unattributed = tmap.data.get("unattributed_memo") or []
    if unattributed:  # F2
        return full(f"the map has {len(unattributed)} memoized functions whose cache served tests it cannot name"
                    f" ({', '.join(unattributed[:3])})")
    every = [(change, False) for change in changes] + [(change, True) for change in drift.changes]
    for change, drifted in every:  # F1, F7
        path = change.path
        since = " (changed since the map)" if drifted else ""
        if cs.matches_any(path, cs.FULL_MATRIX_PATHS) or path.rsplit("/", 1)[-1] in cs.FULL_MATRIX_NAMES:
            return full(f"{path} decides what CI runs{since}")
        if path in cs.PYTHON_TOOLCHAIN_PATHS:
            return full(f"{path} is the Python toolchain{since}")
        if path.rsplit("/", 1)[-1] == "conftest.py":
            return full(f"{path} is a conftest.py{since}")
    if drift.changes:
        reasons.append(f"{len(drift.changes)} files changed between the map ({commit[:12]}) and the base count too")

    global_funcs = tmap.global_funcs()
    global_opens = tmap.global_opens()
    whole: set[str] = set()
    keys: set[str] = set()      # S2-S4: function keys
    files: set[str] = set()     # S5: every function of these files
    opened: set[str] = set()    # S7
    listed: set[str] = set()    # S6, S7
    helpers: list[str] = []     # S9
    names: NameIndex | None = None

    def name_index() -> NameIndex:
        nonlocal names
        if names is None:
            names = index()
        return names

    for change, drifted in every:
        path, status = change.path, change.status
        since = " (changed since the map)" if drifted else ""
        if status in ("A", "D"):
            listed.add(path.rsplit("/", 1)[0] if "/" in path else ".")
        # F3 for data: a file read at import or session time. Not for drift:
        # the hourly price refresh rewrites catalog data read at import, so
        # every pull request would run everything; the refresh's own commit
        # ran the full suite, and the next map is at most hours away.
        if path in global_opens and not drifted:
            return full(f"{path} is read outside every test (at import or session time)")
        opened.add(path)
        if not path.endswith(".py"):
            continue
        if cs.is_test_file(path):  # S1
            if status != "D":
                whole.add(path)
                reasons.append(f"{path}: changed test file runs whole{since}")
            continue
        if status == "A":  # S6
            continue
        if path.startswith(("tests/", "scripts/")):
            helpers.append(path)
        if path not in tmap.universe_files and path not in drift.added_files:  # F5
            return full(f"{path} is newer than the map ({commit[:12]})")
        if status == "D":  # S5
            files.add(path)
            continue
        info = (drift.classify if drifted else classify)(change)
        if info.unparsable:  # F4
            return full(f"{path} does not parse{since}")
        for kind, name in sorted(info.kinds):  # F4
            if kind not in MODULE_KINDS_SELECTING | MODULE_KINDS_INERT:
                return full(f"{path}: a module-level {name or kind} statement changed{since}")
        for q in sorted(info.funcs):  # S2
            key = f"{path}:{q}"
            if key not in tmap.universe_funcs:
                if key in drift.added_keys or path in drift.added_files:
                    continue  # added since the map: its callers are drift changes
                return full(f"{key} is newer than the map ({commit[:12]})")  # F5
            if key in global_funcs:  # F3
                return full(f"{key} runs at import or session time{since}")
            keys |= {key, key + MEMO}
        for cls in sorted(info.classes):  # S3
            keys |= name_index().spread({cls})
            prefix = f"{path}:{cls}."
            keys |= {k for k in tmap.known_funcs if k.startswith(prefix)}
            reasons.append(f"{path}: class {cls} changed{since}")
        spread = {name for kind, name in info.kinds if kind in MODULE_KINDS_SELECTING and name}
        if spread:  # S4
            found = name_index().spread(spread)
            keys |= found
            reasons.append(f"{path}: module-level {', '.join(sorted(spread)[:4])} named by {len(found)} functions{since}")
    static_reach = helper_reach(root, helpers)  # S9
    if static_reach:
        reasons.append(f"{len(static_reach)} test files import or name a changed helper")

    # A selected function selects the tests its cache served too: every key
    # has its "@memo" twin (S2 adds it itself; S3 and S4 reach keys through
    # the name index, which knows only plain keys).
    keys |= {key + MEMO for key in keys if not key.endswith(MEMO)}
    key_ids = tmap.ids(keys)
    for path in files:
        prefix = f"{path}:"
        key_ids |= {i for i, k in enumerate(tmap.keys) if k.startswith(prefix)}
    # S10: a memoized function whose misses were not all recorded (more than
    # the recorder's cap): what called it runs whatever changed.
    incomplete = list(tmap.data.get("incomplete_memo", []))
    if incomplete:
        key_ids |= tmap.ids([*incomplete, *(k + MEMO for k in incomplete)])
        reasons.append(f"{len(incomplete)} memoized functions have misses the map did not record: their users run")
    open_ids = tmap.ids(opened)
    dir_ids = tmap.ids(listed)
    narrow: dict[int, bool] = {}

    def spawn_hit(command_ids: list[int]) -> bool:  # S8: what a command reads is unknown
        for cid in command_ids:
            if cid not in narrow:
                narrow[cid] = command_is_narrow(tmap.keys[cid])
            if not narrow[cid]:
                return True
        return False

    fp_hit: dict[int, bool] = {}

    def is_hit(i: int) -> bool:
        found = fp_hit.get(i)
        if found is None:
            f, o, d, s = tmap.fps[i]
            found = (not key_ids.isdisjoint(f) or not open_ids.isdisjoint(o)
                     or not dir_ids.isdisjoint(d) or (bool(s) and spawn_hit(s)))
            fp_hit[i] = found
        return found

    # S10: a memoized function's users (a miss in their window, or a call or
    # hit the recorder marked) inherit what its misses ran.
    while True:
        new: set[int] = set()
        for name, i in tmap.cached.items():
            twins = tmap.ids({name, name + MEMO})
            if not twins <= key_ids and is_hit(i):
                new |= twins
        if not new - key_ids:
            break
        key_ids |= new
        fp_hit.clear()
    selected: list[str] = []
    not_selected: list[str] = []
    for nodeid, rt, fx in tmap.tests:
        module = nodeid.split("::")[0]
        hit = (module in whole or module in static_reach or is_hit(rt)
               or (module in tmap.collect and is_hit(tmap.collect[module])) or any(is_hit(i) for i in fx))
        (selected if hit else not_selected).append(nodeid)
    total = sum(weights.get(t[0], 0.0) for t in tmap.tests) or 1.0
    share = sum(weights.get(t, 0.0) for t in selected) / total
    if share > FULL_SHARE:  # F6
        return full(f"the selection reaches {share:.0%} of the suite's weight")
    return Selection(False, reasons, whole_files=sorted(whole | static_reach), selected=selected,
                     not_selected=not_selected, share=share, map_commit=commit)



# ---------------------------------------------------------------- git and gh


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout  # noqa: S603,S607 -- git and gh, fixed argv


def git_ok(root: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).returncode == 0  # noqa: S603,S607 -- git and gh, fixed argv


def git_show(root: Path, rev: str, path: str) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"], capture_output=True, text=True)  # noqa: S603,S607 -- git and gh, fixed argv
    return result.stdout if result.returncode == 0 else None


def python_change_reader(root: Path, base: str, head: str) -> Callable[[cs.Change], PyChange]:
    def read(change: cs.Change) -> PyChange:
        patch = git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", base, head, "--", change.path)
        old_lines, new_lines = hunk_lines(patch)
        old = git_show(root, base, change.path) if change.status != "A" else None
        new = git_show(root, head, change.path) if change.status != "D" else None
        return classify_python(old, new, old_lines, new_lines)
    return read


def diff_changes(root: Path, old: str, new: str) -> list[cs.Change] | None:
    records = git(root, "-c", "core.quotePath=true", "diff", "--no-renames", "--raw", old, new).split("\n")
    return cs.plain_changes(records)


def defs_of(source: str | None) -> set[str]:
    if source is None:
        return set()
    try:
        return {span[2] for span in _spans(ast.parse(source)) if span[3] == "func"}
    except SyntaxError:
        return set()


def drift_between(root: Path, map_commit: str, base: str) -> Drift | None:
    """The changes from the map's commit to the base (None: not comparable)."""
    changes = diff_changes(root, map_commit, base)
    if changes is None:
        return None
    drift = Drift(changes, python_change_reader(root, map_commit, base))
    for change in changes:
        if change.path.endswith(".py"):
            if change.status == "A":
                drift.added_files.add(change.path)
            elif change.status == "M":
                added = defs_of(git_show(root, base, change.path)) - defs_of(git_show(root, map_commit, change.path))
                drift.added_keys |= {f"{change.path}:{q}" for q in added}
    return drift


def first_parents(root: Path, base: str, count: int) -> list[str]:
    """`base` and its first-parent ancestors, newest first: the history's own
    order, not commit dates (which their authors set)."""
    shallow = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-shallow-repository"],  # noqa: S603,S607 -- git and gh, fixed argv
                             capture_output=True, text=True).stdout.strip() == "true"
    if shallow or not git_ok(root, "cat-file", "-e", f"{base}^{{commit}}"):
        subprocess.run(["git", "-C", str(root), "fetch", "-q", f"--depth={count}", "origin", base],  # noqa: S603,S607 -- git and gh, fixed argv
                       capture_output=True, check=False)
    result = subprocess.run(["git", "-C", str(root), "rev-list", "--first-parent", f"--max-count={count}", base],  # noqa: S603,S607 -- git and gh, fixed argv
                            capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


def gh_api(path: str, raw: bool = False) -> object:
    out = subprocess.run(["gh", "api", path], capture_output=True, check=True).stdout  # noqa: S603,S607 -- git and gh, fixed argv
    return out if raw else json.loads(out)


def repository_id(repo: str, api: Callable[..., object] | None = None) -> int:
    env = os.environ.get("GITHUB_REPOSITORY_ID")
    if env and env.isdigit():
        return int(env)
    return int((api or gh_api)(f"repos/{repo}")["id"])  # type: ignore[index]


MAX_ARTIFACT_PAGES = 10


def map_artifacts(repo: str, repo_id: int, api: Callable[..., object],
                  now: datetime | None = None) -> dict[str, dict]:
    """Unexpired `testmap` artifacts of this repository's own runs on main,
    newest per commit. A fork's pull request run can upload an artifact with
    the same name and a branch called main: its head repository is not ours.
    Every page is read (newest first) until the artifacts are older than
    MAX_AGE_HOURS, so a flood of other `testmap` artifacts cannot push a real
    map off the first page; MAX_ARTIFACT_PAGES bounds the walk."""
    now = now or datetime.now(UTC)
    by_sha: dict[str, dict] = {}
    for page in range(1, MAX_ARTIFACT_PAGES + 1):
        listing = api(f"repos/{repo}/actions/artifacts?name={ARTIFACT}&per_page=100&page={page}")
        artifacts = (listing or {}).get("artifacts", []) if isinstance(listing, dict) else []
        for art in artifacts:
            run = art.get("workflow_run") or {}
            if art.get("expired") or art.get("name") != ARTIFACT or run.get("head_branch") != "main":
                continue
            if run.get("repository_id") != repo_id or run.get("head_repository_id") != repo_id:
                continue
            sha = run.get("head_sha")
            if sha and (sha not in by_sha or art.get("created_at", "") > by_sha[sha].get("created_at", "")):
                by_sha[sha] = art
        created = [c for c in (_created(a) for a in artifacts) if c is not None]
        if len(artifacts) < 100 or (created and all((now - c).total_seconds() > MAX_AGE_HOURS * 3600 for c in created)):
            break
    return by_sha


def run_problem(repo: str, repo_id: int, art: dict, sha: str, api: Callable[..., object]) -> str | None:
    """Why the run that uploaded `art` is not the map workflow's own green run
    on main at `sha`, or None."""
    run = api(f"repos/{repo}/actions/runs/{art['workflow_run']['id']}")
    if not isinstance(run, dict):
        return "its run could not be read"
    if run.get("path") != MAP_WORKFLOW:
        return f"its run is {run.get('path')}, not {MAP_WORKFLOW}"
    if run.get("event") not in MAP_EVENTS:
        return f"its run is a {run.get('event')} run"
    if run.get("head_branch") != "main" or run.get("head_sha") != sha:
        return f"its run is on {run.get('head_branch')} at {str(run.get('head_sha'))[:12]}"
    if (run.get("repository") or {}).get("id") != repo_id or (run.get("head_repository") or {}).get("id") != repo_id:
        return "its run is from another repository"
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return f"its run is {run.get('status')}/{run.get('conclusion')}"
    return None


def _created(art: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(art.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return None


def find_map(repo: str, repo_id: int, base: str, *, root: Path, api: Callable[..., object] | None = None,
             now: datetime | None = None) -> tuple[dict | None, str]:
    """The newest valid map on `base`'s first-parent history (at most
    MAX_DISTANCE commits back, uploaded within MAX_AGE_HOURS by the server's
    clock), and why there is none."""
    now = now or datetime.now(UTC)
    api = api or gh_api
    candidates = first_parents(root, base, MAX_DISTANCE + 1)
    if not candidates:
        return None, f"the history of {base[:12]} could not be read"
    artifacts = map_artifacts(repo, repo_id, api, now)
    skipped: list[str] = []
    for distance, sha in enumerate(candidates):
        art = artifacts.get(sha)
        if art is None:
            continue
        created = _created(art)
        if created is None or (now - created).total_seconds() > MAX_AGE_HOURS * 3600:
            skipped.append(f"{sha[:12]}: uploaded more than {MAX_AGE_HOURS} h ago")
            continue
        why = run_problem(repo, repo_id, art, sha, api)
        if why is None:
            zipped = api(f"repos/{repo}/actions/artifacts/{art['id']}/zip", raw=True)
            with zipfile.ZipFile(io.BytesIO(zipped)) as archive:  # type: ignore[arg-type]
                name = next((n for n in archive.namelist() if n.endswith(".json.gz")), None)
                data = json.loads(gzip.decompress(archive.read(name))) if name else {}
            why = validate_map(data, commit=sha) if data else "the artifact holds no map"
        if why:
            skipped.append(f"{sha[:12]}: {why}")
            continue
        data["_distance"] = distance
        return data, ""
    note = f" (passed over: {'; '.join(skipped[:3])})" if skipped else ""
    return None, f"no valid map at or within {MAX_DISTANCE} first-parent commits before {base[:12]}{note}"


def needed(repo: str, repo_id: int, head: str, *, root: Path, force: bool = False,
           api: Callable[..., object] | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Should the map workflow record? Not when the newest map a pull request
    on `head` could use (find_map: provenance, first-parent distance, age,
    validity) agrees with `head` on everything the map is built from
    (MAP_INPUTS). No usable map: record."""
    if force:
        return True, "a dispatch asked for a map"
    found, why = find_map(repo, repo_id, head, root=root, api=api, now=now)
    if found is None:
        return True, f"there is no usable map ({why})"
    sha = str(found["commit"])
    if sha == head:
        return False, f"the newest usable map is of {head[:12]} already"
    try:
        changed = git(root, "diff", "--name-only", sha, head, "--", *MAP_INPUTS).split()
    except subprocess.CalledProcessError:
        return True, f"{sha[:12]}, the newest usable map's commit, cannot be compared with {head[:12]}"
    if changed:
        return True, f"{len(changed)} files the map is built from changed since {sha[:12]} (first: {changed[0]})"
    return False, f"nothing the map is built from changed since {sha[:12]}"


# ---------------------------------------------------------------- reports


def summary_lines(sel: Selection, total_tests: int) -> list[str]:
    lines = ["### Test selection (shadow: the full suite still runs)", ""]
    if sel.full:
        lines.append(f"**Full suite.** {sel.reasons[0] if sel.reasons else ''}")
    else:
        lines.append(f"**Selected share: {sel.share:.1%}** of the suite's weight, "
                     f"{len(sel.selected)} of {total_tests} mapped tests "
                     f"({len(sel.whole_files)} whole test files); map {sel.map_commit[:12]}.")
    if sel.reasons[1:] or (not sel.full and sel.reasons):
        lines.append("")
        lines.extend(f"- {r}" for r in (sel.reasons[1:] if sel.full else sel.reasons)[:30])
    lines.extend(f"- note: {n}" for n in sel.notes)
    return lines


SUMMARY_LINE = re.compile(r"(?:^|\s)(?:FAILED|ERROR) (tests/.*)$")
COLLECTION_ERROR = re.compile(r"(?:^|\s)ERROR (tests/\S+\.py)(?: - .*)?$")
UNKNOWN = "unknown:"
MODULE = "module:"


def failed_ids(log: str, known: Iterable[str]) -> set[str]:
    """The tests a pytest log reports FAILED or ERROR. A short-summary line is
    "<status> <node id>[ - <message>]" and a node ID may hold spaces, so a
    line is matched against the known IDs (the map's) of its file, longest
    first; a line no known ID matches is "unknown:<line>": a test the map
    does not know, which the selection runs (S11). "ERROR <file>.py[ - ...]",
    with no "::", is a collection error (an import error, say): every test of
    the file failed; it is "module:<file>"."""
    by_file: dict[str, list[str]] = defaultdict(list)
    for test_id in known:
        by_file[test_id.split("::")[0]].append(test_id)
    for ids in by_file.values():
        ids.sort(key=len, reverse=True)
    out: set[str] = set()
    for line in log.splitlines():
        line = line.rstrip("\r")
        collection = COLLECTION_ERROR.search(line)
        if collection:
            out.add(MODULE + collection.group(1))
            continue
        m = SUMMARY_LINE.search(line)
        if not m or "::" not in m.group(1):
            continue
        rest = m.group(1)
        match = next((t for t in by_file.get(rest.split("::")[0], ())
                      if rest == t or rest.startswith(t + " - ") or rest.startswith(t + " ")), None)
        out.add(match if match is not None else UNKNOWN + rest.split(" - ")[0])
    return out


# ---------------------------------------------------------------- CLI


def _write_summary(path: str | None, lines: list[str]) -> None:
    """The job summary, and stderr: stdout carries machine output (`needed`
    writes it to GITHUB_OUTPUT)."""
    text = "\n".join(lines) + "\n"
    print(text, file=sys.stderr)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def cmd_shadow(args: argparse.Namespace) -> int:
    root = Path.cwd()
    try:
        sel = shadow(args, root)
    except Exception as error:  # report only: a failure here runs the full suite
        sel = Selection(True, [f"the shadow selection failed ({type(error).__name__}: {error})"])
    write_gz(sel.to_json(), Path(args.out))
    _write_summary(args.summary, summary_lines(sel, len(sel.selected) + len(sel.not_selected)))
    return 0


def shadow(args: argparse.Namespace, root: Path) -> Selection:
    if args.full_reason:
        return Selection(True, [args.full_reason])
    if args.pr:
        labels = gh_api(f"repos/{args.repo}/issues/{args.pr}/labels?per_page=100")
        if any(label.get("name") == "ci:full" for label in labels):  # type: ignore[union-attr]
            return Selection(True, ["the pull request is labeled ci:full"])
    if args.map:
        data = read_gz(Path(args.map))
        why = validate_map(data)
        first_parents(root, args.base, MAX_DISTANCE + 1)
    else:
        found, why = find_map(args.repo, repository_id(args.repo), args.base, root=root)
        data = found or {}
    if why or not data:
        return Selection(True, [f"no usable map: {why}"])
    changes = diff_changes(root, args.base, args.head)
    if changes is None:
        return Selection(True, ["a diff record is not an add, modify or delete of a regular file"])
    map_commit = str(data["commit"])
    drift: Drift | None = None
    if map_commit != args.base:
        # The map can be hours old. A call edge added since then can only
        # come from a changed function body: the changes from the map's
        # commit to the base count as changes too.
        if not git_ok(root, "merge-base", "--is-ancestor", map_commit, args.base):
            return Selection(True, [f"the map's commit {map_commit[:12]} is not an ancestor of the base"])
        drift = drift_between(root, map_commit, args.base)
        if drift is None:
            return Selection(True, ["a diff record between the map's commit and the base is not plain"])
    tmap = TestMap(data)
    weights = test_weights(root, (t[0] for t in tmap.tests))
    sel = select(changes, tmap, root=root, classify=python_change_reader(root, args.base, args.head),
                 index=lambda: NameIndex.build(root), weights=weights, drift=drift)
    if "_distance" in data:
        sel.notes.append(f"map {map_commit[:12]} is {data['_distance']} first-parent commits before the base")
    return sel


def cmd_compare(args: argparse.Namespace) -> int:
    lines = ["### Shadow selection against this run's failures", ""]
    try:
        try:
            selection = read_gz(Path(args.selection))
        except OSError:
            selection = {"full": True, "reasons": ["no shadow selection for this run"]}
        known = [*selection.get("selected", ()), *selection.get("not_selected", ())]
        failed: set[str] = set()
        if args.failed:
            failed = failed_ids(Path(args.failed).read_text(encoding="utf-8"), known)
        else:
            jobs = gh_api(f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100")
            for job in (jobs or {}).get("jobs", []):  # type: ignore[union-attr]
                if job.get("name", "").startswith("test (") and job.get("conclusion") == "failure":
                    log = gh_api(f"repos/{args.repo}/actions/jobs/{job['id']}/logs", raw=True)
                    failed |= failed_ids(log.decode("utf-8", "replace"), known)  # type: ignore[union-attr]
        runs = membership(selection)
        unknown = sorted(t for t in failed if t.startswith(UNKNOWN))
        modules = sorted(t for t in failed if t.startswith(MODULE))
        outside = sorted(t for t in failed if not t.startswith((UNKNOWN, MODULE)) and not runs(t))
        # A collection error is outside the selection when the selection runs
        # no test of that file (pytest would not collect it). A file the map
        # does not know runs (S11).
        runs_file = {t.split("::")[0] for t in known if runs(t)} | set(selection.get("whole_files", ()))
        known_files = {t.split("::")[0] for t in known}
        outside += [m for m in modules if not selection.get("full")
                    and m[len(MODULE):] in known_files and m[len(MODULE):] not in runs_file]
        if selection.get("full"):
            lines.append(f"The shadow selection ran the full suite ({'; '.join(selection.get('reasons', [])[:1])}).")
        lines.append(f"failures outside selection: {len(outside)}" + (f" ({', '.join(outside[:50])})" if outside else ""))
        lines.append(f"failures in this run: {len(failed)}, of which {len(modules)} are collection errors and"
                     f" {len(unknown)} are tests the map does not know (the selection runs those)")
    except Exception as error:  # report only
        lines.append(f"the comparison failed ({type(error).__name__}: {error})")
    _write_summary(args.summary, lines)
    return 0


# ---------------------------------------------------------------- heavy lane

# The heavy lane's check against reality (commit_scope.py's run_heavy): every
# repository file a heavy test ran a function of or opened, in a map, must be
# a change that runs the lane. A new map is checked by the map workflow; the
# default suite holds the scope to HEAVY_SNAPSHOT, written from a map.
HEAVY_SNAPSHOT = "scripts/ci/heavy_footprint.json"
# Files a map records in heavy tests' windows that cannot run in the lane:
# what another module leaves behind in the shards' shared processes.
HEAVY_LEAKS: dict[str, str] = {
    "tests/conformance/test_shadow_observation_native.py": (
        "its native_liveness fixture registers a pytest plugin (Reports.pytest_runtest_makereport) that stays "
        "registered for every later test in the same worker; test-heavy does not run that module"),
    "tests/shadow_observation_diagnostics.py": (
        "NativeLiveness.report, called only by that leaked plugin in heavy tests' windows"),
}


def heavy_footprint(tmap: TestMap, test_files: Iterable[str]) -> set[str]:
    """The repository paths the map's tests in `test_files` ran a function of
    or opened: each test's own window, the setup of the fixtures it requests
    and its module's collection. Not a memoized function's misses (`cached`:
    every miss of the function in the process, whichever test's window it
    was) and not `global` (imports and session hooks every test shares)."""
    wanted = set(test_files)
    paths: set[str] = set()
    for nodeid, own, fixtures in tmap.tests:
        module = nodeid.split("::")[0]
        if module not in wanted:
            continue
        for index in [own, *fixtures, *([tmap.collect[module]] if module in tmap.collect else [])]:
            funcs, opened = tmap.fps[index][0], tmap.fps[index][1]
            paths.update(tmap.keys[key].split(":", 1)[0] for key in funcs)
            paths.update(tmap.keys[key] for key in opened)
    return paths


def heavy_outside(root: Path, paths: Iterable[str]) -> list[str]:
    """The paths, of files that exist, whose change would not run the lane."""
    closure = cs.heavy_closure(root)
    return sorted(path for path in set(paths) if (root / path).is_file() and path not in HEAVY_LEAKS
                  and not cs.heavy_covers(path, closure) and not cs.matches_any(path, cs.FULL_MATRIX_PATHS))


def cmd_heavy(args: argparse.Namespace) -> int:
    root = Path.cwd()
    data = read_gz(Path(args.map))
    problem = validate_map(data)
    if problem:
        print(f"::error title=Heavy lane scope::{problem}")
        return 1
    tmap = TestMap(data)
    mapped = {nodeid.split("::")[0] for nodeid, _, _ in tmap.tests}
    absent = sorted(set(cs.HEAVY_TESTS) - mapped)
    paths = heavy_footprint(tmap, cs.HEAVY_TESTS)
    if args.write:
        if absent:
            print(f"::error title=Heavy lane scope::the map has no tests of {', '.join(absent)}")
            return 1
        # Repository files only: a map also records names opened relative to
        # another directory (a temporary one).
        snapshot = {"map": {key: data.get(key) for key in ("commit", "run_id", "created")},
                    "tests": sorted(cs.HEAVY_TESTS), "files": sorted(p for p in paths if (root / p).is_file())}
        Path(args.write).write_text(json.dumps(snapshot, indent=1) + "\n", encoding="utf-8")
    outside = heavy_outside(root, paths)
    lines = [f"### Heavy lane scope against map {str(data.get('commit'))[:12]}", "",
             f"{len(paths)} paths the lane's tests ran or opened; outside the lane's scope: {len(outside)}"]
    lines += [f"- {path}" for path in outside]
    if absent:
        lines.append(f"not in the map (newer than it): {', '.join(absent)}")
    _write_summary(args.summary, lines)
    for path in outside:
        print(f"::error title=Heavy lane scope::{path} is in a heavy test's footprint but outside run_heavy")
    return 1 if outside else 0


def cmd_needed(args: argparse.Namespace) -> int:
    try:
        head = args.head or git(Path.cwd(), "rev-parse", "HEAD").strip()
        record, why = needed(args.repo, repository_id(args.repo), head, root=Path.cwd(), force=args.force == "true")
    except Exception as error:  # cannot tell: record
        record, why = True, f"the check failed ({type(error).__name__}: {error})"
    print(f"record={'true' if record else 'false'}")
    _write_summary(args.summary, [f"### Test map: {'recording' if record else 'skipped'}", "", why])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compact", help="one shard's raw recorder output -> a fragment")
    p.add_argument("raw", type=Path)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("merge", help="every shard's fragment -> the map")
    p.add_argument("fragments", type=Path, help="directory searched for *.json.gz")
    p.add_argument("--shards", type=int, default=SHARDS)
    p.add_argument("--commit", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("needed", help="record=true|false: did the map's inputs change since the last map")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--head", default="")
    p.add_argument("--force", default="false")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p = sub.add_parser("shadow", help="select the tests a pull request reaches (report only)")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--map", help="a local map instead of the newest artifact")
    p.add_argument("--full-reason", default="")
    p.add_argument("--pr", default="", help="pull request number, for its ci:full label")
    p.add_argument("--out", required=True)
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p = sub.add_parser("compare", help="this run's failed tests against the shadow selection")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    p.add_argument("--selection", required=True)
    p.add_argument("--failed", help="a pytest log to read failures from instead of the run's shard logs")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p = sub.add_parser("heavy", help="the heavy lane's footprint in a map against its scope (exit 1 outside)")
    p.add_argument("--map", required=True, help="a map file (testmap.json.gz)")
    p.add_argument("--write", help=f"also write the footprint snapshot ({HEAVY_SNAPSHOT})")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
    if args.command == "heavy":
        return cmd_heavy(args)
    if args.command == "compact":
        fragment = compact(args.raw, args.shard)
        write_gz(fragment, args.out)
        print(f"shard {args.shard}: {len(fragment['tests'])} tests, {len(fragment['fps'])} footprints, "
              f"{len(fragment['keys'])} keys, valid={fragment['valid']} {fragment['errors'][:3]}")
        return 0
    if args.command == "merge":
        fragments = [read_gz(p) for p in sorted(args.fragments.rglob("*.json.gz"))]
        tmap = merge(fragments, shards=args.shards, commit=args.commit, ref=args.ref, event=args.event,
                     run_id=args.run_id, root=Path.cwd())
        write_gz(tmap, args.out)
        print(f"map {args.commit[:12]}: {len(tmap['tests'])} tests, {len(tmap['fps'])} footprints, "
              f"{len(tmap['keys'])} keys, {len(tmap['incomplete_memo'])} incomplete memoized functions, "
              f"{args.out.stat().st_size / 1e6:.1f} MB")
        return 0
    if args.command == "needed":
        return cmd_needed(args)
    if args.command == "shadow":
        return cmd_shadow(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
