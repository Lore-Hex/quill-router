"""A separate, byte-pinned f83bbaac package; no fallback to live router code."""
from __future__ import annotations

import builtins
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import sys
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import FunctionType, MethodType, ModuleType

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


def reject_live_references():
    """Also reject dormant/cached live aliases, even if a C cache skips its body.

    Inspect snapshot-owned globals, definitions, defaults and closures, never a
    list of approved modules/functions. External runtime internals remain external.
    """
    visited = set()
    def visit(value):
        if id(value) in visited:
            return
        visited.add(id(value))
        owner = (value.__name__ if isinstance(value, ModuleType)
                 else getattr(value, '__module__', type(value).__module__))
        if not isinstance(owner, str):
            owner = getattr(owner, '__name__', '')
        assert owner != 'trusted_router' and not owner.startswith('trusted_router.'), (
            'live reference in frozen namespace: '
            + owner + ':' + getattr(value, '__qualname__', type(value).__qualname__))
        if isinstance(value, dict):
            for key, item in list(value.items()):
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item)
        elif isinstance(value, MethodType):
            visit(value.__func__)
            visit(value.__self__)
        elif isinstance(value, FunctionType) and owner.startswith(ALIAS + '.'):
            visit(value.__defaults__)
            visit(value.__kwdefaults__)
            for cell in value.__closure__ or ():
                try:
                    visit(cell.cell_contents)
                except ValueError:  # empty closure cell
                    pass
        elif isinstance(value, (staticmethod, classmethod)):
            visit(value.__func__)
        elif isinstance(value, property):
            visit(value.fget)
            visit(value.fset)
            visit(value.fdel)
        elif owner == ALIAS or owner.startswith(ALIAS + '.'):
            if hasattr(value, '__dict__'):
                for item in list(vars(value).values()):
                    visit(item)
            if hasattr(value, '__wrapped__'):
                visit(value.__wrapped__)
    for name, loaded in list(sys.modules.items()):
        if name == ALIAS or name.startswith(ALIAS + '.'):
            visit(loaded)


@contextmanager
def execution_guard():
    """Audit every Python/C call in this thread and new HTTP worker threads.

    Record the first live call even if application exception handling swallows it.
    There are no production-module or omitted-definition exemptions. Module globals
    catch generated dataclass methods (<string>) as well as ordinary source code.
    """
    reject_live_references()
    first = []
    seen = set()
    def profile(frame, event, arg):
        if event == 'call':
            name = frame.f_globals.get('__name__', '')
            qualname = frame.f_code.co_qualname
            filename = frame.f_code.co_filename
            # dataclasses compile methods with a generic code qualname. Retain
            # the owning class so distinct generated constructors do not collapse.
            if filename == '<string>' and 'self' in frame.f_locals:
                qualname = type(frame.f_locals['self']).__qualname__ + '.' + frame.f_code.co_name
        elif event == 'c_call':
            name = getattr(arg, '__module__', '') or ''
            qualname = getattr(arg, '__qualname__', type(arg).__qualname__)
            filename = ''
        else:
            return
        if not isinstance(name, str):
            name = getattr(name, '__name__', '')
        if name == 'trusted_router' or name.startswith('trusted_router.') or '/src/trusted_router/' in filename and not filename.startswith(str(ROOT)):
            if not first:
                first.append(f'{name}:{qualname}')
        if name == ALIAS or name.startswith(ALIAS + '.'):
            source = sys.modules[name].__file__
            relative = str(Path(source).relative_to(ROOT))
            seen.add((name.replace(ALIAS, 'trusted_router', 1), qualname, frame.f_code.co_firstlineno, relative, PINS[relative]))
    previous, previous_thread = sys.getprofile(), threading.getprofile()
    sys.setprofile(profile)
    threading.setprofile(profile)
    try:
        yield seen
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_thread)
        assert not first, 'live callable reached by frozen leg: ' + ', '.join(first)
        reject_live_references()
