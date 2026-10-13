"""Reentrant signals, safe diagnostics and the pending-request transport gate."""
import asyncio
import dataclasses
import io
import json
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.shadow_observation_diagnostics import client_history, status
from tests.shadow_observation_gate import CommitGate
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    service,
)
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.services import async_settle_shadow_health as shell


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["step", "assignment", "admission_lock"])
async def test_signal_revocation_survives_interrupted_owner(boundary, monkeypatch):
    svc = service()
    svc.store.clock, svc.store.wall = lambda: 100., lambda: NOW_US/1e6
    svc.tick(NOW_US, publisher=True)
    job = svc.state.job
    previous, previous_trace = signal.getsignal(signal.SIGTERM), sys.gettrace()
    calls, epochs = [], [svc.revocation_epoch]
    signal.signal(signal.SIGTERM, lambda *_: calls.append(svc.revocation_epoch))
    svc.trap_sigterm()
    interrupted = []
    inside_handler = [False]
    assert_owner = svc.assert_owner
    def owner_only():
        assert not inside_handler[0], "signal handler accessed owner State"
        assert_owner()
    monkeypatch.setattr(svc, "assert_owner", owner_only)
    def interrupt():
        interrupted.append(True)
        inside_handler[0] = True
        try:
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        finally:
            inside_handler[0] = False
        epochs.append(svc.revocation_epoch)
        assert svc.signal_stamp == 100. and svc.termination_deadline == 109.
    original = m.step
    def step(state, event):
        result = original(state, event)
        if isinstance(event, m.Tick) and not interrupted:
            interrupt()
        return result
    setter = shell.Service._state.fset.__code__
    def trace(frame, event, arg):
        if frame.f_code is setter and event == "line" and not interrupted:
            # The property setter is the actual assignment boundary, without
            # substituting lifecycle.step or depending on a fixed line number.
            interrupt()
        return trace
    try:
        if boundary == "step":
            monkeypatch.setattr(m, "step", step)
        elif boundary == "assignment":
            sys.settrace(trace)
        else:
            with svc.admission_lock:
                interrupt()
        svc.tick(NOW_US, publisher=True)
        sys.settrace(previous_trace)
        epochs.append(svc.revocation_epoch)
        assert interrupted and calls == [1, 1]
        assert epochs == sorted(epochs) and svc.revocation_epoch == 1
        assert svc.state.generation == svc.generation == job.generation
        claims = []
        monkeypatch.setattr(svc.store, "claim", lambda *args: claims.append(True))
        reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
            svc.settings.source_revision, job, NOW_US, svc)
        assert not claims and reply.reason == "revoked"
        await asyncio.sleep(0)
        assert svc.close_task is not None
        await svc.close_task
        epochs.append(svc.revocation_epoch)
        assert epochs == sorted(epochs) and svc.state.shutdown
        assert svc.termination_deadline == 109.
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGTERM, previous)


def test_diagnostics_exclude_payload_and_token(monkeypatch):
    from google.api_core.exceptions import Aborted
    svc = service()
    markers = ("PRIVATE-PAYLOAD", "PRIVATE-TOKEN")
    marker = "payload="+markers[0]+" token="+markers[1]
    svc.tick(NOW_US, publisher=False)
    svc._state = dataclasses.replace(svc.state, days=(dataclasses.replace(svc.state.days[0],
        observers=frozenset({marker}), attachments=frozenset({marker}),
        observer_keys=frozenset({marker}), attachment_keys=frozenset({marker})),))
    svc.last_closure_ack = dict(boot=marker, secret=marker, closure_sha256=marker, elapsed_us=7)
    def fail(*args, **kwargs):
        try:
            raise Aborted(marker)
        except Aborted:
            raise RuntimeError(marker) from None
    monkeypatch.setattr(svc.store.database, "run_in_transaction", fail)
    with client_history(svc.store) as history:
        with pytest.raises(RuntimeError):
            svc.store.database.run_in_transaction(lambda tx: None)
    rendered = status(svc, history)
    assert all(value not in rendered for value in markers)
    assert history[-1]["causes"] == [dict(error="Aborted", code="ABORTED")]
    assert json.loads(rendered)["last_closure_ack"]["elapsed_us"] == 7


class SocketPairListener:
    """Local stream control for sandboxes that prohibit loopback bind."""
    def __init__(self, accepted=None):
        self.accepted = accepted
    def settimeout(self, timeout):
        self.timeout = timeout
    def getsockname(self):
        return ("127.0.0.1", 1)
    def accept(self):
        if self.accepted is None:
            threading.Event().wait(self.timeout)
            raise TimeoutError
        return self.accepted, None
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


@pytest.mark.parametrize("deliver", [False, True])
def test_pending_gate_buffers_request_until_after_client_death(deliver):
    client, downstream = socket.socketpair()
    server, upstream_socket = socket.socketpair()
    gate = CommitGate("127.0.0.1:1", timeout=6, listener=SocketPairListener(downstream),
        connect=lambda *args, **kwargs: upstream_socket)
    preface = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
    def frame(kind, flags, data):
        return len(data).to_bytes(3, "big")+bytes([kind, flags])+b"\0\0\0\1"+data
    received = bytearray()
    def upstream():
        with server:
            server.settimeout(3)
            while chunk := server.recv(65536):
                received.extend(chunk)
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(upstream)
        with client:
            client.sendall(preface)
            gate.arm()
            client.sendall(frame(6, 0, b"pingping"))
            assert not gate.pending.is_set()  # A control frame is not a commit.
            request = frame(1, 4, b"headers")+frame(0, 1, b"commit-request")
            client.sendall(request)
            assert gate.pending.wait(2)
            assert request in gate.buffer and b"commit-request" not in received
            client.sendall(frame(3, 0, b"cancel"))
        # The client is dead BEFORE the parent chooses drop versus delivery.
        gate.close(deliver=deliver)
        result.result(timeout=3)
    assert (b"commit-request" in received) == deliver
    assert b"cancel" not in received


def test_pending_gate_has_hard_deadline():
    gate = CommitGate("127.0.0.1:1", timeout=.01, listener=SocketPairListener())
    gate.thread.join(timeout=1)
    with pytest.raises(AssertionError, match="gate accept deadline"):
        gate.close()


@pytest.mark.asyncio
async def test_worker_snapshot_rejects_state_reads_on_worker():
    from tests.shadow_observation_diagnostics import hold_boundary
    svc = service()
    svc.loop = asyncio.get_running_loop()
    seen, release = threading.Event(), threading.Event()
    release.set()
    stream = io.StringIO()
    await asyncio.to_thread(hold_boundary, svc, [], "request_pending", stream, seen, release)
    assert seen.is_set() and "BOUNDARY" in stream.getvalue()


@pytest.mark.parametrize("repeat", [1, 2])
def test_parent_coalesced_lines_with_open_writer(repeat):
    import os

    from tests.shadow_observation_diagnostics import wait_boundary
    reader, writer = os.pipe()
    try:
        os.write(writer, b'{"boundary":"ready"}\nBOUNDARY\n')
        with os.fdopen(reader, "rb", buffering=0) as stream:
            output = []
            assert wait_boundary(stream, output, time.monotonic()+1)
            assert output == ['{"boundary":"ready"}', 'BOUNDARY']
    finally:
        os.close(writer)


def test_dead_child_sessions_are_released_by_exact_name():
    from types import SimpleNamespace

    from google.cloud.spanner_v1.types import ExecuteSqlRequest

    from tests.shadow_observation_diagnostics import release_sessions, track_sessions
    database = "projects/test/instances/native/databases/isolated"
    session = database+"/sessions/dead-child"
    calls, saves = [], []
    api = SimpleNamespace(execute_streaming_sql=lambda **kwargs: calls.append(kwargs),
        delete_session=lambda **kwargs: calls.append(kwargs))
    manifest = dict(sessions=[])
    track_sessions(api, manifest, lambda: saves.append(True))
    request = ExecuteSqlRequest(session=session, sql="SELECT 1")
    api.execute_streaming_sql(request=request)
    api.execute_streaming_sql(request=request)
    assert len(saves) == 1 and manifest["sessions"] == [session]
    assert calls == [dict(request=request), dict(request=request)]
    release_sessions(api, database, manifest)
    assert calls[-1] == dict(request=dict(name=session), retry=None, timeout=2)
    with pytest.raises(AssertionError):
        release_sessions(api, "projects/other", manifest)
    assert len(calls) == 3
