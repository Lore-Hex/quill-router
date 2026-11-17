"""The per-test footprint map and the test selection it drives (shadow mode).

Stdlib only, like commit_scope.py, so the scope job runs it before `uv sync`.

  compact   one shard's raw recorder output (scripts/ci/testmap_recorder.py,
            one JSONL file per process) -> one gzipped fragment: interned keys,
            identical footprints shared, each test's scoped fixtures resolved.
  merge     the eight fragments of a green push run -> the map, stamped with
            the commit, the recorder and schema versions and the shard count,
            plus the functions and Python files that existed at that commit.
  shadow    on a pull request: find the newest valid map at or before the
            base, select the tests the diff can reach, write the selection
            (gzipped JSON) and a job summary. Report only: nothing reads the
            selection to decide what runs.
  compare   after the test shards: the failed test IDs of this run against
            the shadow selection, "failures outside selection: N (IDs)".

What a footprint holds (the recorder's docstring has how it is recorded):
  f  functions that started or resumed ("path:qualname")
  o  repository files opened for reading (an import is not a read)
  d  repository directories listed
  s  commands started ("argv0 argv1 ... @cwd=<repo-relative or ->")
A test's footprint is its own window (setup, call and teardown), its module's
collection, the setup of every module/class/package/session fixture it
requests, and, through `cached`, what each memoized function it called ran
when it missed. `global` is what ran outside every window: imports, session
hooks, collection hooks.

Selection rules (the full suite runs on any fail-safe; reasons are reported):

  Fail-safes (run everything):
   F1 a diff record that is not a plain add/modify/delete of a regular file;
      an empty diff; a change to what decides CI (commit_scope's
      FULL_MATRIX_PATHS, any .gitattributes) or to the Python toolchain;
      the pull request labeled ci:full.
   F2 no map; a map that is unreadable, of another schema or recorder
      version, not from a push to main, not from all of its shards, or
      stale: not the base or a first-parent ancestor within MAX_DISTANCE
      commits and MAX_AGE_HOURS of it.
   F3 a changed file read outside every window (at import or session
      time), or a changed function that ran outside every window (at import:
      main.py builds its app at import, so create_app's callees are there).
   F4 a changed module-level statement that is not an import, a docstring,
      an assignment, a class body or a decorated definition (a call, an
      `if`, a `try` at module level); a .py file the diff cannot parse.
   F5 a changed function, or a changed Python file, the map cannot know:
      it existed at the base but not at the map's commit.
   F6 the selection reaches more than FULL_SHARE of the suite's weight.

  Selection (union):
   S1 a changed test file runs whole (a deleted one runs nothing).
   S2 a changed line selects the tests that ran the innermost def that
      encloses it and existed at the base; a new nested def changes its
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
   S8 a test that started a Python process, or git in the repository, is
      selected by any change; another command by a change to the file it
      names, to its directory, or to any shell script.
   S9 a changed test helper (Python under tests/ or scripts/) also selects
      what commit_scope's reverse import walk reaches (not through
      conftest.py, which every test imports: its functions select by S2).
   S10 a memoized function's users inherit what its misses ran; module and
      session fixture setups count for every test that requests them; a
      module's collection counts for each of its tests.
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
RECORDER_VERSION = "1"
SHARDS = 8
MAX_DISTANCE = 40
MAX_AGE_HOURS = 24
FULL_SHARE = 0.7
ARTIFACT = "testmap"
# Directories the name index and the map's universe skip: not repository code.
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".hypothesis", ".ruff_cache",
                       ".mypy_cache", ".pytest_cache"})
PYTHON_EXE = re.compile(r"(?:^|/)python[0-9.]*$")
GIT_EXE = re.compile(r"(?:^|/)git$")
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
        "global": joined(globals_),
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
    if data.get("event") != "push" or data.get("ref") != "refs/heads/main":
        return f"map is from {data.get('event')} of {data.get('ref')}, not a push to main"
    if data.get("fragments") != list(range(1, int(data.get("shards") or 0) + 1)) or data.get("shards") != SHARDS:
        return f"map has fragments {data.get('fragments')} of {data.get('shards')} shards, expected {SHARDS}"
    if commit is not None and data.get("commit") != commit:
        return f"map commit {data.get('commit')} is not {commit}"
    for name in ("keys", "fps", "tests", "collect", "cached", "global"):
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


def helper_reach(root: Path, helpers: list[str]) -> set[str]:
    """Test files that import a changed helper, directly or through other
    helpers (not through conftest.py: every test loads it, and its functions
    select by what ran), or name its file."""
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


def select(changes: list[cs.Change], tmap: TestMap, *, root: Path, classify: Callable[[cs.Change], PyChange],
           index: Callable[[], NameIndex], weights: dict[str, float]) -> Selection:
    """Rules S1-S10 and fail-safes F1 and F3-F6 (F2, the map's validity, and
    the ci:full label are the caller's; S11 is `contains`)."""
    commit = str(tmap.data.get("commit", ""))
    reasons: list[str] = []

    def full(reason: str) -> Selection:
        return Selection(True, [reason, *reasons], map_commit=commit)

    if not changes:
        return full("the comparison is empty")
    for change in changes:  # F1
        path = change.path
        if cs.matches_any(path, cs.FULL_MATRIX_PATHS) or path.rsplit("/", 1)[-1] in cs.FULL_MATRIX_NAMES:
            return full(f"{path} decides what CI runs")
        if path in cs.PYTHON_TOOLCHAIN_PATHS:
            return full(f"{path} is the Python toolchain")

    global_funcs = tmap.global_funcs()
    global_opens = tmap.global_opens()
    whole: set[str] = set()
    keys: set[str] = set()      # S2-S4: function keys
    files: set[str] = set()     # S5: every function of these files
    opened: set[str] = set()    # S7
    listed: set[str] = set()    # S6, S7
    helpers: list[str] = []     # S9
    changed_paths = {c.path for c in changes}
    names: NameIndex | None = None

    def name_index() -> NameIndex:
        nonlocal names
        if names is None:
            names = index()
        return names

    for change in changes:
        path, status = change.path, change.status
        if status in ("A", "D"):
            listed.add(path.rsplit("/", 1)[0] if "/" in path else ".")
        if path in global_opens:  # F3
            return full(f"{path} is read outside every test (at import or session time)")
        opened.add(path)
        if not path.endswith(".py"):
            continue
        if cs.is_test_file(path):  # S1
            if status != "D":
                whole.add(path)
                reasons.append(f"{path}: changed test file runs whole")
            continue
        if status == "A":  # S6
            continue
        if path.startswith(("tests/", "scripts/")) and not path.endswith("/conftest.py"):
            helpers.append(path)
        if path not in tmap.universe_files:  # F5
            return full(f"{path} is newer than the map ({commit[:12]})")
        if status == "D":  # S5
            files.add(path)
            continue
        info = classify(change)
        if info.unparsable:  # F4
            return full(f"{path} does not parse")
        for kind, name in sorted(info.kinds):  # F4
            if kind not in MODULE_KINDS_SELECTING | MODULE_KINDS_INERT:
                return full(f"{path}: a module-level {name or kind} statement changed")
        for q in sorted(info.funcs):  # S2
            key = f"{path}:{q}"
            if key not in tmap.universe_funcs:  # F5
                return full(f"{key} is newer than the map ({commit[:12]})")
            if key in global_funcs:  # F3
                return full(f"{key} runs at import or session time")
            keys.add(key)
        for cls in sorted(info.classes):  # S3
            keys |= name_index().spread({cls})
            prefix = f"{path}:{cls}."
            keys |= {k for k in tmap.known_funcs if k.startswith(prefix)}
            reasons.append(f"{path}: class {cls} changed")
        spread = {name for kind, name in info.kinds if kind in MODULE_KINDS_SELECTING and name}
        if spread:  # S4
            found = name_index().spread(spread)
            keys |= found
            reasons.append(f"{path}: module-level {', '.join(sorted(spread)[:4])} named by {len(found)} functions")
    static_reach = helper_reach(root, helpers)  # S9
    if static_reach:
        reasons.append(f"{len(static_reach)} test files import or name a changed helper")

    key_ids = tmap.ids(keys)
    for path in files:
        prefix = f"{path}:"
        key_ids |= {i for i, k in enumerate(tmap.keys) if k.startswith(prefix)}
    open_ids = tmap.ids(opened)
    dir_ids = tmap.ids(listed)
    shell_changed = any(p.endswith(".sh") for p in changed_paths)
    changed_dirs = {p.rsplit("/", 1)[0] for p in changed_paths if "/" in p}
    command_hit: dict[int, bool] = {}

    def spawn_hit(command_ids: list[int]) -> bool:  # S8
        for cid in command_ids:
            hit = command_hit.get(cid)
            if hit is None:
                toks, cwd = _split_command(tmap.keys[cid])
                exe = toks[0] if toks else ""
                in_repo = cwd != "-" or any(tok == "." or tok in tmap.universe_files for tok in toks[1:])
                if PYTHON_EXE.search(exe) or (GIT_EXE.search(exe) and in_repo):
                    hit = True
                else:
                    hit = any(tok in changed_paths or tok in changed_dirs
                              or (tok.rsplit("/", 1)[0] in changed_dirs if "/" in tok else False)
                              for tok in toks)
                    hit = hit or (shell_changed and any(tok.endswith(".sh") for tok in toks))
                command_hit[cid] = hit
            if hit:
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

    while True:  # S10: a memoized function's users inherit what its misses ran
        new = {tmap.key_id[c] for c, i in tmap.cached.items()
               if c in tmap.key_id and tmap.key_id[c] not in key_ids and is_hit(i)}
        if not new:
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


def gh_api(path: str, raw: bool = False) -> object:
    out = subprocess.run(["gh", "api", path], capture_output=True, check=True).stdout  # noqa: S603,S607 -- git and gh, fixed argv
    return out if raw else json.loads(out)


def find_map(repo: str, base: str, *, api: Callable[..., object] = gh_api) -> tuple[dict | None, str]:
    """The newest map at or before `base`, and why there is none."""
    commits = api(f"repos/{repo}/commits?sha={base}&per_page={MAX_DISTANCE + 1}")
    if not isinstance(commits, list) or not commits:
        return None, f"the history of {base} could not be read"
    artifacts = api(f"repos/{repo}/actions/artifacts?name={ARTIFACT}&per_page=100")
    by_sha: dict[str, dict] = {}
    for art in (artifacts or {}).get("artifacts", []) if isinstance(artifacts, dict) else []:
        run = art.get("workflow_run") or {}
        if art.get("expired") or run.get("head_branch") != "main":
            continue
        sha = run.get("head_sha")
        if sha and (sha not in by_sha or art.get("created_at", "") > by_sha[sha].get("created_at", "")):
            by_sha[sha] = art
    base_time = _when(commits[0])
    for distance, commit in enumerate(commits[: MAX_DISTANCE + 1]):
        sha = commit.get("sha")
        if sha not in by_sha:
            continue
        when = _when(commit)
        if base_time and when and (base_time - when).total_seconds() > MAX_AGE_HOURS * 3600:
            return None, f"the newest map ({sha[:12]}) is more than {MAX_AGE_HOURS} h older than the base"
        zipped = api(f"repos/{repo}/actions/artifacts/{by_sha[sha]['id']}/zip", raw=True)
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:  # type: ignore[arg-type]
            name = next((n for n in archive.namelist() if n.endswith(".json.gz")), None)
            if name is None:
                return None, f"the map artifact of {sha[:12]} holds no map"
            data = json.loads(gzip.decompress(archive.read(name)))
        why = validate_map(data, commit=sha)
        if why:
            return None, why
        data["_distance"] = distance
        return data, ""
    return None, f"no map at or within {MAX_DISTANCE} commits before {base[:12]}"


def _when(commit: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


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


FAILED = re.compile(r"\b(?:FAILED|ERROR) (tests/\S+)")


def failed_ids(log: str) -> set[str]:
    out = set()
    for m in FAILED.finditer(log):
        test = m.group(1)
        if "::" in test:
            out.add(XDIST_GROUP.sub("", test))
    return out


# ---------------------------------------------------------------- CLI


def _write_summary(path: str | None, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
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
    subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", args.base], cwd=root, check=False)  # noqa: S603,S607 -- git and gh, fixed argv
    records = git(root, "-c", "core.quotePath=true", "diff", "--no-renames", "--raw", args.base, args.head).split("\n")
    changes = cs.plain_changes(records)
    if changes is None:
        return Selection(True, ["a diff record is not an add, modify or delete of a regular file"])
    if args.map:
        data = read_gz(Path(args.map))
        why = validate_map(data)
    else:
        found, why = find_map(args.repo, args.base)
        data = found or {}
    if why or not data:
        return Selection(True, [f"no usable map: {why}"])
    tmap = TestMap(data)
    weights = test_weights(root, (t[0] for t in tmap.tests))
    sel = select(changes, tmap, root=root, classify=python_change_reader(root, args.base, args.head),
                 index=lambda: NameIndex.build(root), weights=weights)
    if "_distance" in data:
        sel.notes.append(f"map {data['commit'][:12]} is {data['_distance']} commits before the base")
    return sel


def cmd_compare(args: argparse.Namespace) -> int:
    lines = ["### Shadow selection against this run's failures", ""]
    try:
        try:
            selection = read_gz(Path(args.selection))
        except OSError:
            selection = {"full": True, "reasons": ["no shadow selection for this run"]}
        failed: set[str] = set()
        if args.failed:
            failed = {line.strip() for line in Path(args.failed).read_text().splitlines() if line.strip()}
        else:
            jobs = gh_api(f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100")
            for job in (jobs or {}).get("jobs", []):  # type: ignore[union-attr]
                if job.get("name", "").startswith("test (") and job.get("conclusion") == "failure":
                    log = gh_api(f"repos/{args.repo}/actions/jobs/{job['id']}/logs", raw=True)
                    failed |= failed_ids(log.decode("utf-8", "replace"))  # type: ignore[union-attr]
        runs = membership(selection)
        outside = sorted(t for t in failed if not runs(t))
        if selection.get("full"):
            lines.append(f"The shadow selection ran the full suite ({'; '.join(selection.get('reasons', [])[:1])}).")
        lines.append(f"failures outside selection: {len(outside)}" + (f" ({', '.join(outside[:50])})" if outside else ""))
        lines.append(f"failures in this run: {len(failed)}")
    except Exception as error:  # report only
        lines.append(f"the comparison failed ({type(error).__name__}: {error})")
    _write_summary(args.summary, lines)
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
    p.add_argument("--failed", help="a file of failed test IDs instead of the run's logs")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
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
              f"{len(tmap['keys'])} keys, {args.out.stat().st_size / 1e6:.1f} MB")
        return 0
    if args.command == "shadow":
        return cmd_shadow(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
