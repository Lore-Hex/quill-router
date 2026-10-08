"""A separate, byte-pinned frozen-main package; no fallback to live router code."""
from __future__ import annotations

import _abc
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
import weakref
from collections.abc import Sized as _Sized
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
    MethodType,
    ModuleType,
    SimpleNamespace,
    TracebackType,
)

SNAPSHOT = Path(__file__).with_name('frozen_main')
ARCHIVE_SHA256 = '8ee042a19b878760da7f024c8805fc7d6f4add1bde7b8d4fd625ce5790357d93'
ALIAS = 'frozen_main'
PINS = json.loads((SNAPSHOT / 'pins.json').read_text())
_TEMP = tempfile.TemporaryDirectory(prefix='frozen-main-')
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
            raise ImportError(f'Not present in frozen-main snapshot: {fullname}')
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
        _register_generated(module)


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
_ABC_DATA = type(_TYPE_DICT.__get__(_Sized)['_abc_impl'])


# Code identity, never instance locals, attributes generated dataclass methods.
# Register after module execution and again at each guard entry for live modules.
_GENERATED_NAMES = {}
_UNSUPPORTED_INTERPRETER = (
    'unsupported interpreter: frozen execution_guard requires CPython 3.12+; '
    'CPython 3.11 profile/trace trampolines materialize unsafe frame locals')


def _require_execution_support():
    assert sys.implementation.name == 'cpython' and sys.version_info >= (3, 12), (
        _UNSUPPORTED_INTERPRETER)


def _register_generated(loaded):
    namespace = _MODULE_DICT.__get__(loaded)
    module_name = _metadata(dict.items(namespace), '__name__')
    pending = list(dict.values(namespace))
    visited = set()
    while pending:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        if issubclass(type(value), type):
            # Only this module's classes; do not enter imported dependency graphs.
            members = _TYPE_DICT.__get__(value)
            owner = _metadata(members.items(), '__module__')
            if not issubclass(type(owner), str) or str.__eq__(owner, module_name) is not True:
                continue
            pending.extend(members.values())
        elif type(value) is FunctionType:
            # dataclasses' recursive-repr wrapper holds the generated body in
            # a closure; attribute that body to the wrapper's native qualname.
            functions, found = [value], set()
            while functions:
                function = functions.pop()
                if id(function) in found:
                    continue
                found.add(id(function))
                if str.__eq__(function.__code__.co_filename, '<string>') is True:
                    _GENERATED_NAMES[id(function.__code__)] = (function.__code__, str.__str__(value.__qualname__))
                if function.__closure__:
                    for cell in function.__closure__:
                        functions.extend(held for held in gc.get_referents(cell)
                                         if type(held) is FunctionType)


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
    # Exact immutable builtins cannot carry a user-supplied provenance label.
    # Subclasses still take the descriptor-based path below.
    if (value_type is str or value_type is int or value_type is float
            or value_type is bytes or value_type is bool or value_type is type(None)):
        return 'builtins'
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
# Capture native identities before execution_guard replaces module attributes.
# Aliases can denote the same object; keep strong references for identity checks.
_THREAD_STARTER_ATTRIBUTES = (
    (_thread, ('start_new_thread', 'start_joinable_thread', '_start_joinable_thread')),
    (threading, ('_start_new_thread', '_start_joinable_thread')),
)
_NATIVE_THREAD_STARTERS = {id(value): value
                          for namespace, names in _THREAD_STARTER_ATTRIBUTES
                          for name in names
                          if (value := getattr(namespace, name, None)) is not None}
_BOUND_THREAD_STARTERS = (threading.Thread.start, threading.Thread._bootstrap)
_PREBOUND_STARTER_REASON = (
    'prebound native thread starter reachable from frozen roots; '
    'profiling cannot be guaranteed for threads it creates')


def _references(roots, *, namespaces=(), max_objects=MAX_REFERENCE_OBJECTS):
    """Walk every GC edge, with explicit process-registry boundaries for a leg.

    No container-kind dispatch: tp_traverse supplies the edges. CPython treats
    code, datetime, time and timezone as atomic despite held Python objects.
    Supplements follow native frame/traceback/exception/generator references,
    code members, tzinfo, timezone offset/name and weak targets, without overrides.
    Weak proxies fail closed: Python exposes no safe native target accessor.
    Frames come only from held roots, never an interpreter-stack enumeration.
    Before 3.13, locals require native GC traversal through an owned finished
    frame or its reached generator/coroutine owner; other frames fail closed.
    """
    pending, visited = list(roots), {}
    boundaries = {}
    opaque_frames, native_frames = {}, {}
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
            # Before 3.13, f_locals synchronizes into a user-populated dict and
            # can invoke a colliding key's __eq__. Never materialize it. An
            # owned finished frame traverses its code and locals natively; a
            # suspended frame needs its generator/coroutine owner in this walk.
            if sys.version_info < (3, 13):
                # f_trace is independently traversed even on an opaque frame
                # and can itself equal f_code. Discount that edge: only the
                # additional native code edge proves traversal of locals.
                code_edges = sum(held is value.f_code for held in gc.get_referents(value))
                if code_edges <= (value.f_trace is value.f_code):
                    opaque_frames[identity] = value
            else:
                localns = value.f_locals
                if type(localns) is _FRAME_LOCALS_PROXY:
                    for key, held in localns.items():
                        pending.extend((key, held))
                elif type(localns) is dict:
                    # Non-optimized running exec/class/module frames have no
                    # proxy or native locals GC edge. Traverse exact dicts via
                    # the ordinary bounded walk, preserving registry boundaries.
                    pending.append(localns)
                elif not any(held is localns for held in gc.get_referents(value)):
                    raise AssertionError('opaque frame locals: no safe native locals traversal')
            pending.extend((value.f_globals, value.f_back, value.f_code, value.f_trace))
        # Always retain GC edges, including frames. Registry identity boundaries
        # above apply equally to GC and supplemental edges.
        pending.extend(gc.get_referents(value))
        # Retain *all* native edges and yield the value before this fast path.
        # These exact scalar types have none of the supplemental native fields;
        # subclasses can own references and must take the full path.
        if (value_type is str or value_type is int or value_type is float
                or value_type is bytes or value_type is bool or value_type is type(None)):
            continue
        if value_type is weakref.ProxyType or value_type is weakref.CallableProxyType:
            raise AssertionError('weak proxy in frozen reference graph: no safe native target '
                                 'accessor; hold the strong object instead')
        if issubclass(value_type, weakref.ReferenceType):
            # Always use the unbound native accessor, including when
            # type(value).__call__ is not weakref.ReferenceType.__call__.
            # WeakMethod's instance target is native; GC reaches its function
            # ref. Never dispatch a subclass override to reconstruct a method.
            target = weakref.ReferenceType.__call__(value)
            if target is not None:
                pending.append(target)
        if value_type is TracebackType:
            pending.extend((value.tb_frame, value.tb_next))
        elif (value_type is GeneratorType or value_type is CoroutineType
              or value_type is AsyncGeneratorType):
            frame = (value.gi_frame if value_type is GeneratorType else
                     value.cr_frame if value_type is CoroutineType else value.ag_frame)
            pending.append(frame)
            if frame is not None and any(
                    held is frame.f_code for held in gc.get_referents(value)):
                native_frames[id(frame)] = frame
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
    # Resolve after the complete walk: a frame can precede its native owner.
    # Strong references prevent id reuse; no stack or global owner search.
    assert not opaque_frames.keys() - native_frames.keys(), (
        'opaque frame in frozen reference graph: no safe native locals traversal; '
        'hold its generator/coroutine owner instead (running frames are unsupported)')


def _namespace_roots(*namespaces):
    return [loaded for name, loaded in list(sys.modules.items())
            if any(_in_namespace(name, ns) for ns in namespaces)]


def _clear_shared_abc_caches(*harness):
    # ABC isinstance/issubclass memoization in shared dependencies weakly holds
    # live-leg classes. Reset only these recomputable caches, never virtual
    # subclass registrations, owned classes, or explicitly supplied roots.
    explicit = {id(value) for value in harness}
    for name, loaded in list(sys.modules.items()):
        if not issubclass(type(loaded), ModuleType) or any(
                _in_namespace(name, ns) for ns in (ALIAS, 'trusted_router', 'tests')):
            continue
        for value in tuple(dict.values(_MODULE_DICT.__get__(loaded))):
            if not issubclass(type(value), type) or id(value) in explicit:
                continue
            members = _TYPE_DICT.__get__(value)
            owner = _metadata(members.items(), '__module__')
            if not issubclass(type(owner), str) or str.__eq__(owner, name) is not True:
                continue
            cache = _metadata(members.items(), '_abc_impl')
            if type(cache) is _ABC_DATA and id(cache) not in explicit:
                # The native reset accesses _abc_impl on this plain carrier,
                # never via a dependency's metaclass or instance properties.
                _abc._reset_caches(SimpleNamespace(_abc_impl=cache))


def reject_live_references(*harness):
    """Reject live definitions in frozen globals and explicitly supplied IO roots."""
    from tests.fakes import spanner

    _clear_shared_abc_caches(*harness)
    roots = [*_namespace_roots(ALIAS), spanner, *harness]
    owners = {}
    for value in _references(roots, namespaces=(ALIAS, 'tests.fakes.spanner')):
        value_type = type(value)
        # GC follows partial func/args/keywords, containers, closures and bound
        # methods. Reject the native object wherever that walk encounters it:
        # CPython 3.14 emits no c_call for a builtin invoked through partial.
        if (value_type is BuiltinFunctionType
                and _NATIVE_THREAD_STARTERS.get(id(value)) is value
                or value_type is MethodType
                and issubclass(type(value.__self__), threading.Thread)
                and any(value.__func__ is starter for starter in _BOUND_THREAD_STARTERS)):
            raise AssertionError(_PREBOUND_STARTER_REASON)
        owner = _owner(value, owners)
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
    _require_execution_support()
    for loaded in _namespace_roots(ALIAS, 'trusted_router'):
        _register_generated(loaded)
    first = {}  # Thread id -> first live event; survives a raw worker exiting.
    seen = set()
    recorded = set()
    provenance = {}
    start_codes = set()
    def profile(frame, event, arg):
        if event == 'call':
            name = frame.f_globals.get('__name__', '')
            filename = frame.f_code.co_filename
        elif event == 'c_call':
            if (_NATIVE_THREAD_STARTERS.get(id(arg)) is arg
                    and frame.f_code not in start_codes):
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
        # Never read f_locals here, on any interpreter.
        generated = _GENERATED_NAMES.get(id(frame.f_code))
        if str.__eq__(filename, '<string>') is True and generated is not None:
            qualname = generated[1]
        if live:
            first.setdefault(threading.get_ident(), f'{name}:{qualname}')
        identity = (name, qualname, frame.f_code.co_firstlineno)
        if frozen and identity not in recorded:
            recorded.add(identity)
            source = sys.modules[name].__file__
            relative = str(Path(source).relative_to(ROOT))
            seen.add((name.replace(ALIAS, 'trusted_router', 1), qualname, frame.f_code.co_firstlineno, relative, PINS[relative]))
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    monitoring = sys.monitoring if sys.version_info < (3, 13) else None
    all_threads = getattr(threading, 'setprofile_all_threads', None) if monitoring is None else None
    main_thread = threading.get_ident()
    entered = False
    tool_id = 4

    def monitor_python(code, offset, *args):
        if entered or threading.get_ident() != main_thread:
            profile(sys._getframe(1), 'call', None)

    def monitor_call(code, offset, callable, arg):
        if entered or threading.get_ident() != main_thread:
            if type(callable) is BuiltinFunctionType:
                profile(sys._getframe(1), 'c_call', callable)

    def install_profile():
        # On 3.12 a Python sys.setprofile callback is unsafe even if it never
        # reads f_locals: call_trampoline materializes them before dispatch.
        if monitoring is None:
            sys.setprofile(profile)
    starters = []
    active = set()
    started_threads = set()

    def wrap_start(original):
        def start(function, *args, **kwargs):
            token = object()
            active.add(token)  # Register before startup, including delayed bootstraps.
            def bootstrap(*worker_args, **worker_kwargs):
                started_threads.add(threading.get_ident())
                install_profile()
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
    for namespace, names in _THREAD_STARTER_ATTRIBUTES:
        for name in names:
            if hasattr(namespace, name):
                original = getattr(namespace, name)
                starters.append((namespace, name, original))
                setattr(namespace, name, wrap_start(original))
    # sys.monitoring dispatches directly without the legacy locals trampoline
    # and applies interpreter-wide, including raw threads and their teardown.
    # Refuse an occupied tool id; never displace another monitoring client.
    if monitoring is not None:
        try:
            monitoring.use_tool_id(tool_id, 'frozen execution guard')
        except BaseException:
            for namespace, name, original in reversed(starters):
                setattr(namespace, name, original)
            raise
        for event in (monitoring.events.PY_START, monitoring.events.PY_RESUME,
                      monitoring.events.PY_THROW):
            monitoring.register_callback(tool_id, event, monitor_python)
        monitoring.register_callback(tool_id, monitoring.events.CALL, monitor_call)
        monitoring.set_events(tool_id, monitoring.events.PY_START | monitoring.events.PY_RESUME
                              | monitoring.events.PY_THROW | monitoring.events.CALL)
    else:
        threading.setprofile(profile)
        if all_threads is not None:
            all_threads(profile)
        # Reference preflight is harness work; keep only worker hooks active.
        sys.setprofile(previous)
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
        install_profile()
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
            if monitoring is not None:
                monitoring.set_events(tool_id, 0)
                for event in (monitoring.events.PY_START, monitoring.events.PY_RESUME,
                              monitoring.events.PY_THROW, monitoring.events.CALL):
                    monitoring.register_callback(tool_id, event, None)
                monitoring.free_tool_id(tool_id)
            if all_threads is not None:
                all_threads(previous_thread)
            threading.setprofile(previous_thread)
            sys.setprofile(previous)
            for namespace, name, original in reversed(starters):
                setattr(namespace, name, original)
        assert not first, 'live callable reached by frozen leg: ' + repr(first)
        if entered:
            reject_live_references(*harness)
