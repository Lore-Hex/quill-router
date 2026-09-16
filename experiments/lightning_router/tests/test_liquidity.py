import json

import httpx
import pytest
from lightning_router.lnd import Lnd


def client(channels, *, synced=True):
    def handle(request):
        assert request.method == "GET"
        if request.url.path == "/v1/getinfo":
            return httpx.Response(200, json={"synced_to_chain": synced, "synced_to_graph": synced,
                "chains": [{"chain": "bitcoin", "network": "mainnet"}]})
        assert request.url.path == "/v1/channels"
        return httpx.Response(200, json={"channels": channels})
    return Lnd(httpx.Client(base_url="https://node.test", transport=httpx.MockTransport(handle)))


def channel(balance="699567", active=True, pending=()):
    return {"active": active, "remote_balance": balance, "remote_pubkey": "private-peer",
            "chan_id": "private-id", "pending_htlcs": list(pending),
            "remote_constraints": {"chan_reserve_sat": "7500", "max_pending_amt_msat": "742500000"}}


def test_liquidity_is_metadata_only_and_does_not_sum_payment_capacity():
    node = client([channel(), channel("107500"), channel(active=False)])
    report = node.liquidity()
    assert report == {"synced": 1, "active_channels": 2,
                      "inbound_msat": 792067000, "receiving_capacity_msat": 692067000}
    assert "private-" not in json.dumps(report)
    assert node.receiving_capacity() == 692067000


@pytest.mark.parametrize("channels,synced", [([], True), ([channel()], False), ([channel(active=False)], True)])
def test_liquidity_unavailable_is_zero(channels, synced):
    report = client(channels, synced=synced).liquidity()
    assert report["inbound_msat"] == report["receiving_capacity_msat"] == 0


def test_liquidity_reserves_pending_and_inflight_limits():
    entry = channel("900000", pending=[{"amount": "10000", "hash_lock": "secret"}])
    report = client([entry]).liquidity()
    assert report["inbound_msat"] == 882500000
    assert report["receiving_capacity_msat"] == 732500000
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("balance,low", [("307499", True), ("307500", False), ("699567", False)])
def test_liquidity_warning_threshold_and_no_secrets(funding, caplog, balance, low):
    funding.lnd = client([channel(balance)])
    funding.log_liquidity()
    assert ("lightning.liquidity_low" in caplog.text) is low
    report = next(json.loads(r.message) for r in caplog.records if r.message.startswith("{"))
    assert report["event"] == "lightning.liquidity_health"
    assert report["severity"] == ("WARNING" if low else "INFO")
    assert "private-" not in caplog.text


def test_liquidity_failure_never_blocks_settlement_or_logs_exception(funding, caplog, monkeypatch):
    def fail():
        raise RuntimeError("secret macaroon and customer invoice")
    monkeypatch.setattr(funding.lnd, "liquidity", fail, raising=False)
    funding.check_capacity = True
    funding._last_health_log = -1000
    assert funding.reconcile() == {"checked": 0, "failed": 0}
    assert "lightning.liquidity_check_failed error_type=RuntimeError" in caplog.text
    assert "secret macaroon" not in caplog.text


def test_liquidity_monitor_runs_once_per_minute_not_every_reconcile(funding, monkeypatch):
    from unittest.mock import Mock
    check = Mock()
    monkeypatch.setattr(funding, "log_liquidity", check)
    clock = [100.0]
    monkeypatch.setattr("lightning_router.service.time.monotonic", lambda: clock[0])
    funding.check_capacity = True
    funding.reconcile()
    clock[0] = 159.0
    funding.reconcile()
    assert check.call_count == 1
    clock[0] = 160.0
    funding.reconcile()
    assert check.call_count == 2
