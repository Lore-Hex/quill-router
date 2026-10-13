"""Round ten real-close obligation and native harness regression witnesses."""
import asyncio
import json

import pytest

from tests.shadow_observation_diagnostics import client_history, status
from tests.shadow_observation_fixtures import observation_service
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    event_for,
    fake_store,
    service,
)
from tests.test_async_settle_shadow_observation_r8 import delta
from trusted_router.async_settle_shadow_journal import CLOSURE_KIND

DAY = "2026-10-12"


def owe(svc):
    svc.journal.grant(DAY, 100)
    r = svc.port.reserve(NOW_US)
    assert r is not None
    assert svc.port.finalize(r, event_for(r), fold_owed=True)
    assert svc.port.complete(r)
    return r


@pytest.mark.asyncio
async def test_completed_obligation_real_close():
    svc = service()
    r = owe(svc)
    assert svc.port.fold_counts(DAY, delta(), obligation=r)
    await svc.close()
    assert not svc.unclosed and not svc.journal.days, status(svc)
    assert not svc.port.fold_obligations and not svc.port.lost.is_set()


@pytest.mark.asyncio
async def test_concurrent_obligation_real_close(monkeypatch):
    svc = service()
    r = owe(svc)
    entered, release = asyncio.Event(), asyncio.Event()
    async def observer_close():
        entered.set()
        await release.wait()
    monkeypatch.setattr(svc.observer, "close", observer_close)
    closing = asyncio.create_task(svc.close())
    await entered.wait()
    try:
        assert svc.state.shutdown and r in svc.port.fold_owners
        assert await asyncio.to_thread(svc.port.fold_counts, DAY, delta(), obligation=r)
    finally:
        release.set()
        await closing
    assert not svc.unclosed and not svc.journal.days, status(svc)
    assert not svc.port.fold_obligations and not svc.port.lost.is_set()


@pytest.mark.asyncio
async def test_undischarged_obligation_real_close():
    svc = service()
    r = owe(svc)
    await svc.close()
    assert svc.close_finished and svc.unclosed and DAY in svc.journal.days
    assert svc.port.fold_obligations == {r: "pending"}
    assert f"fold:{r.boot}:{r.event_seq}" in svc.state.days[0].observers
    assert not svc.final_plans and svc.last_closure_ack is None


@pytest.mark.asyncio
async def test_obligation_registration_after_close_cutoff(monkeypatch):
    svc = service()
    svc.journal.grant(DAY, 100)
    r = svc.port.reserve(NOW_US)
    entered, release = asyncio.Event(), asyncio.Event()
    async def observer_close():
        entered.set()
        await release.wait()
    monkeypatch.setattr(svc.observer, "close", observer_close)
    closing = asyncio.create_task(svc.close())
    await entered.wait()
    try:
        assert svc.state.shutdown
        assert svc.port.finalize(r, event_for(r), fold_owed=True) is None
        assert svc.port.lost.is_set() and r not in svc.port.fold_obligations
    finally:
        release.set()
        await closing
    assert svc.unclosed


@pytest.mark.asyncio
@pytest.mark.parametrize("receipts", [False, True])
async def test_real_close_advancing_clock_fold_after_interval(receipts):
    store, _ = fake_store()
    wall = [NOW_US+1000_000_000]
    def advancing():
        wall[0] += 100
        return wall[0]/1e6
    store.wall = advancing
    svc = observation_service(store)
    if receipts:
        svc.journal.grant(DAY, 100)
        for _ in range(4):
            r = svc.port.reserve(int(store.wall()*1e6))
            assert r is not None
            assert svc.port.finalize(r, event_for(r))
            assert svc.port.complete(r)
            svc.journal.freeze(DAY)
    with client_history(store) as history:
        await svc.close()
        assert svc.close_finished and not svc.unclosed and not svc.journal.days, status(svc, history)
        assert svc.folded_us > svc.interval_end_us
        assert any(CLOSURE_KIND in op.get("kinds", []) for op in history)
        assert svc.last_closure_ack is not None
        assert json.loads(status(svc))["interval_end_us"] == svc.state.shutdown_stamp


def test_dormancy_per_name_controls():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = '''
import importlib, os, sys
from types import SimpleNamespace
from pathlib import Path
from tests import shadow_observation_dormancy as guard
from tests.shadow_observation_dormancy import EXPLICIT_MODULES, covered_module, Block, assert_dormant
assert os.environ['TR_ASYNC_SETTLE_SHADOW_POLICY'] == ''
root = Path('src')
names = EXPLICIT_MODULES | {'.'.join(p.relative_to(root).with_suffix('').parts)
    for p in root.rglob('*.py') if covered_module('.'.join(p.relative_to(root).with_suffix('').parts))}
for name in sorted(names):
    for loaded in list(sys.modules):
        if covered_module(loaded):
            del sys.modules[loaded]
    blocker = Block()
    sys.meta_path.insert(0, blocker)
    try:
        try:
            importlib.import_module(name)
        except AssertionError as error:
            assert str(error) == 'empty-policy v2 import blocker', (name, error)
        else:
            raise AssertionError('blocker missed '+name)
    finally:
        sys.meta_path.remove(blocker)
    importlib.import_module(name)
    guard.sys = SimpleNamespace(modules={name: sys.modules[name]})
    try:
        assert_dormant()
    except AssertionError as error:
        assert str(error) == 'empty-policy v2 import', (name, error)
    else:
        raise AssertionError('assertion missed '+name)
    finally:
        guard.sys = sys
    print(name)
# Future packages, including a nested location, need no list edit.
for name in ('trusted_router.async_settle_shadow_v2_future',
             'trusted_router.services.future.async_settle_shadow_v2_future'):
    assert covered_module(name)
    try:
        Block().find_spec(name)
    except AssertionError:
        pass
    else:
        raise AssertionError('future blocker missed '+name)
    guard.sys = SimpleNamespace(modules={name: object()})
    try:
        assert_dormant()
    except AssertionError:
        pass
    else:
        raise AssertionError('future assertion missed '+name)
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,  # noqa: S603
        text=True, timeout=180, env=dict(os.environ, TR_ASYNC_SETTLE_SHADOW_POLICY="",
        PYTHONPATH=str(root)+os.pathsep+str(root/"src")))
    assert result.returncode == 0, result.stdout+result.stderr


def test_native_boundary_buffered_lines():
    import os
    import time

    from tests.shadow_observation_diagnostics import wait_boundary
    reader, writer = os.pipe()
    try:
        # Both lines arrive in one write, with the writer remaining open.
        os.write(writer, b'{"boundary": "graceful"}\nBOUNDARY\n')
        with os.fdopen(reader, "rb", buffering=0) as stream:
            output = []
            assert wait_boundary(stream, output, time.monotonic()+1)
        assert output == ['{"boundary": "graceful"}', 'BOUNDARY']
    finally:
        os.close(writer)


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_failure", [False, True])
async def test_native_worker_boundary_uses_owner_snapshot(snapshot_failure, monkeypatch):
    import io
    import threading

    from tests import shadow_observation_diagnostics as diagnostics
    from tests.shadow_observation_diagnostics import hold_boundary
    svc = service()
    svc.loop = asyncio.get_running_loop()
    stream = io.StringIO()
    seen, release = threading.Event(), threading.Event()
    release.set()
    if snapshot_failure:
        def failed(*args):
            raise RuntimeError("snapshot failed")
        monkeypatch.setattr(diagnostics, "status", failed)
        with pytest.raises(RuntimeError, match="snapshot failed"):
            await asyncio.to_thread(hold_boundary, svc, [], "before_ack", stream, seen, release)
        assert not seen.is_set() and not stream.getvalue()
        return
    await asyncio.to_thread(hold_boundary, svc, [], "before_ack", stream, seen, release)
    assert seen.is_set()
    lines = stream.getvalue().splitlines()
    assert lines[-1] == "BOUNDARY"
    assert json.loads(lines[0])["boundary"] == "before_ack"


def test_native_history_preserves_suppressed_rpc_cause(monkeypatch):
    store, _ = fake_store()
    def failed(*args, **kwargs):
        try:
            raise ValueError("native cause witness")
        except ValueError:
            raise RuntimeError("shadow_rpc_failed") from None
    monkeypatch.setattr(store.database, "run_in_transaction", failed)
    with client_history(store) as history:
        with pytest.raises(RuntimeError, match="shadow_rpc_failed"):
            store.database.run_in_transaction(lambda tx: None)
    assert history[-1]["causes"] == [dict(error="ValueError", reason="native cause witness")]
