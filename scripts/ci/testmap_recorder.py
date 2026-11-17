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
     A call of one of the repository's functools.cache/lru_cache functions is
     marked "<key>@memo": a hit runs no Python code, so without the mark a
     test served from a cache would not record the function at all. The
     recorder tracks every cache from the moment functools.lru_cache makes
     it (functools.cache goes through lru_cache): the recorder replaces
     functools.lru_cache, before any application import, with one that
     returns the genuine cache and tells the recorder of it. A CALL event
     (disabled per call site per window) marks a call; every window (hook
     or miss) also marks each tracked cache whose hit count rose while it
     was open, which catches a hit through C (functools.partial,
     Context.run, map, sorted's key) and one at a call site that had
     already reported another callee. The caches themselves are untouched:
     the frozen-execution guard tests walk object graphs from them and
     recognize them by type.
     A cache made past functools.lru_cache (functools._lru_cache_wrapper
     called directly) is tracked from its first call event; if it had missed
     or hit before that, the misses went unrecorded ("mi": its users always
     run) and the hits unmarked ("um"). At the end of the session every
     cache still alive that was never tracked and has hits is "um" too. A
     map with any "um" makes the selector run everything (F2): nothing
     names the tests those hits served. None is expected; the record says
     which function to look at.
  o  repository files opened (the "open" audit event). The import system's
     own reads of .py/.pyc files are not reads: executing the module is
     what the f set records.
  d  repository directories listed (os.listdir, os.scandir, glob.glob,
     os.walk through scandir).
  s  commands started (subprocess.Popen, os.posix_spawn, os.exec*, os.system):
     the first four argv words and the working directory relative to the
     repository ("-" outside it). The map is a public artifact, so a word is
     cut at MAX_WORD characters, program text, secrets, option and
     environment values and URL credentials are placeholders (Recorder.words
     lists them), and a path outside the repository is "<outside>" (the
     executable keeps its name).
Separate windows, each written once and inherited by the tests that use it:
  c   a test module's collection (its import and parametrization);
  fx  the setup of a module, class, package or session fixture;
  cf  a memoized function's miss: from its PY_START (never disabled for it)
      to its PY_RETURN or PY_UNWIND (local and global events), events
      re-armed so every function the miss runs is recorded. Every miss is
      recorded; what one adds to the function's earlier misses is written.
      Past MAX_MISSES misses in one process the function is written as "mi"
      (incomplete) instead, and the selector runs its users whatever
      changed. A miss window is kept on the stack of the thread that missed
      (only that thread closes it), but what any thread runs while it is
      open counts for it: a location one thread reports is disabled for
      all, so another thread's open miss would not see it again, and the
      work may be one the miss handed to that thread. (A miss window that
      opens between another thread's report and its DISABLE taking effect
      is caught by a counter of opened windows, except in the one bytecode
      between the callback's last check and its return.)
      A function that keeps a cache by hand is windowed the same way, every
      call a "miss": one that assigns a module global (`global _CACHE`, a
      singleton built on first use, a "done once" flag; a reset to None or
      a `+= 1` counter does not count), stores into or mutates a private module global
      (_MONITOR_HASH[key] = ..., _VERIFIED.move_to_end(...), _ENSURED.add),
      or stores into, assigns or reads a global or attribute named like a
      cache (CACHE_NAME: _CACHE, _STATUS_RESPONSE_CACHE, self._cache,
      self._cached). A test that called it inherits what any of its calls
      ran, as from a memoized function's misses (selector S10). A generator
      or coroutine's window closes at each yield or await (PY_YIELD) and
      reopens when it resumes. Found by bytecode (keeps_cache) when the
      function first runs. In src that is about seventy functions:
      ai_iq._models_payload, the routes/public.py snapshots
      (_status_snapshot, _leaderboard_snapshot, _fleet_snapshot_cached,
      _cached_public_response, ...), measured, receipt_keys, sns_verify,
      company_affiliations, synthetic/funding.py's monitor_lookup_hash
      (_MONITOR_HASH) and ensure_monitor_funding (_ENSURED),
      async_settle_shadow_v2_gate_prior._resolve (_VERIFIED), the gateway's
      broadcast and fast path singletons, the snapshot publishers.
      routes/public.py's _STATIC_ASSET_BYTES_BY_ROOT is filled in
      _CachedStaticFiles.__init__, at import (the app is built then): the
      global window holds it, and a change there runs everything (F3).
      Not covered, and why:
        - a cache filled outside the windowed functions' calls: by their
          caller, or by another thread after they return. The tests that
          read it do not inherit the filler's work. Found in src:
          storage_gcp_authorize._outbox_table_available probes and fills
          through _cached_outbox_availability/_remember_outbox_availability
          (both windowed, the probe between them is not);
          EdgePolicyPublisher signs on a background thread
          (_sign_in_background) what current() serves; AdmissionRefresher's
          worker fills the cache requests read. The module-level caches
          listed above are filled where they are read.
        - work a coroutine hands off while it awaits (run_in_threadpool,
          asyncio.to_thread) to fill its cache: its window is closed during
          the await, so the cache's later readers do not inherit that work.
          Found in src: routes/bedrock_group_buy.py:273 (_public_snapshot
          awaits _read_public_snapshot_parts) and routes/public.py:2932
          (_cached_public_response awaits `build`). No current test reads a
          value one of them cached in another test: every create_app
          invalidates the bedrock snapshot, and every test builds its own
          app (conftest's client fixture, tests/test_bedrock_group_buy.py);
          the public response cache is bypassed when settings.environment
          is "test", and the tests that run it under "local"
          (tests/test_synthetic_monitoring.py, test_token_exchange_evidence,
          test_clickhouse_unreachable_degrades) empty it first.
        - functools.cached_property: a hit is an instance attribute lookup
          that runs nothing. None in src or scripts (one in
          tests/test_async_settle_proof_oracle.py, on an instance made per
          test, where the test that fills it is the one that reads it).
        - a cache held by a closure (nonlocal), in a public module global
          or an attribute not named like CACHE_NAME, and a library's cache
          of repository results.
        - shared state that is not a cache (the in-memory store, a module
          setting a test leaves behind): any per-test selection assumes
          tests do not depend on what an earlier test left.
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

import dis
import functools
import gc
import json
import os
import re
import sys
import threading
import types
import weakref
from collections.abc import Callable
from typing import Any

import pytest

RECORDER_VERSION = "5"  # scripts/ci/testmap.py holds the same value; its tests compare the two

TOOL_ID = 3
OUT_ENV = "TR_TESTMAP_OUT"
OWNER_ENV = "TR_TESTMAP_OWNER"
MEMO = "@memo"
# Every miss is recorded (a capped recording would leave the function's later
# misses unknown); past this many in one process the function is written as
# incomplete instead, a bound on time and output that the suite never reaches.
MAX_MISSES = 1_000_000
MAX_WORD = 200
# Words after which the next word is program text: python -c, bash -c,
# sh -ec, node -e, perl -pe, ruby -e (a short cluster ending in c or e).
PROGRAM_FLAG = re.compile(r"-[A-Za-z]{0,3}[ce]|--(?:eval|exec|execute|command|print)")
# Options whose next word is a secret: a header (curl -H), a user (-u), a
# password (-p, redis-cli -a, openssl -k), or any option named like one.
# Single letters match case-sensitively (python -P, pip -U are not these).
VALUE_CLUSTER = re.compile(r"-[A-Za-z]*[Hupak]")  # -p, -a; a cluster ending in one (-vp, -up)
LONG_VALUE_FLAG = re.compile(r"--header|--user|--pw|--?[\w-]*(?:pass|token|secret|key|auth|cred|cookie|bearer)[\w-]*",
                             re.IGNORECASE)
GLUED_VALUE = re.compile(r"-([A-Za-z]*?[upH]).+", re.DOTALL)  # -phunter2, -uroot, -vpX: glued to its flag
OPTION_VALUE = re.compile(r"(--?[A-Za-z0-9][\w.-]*)=.*", re.DOTALL)  # --password=x, -Dname=x
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)  # env NAME=value
# A URL's user and password run to its last "@" before any "?" or "#": a
# password may hold "@" or "/", a query may hold "@".
URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)(?:[^?#\s]*@)?([^/?#\s]*)([^?#\s]*)[?#]?.*", re.DOTALL)
CREDENTIALS = re.compile(r"[^\s/@:]+:[^\s/]*@(\S*)", re.DOTALL)  # user:password@host, no scheme
# A function that keeps a cache by hand (see the module docstring and
# keeps_cache): it assigns a module global, mutates a private one, or
# stores into, assigns or reads a global or attribute named like a cache
# (_CACHE, self._cache, _STATUS_RESPONSE_CACHE).
CACHE_NAME = re.compile(r"(?:^|_)cache[ds]?$", re.IGNORECASE)
CACHE_MUTATORS = frozenset({"setdefault", "update", "pop", "popitem", "clear", "move_to_end", "append",
                            "appendleft", "extend", "insert", "add", "discard", "remove", "__setitem__",
                            "__delitem__"})
NAME_LOADS = frozenset({"LOAD_GLOBAL", "LOAD_ATTR", "LOAD_DEREF", "LOAD_NAME", "LOAD_CLASSDEREF"})
GLOBAL_WRITES = (bytes([dis.opmap["STORE_GLOBAL"]]), bytes([dis.opmap["DELETE_GLOBAL"]]))
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


Miss = tuple[Any, Window, dict[int, int]]  # (code, window, cache hits when it opened)


def cached_code(cache: Any) -> Any:
    """The code of the function a functools cache calls: its __wrapped__
    (functools.update_wrapper sets it), else the function the cache holds."""
    wrapped = getattr(cache, "__wrapped__", None)
    if wrapped is None:
        wrapped = next((o for o in gc.get_referents(cache) if isinstance(o, (types.FunctionType, types.MethodType))),
                       None)
    return getattr(wrapped, "__code__", None)


def _start_of(instruction: dis.Instruction) -> tuple[int, int] | None:
    p = instruction.positions
    return None if p is None or p.lineno is None or p.col_offset is None else (p.lineno, p.col_offset)


def _private_global(ins: dis.Instruction) -> bool:
    name = ins.argval if isinstance(ins.argval, str) else ""
    return ins.opname == "LOAD_GLOBAL" and name.startswith("_") and not name.startswith("__")


def _reset_or_counter(instructions: list[dis.Instruction], index: int) -> bool:
    """Is the global store at `index` a reset to None, or `+= <constant>` /
    `-= <constant>` (a counter)? Neither hands a later caller a value an
    earlier call computed. A store some jump reaches is neither (`_v =
    compute() if FLAG else None`, `_c = build() or None`), and a constant
    flag is not exempt: `if _ready: return; _populate(); _ready = True`
    serves what _populate() did."""
    store = instructions[index]
    before = instructions[index - 1] if index else None
    if before is None or store.is_jump_target:
        return False
    if before.opname == "LOAD_CONST":
        return before.argval is None and not before.is_jump_target
    return (before.opname == "BINARY_OP" and before.argrepr in ("+=", "-=") and index >= 3
            and instructions[index - 2].opname == "LOAD_CONST"
            and instructions[index - 3].opname == "LOAD_GLOBAL"
            and instructions[index - 3].argval == store.argval
            and not any(ins.is_jump_target for ins in instructions[index - 2:index]))


def keeps_cache(code: Any) -> bool:
    """Does a function keep state across calls that a later call serves?
      - it assigns a module global, other than a reset to None or a counter;
      - it stores into, or calls a mutating method of, a private module
        global (_MONITOR_HASH[key] = ..., _VERIFIED.move_to_end(...)) or a
        global or attribute named like a cache (self._cache[key] = ...);
      - it assigns an attribute named like a cache (self._cached = ...);
      - it reads a private global or attribute named like a cache (_CACHE,
        self._cache; VERTEX_DURABLE_CACHE is a constant): a function that
        serves a cached value and calls a filler on a miss is the one its
        callers run every time. A function named like a cache that is only
        called is not one."""
    instructions: list[dis.Instruction] | None = None
    ops = code.co_code[::2]
    if any(op in ops for op in GLOBAL_WRITES):
        instructions = list(dis.get_instructions(code))
        if any(ins.opname in ("STORE_GLOBAL", "DELETE_GLOBAL") and not _reset_or_counter(instructions, index)
               for index, ins in enumerate(instructions)):
            return True
    if not any(CACHE_NAME.search(name) or name.startswith("_") and not name.startswith("__") for name in code.co_names):
        return False
    if instructions is None:
        instructions = list(dis.get_instructions(code))
    named: dict[tuple[int, int], int] = {}  # where a cache-named or private global load starts -> its index
    for index, ins in enumerate(instructions):
        name = ins.argval if isinstance(ins.argval, str) else ""
        if ins.opname in ("STORE_ATTR", "DELETE_ATTR") and CACHE_NAME.search(name):
            return True  # self._cached = ...
        if (ins.opname in NAME_LOADS and CACHE_NAME.search(name)) or _private_global(ins):
            where = _start_of(ins)
            if where is not None:
                named.setdefault(where, index)
            after = instructions[index + 1] if index + 1 < len(instructions) else None
            if after is not None and after.opname == "LOAD_ATTR" and after.argval in CACHE_MUTATORS:
                return True  # _CACHE.setdefault(...), self._cache.clear(), _ENSURED.add(...)
        if ins.opname in ("STORE_SUBSCR", "DELETE_SUBSCR"):
            where = _start_of(ins)
            first = named.get(where) if where is not None else None
            if first is not None and not any(other.opname.startswith("CALL") and _start_of(other) == where
                                             for other in instructions[first + 1:index]):
                return True  # _CACHE[key] = ..., del self._cache[key]; not _make()[key] = ...
    for where, index in named.items():
        name = instructions[index].argval
        if not CACHE_NAME.search(name) or not name.startswith("_"):
            continue  # reads count for private names like a cache only
        after = instructions[index + 1] if index + 1 < len(instructions) else None
        callee = any(other.opname.startswith("CALL") and _start_of(other) == where
                     for other in instructions[index + 1:])
        if not callee or (after is not None and after.opname == "LOAD_ATTR"):
            return True  # a read: a function named like a cache, called, is not one
    return False


class Recorder:
    """Hook windows (a test, a scoped fixture's setup, a module's collection)
    nest on one stack, entered and left by pytest's hooks under a lock. A
    miss window belongs to the thread whose call missed: each thread keeps
    its own stack of them, and only that thread closes them. What any thread
    runs counts for the hook window open at the time and for every open miss
    window, whichever thread opened it."""

    def __init__(self, out_dir: str) -> None:
        self.glob = Window()
        self.cur = self.glob
        self.stack: list[tuple[Window, dict[int, int]]] = []  # (outer window, cache hits at entry)
        self.hook_lock = threading.RLock()
        self.out_lock = threading.Lock()
        self.miss_lock = threading.Lock()
        self.local = threading.local()          # .misses: [Miss], innermost last
        self.miss_stacks: list[list[Miss]] = []
        self.open_misses: tuple[Window, ...] = ()  # every thread's open miss windows
        self.opened = 0                         # miss windows opened so far, every thread
        self.windowed: dict[int, Any] = {}      # memoized or hand-rolled cache functions: id -> code
        self.classified: dict[int, Any] = {}    # repository code objects classified: id -> code
        self.memo_caches: dict[int, tuple[Any, Any, int]] = {}  # id(cache) -> (weak cache, code, hits when seen)
        self.cf_seen: dict[str, dict[str, set[str]]] = {}  # what a function's misses ran, written so far
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
    def cache_hits(self) -> dict[int, int]:
        hits = {}
        for ident, (ref, _code, _seen) in list(self.memo_caches.items()):
            cache = ref()
            if cache is None:
                self.memo_caches.pop(ident, None)
            else:
                hits[ident] = cache.cache_info().hits
        return hits

    def mark_hits(self, window: Window, before: dict[int, int]) -> None:
        """A cache whose hit count rose while the window was open served it,
        whichever call site asked (a CALL event reports a site once per
        window, so a site that called something else first never reports)."""
        for ident, (ref, code, seen) in list(self.memo_caches.items()):
            cache = ref()
            if cache is not None and cache.cache_info().hits > before.get(ident, seen):
                window.memo[id(code)] = code

    def enter(self) -> None:
        with self.hook_lock:
            self.stack.append((self.cur, self.cache_hits()))
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
            prev, hits = self.stack.pop()
            self.mark_hits(window, hits)
            if prev is not self.glob:  # what ran in a nested window ran in the outer one too
                prev.absorb(window)
            self.cur = prev
            mon.restart_events()
            return window

    def thread_misses(self) -> list[Miss]:
        stack = getattr(self.local, "misses", None)
        if stack is None:
            stack = self.local.misses = []
            with self.miss_lock:
                self.miss_stacks.append(stack)
        return stack

    def note(self, attr: str, value: Any, ident: int | None = None) -> None:
        """Record into the hook window and every open miss window. A miss on
        another thread counts too: a location reported here is disabled for
        every thread, so the other thread's miss would not see it again, and
        what this thread runs may be work that miss handed it."""
        for window in (self.cur, *self.open_misses):
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
                opened = self.opened
                self.note("codes", code, ident)
                if ident in self.windowed or (self.classified.get(ident) is not code and self.classify(code)):
                    self.open_miss(code, ident)
                    return None  # a memoized function's start is a miss: count every one
                if self.opened != opened:
                    # A miss window opened on another thread since this
                    # callback read the windows: its restart may precede the
                    # DISABLE, so leave the location armed.
                    return None
        except Exception as error:
            self.fail("PY_START", error)
        return _disable

    def classify(self, code: Any) -> bool:
        """Does this repository function keep a cache by hand? Then it is
        windowed like a memoized one (see the module docstring)."""
        self.classified[id(code)] = code
        if (code.co_name == "<module>" or code.co_filename == __file__ or self.code_path(code.co_filename) is None
                or not keeps_cache(code)):
            return False
        self.windowed[id(code)] = code
        mon.set_local_events(TOOL_ID, code, mon.events.PY_RETURN | mon.events.PY_YIELD)
        return True

    def open_miss(self, code: Any, ident: int) -> None:
        count = self.misses[ident] = self.misses.get(ident, 0) + 1
        if count <= MAX_MISSES:
            window = Window()
            window.codes[ident] = code
            hits = self.cache_hits()
            stack = self.thread_misses()
            with self.miss_lock:
                stack.append((code, window, hits))
                self.open_misses = (*self.open_misses, window)
                self.opened += 1
            mon.restart_events()  # the miss records every function it runs
        elif count == MAX_MISSES + 1:
            key = self.key(code)
            if key is not None:
                self.write({"mi": key})  # later misses go unrecorded: its users always run

    def on_return(self, code: Any, offset: int, value: Any) -> Any:
        """PY_RETURN and PY_YIELD (local events of windowed functions), and
        PY_UNWIND (global): the frame of `code` ended or suspended."""
        try:
            stack = getattr(self.local, "misses", None)
            if stack:
                for index in range(len(stack) - 1, -1, -1):
                    if stack[index][0] is code:
                        while len(stack) > index:  # and any inner window left open
                            self.close_miss(stack)
                        break
        except Exception as error:
            self.fail("PY_RETURN", error)
        return None

    def on_unwind(self, code: Any, offset: int, exception: Any) -> Any:
        if self.open_misses:  # nothing open: the common case, at once
            self.on_return(code, offset, exception)
        return None

    def close_miss(self, stack: list[Miss]) -> None:
        """Write what this miss ran that the function's earlier misses did not."""
        try:
            with self.miss_lock:
                code, window, hits = stack.pop()
                self.open_misses = tuple(w for w in self.open_misses if w is not window)
            self.mark_hits(window, hits)
            key = self.key(code)
            if key is not None and self.active:
                seen = self.cf_seen.setdefault(key, {part: set() for part in ("f", "o", "d", "s")})
                new = {part: sorted(set(values) - seen[part]) for part, values in self.dump(window).items()}
                if any(new.values()):
                    for part, values in new.items():
                        seen[part].update(values)
                    self.write({"cf": key, **new})
        except Exception as error:
            self.fail("miss", error)

    def close_misses(self, every_thread: bool = False) -> None:
        """Close misses still open: this thread's before a hook window closes
        (a generator suspended by an exception, a frame that never returned),
        every thread's at the end of the session."""
        stacks = list(self.miss_stacks) if every_thread else [getattr(self.local, "misses", None) or []]
        for stack in stacks:
            while stack:
                self.close_miss(stack)

    def on_call(self, code: Any, offset: int, func: Any, arg0: Any,
                _disable: Any = sys.monitoring.DISABLE, _lru: Any = LRU, _method: Any = types.MethodType) -> Any:
        try:
            if self.active:
                opened = self.opened
                kind = type(func)
                if kind is _method:
                    func = func.__func__
                    kind = type(func)
                if kind is _lru:
                    entry = self.memo_caches.get(id(func))
                    if entry is not None and entry[0]() is func:
                        target = entry[1]
                    else:  # created before the recorder, or past functools.lru_cache: late
                        target = self.register(func, late=True)
                    if target is not None:
                        self.note("memo", target, id(target))
                if self.opened != opened:
                    return None  # as in on_code
        except Exception as error:
            self.fail("CALL", error)
        return _disable

    def register(self, func: Any, *, late: bool) -> Any:
        """Track one of the repository's functools caches: its misses open
        windows and a rise in its hit count marks the window open at the
        time. Returns the cached function's code, or None for a library's
        cache (not in the map). A cache tracked only after it was used
        (`late`) has misses no window recorded, so its users always run
        ("mi"); one that served hits before is "um": nothing names the tests
        it served, and the selector runs everything."""
        target = cached_code(func)
        if target is None or self.code_path(target.co_filename) is None:
            return None
        info = func.cache_info()
        self.memo_caches[id(func)] = (weakref.ref(func), target, info.hits)
        ident = id(target)
        if ident not in self.windowed:
            self.windowed[ident] = target
            mon.set_local_events(TOOL_ID, target, mon.events.PY_RETURN | mon.events.PY_YIELD)
            mon.restart_events()  # its start may be disabled already: every miss must report
        if late and (info.hits or info.misses):
            key = self.key(target)
            if info.misses:
                self.write({"mi": key})
            if info.hits:
                self.write({"um": key})
        return target

    def created(self, func: Any) -> None:
        """functools.lru_cache made a cache: track it before any call, so a
        miss or hit through C (functools.partial, Context.run, map, sorted's
        key) is recorded like any other."""
        if self.active:
            try:
                self.register(func, late=False)
            except Exception as error:
                self.fail("cache", error)

    def unregistered(self) -> None:
        """At the end of the session: a repository cache never tracked that
        served hits (made past functools.lru_cache and only called from C)
        served tests nothing names ("um")."""
        for obj in gc.get_objects():
            if type(obj) is LRU:
                entry = self.memo_caches.get(id(obj))
                if entry is not None and entry[0]() is obj:
                    continue
                target = cached_code(obj)
                if target is not None and self.code_path(target.co_filename) is not None and obj.cache_info().hits:
                    self.write({"um": self.key(target)})

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
        the repository is its name; other paths outside it are placeholders,
        and so are program text (after -c or -e), the word after a header,
        user or password option (-H, -u, -p, -a, -k, --password, --pw), a
        value glued to -u, -p or -H (-phunter2), an option's secret =value
        or inner NAME=value, an environment value, a URL's user, password,
        query and fragment, and user:password@ without a scheme. The word
        after such an option is hidden only if it is not an option itself
        (python -u -c: the -c still hides its program). Every word is cut
        at MAX_WORD characters. A path under the repository is the
        repository's even where rel() skips it (.git)."""
        if isinstance(argv, (str, bytes, os.PathLike)):
            raw = [os.fsdecode(argv)]
        else:
            raw = [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a) for a in list(argv)[:4]]
        out: list[str] = []
        hidden = ""
        for index, word in enumerate(raw):
            if hidden:
                held, hidden = hidden, ""
                if not word.startswith("-"):  # an option is never taken for the value: read it as one
                    out.append(held)
                    continue
            if index:
                word, hidden = self.option(word)
            elif ASSIGNMENT.fullmatch(word):
                word = word.split("=", 1)[0] + "=<value>"
            else:
                word = self.word(word, index)
            out.append(" ".join(word.split())[:MAX_WORD] or "''")
        return out

    def option(self, word: str) -> tuple[str, str]:
        """An argument as recorded, and the placeholder for the next word if
        it is program text or a secret."""
        program = "<program>" if PROGRAM_FLAG.fullmatch(word) else ""
        takes_next = bool(VALUE_CLUSTER.fullmatch(word) or LONG_VALUE_FLAG.fullmatch(word))
        glued = GLUED_VALUE.fullmatch(word)
        if glued:
            # -phunter2, -uroot. It may be a cluster whose last flag takes
            # the next word (-up x, openssl's -passin x), or perl's -pe: hide
            # that word too.
            return "-" + glued.group(1) + "<value>", program or ("<value>" if takes_next else "")
        if program:
            return word, program
        if takes_next:
            # A single-dash word holding a keyword may be a value glued to a
            # flag (-tTokenAbc): hide it too.
            hide = not word.startswith("--") and not VALUE_CLUSTER.fullmatch(word)
            return ("-<value>" if hide else word), "<value>"
        if OPTION_VALUE.fullmatch(word):
            name, value = word.split("=", 1)
            if VALUE_CLUSTER.fullmatch(name) or LONG_VALUE_FLAG.fullmatch(name):
                value = "<value>"
            elif ASSIGNMENT.fullmatch(value):  # --data=token=X
                value = value.split("=", 1)[0] + "=<value>"
            else:
                value = self.word(value, 1)
            return name + "=" + value, ""
        if ASSIGNMENT.fullmatch(word):
            return word.split("=", 1)[0] + "=<value>", ""
        return self.word(word, 1), ""

    def word(self, word: str, index: int) -> str:
        """A URL without its user, password, query and fragment; a path in
        the repository relative to it, as "./<path>" (a slash in it tells
        the selector it is a path: S8); outside it a placeholder (the
        executable keeps its name)."""
        url = URL.fullmatch(word)
        if url is not None:
            return url.group(1) + url.group(2) + url.group(3)
        credentials = CREDENTIALS.fullmatch(word)
        if credentials is not None:
            return "<value>@" + credentials.group(1)
        if os.path.isabs(word) or word.startswith("."):
            rel = self.rel(word)
            if rel is None and index:
                rel = self.under_root(word)  # <repo>/.git, <repo>/.venv: the repository's all the same
            if rel is not None:
                return rel if rel == "." else "./" + rel
            return os.path.basename(word) if index == 0 else "<outside>"
        return word

    def under_root(self, path: str) -> str | None:
        """A path under the repository that rel() leaves out (SKIP_PARTS)."""
        try:
            text = os.path.realpath(os.path.join(os.getcwd(), path)) + os.sep
        except OSError:
            return None
        return (text[len(ROOT):-1] or ".") if text.startswith(ROOT) else None

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
    events = mon.events
    try:
        mon.register_callback(TOOL_ID, events.PY_START, recorder.on_code)
        mon.register_callback(TOOL_ID, events.PY_RESUME, recorder.on_code)
        mon.register_callback(TOOL_ID, events.CALL, recorder.on_call)
        mon.register_callback(TOOL_ID, events.PY_RETURN, recorder.on_return)
        mon.register_callback(TOOL_ID, events.PY_YIELD, recorder.on_return)
        mon.register_callback(TOOL_ID, events.PY_UNWIND, recorder.on_unwind)
        mon.set_events(TOOL_ID, events.PY_START | events.PY_RESUME | events.CALL | events.PY_UNWIND)
        sys.addaudithook(_audit_hook(recorder))
        functools.lru_cache = _observed_lru_cache  # type: ignore[assignment]
        for obj in gc.get_objects():  # caches made before this module: none, unless an import moved
            if type(obj) is LRU:
                recorder.register(obj, late=True)
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


_ORIGINAL_LRU_CACHE = functools.lru_cache


def _created(cache: Any) -> Any:
    rec = RECORDER
    if rec is not None:
        rec.created(cache)
    return cache


@functools.wraps(_ORIGINAL_LRU_CACHE)
def _observed_lru_cache(maxsize: Any = 128, typed: bool = False) -> Any:
    """functools.lru_cache, telling the recorder of every cache it makes. The
    cache is the genuine one (the frozen-execution guard tests recognize
    caches by type); functools.cache goes through it too. It reaches the
    recorder through this module's globals, never a closure: a module that
    imported lru_cache holds this function, and the guard tests walk every
    reference from such modules except into other modules' globals."""
    made = _ORIGINAL_LRU_CACHE(maxsize, typed)
    if callable(maxsize) and not isinstance(maxsize, bool):  # @lru_cache, no arguments
        return _created(made)
    return functools.partial(_decorate, made)


def _decorate(made: Any, user_function: Any) -> Any:
    return _created(made(user_function))


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


def _close(where: str, record: Callable[[], dict]) -> None:
    """Leave the innermost window and write it under the key `record()`
    builds (inside the try: a failure to build it is the recorder's)."""
    rec = RECORDER
    if rec is None or not rec.active:
        return
    try:
        rec.close_misses()
        rec.write({**record(), **rec.dump(rec.leave())})
    except Exception as error:
        rec.fail(where, error)


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
        _close("c", lambda: {"c": collector.nodeid})


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    if fixturedef.scope == "function" or not _open(request.config):
        return (yield)
    try:
        return (yield)
    finally:
        _close("fx", lambda: {"fx": _fixture_key(fixturedef, request)})


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: Any) -> Any:
    if not _open(item.config):
        return (yield)
    try:
        return (yield)
    finally:
        _close("t", lambda: {"t": item.nodeid, "x": _scoped_fixtures(item)})


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
            try:
                rec.unregistered()
            except Exception as error:  # an "err" record: the shard's fragment is invalid
                rec.write({"err": f"unregistered caches: {type(error).__name__}: {error}"})
            rec.write({"g": True, **rec.dump(rec.glob)})
            rec.write({"end": True})  # compact treats a file without it as cut short
        rec.out.close()
    except Exception:  # noqa: S110 - the session is ending
        pass
