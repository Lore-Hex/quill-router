"""Record what each test runs and reads: the per-test footprint map's source.

Opt-in. Nothing happens unless TR_TESTMAP_OUT names an output directory, and
tests/conftest.py imports this module only then, before any application
import (an import-time read or memoized function would otherwise go
unrecorded). ci.yml sets it on pushes to main (and on a dispatch asking for
test_map) only; pull request shards and local runs never load it.
scripts/ci/testmap.py compacts the output and holds the selection rules.

What a test's window (setup, call and teardown) records:
  f  every function that starts or resumes under the repository: a
     sys.monitoring PY_START/PY_RESUME callback (tool id 3; the suite's frozen
     execution guard takes 4) that returns DISABLE after the first hit, so a
     location costs one callback per window; restart_events() re-arms every
     location when a window opens or closes. Resume events catch a generator
     or coroutine started in an earlier test.
  o  repository files opened (the "open" audit event). The import system's
     own reads of .py/.pyc files are not reads: executing the module is
     what the f set records.
  d  repository directories listed (os.listdir, os.scandir, glob.glob,
     os.walk through scandir).
  s  commands started (subprocess.Popen, os.posix_spawn, os.exec*, os.system):
     the first argv words, repository paths made relative, and the working
     directory relative to the repository ("-" outside it).
Separate windows, each written once and inherited by the tests that use it:
  c   a test module's collection (its import and parametrization);
  fx  the setup of a module, class, package or session fixture;
  cf  a memoized function's miss (functools.cache/lru_cache are wrapped while
      recording, so a later hit still marks the function as used and the
      user inherits what the miss ran; the first 20 misses per function);
  g   everything outside every window: imports, session and collection hooks.
Jinja's template cache is off while recording, so every render reads its
template files.

Why opens inside shutil.copytree are kept: the deploy-script tests copy the
repository and run shell scripts and git inside the copy. Those processes'
reads are invisible here; the copy's reads are the only record that the
test depends on the copied files, so dropping them would let a change to a
script those tests run select nothing.

PYTEST_DONT_REWRITE: tests/conftest.py imports this module before naming it
in pytest_plugins, and a test that runs pytest in-process names it again;
without the marker pytest warns that it cannot rewrite an imported module,
and that extra warning changes the inner session's summary line, which
tests/test_lock_order_guard.py asserts on.

The recorder never raises into a test: an internal error writes an "err"
record, stops recording, and makes the shard's fragment invalid, so no map
is built from it.

Processes: the process that first loads the recorder owns the output (it
sets TR_TESTMAP_OWNER); its xdist workers (direct children) record too. A
process a test starts (a nested pytest, a subprocess importing the app) sees
an owner that is not its parent and stays off.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest

try:
    from scripts.ci.testmap import RECORDER_VERSION
except ImportError:  # pragma: no cover - loaded by path in a synthetic project
    RECORDER_VERSION = "1"

TOOL_ID = 3
MAX_MISSES = 20
OUT_ENV = "TR_TESTMAP_OUT"
OWNER_ENV = "TR_TESTMAP_OWNER"
ROOT = os.path.realpath(os.getcwd()) + os.sep
SKIP_PARTS = ("/.venv/", "/.git/", "/__pycache__/", "/.hypothesis/", "/.pytest_cache/", "/.mypy_cache/",
              "/.ruff_cache/", "/node_modules/", "/site-packages/")
# Who opens a .py file without the test depending on its text: the import
# system and pytest's assertion rewriting (executing the module is what the
# f set records), warning or traceback display reading a source line, and
# Hypothesis harvesting literal constants from every loaded module at
# collection (it biases which examples are tried, never what a test
# asserts; counting it would put every module in the global window).
# inspect.getsource reads through linecache too, and that read counts.
IMPORT_FRAMES = ("<frozen importlib._bootstrap", "/_pytest/assertion/rewrite.py",
                 "/hypothesis/internal/constants_ast.py")
DISPLAY_FRAMES = ("/warnings.py", "/traceback.py")
mon = sys.monitoring


class Window:
    __slots__ = ("codes", "opens", "dirs", "spawns")

    def __init__(self) -> None:
        self.codes: set[Any] = set()
        self.opens: set[str] = set()
        self.dirs: set[str] = set()
        self.spawns: set[str] = set()


class Recorder:
    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.glob = Window()
        self.cur = self.glob
        self.active = True
        self.lock = threading.Lock()
        self.misses: dict[str, int] = {}
        self.config: Any = None  # the session this process records; see pytest_configure
        self.paths: dict[str, str | None] = {}
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        os.makedirs(out_dir, exist_ok=True)
        self.out = open(os.path.join(out_dir, f"{worker}-{os.getpid()}.jsonl"), "w", encoding="utf-8")  # noqa: SIM115
        self.write({"h": {"recorder": RECORDER_VERSION, "root": ROOT, "pid": os.getpid(), "worker": worker,
                          "python": sys.version.split()[0]}})

    # -- output
    def write(self, record: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(record, separators=(",", ":")) + "\n")

    def fail(self, where: str, error: BaseException) -> None:
        if not self.active:
            return
        self.active = False
        try:
            mon.set_events(TOOL_ID, 0)
        except Exception:  # noqa: S110 - already broken, keep the test running
            pass
        try:
            self.write({"err": f"{where}: {type(error).__name__}: {error}"})
            self.out.flush()
        except Exception:  # noqa: S110
            pass

    def code_path(self, filename: str) -> str | None:
        """A code object's file relative to the repository, or None."""
        found = self.paths.get(filename, "")
        if found == "":
            found = self.paths[filename] = self.rel(filename) if not filename.startswith("<") else None
        return found

    def dump(self, window: Window) -> dict:
        funcs = set()
        for code in list(window.codes):
            path = self.code_path(code.co_filename)
            if path is not None:
                funcs.add(f"{path}:{code.co_qualname}")
        return {"f": sorted(funcs), "o": sorted(window.opens), "d": sorted(window.dirs), "s": sorted(window.spawns)}

    # -- windows
    def enter(self) -> Window:
        prev = self.cur
        self.cur = Window()
        mon.restart_events()
        return prev

    def leave(self, prev: Window) -> Window:
        window = self.cur
        if prev is not self.glob:  # what ran in a nested window ran in the outer one too
            prev.codes |= window.codes
            prev.opens |= window.opens
            prev.dirs |= window.dirs
            prev.spawns |= window.spawns
        self.cur = prev
        mon.restart_events()
        return window

    # -- event sources
    def on_code(self, code: Any, offset: int, _disable: Any = sys.monitoring.DISABLE) -> Any:
        # DISABLE is bound at definition: module globals are cleared at
        # interpreter shutdown, while a callback can still fire.
        self.cur.codes.add(code)
        return _disable

    def rel(self, path: Any) -> str | None:
        try:
            text = os.fsdecode(path)
        except TypeError:
            return None
        if not os.path.isabs(text):
            text = os.path.join(os.getcwd(), text)
        text = os.path.realpath(text) + os.sep
        if not text.startswith(ROOT) or any(part in text for part in SKIP_PARTS):
            return None
        return text[len(ROOT):-1] or "."

    def audit(self, event: str, args: tuple) -> None:
        if not self.active:
            return
        try:
            if event == "open":
                if not args or isinstance(args[0], int):
                    return
                rel = self.rel(args[0])
                if rel is None or rel.endswith(".pyc"):
                    return
                if rel.endswith(".py") and self.importing():
                    return
                self.cur.opens.add(rel)
            elif event in ("os.listdir", "os.scandir", "glob.glob"):
                if args and args[0] is not None and not isinstance(args[0], int):
                    rel = self.rel(args[0])
                    if rel is not None:
                        self.cur.dirs.add(rel)
            elif event == "subprocess.Popen":
                self.spawn(args[1], args[2])
            elif event in ("os.posix_spawn", "os.exec"):
                self.spawn(args[1], None)
            elif event == "os.system":
                self.spawn(str(args[0]).split(), None)
        except Exception as error:
            self.fail(f"audit {event}", error)

    def importing(self) -> bool:
        """Is the import system (or pytest's assertion rewriting, which reads
        a test module to import it) the one opening this file?"""
        frame = sys._getframe(1)
        display = False
        for _ in range(16):
            if frame is None:
                break
            name = frame.f_code.co_filename
            if any(marker in name for marker in IMPORT_FRAMES):
                return True
            if name.endswith("/inspect.py"):
                return False
            display = display or name.endswith(DISPLAY_FRAMES)
            frame = frame.f_back
        return display

    def spawn(self, argv: Any, cwd: Any) -> None:
        if isinstance(argv, (str, bytes, os.PathLike)):
            words = [os.fsdecode(argv)]
        else:
            words = [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a) for a in list(argv)[:4]]
        words = [(self.rel(w) or w) if os.path.isabs(w) or w.startswith(".") else w for w in words]
        where = self.rel(cwd if cwd is not None else os.getcwd())
        self.cur.spawns.add(" ".join(" ".join(words).split()) + f" @cwd={where or '-'}")

    # -- memoized functions
    def cached(self, fn: Callable, make: Callable[[Callable], Callable]) -> Callable:
        code = getattr(fn, "__code__", None)
        path = self.code_path(code.co_filename) if code is not None else None
        if code is None or path is None:
            return make(fn)
        key = f"{path}:{code.co_qualname}"
        recorder = self

        @functools.wraps(fn)
        def miss(*args: Any, **kwargs: Any) -> Any:
            if not recorder.active or recorder.misses.get(key, 0) >= MAX_MISSES:
                return fn(*args, **kwargs)
            recorder.misses[key] = recorder.misses.get(key, 0) + 1
            prev = recorder.enter()
            try:
                return fn(*args, **kwargs)
            finally:
                window = recorder.leave(prev)
                try:
                    recorder.write({"cf": key, **recorder.dump(window)})
                except Exception as error:
                    recorder.fail("cached", error)

        wrapped = make(miss)

        @functools.wraps(fn)
        def used(*args: Any, **kwargs: Any) -> Any:
            recorder.cur.codes.add(code)
            return wrapped(*args, **kwargs)

        used.cache_info = wrapped.cache_info  # type: ignore[attr-defined]
        used.cache_clear = wrapped.cache_clear  # type: ignore[attr-defined]
        if hasattr(wrapped, "cache_parameters"):
            used.cache_parameters = wrapped.cache_parameters  # type: ignore[attr-defined]
        return used


RECORDER: Recorder | None = None


def _start() -> Recorder | None:
    out_dir = os.environ.get(OUT_ENV, "")
    if not out_dir:
        return None
    owner = os.environ.get(OWNER_ENV)
    if owner is None:
        os.environ[OWNER_ENV] = str(os.getpid())
    elif not (os.environ.get("PYTEST_XDIST_WORKER") and owner == str(os.getppid())):
        return None  # a process some test started: not part of this session
    try:
        recorder = Recorder(out_dir)
    except OSError as error:  # no output, no fragment, no map: the tests still run
        print(f"testmap recorder off: {error}", file=sys.stderr)
        return None
    try:
        mon.use_tool_id(TOOL_ID, "testmap")
    except ValueError as error:
        recorder.fail("sys.monitoring tool 3 is taken", error)
        return recorder
    mon.register_callback(TOOL_ID, mon.events.PY_START, recorder.on_code)
    mon.register_callback(TOOL_ID, mon.events.PY_RESUME, recorder.on_code)
    mon.set_events(TOOL_ID, mon.events.PY_START | mon.events.PY_RESUME)
    sys.addaudithook(recorder.audit)
    original_lru = functools.lru_cache

    def lru_cache(maxsize: Any = 128, typed: bool = False) -> Any:
        if callable(maxsize) and not isinstance(maxsize, bool):
            return recorder.cached(maxsize, original_lru(128, typed))
        return lambda fn: recorder.cached(fn, original_lru(maxsize, typed))

    def cache(fn: Callable) -> Callable:
        return recorder.cached(fn, original_lru(None))

    functools.lru_cache = lru_cache  # type: ignore[assignment]
    functools.cache = cache  # type: ignore[assignment]
    try:
        import jinja2.environment

        original_init = jinja2.environment.Environment.__init__

        def init(self: Any, *args: Any, **kwargs: Any) -> None:
            kwargs["cache_size"] = 0
            original_init(self, *args, **kwargs)

        jinja2.environment.Environment.__init__ = init  # type: ignore[method-assign]
    except ImportError:
        pass
    return recorder


RECORDER = _start()


def _fixture_key(fixturedef: Any, request: Any) -> str:
    scope = fixturedef.scope
    nodeid = request.node.nodeid
    if scope == "session":
        anchor = ""
    elif scope == "module":
        anchor = nodeid.split("::")[0]
    elif scope == "class":
        anchor = "::".join(nodeid.split("::")[:2])
    else:
        anchor = "*"
    return f"{scope}|{anchor}|{fixturedef.baseid}|{fixturedef.argname}"


def _ours(config: Any) -> Recorder | None:
    """The recorder, when `config` is the session it records. A test that runs
    pytest in-process (pytest.main on a copy of conftest) registers this
    module again; that inner session's tests run inside the outer test's
    window and count for it, but write no records of their own."""
    rec = RECORDER
    if rec is None or not rec.active or config is not rec.config:
        return None
    return rec


def pytest_configure(config: pytest.Config) -> None:
    rec = RECORDER
    if rec is not None and rec.config is None:
        rec.config = config


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Any:
    rec = _ours(collector.config)
    if rec is None or not isinstance(collector, pytest.Module):
        return (yield)
    prev = rec.enter()
    try:
        return (yield)
    finally:
        window = rec.leave(prev)
        try:
            rec.write({"c": collector.nodeid, **rec.dump(window)})
        except Exception as error:
            rec.fail("collect", error)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    rec = _ours(request.config)
    if rec is None or fixturedef.scope == "function":
        return (yield)
    prev = rec.enter()
    try:
        return (yield)
    finally:
        window = rec.leave(prev)
        try:
            rec.write({"fx": _fixture_key(fixturedef, request), **rec.dump(window)})
        except Exception as error:
            rec.fail("fixture", error)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Any) -> Any:
    rec = _ours(item.config)
    if rec is None:
        return (yield)
    prev = rec.enter()
    try:
        return (yield)
    finally:
        window = rec.leave(prev)
        try:
            scoped = []
            info = getattr(item, "_fixtureinfo", None)
            if info is not None:
                for name in getattr(item, "fixturenames", ()):
                    defs = info.name2fixturedefs.get(name) or ()
                    if defs and defs[-1].scope != "function":
                        scoped.append(f"{defs[-1].scope}|{defs[-1].baseid}|{defs[-1].argname}")
            rec.write({"t": item.nodeid, "x": sorted(scoped), **rec.dump(window)})
        except Exception as error:
            rec.fail("test", error)


def pytest_unconfigure(config: pytest.Config) -> None:
    rec = RECORDER
    if rec is None or config is not rec.config:
        return
    try:
        if rec.active:
            rec.active = False
            mon.set_events(TOOL_ID, 0)
            rec.write({"g": True, **rec.dump(rec.glob)})
            rec.write({"end": True})  # compact treats a file without it as cut short
        rec.out.close()
    except Exception:  # noqa: S110 - the session is ending
        pass
