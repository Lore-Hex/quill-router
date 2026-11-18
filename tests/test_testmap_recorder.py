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


@functools.lru_cache(maxsize=None)
def spread(n):
    return inner() + n


@functools.lru_cache(maxsize=None)
def many(n):
    return n
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

from pkg import core  # noqa: E402

core.collected()  # at import: the collection window must still record it (restart_events in enter)
{after}
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


def test_n_relative_open_after_cwd_removed(tmp_path):
    # A test that chdirs into a directory and removes it: the recorder must
    # not fail on the next relative open (os.getcwd raises).
    gone = tmp_path / "gone"
    gone.mkdir()
    here = os.getcwd()
    os.chdir(gone)
    try:
        gone.rmdir()
        try:
            open("missing.txt")
        except FileNotFoundError:
            pass
        subprocess.run([sys.executable, "-c", "pass"], check=False, cwd=here)
    finally:
        os.chdir(here)


def test_p_spawn_words(tmp_path):
    subprocess.run(["true", str(tmp_path), "x" * 500, "SECRET_TOKEN=hunter2"], check=False)
    subprocess.run([sys.executable, "-c", "print('secret program text')"], check=False)
    subprocess.run(["env", "FOO=bar", "true"], check=False)
    subprocess.run(["true", "--password=hunter3", "--token", "tok123"], check=False)
    subprocess.run(["true", "-H", "Authorization: Bearer tok456", "https://user:pw789@example.com/p?q=sec0"],
                   check=False)
    subprocess.run(["true", "-e", "console.log('js secret')"], check=False)
    subprocess.run(["true", "--work-tree=" + os.path.join(HERE, "src"), "--format=%H"], check=False)
    subprocess.run(["true", "-uroot", "-phunter4", "db"], check=False)       # mysql
    subprocess.run(["true", "-uuser:pw123", "https://example.com/x"], check=False)  # curl
    subprocess.run(["true", "login", "-p", "dockerpw"], check=False)         # docker login
    subprocess.run(["true", "postgresql://u:p@ss789@db.example/name"], check=False)
    subprocess.run(["true", "--git-dir=" + os.path.join(HERE, ".git"), "log"], check=False)


def test_q_thread_misses():
    import threading

    def work():
        for n in range(5):
            core.spread(n)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(200):
        core.plain()
    for thread in threads:
        thread.join()


def test_r_many_misses():
    import scripts.ci.testmap_recorder as recorder

    bound = recorder.MAX_MISSES
    recorder.MAX_MISSES = 5  # the safety bound, lowered to reach it
    try:
        for n in range(40):
            core.many(n)
    finally:
        recorder.MAX_MISSES = bound


def test_u_polymorphic_call_site():
    # The loop's call site reports its first callee (plain) and is then
    # disabled for this test; the cache's hit count shows the second call
    # (test_c_fills_cache filled the cache).
    for function in (core.plain, core.cached):
        function()


@pytest.fixture(scope="module")
def late():
    return core.setup_value()


def test_s_fixture_after_a_direct_call(request):
    # setup_value() runs in this test's window first, then in the setup of a
    # module fixture opened inside it: that window must record it again.
    assert core.setup_value() == 5
    assert request.getfixturevalue("late") == 5


def test_t_uses_late(late):
    assert late == 5


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


def project(tmp_path: Path, occupy: str = "", after: str = "", tests: str | None = None) -> Path:
    root = tmp_path / "proj"
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/pkg/__init__.py").write_text("")
    (root / "src/pkg/core.py").write_text(CORE)
    (root / "src/pkg/gen.py").write_text(GEN)
    (root / "src/pkg/data.txt").write_text("x")
    (root / "src/pkg/page.html").write_text("hi")
    (root / "tests/conftest.py").write_text(CONFTEST.format(occupy=occupy, after=after))
    (root / "tests/test_sample.py").write_text(TESTS if tests is None else tests)
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
    own = {nodeid: {fragment["keys"][k] for k in fragment["fps"][rt][0]} for nodeid, rt, _ in fragment["tests"]}
    # A generator started in one test and resumed in the next (PY_RESUME).
    assert "src/pkg/gen.py:gen" in fp[t + "test_b_resumes_generator"]["f"]
    # A cache hit runs no Python code: the call is marked, the miss ran the body.
    assert "src/pkg/core.py:cached@memo" in fp[t + "test_d_hits_cache"]["f"]
    assert "src/pkg/core.py:inner" not in fp[t + "test_d_hits_cache"]["f"]
    assert {"src/pkg/core.py:cached", "src/pkg/core.py:inner"} <= fp[t + "test_c_fills_cache"]["f"]
    # A module fixture set up during an earlier test.
    assert "src/pkg/core.py:setup_value" in fp[t + "test_f_fixture_second"]["f"]
    # A module fixture whose setup ran inside another test's window, after
    # that test had run the same function: entering the fixture's window
    # re-arms it, so the fixture's record (inherited here) has it.
    assert "src/pkg/core.py:setup_value" in fp[t + "test_t_uses_late"]["f"]
    assert "src/pkg/core.py:setup_value" not in own[t + "test_t_uses_late"]
    # A call made while the module was collected (its parametrization),
    # although conftest.py already ran it at import: entering the collection
    # window re-arms what ran before it.
    collect = {fragment["keys"][k] for k in fragment["fps"][fragment["collect"]["tests/test_sample.py"]][0]}
    assert "src/pkg/core.py:collected" in collect
    assert "src/pkg/core.py:collected" in fp[t + "test_g_param[2]"]["f"]
    # Reads, listings and commands.
    assert "src/pkg/data.txt" in fp[t + "test_h_reads"]["o"]
    assert "src/pkg" in fp[t + "test_i_lists"]["d"]
    assert any("python" in s and "@cwd=." in s for s in fp[t + "test_j_nested_python_stays_off"]["s"])
    # A template served from Jinja's cache still counts as read.
    assert "src/pkg/page.html" in fp[t + "test_l_render_second"]["o"]
    # An in-process pytest's tests count for the test that ran it and write
    # no records of their own; the outer session's output stays whole.
    assert "src/pkg/gen.py:gen" in fp[t + "test_m_inprocess_pytest"]["f"]
    assert not any("test_inner" in nodeid for nodeid in fp)
    assert fragment["valid"], fragment["errors"]
    # A command's words identify it without leaking: outside paths, program
    # text, environment values and long words are cut.
    spawns = " ".join(fp[t + "test_p_spawn_words"]["s"])
    assert "true <outside> " + "x" * 200 + " SECRET_TOKEN=<value> @cwd=." in spawns
    assert f"{os.path.basename(sys.executable)} -c <program> @cwd=." in spawns
    assert "env FOO=<value> true @cwd=." in spawns
    assert "secret program text" not in spawns and "hunter2" not in spawns and str(tmp_path) not in spawns
    # Option values, a header, a URL's user, password and query, -e program text.
    assert "true --password=<value> --token <value> @cwd=." in spawns
    assert "true -H <value> https://example.com/p @cwd=." in spawns
    assert "true -e <program> @cwd=." in spawns
    assert "true --work-tree=./src --format=<value> @cwd=." in spawns
    assert "true -u<value> -p<value> db @cwd=." in spawns
    assert "true -u<value> https://example.com/x @cwd=." in spawns
    assert "true login -p <value> @cwd=." in spawns
    assert "true postgresql://db.example/name @cwd=." in spawns
    # The repository's .git is the repository's (rel() skips it for opens): S8 must see a path.
    assert "true --git-dir=./.git log @cwd=." in spawns
    for secret in ("hunter3", "tok123", "tok456", "pw789", "sec0", "js secret", "root", "hunter4", "pw123",
                   "dockerpw", "ss789"):
        assert secret not in spawns, secret
    assert "x" * 201 not in spawns
    # Misses on other threads are recorded on their own (and the shard stays valid).
    assert "src/pkg/core.py:spread" in fragment["cached"]
    # Past the safety bound of misses a function is incomplete: its users always run.
    assert fragment["incomplete_memo"] == ["src/pkg/core.py:many"]
    # A hit at a call site that had reported another callee is still marked.
    assert "src/pkg/core.py:cached@memo" in own[t + "test_u_polymorphic_call_site"]
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
    occupy = ("if sys.monitoring.get_tool(3) is None:\n"
              "    sys.monitoring.use_tool_id(3, 'someone else')\n"
              "    sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, lambda code, offset: None)\n"
              "    sys.monitoring.set_events(3, sys.monitoring.events.PY_START)")
    after = ("with open(os.environ['EVENTS_OUT'], 'w') as handle:\n"
             "    handle.write(str(sys.monitoring.get_events(3)))")
    root = project(tmp_path, occupy=occupy, after=after)
    fragment, result = recorded(root, tmp_path, EVENTS_OUT=str(tmp_path / "events"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fragment["valid"] and any("tool 3" in e for e in fragment["errors"])
    # The recorder did not turn off the events of a tool that is not its own.
    assert (tmp_path / "events").read_text() == str(sys.monitoring.events.PY_START)


SABOTAGE = '''
import scripts.ci.testmap_recorder as recorder

from pkg import core


def boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_a_a_failing_callback_does_not_raise():
    setattr(recorder.RECORDER, "{attribute}", boom)
    assert core.spread(7) == 11  # a miss: PY_START opens it, PY_RETURN closes it
    assert core.plain() == 1


def test_b_later_tests_still_run():
    assert core.spread(8) == 12 and core.plain() == 1
'''


@pytest.mark.parametrize(("attribute", "callback"), [("open_miss", "PY_START"), ("close_miss", "PY_RETURN"),
                                                     ("note", "CALL")])
def test_an_error_in_a_callback_never_reaches_the_test(tmp_path: Path, attribute: str, callback: str) -> None:
    root = project(tmp_path, tests=SABOTAGE.replace("{attribute}", attribute))
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert not fragment["valid"]
    assert any(f"{callback}: RuntimeError: boom" in e for e in fragment["errors"]), fragment["errors"]


def test_a_failure_to_name_a_window_stops_the_recorder_not_the_test(tmp_path: Path) -> None:
    tests = '''
import pytest

import scripts.ci.testmap_recorder as recorder


def boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_a_breaks_the_fixture_key():
    recorder._fixture_key = boom


@pytest.fixture(scope="module")
def scoped():
    return 1


def test_b_uses_a_module_fixture(scoped):
    assert scoped == 1


def test_c_later_tests_still_run():
    assert True
'''
    fragment, result = recorded(project(tmp_path, tests=tests), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert not fragment["valid"]
    assert any("fx: RuntimeError: boom" in e for e in fragment["errors"]), fragment["errors"]


CACHES = '''
import asyncio
import contextvars
import functools
import threading


def leaf():
    return 7


def inner_b():
    return 2


def inner_c():
    return 3


def inner_d():
    return 4


def inner_e():
    return 5


def inner_f():
    return 6


@functools.lru_cache(maxsize=None)
def via_c_only():
    return inner_c()


@functools.lru_cache(maxsize=None)
def via_partial():
    return inner_d()


@functools.lru_cache(maxsize=None)
def first_through_c():
    return leaf()


_CACHE = {}


def handrolled():
    if "k" not in _CACHE:
        _CACHE["k"] = inner_b()
    return _CACHE["k"]


_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = inner_e()
    return _CLIENT


_ASYNC_VALUE = None


async def fetch():
    await asyncio.sleep(0)
    return inner_f()


async def cached_async():
    global _ASYNC_VALUE
    if _ASYNC_VALUE is None:
        _ASYNC_VALUE = await fetch()
    return _ASYNC_VALUE


def inner_h():
    return 9


_HASHES = {}  # not named like a cache: a private module dict a function fills and serves


def hashed(key):
    found = _HASHES.get(key)
    if found is None:
        found = inner_h()
        _HASHES[key] = found
    return found


INSIDE = threading.Event()
GO = threading.Event()


def shared():
    return 1


@functools.lru_cache(maxsize=None)
def m1():
    INSIDE.set()
    GO.wait(5)
    return shared()


@functools.lru_cache(maxsize=None)
def m2():
    value = shared()
    GO.set()
    return value


@functools.lru_cache(maxsize=None)
def raising():
    inner_b()
    raise ValueError("no")


def after_raise():
    return 0


def inner_g():
    return 8


def _late_a():
    return inner_g()


def _late_b():
    return inner_g()


# Caches made past functools.lru_cache, as a library might: the recorder
# cannot see them made. One is called directly after a miss through C (a
# late registration); the other only through C, miss and hit.
late_a = functools.update_wrapper(functools._lru_cache_wrapper(_late_a, None, False, functools._CacheInfo), _late_a)
late_b = functools._lru_cache_wrapper(_late_b, None, False, functools._CacheInfo)  # no __wrapped__ even
'''

CACHE_TESTS = '''
import asyncio
import contextvars
import functools
import threading

from pkg import core


def test_01_ctx_fill():
    assert contextvars.copy_context().run(core.via_c_only) == 3


def test_02_ctx_hit():
    assert contextvars.copy_context().run(core.via_c_only) == 3


def test_03_partial_fill():
    assert functools.partial(core.via_partial)() == 4


def test_04_partial_hit():
    assert functools.partial(core.via_partial)() == 4


def test_05_miss_through_c():
    assert contextvars.copy_context().run(core.first_through_c) == 7


def test_06_hit_direct():
    assert core.first_through_c() == 7


def test_07_hand_fill():
    assert core.handrolled() == 2


def test_08_hand_hit():
    assert core.handrolled() == 2


def test_09_singleton_built():
    assert core.client() == 5


def test_10_singleton_reused():
    assert core.client() == 5


def test_11_async_fill():
    assert asyncio.run(core.cached_async()) == 6


def test_12_async_hit():
    assert asyncio.run(core.cached_async()) == 6


def test_13_two_threads_miss_at_once():
    thread = threading.Thread(target=core.m1)
    thread.start()
    core.INSIDE.wait(5)
    core.m2()  # runs shared() while m1 waits; shared() is then disabled for m1's thread too
    thread.join()


def test_14_m1_hit():
    assert core.m1() == 1


def test_15a_private_fill():
    assert core.hashed("k") == 9


def test_15b_private_hit():
    assert core.hashed("k") == 9


def test_15_a_raising_miss_closes():
    try:
        core.raising()
    except ValueError:
        pass
    assert core.after_raise() == 0

'''

LATE_TESTS = '''
import contextvars

from pkg import core


def test_late_and_unseen_caches():
    contextvars.copy_context().run(core.late_a)
    core.late_a()
    contextvars.copy_context().run(core.late_b)
    contextvars.copy_context().run(core.late_b)


def test_other():
    assert core.plain() == 1
'''


def _selected_for(fragment: dict, root: Path, func: str) -> tuple[bool, list[str]]:
    tmap = tm.TestMap({**fragment, "memoized": [], "universe_files": ["src/pkg/core.py"],
                       "universe_funcs": [f"src/pkg/core.py:{func}"], "unattributed_memo": []})
    sel = tm.select([cs.Change("M", "src/pkg/core.py")], tmap, root=root,
                    classify=lambda c: tm.PyChange(funcs={func}), index=lambda: tm.NameIndex({}, {}, {}),
                    weights={})
    return sel.full, sorted(t.split("::")[1] for t in sel.selected)


def test_a_change_selects_the_tests_any_cache_served(tmp_path: Path) -> None:
    root = project(tmp_path, tests=CACHE_TESTS)
    (root / "src/pkg/core.py").write_text(CORE + CACHES)
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fragment["valid"], fragment["errors"]
    # A functools cache called only from C (Context.run, functools.partial):
    # tracked from the moment it is made, so its hit is marked and its miss
    # recorded, and a change to what the miss ran selects the test it served.
    assert _selected_for(fragment, root, "inner_c") == (False, ["test_01_ctx_fill", "test_02_ctx_hit"])
    assert _selected_for(fragment, root, "inner_d") == (False, ["test_03_partial_fill", "test_04_partial_hit"])
    # A cache whose only miss came through C, hit directly later.
    assert _selected_for(fragment, root, "leaf") == (False, ["test_05_miss_through_c", "test_06_hit_direct"])
    # A cache kept by hand in a module dict, a singleton built on first use,
    # and an async one (its windows close at each await and reopen after).
    assert _selected_for(fragment, root, "inner_e") == (False, ["test_09_singleton_built",
                                                               "test_10_singleton_reused"])
    assert _selected_for(fragment, root, "inner_f") == (False, ["test_11_async_fill", "test_12_async_hit"])
    assert _selected_for(fragment, root, "inner_h") == (False, ["test_15a_private_fill", "test_15b_private_hit"])
    full, selected = _selected_for(fragment, root, "inner_b")
    assert not full and {"test_07_hand_fill", "test_08_hand_hit"} <= set(selected)
    # Two misses at once on two threads: the location the first thread's
    # call disables still counts for the other thread's open miss.
    assert _selected_for(fragment, root, "shared") == (False, ["test_13_two_threads_miss_at_once",
                                                              "test_14_m1_hit"])
    # A miss that raises closes there: what its test runs next is not the miss's.
    keys = fragment["keys"]
    raised = {keys[k] for k in fragment["fps"][fragment["cached"]["src/pkg/core.py:raising"]][0]}
    assert "src/pkg/core.py:inner_b" in raised and "src/pkg/core.py:after_raise" not in raised
    assert fragment["incomplete_memo"] == [] and fragment["unattributed_memo"] == []


KEEPS = """
import collections

_MONITOR_HASH = {}
_VERIFIED = collections.OrderedDict()
_ENSURED = set()
_logged = False
_ACTIVE = 0
_CLIENT = None


def monitor_hash(key):
    cached = _MONITOR_HASH.get(key)
    if cached is None:
        cached = len(key)
        _MONITOR_HASH[key] = cached
    return cached


def resolve(key):
    if key in _VERIFIED:
        _VERIFIED.move_to_end(key)
        return _VERIFIED[key]
    return None


def ensure(key):
    _ENSURED.add(key)


def client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = object()
    return _CLIENT


def log_once():
    global _logged
    if not _logged:
        _logged = True


def lazy_init():
    global _ready
    if _ready:
        return
    _populate_registry()
    _ready = True


def ternary_singleton():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = compute() if FLAG else None
    return _CLIENT


def or_none_singleton():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = build() or None
    return _CLIENT


def counter_as_once_flag():
    global _ACTIVE
    if _ACTIVE:
        return
    _load()
    _ACTIVE += 1


def none_as_done_flag():
    global _CLIENT
    if _CLIENT is None:
        return
    _load()
    _CLIENT = None


def reset():
    global _CLIENT
    _CLIENT = None


def count():
    global _ACTIVE
    _ACTIVE += 1
    _ACTIVE -= 1


def from_a_call():
    _make()[1] = 2


def reads_a_constant():
    return VERTEX_DURABLE_CACHE
"""


def test_keeps_cache_windows_private_module_state_but_not_resets_or_counters() -> None:
    from scripts.ci.testmap_recorder import keeps_cache

    module = compile(KEEPS, "keeps.py", "exec")
    functions = {c.co_name: c for c in module.co_consts if hasattr(c, "co_code")}
    # A "done once" flag serves what ran before it was set (lazy_init), and
    # so does a counter or a None the function also reads (`if _n: return`);
    # a store a jump reaches is no constant (`compute() if FLAG else None`,
    # `build() or None`). A reset to None and a counter serve nothing.
    assert {name for name, code in functions.items() if keeps_cache(code)} == {
        "monitor_hash", "resolve", "ensure", "client", "log_once", "lazy_init", "ternary_singleton",
        "or_none_singleton", "counter_as_once_flag", "none_as_done_flag"}


@pytest.mark.parametrize(("argv", "recorded"), [
    # A value glued to its flag holding a keyword (pass, key, token, auth).
    (["mysql", "-uroot", "-pMyPassw0rd", "db"], "mysql -u<value> -p<value> <value>"),
    (["mysql", "-uroot", "-pmonkey_key1", "db"], "mysql -u<value> -p<value> <value>"),
    (["sshpass", "-pTokenAbc9", "ssh"], "sshpass -p<value> <value>"),
    (["curl", "-uapi_key_user", "https://h.example/x"], "curl -u<value> <value>"),
    (["openssl", "-passin", "pass:Zq11v"], "openssl -p<value> <value>"),
    (["tool", "-up", "Zq28v"], "tool -u<value> <value>"),
    # An option after a value flag is read as an option: its program text stays hidden.
    (["python", "-P", "-c", "print('prog text')"], "python -P -c <program>"),
    (["python", "-u", "-c", "import sys; print('prog text')"], "python -u -c <program>"),
    (["bash", "-e", "-c", "echo prog text"], "bash -e -c <program>"),
    (["python", "-P", "-m", "trusted_router.routing_snapshot_worker"],
     "python -P -m trusted_router.routing_snapshot_worker"),
    # A URL's user and password stop at "?" and "#"; scheme-less user:password@host.
    (["curl", "https://h.example/cb?sig=abc@Zq9vTail"], "curl https://h.example/cb"),
    (["curl", "https://user:SEC/RETV@x.example/p"], "curl https://x.example/p"),
    (["tool", "user:Zq24v@host"], "tool <value>@host"),
    # An option's inner NAME=value, and more password options.
    (["curl", "--data=token=Zq1v"], "curl --data=token=<value>"),
    (["redis-cli", "-a", "Zq8v"], "redis-cli -a <value>"),
    (["openssl", "-k", "Zq12v"], "openssl -k <value>"),
    (["tool", "--pw", "Zq16v"], "tool --pw <value>"),
    (["tool", "--bearer", "Zq15v"], "tool --bearer <value>"),
    # A secret that starts with "-" after its flag: an option only if it looks like one.
    (["redis-cli", "-a", "-Zk3yQ"], "redis-cli -a <value>"),
    (["sshpass", "-p", "-x9Zq", "ssh"], "sshpass -p <value> ssh"),
    (["curl", "-H", "-QZq", "https://h.example/x"], "curl -H <value> https://h.example/x"),
    (["openssl", "-k", "-QZq"], "openssl -k <value>"),
    (["git", "log", "-p", "--stat"], "git log -p --stat"),
    # Dotted keys, any option's non-path value, program text after "=".
    (["tool", "-Ddb.password=Zq1"], "tool -Ddb.password=<value>"),
    (["tool", "--db.password=Zq2"], "tool --db.password=<value>"),
    (["helm", "--set", "db.password=Zq3"], "helm --set db.password=<value>"),
    (["tool", "-Dfoo=Zq4"], "tool -Dfoo=<value>"),
    (["node", "--eval=code('Zq5')"], "node --eval=<program>"),
    (["sh", "--command=echo Zq6"], "sh --command=<program>"),
    # Only the recorder's own placeholders and real repository paths survive in an option's value.
    (["tool", "--opt=<Zq7"], "tool --opt=<value>"),
    (["tool", "--opt=.Zq8"], "tool --opt=<value>"),
    (["tool", ".Zq9"], "tool <value>"),
    # A secret glued to -a or -k shows none of its characters.
    (["redis-cli", "-ahunter2Zq"], "redis-cli -a<value>"),
    (["openssl", "-khunter2Zq"], "openssl -k<value>"),
    # A single-dash word holding a keyword past its first letter: all of it.
    (["tool", "-tTokenAbc"], "tool -<value>"),
    # A keyword after a leading -a or -k, and keyword words carrying a
    # punctuation character, are hidden whole too; the word after a wholly
    # hidden value flag is still its value.
    (["tool", "-aToken"], "tool -<value>"),
    (["tool", "-keyZq"], "tool -<value>"),
    (["tool", "-tToken:Zq"], "tool -<value>"),
    (["tool", "-tToken", "Zqv"], "tool -<value> <value>"),
    (["tool", "-tmysecrettoken"], "tool -<value>"),
    (["tool", "-sbearerXYZ"], "tool -<value>"),
    (["tool", "-Ccookie_val"], "tool -<value>"),
    (["tool", "-ckeyZq"], "tool -<value>"),
    # A "."-prefixed repository path that exists is kept.
    (["tool", "./scripts/ci"], "tool ./scripts/ci"),
    (["tool", "--from=./scripts/ci"], "tool --from=./scripts/ci"),
])
def test_command_words_hide_secrets_and_program_text(tmp_path: Path, argv: list[str], recorded: str) -> None:
    from scripts.ci import testmap_recorder

    words = " ".join(testmap_recorder.Recorder(str(tmp_path)).words(argv))
    assert words == recorded
    for secret in ("MyPassw0rd", "monkey_key1", "TokenAbc", "api_key_user", "Zq", "prog text", "hunter2",
                   "mysecret", "bearerXYZ", "cookie_val"):
        assert secret not in words


def test_caches_made_past_functools_lru_cache_are_incomplete_or_unattributed(tmp_path: Path) -> None:
    root = project(tmp_path, tests=LATE_TESTS)
    (root / "src/pkg/core.py").write_text(CORE + CACHES)
    fragment, result = recorded(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fragment["valid"], fragment["errors"]
    # One first called directly after a miss through C is incomplete (its
    # users always run); one that only ever served through C served a test
    # nothing names, and the selector runs everything.
    assert fragment["incomplete_memo"] == ["src/pkg/core.py:_late_a"]
    assert fragment["unattributed_memo"] == ["src/pkg/core.py:_late_b"]
    sel = tm.select([cs.Change("M", "src/pkg/core.py")], tm.TestMap({**fragment, "memoized": []}), root=root,
                    classify=lambda c: tm.PyChange(funcs={"plain"}), index=lambda: tm.NameIndex({}, {}, {}),
                    weights={})
    assert sel.full and "_late_b" in sel.reasons[0]


@pytest.mark.parametrize("workers", ["2"])
def test_xdist_workers_record_and_the_shard_compacts(tmp_path: Path, workers: str) -> None:
    root = project(tmp_path)
    # loadfile keeps the generator's two tests in one worker.
    fragment, result = recorded(root, tmp_path, "-n", workers, "--dist", "loadfile")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fragment["valid"], fragment["errors"]
    assert len(fragment["tests"]) == 21
    assert len(list((tmp_path / "raw").glob("gw*.jsonl"))) == int(workers)
