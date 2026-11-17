"""scripts/ci/testmap.py: the footprint map's tools and the shadow selection's rules."""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.ci import commit_scope as cs
from scripts.ci import testmap as tm

# ---------------------------------------------------------------- a map, built by hand


def build_map(tests: dict[str, dict], *, collect: dict | None = None, cached: dict | None = None,
              glob: dict | None = None, fixtures: dict[str, list[dict]] | None = None,
              universe_funcs: set[str] = frozenset(), universe_files: set[str] = frozenset(),
              commit: str = "a" * 40) -> dict:
    """tests: nodeid -> footprint {"f": [...], "o": [...], "d": [...], "s": [...]};
    fixtures: nodeid -> list of fixture footprints that test inherits."""
    interner = tm.Interner()

    def fp(part: dict | None) -> int:
        part = part or {}
        return interner.fp(part.get(k, ()) for k in tm.PARTS)

    rows = [[t, fp(f), sorted({fp(x) for x in (fixtures or {}).get(t, [])})] for t, f in sorted(tests.items())]
    return {
        "schema": tm.SCHEMA_VERSION, "recorder": tm.RECORDER_VERSION, "commit": commit, "ref": "refs/heads/main",
        "event": "push", "run_id": "1", "shards": tm.SHARDS, "fragments": list(range(1, tm.SHARDS + 1)),
        "created": "2026-11-17T00:00:00Z", "keys": interner.keys, "fps": interner.fps, "tests": rows,
        "collect": {m: fp(f) for m, f in (collect or {}).items()},
        "cached": {c: fp(f) for c, f in (cached or {}).items()}, "memoized": sorted(cached or {}),
        "global": fp(glob), "universe_files": sorted(universe_files), "universe_funcs": sorted(universe_funcs),
    }


SRC = "src/app/core.py"
OTHER = "src/app/other.py"
UNIVERSE_FILES = {SRC, OTHER, "tests/helper.py", "tests/conftest.py", "scripts/tool.py"}
UNIVERSE_FUNCS = {f"{SRC}:work", f"{SRC}:Thing.run", f"{SRC}:helper", f"{OTHER}:other", f"{OTHER}:reads",
                  f"{SRC}:boot", "tests/conftest.py:client", "tests/helper.py:make"}


def run_select(tmap: dict, changes: list[tuple[str, str]], *, py: dict[str, tm.PyChange] | None = None,
               index: tm.NameIndex | None = None, root: Path | None = None,
               weights: dict[str, float] | None = None) -> tm.Selection:
    root = root or Path("/nonexistent")
    m = tm.TestMap(tmap)
    weights = weights or {t[0]: 1.0 for t in tmap["tests"]}
    return tm.select([cs.Change(s, p) for s, p in changes], m, root=root,
                     classify=lambda c: (py or {}).get(c.path, tm.PyChange()),
                     index=lambda: index or tm.NameIndex({}, {}, {}), weights=weights)


def many(n: int, footprint: dict | None = None) -> dict[str, dict]:
    """Filler tests that run nothing a change here touches, so a selection
    stays under the full-suite share."""
    return {f"tests/test_filler.py::test_{i}": footprint or {"f": [f"{OTHER}:other"]} for i in range(n)}


def changed(*funcs: str, classes: tuple[str, ...] = (), kinds: tuple[tuple[str, str], ...] = ()) -> tm.PyChange:
    return tm.PyChange(funcs=set(funcs), classes=set(classes), kinds=set(kinds))


def base_map(**extra) -> dict:
    tests = {"tests/test_a.py::test_work": {"f": [f"{SRC}:work"]},
             "tests/test_b.py::test_other": {"f": [f"{OTHER}:other"]},
             **many(10)}
    tests.update(extra.pop("tests", {}))
    return build_map(tests, universe_funcs=UNIVERSE_FUNCS, universe_files=UNIVERSE_FILES, **extra)


# ---------------------------------------------------------------- python change classification


BASE_SOURCE = '''"""doc"""
import os

LIMIT = 3


def work(x):
    total = 0
    for i in range(x):
        total += i
    return total


def register():
    def handler():
        return 1
    return handler


class Thing:
    size = 1

    def run(self):
        return self.size
'''


def lines_of(old: str, new: str) -> tuple[list[int], list[int]]:
    import difflib
    old_lines: list[int] = []
    new_lines: list[int] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old.split("\n"), b=new.split("\n")).get_opcodes():
        if tag != "equal":
            old_lines += range(i1 + 1, i2 + 1)
            new_lines += range(j1 + 1, j2 + 1)
    return old_lines, new_lines


def classify(new: str, old: str = BASE_SOURCE) -> tm.PyChange:
    return tm.classify_python(old, new, *lines_of(old, new))


def test_a_changed_line_counts_for_the_innermost_existing_def() -> None:
    change = classify(BASE_SOURCE.replace("total += i", "total += 2 * i"))
    assert change.funcs == {"work"} and not change.classes and not change.kinds
    assert classify(BASE_SOURCE.replace("return 1", "return 2")).funcs == {"register.<locals>.handler"}


def test_a_new_nested_def_changes_its_enclosing_def() -> None:
    # PR #2618: new routes defined inside register_public_routes broke the
    # OpenAPI tests; the change has to select what ran the enclosing def.
    new = BASE_SOURCE.replace("    return handler\n", "    def second():\n        return 2\n    return handler\n")
    assert classify(new).funcs == {"register"}


def test_a_new_method_or_class_statement_changes_the_class() -> None:
    new = BASE_SOURCE + "\n    def stop(self):\n        return 0\n"
    assert classify(new).classes == {"Thing"} and not classify(new).funcs
    assert classify(BASE_SOURCE.replace("size = 1", "size = 2")).classes == {"Thing"}


def test_module_level_changes_are_classified_by_statement() -> None:
    assert classify(BASE_SOURCE.replace("import os", "import os\nimport sys")).kinds == {("import", "")}
    assert classify(BASE_SOURCE.replace("LIMIT = 3", "LIMIT = 4")).kinds == {("assign", "LIMIT")}
    assert classify(BASE_SOURCE.replace('"""doc"""', '"""Doc."""')).kinds == {("docstring", "")}
    call = classify(BASE_SOURCE + "\nregister()\n").kinds
    assert ("other", "Expr") in call
    branch = classify(BASE_SOURCE.replace("LIMIT = 3", "if os.environ.get('X'):\n    LIMIT = 3"))
    assert ("other", "If") in branch.kinds


def test_a_new_decorated_top_level_def_names_what_it_registers_with() -> None:
    new = BASE_SOURCE + "\n@router.get('/x')\ndef new_route():\n    return 1\n"
    assert ("assign", "router") in classify(new).kinds
    plain = BASE_SOURCE + "\ndef new_helper():\n    return 1\n"
    assert classify(plain) == tm.PyChange()  # nothing calls it yet


def test_comments_and_blank_lines_select_nothing() -> None:
    assert classify(BASE_SOURCE.replace("    total = 0\n", "    # start at zero\n    total = 0\n\n")) == tm.PyChange()


def test_a_deleted_def_is_a_change_to_it_and_an_unparsable_file_is_flagged() -> None:
    new = BASE_SOURCE.replace("def register():\n    def handler():\n        return 1\n    return handler\n", "")
    assert "register" in classify(new).funcs
    assert classify("def broken(:\n").unparsable


def test_hunk_lines_reads_unified_zero_context_hunks() -> None:
    patch = "diff --git a/x b/x\n@@ -3,2 +3,0 @@\n-a\n-b\n@@ -10 +8,3 @@\n-c\n+d\n+e\n+f\n"
    assert tm.hunk_lines(patch) == ([3, 4, 10], [8, 9, 10])


# ---------------------------------------------------------------- selection rules


def test_s2_a_changed_function_selects_the_tests_that_ran_it_and_only_them() -> None:
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed("work")})
    assert not sel.full
    assert sel.selected == ["tests/test_a.py::test_work"]
    assert "tests/test_b.py::test_other" in sel.not_selected
    # Positive control: the same rules with the other file's function.
    sel = run_select(base_map(), [("M", OTHER)], py={OTHER: changed("other")})
    assert sel.full  # every filler test ran it: over the full-suite share


def test_s10_fixture_collection_and_memoized_misses_are_inherited() -> None:
    tests = {"tests/test_fx.py::test_uses_fixture": {"f": ["tests/test_fx.py:test_uses_fixture"]},
             "tests/test_col.py::test_collected": {"f": []},
             # helper is memoized: the first test missed (it ran helper, and
             # helper ran work); the second was served from the cache.
             "tests/test_cache.py::test_miss": {"f": [f"{SRC}:helper", f"{SRC}:helper@memo", f"{SRC}:work"]},
             "tests/test_cache.py::test_hit": {"f": [f"{SRC}:helper@memo"]}}
    fixtures = {"tests/test_fx.py::test_uses_fixture": [{"f": [f"{SRC}:work"]}]}
    collect = {"tests/test_col.py": {"f": [f"{SRC}:work"]}}
    tmap = base_map(tests=tests, fixtures=fixtures, collect=collect,
                    cached={f"{SRC}:helper": {"f": [f"{SRC}:helper", f"{SRC}:work"]}})
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed("work")})
    assert set(sel.selected) == {"tests/test_a.py::test_work", "tests/test_fx.py::test_uses_fixture",
                                 "tests/test_col.py::test_collected", "tests/test_cache.py::test_miss",
                                 "tests/test_cache.py::test_hit"}
    # A change to the memoized function itself selects its cache's users too.
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed("helper")})
    assert set(sel.selected) == {"tests/test_cache.py::test_miss", "tests/test_cache.py::test_hit"}
    # Positive control: without fixtures, collection and the misses,
    # only the tests whose own windows ran `work` are selected.
    bare = base_map(tests=tests)
    assert set(run_select(bare, [("M", SRC)], py={SRC: changed("work")}).selected) == {
        "tests/test_a.py::test_work", "tests/test_cache.py::test_miss"}


def test_s4_a_module_level_name_selects_what_names_it_transitively() -> None:
    index = tm.NameIndex(funcs_by_ident={"DERIVED": {f"{SRC}:work"}}, defined_from={"LIMIT": {"DERIVED"}}, universe={})
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed(kinds=(("assign", "LIMIT"),))}, index=index)
    assert sel.selected == ["tests/test_a.py::test_work"]
    inert = run_select(base_map(), [("M", SRC)], py={SRC: changed(kinds=(("import", ""), ("docstring", "")))})
    assert not inert.full and inert.selected == []


def test_s3_s4_select_the_tests_a_cache_served_not_only_those_that_missed() -> None:
    # Review probe probe_s4_memo.py: a module-level PRICE_TABLE that an
    # lru_cache function reads changed. The test that missed the cache ran
    # price_for; the test the cache served has only "price_for@memo".
    price = f"{SRC}:price_for"
    tests = {"tests/test_p.py::test_miss": {"f": [price, price + "@memo"]},
             "tests/test_p.py::test_hit": {"f": [price + "@memo"]}}
    tmap = base_map(tests=tests, cached={price: {"f": [price]}})
    s4 = tm.NameIndex(funcs_by_ident={"PRICE_TABLE": {price}}, defined_from={}, universe={})
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed(kinds=(("assign", "PRICE_TABLE"),))}, index=s4)
    assert sel.selected == ["tests/test_p.py::test_hit", "tests/test_p.py::test_miss"]
    s3 = tm.NameIndex(funcs_by_ident={"Thing": {price}}, defined_from={}, universe={})
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed(classes=("Thing",))}, index=s3)
    assert sel.selected == ["tests/test_p.py::test_hit", "tests/test_p.py::test_miss"]


def test_s3_a_class_change_selects_its_methods_and_what_names_it() -> None:
    tmap = base_map(tests={"tests/test_t.py::test_thing": {"f": [f"{SRC}:Thing.run"]}})
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed(classes=("Thing",))})
    assert sel.selected == ["tests/test_t.py::test_thing"]


def test_s1_s5_s6_test_files_deleted_and_added_files() -> None:
    sel = run_select(base_map(), [("M", "tests/test_b.py"), ("D", "tests/test_gone.py")])
    assert sel.whole_files == ["tests/test_b.py"] and sel.selected == ["tests/test_b.py::test_other"]
    deleted = run_select(base_map(), [("D", SRC)])
    assert deleted.selected == ["tests/test_a.py::test_work"]
    added = run_select(base_map(), [("A", "src/app/new.py")])
    assert not added.full and added.selected == []
    lister = base_map(tests={"tests/test_l.py::test_lists": {"d": ["src/app"]}})
    assert run_select(lister, [("A", "src/app/new.py")]).selected == ["tests/test_l.py::test_lists"]


def test_s7_a_data_file_selects_its_readers() -> None:
    tmap = base_map(tests={"tests/test_d.py::test_reads": {"o": ["docs/guide.md"]}})
    assert run_select(tmap, [("M", "docs/guide.md")]).selected == ["tests/test_d.py::test_reads"]
    assert run_select(tmap, [("M", "docs/other.md")]).selected == []


@pytest.mark.parametrize(("command", "narrow"), [
    ("git init @cwd=-", True),                        # a test's scratch repository
    ("git -C <outside> log @cwd=-", True),
    ("id -u @cwd=.", True),
    ("git ls-files @cwd=.", False),                   # the repository itself
    ("git -C . rev-parse @cwd=-", False),
    ("git diff src/app/core.py @cwd=-", False),
    ("git --work-tree=./src status @cwd=-", False),   # an option's value names the repository
    ("git --work-tree=. status @cwd=-", False),
    ("git --git-dir=./.git log @cwd=-", False),       # the repository's own history
    ("git --git-dir=<outside> log @cwd=-", True),
    ("git log -p <value> @cwd=-", False),              # a hidden word may be a repository path
    ("git -c <program> commit @cwd=-", False),
    ("git --work-tree=<value> status @cwd=-", False),
    ("python -c <program> @cwd=.", False),
    ("uv run python -m @cwd=.", False),
    ("env PYTHONPATH=<value> python3 -c @cwd=-", False),
    ("bash -c <program> @cwd=-", False),
    ("sh -ec <program> @cwd=-", False),
    ("bash scripts/deploy/run.sh @cwd=-", False),     # a script may start Python, read anything
    ("gcloud run deploy @cwd=-", False),
    ("", False),
])
def test_s8_a_command_is_narrow_only_when_it_reads_nothing_of_the_repository(command: str, narrow: bool) -> None:
    assert tm.command_is_narrow(command) is narrow


def test_s8_a_test_that_starts_an_unknown_command_runs_on_any_change() -> None:
    tests = {"tests/test_p.py::test_uv": {"s": ["uv run python -m @cwd=."]},
             "tests/test_p.py::test_bash": {"s": ["bash -c <program> @cwd=-"]},
             "tests/test_g.py::test_git_elsewhere": {"s": ["git init @cwd=-"]},
             "tests/test_g.py::test_id": {"s": ["id -u @cwd=-"]}}
    sel = run_select(base_map(tests=tests), [("M", "docs/readme.md")])
    assert set(sel.selected) == {"tests/test_p.py::test_uv", "tests/test_p.py::test_bash"}


def test_s9_a_helper_selects_its_importers(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "tests/helper.py").write_text("def make():\n    return 1\n")
    (tmp_path / "tests/test_uses.py").write_text("from tests.helper import make\n\ndef test_x():\n    make()\n")
    (tmp_path / "tests/test_plain.py").write_text("def test_y():\n    pass\n")
    tmap = base_map(tests={"tests/test_uses.py::test_x": {}, "tests/test_plain.py::test_y": {}})
    sel = run_select(tmap, [("M", "tests/helper.py")], py={"tests/helper.py": changed("make")}, root=tmp_path)
    assert sel.whole_files == ["tests/test_uses.py"]
    assert "tests/test_plain.py::test_y" in sel.not_selected


@pytest.mark.parametrize(("path", "why"), [
    (".github/workflows/ci.yml", "decides what CI runs"),
    ("scripts/ci/testmap.py", "decides what CI runs"),
    ("pyproject.toml", "Python toolchain"),
    ("uv.lock", "Python toolchain"),
])
def test_f1_what_decides_ci_and_the_toolchain_run_everything(path: str, why: str) -> None:
    sel = run_select(base_map(), [("M", path)])
    assert sel.full and why in sel.reasons[0]
    assert run_select(base_map(), []).full


@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/conformance/conftest.py", "conftest.py",
                                  "src/app/conftest.py"])
def test_f7_any_conftest_at_any_depth_runs_everything(path: str) -> None:
    sel = run_select(base_map(), [("M", path)], py={path: changed("client")})
    assert sel.full and "conftest.py" in sel.reasons[0]
    drift = tm.Drift([cs.Change("M", path)], lambda c: tm.PyChange())
    sel = tm.select([cs.Change("M", "docs/readme.md")], tm.TestMap(base_map()), root=Path("/nonexistent"),
                    classify=lambda c: tm.PyChange(), index=lambda: tm.NameIndex({}, {}, {}), weights={},
                    drift=drift)
    assert sel.full and "changed since the map" in sel.reasons[0]


def test_f3_what_runs_or_is_read_outside_every_test_runs_everything() -> None:
    tmap = base_map(glob={"f": [f"{SRC}:boot"], "o": ["src/app/data/catalog.json"]})
    sel = run_select(tmap, [("M", "src/app/data/catalog.json")])
    assert sel.full and "outside every test" in sel.reasons[0]
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed("boot")})
    assert sel.full and "import or session time" in sel.reasons[0]
    # Positive control: the same map, a function that runs only in tests.
    assert not run_select(tmap, [("M", SRC)], py={SRC: changed("work")}).full


def test_f4_an_unknown_module_level_statement_or_unparsable_file_runs_everything() -> None:
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed(kinds=(("other", "If"),))})
    assert sel.full and "module-level" in sel.reasons[0]
    sel = run_select(base_map(), [("M", SRC)], py={SRC: tm.PyChange(unparsable=True)})
    assert sel.full


def test_f5_what_the_map_cannot_know_runs_everything() -> None:
    sel = run_select(base_map(), [("M", "src/app/newer.py")], py={"src/app/newer.py": changed("f")})
    assert sel.full and "newer than the map" in sel.reasons[0]
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed("added_after_the_map")})
    assert sel.full and "newer than the map" in sel.reasons[0]


def test_f6_a_selection_over_the_full_share_runs_everything() -> None:
    weights = {row[0]: 1.0 for row in base_map()["tests"]}
    weights["tests/test_a.py::test_work"] = 1000.0
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed("work")}, weights=weights)
    assert sel.full and "of the suite's weight" in sel.reasons[0]


def test_s10_a_memoized_function_with_unrecorded_misses_runs_its_users_whatever_changed() -> None:
    tests = {"tests/test_c.py::test_hit": {"f": [f"{SRC}:helper@memo"]},
             "tests/test_c.py::test_miss": {"f": [f"{SRC}:helper"]},
             "tests/test_c.py::test_other": {"f": [f"{OTHER}:reads"]}}
    tmap = base_map(tests=tests)
    tmap["incomplete_memo"] = [f"{SRC}:helper"]
    sel = run_select(tmap, [("M", "docs/readme.md")])
    assert set(sel.selected) == {"tests/test_c.py::test_hit", "tests/test_c.py::test_miss"}
    tmap["incomplete_memo"] = []
    tmap["unattributed_memo"] = []
    assert run_select(tmap, [("M", "docs/readme.md")]).selected == []


# ---------------------------------------------------------------- drift: the map's commit before the base


def drift_select(tmap: dict, changes: list[tuple[str, str]], drift_changes: list[tuple[str, str]], *,
                 py: dict[str, tm.PyChange] | None = None, drift_py: dict[str, tm.PyChange] | None = None,
                 added_files: set[str] = frozenset(), added_keys: set[str] = frozenset()) -> tm.Selection:
    drift = tm.Drift([cs.Change(s, p) for s, p in drift_changes], lambda c: (drift_py or {}).get(c.path, tm.PyChange()),
                     added_files=set(added_files), added_keys=set(added_keys))
    return tm.select([cs.Change(s, p) for s, p in changes], tm.TestMap(tmap), root=Path("/nonexistent"),
                     classify=lambda c: (py or {}).get(c.path, tm.PyChange()),
                     index=lambda: tm.NameIndex({}, {}, {}), weights={t[0]: 1.0 for t in tmap["tests"]}, drift=drift)


def test_drift_a_function_changed_since_the_map_counts_as_changed() -> None:
    # Between the map and the base, work() gained a call to helper(): the
    # map cannot show the edge, so work's tests run when helper changes.
    sel = drift_select(base_map(), [("M", "docs/readme.md")], [("M", SRC)], drift_py={SRC: changed("work")})
    assert sel.selected == ["tests/test_a.py::test_work"]
    # Positive control: without the drift the same diff selects nothing.
    assert drift_select(base_map(), [("M", "docs/readme.md")], []).selected == []


def test_drift_a_function_added_since_the_map_is_not_unknown() -> None:
    key = f"{SRC}:added_since_the_map"
    sel = drift_select(base_map(), [("M", SRC)], [("M", SRC)], py={SRC: changed("added_since_the_map")},
                       drift_py={SRC: changed("work")}, added_keys={key})
    assert not sel.full and sel.selected == ["tests/test_a.py::test_work"]
    sel = drift_select(base_map(), [("M", "src/app/new.py")], [("A", "src/app/new.py")],
                       py={"src/app/new.py": changed("f")}, added_files={"src/app/new.py"})
    assert not sel.full


def test_drift_catalog_data_read_at_import_is_not_a_full_run_but_code_run_at_import_is() -> None:
    tmap = base_map(glob={"f": [f"{SRC}:boot"], "o": ["src/app/data/catalog.json"]})
    sel = drift_select(tmap, [("M", "docs/readme.md")], [("M", "src/app/data/catalog.json")])
    assert not sel.full
    sel = drift_select(tmap, [("M", "docs/readme.md")], [("M", SRC)], drift_py={SRC: changed("boot")})
    assert sel.full and "changed since the map" in sel.reasons[0]


# ---------------------------------------------------------------- reports


def test_contains_runs_unknown_tests_and_strips_the_xdist_group() -> None:
    selection = {"full": False, "whole_files": ["tests/test_w.py"], "selected": ["tests/test_a.py::test_x"],
                 "not_selected": ["tests/test_b.py::test_y", "tests/test_c.py::test_z[g]",
                                  "tests/test_d.py::test_q[postgres]@conformance-postgres"]}
    assert tm.contains(selection, "tests/test_a.py::test_x")
    assert tm.contains(selection, "tests/test_w.py::test_anything")
    assert not tm.contains(selection, "tests/test_b.py::test_y")
    assert not tm.contains(selection, "tests/test_c.py::test_z[g]@conformance-postgres")
    assert not tm.contains(selection, "tests/test_d.py::test_q[postgres]")
    assert not tm.contains(selection, "tests/test_d.py::test_q[postgres]@conformance-postgres")
    assert tm.contains(selection, "tests/test_new.py::test_unknown")  # S11
    assert tm.contains({"full": True}, "tests/test_b.py::test_y")


def test_failed_ids_matches_known_ids_exactly_spaces_included() -> None:
    known = ["tests/test_a.py::test_x[1]", "tests/test_a.py::test_x[1 2]", "tests/test_a.py::test_x[1 2 3]",
             "tests/conformance/test_c.py::test_y[backend=spanner-pg]@conformance-spanner-pg"]
    log = ("2026-11-17T01:02:03.000Z FAILED tests/test_a.py::test_x[1 2] - AssertionError: no\n"
           "2026-11-17T01:02:03.000Z FAILED tests/test_a.py::test_x[1] - AssertionError: no\n"
           "ERROR tests/conformance/test_c.py::test_y[backend=spanner-pg]@conformance-spanner-pg - boom\n"
           "FAILED tests/test_new.py::test_q[a b] - boom\n"
           "FAILED tests/test_a.py\n"
           "  somewhere in a traceback: FAILED is a word\n")
    assert tm.failed_ids(log, known) == {
        "tests/test_a.py::test_x[1 2]", "tests/test_a.py::test_x[1]",
        "tests/conformance/test_c.py::test_y[backend=spanner-pg]@conformance-spanner-pg",
        tm.UNKNOWN + "tests/test_new.py::test_q[a b]"}
    # The old regex cut "test_x[1 2]" to "test_x[1", unknown, and counted it as selected.
    selection = {"full": False, "whole_files": [], "selected": ["tests/test_a.py::test_x[1]"],
                 "not_selected": ["tests/test_a.py::test_x[1 2]", "tests/test_a.py::test_x[1 2 3]"]}
    runs = tm.membership(selection)
    assert not runs("tests/test_a.py::test_x[1 2]")


def test_compare_counts_collection_errors_and_files_the_selection_would_not_collect(tmp_path: Path) -> None:
    selection = {"full": False, "whole_files": ["tests/test_w.py"], "selected": ["tests/test_s.py::test_a"],
                 "not_selected": ["tests/test_s.py::test_b", "tests/test_n.py::test_c", "tests/test_w.py::test_d"]}
    tm.write_gz(selection, tmp_path / "selection.json.gz")
    log = tmp_path / "shard.log"
    log.write_text("ERROR tests/test_n.py - ImportError: cannot import name 'x'\n"   # no test of it selected
                   "ERROR tests/test_s.py - ImportError: boom\n"                    # one of its tests selected
                   "ERROR tests/test_w.py\n"                                        # runs whole
                   "ERROR tests/test_new.py - SyntaxError\n")                       # unknown: runs (S11)
    assert tm.failed_ids(log.read_text(), []) == {"module:tests/test_n.py", "module:tests/test_s.py",
                                                  "module:tests/test_w.py", "module:tests/test_new.py"}
    summary = tmp_path / "summary.md"
    assert tm.main(["compare", "--selection", str(tmp_path / "selection.json.gz"), "--failed", str(log),
                    "--summary", str(summary)]) == 0
    text = summary.read_text()
    assert "failures outside selection: 1 (module:tests/test_n.py)" in text
    assert "of which 4 are collection errors" in text


def _records(root: Path, *records: dict) -> Path:
    for n, record in enumerate(records, 1):
        (root / f"test-failures-{n}").mkdir(parents=True)
        (root / f"test-failures-{n}" / "failures.json").write_text(json.dumps(record))
    return root


def _shard(group: int, *failed: str, complete: bool = True, exitstatus: int = 1, collection: tuple = ()) -> dict:
    return {"group": group, "splits": 3, "complete": complete, "exitstatus": exitstatus if complete else None,
            "failed": list(failed), "collection_errors": list(collection)}


def test_recorded_failures_are_compared_only_when_every_shard_recorded_them(tmp_path: Path) -> None:
    every = [_shard(1, "tests/test_a.py::test_x[1 2]"), _shard(2, collection=("tests/test_b.py",)),
             _shard(3, exitstatus=0)]
    assert tm.recorded_failures(every, "failure") == ({"tests/test_a.py::test_x[1 2]", tm.MODULE + "tests/test_b.py"},
                                                      None)
    # A green test job failed nothing, records or not.
    assert tm.recorded_failures([], "success") == (set(), None)
    # A shard that stopped before pytest finished (cancelled, timed out) or left nothing.
    failed, why = tm.recorded_failures([every[0], _shard(2, complete=False), every[2]], "failure")
    assert failed == {"tests/test_a.py::test_x[1 2]"} and why is not None and "shards 2 " in why
    failed, why = tm.recorded_failures([every[0], every[2]], "failure")
    assert why is not None and "shards 2 " in why
    # A record that says complete but shows otherwise (interrupted: exit 2;
    # fewer tests reported than collected) is not taken at its word.
    interrupted = {**_shard(2, "tests/test_n.py::test_c"), "exitstatus": 2}
    short = {**_shard(2, "tests/test_n.py::test_c"), "collected": 10, "reported": 1}
    for record in (interrupted, short):
        assert "shards 2 " in str(tm.recorded_failures([every[0], record, every[2]], "failure")[1])
    assert "no shard left a failure record" in str(tm.recorded_failures([], "failure")[1])
    # Every shard finished without a failing test, yet the job is red: a step outside pytest failed.
    why = tm.recorded_failures([_shard(1, exitstatus=0), _shard(2, exitstatus=0), _shard(3, exitstatus=0)],
                               "failure")[1]
    assert why is not None and "no shard recorded a failing test" in why


def test_compare_reads_the_failure_records_and_never_reports_a_zero_it_did_not_measure(tmp_path: Path) -> None:
    selection = {"full": False, "whole_files": [], "selected": ["tests/test_s.py::test_a"],
                 "not_selected": ["tests/test_s.py::test_b words[x y]", "tests/test_n.py::test_c@grp"]}
    tm.write_gz(selection, tmp_path / "selection.json.gz")
    records = _records(tmp_path / "red",
                       _shard(1, "tests/test_s.py::test_a", "tests/test_s.py::test_b words[x y]"),
                       _shard(2, "tests/test_n.py::test_c@grp", "tests/test_new.py::test_q"),
                       _shard(3, exitstatus=0))
    summary = tmp_path / "summary.md"

    def compare(failures: Path, result: str) -> str:
        summary.write_text("")
        assert tm.main(["compare", "--selection", str(tmp_path / "selection.json.gz"), "--failures", str(failures),
                        "--test-result", result, "--summary", str(summary)]) == 0
        return summary.read_text()

    text = compare(records, "failure")
    assert ("failures outside selection: 2 (tests/test_n.py::test_c@grp, tests/test_s.py::test_b words[x y])"
            in text)
    assert "failures in this run: 4, of which 0 are collection errors and 1 are tests the map does not know" in text
    # A shard without a complete record: unavailable, with the reason; never "0".
    partial = _records(tmp_path / "partial", _shard(1, "tests/test_s.py::test_a"), _shard(2, complete=False))
    text = compare(partial, "failure")
    assert "comparison unavailable: shards 2, 3 left no complete failure record" in text
    assert "failures outside selection" not in text
    text = compare(tmp_path / "no-artifacts", "failure")
    assert "comparison unavailable" in text and "failures outside selection" not in text
    # A green run.
    assert "failures outside selection: 0" in compare(tmp_path / "no-artifacts", "success")


def test_a_directory_that_fails_to_collect_covers_every_test_file_under_it(tmp_path: Path) -> None:
    # A failing tests/sub/conftest.py: pytest reports the directory. It is
    # outside the selection when the selection runs no test file under it.
    def outside(selected: list[str]) -> str:
        known = ["tests/sub/test_x.py::test_b", "tests/sub/deep/test_y.py::test_c", "tests/test_s.py::test_a"]
        selection = {"full": False, "whole_files": [], "selected": selected,
                     "not_selected": [t for t in known if t not in selected]}
        tm.write_gz(selection, tmp_path / "selection.json.gz")
        records = tmp_path / f"records-{len(selected)}"
        _records(records, _shard(1, "tests/test_s.py::test_a"), _shard(2, collection=("tests/sub",)),
                 _shard(3, exitstatus=0))
        summary = tmp_path / "summary.md"
        summary.write_text("")
        tm.main(["compare", "--selection", str(tmp_path / "selection.json.gz"), "--failures", str(records),
                 "--test-result", "failure", "--summary", str(summary)])
        return next(line for line in summary.read_text().splitlines() if line.startswith("failures outside"))

    assert outside(["tests/test_s.py::test_a"]) == "failures outside selection: 1 (module:tests/sub)"
    assert outside(["tests/test_s.py::test_a", "tests/sub/deep/test_y.py::test_c"]) == "failures outside selection: 0"


def test_failed_ids_reads_only_the_runs_own_summary() -> None:
    known = ["tests/test_a.py::test_x", "tests/a dir/test_b.py::test_y"]
    log = "\n".join([
        "2026-11-17T01:02:03.0000000Z ________ test_x ________",
        # A test that ran pytest inside it shows that run's output in its
        # failure report: its own summary, indented or "E"-prefixed lines.
        "2026-11-17T01:02:03.0000000Z ----- Captured stdout call -----",
        "2026-11-17T01:02:03.0000000Z =========== short test summary info ===========",
        "2026-11-17T01:02:03.0000000Z FAILED tests/test_inner.py::test_nested - boom",
        "2026-11-17T01:02:03.0000000Z ERROR tests/test_sample.py - ImportError",
        "2026-11-17T01:02:03.0000000Z ======= 1 failed in 0.1s =======",
        "2026-11-17T01:02:03.0000000Z E         ERROR tests/test_other.py::test_z - RuntimeError",
        "2026-11-17T01:02:04.0000000Z =========== short test summary info ===========",
        "2026-11-17T01:02:04.0000000Z FAILED tests/test_a.py::test_x - AssertionError",
        "2026-11-17T01:02:04.0000000Z ERROR tests/a dir/test_c.py - ImportError: no",  # a path with a space
        "2026-11-17T01:02:04.0000000Z ===== 1 failed, 1 error in 9.9s =====",
        "2026-11-17T01:02:05.0000000Z ##[error]Process completed with exit code 1.",
    ])
    assert tm.failed_ids(log, known) == {"tests/test_a.py::test_x", tm.MODULE + "tests/a dir/test_c.py"}
    # Without a summary section (a log cut short), only lines that start with the status count.
    cut = "FAILED tests/test_a.py::test_x - boom\nE   ERROR tests/test_other.py::test_z - boom\n"
    assert tm.failed_ids(cut, known) == {"tests/test_a.py::test_x"}


# ---------------------------------------------------------------- map validity and lookup


def test_validate_map_checks_schema_recorder_event_shards_and_commit() -> None:
    good = base_map()
    good["incomplete_memo"] = []
    good["unattributed_memo"] = []
    assert tm.validate_map(good) is None
    assert tm.validate_map(good, commit="a" * 40) is None
    for event in ("push", "schedule", "workflow_dispatch"):
        assert tm.validate_map({**good, "event": event}) is None
    for change, words in (({"schema": 99}, "schema"), ({"recorder": "0"}, "recorder"),
                          ({"event": "pull_request"}, "not main's"),
                          ({"ref": "refs/heads/feature"}, "not main's"),
                          ({"fragments": [1, 2, 3]}, "fragments"), ({"shards": 4}, "fragments")):
        assert words in (tm.validate_map({**good, **change}) or ""), change
    assert "commit" in (tm.validate_map(good, commit="b" * 40) or "")


REPO_ID = 4242
NOW = __import__("datetime").datetime(2026, 11, 17, 12, tzinfo=__import__("datetime").UTC)


def history(tmp_path: Path, dates: list[str]) -> tuple[Path, list[str]]:
    """A repository of len(dates) commits on main, oldest first, each with the
    given committer date; returns it and the shas newest first."""
    root = tmp_path / "hist"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for n, date in enumerate(dates):
        (root / "f.txt").write_text(str(n))
        git(root, "add", "f.txt")
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", str(n),  # noqa: S603,S607 -- fixed argv
                        "--date", date], check=True, env={"GIT_COMMITTER_DATE": date, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    return root, git(root, "rev-list", "--first-parent", "main").split()


def artifact(art_id: int, sha: str, *, branch: str = "main", expired: bool = False, repo_id: int = REPO_ID,
             head_repo_id: int = REPO_ID, created: str = "2026-11-17T11:00:00Z") -> dict:
    return {"id": art_id, "name": tm.ARTIFACT, "expired": expired, "created_at": created,
            "workflow_run": {"id": art_id * 10, "head_sha": sha, "head_branch": branch, "repository_id": repo_id,
                             "head_repository_id": head_repo_id}}


def run(art_id: int, sha: str, *, path: str = tm.MAP_WORKFLOW, event: str = "schedule", branch: str = "main",
        head_repo_id: int = REPO_ID, conclusion: str = "success") -> dict:
    return {"id": art_id * 10, "path": path, "event": event, "head_branch": branch, "head_sha": sha,
            "repository": {"id": REPO_ID}, "head_repository": {"id": head_repo_id}, "status": "completed",
            "conclusion": conclusion}


def fake_api(artifacts: list[dict], runs: list[dict], maps: dict[int, dict]):
    by_run = {r["id"]: r for r in runs}

    def api(path: str, raw: bool = False):
        if path.endswith("/zip"):
            art = int(path.split("/artifacts/")[1].split("/")[0])
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("testmap.json.gz", gzip.compress(json.dumps(maps[art]).encode()))
            return buf.getvalue()
        if "/artifacts?" in path:
            return {"artifacts": artifacts}
        if "/actions/runs/" in path:
            return by_run[int(path.rsplit("/", 1)[1])]
        raise AssertionError(f"unexpected API call {path}")  # never the commits listing: dates lie
    return api


def mapped(sha: str) -> dict:
    data = base_map(commit=sha)
    data["incomplete_memo"] = []
    data["unattributed_memo"] = []
    data["event"] = "schedule"
    return data


def test_find_map_takes_the_nearest_valid_map_on_the_first_parent_history(tmp_path: Path) -> None:
    root, shas = history(tmp_path, ["2026-11-17T10:00:00Z", "2026-11-17T10:30:00Z", "2026-11-17T11:00:00Z"])
    api = fake_api([artifact(1, shas[1]), artifact(2, shas[2])], [run(1, shas[1]), run(2, shas[2])],
                   {1: mapped(shas[1]), 2: mapped(shas[2])})
    found, why = tm.find_map("o/r", REPO_ID, shas[0], root=root, api=api, now=NOW)
    assert why == "" and found is not None and found["commit"] == shas[1] and found["_distance"] == 1


@pytest.mark.parametrize("forgery", ["fork artifact", "fork run", "wrong workflow", "pull_request event",
                                     "other branch", "red run", "expired", "uploaded too long ago"])
def test_find_map_refuses_a_map_from_anything_but_its_own_workflow_on_main(tmp_path: Path, forgery: str) -> None:
    root, shas = history(tmp_path, ["2026-11-17T10:00:00Z", "2026-11-17T10:30:00Z", "2026-11-17T11:00:00Z"])
    near_art, near_run = artifact(1, shas[1]), run(1, shas[1])
    if forgery == "fork artifact":
        near_art = artifact(1, shas[1], head_repo_id=999)
    elif forgery == "fork run":
        near_run = run(1, shas[1], head_repo_id=999)
    elif forgery == "wrong workflow":
        near_run = run(1, shas[1], path=".github/workflows/other.yml")
    elif forgery == "pull_request event":
        near_run = run(1, shas[1], event="pull_request")
    elif forgery == "other branch":
        near_art = artifact(1, shas[1], branch="feature")
    elif forgery == "red run":
        near_run = run(1, shas[1], conclusion="failure")
    elif forgery == "expired":
        near_art = artifact(1, shas[1], expired=True)
    elif forgery == "uploaded too long ago":
        near_art = artifact(1, shas[1], created="2026-11-14T00:00:00Z")
    api = fake_api([near_art, artifact(2, shas[2])], [near_run, run(2, shas[2])],
                   {1: mapped(shas[1]), 2: mapped(shas[2])})
    found, why = tm.find_map("o/r", REPO_ID, shas[0], root=root, api=api, now=NOW)
    # The forged or unusable map at distance 1 is passed over for the valid one at distance 2.
    assert found is not None and found["commit"] == shas[2], (forgery, why)
    # Positive control: the same map, untampered, is taken.
    api = fake_api([artifact(1, shas[1]), artifact(2, shas[2])], [run(1, shas[1]), run(2, shas[2])],
                   {1: mapped(shas[1]), 2: mapped(shas[2])})
    assert tm.find_map("o/r", REPO_ID, shas[0], root=root, api=api, now=NOW)[0]["commit"] == shas[1]


def test_map_artifacts_reads_past_a_first_page_of_other_testmap_artifacts(tmp_path: Path) -> None:
    root, shas = history(tmp_path, ["2026-11-17T10:00:00Z", "2026-11-17T11:00:00Z"])
    forks = [artifact(100 + n, shas[0], head_repo_id=999) for n in range(100)]
    real = artifact(1, shas[1])
    runs = {r["id"]: r for r in [run(1, shas[1])]}

    def api(path: str, raw: bool = False):
        if path.endswith("/zip"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("testmap.json.gz", gzip.compress(json.dumps(mapped(shas[1])).encode()))
            return buf.getvalue()
        if "/artifacts?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {"artifacts": forks if page == 1 else [real] if page == 2 else []}
        return runs[int(path.rsplit("/", 1)[1])]

    found, why = tm.find_map("o/r", REPO_ID, shas[0], root=root, api=api, now=NOW)
    assert found is not None and found["commit"] == shas[1], why


def test_find_map_orders_candidates_by_history_not_by_dates(tmp_path: Path) -> None:
    # The parent of the base claims to be from next year, its own parent from
    # last year: the order is the history's, so the parent's map is the nearest.
    root, shas = history(tmp_path, ["2025-11-17T10:00:00Z", "2027-11-17T10:00:00Z", "2026-11-17T11:00:00Z"])
    api = fake_api([artifact(1, shas[1]), artifact(2, shas[2])], [run(1, shas[1]), run(2, shas[2])],
                   {1: mapped(shas[1]), 2: mapped(shas[2])})
    found, _ = tm.find_map("o/r", REPO_ID, shas[0], root=root, api=api, now=NOW)
    assert found is not None and found["commit"] == shas[1] and found["_distance"] == 1


def test_find_map_ignores_a_map_newer_than_the_base_and_far_ancestors(tmp_path: Path) -> None:
    root, shas = history(tmp_path, [f"2026-11-17T{h:02d}:00:00Z" for h in range(3)])
    # The base is the oldest commit; the only map is of a newer one.
    api = fake_api([artifact(1, shas[0])], [run(1, shas[0])], {1: mapped(shas[0])})
    found, why = tm.find_map("o/r", REPO_ID, shas[2], root=root, api=api, now=NOW)
    assert found is None and "no valid map" in why
    root2, far = history(tmp_path / "far", [f"2026-11-17T00:{m:02d}:00Z" for m in range(tm.MAX_DISTANCE + 3)])
    api = fake_api([artifact(1, far[-1])], [run(1, far[-1])], {1: mapped(far[-1])})
    found, why = tm.find_map("o/r", REPO_ID, far[0], root=root2, api=api, now=NOW)
    assert found is None and "no valid map" in why


def test_needed_records_unless_nothing_the_map_reads_changed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("x = 1\n")
    (root / "docs").mkdir()
    (root / "docs/a.md").write_text("a\n")
    git(root, "init", "-q", "-b", "main")
    mapped_sha = commit_all(root, "mapped")
    api = fake_api([artifact(1, mapped_sha)], [run(1, mapped_sha)], {1: mapped(mapped_sha)})
    assert tm.needed("o/r", REPO_ID, mapped_sha, root=root, api=api, now=NOW)[0] is False
    (root / "docs/a.md").write_text("b\n")
    docs_sha = commit_all(root, "docs")
    assert tm.needed("o/r", REPO_ID, docs_sha, root=root, api=api, now=NOW)[0] is False
    assert tm.needed("o/r", REPO_ID, docs_sha, root=root, api=api, now=NOW, force=True)[0] is True
    (root / "src/a.py").write_text("x = 2\n")
    code_sha = commit_all(root, "code")
    record, why = tm.needed("o/r", REPO_ID, code_sha, root=root, api=api, now=NOW)
    assert record and "src/a.py" in why


@pytest.mark.parametrize("unusable", ["fork artifact", "uploaded too long ago", "other recorder version",
                                      "beyond the distance"])
def test_needed_records_when_the_newest_map_is_unusable(tmp_path: Path, unusable: str) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/a.md").write_text("a\n")
    git(root, "init", "-q", "-b", "main")
    mapped_sha = commit_all(root, "mapped")
    head = mapped_sha
    art, data = artifact(1, mapped_sha), mapped(mapped_sha)
    if unusable == "fork artifact":
        art = artifact(1, mapped_sha, head_repo_id=999)
    elif unusable == "uploaded too long ago":
        art = artifact(1, mapped_sha, created="2026-11-14T00:00:00Z")
    elif unusable == "other recorder version":
        data = {**data, "recorder": "0"}
    else:
        for n in range(tm.MAX_DISTANCE + 1):  # docs only: nothing the map reads changed
            (root / "docs/a.md").write_text(str(n))
            head = commit_all(root, f"docs {n}")
    record, why = tm.needed("o/r", REPO_ID, head, root=root, api=fake_api([art], [run(1, mapped_sha)], {1: data}),
                            now=NOW)
    assert record and "no usable map" in why, why
    # Positive control: the same map, usable, at the same head.
    if unusable != "beyond the distance":
        good = fake_api([artifact(1, mapped_sha)], [run(1, mapped_sha)], {1: mapped(mapped_sha)})
        assert tm.needed("o/r", REPO_ID, head, root=root, api=good, now=NOW)[0] is False


# ---------------------------------------------------------------- compact and merge



def raw(tmp: Path, name: str, records: list[dict]) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / name).write_text("".join(json.dumps(r) + "\n" for r in records))


HEADER = {"h": {"recorder": tm.RECORDER_VERSION, "root": "/r/", "pid": 1, "worker": "gw0"}}


def test_compact_resolves_scoped_fixtures_and_shares_footprints(tmp_path: Path) -> None:
    raw(tmp_path / "raw", "gw0-1.jsonl", [
        HEADER,
        {"c": "tests/test_a.py", "f": ["src/x.py:collected"]},
        {"fx": "module|tests/test_a.py||db", "f": ["src/x.py:setup"]},
        {"t": "tests/test_a.py::test_1", "x": ["module||db"], "f": ["src/x.py:f"]},
        {"t": "tests/test_a.py::test_2", "x": ["module||db"], "f": ["src/x.py:f"]},
        {"cf": "src/x.py:cached", "f": ["src/x.py:inner"]},
        {"g": True, "f": ["src/x.py:boot"], "o": ["data.json"]},
        {"end": True},
    ])
    frag = tm.compact(tmp_path / "raw", 1)
    assert frag["valid"] and frag["shard"] == 1
    rows = {t: (rt, fx) for t, rt, fx in frag["tests"]}
    assert rows["tests/test_a.py::test_1"] == rows["tests/test_a.py::test_2"]  # one shared footprint
    fx_fp = frag["fps"][rows["tests/test_a.py::test_1"][1][0]]
    assert [frag["keys"][i] for i in fx_fp[0]] == ["src/x.py:setup"]
    miss = frag["fps"][frag["cached"]["src/x.py:cached"]]
    assert [frag["keys"][i] for i in miss[0]] == ["src/x.py:inner"]


def test_compact_marks_a_shard_invalid_on_a_recorder_error(tmp_path: Path) -> None:
    raw(tmp_path / "raw", "gw0-1.jsonl", [HEADER, {"t": "tests/test_a.py::t", "f": []}, {"err": "boom"}, {"end": True}])
    assert not tm.compact(tmp_path / "raw", 1)["valid"]
    raw(tmp_path / "cut", "gw0-1.jsonl", [HEADER, {"t": "tests/test_a.py::t", "f": []}])
    assert "no end record" in tm.compact(tmp_path / "cut", 1)["errors"][0]
    raw(tmp_path / "other", "gw0-1.jsonl", [{"h": {"recorder": "0"}}, {"t": "tests/test_a.py::t"}])
    assert not tm.compact(tmp_path / "other", 1)["valid"]
    assert not tm.compact(tmp_path / "empty", 1)["valid"]


def fragments(tmp_path: Path, shards: int = tm.SHARDS) -> list[dict]:
    out = []
    for shard in range(1, shards + 1):
        raw(tmp_path / f"raw{shard}", "gw0-1.jsonl", [
            HEADER, {"t": f"tests/test_{shard}.py::test", "f": [f"src/x.py:f{shard}"]},
            {"g": True, "o": [f"data{shard}.json"]}, {"end": True}])
        out.append(tm.compact(tmp_path / f"raw{shard}", shard))
    return out


def test_merge_requires_every_shard_once_and_stamps_the_map(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/x.py").write_text("def f1():\n    pass\n")
    frags = fragments(tmp_path)
    tmap = tm.merge(frags, shards=tm.SHARDS, commit="c" * 40, ref="refs/heads/main", event="push", run_id="7", root=root)
    assert tm.validate_map(tmap, commit="c" * 40) is None
    assert len(tmap["tests"]) == tm.SHARDS and "src/x.py:f1" in tmap["universe_funcs"]
    assert set(tm.TestMap(tmap).global_opens()) == {f"data{s}.json" for s in range(1, tm.SHARDS + 1)}
    with pytest.raises(ValueError, match="expected 1..8"):
        tm.merge(frags[:-1], shards=tm.SHARDS, commit="c", ref="r", event="push", run_id="1", root=root)
    with pytest.raises(ValueError, match="expected 1..8"):
        tm.merge([*frags[:-1], frags[0]], shards=tm.SHARDS, commit="c", ref="r", event="push", run_id="1", root=root)
    bad = [*frags[:-1], {**frags[-1], "valid": False}]
    with pytest.raises(ValueError, match="invalid"):
        tm.merge(bad, shards=tm.SHARDS, commit="c", ref="r", event="push", run_id="1", root=root)


# ---------------------------------------------------------------- the CLI against a repository


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603,S607 -- fixed argv


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def test_shadow_and_compare_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    (root / "src/app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src/app/core.py").write_text(BASE_SOURCE)
    (root / "tests/test_core.py").write_text("def test_work():\n    pass\n")
    (root / "docs/a.md").write_text("a\n")
    git(root, "init", "-q", "-b", "main")
    mapped_sha = commit_all(root, "mapped")
    (root / "src/app/core.py").write_text(BASE_SOURCE.replace("total += i", "total += 2 * i"))
    base = commit_all(root, "drift: work changed after the map")
    (root / "docs/a.md").write_text("b\n")
    head = commit_all(root, "the pull request: docs only")
    tests = {"tests/test_core.py::test_work": {"f": ["src/app/core.py:work"]},
             "tests/test_core.py::test_two words[a b]": {"f": ["src/app/core.py:register"]}, **many(10)}
    universe = {"universe_files": {"src/app/core.py"}, "universe_funcs": {"src/app/core.py:work",
                                                                          "src/app/core.py:register"}}
    monkeypatch.chdir(root)
    summary = tmp_path / "summary.md"
    out = tmp_path / "selection.json.gz"
    # The map is of the base: the pull request changes docs no test reads.
    tmap = {**build_map(tests, commit=base, **universe), "incomplete_memo": [], "unattributed_memo": []}
    tm.write_gz(tmap, tmp_path / "map.json.gz")
    assert tm.main(["shadow", "--base", base, "--head", head, "--map", str(tmp_path / "map.json.gz"),
                    "--out", str(out), "--summary", str(summary)]) == 0
    selection = tm.read_gz(out)
    assert not selection["full"], selection["reasons"]
    assert selection["selected"] == []
    # The map is of the commit before the base: work() changed in between,
    # so the tests that ran it are selected although the pull request did not touch it.
    tmap = {**build_map(tests, commit=mapped_sha, **universe), "incomplete_memo": [], "unattributed_memo": []}
    tm.write_gz(tmap, tmp_path / "map.json.gz")
    assert tm.main(["shadow", "--base", base, "--head", head, "--map", str(tmp_path / "map.json.gz"),
                    "--out", str(out), "--summary", str(summary)]) == 0
    selection = tm.read_gz(out)
    assert not selection["full"], selection["reasons"]
    assert selection["selected"] == ["tests/test_core.py::test_work"]
    assert "Selected share" in summary.read_text() and "changed between the map" in summary.read_text()
    log = tmp_path / "shard.log"
    log.write_text("FAILED tests/test_core.py::test_work - boom\n"
                   "FAILED tests/test_core.py::test_two words[a b] - boom\n"
                   "FAILED tests/test_new.py::test_n - boom\n")
    assert tm.main(["compare", "--selection", str(out), "--failed", str(log), "--summary", str(summary)]) == 0
    text = summary.read_text()
    assert "failures outside selection: 1 (tests/test_core.py::test_two words[a b])" in text
    assert "failures in this run: 3, of which 0 are collection errors and 1 are tests the map does not know" in text


def test_shadow_refuses_a_map_whose_commit_is_not_an_ancestor_of_the_base(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("x = 1\n")
    git(root, "init", "-q", "-b", "main")
    base = commit_all(root, "base")
    (root / "src/a.py").write_text("x = 2\n")
    newer = commit_all(root, "after the base")
    monkeypatch.chdir(root)
    tm.write_gz({**build_map(many(3), commit=newer), "incomplete_memo": [], "unattributed_memo": []},
                tmp_path / "map.json.gz")
    out = tmp_path / "selection.json.gz"
    assert tm.main(["shadow", "--base", base, "--head", base, "--map", str(tmp_path / "map.json.gz"),
                    "--out", str(out), "--summary", str(tmp_path / "s.md")]) == 0
    selection = tm.read_gz(out)
    assert selection["full"] and "not an ancestor of the base" in selection["reasons"][0]


def test_shadow_never_fails_the_step(tmp_path: Path) -> None:
    out = tmp_path / "selection.json.gz"
    summary = tmp_path / "summary.md"
    assert tm.main(["shadow", "--base", "0" * 40, "--head", "1" * 40, "--map", str(tmp_path / "missing.json.gz"),
                    "--out", str(out), "--summary", str(summary)]) == 0
    selection = tm.read_gz(out)
    assert selection["full"] and "failed" in selection["reasons"][0]
    assert tm.main(["compare", "--selection", str(tmp_path / "none.json.gz"), "--failed", str(summary),
                    "--summary", str(summary)]) == 0


def test_needed_prints_only_github_output_lines_on_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    head = commit_all(root, "x") if (root / "f").write_text("x") else ""
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_REPOSITORY_ID", str(REPO_ID))
    monkeypatch.setattr(tm, "gh_api", fake_api([], [], {}))
    summary = tmp_path / "summary.md"
    assert tm.main(["needed", "--repo", "o/r", "--head", head, "--summary", str(summary)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["record=true"]  # GITHUB_OUTPUT takes stdout whole
    assert "Test map: recording" in summary.read_text() and "no usable map" in summary.read_text()

