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
        "cached": {c: fp(f) for c, f in (cached or {}).items()},
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
             "tests/test_cache.py::test_hit": {"f": [f"{SRC}:helper"]}}
    tmap = base_map(tests=tests,
                    fixtures={"tests/test_fx.py::test_uses_fixture": [{"f": [f"{SRC}:work"]}]},
                    collect={"tests/test_col.py": {"f": [f"{SRC}:work"]}},
                    cached={f"{SRC}:helper": {"f": [f"{SRC}:work"]}})
    sel = run_select(tmap, [("M", SRC)], py={SRC: changed("work")})
    assert set(sel.selected) == {"tests/test_a.py::test_work", "tests/test_fx.py::test_uses_fixture",
                                 "tests/test_col.py::test_collected", "tests/test_cache.py::test_hit"}
    # Positive control: without the inherited footprints none of the three is selected.
    bare = base_map(tests=tests)
    assert run_select(bare, [("M", SRC)], py={SRC: changed("work")}).selected == ["tests/test_a.py::test_work"]


def test_s4_a_module_level_name_selects_what_names_it_transitively() -> None:
    index = tm.NameIndex(funcs_by_ident={"DERIVED": {f"{SRC}:work"}}, defined_from={"LIMIT": {"DERIVED"}}, universe={})
    sel = run_select(base_map(), [("M", SRC)], py={SRC: changed(kinds=(("assign", "LIMIT"),))}, index=index)
    assert sel.selected == ["tests/test_a.py::test_work"]
    inert = run_select(base_map(), [("M", SRC)], py={SRC: changed(kinds=(("import", ""), ("docstring", "")))})
    assert not inert.full and inert.selected == []


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


def test_s8_spawned_commands() -> None:
    tests = {"tests/test_p.py::test_python": {"s": [".venv/bin/python -c x @cwd=-"]},
             "tests/test_g.py::test_git_here": {"s": ["git ls-files @cwd=."]},
             "tests/test_g.py::test_git_elsewhere": {"s": ["git init @cwd=-"]},
             "tests/test_sh.py::test_script": {"s": ["bash scripts/deploy/run.sh @cwd=-"]}}
    tmap = base_map(tests=tests)
    sel = run_select(tmap, [("M", "docs/readme.md")])
    assert set(sel.selected) == {"tests/test_p.py::test_python", "tests/test_g.py::test_git_here"}
    sel = run_select(tmap, [("M", "scripts/deploy/lib.sh")])
    assert "tests/test_sh.py::test_script" in sel.selected
    assert "tests/test_g.py::test_git_elsewhere" not in sel.selected


def test_s9_a_helper_selects_its_importers_but_not_through_conftest(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "tests/helper.py").write_text("def make():\n    return 1\n")
    (tmp_path / "tests/conftest.py").write_text("from tests.helper import make\n")
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


def test_failed_ids_reads_pytest_short_summaries() -> None:
    log = ("2026-11-17T01:02:03.000Z FAILED tests/test_a.py::test_x[1] - AssertionError: no\n"
           "ERROR tests/conformance/test_c.py::test_y[backend=spanner-pg]@conformance-spanner-pg - boom\n"
           "FAILED tests/test_a.py\n")
    assert tm.failed_ids(log) == {"tests/test_a.py::test_x[1]",
                                  "tests/conformance/test_c.py::test_y[backend=spanner-pg]"}


# ---------------------------------------------------------------- map validity and lookup


def test_validate_map_checks_schema_recorder_event_shards_and_commit() -> None:
    good = base_map()
    assert tm.validate_map(good) is None
    assert tm.validate_map(good, commit="a" * 40) is None
    for change, words in (({"schema": 99}, "schema"), ({"recorder": "0"}, "recorder"),
                          ({"event": "pull_request"}, "not a push to main"),
                          ({"ref": "refs/heads/feature"}, "not a push to main"),
                          ({"fragments": [1, 2, 3]}, "fragments"), ({"shards": 4}, "fragments")):
        assert words in (tm.validate_map({**good, **change}) or ""), change
    assert "commit" in (tm.validate_map(good, commit="b" * 40) or "")


def fake_api(commits: list[dict], artifacts: list[dict], maps: dict[int, dict]):
    def api(path: str, raw: bool = False):
        if "/commits?" in path:
            return commits
        if path.endswith("/zip"):
            art = int(path.split("/artifacts/")[1].split("/")[0])
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("testmap.json.gz", gzip.compress(json.dumps(maps[art]).encode()))
            return buf.getvalue()
        if "/artifacts?" in path:
            return {"artifacts": artifacts}
        raise AssertionError(path)
    return api


def commit(sha: str, hours: float) -> dict:
    return {"sha": sha, "commit": {"committer": {"date": f"2026-11-17T{24 - hours:02.0f}:00:00Z"
                                                 if hours < 24 else "2026-11-15T00:00:00Z"}}}


def artifact(art_id: int, sha: str, *, branch: str = "main", expired: bool = False) -> dict:
    return {"id": art_id, "expired": expired, "created_at": "2026-11-17T00:00:00Z",
            "workflow_run": {"head_sha": sha, "head_branch": branch}}


def test_find_map_takes_the_newest_valid_map_at_or_before_the_base() -> None:
    shas = [c * 40 for c in "abcd"]
    commits = [commit(shas[0], 1), commit(shas[1], 2), commit(shas[2], 3), commit(shas[3], 4)]
    maps = {2: base_map(commit=shas[2]), 3: base_map(commit=shas[3])}
    artifacts = [artifact(9, shas[1], branch="feature"), artifact(8, shas[1], expired=True),
                 artifact(2, shas[2]), artifact(3, shas[3])]
    found, why = tm.find_map("o/r", shas[0], api=fake_api(commits, artifacts, maps))
    assert why == "" and found is not None and found["commit"] == shas[2] and found["_distance"] == 2


def test_find_map_refuses_stale_mismatched_or_missing_maps() -> None:
    shas = [c * 40 for c in "ab"]
    # Older than MAX_AGE_HOURS before the base.
    found, why = tm.find_map("o/r", shas[0], api=fake_api(
        [commit(shas[0], 1), commit(shas[1], 30)], [artifact(1, shas[1])], {1: base_map(commit=shas[1])}))
    assert found is None and "older than the base" in why
    # A map whose stamped commit is not the commit its run built.
    found, why = tm.find_map("o/r", shas[0], api=fake_api(
        [commit(shas[0], 1), commit(shas[1], 2)], [artifact(1, shas[1])], {1: base_map(commit="c" * 40)}))
    assert found is None and "commit" in why
    # Another recorder version.
    found, why = tm.find_map("o/r", shas[0], api=fake_api(
        [commit(shas[0], 1)], [artifact(1, shas[0])], {1: {**base_map(commit=shas[0]), "recorder": "0"}}))
    assert found is None and "recorder" in why
    # Beyond MAX_DISTANCE commits, or none at all.
    far = [commit(f"{i:040x}", 1) for i in range(tm.MAX_DISTANCE + 5)]
    found, why = tm.find_map("o/r", far[0]["sha"], api=fake_api(far, [artifact(1, far[-1]["sha"])], {}))
    assert found is None and "no map" in why


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


def test_shadow_and_compare_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    (root / "src/app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/app/core.py").write_text(BASE_SOURCE)
    (root / "tests/test_core.py").write_text("def test_work():\n    pass\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "src/app/core.py").write_text(BASE_SOURCE.replace("total += i", "total += 2 * i"))
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "head")
    head = git(root, "rev-parse", "HEAD")
    tmap = build_map({"tests/test_core.py::test_work": {"f": ["src/app/core.py:work"]}, **many(10)},
                     universe_files={"src/app/core.py"}, universe_funcs={"src/app/core.py:work"})
    tm.write_gz(tmap, tmp_path / "map.json.gz")
    summary = tmp_path / "summary.md"
    out = tmp_path / "selection.json.gz"
    monkeypatch.chdir(root)
    assert tm.main(["shadow", "--base", base, "--head", head, "--map", str(tmp_path / "map.json.gz"),
                    "--out", str(out), "--summary", str(summary)]) == 0
    selection = tm.read_gz(out)
    assert not selection["full"], selection["reasons"]
    assert selection["selected"] == ["tests/test_core.py::test_work"]
    assert "Selected share" in summary.read_text()
    failed = tmp_path / "failed.txt"
    failed.write_text("tests/test_core.py::test_work\ntests/test_filler.py::test_3\ntests/test_new.py::test_n\n")
    assert tm.main(["compare", "--selection", str(out), "--failed", str(failed), "--summary", str(summary)]) == 0
    assert "failures outside selection: 1 (tests/test_filler.py::test_3)" in summary.read_text()


def test_shadow_never_fails_the_step(tmp_path: Path) -> None:
    out = tmp_path / "selection.json.gz"
    summary = tmp_path / "summary.md"
    assert tm.main(["shadow", "--base", "0" * 40, "--head", "1" * 40, "--map", str(tmp_path / "missing.json.gz"),
                    "--out", str(out), "--summary", str(summary)]) == 0
    selection = tm.read_gz(out)
    assert selection["full"] and "failed" in selection["reasons"][0]
    assert tm.main(["compare", "--selection", str(tmp_path / "none.json.gz"), "--failed", str(summary),
                    "--summary", str(summary)]) == 0
