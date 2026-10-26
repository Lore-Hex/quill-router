"""Observer reads finish on the server; lateness is a client-side install verdict.

2026-10-23: `ExecuteStreamingSql` DEADLINE_EXCEEDED/CANCELLED at 1.1-1.4/min
tripped the Spanner API-failure alert. The server-side work averaged 31-53 ms.
The failures were the shadow observer's tight client deadlines: 250 ms for the
1 s fleet-health read and 500 ms for the 4 s workspace admission read. These
reads now carry a generous RPC deadline and consume the whole stream. The same
250/500 ms limits remain install cut-offs, so a slow reply is still rejected
as `late_installs`, but it is never cancelled at Spanner.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import fake_store, health, logical_time, service
from trusted_router import storage_gcp_shadow_observation as observation
from trusted_router.storage_gcp_shadow_observation import OBSERVER_READ_RPC_SECONDS, Budget


class Clock:
    def __init__(self) -> None:
        self.now = 100.

    def __call__(self) -> float:
        return self.now


def slow_store(delay: float) -> tuple[Any, Clock, list[float], list[int]]:
    """A fake store whose health snapshot takes ``delay`` seconds and streams rows."""
    store, db = fake_store()
    budget = Budget(logical_time()+.5, logical_time)
    store.claim("publisher", health()["slot"], 0, budget)
    store.publish(health(), budget)
    clock = Clock()
    store.clock = clock
    timeouts: list[float] = []
    consumed: list[int] = []
    original = db.snapshot

    class Slow:
        def __init__(self, reader: Any) -> None:
            self.reader = reader

        def execute_sql(self, sql: str, **options: Any) -> Any:
            timeouts.append(options["timeout"])
            rows = self.reader.execute_sql(sql, **options)
            clock.now += delay
            class Stream(list):
                metadata = rows.metadata
                def __iter__(self):
                    for row in super().__iter__():
                        consumed.append(1)
                        yield row
            return Stream(rows)

    @contextmanager
    def snapshot(**kwargs: Any) -> Any:
        with original(**kwargs) as reader:
            yield Slow(reader)
    db.snapshot = snapshot
    return store, clock, timeouts, consumed


def test_slow_health_read_completes_and_is_a_late_install():
    store, clock, timeouts, consumed = slow_store(.4)
    started = clock()
    value = store.read_shadow_health()
    # The RPC asked for the generous deadline and read the whole stream.
    assert timeouts == [pytest.approx(OBSERVER_READ_RPC_SECONDS)]
    assert OBSERVER_READ_RPC_SECONDS >= 2
    assert consumed == [1]
    assert value is not None and value["read_elapsed_ns"] == 400_000_000
    # The client still refuses to install it: a late install, not a read failure.
    svc = service()
    svc.observer.clock = clock
    before = dict(svc.observer.counts)
    svc.observer.install_health(started, value)
    assert svc.observer.counts["late_installs"] == before.get("late_installs", 0) + 1
    assert svc.observer.counts.get("read_failures", 0) == before.get("read_failures", 0)
    assert svc.observer.health is None


def test_fast_health_read_installs():
    store, clock, timeouts, _ = slow_store(.04)
    started = clock()
    value = store.read_shadow_health()
    svc = service()
    svc.observer.clock = clock
    svc.observer.install_health(started, value)
    assert svc.observer.health is not None and svc.observer.health["body"] == health()


def test_positive_control_tight_rpc_deadline_fails_the_same_read(monkeypatch):
    # The pre-fix shape: a 250 ms RPC budget turns the same slow read into an
    # error, which on Spanner is a client DEADLINE_EXCEEDED/CANCELLED.
    monkeypatch.setattr(observation, "OBSERVER_READ_RPC_SECONDS", .25)
    store, _, timeouts, _ = slow_store(.4)
    with pytest.raises(TimeoutError, match="shadow_budget"):
        store.read_shadow_health()
    assert timeouts == [pytest.approx(.25)]


def test_workspace_admission_read_uses_the_generous_deadline(monkeypatch):
    from trusted_router import storage_gcp_async_admission as admission
    seen: list[float] = []

    class Snapshot:
        def __enter__(self) -> Snapshot:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute_sql(self, sql: str, **options: Any) -> Any:
            seen.append(options["timeout"])
            return iter([(0, None, 1)])

    class Database:
        def snapshot(self) -> Snapshot:
            return Snapshot()

    svc = service()
    read = svc.observer.read
    assert read.keywords == {"deadline_seconds": OBSERVER_READ_RPC_SECONDS}
    assert read.func is admission.read_admission
    result = admission.read_admission(Database(), "ws", deadline_seconds=OBSERVER_READ_RPC_SECONDS)
    assert result.pending_micro == 0 and seen == [pytest.approx(OBSERVER_READ_RPC_SECONDS, abs=.01)]
    # Money admission keeps its 500 ms RPC deadline.
    admission.read_admission(Database(), "ws")
    assert seen[-1] == pytest.approx(admission.READ_DEADLINE_SECONDS, abs=.01)


def test_slow_workspace_reply_is_a_late_install():
    from trusted_router.services.async_settle import Admission
    svc = service()
    clock = Clock()
    svc.observer.clock = clock
    started = clock()
    clock.now += .6  # Beyond the 500 ms install cut-off, inside the RPC deadline.
    svc.observer.install_workspace("ws", started, Admission(0, 1))
    assert svc.observer.counts["late_installs"] == 1
    assert "ws" not in svc.observer.entries
