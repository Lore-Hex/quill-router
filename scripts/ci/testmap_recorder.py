"""Record what each test runs and reads: the per-test footprint map's source.

Opt-in. Nothing happens unless TR_TESTMAP_OUT names an output directory, and
tests/conftest.py imports this module only then, before any application
import (an import-time read would otherwise go unrecorded). ci.yml sets it on
pushes to main (and on a dispatch asking for test_map) only; pull request
shards and local runs never load it. scripts/ci/testmap.py compacts the
output and holds the selection rules.

What a test's window (setup, call and teardown) records:
  f  every function that starts or resumes under the repository: a
     sys.monitoring PY_START/PY_RESUME callback (tool id 3; the suite's frozen
     execution guard takes 4) that returns DISABLE after the first hit, so a
     location costs one callback per window; restart_events() re-arms every
     location when a window opens or closes. Resume events catch a generator
     or coroutine started in an earlier test.
     A call of a functools.cache/lru_cache function is marked "<key>@memo"
     (a CALL event, also disabled per call site per window): a hit runs no
     Python code, so without the mark a test served from a cache would not
     record the function at all. The caches themselves are untouched: the
     frozen-execution guard tests walk object graphs from them and recognize
     them by type.
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
  cf  a memoized function's miss: from its PY_START to its PY_RETURN (a local
      event set on the function once its first call is seen), events re-armed
      so every function the miss runs is recorded; the first 20 misses per
      function. A test that marked the function inherits it. A miss that
      raises closes with the window around it.
  g   everything outside every window: imports, session and collection hooks
      (a miss at import time counts here too).
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

Nothing reachable from a test holds the recorder: hook frames keep no
windows (the recorder keeps its own stack), and the session is held weakly.
Tests that walk object graphs from their frames stay within their bounds.

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
import types
import weakref
from typing import Any

import pytest

try:
    from scripts.ci.testmap import RECORDER_VERSION
except ImportError:  # pragma: no cover - loaded by path in a synthetic project
    RECORDER_VERSION = "1"

TOOL_ID = 3
OUT_ENV = "TR_TESTMAP_OUT"
OWNER_ENV = "TR_TESTMAP_OWNER"
MEMO = "@memo"
MAX_MISSES = 20
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
AUDITED = frozenset({"open", "os.listdir", "os.scandir", "glob.glob", "subprocess.Popen", "os.posix_spawn",
                     "os.exec", "os.system"})
LRU = functools._lru_cache_wrapper  # type: ignore[attr-defined]
mon = sys.monitoring


class Window:
    """Code objects are kept by identity (id -> code): adding one to a set
    would hash it, and hashing a code object hashes its constants, which a
    test can observe (test_guard_worker_admission_does_not_hash_code_constants)."""
    __slots__ = ("codes", "memo", "opens", "dirs", "spawns")

    def __init__(self) -> None:
        self.codes: dict[int, Any] = {}
        self.memo: dict[int, Any] = {}
        self.opens: set[str] = set()
        self.dirs: set[str] = set()
        self.spawns: set[str] = set()


class Recorder:
    def __init__(self, out_dir: str) -> None:
        self.glob = Window()
        self.cur = self.glob
        self.stack: list[tuple[Window, bool]] = []  # (outer window, merge into it even if global)
        self.memo_codes: dict[int, Any] = {}   # memoized functions seen called: id -> code
        self.memo_open: list[Any] = []         # codes of the misses now recording, innermost last
        self.misses: dict[int, int] = {}
        self.active = True
        self.lock = threading.Lock()
        self.paths: dict[str, str | None] = {}
        self.config_ref: Any = None  # the session this process records, weakly; see pytest_configure
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
        for codes, suffix in ((window.codes, ""), (window.memo, MEMO)):
            for code in list(codes.values()):
                path = self.code_path(code.co_filename)
                if path is not None:
                    funcs.add(f"{path}:{code.co_qualname}{suffix}")
        return {"f": sorted(funcs), "o": sorted(window.opens), "d": sorted(window.dirs), "s": sorted(window.spawns)}

    # -- windows: the recorder keeps the stack, so no hook frame holds one
    def enter(self, into_global: bool = False) -> None:
        self.stack.append((self.cur, into_global))
        self.cur = Window()
        mon.restart_events()

    def leave(self) -> Window:
        window = self.cur
        prev, into_global = self.stack.pop()
        # What ran in a nested window ran in the outer one too; a test's own
        # window is not part of the global one, a miss at import time is.
        if prev is not self.glob or into_global:
            prev.codes.update(window.codes)
            prev.memo.update(window.memo)
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
        ident = id(code)
        self.cur.codes[ident] = code
        if ident in self.memo_codes and self.misses.get(ident, 0) < MAX_MISSES:
            self.misses[ident] = self.misses.get(ident, 0) + 1
            self.enter(into_global=True)
            self.cur.codes[ident] = code
            self.memo_open.append(code)
        return _disable

    def on_return(self, code: Any, offset: int, value: Any) -> Any:
        if self.memo_open and self.memo_open[-1] is code:
            self.close_miss()
        return None

    def close_miss(self) -> None:
        code = self.memo_open.pop()
        window = self.leave()
        path = self.code_path(code.co_filename)
        if path is not None:
            self.write({"cf": f"{path}:{code.co_qualname}", **self.dump(window)})

    def close_misses(self) -> None:
        """Close the misses still recording (one raised) before a hook's window closes."""
        while self.memo_open:
            self.close_miss()

    def on_call(self, code: Any, offset: int, func: Any, arg0: Any,
                _disable: Any = sys.monitoring.DISABLE, _lru: Any = LRU, _method: Any = types.MethodType) -> Any:
        kind = type(func)
        if kind is _method:
            func = func.__func__
            kind = type(func)
        if kind is _lru:
            wrapped = getattr(func, "__wrapped__", None)
            target = getattr(wrapped, "__code__", None)
            if target is not None:
                ident = id(target)
                self.cur.memo[ident] = target
                if ident not in self.memo_codes:
                    self.memo_codes[ident] = target
                    mon.set_local_events(TOOL_ID, target, mon.events.PY_RETURN)
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
                if rel.endswith(".py") and self.not_a_read():
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

    def not_a_read(self) -> bool:
        """Is the import system, assertion rewriting, Hypothesis's constant
        harvest or a warning/traceback display the one opening this file?"""
        frame = sys._getframe(2)
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


RECORDER: Recorder | None = None


def _audit_hook(recorder: Recorder) -> Any:
    # The hook runs for every audit event in the process (gc.get_referents and
    # sys._getframe among them, millions in the guard tests): reject the
    # events it does not record before any attribute lookup.
    events = AUDITED
    handle = recorder.audit

    def hook(event: str, args: tuple) -> None:
        if event in events:
            handle(event, args)

    return hook


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
    mon.register_callback(TOOL_ID, mon.events.CALL, recorder.on_call)
    mon.register_callback(TOOL_ID, mon.events.PY_RETURN, recorder.on_return)
    mon.set_events(TOOL_ID, mon.events.PY_START | mon.events.PY_RESUME | mon.events.CALL)
    sys.addaudithook(_audit_hook(recorder))
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


def _ours(config: Any) -> Recorder | None:
    """The recorder, when `config` is the session it records. A test that runs
    pytest in-process (pytest.main on a copy of conftest) registers this
    module again; that inner session's tests run inside the outer test's
    window and count for it, but write no records of their own."""
    rec = RECORDER
    if rec is None or not rec.active or rec.config_ref is None or rec.config_ref() is not config:
        return None
    return rec


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


def _close(record: dict) -> None:
    """Leave the innermost window and write it under `record`'s key."""
    rec = RECORDER
    if rec is None or not rec.active:
        return
    try:
        rec.close_misses()
        rec.write({**record, **rec.dump(rec.leave())})
    except Exception as error:
        rec.fail(next(iter(record)), error)


def pytest_configure(config: pytest.Config) -> None:
    rec = RECORDER
    if rec is not None and rec.config_ref is None:
        rec.config_ref = weakref.ref(config)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Any:
    if not isinstance(collector, pytest.Module) or _ours(collector.config) is None:
        return (yield)
    RECORDER.enter()  # type: ignore[union-attr]
    try:
        return (yield)
    finally:
        _close({"c": collector.nodeid})


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    if fixturedef.scope == "function" or _ours(request.config) is None:
        return (yield)
    RECORDER.enter()  # type: ignore[union-attr]
    try:
        return (yield)
    finally:
        _close({"fx": _fixture_key(fixturedef, request)})


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Any) -> Any:
    if _ours(item.config) is None:
        return (yield)
    RECORDER.enter()  # type: ignore[union-attr]
    try:
        return (yield)
    finally:
        scoped = []
        info = getattr(item, "_fixtureinfo", None)
        if info is not None:
            for name in getattr(item, "fixturenames", ()):
                defs = info.name2fixturedefs.get(name) or ()
                if defs and defs[-1].scope != "function":
                    scoped.append(f"{defs[-1].scope}|{defs[-1].baseid}|{defs[-1].argname}")
        _close({"t": item.nodeid, "x": sorted(scoped)})


def pytest_unconfigure(config: pytest.Config) -> None:
    rec = RECORDER
    if rec is None or rec.config_ref is None or rec.config_ref() is not config:
        return
    try:
        if rec.active:
            rec.close_misses()
            rec.active = False
            mon.set_events(TOOL_ID, 0)
            rec.write({"g": True, **rec.dump(rec.glob)})
            rec.write({"end": True})  # compact treats a file without it as cut short
        rec.out.close()
    except Exception:  # noqa: S110 - the session is ending
        pass
