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
  summarize  reads the `needs` context of the summary job (JSON in NEEDS)
             and the scope output (DATA_ONLY) and exits non-zero unless every
             job succeeded, allowing a skipped job only when it is one of the
             jobs ci.yml skips for a data-only commit AND the commit was
             data-only, or when it is path-scoped (PATH_SCOPED_JOBS) and the
             scope job's output for its flag is exactly `false`. A heavy job
             skipped while its flag is not `false`, a failed scope job, or a
             job in any other state fails the run.

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
pull requests, pushes to main and dispatches alike, whenever one of those
files differs between REF and HEAD, and skip otherwise. There is no scheduled
run: a job runs when something it reads changed, and pushes to main apply the
same rules as pull requests, so two green pull requests that combine into a
red main are caught by the push that combines them. A pull request's REF is
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
  imports) or a test fixture; for any other changed file that is not Python,
  Markdown or under .github/ (a test may read it through a path it builds,
  say a JSON of catalog expectations); for a changed line in a Markdown,
  .github/ or Python file that names a lifecycle identifier
  (LIFECYCLE_IDENTIFIERS) or a scheduled entity (a model, upstream id or
  provider with a retirement still ahead, or one whose price
  provider_lifecycle.py switches by date, read from HEAD at scope time), or a
  file whose lines the diff cannot show; for a changed Python file that no
  test imports or names (a test may load it by a computed name or path); and
  on every dispatch, the price refresh's publication gate. A test is code
  that can depend on the catalog without naming anything (a count over every
  model), so when only Python outside src/ (tests, helpers, scripts) changed
  otherwise, the suite runs, in one shard, on the tests that can reach it:
  each changed test file, every test that imports a changed module directly
  or through other modules (read from HEAD's sources, a directory's
  conftest.py counted as imported by its tests), and every test whose source
  names a changed file. More of those than an eighth of the test files runs
  the whole suite instead.

  Two gaps are accepted trade-offs. A helper that some tests import and
  another test loads by a computed name selects only the importers. And a
  Markdown change that names nothing scheduled runs nothing: prose documents
  do not feed the catalog expectations tests assert. A test such a change
  breaks past the cutovers goes red on the next change that runs it, or at
  the cutover.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import tomllib
from collections.abc import Callable, Iterable
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

# Jobs whose `if` in ci.yml carries path_condition(flag): they run when the
# flag is anything but `false`, and summarize() accepts their skip only when
# it is exactly `false`.
PATH_SCOPED_JOBS: dict[str, str] = {
    "proofs": "run_proofs",
    "proof-oracle": "run_proof_oracle",
    "test-post-cutover": "run_post_cutover",
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
ORACLE_INPUTS: tuple[str, ...] = ("src/**", ".test_durations")

POST_CUTOVER_INPUTS: tuple[str, ...] = (
    "src/**",
    "scripts/pricing/**",
    "scripts/update_model_changes.py",
    "fastpath/testdata/**",
    ".test_durations-post-cutover",
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
    package = path.removesuffix(".py").split("/")[:-1]
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and REPO_MODULE.fullmatch(node.value):
            names.add(node.value)
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
    # is not Python. Any other file but Markdown or one under .github/ may be
    # read by a test through a path it builds: the whole suite.
    python = [change for change in changes if change.path.endswith(".py")]
    others = [change.path for change in changes if not change.path.endswith(".py")]
    for path in others:
        if not (path.endswith(".md") or path.startswith(".github/")):
            return f"{path} may be read by a test through a constructed path", whole
    # Markdown, .github/ and Python: a changed line naming the lifecycle, or
    # a file whose lines the patch does not show, runs the whole suite.
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


def summarize(needs: dict[str, dict[str, object]], data_only: str) -> list[str]:
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
    sub.add_parser("summarize", help="env NEEDS (JSON), DATA_ONLY; exit 1 on any failure")
    args = parser.parse_args(argv)
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
    for job, info in sorted(needs.items()):
        print(f"{job}: {info.get('result')}")
    print(f"data_only: {data_only or '<unset>'}")
    outputs = needs.get("scope", {}).get("outputs")
    for flag in PATH_FLAGS:
        value = outputs.get(flag) if isinstance(outputs, dict) else None
        print(f"{flag}: {value or '<unset>'}")
    failures = summarize(needs, data_only)
    for failure in failures:
        print(f"::error title=CI job did not succeed::{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
