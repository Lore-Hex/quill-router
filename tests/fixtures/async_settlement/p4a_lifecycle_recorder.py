"""Fresh interpreter witness; tracing starts before any router/test import."""
import asyncio
import builtins
import json
import sys
import threading
from pathlib import Path

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
owned = {str(root/'src/trusted_router'/path) for path in (
    'async_settle_shadow_cohort.py', 'services/async_settle_shadow_cohort.py',
    'services/async_settle_shadow_cohort_runtime.py')}

entries, imports = [], []
original_import = builtins.__import__


def importer(name, globals=None, locals=None, fromlist=(), level=0):
    if 'shadow_cohort' in name or any('shadow_cohort' in item for item in (fromlist or ())):
        imports.append(name)
    return original_import(name, globals, locals, fromlist, level)


def recorder(frame, event, arg):
    if event == 'call' and frame.f_code.co_filename in owned:
        entries.append((frame.f_globals.get('__name__', ''), frame.f_code.co_qualname))


builtins.__import__ = importer
sys.setprofile(recorder)
threading.setprofile(recorder)
try:
    import tests.conftest  # noqa: F401, E402
    from tests.test_async_settle_ticket import settings
    from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
    from trusted_router.main import create_app

    _seed_typed_gateway_store()
    evidence = {}
    # Dormant modes have no package state. Run them before the positive control
    # in one fresh interpreter; do not erase sys.modules to fake fresh imports.
    assert not imports and not entries
    for mode in ('empty', 'no_members', 'money', 'active'):
        imports.clear()
        entries.clear()
        cfg = settings(async_settle_shadow_policy='' if mode == 'empty' else 'shadow-v2',
            async_settle_enabled=mode == 'money',
            async_settle_shadow_workspaces='' if mode == 'no_members' else 'member')
        app = create_app(cfg, configure_store_arg=False, init_observability=False)
        async def lifecycle(app=app):
            async with app.router.lifespan_context(app):
                pass
        asyncio.run(lifecycle())
        evidence[mode] = dict(imports=list(imports), entries=list(entries),
                             installed=hasattr(app.state, 'async_settle_shadow_cohort'))

finally:
    sys.setprofile(None)
    threading.setprofile(None)
    builtins.__import__ = original_import
print(json.dumps(evidence))
