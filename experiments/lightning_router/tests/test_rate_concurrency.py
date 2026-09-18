import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
from lightning_router.errors import QuoteUnavailable
from lightning_router.rates import RATE_REFRESH_MAX_WAITERS, Rate, Rates


class ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if blocking:
            self.waiting.set()
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()


def delayed_rates(*, fail=False):
    started, finish = threading.Event(), threading.Event()
    calls = []

    def fetch(_):
        calls.append(1)
        started.set()
        assert finish.wait(5)
        return httpx.Response(503 if fail else 200, json={
            "data": {"base": "BTC", "currency": "USD", "amount": "100000"}})

    rates = Rates(httpx.Client(transport=httpx.MockTransport(fetch)))
    rates._lock = ObservedLock()
    return rates, started, finish, calls


@pytest.mark.parametrize("stale", [False, True])
def test_concurrent_quote_requests_share_one_fresh_result(stale):
    rates, started, finish, calls = delayed_rates()
    if stale:
        rates._rate = Rate.from_spot(Decimal("70000"), int(time.time()) - 61)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(rates.current)
        assert started.wait(5)
        follower = pool.submit(rates.current)
        try:
            assert rates._lock.waiting.wait(1), "Concurrent request failed instead of joining refresh"
        finally:
            finish.set()
        result = leader.result(timeout=5)
        assert follower.result(timeout=5) is result
    assert result.usd_per_btc == Decimal("90000")
    assert len(calls) == 1


def test_waiting_quote_does_not_reuse_stale_rate_after_fetch_failure():
    rates, started, finish, calls = delayed_rates(fail=True)
    rates._rate = Rate.from_spot(Decimal("70000"), int(time.time()) - 61)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(rates.current)
        assert started.wait(5)
        follower = pool.submit(rates.current)
        try:
            assert rates._lock.waiting.wait(1)
        finally:
            finish.set()
        with pytest.raises(httpx.HTTPStatusError):
            leader.result(timeout=5)
        with pytest.raises(QuoteUnavailable):
            follower.result(timeout=5)
    assert len(calls) == 1


def test_quote_refresh_admission_is_bounded_and_recovers():
    rates, started, finish, calls = delayed_rates()
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(rates.current)
        assert started.wait(5)
        try:
            for _ in range(RATE_REFRESH_MAX_WAITERS):
                assert rates._waiters.acquire(blocking=False)
            try:
                with pytest.raises(QuoteUnavailable, match="busy"):
                    rates.current()
                assert not rates._lock.waiting.is_set()
            finally:
                for _ in range(RATE_REFRESH_MAX_WAITERS):
                    rates._waiters.release()
        finally:
            finish.set()
        result = leader.result(timeout=5)
    assert rates.current() is result
    assert len(calls) == 1


def test_timed_out_waiter_releases_admission_slot(monkeypatch):
    monkeypatch.setattr("lightning_router.rates.RATE_REFRESH_WAIT_SECONDS", 0.01)
    rates, started, finish, _ = delayed_rates()
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(rates.current)
        assert started.wait(5)
        try:
            for _ in range(RATE_REFRESH_MAX_WAITERS + 1):
                with pytest.raises(QuoteUnavailable, match="timed out"):
                    rates.current()
        finally:
            finish.set()
        assert rates.current() is leader.result(timeout=5)


def test_concurrent_funding_quotes_keep_frozen_rates_and_credit_once(funding):
    rates, started, finish, calls = delayed_rates()
    funding.rates = rates
    keys = ["sk-tr-v1-" + secrets.token_urlsafe(32) for _ in range(2)]
    requests = [uuid.uuid4().hex for _ in keys]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(funding.create, keys[0], requests[0], 100, new=True)
        assert started.wait(5)
        second = pool.submit(funding.create, keys[1], requests[1], 100, new=True)
        try:
            assert rates._lock.waiting.wait(1)
        finally:
            finish.set()
        views = [first.result(timeout=5), second.result(timeout=5)]
    assert len(calls) == 1
    assert funding.credits.balances == {}
    # A later market move must not revalue either previously issued invoice.
    rates._rate = Rate.from_spot(Decimal("200000"), int(time.time()))
    for key, request_id, view in zip(keys, requests, views, strict=True):
        row = funding.store.invoice(view["id"], funding.credentials.fingerprint(key))
        assert Decimal(row["usd_per_btc"]) == Decimal("90000")
        funding.lnd.pay(row["payment_hash"])
        result = funding.refresh(row)
        assert result["credited"]
        assert int(result["balance_microdollars"]) == 1_000_800
        assert funding.create(key, request_id, 100, new=True)["id"] == view["id"]
        assert funding.refresh(row)["balance_microdollars"] == result["balance_microdollars"]
    assert len(funding.credits.payments) == 2
