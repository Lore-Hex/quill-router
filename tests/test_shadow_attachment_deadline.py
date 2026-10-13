"""Real HTTP deadlines: response progress must never renew a lane budget."""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation_a1 import basis
from trusted_router.async_settle_shadow_attachments import Lane
from trusted_router.storage_gcp_shadow_attachments import GCSAttachments
from trusted_router.storage_gcp_shadow_observation import Budget


@pytest.mark.parametrize("fault", ["trickle", "headers", "mid_body", "never_close"])
def test_http_absolute_deadline_keeps_unknown(fault, tmp_path):
    release = threading.Event()
    entered = threading.Event()
    payload = [b""]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if fault == "headers":
                entered.set()
                release.wait()
                return
            self.send_response(200)
            if fault != "never_close":
                self.send_header("Content-Length", str(len(payload[0])))
            self.end_headers()
            entered.set()
            try:
                if fault == "trickle":
                    # Inactivity-only timeouts take at least 12 seconds here,
                    # beyond the generous structural bound below.
                    for byte in payload[0][:40]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if release.wait(.3):
                            return
                    self.wfile.write(payload[0][40:])
                elif fault == "mid_body":
                    self.wfile.write(payload[0][:1])
                    self.wfile.flush()
                    release.wait()
                else:
                    release.wait()
            except (BrokenPipeError, ConnectionResetError):
                pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        if "CI" in os.environ:
            pytest.fail("CI must permit native loopback qualification")
        pytest.skip("sandbox forbids binding a local HTTP server; requires unrestricted CI")
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    try:
        server_thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/"
        transport = GCSAttachments("offline")
        transport.endpoint = endpoint
        transport.authenticated = False
        lane = Lane("basis", "issuer", transport)
        lane.append("2026-10-12", basis())
        item = lane.queue[0]
        item.uncertain = True
        payload[0] = item.body
        start = time.monotonic()
        lane.step(Budget(start+2.))
        elapsed = time.monotonic()-start
        # Generous scheduling/cleanup bound. Tight latency is a measurement,
        # never a claim that a shared runner schedules within milliseconds.
        assert elapsed < 10
        assert lane.queue == [item] and item.uncertain
        assert lane.operations == dict(put=0, get=1, sent_bytes=0, received_bytes=0)
        (tmp_path/"deadline.json").write_text(json.dumps(dict(fault=fault, budget=2.,
            elapsed=elapsed, server_reached=entered.is_set())))
        assert entered.is_set(), "operation never reached the server fault"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join()



@pytest.mark.parametrize("parts", [3, 12], ids=["reviewer-late-get", "reviewer-entire-flush-trickle"])
def test_reviewer_stream_deadlines(monkeypatch, parts):
    """Both review stream witnesses, now using the production socket reader."""
    from types import SimpleNamespace

    from trusted_router import storage_gcp_shadow_attachments as module
    now = [0.]
    transport = GCSAttachments("offline")
    transport.authenticated = False
    lane = Lane("basis", "issuer", transport)
    lane.append("2026-10-12", basis())
    item = lane.queue[0]
    item.uncertain = True
    reads = []
    class Response:
        status = 200
        fp = None
        def read1(self, cap):
            reads.append(cap)
            now[0] += 1.
            return item.body[len(reads)-1:len(reads)] if len(reads) < parts else item.body[parts-1:]
        def getheaders(self): return []
        def close(self): pass
    class Connection:
        def __init__(self, *a, **kw): self.sock = SimpleNamespace(settimeout=lambda v: None, shutdown=lambda v: None)
        def connect(self): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    lane.step(Budget(4., lambda: now[0]))
    assert now[0] == 2. and len(reads) == 2
    assert max(reads) <= 8192
    assert lane.queue == [item] and item.uncertain
    assert lane.operations["get"] == 1 and lane.operations["received_bytes"] == 0


def test_late_transport_return_never_acknowledges_get():
    now = [0.]
    lane = Lane("basis", "issuer", None)
    lane.append("2026-10-12", basis())
    item = lane.queue[0]
    item.uncertain = True
    class Transport:
        def get(self, key, timeout, *, deadline, clock):
            assert timeout == deadline == 2.
            now[0] = 3.
            return item.body
    lane.transport = Transport()
    lane.step(Budget(4., lambda: now[0]))
    assert lane.queue == [item] and item.uncertain


@pytest.mark.parametrize("fault", ["transport", "503"])
def test_auth_failure_is_not_retried(monkeypatch, fault):
    import google.auth

    from trusted_router import storage_gcp_shadow_attachments as module
    calls = []
    def exchange(self, *args):
        calls.append(args)
        if fault == "transport":
            raise TimeoutError("offline")
        return 503, {}, b""
    monkeypatch.setattr(module.Lifetime, "exchange", exchange)
    def default(**kwargs):
        kwargs["request"](url="http://offline.invalid", method="GET")
        raise AssertionError("failed authentication returned")
    monkeypatch.setattr(google.auth, "default", default)
    with pytest.raises(RuntimeError, match="attachment_auth"):
        GCSAttachments("offline").get("exact-key", 2.)
    assert len(calls) == 1


def test_lane_passes_absolute_deadline_before_transport_start():
    now = [100.]
    launched = []
    class Delayed(GCSAttachments):
        def get(self, key, timeout, **kwargs):
            now[0] += 1
            return super().get(key, timeout, **kwargs)
        def _request(self, *args):
            launched.append(True)
    lane = Lane("basis", "issuer", Delayed("offline"))
    lane.append("2026-10-12", basis())
    lane.queue[0].uncertain = True
    lane.step(Budget(100.2, lambda: now[0]))
    assert lane.queue[0].uncertain and launched == []


def test_transport_uses_in_process_exchange(monkeypatch):
    import os
    import subprocess

    from trusted_router.storage_gcp_shadow_attachments import Lifetime
    pid = os.getpid()
    calls = []
    def exchange(self, *args):
        assert os.getpid() == pid
        calls.append(args)
        return 200, {}, b"exact"
    def forbidden(*args, **kwargs):
        raise AssertionError("attachment spawned a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Lifetime, "exchange", exchange)
    transport = GCSAttachments("offline")
    transport.authenticated = False
    assert transport.get("exact-key", 2.) == b"exact"
    assert len(calls) == 1


@pytest.mark.parametrize("size", [65536, 65537])
def test_native_body_reads_have_explicit_byte_cap(monkeypatch, size):
    from types import SimpleNamespace

    from trusted_router import storage_gcp_shadow_attachments as module
    remaining = [size]
    caps = []
    class Response:
        status = 200
        fp = None
        def getheaders(self): return []
        def close(self): pass
        def read1(self, cap):
            caps.append(cap)
            take = min(remaining[0], cap)
            remaining[0] -= take
            return b"x"*take
    class Connection:
        def __init__(self, *a, **kw): self.sock = SimpleNamespace(settimeout=lambda v: None, shutdown=lambda v: None)
        def connect(self): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    transport = GCSAttachments("offline")
    transport.authenticated = False
    if size == 65536:
        assert len(transport.get("exact-key", 2., clock=lambda: 0.)) == size
    else:
        with pytest.raises(ValueError, match="attachment_bytes"):
            transport.get("exact-key", 2., clock=lambda: 0.)
    assert all(0 < cap <= 8192 for cap in caps) and caps[-1] == 1


def test_credentials_and_object_share_admission_deadline(monkeypatch):
    import google.auth

    from trusted_router.storage_gcp_shadow_attachments import Lifetime
    now = [0.]
    observed = []
    class Credentials:
        def before_request(self, request, *args):
            observed.append(("refresh", request.lifetime.deadline))
            now[0] += .75
    def default(**kwargs):
        observed.append(("discovery", kwargs["request"].lifetime.deadline))
        now[0] += .75
        return Credentials(), "offline"
    def exchange(lifetime, *args):
        observed.append(("object", lifetime.deadline))
        assert lifetime.remaining() == .5
        now[0] += .6
        return 200, {}, b"late"
    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(Lifetime, "exchange", exchange)
    with pytest.raises(TimeoutError):
        GCSAttachments("offline").get("exact-key", 2., deadline=2., clock=lambda: now[0])
    assert observed == [("discovery", 2.), ("refresh", 2.), ("object", 2.)]


@pytest.mark.parametrize("stage", ["credential", "connect", "send", "read", "cleanup"])
def test_authenticated_native_cancellation(stage, monkeypatch):
    import http.client

    import google.auth

    from trusted_router.storage_gcp_shadow_attachments import Lifetime
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1")
            self.end_headers()
            try:
                self.wfile.write(b"x")
            except (BrokenPipeError, ConnectionResetError):
                pass
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        if "CI" in os.environ:
            pytest.fail("CI must permit authenticated native cancellation qualification")
        pytest.skip("sandbox forbids native loopback")
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    lifetime = []
    auth_calls = []
    cancelled = []
    def cancel():
        if not cancelled:
            cancelled.append(stage)
            lifetime[0].cancelled = True
    class Credentials:
        def before_request(self, request, method, url, headers):
            auth_calls.append("refresh")
            headers["Authorization"] = "Bearer offline-test-token"
    def credentials(**kwargs):
        auth_calls.append("discovery")
        lifetime.append(kwargs["request"].lifetime)
        if stage == "credential":
            cancel()
        return Credentials(), "offline"
    monkeypatch.setattr(google.auth, "default", credentials)
    target, method = {
        "connect": (http.client.HTTPConnection, "connect"),
        "send": (http.client.HTTPConnection, "request"),
        "read": (http.client.HTTPResponse, "read1"),
        "cleanup": (http.client.HTTPResponse, "close"),
        "credential": (Lifetime, "remaining"),
    }[stage]
    if stage != "credential":
        original = getattr(target, method)
        def boundary(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            cancel()
            return result
        monkeypatch.setattr(target, method, boundary)
    transport = GCSAttachments("offline")
    transport.endpoint = f"http://127.0.0.1:{server.server_port}/"
    try:
        worker.start()
        with pytest.raises(TimeoutError):
            transport.get("exact-key", 2., clock=lambda: 0.)
        assert cancelled == [stage]
        assert auth_calls == (["discovery"] if stage == "credential" else ["discovery", "refresh"])
        assert not transport.active.locked()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("lost", [False, True])
def test_attachment_auth_and_transport_inventory_per_export(monkeypatch, lost):
    import google.auth

    from trusted_router.storage_gcp_shadow_attachments import Lifetime
    now, calls, stored = [0.], [], []
    class Credentials:
        def before_request(self, request, method, url, headers):
            calls.append("refresh")
    def default(**kwargs):
        calls.append("discovery")
        return Credentials(), "offline"
    def exchange(self, method, url, body, headers):
        calls.append(method)
        if method == "POST":
            stored.append(body)
            if lost:
                raise TimeoutError("original attachment reply lost")
            return 200, {}, b""
        return 200, {}, stored[0]
    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(Lifetime, "exchange", exchange)
    lane = Lane("basis", "issuer", GCSAttachments("offline"))
    lane.append("2026-10-12", basis())
    size = lane.retained_bytes
    lane.step(Budget(2., lambda: now[0]))
    assert calls == ["discovery", "refresh", "POST"]
    if lost:
        assert lane.queue[0].uncertain
        now[0] = 5.
        lane.step(Budget(7., lambda: now[0]))
        assert calls == ["discovery", "refresh", "POST", "discovery", "refresh", "GET"]
    assert not lane.queue
    assert lane.operations == dict(put=1, get=int(lost), sent_bytes=size, received_bytes=size*int(lost))
