import logging
import re

import httpx
import pytest
from lightning_router.lexe import Lexe


@pytest.mark.parametrize("path", ["client_info", "node_info", "payment", "updated_payments"])
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_two_transient_http_failures_recover_with_bounded_jitter(path, status, monkeypatch, caplog):
    calls, sleeps = [], []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", sleeps.append)

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"code": 123, "msg": "private-error-body"}) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://127.0.0.1:5393", timeout=20, transport=httpx.MockTransport(handle)) as client:
        assert Lexe(client, "a" * 64).request("GET", "/v2/node/" + path, params={"index": "private-index"}) == {"ok": True}
    assert len(calls) == 3
    assert len(sleeps) == 2 and .5 <= sleeps[0] <= 1 and 1 <= sleeps[1] <= 2
    assert len({str(call.url) for call in calls}) == 1
    trace_ids = {call.headers["lexe-trace-id"] for call in calls}
    assert len(trace_ids) == 1 and re.fullmatch(r"[A-Za-z0-9]{16}", trace_ids.pop())
    assert calls[0].extensions["timeout"]["read"] == 20
    assert all(call.extensions["timeout"]["read"] <= 5 for call in calls[1:])
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "lightning.lexe_read_recovered" in caplog.text
    assert "private-error-body" not in caplog.text and "private-index" not in caplog.text


def test_persistent_http_failure_keeps_only_safe_diagnostics(monkeypatch, caplog):
    calls, sleeps = [], []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", sleeps.append)

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"code": 123, "msg": "private-message", "sensitive": True,
                                        "data": {"preimage": "private-preimage"}})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        with pytest.raises(RuntimeError):
            lexe.ready()
        assert lexe._checked == 0
    assert len(calls) == 5 and len(sleeps) == 4
    failures = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(failures) == 1
    for value in ("lightning.lexe_http_failed", "method=GET", "path=/v2/node/client_info", "http_status=503", "lexe_code=123",
                  "trace_id=" + calls[0].headers["lexe-trace-id"], "attempts=5"):
        assert value in failures[0]
    assert "private-message" not in caplog.text and "private-preimage" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 501, 301])
def test_other_http_failures_do_not_retry(status, monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", sleeps.append)

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"code": 123, "msg": "private"})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).ready()
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("method,path", [("POST", "/v2/node/create_invoice"), ("POST", "/v2/node/cancel_payment"),
                                        ("GET", "/unexpected")])
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_no_mutation_or_unknown_endpoint_replay(method, path, status, monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", sleeps.append)

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"code": 123})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).request(method, path)
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("code,expected", [(0, "0"), (65535, "65535"), (-1, "unknown"), (65536, "unknown"),
                                          (True, "unknown"), (1.5, "unknown"), ("private-token", "unknown"), (None, "unknown")])
def test_error_code_is_only_a_bounded_integer(code, expected, caplog):
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(403, json={"code": code, "msg": "private-message"}))) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).request("GET", "/v2/node/client_info")
    assert f"lexe_code={expected}" in caplog.text
    assert "private-token" not in caplog.text and "private-message" not in caplog.text


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"code": 123, "msg":"' + b"x" * 4096 + b'"}',
                                 b"[" * 1500 + b"]" * 1500])
def test_malformed_or_oversized_error_body_preserves_original_status(body, caplog):
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(403, content=body, headers={"content-type": "application/json"}))) as client:
        with pytest.raises(RuntimeError, match="Lexe request unavailable"):
            Lexe(client, "a" * 64).ready()
    assert "http_status=403" in caplog.text and "lexe_code=unknown" in caplog.text


def test_stream_diagnostics_are_bounded_and_close_response(monkeypatch, caplog):
    clock = [0]
    monkeypatch.setattr("lightning_router.lexe.time.monotonic", lambda: clock[0])

    class SlowBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"code":'
            clock[0] += 2
            yield b"123"
            raise AssertionError("Diagnostic parsing must stop after its time budget")

        def close(self):
            self.closed = True

    body = SlowBody()
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(403, stream=body, headers={"content-type": "application/json"}))) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).ready()
    assert body.closed and "lexe_code=unknown" in caplog.text


@pytest.mark.parametrize("status,attempts", [(401, 1), (503, 5)])
def test_unreadable_diagnostics_do_not_change_status_retry_policy(status, attempts, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("private-error-body")
            yield b""  # Make this a stream without emitting diagnostic content.

    def handle(request):
        calls.append(request)
        return httpx.Response(status, stream=BrokenBody(), headers={"content-type": "application/json"})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).ready()
    assert len(calls) == attempts
    assert f"http_status={status}" in caplog.text and "lexe_code=unknown" in caplog.text
    assert "private-error-body" not in caplog.text


def test_compressed_error_body_is_not_read(caplog):
    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Compressed error bodies must not be decoded")

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(403, stream=UnreadBody(), headers={"content-type": "application/json", "content-encoding": "gzip"}))) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).ready()
    assert "lexe_code=unknown" in caplog.text


def test_mixed_http_and_transport_failures_share_one_attempt_budget(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", sleeps.append)

    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout("private")
        return httpx.Response(503, json={"code": 123})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError):
            Lexe(client, "a" * 64).ready()
    assert len(calls) == 5 and len(sleeps) == 4


def test_transient_response_followed_by_invalid_authority_stays_closed(monkeypatch):
    calls = []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"code": 123}) if len(calls) == 1 else httpx.Response(200, json={"kind": "root"})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        with pytest.raises(ValueError, match="Receive-only Lexe authority required"):
            lexe.ready()
        assert lexe._checked == 0
    assert len(calls) == 2


def test_local_trace_overrides_untrusted_header_and_changes_for_each_request(caplog):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, json={"code": 1})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                lexe.request("GET", "/v2/node/client_info", headers={"Lexe-Trace-Id": "private-token"})
    traces = {request.headers["lexe-trace-id"] for request in calls}
    assert len(traces) == 2 and all(re.fullmatch(r"[A-Za-z0-9]{16}", trace) for trace in traces)
    assert "private-token" not in caplog.text
