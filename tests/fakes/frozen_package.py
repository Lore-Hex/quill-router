"""A separate, byte-pinned f83bbaac package; no fallback to live router code."""
from __future__ import annotations

import _thread
import builtins
import functools
import gc
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import logging
import sys
import tarfile
import tempfile
import threading
import time
import typing
from contextlib import contextmanager
from datetime import datetime as _Datetime
from datetime import time as _Time
from datetime import timezone as _Timezone
from pathlib import Path
from types import (
    AsyncGeneratorType,
    BuiltinFunctionType,
    CodeType,
    CoroutineType,
    FrameType,
    FunctionType,
    GeneratorType,
    MemberDescriptorType,
    ModuleType,
    TracebackType,
)

SNAPSHOT = Path(__file__).with_name('frozen_f83bbaac')
ARCHIVE_SHA256 = 'e68b785ca7d5d62d82e71be5df07c605aa3f2ef83a528769138de6d2eba8d4da'
ALIAS = 'frozen_f83bbaac'
PINS = json.loads((SNAPSHOT / 'pins.json').read_text())
_TEMP = tempfile.TemporaryDirectory(prefix='f83bbaac-')
ROOT = Path(_TEMP.name)
assert hashlib.sha256((SNAPSHOT / 'package.tar.gz').read_bytes()).hexdigest() == ARCHIVE_SHA256
with tarfile.open(SNAPSHOT / 'package.tar.gz') as archive:
    assert {m.name for m in archive if m.isfile()} == set(PINS)
    for member in archive:
        assert member.isfile() and not member.name.startswith('/') and '..' not in Path(member.name).parts
        data = archive.extractfile(member).read()
        assert hashlib.sha256(data).hexdigest() == PINS[member.name], member.name
        path = ROOT / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def frozen_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'trusted_router' or name.startswith('trusted_router.'):
        name = ALIAS + name[len('trusted_router'):]
    return builtins.__import__(name, globals, locals, fromlist, level)


FROZEN_BUILTINS = {**vars(builtins), '__import__': frozen_import}


class SnapshotLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != ALIAS and not fullname.startswith(ALIAS + '.'):
            return None
        relative = fullname.replace(ALIAS, 'src/trusted_router', 1).replace('.', '/')
        source = ROOT / (relative + '.py')
        package = ROOT / relative / '__init__.py'
        if package.is_file():
            source = package
        # Namespace packages exist in the original tree too.
        if not source.is_file():
            if (ROOT / relative).is_dir():
                spec = importlib.util.spec_from_loader(fullname, loader=None, is_package=True)
                spec.submodule_search_locations = [str(ROOT / relative)]
                return spec
            raise ImportError(f'Not present in frozen f83bbaac snapshot: {fullname}')
        return importlib.util.spec_from_file_location(
            fullname, source, loader=self,
            submodule_search_locations=[str(source.parent)] if source == package else None)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__dict__['__builtins__'] = FROZEN_BUILTINS
        source = Path(module.__file__)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == PINS[str(source.relative_to(ROOT))]
        exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)  # noqa: S102


sys.meta_path.insert(0, SnapshotLoader())


def module(name):
    return importlib.import_module(ALIAS + '.' + name)


def fake_store():
    """Keep the fake IO engine, but construct all real feature stores from the snapshot."""
    from tests.fakes.spanner import make_fake_store

    factory = FunctionType(make_fake_store.__code__,
                           {**make_fake_store.__globals__, '__builtins__': FROZEN_BUILTINS})
    factory.__kwdefaults__ = make_fake_store.__kwdefaults__
    return factory(request_record_write_mode='typed', operational_analytics_outbox_enabled=True,
                   generation_records_enabled=True, analytics_outbox_enabled=True)


_TYPE_DICT = type.__dict__['__dict__']
_TYPE_QUALNAME = type.__dict__['__qualname__']
_MODULE_DICT = ModuleType.__dict__['__dict__']


def _metadata(items, name):
    # Native item iterators and comparisons never call a dict subclass's get,
    # or a metadata key's overridden equality/hash methods.
    for key, value in items:
        if issubclass(type(key), str) and str.__eq__(key, name) is True:
            return value
    return ''


def _owner(value, owners=None):
    """Read provenance without invoking instance/metaclass properties."""
    value_type = type(value)
    if issubclass(value_type, ModuleType):
        owner = _metadata(dict.items(_MODULE_DICT.__get__(value)), '__name__')
    elif value_type is FunctionType or value_type is BuiltinFunctionType:
        owner = value.__module__  # These native function types cannot be subclassed.
    elif value_type is functools._lru_cache_wrapper:
        owner = _metadata(dict.items(vars(value)), '__module__')
    else:
        cls = value if issubclass(value_type, type) else value_type
        # Class labels are stable during this property-free audit; memoize only
        # within one traversal. Identity keys avoid custom metaclass hashing.
        entry = owners.get(id(cls)) if owners is not None else None
        if entry is None:
            owner = _metadata(_TYPE_DICT.__get__(cls).items(), '__module__')
            if owners is not None:
                owners[id(cls)] = (cls, owner)
        else:
            _, owner = entry
    if issubclass(type(owner), ModuleType):
        owner = _metadata(dict.items(_MODULE_DICT.__get__(owner)), '__name__')
    return owner if issubclass(type(owner), str) else ''


def _in_namespace(name, namespace):
    return str.__eq__(name, namespace) is True or str.startswith(name, namespace + '.')


def _live_source(filename):
    return (str.__contains__(filename, '/src/trusted_router/')
            and not str.startswith(filename, str(ROOT) + '/'))


MAX_REFERENCE_OBJECTS = 2_000_000
# CPython does not export FrameLocalsProxy from types on every 3.13+ release.
# Discover the sealed native type from a disposable, unstarted generator;
# never capture or enumerate the executing traversal's frame.
_FRAME_LOCALS_PROXY = (type((item for item in ()).gi_frame.f_locals)
                       if sys.version_info >= (3, 13) else None)
_CODE_MEMBERS = tuple(field for field in vars(CodeType).values()
                      if isinstance(field, MemberDescriptorType))


def _references(roots, *, namespaces=(), max_objects=MAX_REFERENCE_OBJECTS):
    """Walk every GC edge, with explicit process-registry boundaries for a leg.

    No container-kind dispatch: tp_traverse supplies the edges. CPython treats
    code, datetime, time and timezone as atomic despite held Python objects.
    Supplements follow native frame/traceback/exception/generator references,
    code members, tzinfo and timezone offset/name, without overridden properties.
    Frames come only from held roots, never an interpreter-stack enumeration.
    """
    pending, visited = list(roots), {}
    boundaries = {}
    if namespaces:
        # External module globals and the two process registries are outside the
        # leg. Explicit module roots override the module-global boundary.
        explicit = {id(root) for root in pending}
        boundaries = {id(vars(loaded)): vars(loaded)
                      for name, loaded in list(sys.modules.items())
                      if issubclass(type(loaded), ModuleType) and id(loaded) not in explicit
                      and not any(_in_namespace(name, ns) for ns in namespaces)}
        boundaries[id(sys.modules)] = sys.modules
        registry = logging.Logger.manager.loggerDict
        boundaries[id(registry)] = registry
        boundaries.update((id(logger), logger) for logger in [logging.root, *registry.values()]
                          if id(logger) not in explicit)
        for identity in explicit:
            boundaries.pop(identity, None)
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in visited or identity in boundaries:
            continue
        assert len(visited) < max_objects, (
            f'frozen reference graph exceeds {max_objects} objects')
        visited[identity] = value  # Strong references prevent visited-id reuse.
        yield value
        value_type = type(value)
        if value_type is FrameType:
            # Supplement only: GC still owns extra-locals dictionaries and
            # exec's supplied mapping, including keys and dict subclasses.
            localns = value.f_locals
            if type(localns) is _FRAME_LOCALS_PROXY:
                # Exact sealed native type: no user mapping protocol dispatch,
                # copying, or key lookup (which could call a key's __hash__).
                for key, held in localns.items():
                    pending.extend((key, held))
            pending.extend((value.f_globals, value.f_back, value.f_code, value.f_trace))
        # Always retain GC edges, including frames. Registry identity boundaries
        # above apply equally to GC and supplemental edges.
        pending.extend(gc.get_referents(value))
        if value_type is TracebackType:
            pending.extend((value.tb_frame, value.tb_next))
        elif value_type is GeneratorType:
            pending.append(value.gi_frame)
        elif value_type is CoroutineType:
            pending.append(value.cr_frame)
        elif value_type is AsyncGeneratorType:
            pending.append(value.ag_frame)
        elif issubclass(value_type, BaseException):
            # Exceptions can override properties; read the base C descriptors.
            pending.extend(BaseException.__dict__[name].__get__(value)
                           for name in ('__traceback__', '__context__', '__cause__'))
        if value_type is CodeType:
            pending.extend(field.__get__(value) for field in _CODE_MEMBERS)
        elif issubclass(value_type, _Datetime):
            pending.append(_Datetime.tzinfo.__get__(value))
        elif issubclass(value_type, _Time):
            pending.append(_Time.tzinfo.__get__(value))
        elif value_type is _Timezone:
            pending.append(_Timezone.utcoffset(value, None))
            pending.append(_Timezone.tzname(value, None))


def _namespace_roots(*namespaces):
    return [loaded for name, loaded in list(sys.modules.items())
            if any(_in_namespace(name, ns) for ns in namespaces)]


def reject_live_references(*harness):
    """Reject live definitions in frozen globals and explicitly supplied IO roots."""
    from tests.fakes import spanner

    roots = [*_namespace_roots(ALIAS), spanner, *harness]
    owners = {}
    for value in _references(roots, namespaces=(ALIAS, 'tests.fakes.spanner')):
        owner = _owner(value, owners)
        value_type = type(value)
        code = (value.__code__ if value_type is FunctionType
                else value if value_type is CodeType else None)
        if (_in_namespace(owner, 'trusted_router')
                or code is not None and _live_source(code.co_filename)):
            if code is not None:
                label = code.co_qualname
            elif value_type is functools._lru_cache_wrapper:
                label = _metadata(dict.items(vars(value)), '__qualname__')
            elif value_type is BuiltinFunctionType:
                label = value.__qualname__
            else:
                label = _TYPE_QUALNAME.__get__(value if issubclass(value_type, type) else value_type)
            if not issubclass(type(label), str):
                label = _TYPE_QUALNAME.__get__(value_type)
            raise AssertionError('live reference in frozen namespace: '
                                 + str.__str__(owner) + ':' + str.__str__(label))


def _is_functools_cache(value):
    # Recognize the sealed native wrapper without executing attribute lookups.
    # Its wrapped function and cache contents are ordinary GC referents.
    return type(value) is functools._lru_cache_wrapper


def clear_functools_caches(*harness, external_only=False):
    roots = [*_namespace_roots(ALIAS, 'trusted_router', 'tests.fakes.spanner'), *harness]
    # Materialize before clearing so nested caches in keys/results are included.
    caches = {id(value): value for value in _references(
        roots, namespaces=(ALIAS, 'trusted_router', 'tests.fakes.spanner'))
        if _is_functools_cache(value)}
    # Only this known process-wide registry needs normalization before audit:
    # typing retains schemas from the live comparison leg. Module labels alone
    # do not make a cache shared runtime state: purging an external callback's
    # cache here could discard its sole held live reference before inspection.
    # On 3.14 _tp_cache looks up caches in a global registry instead of holding
    # them in closures, so collect the registered caches explicitly as well.
    shared_runtime_caches = {id(cleanup.__self__): cleanup.__self__
                            for cleanup in typing._cleanups}
    caches.update(shared_runtime_caches)
    explicit = {id(root) for root in harness}
    for identity, cache in caches.items():
        if not external_only or (identity in shared_runtime_caches and identity not in explicit):
            functools._lru_cache_wrapper.cache_clear(cache)
    return list(caches.values())


def reject_existing_workers():
    """Refuse application workers that could outlive the guarded scope."""
    current = threading.get_ident()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    foreign = []
    for ident, frame in sys._current_frames().items():
        if ident == current:
            continue
        stack = []
        while frame:
            stack.append((frame.f_globals.get('__name__'), frame.f_code.co_name))
            frame = frame.f_back
        # xdist transport and pytest's timeout watchdog cannot execute the frozen
        # leg. Do not exempt executor/AnyIO workers or rely on thread names.
        if ('execnet.gateway_base', '_thread_receiver') in stack:
            continue
        thread = next((t for t in threading.enumerate() if t.ident == ident), None)
        target = getattr(thread, 'function', None)
        if isinstance(thread, threading.Timer) and _owner(target) == 'pytest_timeout':
            continue
        foreign.append((names.get(ident, str(ident)), stack))
    assert not foreign, ('pre-existing worker threads; create and join the frozen executor '
                         'inside execution_guard: ' + repr(foreign))


@contextmanager
def execution_guard(*harness):
    """Audit Python/C calls; refuse pre-existing workers and inspect harness roots.

    Record the first live call even if application exception handling swallows it.
    There are no production-module or omitted-definition exemptions. Module globals
    catch generated dataclass methods (<string>) as well as ordinary source code.
    """
    first = {}  # Thread id -> first live event; survives a raw worker exiting.
    seen = set()
    recorded = set()
    provenance = {}
    raw_starters = set()
    start_codes = set()
    def profile(frame, event, arg):
        if event == 'call':
            name = frame.f_globals.get('__name__', '')
            filename = frame.f_code.co_filename
        elif event == 'c_call':
            if id(arg) in raw_starters and frame.f_code not in start_codes:
                first.setdefault(threading.get_ident(), 'unwrapped raw worker creation')
                raise AssertionError('raw worker must use guarded thread bootstrap')
            name = getattr(arg, '__module__', '') or ''
            filename = ''
        else:
            return
        if not isinstance(name, str):
            name = getattr(name, '__name__', '')
        # Module label and code filename are both part of the key: changing
        # either is rechecked. This memoizes provenance, never callable results.
        key = (name, filename)
        flags = provenance.get(key)
        if flags is None:
            flags = (_in_namespace(name, 'trusted_router') or _live_source(filename),
                     _in_namespace(name, ALIAS))
            provenance[key] = flags
        live, frozen = flags
        # External runtime calls still undergo both provenance checks. Avoid
        # building inventory keys/qualnames for millions of irrelevant events.
        if not live and not frozen:
            return
        qualname = (frame.f_code.co_qualname if event == 'call'
                    else getattr(arg, '__qualname__', type(arg).__qualname__))
        # Dataclasses compile generic qualnames; preserve the owning class.
        if filename == '<string>' and 'self' in frame.f_locals:
            qualname = type(frame.f_locals['self']).__qualname__ + '.' + frame.f_code.co_name
        if live:
            first.setdefault(threading.get_ident(), f'{name}:{qualname}')
        identity = (name, qualname, frame.f_code.co_firstlineno)
        if frozen and identity not in recorded:
            recorded.add(identity)
            source = sys.modules[name].__file__
            relative = str(Path(source).relative_to(ROOT))
            seen.add((name.replace(ALIAS, 'trusted_router', 1), qualname, frame.f_code.co_firstlineno, relative, PINS[relative]))
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    all_threads = getattr(threading, 'setprofile_all_threads', None)
    starters = []
    active = set()
    started_threads = set()

    def wrap_start(original):
        def start(function, *args, **kwargs):
            token = object()
            active.add(token)  # Register before startup, including delayed bootstraps.
            def bootstrap(*worker_args, **worker_kwargs):
                started_threads.add(threading.get_ident())
                sys.setprofile(profile)
                try:
                    return function(*worker_args, **worker_kwargs)
                finally:
                    active.discard(token)
                    # Keep profiling through native thread teardown, including
                    # sys.unraisablehook after a raw callback raises. The thread
                    # state owns this hook until it exits; the main/default hooks
                    # are restored by the guard after joining all started ids.
            try:
                return original(bootstrap, *args, **kwargs)
            except BaseException:
                active.discard(token)
                raise
        start_codes.add(start.__code__)
        return start

    # Cover raw APIs as well as threading's cached aliases on 3.11 and 3.14.
    for namespace, names in ((_thread, ('start_new_thread', 'start_joinable_thread')),
                             (threading, ('_start_new_thread', '_start_joinable_thread'))):
        for name in names:
            if hasattr(namespace, name):
                original = getattr(namespace, name)
                starters.append((namespace, name, original))
                raw_starters.add(id(original))
                setattr(namespace, name, wrap_start(original))
    # 3.12+ covers every existing Python thread, including raw workers. The
    # 3.11 fallback refuses existing workers; new workers use the bootstraps.
    threading.setprofile(profile)
    if all_threads is not None:
        all_threads(profile)
    # all_threads also changes this thread. Reference preflight is test-harness
    # work, not the frozen leg; keep worker hooks active but avoid profiling the
    # GC walk itself. Install the main hook immediately before yielding below.
    sys.setprofile(previous)
    entered = False
    try:
        reject_existing_workers()
        # Shared runtime caches (notably typing.Annotated) retain schemas from
        # the preceding live leg. Purge them first, without exempting their
        # wrapped functions/captures from the reference scan. Preserve router
        # and harness cache state for inspection, then clear every collected
        # cache, including nested caches disconnected by the initial purge.
        caches = clear_functools_caches(*harness, external_only=True)
        reject_live_references(*harness)
        for cache in caches:
            functools._lru_cache_wrapper.cache_clear(cache)
        entered = True
        sys.setprofile(profile)
        yield seen
    finally:
        try:
            if entered:
                # AnyIO signals worker shutdown without joining the workers.
                # Finish the join inside our scope, with profiling still on.
                # Tokens also cover delayed starts; thread ids cover the tail
                # between bootstrap completion and native thread-state removal.
                deadline = time.monotonic() + 5
                while active or started_threads.intersection(sys._current_frames()):
                    assert time.monotonic() < deadline, (
                        'worker threads must finish inside execution_guard')
                    time.sleep(.001)
                reject_existing_workers()
        finally:
            if all_threads is not None:
                all_threads(previous_thread)
            threading.setprofile(previous_thread)
            sys.setprofile(previous)
            for namespace, name, original in reversed(starters):
                setattr(namespace, name, original)
        assert not first, 'live callable reached by frozen leg: ' + repr(first)
        if entered:
            reject_live_references(*harness)
