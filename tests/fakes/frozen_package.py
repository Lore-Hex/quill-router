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
from collections.abc import Mapping, Sequence, Set
from contextlib import contextmanager
from pathlib import Path
from types import FunctionType, MemberDescriptorType, MethodType, ModuleType

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


def _owner(value):
    owner = (value.__name__ if isinstance(value, ModuleType)
             else getattr(value, '__module__', type(value).__module__))
    return owner if isinstance(owner, str) else getattr(owner, '__name__', '')


def _in_namespace(name, namespace):
    return name == namespace or name.startswith(namespace + '.')


def _live_source(filename):
    return '/src/trusted_router/' in filename and not filename.startswith(str(ROOT) + '/')


def _references(roots, *, namespaces):
    """Walk data/captures, not the entire interpreter via external module globals.

    External functions' captures are inspected too. Harness function globals used
    by bytecode are followed. External module globals and logging registries
    are boundaries (see the appendix); class definitions and bases are inspected.
    """
    pending, visited = list(roots), {}
    root_ids = {id(root) for root in pending}
    while pending:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited[id(value)] = value  # Keep synthesized state alive; ids must not recycle.
        if value is None or type(value) in (str, bytes, int, float, bool, complex):
            continue
        yield value
        owner = _owner(value)
        owned = any(_in_namespace(owner, ns) for ns in namespaces)
        if isinstance(value, ModuleType):
            if owned or id(value) in root_ids:
                pending.extend(v for k, v in list(vars(value).items())
                               if k not in {'__builtins__', '__loader__', '__spec__'})
            continue
        # Loggers are process-wide registries, not IO/callback inputs. Following
        # their manager walks every live application logger/formatter in Python.
        # Executed formatter code is still covered by the call profiler.
        if isinstance(value, logging.Logger) and id(value) not in root_ids:
            continue
        if isinstance(value, (Mapping, Sequence, Set)):
            try:
                if isinstance(value, Mapping):
                    for key in value:
                        pending.extend((key, value[key]))
                else:
                    pending.extend(value)
            except (TypeError, ValueError, RuntimeError):
                # E.g. Pydantic's unbuilt abstract schema refuses iteration.
                # Its attributes/slots and class definitions are still walked.
                pass
        elif isinstance(value, functools.partial):
            pending.extend((value.func, value.args, value.keywords))
        elif isinstance(value, MethodType):
            pending.extend((value.__func__, value.__self__))
        elif isinstance(value, FunctionType):
            pending.extend((value.__defaults__, value.__kwdefaults__))
            for cell in value.__closure__ or ():
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
            # The frozen modules are already roots. Only callbacks/fake IO need
            # global-name resolution; walking all test globals would include the
            # deliberately live comparison leg and pytest's process registries.
            if owner.startswith('tests.'):
                pending.extend(value.__globals__[name] for name in value.__code__.co_names
                               if name in value.__globals__ and name != '__builtins__')
        elif isinstance(value, (staticmethod, classmethod)):
            pending.append(value.__func__)
        elif isinstance(value, property):
            pending.extend((value.fget, value.fset, value.fdel))
        # Includes C bound methods/method-wrappers (dict.get, cached.__call__).
        try:
            pending.append(object.__getattribute__(value, '__self__'))
        except (AttributeError, TypeError):
            pass
        if isinstance(value, functools._lru_cache_wrapper):
            # CPython exposes keys/results as GC referents even though the cache
            # has no public item iterator. Inspect before clearing its state.
            pending.extend(gc.get_referents(value))
        if isinstance(value, type):
            pending.extend(vars(value).values())
            pending.extend(value.__bases__)
            continue
        try:
            attributes = object.__getattribute__(value, '__dict__')
        except (AttributeError, TypeError):
            attributes = None
        if attributes is not None:
            pending.append(attributes)
        # Descriptor names are already mangled; walking every MRO dictionary
        # also preserves distinct base/subclass slots with the same spelling.
        for cls in type(value).__mro__:
            for descriptor in vars(cls).values():
                if isinstance(descriptor, MemberDescriptorType):
                    # Function globals are handled by the namespace/bytecode
                    # rules above, not as an external interpreter registry.
                    if isinstance(value, FunctionType) and descriptor.__name__ in {
                        '__globals__', '__builtins__',
                    }:
                        continue
                    try:
                        pending.append(descriptor.__get__(value, type(value)))
                    except AttributeError:  # An unset slot has no reference.
                        pass
        try:
            pending.append(object.__getattribute__(value, '__wrapped__'))
        except AttributeError:
            pass
        # Includes custom pickle state and the default dict/slot state. Do not
        # evaluate arbitrary properties: cached property values are in __dict__.
        try:
            getstate = object.__getattribute__(value, '__getstate__')
        except AttributeError:
            pass
        else:
            try:
                pending.append(getstate())
            except (TypeError, ValueError, RuntimeError):  # Non-pickleable objects still expose dict/slot state.
                pass
        pending.append(type(value))


def _namespace_roots(*namespaces):
    return [loaded for name, loaded in list(sys.modules.items())
            if any(_in_namespace(name, ns) for ns in namespaces)]


def reject_live_references(*harness):
    """Reject live definitions in frozen globals and explicitly supplied IO roots."""
    from tests.fakes import spanner

    roots = [*_namespace_roots(ALIAS), spanner, *harness]
    for value in _references(roots, namespaces=(ALIAS, 'tests.fakes.spanner')):
        owner = _owner(value)
        code = value.__code__ if isinstance(value, FunctionType) else None
        assert not (_in_namespace(owner, 'trusted_router')
                    or code is not None and _live_source(code.co_filename)), (
            'live reference in frozen namespace: '
            + owner + ':' + getattr(value, '__qualname__', type(value).__qualname__))


def clear_functools_caches(*harness, external_only=False):
    roots = [*_namespace_roots(ALIAS, 'trusted_router', 'tests.fakes.spanner'), *harness]
    # Materialize before clearing so nested caches in keys/results are included.
    caches = [value for value in _references(
        roots, namespaces=(ALIAS, 'trusted_router', 'tests.fakes.spanner'))
        if isinstance(value, functools._lru_cache_wrapper)]
    for cache in caches:
        if not external_only or not any(_in_namespace(_owner(cache), ns)
                                        for ns in (ALIAS, 'trusted_router', 'tests')):
            cache.cache_clear()
    return caches


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
            cache.cache_clear()
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
