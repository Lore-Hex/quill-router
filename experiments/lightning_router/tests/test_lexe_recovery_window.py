import logging

import httpx
import pytest
from lightning_router.lexe import Lexe, LexeHTTPError


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("lightning_router.lexe.time.monotonic", lambda: now[0])
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr("lightning_router.lexe.secrets.SystemRandom.uniform", lambda self, low, high: low)
    return now


@pytest.mark.parametrize("path", ["client_info", "node_info", "payment", "updated_payments"])
@pytest.mark.parametrize("outage_seconds", [2, 5])
def test_multi_second_read_outage_recovers_without_replaying_writes(clock, path, outage_seconds, caplog):
    calls = []

    def handle(request):
        calls.append(request)
        if clock[0] < 100 + outage_seconds:
            return httpx.Response(500, json={"code": 100, "msg": "private upstream detail"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        assert Lexe(client, "a" * 64).request("GET", "/v2/node/" + path) == {"ok": True}
    assert 100 + outage_seconds <= clock[0] <= 115
    assert len(calls) <= 5
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "lightning.lexe_read_recovered" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "private upstream detail" not in caplog.text


def test_slow_failures_cannot_start_retries_past_recovery_budget(clock, caplog):
    calls = []

    def handle(request):
        calls.append((clock[0], request.extensions["timeout"]["read"]))
        clock[0] += min(5, request.extensions["timeout"]["read"])
        return httpx.Response(503, json={"code": 100})

    with httpx.Client(base_url="http://127.0.0.1:5393", timeout=20,
                      transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        with pytest.raises(LexeHTTPError):
            lexe.ready()
        assert lexe._checked == 0
    assert len(calls) == 4
    assert calls[-1][1] == 1.5
    assert clock[0] == 120
    assert "lightning.lexe_http_failed" in caplog.text
    assert "elapsed_ms=20000" in caplog.text


def test_scheduler_delay_exhausts_budget_without_another_request(clock, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: clock.__setitem__(0, clock[0] + 16))

    def handle(request):
        calls.append(request)
        return httpx.Response(500, json={"code": 100})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(LexeHTTPError):
            Lexe(client, "a" * 64).ready()
    assert len(calls) == 1
    assert "elapsed_ms=16000" in caplog.text
