"""Path-aware job selection for ci.yml.

Three subcommands, all stdlib only so the runner's python3 can execute them
before `uv sync`:

  classify   reads `git diff --no-renames --raw BASE HEAD` on stdin and prints
             `true` when every record adds, modifies or deletes a regular file
             that is published catalog data, and `false` otherwise. An empty
             diff is `false`: nothing changed is not the same as only data
             changed, and the complete matrix is the safe default for a
             comparison that produced no records. A rename or copy record, a
             symlink, submodule or executable mode, or a line that is not a
             raw record is `false`.
  jobs       reads `git diff --no-renames --raw REF HEAD` on stdin and prints
             one `<flag>=true|false` line for each of PATH_FLAGS, `true` when
             the jobs behind the flag must run, and `post_cutover_tests=`
             with the test files the post-cutover suite is limited to (empty:
             the whole suite). REF is the commit ci.yml's
             scope job compares with (see "Path-scoped jobs" below).
             --lines names the `git diff -U0` of the same two commits outside
             src/, which the post-cutover rule reads. --full
             prints every flag true. So does a record that is not an add,
             modify or delete of a regular file (a rename or copy, a type or
             mode change, a symlink or submodule, a quoted path, a line that is
             not a raw record), an empty diff, a change to ci.yml, scripts/ci/
             or any .gitattributes (which decides what a diff shows), and a
             rule whose inputs cannot be derived.
  summarize  reads the `needs` context of the summary job (JSON in NEEDS),
             the scope output (DATA_ONLY) and the event (EVENT_NAME) and exits
             non-zero unless every job succeeded, allowing a skipped job only
             when it is one of the jobs ci.yml skips for a data-only commit AND
             the commit was data-only, when it is path-scoped
             (PATH_SCOPED_JOBS) and the scope job's output for its flag is
             exactly `false`, or when it is one of SKIPPED_ON_PUSH (the
             post-cutover suite) AND the event is a push. A heavy job skipped
             otherwise, a failed scope job, or a job in any other state fails
             the run.

Data-only commits. The hourly price refresh publishes commits that touch only
DATA_ONLY_PATHS and gates publication on the ci.yml run of that commit. Every
job in SKIPPED_FOR_DATA_ONLY reads none of those files (Go store tests, TLA+
model checks, the frozen-main settle oracle with its fixture catalog, the BYOK
write-format diff, the frontend build, the Spanner conformance suites and the
coverage floor over the Python suite). The jobs that do read them run on every
commit: lint's catalog history check, the production import, the default-clock
suite and the browser smoke, and the post-cutover suite, whose flag every
catalog data path sets. tests/test_ci_workflow.py holds the workflow text to
these lists, and holds DATA_ONLY_PATHS to the files refresh-prices.yml stages.

Path-scoped jobs. Each flag names the files its jobs read; the jobs run on
pull requests, pushes to main and dispatches alike (the post-cutover suite
excepted, below), whenever one of those files differs between REF and HEAD,
and skip otherwise. There is no scheduled run: a job runs when something it
reads changed, and pushes to main apply the same rules as pull requests, so
two green pull requests that combine into a red main are caught by the push
that combines them. A pull request's REF is
its base: what it skips it did not change, and nothing ships from its run.
A push's or a dispatch's REF is the newest ancestor-or-self of its base whose
own ci.yml run passed, so a job a red commit failed keeps running on every
later push until a run passes it again, and its red holds every deploy.

  run_proofs (proofs: ./proofs/check.sh). check.sh model-checks proofs/*.tla
  with the pinned jar and runs proofs/check_mutants.py and guard_sweep.py,
  which read proofs/ and test only whether the code and tests that
  proofs/manifest.toml names for each spec exist (or, for a planned spec, do
  not). So: proofs/**, and every path the manifest names or a file under a
  directory it names, read from HEAD's manifest at scope time.

  run_fastpath (fastpath, fastpath-spanner, fastpath-walks). The Go module and
  the files its tests read outside it (FASTPATH_INPUTS):
  internal/tlc runs TLC on proofs/*.tla, *.cfg and *.guards.toml with
  proofs/tla2tools.jar; schema/ reads every scripts/deploy/*.sh and runs
  migrate_fastpath.sh; internal/store reads migrate_typed_counters.sh;
  internal/routingsnapshot reads tests/fixtures/usage_cost_vectors.json.
  scripts/fastpath/*.py only write fastpath/testdata, which the Go tests read
  as fastpath/**; a Python test in the default suite holds the two together.
  tests/test_commit_scope.py scans the Go sources for paths that leave the
  module and fails on one this list does not cover.

  run_proof_oracle (proof-oracle). The oracle runs live application code
  against a frozen copy of main (tests/fakes/frozen_main). Its live leg is the
  whole application: its tests import application modules by computed names,
  and execution-inventory.json records only the frozen leg at an older base,
  not the modules the live code runs today. So every path under src/ (pytest
  puts src on sys.path, so a sibling package there is application code too)
  except the published catalog data (the oracle builds its catalog from each
  case's fixture body, see ci.yml's proof-oracle). On the
  test side: the tests/ and scripts/ modules its two test files and
  tests/conftest.py import, transitively, read from HEAD's sources at scope
  time (imports and module names written as strings), every non-Python file
  under tests/ (fixtures, frozen_main), .test_durations (its split weights)
  and the Python toolchain. The oracle runs at the default lifecycle clock
  only: none of its fixture endpoints has an effective-dated price or
  retirement in either leg's provider_lifecycle (the live one and the frozen
  package's; tests/test_async_settle_proof_oracle.py holds both), so the
  post-cutover clock compares the same values. post-cutover-daily.yml, run by
  hand, still runs the post-cutover clock.

  run_post_cutover (test-post-cutover). The same suite as `test`, which runs
  on every commit, with the lifecycle clock past every scheduled cutover. It
  can only fail where `test` passes through something that differs between
  the two clocks: the catalog and prices provider_lifecycle.py schedules.
  The whole suite runs for any change to application code or data (all of
  src/: code behaves differently on the post-cutover catalog), to the pricing
  scripts, the catalog-derived Go vectors, its split weights, the Python
  toolchain, the harness every test runs under (tests/conftest.py and what it
  imports) or a test fixture; for any other changed file in a data format a
  test could load expectations from (DATA_SUFFIXES: JSON, YAML, TOML, CSV,
  text and the like), outside .github/; for a changed line in any other file
  that names a lifecycle identifier (LIFECYCLE_IDENTIFIERS) or a scheduled
  entity (a model, upstream id or provider with a retirement still ahead, or
  one whose price provider_lifecycle.py switches by date, read from HEAD at
  scope time), or a file whose lines the diff cannot show; for a changed
  Python file that no test imports or names (a test may load it by a
  computed name or path); and on every dispatch, the price refresh's
  publication gate. An expectation that changes past the cutovers without
  naming anything scheduled has to come from catalog-shaped data (a count, a
  list), hence the data formats; code and configuration that feed a test
  (Terraform, shell, Go, TypeScript, HTML, Markdown, workflows) name their
  models, and the line scan sees those. A test is code
  that can depend on the catalog without naming anything (a count over every
  model), so when only Python outside src/ (tests, helpers, scripts) changed
  otherwise, the suite runs, in one shard, on the tests that can reach it:
  each changed test file, every test that imports a changed module directly
  or through other modules (read from HEAD's sources, a directory's
  conftest.py counted as imported by its tests), and every test whose source
  names a changed file. More of those than an eighth of the test files runs
  the whole suite instead.

  It runs on pull requests and dispatches, not on a push to main
  (SKIPPED_ON_PUSH): a post-cutover red means something breaks on a future
  scheduled date, not that the commit is broken, and the pull request already
  ran it under the same rules. Two green pull requests that combine into a
  post-cutover red go red on the next pull request that runs the suite;
  cutovers are scheduled weeks out.

  These are accepted owner trade-offs: a helper that some tests import and
  another loads by a computed name selects only the importers; a file in no
  data format that names nothing scheduled runs nothing; and a push to main
  never runs the suite. A test such a change breaks past the cutovers goes
  red on the next change that runs it, or at the cutover.

  run_heavy (test-heavy). The test files in HEAVY_TESTS (scripts/ci/
  heavy_tests.txt: the slowest of the async settle family, 28% of the default
  suite's recorded weight) left the
  eight `test` shards, which pass each as --ignore (`heavy-tests --ignore`);
  test-heavy runs them, with coverage, in HEAVY_SHARDS shards, whenever a
  change can reach them:
    - all of src/ (HEAVY_INPUTS), catalog data included: their imports alone
      reach most of the application (the evidence file starts
      trusted_router.main in a subprocess), one of them parses every .py
      file under src/, and others start `python -c` and `python -m pytest`
      and read src/ files from git history;
    - heavy_closure(): from HEAVY_TESTS and tests/conftest.py (every test
      runs under it, and its fixtures run inside theirs), every module
      imported anywhere in a file, named as a string or imported by code in
      a string, and every file named by a path (a string, a path built from
      __file__, a variable or constants, a glob or a listing of a directory
      the file names, an f-string or concatenation whose constant start is a
      directory or a module), followed through every Python file reached,
      application code included. A test module the heavy tests only import
      helpers or fixtures from does not run its own tests there, so the
      bodies of its test functions count only when something in the
      closure names one;
    - every non-Python file under tests/ (fixtures, fakes), .test_durations
      (their split weights) and the Python toolchain.
  A glob or listing of a directory the file computes some other way (a
  parameter, an attribute) is taken to list one the file names, or one
  outside the repository. That gap, and any read the rules miss, is what
  testmap.py's `heavy` check closes: on every new per-test footprint map of
  main, every file the lane's tests ran or opened must be a change that runs
  the lane, at the same commit; a failure opens (or updates) an issue, and
  the next pass closes it.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import fnmatch
import json
import os
import posixpath
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Every file the refresh commits except parser code, Go alias vectors, and
# model_routing_evidence.json. Model/workload ordering refreshes deliberately
# require the complete matrix, including Python/Go equivalence tests. The
# social-card directory admits only what scripts/generate_provider_og.py
# writes: one PNG per provider and its manifest.
DATA_ONLY_PATHS: tuple[str, ...] = (
    "src/trusted_router/data/openrouter_snapshot.json",
    "src/trusted_router/data/openrouter_reasoning.json",
    "src/trusted_router/data/speech_pricing.json",
    "src/trusted_router/data/transcription_pricing.json",
    "src/trusted_router/data/provider_routing_evidence.json",
    "src/trusted_router/data/provider_models/*.json",
    "src/trusted_router/data/model_changes.jsonl",
    "src/trusted_router/data/model_changes_state.json",
    "src/trusted_router/static/og/providers/*.png",
    "src/trusted_router/static/og/providers/manifest.json",
)

# Jobs whose `if` in ci.yml carries DATA_ONLY_CONDITION. Keep in step with the
# workflow; test_ci_workflow derives the list from the workflow text and
# compares.
SKIPPED_FOR_DATA_ONLY: frozenset[str] = frozenset({
    "frontend",
    "spanner-emulator",
    "coverage",
    "proof-oracle",
    "byok-format-ordering",
    "fastpath",
    "fastpath-spanner",
    "fastpath-walks",
    "proofs",
})

DATA_ONLY_CONDITION = "needs.scope.outputs.data_only != 'true'"

# Jobs whose `if` in ci.yml starts with PUSH_SKIP_CONDITION: they skip on a
# push to main, and summarize() accepts that skip on a push only.
SKIPPED_ON_PUSH: frozenset[str] = frozenset({"test-post-cutover"})
# ci.yml's base-ci job: waits for the base's own push run to conclude success
# when the scope job took the data-only skips before it had (base_pending).
BASE_CI_JOB = "base-ci"
PUSH_SKIP_CONDITION = "github.event_name != 'push'"

# Jobs whose `if` in ci.yml carries path_condition(flag): they run when the
# flag is anything but `false`, and summarize() accepts their skip only when
# it is exactly `false`.
PATH_SCOPED_JOBS: dict[str, str] = {
    "proofs": "run_proofs",
    "proof-oracle": "run_proof_oracle",
    "test-post-cutover": "run_post_cutover",
    "test-heavy": "run_heavy",
    "fastpath": "run_fastpath",
    "fastpath-spanner": "run_fastpath",
    "fastpath-walks": "run_fastpath",
}
PATH_FLAGS: tuple[str, ...] = tuple(sorted(set(PATH_SCOPED_JOBS.values())))


def path_condition(flag: str) -> str:
    return f"needs.scope.outputs.{flag} != 'false'"


# A change to how CI itself decides what to run runs everything, and so does
# a .gitattributes anywhere: it decides what `git diff` shows the rules.
FULL_MATRIX_PATHS: tuple[str, ...] = (".github/workflows/ci.yml", "scripts/ci/**")
FULL_MATRIX_NAMES: tuple[str, ...] = (".gitattributes",)
# What every Python job runs with. A root conftest.py loads into every pytest
# session, and pytest.ini, tox.ini or setup.cfg take precedence over
# pyproject.toml's pytest configuration.
PYTHON_TOOLCHAIN_PATHS: tuple[str, ...] = (
    "pyproject.toml", "uv.lock", ".python-version", "conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
)

PROOFS_INPUTS: tuple[str, ...] = ("proofs/**",)
PROOFS_MANIFEST = "proofs/manifest.toml"

FASTPATH_INPUTS: tuple[str, ...] = (
    "fastpath/**",
    "proofs/**",
    "scripts/deploy/*.sh",
    "tests/fixtures/usage_cost_vectors.json",
)

# The oracle's test files (the files on the proof-oracle job's pytest
# command; test_ci_workflow holds the two together) and the harness pytest
# loads for every test.
ORACLE_TESTS: tuple[str, ...] = (
    "tests/test_async_settle_proof_oracle.py",
    "tests/test_async_settle_shadow_v2_gate_window.py",
)
TEST_HARNESS: tuple[str, ...] = ("tests/conftest.py",)
ORACLE_INPUTS: tuple[str, ...] = ("src/**", ".test_durations", ".test_durations-oracle")

POST_CUTOVER_INPUTS: tuple[str, ...] = (
    "src/**",
    "scripts/pricing/**",
    "scripts/update_model_changes.py",
    "fastpath/testdata/**",
    ".test_durations-post-cutover",
)
# Formats a test could load catalog expectations from: a change to one runs
# the whole post-cutover suite whatever its lines say.
DATA_SUFFIXES: tuple[str, ...] = (
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".txt", ".ini", ".cfg", ".xml",
)
LIFECYCLE_MODULE = "src/trusted_router/provider_lifecycle.py"
LIFECYCLE_IDENTIFIERS = re.compile(
    r"provider_lifecycle|lifecycle_clock|lifecycle_freeze|LIFECYCLE_CLOCK|catalog_predates"
    r"|CATALOG_CLOCK|CATALOG_RESOLVED_AT|latest_scheduled_cutover|effective_at|cutover|retir",
    re.IGNORECASE,
)

# One record of `git diff --raw` without -z: modes, abbreviated or full object
# ids, a status letter (a rename or copy carries a score, R100, which this
# does not match and so classifies false) and the path. Git quotes paths with
# unusual bytes in double quotes; a quoted path matches no data pattern.
RAW_RECORD = re.compile(
    r"^:(?P<old>[0-7]{6}) (?P<new>[0-7]{6}) [0-9a-f]+\.* [0-9a-f]+\.* (?P<status>[A-Z])\t(?P<path>[^\t]+)$"
)
ABSENT = "000000"
REGULAR_FILE = "100644"
REGULAR_MODES = frozenset({"100644", "100755"})
DATA_STATUSES = frozenset({"A", "M", "D"})


def is_data_path(path: str) -> bool:
    # The path is matched as git printed it: a name with whitespace in it
    # (git prints a trailing space unquoted) is not the allowlisted file.
    if not path or path != path.strip() or any(c.isspace() for c in path):
        return False
    if path.startswith("/") or ".." in path.split("/"):
        return False
    return any(
        fnmatch.fnmatchcase(path, pattern) and path.count("/") == pattern.count("/")
        for pattern in DATA_ONLY_PATHS
    )


def is_data_change(record: str) -> bool:
    """True for a raw record that adds, modifies or deletes a regular data file."""
    match = RAW_RECORD.match(record.rstrip("\n"))
    if match is None:
        return False
    modes = {match["old"], match["new"]}
    return (
        match["status"] in DATA_STATUSES
        and modes <= {ABSENT, REGULAR_FILE}
        and is_data_path(match["path"])
    )


def classify(records: list[str]) -> bool:
    """True only for a non-empty diff whose every record is a data change."""
    records = [record for record in records if record.strip()]
    return bool(records) and all(is_data_change(record) for record in records)


# ---------------------------------------------------------------------------
# Path-scoped jobs


@dataclass(frozen=True)
class Change:
    status: str
    path: str


def matches(path: str, pattern: str) -> bool:
    """`dir/**` is everything under dir; otherwise fnmatch at the same depth."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern) and path.count("/") == pattern.count("/")


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns)


def plain_change(record: str) -> Change | None:
    """The change a raw record makes, or None unless it is a plain add, modify
    or delete of a regular file (mode 100644 or 100755, unchanged)."""
    match = RAW_RECORD.match(record.rstrip("\n"))
    if match is None or match["path"].startswith('"'):
        return None
    status, old, new = match["status"], match["old"], match["new"]
    plain = (
        (status == "A" and old == ABSENT and new in REGULAR_MODES)
        or (status == "D" and new == ABSENT and old in REGULAR_MODES)
        or (status == "M" and old == new and old in REGULAR_MODES)
    )
    return Change(status, match["path"]) if plain else None


def plain_changes(records: Iterable[str]) -> list[Change] | None:
    """Every record's change, or None when any record is not a plain one."""
    changes: list[Change] = []
    for record in records:
        if not record.strip():
            continue
        change = plain_change(record)
        if change is None:
            return None
        changes.append(change)
    return changes


EMPTY_BLOB_INDEX = re.compile(r"^index (?:0+\.\.e69de29[0-9a-f]*|e69de29[0-9a-f]*\.\.0+)(?: [0-7]{6})?$")


def changed_lines(patch: str) -> dict[str, list[str] | None]:
    """Each file's added and removed lines in a `git diff -U0` patch, keyed by
    its `diff --git` header; None for a file with no hunk whose lines the
    patch does not show (a binary file). An empty file added or deleted has
    no hunk and no lines: []."""
    files: dict[str, list[str] | None] = {}
    header: str | None = None
    current: list[str] | None = None
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            header = line.removeprefix("diff --git ")
            files[header] = current = None
        elif header is None:
            continue
        elif current is None and EMPTY_BLOB_INDEX.match(line):
            files[header] = current = []
        elif line.startswith("@@"):
            if current is None:
                files[header] = current = []
        elif current is not None and line[:1] in ("+", "-"):
            current.append(line[1:])
    return files


def header_path(header: str) -> str:
    """The new path of a `diff --git a/X b/X` header."""
    return header.split(" b/", 1)[1] if " b/" in header else header


REPO_MODULE = re.compile(r"(?:tests|scripts)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def imported_modules(path: str, source: str) -> set[str]:
    """Modules a file imports, and repository module names it writes as strings
    (for importlib calls and lists of modules to load)."""
    tree = ast.parse(source, filename=path)
    names = statement_imports(path, tree)
    names.update(node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str) and REPO_MODULE.fullmatch(node.value))
    return names


def statement_imports(path: str, tree: ast.AST) -> set[str]:
    """The modules a parsed file's import statements name, wherever they are,
    each `from M import N` as both M and M.N (N may be a submodule). A file
    under src/ is a module of the package it is in there."""
    package = (path[4:] if path.startswith("src/") else path).removesuffix(".py").split("/")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def import_edges(root: Path, starts: Iterable[str]) -> dict[str, set[str]]:
    """Each tests/ and scripts/ file reachable from `starts`, mapped to what
    loading it loads: the files of the repository modules it imports (both
    candidate paths of each, so a module that resolves to no file, a deleted
    one, still has its paths) and the __init__.py of each of its packages."""
    edges: dict[str, set[str]] = {}
    pending = list(starts)
    while pending:
        path = pending.pop()
        if path in edges:
            continue
        targets: set[str] = set()
        edges[path] = targets
        parts = path.split("/")
        for depth in range(1, len(parts)):
            init = "/".join(parts[:depth]) + "/__init__.py"
            if (root / init).is_file():
                targets.add(init)
        file = root / path
        if file.is_file():
            for module in imported_modules(path, file.read_text(encoding="utf-8")):
                if module.split(".")[0] in ("tests", "scripts"):
                    base = module.replace(".", "/")
                    targets.update((f"{base}.py", f"{base}/__init__.py"))
        pending.extend(target for target in targets if target not in edges and (root / target).is_file())
    return edges


def python_closure(root: Path, starts: Iterable[str]) -> frozenset[str]:
    """The tests/ and scripts/ files `starts` import, transitively, with every
    package __init__.py on the way. A module that resolves to no file keeps
    its candidate paths, so deleting an imported module counts as a change to
    the closure."""
    edges = import_edges(root, starts)
    return frozenset(edges).union(*edges.values())


SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
GENERIC_NAMES = frozenset({"__init__.py", "conftest.py"})


def test_files(root: Path) -> list[str]:
    """The files pytest collects tests from under tests/ (test_*.py, *_test.py)."""
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("*.py")
                  if path.name.startswith("test_") or path.name.endswith("_test.py"))


def reaching_tests(root: Path, changed: Iterable[str]) -> dict[str, set[str]]:
    """For each changed Python file, the test files a change to it can affect:
    itself if it is one, every test file that imports it directly or through
    other modules (a directory's conftest.py counts as imported by the tests
    under it), and every test file whose source names it by its file name.
    A test that loads it by a computed name or path is not found."""
    changed = list(changed)
    tests = test_files(root)
    conftests = {test: [f"{'/'.join(test.split('/')[:depth])}/conftest.py"
                        for depth in range(2, test.count("/") + 1)] for test in tests}
    edges = import_edges(root, [*tests, *{c for cs in conftests.values() for c in cs if (root / c).is_file()}])
    for test, own in conftests.items():
        edges[test].update(own)
    dependents: dict[str, set[str]] = {}
    for path, targets in edges.items():
        for target in targets:
            dependents.setdefault(target, set()).add(path)
    sources: dict[str, str] = {}
    reach: dict[str, set[str]] = {}
    for path in changed:
        reached = {path}
        pending = [path]
        while pending:
            for dependent in dependents.get(pending.pop(), ()):
                if dependent not in reached:
                    reached.add(dependent)
                    pending.append(dependent)
        found = {test for test in tests if test in reached}
        name = path.rsplit("/", 1)[-1]
        if name not in GENERIC_NAMES:
            for test in tests:
                if test not in found:
                    if test not in sources:
                        sources[test] = (root / test).read_text(encoding="utf-8")
                    if name in sources[test]:
                        found.add(test)
        reach[path] = found
    return reach


def tests_reaching(root: Path, changed: Iterable[str]) -> list[str]:
    """Every test file a change to the Python files `changed` can affect."""
    return sorted(set().union(*reaching_tests(root, changed).values()))


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and (name.startswith("test_") or name.endswith("_test.py"))


def manifest_paths(root: Path) -> frozenset[str]:
    """The code and tests paths proofs/manifest.toml names for its specs."""
    manifest = tomllib.loads((root / PROOFS_MANIFEST).read_text(encoding="utf-8"))
    named: set[str] = set()
    for entry in manifest.values():
        if isinstance(entry, dict):
            for key in ("code", "tests"):
                named.update(path.rstrip("/") for path in entry.get(key, ()))
    if not named:
        raise ValueError(f"{PROOFS_MANIFEST} names no code or tests")
    return frozenset(named)


def scheduled_entities(source: str, now: datetime) -> frozenset[str]:
    """Models, upstream ids and providers whose catalog state differs between
    the default and the post-cutover lifecycle clocks, read from
    provider_lifecycle.py's source: every retirement still ahead of `now`
    (an effective time this cannot evaluate counts as ahead), and every
    string the module compares a provider, model or upstream id with (its
    date-switched prices). Raises on a structure it does not know."""
    tree = ast.parse(source)
    assigned: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assigned[node.target.id] = node.value

    def strings(node: ast.AST | None, seen: frozenset[str] = frozenset()) -> set[str]:
        found: set[str] = set()
        if node is None:
            return found
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.add(sub.value)
            elif isinstance(sub, ast.Name) and sub.id in assigned and sub.id not in seen:
                found |= strings(assigned[sub.id], seen | {sub.id})
        return found

    def instant(node: ast.expr, seen: frozenset[str] = frozenset()) -> datetime | None:
        if isinstance(node, ast.Name) and node.id in assigned and node.id not in seen:
            return instant(assigned[node.id], seen | {node.id})
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "datetime"):
            return None
        fields = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and type(arg.value) is int]
        zone = {keyword.arg: keyword.value for keyword in node.keywords}.get("tzinfo")
        if len(fields) != len(node.args) or not 3 <= len(fields) <= 6:
            return None
        if not (isinstance(zone, ast.Name) and zone.id == "UTC") or len(node.keywords) != 1:
            return None
        return datetime(*fields, tzinfo=UTC)

    def retirement_calls(node: ast.expr, seen: frozenset[str] = frozenset()) -> list[ast.Call]:
        if isinstance(node, ast.Name) and node.id in assigned and node.id not in seen:
            return retirement_calls(assigned[node.id], seen | {node.id})
        if not isinstance(node, ast.Tuple | ast.List):
            raise ValueError(f"_RETIREMENTS holds a {type(node).__name__}")
        calls: list[ast.Call] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                calls += retirement_calls(element.value, seen)
            elif isinstance(element, ast.Call) and isinstance(element.func, ast.Name) and element.func.id == "_Retirement":
                calls.append(element)
            else:
                raise ValueError(f"_RETIREMENTS holds a {type(element).__name__}")
        return calls

    if "_RETIREMENTS" not in assigned:
        raise ValueError("provider_lifecycle.py assigns no _RETIREMENTS")
    calls = retirement_calls(assigned["_RETIREMENTS"])
    if not calls:
        raise ValueError("_RETIREMENTS is empty")
    known = {"provider", "model_ids", "upstream_ids", "effective_at", "replacement_model_ids"}
    entities: set[str] = set()
    for call in calls:
        fields = {keyword.arg: keyword.value for keyword in call.keywords}
        if call.args or not {"provider", "model_ids", "effective_at"} <= set(fields) <= known:
            raise ValueError(f"_Retirement at line {call.lineno} has fields {sorted(map(str, fields))}")
        when = instant(fields["effective_at"])
        if when is not None and when <= now:
            continue
        entities |= strings(fields["provider"]) | strings(fields["model_ids"]) | strings(fields.get("upstream_ids"))
    subjects = {"provider_slug", "model_id", "upstream_id"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(isinstance(operand, ast.Name) and operand.id in subjects for operand in operands):
                for operand in operands:
                    if not (isinstance(operand, ast.Name) and operand.id in subjects):
                        entities |= strings(operand)
    return frozenset(entity for entity in entities if len(entity) >= 2 and not any(c.isspace() for c in entity))


def entity_pattern(entities: Iterable[str]) -> re.Pattern[str]:
    """A whole-token match of any entity; a slash may border it on either side."""
    alternatives = "|".join(re.escape(entity) for entity in sorted(entities, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_.-])(?:{alternatives})(?![A-Za-z0-9_.-])", re.IGNORECASE)


def proofs_reason(changes: list[Change], root: Path) -> str | None:
    named = manifest_paths(root)
    for change in changes:
        path = change.path
        if matches_any(path, PROOFS_INPUTS) or any(path == name or path.startswith(name + "/") for name in named):
            return path
    return None


def fastpath_reason(changes: list[Change]) -> str | None:
    return next((change.path for change in changes if matches_any(change.path, FASTPATH_INPUTS)), None)


def is_test_fixture(path: str) -> bool:
    return path.startswith("tests/") and not path.endswith(".py")


def oracle_reason(changes: list[Change], root: Path) -> str | None:
    closure = python_closure(root, (*ORACLE_TESTS, *TEST_HARNESS))
    for change in changes:
        path = change.path
        if matches_any(path, (*PYTHON_TOOLCHAIN_PATHS, *ORACLE_INPUTS)) and not is_data_path(path):
            return path
        if is_test_fixture(path) or path in closure:
            return path
    return None


# ---------------------------------------------------------------------------
# The heavy lane (test-heavy): test files moved out of the eight `test`
# shards, which pass each as --ignore; test-heavy runs them with coverage
# when run_heavy is set.

# The lane's test files, listed in scripts/ci/heavy_tests.txt (a list, not
# code: application boundary guards scan scripts/*.py for module names).
HEAVY_LIST = Path(__file__).resolve().with_name("heavy_tests.txt")
HEAVY_TESTS: tuple[str, ...] = tuple(
    line.strip() for line in HEAVY_LIST.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#"))
# The heavy lane's shards (ci.yml's test-heavy matrix; test_ci_workflow
# holds the two together).
HEAVY_SHARDS = 3
# All of src/, catalog data included (the docstring says why), and the
# lane's split weights.
HEAVY_INPUTS: tuple[str, ...] = ("src/**", ".test_durations")
# Directories a repository walk skips: not repository files.
WALK_SKIP: frozenset[str] = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".hypothesis", ".ruff_cache", ".mypy_cache",
    ".pytest_cache", ".security",
})
MODULE_ROOTS: tuple[str, ...] = ("src", "tests", "scripts")
PATH_TEXT = re.compile(r"[A-Za-z0-9_@+=-][A-Za-z0-9_.@+=/-]*|\.{1,2}/[A-Za-z0-9_.@+=/-]+")
GLOB_TEXT = re.compile(r"[A-Za-z0-9_.@+=/!*?\[\]-]*[*?\[][A-Za-z0-9_.@+=/!*?\[\]-]*")
FILE_SUFFIX = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
MODULE_TEXT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
CODE_TEXT = re.compile(r"^[ \t]*(?:from[ \t]+\S+[ \t]+)?import[ \t]", re.MULTILINE)
GLOB_CALLS = frozenset({"glob", "rglob", "iglob"})
LIST_CALLS = frozenset({"iterdir", "listdir", "scandir", "walk", "copytree"})
JOIN_CALLS = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath", "joinpath", "join"})
SAME_PATH_CALLS = frozenset({"resolve", "absolute", "expanduser", "realpath", "abspath", "normpath"})


class Tree:
    """The repository's files, read once: each file, each directory, and the
    module each Python file under src/, tests/ or scripts/ is."""

    def __init__(self, root: Path) -> None:
        self.root = root
        files: list[str] = []
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = sorted(name for name in subdirs if name not in WALK_SKIP)
            relative = os.path.relpath(directory, root)
            prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
            files.extend(prefix + name for name in names)
        self.files = sorted(files)
        self.file_set = frozenset(files)
        self.dirs = frozenset({"/".join(path.split("/")[:depth]) for path in files
                               for depth in range(1, path.count("/") + 1)})
        self.modules: dict[str, str] = {}
        for path in files:
            top = path.split("/", 1)[0]
            if path.endswith(".py") and top in MODULE_ROOTS:
                name = (path[4:] if top == "src" else path).removesuffix(".py").removesuffix("/__init__")
                if all(part.isidentifier() for part in name.split("/")):
                    self.modules[name.replace("/", ".")] = path

    def under(self, directory: str) -> list[str]:
        if not directory:
            return self.files
        # Sorted, and "/" sorts just before "0": the paths under the
        # directory are one slice.
        prefix = directory.rstrip("/") + "/"
        return self.files[bisect.bisect_left(self.files, prefix):bisect.bisect_left(self.files, prefix[:-1] + "0")]

    def module_files(self, name: str) -> set[str]:
        """The files importing `name` executes: each package's __init__.py and
        the module, or for a name under tests or scripts that resolves to no
        file, both candidate paths (a deleted module is still a change)."""
        parts = name.split(".")
        found = {self.modules[".".join(parts[:depth])] for depth in range(1, len(parts) + 1)
                 if ".".join(parts[:depth]) in self.modules}
        if parts[0] in ("tests", "scripts") and name not in self.modules:
            base = "/".join(parts)
            found |= {f"{base}.py", f"{base}/__init__.py"}
        return found


@dataclass(frozen=True)
class Place:
    """Where a path expression points: a repository path, and whether an
    unknown part follows it (everything under it, then)."""
    path: str
    wild: bool = False


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(places: list[Place], text: str) -> list[Place]:
    out = []
    for place in places:
        if place.wild:
            out.append(place)
            continue
        joined = posixpath.normpath(f"{place.path}/{text}" if place.path else text)
        if joined == ".":
            joined = ""
        if not joined.startswith(".."):
            out.append(Place(joined))
    return out


class Names:
    """What one Python file names by a string: repository files and
    directories (expanded to the files under them), modules, and globs."""

    def __init__(self, tree: Tree, path: str) -> None:
        self.tree = tree
        self.path = path
        own = _parent(path)
        self.ancestors = [own]
        while own:
            own = _parent(own)
            self.ancestors.append(own)
        self.variables: dict[str, list[Place]] = {}
        self.choices: dict[str, list[str]] = {}
        self.found: set[str] = set()  # files named as paths (read, or run as scripts)
        self.modules_found: set[str] = set()  # files of modules named as strings
        self.consumed: set[int] = set()
        self.named_dirs: set[str] = set()
        self.unplaced: list[str] = []

    # Places an expression points at; None when it is not a path the file
    # builds from __file__, a variable or constants.
    def places(self, node: ast.AST) -> list[Place] | None:
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return [Place(self.path)]
            return self.variables.get(node.id)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.consumed.add(id(node))
            if not PATH_TEXT.fullmatch(node.value):
                return None
            return _join([Place(base) for base in self.ancestors], node.value)
        if isinstance(node, ast.Attribute):
            if node.attr == "parent":
                inner = self.places(node.value)
                return None if inner is None else [p if p.wild else Place(_parent(p.path)) for p in inner]
            return None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            inner = self.places(node.value.value)
            index = node.slice
            if inner is None or not (isinstance(index, ast.Constant) and type(index.value) is int):
                return None
            out = []
            for place in inner:
                path = place.path
                for _ in range(index.value + 1):
                    path = _parent(path)
                out.append(place if place.wild else Place(path))
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self.places(node.left)
            if left is None:
                # An unplaced base: constants after it are a path relative to
                # the root or a directory above the file; anything else is
                # not a path this can follow.
                if isinstance(node.left, ast.BinOp) or not (
                        isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
                    return None
                left = [Place(base) for base in self.ancestors]
            return self.extend(left, [node.right])
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in SAME_PATH_CALLS and isinstance(func, ast.Attribute) and not node.args:
                return self.places(func.value)
            if name in SAME_PATH_CALLS and node.args:
                return self.places(node.args[0])
            if name == "dirname" and node.args:
                inner = self.places(node.args[0])
                return None if inner is None else [p if p.wild else Place(_parent(p.path)) for p in inner]
            if name in JOIN_CALLS and node.args:
                if name == "joinpath" and isinstance(func, ast.Attribute):
                    start = self.places(func.value)
                    return None if start is None else self.extend(start, node.args)
                if name == "join" and not (isinstance(func, ast.Attribute) and (
                        isinstance(func.value, ast.Attribute) and func.value.attr == "path"
                        or isinstance(func.value, ast.Name) and func.value.id in ("posixpath", "ntpath"))):
                    return None  # str.join and the like
                first = self.places(node.args[0])
                return None if first is None else self.extend(first, node.args[1:])
        return None

    def extend(self, start: list[Place], parts: list[ast.expr]) -> list[Place]:
        places = start
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                self.consumed.add(id(part))
                if GLOB_TEXT.fullmatch(part.value):
                    for place in places:
                        self.glob(part.value, [place.path], recursive=False)
                    return []
                places = _join(places, part.value) if PATH_TEXT.fullmatch(part.value) else []
            else:
                places = [Place(place.path, wild=True) for place in places]
        return places

    def take(self, places: list[Place]) -> None:
        for place in places:
            if place.wild:
                # An unknown part under the repository root, or under a
                # directory holding all of src/, tests/ or scripts/, is left to
                # the string rules (each name the file builds it from is a
                # string somewhere); under any narrower directory it is
                # everything under it.
                if place.path and place.path not in MODULE_ROOTS:
                    self.found.update(self.tree.under(place.path))
                    self.named_dirs.add(place.path)
            elif place.path in self.tree.file_set:
                self.found.add(place.path)
            elif place.path in self.tree.dirs:
                # A directory is a base for other paths; its files count
                # when something lists or globs it.
                self.named_dirs.add(place.path)
            elif place.path and FILE_SUFFIX.search(place.path):
                # A file the code names that does not exist at HEAD: one a
                # change deleted or renamed away (its deletion record must run
                # the lane), or one a change may add. Kept wherever it is.
                self.found.add(place.path)

    def glob(self, pattern: str, directories: list[str] | None, *, recursive: bool) -> None:
        """Files a glob (or a listing: pattern "*") can match under each
        directory it is called on; on a directory this cannot place, see
        finish()."""
        del recursive  # Path.glob and rglob alike: matching anywhere under is a superset.
        if directories is None:
            self.unplaced.append(pattern)
            return
        for base in directories:
            for path in self.tree.under(base):
                relative = path[len(base) + 1:] if base else path
                name = relative.rsplit("/", 1)[-1]
                if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative, pattern) \
                        or fnmatch.fnmatchcase(relative, f"*/{pattern}"):
                    self.found.add(path)

    def finish(self) -> None:
        """Globs and listings of a directory this cannot place. A pattern
        with a "/" places itself (relative to the root or a directory above
        the file); any other is taken to list one of the directories the file
        names (narrower than src/, tests/ or scripts/) or one outside the
        repository (a temporary directory). A listing of a directory the file
        computes some other way is the documented gap the footprint check
        covers."""
        dirs = sorted(d for d in self.named_dirs if d and d not in MODULE_ROOTS)
        for pattern in self.unplaced:
            if "/" in pattern:
                for base in self.ancestors:
                    for path in self.tree.under(base):
                        if fnmatch.fnmatchcase(path[len(base) + 1:] if base else path, pattern):
                            self.found.add(path)
            else:
                self.glob(pattern, dirs, recursive=True)

    def pattern(self, node: ast.JoinedStr | ast.BinOp) -> None:
        """An f-string or a concatenation of strings: the files and modules
        whose names it can spell, when its constant parts name a module of
        the repository or a path."""
        # (text, dynamic): a constant, or a regex for a part only known at run
        # time (a loop variable over literal strings: exactly those).
        parts: list[tuple[str, bool]] = []

        def flatten(sub: ast.AST) -> None:
            if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add):
                flatten(sub.left)
                flatten(sub.right)
            elif isinstance(sub, ast.JoinedStr):
                for value in sub.values:
                    flatten(value)
            elif isinstance(sub, ast.FormattedValue) and sub.format_spec is None and sub.conversion == -1:
                flatten(sub.value)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                self.consumed.add(id(sub))
                parts.append((sub.value, False))
            elif isinstance(sub, ast.Name) and sub.id in self.choices:
                parts.append(("(?:" + "|".join(map(re.escape, self.choices[sub.id])) + ")", True))
            else:
                parts.append((".*", True))

        flatten(node)
        if not parts or parts[0][1] or not any(is_dynamic for _, is_dynamic in parts):
            return
        regex = re.compile("".join(text if is_dynamic else re.escape(text) for text, is_dynamic in parts), re.DOTALL)
        lead = parts[0][0]
        if lead.split(".", 1)[0] in ("trusted_router", "tests", "scripts") and "." in lead:
            for name in self.tree.modules:
                if regex.fullmatch(name):
                    self.modules_found |= self.tree.module_files(name)
        # A path: its constant start names a directory (and perhaps the start
        # of a file name in it), relative to the root or a directory above.
        directory = lead.rsplit("/", 1)[0] if "/" in lead else ""
        if directory and PATH_TEXT.fullmatch(directory):
            for base in self.ancestors:
                start = posixpath.normpath(f"{base}/{directory}" if base else directory)
                if start in self.tree.dirs:
                    for path in self.tree.under(start):
                        if regex.fullmatch(path[len(base) + 1:] if base else path):
                            self.found.add(path)

    @staticmethod
    def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
        """The nodes of one scope: everything under it but the bodies of the
        functions defined in it (yielded themselves, to be scanned in turn)."""
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            yield node
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                pending.extend(ast.iter_child_nodes(node))

    def literal_strings(self, node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Name):
            return self.choices.get(node.id)
        if isinstance(node, ast.Tuple | ast.List | ast.Set) and node.elts and all(
                isinstance(element, ast.Constant) and isinstance(element.value, str) for element in node.elts):
            return [element.value for element in node.elts]  # type: ignore[attr-defined]
        return None

    def scope(self, scope: ast.AST, outer: tuple[dict[str, list[Place]], dict[str, list[str]]]) -> None:
        """One module or function: its variables bound to paths and its loop
        variables over literal strings (a parameter shadows the enclosing
        binding: unknown), then the paths, globs and patterns it builds, then
        the functions defined in it."""
        self.variables, self.choices = dict(outer[0]), dict(outer[1])
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            arguments = scope.args
            for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                        *filter(None, [arguments.vararg, arguments.kwarg])]:
                self.variables.pop(arg.arg, None)
                self.choices.pop(arg.arg, None)
        nodes = list(self.own_nodes(scope))
        consumed = set(self.consumed)
        for _ in range(2):  # twice, so one binding can build on another
            for node in nodes:
                if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None \
                        and not isinstance(node.value, ast.Constant):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    places = self.places(node.value)
                    strings = self.literal_strings(node.value)
                    for target in targets:
                        if isinstance(target, ast.Name) and places is not None:
                            self.variables[target.id] = places
                        if isinstance(target, ast.Name) and strings is not None:
                            self.choices[target.id] = strings
                elif isinstance(node, ast.comprehension | ast.For) and isinstance(node.target, ast.Name):
                    strings = self.literal_strings(node.iter)
                    if strings is not None:
                        self.choices[node.target.id] = strings
        self.consumed = consumed
        for node in nodes:
            if id(node) in self.consumed:
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id
                if name in GLOB_CALLS and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    self.consumed.add(id(node.args[0]))
                    receiver = self.places(func.value) if isinstance(func, ast.Attribute) else None
                    known = None if receiver is None else [p.path for p in receiver]
                    self.glob(node.args[0].value, known, recursive=name == "rglob")
                    continue
                if name in LIST_CALLS:
                    target = func.value if name == "iterdir" and isinstance(func, ast.Attribute) else \
                        node.args[0] if node.args else None
                    receiver = None if target is None else self.places(target)
                    self.glob("*", None if receiver is None else [p.path for p in receiver], recursive=True)
                    if target is not None:
                        self.consumed.update(id(sub) for sub in ast.walk(target))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) or isinstance(node, ast.Call):
                places = self.places(node)
                if places is not None:
                    self.take(places)
                    self.consumed.update(id(sub) for sub in ast.walk(node) if isinstance(sub, ast.BinOp | ast.Call))
                    continue
            if isinstance(node, ast.JoinedStr) or isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                self.pattern(node)
                self.consumed.update(id(sub) for sub in ast.walk(node))
        bindings = (self.variables, self.choices)
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                self.scope(node, bindings)

    def scan(self, tree: ast.AST) -> set[str]:
        """Everything the parsed file names; its import statements are the
        caller's (heavy_closure follows them)."""
        self.scope(tree, ({}, {}))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in self.consumed:
                continue
            text = node.value
            if MODULE_TEXT.fullmatch(text) and text.split(".", 1)[0] in ("trusted_router", "tests", "scripts"):
                self.modules_found |= self.tree.module_files(text)
            if "\n" in text and CODE_TEXT.search(text):
                # Code in a string (a subprocess's program): what it imports
                # and names. Code that does not parse names nothing.
                program = _parse(text, self.path)
                if program is not None:
                    inner = Names(self.tree, self.path)
                    inner.scan(program)
                    self.found |= inner.found
                    self.modules_found |= inner.modules_found
                    for module in statement_imports(self.path, program):
                        self.modules_found |= self.tree.module_files(module)
            elif GLOB_TEXT.fullmatch(text) and len(re.findall(r"[A-Za-z0-9]", re.sub(r"\[[^\]]*\]", "", text))) >= 2:
                self.glob(text, None, recursive=True)
            elif PATH_TEXT.fullmatch(text) and ("/" in text or FILE_SUFFIX.search(text)):
                self.take(_join([Place(base) for base in self.ancestors], text))
        self.finish()
        return self.found | self.modules_found


def _parse(source: str, path: str) -> ast.AST | None:
    try:
        return ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return None


_CLOSURES: dict[tuple[str, int], frozenset[str]] = {}  # (root, what the closure reads) -> closure


def heavy_closure(root: Path, tree: Tree | None = None) -> frozenset[str]:
    """Every repository file the heavy tests can load or read, by these rules,
    from HEAD's sources: start at HEAVY_TESTS and tests/conftest.py; from each
    Python file, follow the modules it imports and the package __init__.py of
    each (imports anywhere, module names written as strings, code written in
    a string), and take the files it names by a string: a path or a directory
    (everything under it) relative to the repository root or any directory
    above the file, a path it builds from __file__, a variable or constants
    (an unknown part under a directory narrower than tests/ or scripts/ is
    everything under it), a glob (under the directory it is called on, or
    anywhere), and an f-string or concatenation whose constant parts name a
    module or a path. Every Python file reached is read in turn, src/
    included, so a path application code names (a script it runs, a fixture
    it hashes) counts as well."""
    tree = tree or Tree(root)
    # The same tree (every name, every Python file's size and time) gives the
    # same closure: the test suite asks about this repository many times.
    key = (str(root.resolve()), hash((HEAVY_TESTS, TEST_HARNESS, tuple(tree.files), tuple(
        (lambda stat: (stat.st_size, stat.st_mtime_ns))((root / path).stat())
        for path in tree.files if path.endswith(".py")))))
    if key in _CLOSURES:
        return _CLOSURES[key]
    heavy = frozenset(HEAVY_TESTS)
    seen: set[str] = set()
    emptied: dict[str, set[str]] = {}  # test module -> the tests whose bodies were left out
    kept: dict[str, frozenset[str]] = {}  # test module -> the tests something calls
    identifiers: dict[str, set[str]] = {}
    named_as_paths: set[str] = set()

    def read(path: str, keep: frozenset[str] | None) -> set[str]:
        """What loading `path` loads and names. keep=None reads it whole;
        otherwise the bodies of its tests other than `keep` are left out."""
        targets: set[str] = set()
        parts = path.split("/")
        for depth in range(1, len(parts)):
            init = "/".join(parts[:depth]) + "/__init__.py"
            if init in tree.file_set:
                targets.add(init)
        parsed = _parse((root / path).read_text(encoding="utf-8", errors="replace"), path)
        if parsed is None:
            # What it imports is unknown: scope_jobs runs the lane.
            raise ValueError(f"{path} does not parse")
        if keep is not None:
            emptied[path] = empty_test_bodies(parsed, keep)
        else:
            emptied.pop(path, None)
        for module in statement_imports(path, parsed):
            targets |= tree.module_files(module)
        names = Names(tree, path)
        targets |= names.scan(parsed)
        named_as_paths.update(names.found)
        identifiers[path] = referenced_identifiers(parsed)
        return targets

    def reached(targets: set[str]) -> None:
        pending.extend(sorted(targets - seen))

    pending = [*HEAVY_TESTS, *TEST_HARNESS]
    while pending:
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if path.endswith(".py") and path in tree.file_set:
                # A test module the heavy tests import (for a helper or a
                # fixture), not one they run: its tests do not run, unless
                # something calls one by name or reads the file as a path.
                partial = is_test_file(path) and path not in heavy and path not in named_as_paths
                reached(read(path, frozenset() if partial else None))
        called = set().union(*identifiers.values()) if identifiers else set()
        for path, names in list(emptied.items()):
            if path in named_as_paths:
                reached(read(path, None))
            elif names & called:
                kept[path] = kept.get(path, frozenset()) | (names & called)
                reached(read(path, kept[path]))
    _CLOSURES[key] = frozenset(seen)
    return _CLOSURES[key]


def empty_test_bodies(tree: ast.Module, keep: frozenset[str]) -> set[str]:
    """Leave out the bodies of the tests a parsed module defines (top-level
    test* functions, Test* classes' test* methods), except those named in
    keep, and return the names left out. Decorators, default values and class
    bodies run when the module is imported and stay."""
    left_out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            if node.name not in keep:
                node.body = [ast.Pass()]
                left_out.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test") and node.name not in keep:
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name.startswith("test"):
                    item.body = [ast.Pass()]
            left_out.add(node.name)
    return left_out


def referenced_identifiers(tree: ast.AST) -> set[str]:
    """Every name a parsed file can call something by: names, attributes,
    imported names and identifier strings (getattr, monkeypatch targets)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value) if len(node.value) < 200 else ())
    return found


def heavy_covers(path: str, closure: frozenset[str]) -> bool:
    """Whether a change to `path` runs the heavy lane, given heavy_closure()."""
    return (matches_any(path, (*PYTHON_TOOLCHAIN_PATHS, *HEAVY_INPUTS)) or is_test_fixture(path)
            or path in closure)


def heavy_reason(changes: list[Change], root: Path) -> str | None:
    closure = heavy_closure(root)
    return next((change.path for change in changes if heavy_covers(change.path, closure)), None)


def post_cutover_plan(
    changes: list[Change], root: Path, event: str, lines: str | None, now: datetime,
) -> tuple[str | None, tuple[str, ...]]:
    """Why the post-cutover suite runs (None: it skips) and the test files it
    is limited to (empty: the whole suite)."""
    whole: tuple[str, ...] = ()
    if event == "workflow_dispatch":
        return "a dispatch (the price refresh's publication gate) always runs it", whole
    harness = python_closure(root, TEST_HARNESS)
    for change in changes:
        path = change.path
        if matches_any(path, (*PYTHON_TOOLCHAIN_PATHS, *POST_CUTOVER_INPUTS)) or is_test_fixture(path) or path in harness:
            return path, whole
    # Nothing under src/ changed from here on, and no file under tests/ that
    # is not Python. A data file a test could load expectations from, through
    # a path it builds: the whole suite.
    python = [change for change in changes if change.path.endswith(".py")]
    others = [change.path for change in changes if not change.path.endswith(".py")]
    for path in others:
        if path.lower().endswith(DATA_SUFFIXES) and not path.startswith(".github/"):
            return f"{path} is data a test could load", whole
    # Any other file, and Python: a changed line naming the lifecycle, or a
    # file whose lines the patch does not show, runs the whole suite.
    named = [*others, *(change.path for change in python)]
    if lines is None:
        return f"{named[0]} (no diff text to read)", whole
    files = {header_path(header): text for header, text in changed_lines(lines).items()}
    entities = entity_pattern(scheduled_entities((root / LIFECYCLE_MODULE).read_text(encoding="utf-8"), now))
    for path in named:
        if path not in files or files[path] is None:
            return f"{path} has no lines the diff shows", whole
        for line in files[path] or ():
            found = LIFECYCLE_IDENTIFIERS.search(line) or entities.search(line)
            if found:
                return f"{path} names {found.group(0)!r}", whole
    if not python:
        return None, whole
    # Python outside src/: the tests each file reaches. A file that reaches
    # none may be loaded by a computed name or path: the whole suite. A
    # deleted test file is no test to run.
    reach = reaching_tests(root, [change.path for change in python])
    for change in python:
        if not reach[change.path] and not (change.status == "D" and is_test_file(change.path)):
            return f"{change.path} reaches no test file that imports or names it", whole
    selected = sorted(set().union(*reach.values()))
    if not selected:
        return None, whole
    reason = f"{len(selected)} test files reach {python[0].path}"
    if len(selected) > len(test_files(root)) // 8 or not all(SAFE_PATH.fullmatch(path) for path in selected):
        return f"{reason}, so the whole suite", whole
    return reason, tuple(selected)


def everything(reason: str) -> tuple[dict[str, bool], list[str], tuple[str, ...]]:
    return dict.fromkeys(PATH_FLAGS, True), [f"every path-scoped job runs: {reason}"], ()


def scope_jobs(
    records: Iterable[str], *, root: Path, event: str, lines: str | None, now: datetime,
) -> tuple[dict[str, bool], list[str], tuple[str, ...]]:
    """Which path-scoped flags must run, a line for the log per flag, and the
    test files the post-cutover suite is limited to (empty: the whole suite)."""
    changes = plain_changes(records)
    if changes is None:
        return everything("a record is not an add, modify or delete of a regular file")
    if not changes:
        return everything("the comparison is empty")
    governing = next((change.path for change in changes if matches_any(change.path, FULL_MATRIX_PATHS)
                      or change.path.rsplit("/", 1)[-1] in FULL_MATRIX_NAMES), None)
    if governing is not None:
        return everything(f"{governing} decides what CI runs")
    selected: tuple[str, ...] = ()

    def post_cutover() -> str | None:
        nonlocal selected
        reason, selected = post_cutover_plan(changes, root, event, lines, now)
        return reason

    rules: dict[str, Callable[[], str | None]] = {
        "run_fastpath": lambda: fastpath_reason(changes),
        "run_heavy": lambda: heavy_reason(changes, root),
        "run_post_cutover": post_cutover,
        "run_proof_oracle": lambda: oracle_reason(changes, root),
        "run_proofs": lambda: proofs_reason(changes, root),
    }
    flags: dict[str, bool] = {}
    notes: list[str] = []
    for flag in PATH_FLAGS:
        try:
            reason = rules[flag]()
        except Exception as error:  # a rule that cannot read its inputs runs its jobs
            reason = f"its inputs could not be derived ({type(error).__name__}: {error})"
            notes.append(f"::warning title=CI scope::{flag}: {reason}")
        flags[flag] = reason is not None
        notes.append(f"{flag}: runs, {reason}" if reason is not None else f"{flag}: skips, none of its inputs changed")
    if selected:
        notes += [f"  post-cutover test: {path}" for path in selected]
    return flags, notes, selected


def summarize(needs: dict[str, dict[str, object]], data_only: str, event_name: str = "") -> list[str]:
    """Return the reasons the run must fail; an empty list means success."""
    failures: list[str] = []
    scope = needs.get("scope", {})
    if scope.get("result") != "success":
        failures.append(f"scope: {scope.get('result')}")
    outputs = scope.get("outputs")
    flags = outputs if isinstance(outputs, dict) else {}
    for job, info in sorted(needs.items()):
        if job == "scope":
            continue
        result = info.get("result")
        if result == "success":
            continue
        if result == "skipped" and job in SKIPPED_FOR_DATA_ONLY and data_only == "true":
            continue
        if result == "skipped" and job in PATH_SCOPED_JOBS and flags.get(PATH_SCOPED_JOBS[job]) == "false":
            continue
        if result == "skipped" and job in SKIPPED_ON_PUSH and event_name == "push":
            continue
        # base-ci runs only when the data-only skips rest on a base whose push
        # run had not yet concluded; it may skip only when they do not.
        if result == "skipped" and job == BASE_CI_JOB and flags.get("base_pending") != "true":
            continue
        failures.append(f"{job}: {result}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("classify", help="stdin: git diff --no-renames --raw; stdout: true|false")
    jobs = sub.add_parser("jobs", help="stdin: git diff --no-renames --raw REF HEAD; stdout: <flag>=true|false")
    jobs.add_argument("--event", default="", help="github.event_name")
    jobs.add_argument("--lines", type=Path, help="git diff -U0 REF HEAD outside src/")
    jobs.add_argument("--full", action="store_true", help="every flag true; stdin is not read")
    sub.add_parser("summarize", help="env NEEDS (JSON), DATA_ONLY, EVENT_NAME; exit 1 on any failure")
    heavy = sub.add_parser("heavy-tests", help="the heavy lane's test files, as one line of pytest arguments")
    mode = heavy.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ignore", action="store_true", help="--ignore=<file> for each (the test job)")
    mode.add_argument("--files", action="store_true", help="each file that exists (the test-heavy job)")
    args = parser.parse_args(argv)
    if args.command == "heavy-tests":
        # The line is word-split into arguments, so every path must be safe.
        assert all(SAFE_PATH.fullmatch(path) for path in HEAVY_TESTS), HEAVY_TESTS
        if args.ignore:
            print(" ".join(f"--ignore={path}" for path in HEAVY_TESTS))
            return 0
        present = [path for path in HEAVY_TESTS if Path(path).is_file()]
        if not present:
            # pytest with no file arguments would run the whole suite.
            print("::error title=Heavy lane::none of commit_scope.HEAVY_TESTS exists", file=sys.stderr)
            return 1
        print(" ".join(present))
        return 0
    if args.command == "classify":
        # Records are split on "\n" alone: str.splitlines also splits on
        # U+0085, U+2028 and U+2029, which git prints literally in a path
        # when core.quotePath is off, and that would hide them from the
        # record check.
        print("true" if classify(sys.stdin.read().split("\n")) else "false")
        return 0
    if args.command == "jobs":
        if args.full:
            flags, notes, selected = everything("the scope job asked for the complete matrix")
        else:
            lines: str | None = None
            if args.lines is not None:
                try:
                    lines = args.lines.read_text(encoding="utf-8", errors="replace")
                except OSError as error:
                    print(f"::warning title=CI scope::{args.lines} is unreadable ({error})", file=sys.stderr)
            flags, notes, selected = scope_jobs(sys.stdin.read().split("\n"), root=Path.cwd(),
                                                event=args.event, lines=lines, now=datetime.now(UTC))
        for note in notes:
            print(note, file=sys.stderr)
        for flag in PATH_FLAGS:
            print(f"{flag}={'true' if flags[flag] else 'false'}")
        print(f"post_cutover_tests={' '.join(selected)}")
        return 0
    needs = json.loads(os.environ["NEEDS"])
    data_only = os.environ.get("DATA_ONLY", "")
    event_name = os.environ.get("EVENT_NAME", "")
    for job, info in sorted(needs.items()):
        print(f"{job}: {info.get('result')}")
    print(f"data_only: {data_only or '<unset>'}")
    print(f"event_name: {event_name or '<unset>'}")
    outputs = needs.get("scope", {}).get("outputs")
    for flag in PATH_FLAGS:
        value = outputs.get(flag) if isinstance(outputs, dict) else None
        print(f"{flag}: {value or '<unset>'}")
    failures = summarize(needs, data_only, event_name)
    for failure in failures:
        print(f"::error title=CI job did not succeed::{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
