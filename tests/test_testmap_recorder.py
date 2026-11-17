"""scripts/ci/testmap_recorder.py, run on a small synthetic project.

Each case runs pytest in a subprocess, in a project of its own, with the
recorder loaded the way tests/conftest.py loads it. The headline check: for
every test, the files coverage.py's per-test contexts (C tracer) measure are
inside the files the recorder attributes to that test. The rest are the
attributions coverage cannot make (a memoized hit, a module fixture set up by
an earlier test, collection-time calls, a generator resumed in a later test)
and the opt-in and isolation guarantees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.ci import commit_scope as cs
from scripts.ci import testmap as tm

REPO = Path(__file__).resolve().parents[1]

CORE = '''
import functools
import os


def plain():
    return 1


@functools.lru_cache(maxsize=None)
def cached():
    return inner()


def inner():
    return 4


def collected():
    return [1, 2]


def setup_value():
    return 5


def reads(path):
    with open(path) as handle:
        return handle.read()


def lists(path):
    return sorted(os.listdir(path))
'''

GEN = '''
def gen():
    yield 1
    total = 0
    for i in range(3):
        total += i
    yield total
'''

CONFTEST = '''
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
{occupy}
if os.environ.get("TR_TESTMAP_OUT"):
    import scripts.ci.testmap_recorder  # noqa: F401

    pytest_plugins = ["scripts.ci.testmap_recorder"]
'''

TESTS = '''
import os
import subprocess
import sys

import jinja2
import pytest

from pkg import core, gen

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = gen.gen()
PARAMS = core.collected()
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(HERE, "src", "pkg")))


@pytest.fixture(scope="module")
def shared():
    return core.setup_value()


def test_a_starts_generator():
    assert next(GEN) == 1


def test_b_resumes_generator():
    assert next(GEN) == 3


def test_c_fills_cache():
    assert core.cached() == 4


def test_d_hits_cache():
    assert core.cached() == 4


def test_e_fixture_first(shared):
    assert shared == 5


def test_f_fixture_second(shared):
    assert shared == 5


@pytest.mark.parametrize("n", PARAMS)
def test_g_param(n):
    assert core.plain() == 1


def test_h_reads():
    assert core.reads(os.path.join(HERE, "src", "pkg", "data.txt")) == "x"


def test_i_lists():
    assert "core.py" in core.lists(os.path.join(HERE, "src", "pkg"))


def test_j_nested_python_stays_off():
    out = subprocess.run([sys.executable, "-c",
                          "import scripts.ci.testmap_recorder as r; print(r.RECORDER is None)"],
                         check=True, capture_output=True, text=True).stdout
    assert out.strip() == "True"


def test_k_render_first():
    assert ENV.get_template("page.html").render() == "hi"


def test_l_render_second():
    assert ENV.get_template("page.html").render() == "hi"


def test_m_inprocess_pytest(tmp_path):
    # As tests/test_lock_order_guard.py does: pytest.main on a copy of conftest.
    with open(os.path.join(HERE, "tests", "conftest.py")) as handle:
        (tmp_path / "conftest.py").write_text(handle.read())
    inner = tmp_path / "test_inner.py"
    inner.write_text(chr(10).join(["from pkg import gen", "", "def test_inner():",
                                   "    assert next(gen.gen()) == 1", ""]))
    assert pytest.main([str(inner), "-q", "-p", "no:cacheprovider", "--confcutdir=" + str(tmp_path),
                        "-o", "addopts=", "--import-mode=importlib"]) == 0
'''


def project(tmp_path: Path, occupy: str = "") -> Path:
    root = tmp_path / "proj"
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/pkg/__init__.py").write_text("")
    (root / "src/pkg/core.py").write_text(CORE)
    (root / "src/pkg/gen.py").write_text(GEN)
    (root / "src/pkg/data.txt").write_text("x")
    (root / "src/pkg/page.html").write_text("hi")
    (root / "tests/conftest.py").write_text(CONFTEST.format(occupy=occupy))
    (root / "tests/test_sample.py").write_text(TESTS)
    return root


def run(root: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",  # noqa: S603,S607 -- fixed argv
                           "-o", "addopts=", "--rootdir", str(root), "tests", *args],
                          cwd=root, env={**base, "PYTHONPATH": str(REPO), **env},
                          capture_output=True, text=True, timeout=300)


def recorded(root: Path, tmp_path: Path, *args: str, **env: str) -> tuple[dict, subprocess.CompletedProcess[str]]:
    out = tmp_path / "raw"
    result = run(root, *args, TR_TESTMAP_OUT=str(out), **env)
    return tm.compact(out, 1), result


def effective(fragment: dict) -> dict[str, dict[str, set[str]]]:
    """Each test's footprint with its collection and scoped fixtures folded
    in, as the selector reads it."""
    fake = tm.TestMap({**fragment, "memoized": []})  # misses stay out: the attribution tests read them
    out = {}
    for nodeid, rt, fx in fake.tests:
        ids = [rt, *fx]
        module = nodeid.split("::")[0]
        if module in fake.collect:
            ids.append(fake.collect[module])
        parts: list[set[str]] = [set(), set(), set(), set()]
        for i in ids:
            for n, part in enumerate(fake.fps[i]):
                parts[n] |= {fake.keys[k] for k in part}
        out[nodeid] = dict(zip(tm.PARTS, parts, strict=True))
    return out


def test_the_recorder_holds_every_file_coverage_measures_per_test(tmp_path: Path) -> None:
    root = project(tmp_path)
    fragment, result = recorded(root, tmp_path, "--cov=src", "--cov-context=test", "--cov-report=",
                                COVERAGE_CORE="ctrace", COVERAGE_FILE=str(tmp_path / ".coverage"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert fragment["valid"], fragment["errors"]
    import coverage

    data = coverage.CoverageData(str(tmp_path / ".coverage"))
    data.read()
    measured: dict[str, set[str]] = {}
    for path in data.measured_files():
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        for contexts in data.contexts_by_lineno(path).values():
            for context in contexts:
                if context:
                    measured.setdefault(context.rsplit("|", 1)[0], set()).add(rel)
    footprints = effective(fragment)
    assert set(measured) <= set(footprints)
    assert len(measured) >= 8  # the comparison covered most tests, not none
    for nodeid, files in measured.items():
        recorder_files = {key.split(":", 1)[0] for key in footprints[nodeid]["f"]}
        assert files <= recorder_files, (nodeid, files - recorder_files)


def test_what_coverage_cannot_attribute_the_recorder_does(tmp_path: Path) -> None:
    root = project(tmp_path)
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    fp = effective(fragment)
    t = "tests/test_sample.py::"
    # A generator started in one test and resumed in the next (PY_RESUME).
    assert "src/pkg/gen.py:gen" in fp[t + "test_b_resumes_generator"]["f"]
    # A cache hit runs no Python code: the call is marked, the miss ran the body.
    assert "src/pkg/core.py:cached@memo" in fp[t + "test_d_hits_cache"]["f"]
    assert "src/pkg/core.py:inner" not in fp[t + "test_d_hits_cache"]["f"]
    assert {"src/pkg/core.py:cached", "src/pkg/core.py:inner"} <= fp[t + "test_c_fills_cache"]["f"]
    # A module fixture set up during an earlier test.
    assert "src/pkg/core.py:setup_value" in fp[t + "test_f_fixture_second"]["f"]
    # A call made while the module was collected (its parametrization).
    assert "src/pkg/core.py:collected" in fp[t + "test_g_param[2]"]["f"]
    # Reads, listings and commands.
    assert "src/pkg/data.txt" in fp[t + "test_h_reads"]["o"]
    assert "src/pkg" in fp[t + "test_i_lists"]["d"]
    assert any("python" in s and "@cwd=." in s for s in fp[t + "test_j_nested_python_stays_off"]["s"])
    # Jinja renders from source while recording: the second render reads it too.
    assert "src/pkg/page.html" in fp[t + "test_l_render_second"]["o"]
    # An in-process pytest's tests count for the test that ran it and write
    # no records of their own; the outer session's output stays whole.
    assert "src/pkg/gen.py:gen" in fp[t + "test_m_inprocess_pytest"]["f"]
    assert not any("test_inner" in nodeid for nodeid in fp)
    assert fragment["valid"], fragment["errors"]
    # Importing a module is not reading it.
    assert not any(o.endswith(".py") for o in fp[t + "test_a_starts_generator"]["o"])
    # Positive control for the attributions above: none is in the test's own window.
    own = {nodeid: {fragment["keys"][k] for k in fragment["fps"][rt][0]} for nodeid, rt, _ in fragment["tests"]}
    assert "src/pkg/core.py:setup_value" not in own[t + "test_f_fixture_second"]
    # The miss is recorded on its own, and the cache's later user inherits it:
    # a change to inner() selects the test the cache served.
    miss = fragment["fps"][fragment["cached"]["src/pkg/core.py:cached"]]
    assert "src/pkg/core.py:inner" in {fragment["keys"][k] for k in miss[0]}
    tmap = tm.TestMap({**fragment, "memoized": ["src/pkg/core.py:cached"],
                       "universe_files": ["src/pkg/core.py"], "universe_funcs": ["src/pkg/core.py:inner"]})
    sel = tm.select([cs.Change("M", "src/pkg/core.py")], tmap, root=root,
                    classify=lambda c: tm.PyChange(funcs={"inner"}), index=lambda: tm.NameIndex({}, {}, {}),
                    weights={})
    assert {t + "test_c_fills_cache", t + "test_d_hits_cache"} <= set(sel.selected)


def test_the_recorder_is_opt_in_and_a_nested_process_stays_off(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = run(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "raw").exists()
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0
    # One process wrote output: the session's; the nested python did not.
    assert len(list((tmp_path / "raw").glob("*.jsonl"))) == 1
    header = json.loads((next((tmp_path / "raw").glob("*.jsonl"))).read_text().split("\n", 1)[0])
    assert header["h"]["recorder"] == tm.RECORDER_VERSION


def test_a_taken_monitoring_tool_invalidates_the_shard_without_failing_tests(tmp_path: Path) -> None:
    occupy = "if sys.monitoring.get_tool(3) is None:\n    sys.monitoring.use_tool_id(3, 'someone else')"
    root = project(tmp_path, occupy=textwrap.dedent(occupy))
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fragment["valid"] and any("tool 3" in e for e in fragment["errors"])


@pytest.mark.parametrize("workers", ["2"])
def test_xdist_workers_record_and_the_shard_compacts(tmp_path: Path, workers: str) -> None:
    root = project(tmp_path)
    # loadfile keeps the generator's two tests in one worker.
    fragment, result = recorded(root, tmp_path, "-n", workers, "--dist", "loadfile")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fragment["valid"], fragment["errors"]
    assert len(fragment["tests"]) == 14
    assert len(list((tmp_path / "raw").glob("gw*.jsonl"))) == int(workers)
