"""Real HTTP deadlines: response progress must never renew a lane budget."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

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
        pytest.skip("sandbox forbids binding a local HTTP server; requires unrestricted CI")
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    server_thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/"
    transport = object.__new__(GCSAttachments)
    transport.bucket = "offline"
    transport.endpoint = endpoint
    transport.authenticated = False
    # Compatibility setup also exercises the pre-fix Requests implementation.
    class Session(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs.pop("max_allowed_time", None)
            return super().request(method, endpoint, **kwargs)
    transport.credentials = None
    transport.session_type = lambda *a, **kw: Session()
    lane = Lane("basis", "issuer", transport)
    lane.append("2026-10-12", basis())
    item = lane.queue[0]
    item.uncertain = True
    payload[0] = item.body
    start = time.monotonic()
    try:
        lane.step(Budget(start+2.))
        elapsed = time.monotonic()-start
        # Generous scheduling/cleanup bound. Tight latency is a measurement,
        # never a claim that a shared runner schedules within milliseconds.
        assert elapsed < 10
        assert lane.queue == [item] and item.uncertain
        assert lane.operations == dict(put=0, get=1, sent_bytes=0, received_bytes=0)
        (tmp_path/"deadline.json").write_text(json.dumps(dict(fault=fault, budget=2.,
            elapsed=elapsed, server_reached=entered.is_set())))
        if not entered.is_set():
            pytest.skip("worker startup exhausted budget before HTTP fault; no stream proof")
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join()


def test_trickling_stream_cannot_renew_absolute_deadline(monkeypatch):
    """A byte every .1 s defeats a .2 s per-read timeout without a watchdog."""
    import subprocess
    from types import SimpleNamespace

    from trusted_router import storage_gcp_shadow_attachments as module
    now = [100.]
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            for _ in range(10):
                now[0] += .1
                yield b"x"
    class Session(Response):
        def request(self, *a, **kw): return Response()
    killed = []
    class Process:
        returncode = 0
        def communicate(self, data=None, timeout=None):
            if not killed:
                assert 0 < timeout <= .2
                now[0] += timeout
                raise subprocess.TimeoutExpired("offline", timeout)
            return b"", None
        def kill(self): killed.append(True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Process())
    # The current implementation ignores this clock during streaming.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    transport = object.__new__(GCSAttachments)
    transport.bucket = "offline"
    transport.credentials = None
    transport.session_type = lambda *a, **kw: Session()
    with pytest.raises(TimeoutError):
        transport.get("exact-key", .2)
    assert killed == [True]


@pytest.mark.parametrize("fault", ["transport", "503"])
def test_auth_failure_is_not_retried(monkeypatch, fault):
    import io
    from types import SimpleNamespace

    import google.auth

    from trusted_router import storage_gcp_shadow_attachments as module
    calls = []
    def request(self, *a, **kw):
        calls.append((a, kw))
        if fault == "transport":
            raise requests.Timeout("offline")
        return SimpleNamespace(status_code=503, close=lambda: None)
    monkeypatch.setattr(requests.Session, "request", request)
    def default(**kwargs):
        kwargs["request"](url="http://offline.invalid", method="GET")
        raise AssertionError("failed authentication returned")
    monkeypatch.setattr(google.auth, "default", default)
    value = dict(bucket="offline", endpoint="http://offline.invalid/", authenticated=True, timeout=.2)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(value).encode())))
    with pytest.raises(RuntimeError, match="attachment_auth"):
        module.worker()
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False


def test_lane_passes_absolute_deadline_before_transport_start(monkeypatch):
    from types import SimpleNamespace

    from trusted_router import storage_gcp_shadow_attachments as module
    now = [100.]
    def clock():
        return now[0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock))
    launched = []
    class Process:
        returncode = 1
        def communicate(self, *a, **kw): return b"null", None
        def poll(self): return 1
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: launched.append(True) or Process())
    class Delayed(GCSAttachments):
        def get(self, key, timeout, **kwargs):
            now[0] += 1  # Descheduled after lane admission, before transport entry.
            return super().get(key, timeout, **kwargs)
    lane = Lane("basis", "issuer", Delayed("offline"))
    lane.append("2026-10-12", basis())
    lane.queue[0].uncertain = True
    lane.step(Budget(100.2, clock))
    assert lane.queue[0].uncertain
    assert launched == []
