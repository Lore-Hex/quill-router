from threading import BoundedSemaphore

import pytest

from trusted_router.schemas import CreateKeyRequest, PatchKeyRequest
from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission
from trusted_router.storage_gcp_counters import key_usage_shard_count
from trusted_router.strict_budget import StrictBudgetBusy, strict_budget_slot


@pytest.fixture(autouse=True)
def isolated_waiters(monkeypatch):
    from trusted_router import strict_budget

    monkeypatch.setattr(strict_budget, "STRICT_WAITERS", KeyedConcurrencyAdmission(max_subjects=16))
    monkeypatch.setattr(strict_budget, "STRICT_WAITER_CAPACITY", BoundedSemaphore(16))


def assert_waiter_capacity_released(strict_budget):
    capacity = strict_budget.STRICT_WAITER_CAPACITY
    for _ in range(16):
        assert capacity.acquire(blocking=False)
    assert not capacity.acquire(blocking=False)
    for _ in range(16):
        capacity.release()


def test_strict_mode_is_immutable_and_single_shard():
    assert CreateKeyRequest(name="default").budget_strict is False
    assert CreateKeyRequest(name="strict", budget_strict=True).budget_strict is True
    with pytest.raises(ValueError):
        PatchKeyRequest(budget_strict=True)
    with pytest.raises(ValueError):
        PatchKeyRequest(budget_strict=False)
    with pytest.raises(ValueError, match="one key counter"):
        key_usage_shard_count({"budget_strict": True, "usage_shard_count": 16})
    assert key_usage_shard_count({"budget_strict": True}) == 1


def test_strict_admission_exhaustion_releases_capacity(monkeypatch):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    with pytest.raises(RuntimeError):
        with strict_budget_slot("a"), strict_budget_slot("b"):
            with pytest.raises(StrictBudgetBusy):
                with strict_budget_slot("a"):
                    pytest.fail("a concurrent strict request was admitted")
            with pytest.raises(StrictBudgetBusy):
                with strict_budget_slot("c"):
                    pytest.fail("global strict capacity was exceeded")
            raise RuntimeError("database unavailable")
    assert limiter.count("a") == limiter.count("b") == 0
    with strict_budget_slot("c"):
        assert limiter.count("c") == 1


def test_strict_admission_recovers_brief_collision_before_entering_transaction(monkeypatch):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    assert limiter.try_acquire("hot", limit=1)

    class Clock:
        now = 10.0
        waits = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            assert limiter.count("hot") == 1
            self.waits.append(seconds)
            self.now += seconds
            limiter.release("hot")

    clock = Clock()
    monkeypatch.setattr(strict_budget, "time", clock)
    with strict_budget_slot("hot") as deadline:
        assert limiter.count("hot") == 1
        assert deadline == 15.0  # Admission must not renew the transaction budget.
        assert strict_budget.STRICT_WAITERS.count("hot") == 0
        assert_waiter_capacity_released(strict_budget)
    assert len(clock.waits) == 1
    assert limiter.count("hot") == 0


def test_strict_admission_does_not_repeat_a_failed_transaction(monkeypatch):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    transactions = 0
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        with strict_budget_slot("hot"):
            transactions += 1
            raise RuntimeError("commit outcome unknown")
    assert transactions == 1
    assert limiter.count("hot") == 0


@pytest.mark.parametrize("budget_seconds", [0.01, 5.0])
def test_strict_admission_deadline_cannot_be_extended_or_enter_after_expiry(monkeypatch, budget_seconds):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    monkeypatch.setattr(strict_budget, "STRICT_BUDGET_SECONDS", budget_seconds)
    assert limiter.try_acquire("hot", limit=1)
    wait_seconds = min(budget_seconds, strict_budget.STRICT_ADMISSION_WAIT_SECONDS)

    class Clock:
        now = 0.0
        attempts = 0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.attempts += 1
            self.now += seconds
            if self.now >= wait_seconds:
                limiter.release("hot")

    clock = Clock()
    monkeypatch.setattr(strict_budget, "time", clock)
    with pytest.raises(StrictBudgetBusy):
        with strict_budget_slot("hot"):
            pytest.fail("transaction started after the admission deadline")
    assert clock.now == wait_seconds
    assert 1 <= clock.attempts <= 11
    assert strict_budget.STRICT_WAITERS.count("hot") == 0
    assert_waiter_capacity_released(strict_budget)
    with strict_budget_slot("hot"):
        assert limiter.count("hot") == 1


@pytest.mark.parametrize("bound", ["per_key", "global"])
def test_strict_waiter_limits_fail_fast_without_releasing_other_callers(monkeypatch, bound):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    assert limiter.try_acquire("hot", limit=1)
    if bound == "per_key":
        for _ in range(2):
            assert strict_budget.STRICT_WAITERS.try_acquire("hot", limit=2)
    else:
        for _ in range(16):
            assert strict_budget.STRICT_WAITER_CAPACITY.acquire(blocking=False)

    class Clock:
        def monotonic(self):
            return 0.0

        def sleep(self, seconds):
            pytest.fail("saturated waiters must reject immediately")

    monkeypatch.setattr(strict_budget, "time", Clock())
    with pytest.raises(StrictBudgetBusy):
        with strict_budget_slot("hot"):
            pytest.fail("saturated admission started a transaction")
    assert limiter.count("hot") == 1
    # Free keys must not queue behind another key's waiters.
    with strict_budget_slot("other"):
        assert limiter.count("other") == 1
    if bound == "per_key":
        assert strict_budget.STRICT_WAITERS.count("hot") == 2
        assert_waiter_capacity_released(strict_budget)
    else:
        assert not strict_budget.STRICT_WAITER_CAPACITY.acquire(blocking=False)
        assert strict_budget.STRICT_WAITERS.count("hot") == 0


def test_strict_waiter_exception_releases_only_its_own_capacity(monkeypatch):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    assert limiter.try_acquire("hot", limit=1)

    class Clock:
        def monotonic(self):
            return 0.0

        def sleep(self, seconds):
            raise RuntimeError("interrupted")

    monkeypatch.setattr(strict_budget, "time", Clock())
    with pytest.raises(RuntimeError, match="interrupted"):
        with strict_budget_slot("hot"):
            pytest.fail("interrupted admission must not enter the transaction")
    assert limiter.count("hot") == 1
    assert strict_budget.STRICT_WAITERS.count("hot") == 0
    assert_waiter_capacity_released(strict_budget)


def test_strict_waiting_thread_leaves_other_keys_available(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    waiting = Event()
    proceed = Event()

    class Clock:
        monotonic = staticmethod(time.monotonic)

        def sleep(self, seconds):
            waiting.set()
            assert proceed.wait(timeout=2)

    monkeypatch.setattr(strict_budget, "time", Clock())
    # The events control scheduling, not a real-time deadline in a loaded CI host.
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION_WAIT_SECONDS", 5.0)

    def contender():
        with strict_budget_slot("hot"):
            assert limiter.count("hot") == 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with strict_budget_slot("hot"):
                future = executor.submit(contender)
                assert waiting.wait(timeout=2)
                with strict_budget_slot("other"):
                    assert limiter.count("other") == 1
                assert not future.done()
        finally:
            proceed.set()
        future.result(timeout=2)
    assert limiter.count("hot") == limiter.count("other") == 0
    assert_waiter_capacity_released(strict_budget)
