"""Local controls for disposable native resources, fail-fast RPCs and late delivery."""
import io
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import pytest

from tests.shadow_observation_diagnostics import NativeLiveness
from tests.shadow_observation_fixtures import NATIVE_RPC_SECONDS, native_rpc_deadlines
from tests.shadow_observation_gate import CommitGate
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation_r11 import SocketPairListener


def test_closed_port_names_previous_test_within_two_seconds(monkeypatch):
    # Port 1 has no service on CI; restricted sandboxes may instead deny the
    # connection. Both must fail promptly through the same OSError path.
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "127.0.0.1:1")
    state = NativeLiveness()
    state.previous = "native::previous-history"
    started = time.monotonic()
    with pytest.raises(AssertionError, match="emulator not reachable; previous native test: native::previous-history"):
        state.check()
    assert time.monotonic()-started < 2


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped"])
def test_real_stderr_markers_and_failure_probe(monkeypatch, outcome):
    real, captured = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", real)
    monkeypatch.setattr(sys, "stderr", captured)
    state = NativeLiveness()
    checks = []
    monkeypatch.setattr(state, "check", lambda: checks.append(True))
    state.start("native::history")
    for phase, result in (("setup", "passed"), ("call", outcome), ("teardown", "passed")):
        state.report(SimpleNamespace(nodeid="native::history", when=phase, outcome=result,
                                     failed=result == "failed", longrepr=""))
    assert real.getvalue().splitlines() == ["NATIVE_START native::history", f"NATIVE_END native::history {outcome}"]
    assert not captured.getvalue()
    assert len(checks) == 2
    assert state.previous == "native::history"


def test_failure_path_dead_emulator_keeps_attribution(monkeypatch):
    state = NativeLiveness()
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    state.start("native::prior")
    state.report(SimpleNamespace(nodeid="native::prior", when="teardown", outcome="passed", failed=False))
    state.start("native::current")
    def dead():
        raise AssertionError(f"emulator not reachable; previous native test: {state.previous}")
    monkeypatch.setattr(state, "check", dead)
    report = SimpleNamespace(nodeid="native::current", when="call", outcome="failed", failed=True, longrepr="original error")
    state.report(report)
    assert "original error\nemulator not reachable; previous native test: native::prior" == report.longrepr
    state.report(SimpleNamespace(nodeid="native::current", when="teardown", outcome="passed", failed=False, longrepr=""))


def test_each_death_history_has_disposable_resources(monkeypatch):
    from tests.conformance import spanner_emulator
    from tests.conformance.test_shadow_observation_native import shadow_emulator_resources
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    events = []
    @contextmanager
    def resource():
        database = SimpleNamespace(drop=lambda: events.append(("drop", database)))
        events.append(("create", database))
        try:
            yield database, "instance"
        finally:
            events.append(("release", database))
    monkeypatch.setattr(spanner_emulator, "emulator_resources", resource)
    resources = contextmanager(shadow_emulator_resources.__wrapped__)
    with ExitStack() as stack:
        pool = stack, []
        ordinary = SimpleNamespace(node=SimpleNamespace(name="ordinary"))
        with resources(ordinary, None, pool, None) as first:
            pass
        with resources(ordinary, None, pool, None) as second:
            assert second is first
        for point in ("before_ack", "request_pending_drop"):
            request = SimpleNamespace(node=SimpleNamespace(name="process_restart["+point+"]"))
            with resources(request, None, pool, None) as separate:
                assert separate[0] is not first[0]
            assert events[-2:] == [("drop", separate[0]), ("release", separate[0])]
    assert len([e for e in events if e[0] == "create"]) == 3
    assert events[-2:] == [("drop", first[0]), ("release", first[0])]


@pytest.mark.parametrize("timeout", [None, .125, 600])
def test_rpc_deadlines_include_default_admin_and_data_calls(timeout):
    from google.api_core.gapic_v1.method import _GapicCallable
    calls = []
    call = _GapicCallable(lambda **kw: calls.append(kw), retry=None, timeout=None, compression=None)
    with native_rpc_deadlines():
        call(timeout=timeout)
        call()  # SDK create/drop/poll/session operations omit timeout.
    assert 0 < calls[0]["timeout"] <= min(timeout or NATIVE_RPC_SECONDS, NATIVE_RPC_SECONDS)
    assert 0 < calls[1]["timeout"] <= NATIVE_RPC_SECONDS
    call()
    assert "timeout" not in calls[-1]  # Scope restoration.


def test_operation_polling_has_total_deadline():
    from google.api_core.future.polling import PollingFuture
    class NeverDone(PollingFuture):
        def cancel(self):
            return False
        def cancelled(self):
            return False
        def done(self, retry=None):
            return False
    started = time.monotonic()
    with native_rpc_deadlines(), pytest.raises(TimeoutError):
        NeverDone().result(timeout=.04)
    assert time.monotonic()-started < .5


def frame(kind, flags, stream, data):
    return len(data).to_bytes(3, "big")+bytes([kind, flags])+stream.to_bytes(4, "big")+data


@pytest.mark.parametrize("deliver", [False, True], ids=["drop-native-history", "deliver-after-death-local-only"])
def test_complete_http2_continuation_and_fake_store(deliver):
    from tests.test_async_settle_shadow_observation import maximum_rows, service
    from trusted_router.storage_gcp_shadow_observation import Budget
    svc = service()
    pages, counter, closure = maximum_rows("c"*64)
    client, downstream = socket.socketpair()
    server, upstream_socket = socket.socketpair()
    drained = bytearray()
    class ObservedUpstream:
        def __getattr__(self, name):
            return getattr(upstream_socket, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            upstream_socket.close()
        def recv(self, size):
            chunk = upstream_socket.recv(size)
            drained.extend(chunk)
            return chunk
    gate = CommitGate("127.0.0.1:1", timeout=3, listener=SocketPairListener(downstream),
                      connect=lambda *a, **kw: ObservedUpstream())
    preface = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
    settings = frame(4, 0, 0, b"")
    # HPACK static indexed :method POST, :scheme http, :path /; complete frames.
    headers = frame(1, 4, 1, b"\x83\x86\x84")
    data = frame(0, 1, 1, b"commit")
    prefix_seen, eof_seen, reply_drained = threading.Event(), threading.Event(), threading.Event()
    received = bytearray()
    def fake_server():
        with server:
            server.settimeout(2)
            while chunk := server.recv(65536):
                received.extend(chunk)
                if received == preface+settings:
                    prefix_seen.set()
            eof_seen.set()
            assert bytes(received) == preface+settings+(headers+data if deliver else b"")
            # A half-close must still permit the server's final response.
            server.sendall(frame(0, 1, 1, b"ack"))
            reply_drained.set()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(fake_server)
        try:
            with client:
                client.sendall(preface+settings)
                assert prefix_seen.wait(1)
                gate.arm()
                client.sendall(headers+data[:-1])
                assert not gate.pending.wait(.03), "partial DATA must not signal pending"
                client.sendall(data[-1:])
                assert gate.pending.wait(1)
                assert not eof_seen.is_set()
            gate.close(deliver=deliver)
            future.result(timeout=2)
        finally:
            gate.close()
    assert eof_seen.is_set() and reply_drained.is_set()
    assert bytes(drained) == frame(0, 1, 1, b"ack")
    # Wire receipt was proved above after client death. Apply the fake commit
    # separately: CPU-heavy row validation under coverage must not manufacture
    # a slow-server failure in the 0.5-second orderly-drain witness.
    if deliver:
        svc.store.flush(pages, counter, closure, Budget(svc.store.clock()+4, svc.store.clock))
    assert svc.store.reconcile([closure], Budget(svc.store.clock()+4, svc.store.clock)) == deliver
    svc.publisher_executor.shutdown(wait=True)
    svc.evidence_executor.shutdown(wait=True)


def test_gate_forwarding_and_orderly_drain_have_hard_deadlines():
    for finish in (False, True):
        client, downstream = socket.socketpair()
        server, upstream = socket.socketpair()
        gate = CommitGate("127.0.0.1:1", timeout=.1, listener=SocketPairListener(downstream),
                          connect=lambda *a, _upstream=upstream, **kw: _upstream)
        started = time.monotonic()
        with client, server:
            if finish:
                gate.close()  # Server deliberately never replies/closes.
            else:
                gate.thread.join(timeout=1)
                with pytest.raises(AssertionError, match="gate forwarding deadline"):
                    gate.close()
        assert time.monotonic()-started < 1


def test_connection_refused_probe_uses_one_second_timeout(monkeypatch):
    state = NativeLiveness()
    state.previous = "native::culprit"
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "127.0.0.1:1")
    calls = []
    def refused(address, *, timeout):
        calls.append((address, timeout))
        raise ConnectionRefusedError("closed port")
    monkeypatch.setattr(socket, "create_connection", refused)
    started = time.monotonic()
    with pytest.raises(AssertionError, match="emulator not reachable; previous native test: native::culprit"):
        state.check()
    assert calls == [(("127.0.0.1", 1), 1)]
    assert time.monotonic()-started < 2


def test_serial_native_budget_cannot_pass_after_five_minutes(monkeypatch):
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    state = NativeLiveness()
    state.start("native::slow")
    state.started -= 286
    report = SimpleNamespace(nodeid="native::slow", when="teardown", outcome="passed", failed=False, longrepr="")
    state.report(report)
    assert report.outcome == "failed"
    assert "native module exceeded 285s serial budget" in report.longrepr


def test_autouse_marker_suspends_pytest_capture(monkeypatch):
    from tests.conformance.test_shadow_observation_native import native_watchdog
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    active = []
    @contextmanager
    def disabled():
        active.append(True)
        yield
        active.pop()
    class RealStderr(io.StringIO):
        def write(self, value):
            assert active, "start marker swallowed by pytest capture"
            return super().write(value)
    stderr = RealStderr()
    monkeypatch.setattr(sys, "__stderr__", stderr)
    capture = SimpleNamespace(global_and_fixture_disabled=disabled)
    request = SimpleNamespace(node=SimpleNamespace(nodeid="native::fixture"),
        config=SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: capture)))
    native_watchdog.__wrapped__(request, NativeLiveness())
    assert stderr.getvalue() == "NATIVE_START native::fixture\n"
