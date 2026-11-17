"""Record what each test runs and reads: the per-test footprint map's source.

Opt-in. Nothing happens unless TR_TESTMAP_OUT names an output directory, and
tests/conftest.py imports this module only then, before any application
import (an import-time read would otherwise go unrecorded). Only the "Test
map" workflow (.github/workflows/testmap.yml) sets it: no ci.yml job, so no
deploy gate, ever runs a test under the recorder. scripts/ci/testmap.py
compacts the output and holds the selection rules.

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
     the first four argv words and the working directory relative to the
     repository ("-" outside it). The map is a public artifact, so a word is
     cut at MAX_WORD characters, program text after -c and environment values
     are placeholders, and a path outside the repository is "<outside>" (the
     executable keeps its name).
Separate windows, each written once and inherited by the tests that use it:
  c   a test module's collection (its import and parametrization);
  fx  the setup of a module, class, package or session fixture;
  cf  a memoized function's miss: from its PY_START to its PY_RETURN (a local
      event set on the function once its first call is seen), events re-armed
      so every function the miss runs is recorded; the first MAX_MISSES misses
      per function, on the thread that missed. A function that misses more
      often is written as "mi" (incomplete): the selector runs its users
      whatever changed. A miss that raises closes with the window around it.
  g   everything outside every window: imports, session and collection hooks
      (a miss at import time counts here too).
Jinja serves a template from its cache after the first load, without
reading the file: every template load, cached or not, records the
template's file as opened. (Turning the cache off instead recompiled every
render and made tests/test_sitemap_seo_hygiene.py four times slower.)

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

The recorder never raises into a test: every callback, hook and audit
handler catches everything (an exception escaping a PY_RETURN callback would
propagate into the function's caller); an internal error writes an "err"
record, stops recording (turning off tool 3's events only if the tool is
ours), and makes the shard's fragment invalid, so no map is built from it.
Hook windows are entered and left under a lock by pytest's hooks; miss
windows are per thread.

Processes: the process that first loads the recorder owns the output (it
sets TR_TESTMAP_OWNER); its xdist workers (direct children) record too. A
process a test starts (a nested pytest, a subprocess importing the app) sees
an owner that is not its parent and stays off.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import threading
import types
import weakref
from typing import Any

import pytest

try:
    from scripts.ci.testmap import RECORDER_VERSION
except ImportError:  # pragma: no cover - loaded by path in a synthetic project
    RECORDER_VERSION = "2"

TOOL_ID = 3
OUT_ENV = "TR_TESTMAP_OUT"
OWNER_ENV = "TR_TESTMAP_OWNER"
MEMO = "@memo"
MAX_MISSES = 20
MAX_WORD = 200
PROGRAM_FLAG = re.compile(r"-[A-Za-z]*c")          # python -c, bash -c, sh -ec: program text follows
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)  # env NAME=value
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

    def absorb(self, other: Window) -> None:
        self.codes.update(other.codes)
        self.memo.update(other.memo)
        self.opens |= other.opens
        self.dirs |= other.dirs
        self.spawns |= other.spawns


class Recorder:
    """Hook windows (a test, a scoped fixture's setup, a module's collection)
    nest on one stack, entered and left by pytest's hooks under a lock. A
    miss window belongs to the thread whose call missed: each thread keeps
    its own stack of them, so a callback on another thread can neither pop
    nor mix into it. What any thread runs counts for the hook window open
    at the time, and for the open misses of its own thread."""

    def __init__(self, out_dir: str) -> None:
        self.glob = Window()
        self.cur = self.glob
        self.stack: list[Window] = []
        self.hook_lock = threading.RLock()
        self.out_lock = threading.Lock()
        self.local = threading.local()          # .misses: [(code, Window)], innermost last
        self.miss_stacks: list[list[tuple[Any, Window]]] = []
        self.memo_codes: dict[int, Any] = {}    # memoized functions seen called: id -> code
        self.misses: dict[int, int] = {}
        self.active = True
        self.tool_ours = False
        self.paths: dict[str, str | None] = {}
        self.config_ref: Any = None  # the session this process records, weakly; see pytest_configure
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        os.makedirs(out_dir, exist_ok=True)
        self.out = open(os.path.join(out_dir, f"{worker}-{os.getpid()}.jsonl"), "w", encoding="utf-8")  # noqa: SIM115
        self.write({"h": {"recorder": RECORDER_VERSION, "pid": os.getpid(), "worker": worker,
                          "python": sys.version.split()[0]}})

    # -- output
    def write(self, record: dict) -> None:
        with self.out_lock:
            self.out.write(json.dumps(record, separators=(",", ":")) + "\n")

    def fail(self, where: str, error: BaseException) -> None:
        """Stop recording and mark this process's output invalid. Never raises."""
        if not self.active:
            return
        self.active = False
        try:
            if self.tool_ours:
                mon.set_events(TOOL_ID, 0)
        except Exception:  # noqa: S110 - already failing; the tests keep running
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

    def key(self, code: Any) -> str | None:
        path = self.code_path(code.co_filename)
        return None if path is None else f"{path}:{code.co_qualname}"

    def dump(self, window: Window) -> dict:
        funcs = set()
        for codes, suffix in ((window.codes, ""), (window.memo, MEMO)):
            for code in codes.copy().values():
                key = self.key(code)
                if key is not None:
                    funcs.add(key + suffix)
        return {"f": sorted(funcs), "o": sorted(window.opens.copy()), "d": sorted(window.dirs.copy()),
                "s": sorted(window.spawns.copy())}

    # -- hook windows: the recorder keeps the stack, so no hook frame holds one
    def enter(self) -> None:
        with self.hook_lock:
            self.stack.append(self.cur)
            self.cur = Window()
            # Re-arm every location: what ran before this window (an earlier
            # test, or the import of the module now being collected) must
            # report again when it runs in this one.
            mon.restart_events()

    def leave(self) -> Window:
        with self.hook_lock:
            window = self.cur
            if not self.stack:
                raise RuntimeError("a window closed that was not open")
            prev = self.stack.pop()
            if prev is not self.glob:  # what ran in a nested window ran in the outer one too
                prev.absorb(window)
            self.cur = prev
            mon.restart_events()
            return window

    def thread_misses(self) -> list[tuple[Any, Window]]:
        stack = getattr(self.local, "misses", None)
        if stack is None:
            stack = self.local.misses = []
            with self.out_lock:
                self.miss_stacks.append(stack)
        return stack

    def note(self, attr: str, value: Any, ident: int | None = None) -> None:
        """Record into the hook window and the open misses of this thread."""
        targets = [self.cur, *(window for _, window in getattr(self.local, "misses", ()) or ())]
        for window in targets:
            if ident is None:
                getattr(window, attr).add(value)
            else:
                getattr(window, attr)[ident] = value

    # -- sys.monitoring callbacks: each one catches everything (an exception
    # escaping a PY_RETURN callback propagates into the function's caller)
    def on_code(self, code: Any, offset: int, _disable: Any = sys.monitoring.DISABLE) -> Any:
        # DISABLE is bound at definition: module globals are cleared at
        # interpreter shutdown, while a callback can still fire.
        try:
            if self.active:
                ident = id(code)
                self.note("codes", code, ident)
                if ident in self.memo_codes:
                    self.open_miss(code, ident)
                    return None  # a memoized function's start is a miss: count every one
        except Exception as error:
            self.fail("PY_START", error)
        return _disable

    def open_miss(self, code: Any, ident: int) -> None:
        count = self.misses[ident] = self.misses.get(ident, 0) + 1
        if count <= MAX_MISSES:
            window = Window()
            window.codes[ident] = code
            self.thread_misses().append((code, window))
            mon.restart_events()  # the miss records every function it runs
        elif count == MAX_MISSES + 1:
            key = self.key(code)
            if key is not None:
                self.write({"mi": key})  # later misses go unrecorded: its users always run

    def on_return(self, code: Any, offset: int, value: Any) -> Any:
        try:
            stack = getattr(self.local, "misses", None)
            if stack and stack[-1][0] is code:
                self.close_miss(stack)
        except Exception as error:
            self.fail("PY_RETURN", error)
        return None

    def close_miss(self, stack: list[tuple[Any, Window]]) -> None:
        try:
            code, window = stack.pop()
            key = self.key(code)
            if key is not None and self.active:
                self.write({"cf": key, **self.dump(window)})
        except Exception as error:
            self.fail("miss", error)

    def close_misses(self, every_thread: bool = False) -> None:
        """Close misses still open (one raised): this thread's before a hook
        window closes, every thread's at the end of the session."""
        stacks = list(self.miss_stacks) if every_thread else [getattr(self.local, "misses", None) or []]
        for stack in stacks:
            while stack:
                self.close_miss(stack)

    def on_call(self, code: Any, offset: int, func: Any, arg0: Any,
                _disable: Any = sys.monitoring.DISABLE, _lru: Any = LRU, _method: Any = types.MethodType) -> Any:
        try:
            if self.active:
                kind = type(func)
                if kind is _method:
                    func = func.__func__
                    kind = type(func)
                if kind is _lru:
                    target = getattr(getattr(func, "__wrapped__", None), "__code__", None)
                    # Only the repository's caches: a library's is not in the map.
                    if target is not None and self.code_path(target.co_filename) is not None:
                        ident = id(target)
                        self.note("memo", target, ident)
                        if ident not in self.memo_codes:
                            self.memo_codes[ident] = target
                            mon.set_local_events(TOOL_ID, target, mon.events.PY_RETURN)
                            mon.restart_events()  # its start may be disabled already: every miss must report
        except Exception as error:
            self.fail("CALL", error)
        return _disable

    def rel(self, path: Any) -> str | None:
        try:
            text = os.fsdecode(path)
        except TypeError:
            return None
        if not os.path.isabs(text):
            try:
                text = os.path.join(os.getcwd(), text)
            except OSError:  # a test removed the directory it chdir'd into:
                return None  # nothing relative can be opened there
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
                self.note("opens", rel)
            elif event in ("os.listdir", "os.scandir", "glob.glob"):
                if args and args[0] is not None and not isinstance(args[0], int):
                    rel = self.rel(args[0])
                    if rel is not None:
                        self.note("dirs", rel)
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

    def words(self, argv: Any) -> list[str]:
        """A command's first words, as little as identifies it: the map is a
        public artifact. Repository paths are relative; the executable outside
        the repository is its name; other paths outside it, program text
        after -c, and environment values are placeholders; every word is cut
        at MAX_WORD characters."""
        if isinstance(argv, (str, bytes, os.PathLike)):
            raw = [os.fsdecode(argv)]
        else:
            raw = [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a) for a in list(argv)[:4]]
        out: list[str] = []
        program = False
        for index, word in enumerate(raw):
            if program:
                out.append("<program>")
                program = False
                continue
            if PROGRAM_FLAG.fullmatch(word):
                program = True
            elif ASSIGNMENT.fullmatch(word):
                word = word.split("=", 1)[0] + "=<value>"
            elif os.path.isabs(word) or word.startswith("."):
                rel = self.rel(word)
                word = rel if rel is not None else (os.path.basename(word) if index == 0 else "<outside>")
            out.append(" ".join(word.split())[:MAX_WORD] or "''")
        return out

    def spawn(self, argv: Any, cwd: Any) -> None:
        if cwd is None:
            try:
                cwd = os.getcwd()
            except OSError:
                cwd = None
        where = self.rel(cwd) if cwd is not None else None
        self.note("spawns", " ".join(self.words(argv)) + f" @cwd={where or '-'}")


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
    recorder.tool_ours = True
    try:
        mon.register_callback(TOOL_ID, mon.events.PY_START, recorder.on_code)
        mon.register_callback(TOOL_ID, mon.events.PY_RESUME, recorder.on_code)
        mon.register_callback(TOOL_ID, mon.events.CALL, recorder.on_call)
        mon.register_callback(TOOL_ID, mon.events.PY_RETURN, recorder.on_return)
        mon.set_events(TOOL_ID, mon.events.PY_START | mon.events.PY_RESUME | mon.events.CALL)
        sys.addaudithook(_audit_hook(recorder))
    except Exception as error:
        recorder.fail("setup", error)
        return recorder
    try:
        import jinja2.environment

        original_load = jinja2.environment.Environment._load_template

        def load_template(self: Any, name: Any, globals: Any) -> Any:
            template = original_load(self, name, globals)
            _template_read(getattr(template, "filename", None))
            return template

        jinja2.environment.Environment._load_template = load_template  # type: ignore[method-assign]
    except (ImportError, AttributeError):
        pass
    return recorder


def _template_read(filename: Any) -> None:
    rec = RECORDER
    if rec is None or not rec.active or not filename:
        return
    try:
        rel = rec.rel(filename)
        if rel is not None:
            rec.note("opens", rel)
    except Exception as error:
        rec.fail("template", error)


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


def _open(config: Any) -> bool:
    """Enter a hook window for `config`'s session; False when not recording."""
    rec = _ours(config)
    if rec is None:
        return False
    try:
        rec.enter()
        return True
    except Exception as error:
        rec.fail("enter", error)
        return False


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
    if not isinstance(collector, pytest.Module) or not _open(collector.config):
        return (yield)
    try:
        return (yield)
    finally:
        _close({"c": collector.nodeid})


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    if fixturedef.scope == "function" or not _open(request.config):
        return (yield)
    try:
        return (yield)
    finally:
        _close({"fx": _fixture_key(fixturedef, request)})


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Any) -> Any:
    if not _open(item.config):
        return (yield)
    try:
        return (yield)
    finally:
        _close({"t": item.nodeid, "x": _scoped_fixtures(item)})


def _scoped_fixtures(item: pytest.Item) -> list[str]:
    try:
        scoped = []
        info = getattr(item, "_fixtureinfo", None)
        if info is not None:
            for name in getattr(item, "fixturenames", ()):
                defs = info.name2fixturedefs.get(name) or ()
                if defs and defs[-1].scope != "function":
                    scoped.append(f"{defs[-1].scope}|{defs[-1].baseid}|{defs[-1].argname}")
        return sorted(scoped)
    except Exception as error:
        if RECORDER is not None:
            RECORDER.fail("fixtures", error)
        return []


def pytest_unconfigure(config: pytest.Config) -> None:
    rec = RECORDER
    if rec is None or rec.config_ref is None or rec.config_ref() is not config:
        return
    try:
        if rec.active:
            rec.close_misses(every_thread=True)
            rec.active = False
            if rec.tool_ours:
                mon.set_events(TOOL_ID, 0)
            rec.write({"g": True, **rec.dump(rec.glob)})
            rec.write({"end": True})  # compact treats a file without it as cut short
        rec.out.close()
    except Exception:  # noqa: S110 - the session is ending
        pass
