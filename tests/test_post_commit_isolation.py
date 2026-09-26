"""Regression for the refund race under the suite-wide inline fixture."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import anyio
from starlette.background import BackgroundTasks

from tests.test_gateway_fallback_billing import (
    test_gateway_refund_records_provider_benchmark_without_generation as refund_assertion_path,
)
from trusted_router import post_commit
from trusted_router.storage import STORE, InMemoryStore, configure_store

# Capture production's pool at collection time, before autouse fixtures replace
# it. Removing the suite fixture routes the refund into these blocked workers.
_REAL_EXECUTOR = post_commit.POST_COMMIT


def test_refund_assertions_and_store_teardown_with_real_workers_occupied() -> None:
    entered = threading.Barrier(post_commit.WORKERS + 1)
    release = threading.Event()

    def blocked() -> None:
        entered.wait(timeout=5)
        # Only the teardown below may release these workers. A timeout here
        # could let the missing-fixture mutation pass on a slow test runner.
        release.wait()

    replacement = InMemoryStore()
    try:
        for _ in range(post_commit.WORKERS):
            _REAL_EXECUTOR.submit(blocked)
        entered.wait(timeout=5)
        # Call the existing test unchanged, including its immediate row check.
        refund_assertion_path()
        original = STORE.target
        assert post_commit.POST_COMMIT.in_flight == 0
    finally:
        # Simulate the next test's store before letting real queued work run.
        configure_store(replacement)
        release.set()
        assert _REAL_EXECUTOR.wait_idle()
        assert replacement.provider_benchmark_samples() == []
    assert len(original.provider_benchmark_samples()) == 1


def test_inline_executor_keeps_bound_drops_and_does_not_borrow_anyio(
    optional_executor: Any,
) -> None:
    caller_thread = threading.get_ident()
    ran: list[int] = []

    def nested() -> None:
        assert threading.get_ident() == caller_thread
        ran.append(optional_executor.in_flight)
        optional_executor.submit(nested)

    async def drive() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.borrowed_tokens == 0
        tasks = BackgroundTasks()
        post_commit.defer_post_commit(tasks, nested)
        await tasks()
        assert limiter.borrowed_tokens == 0

    asyncio.run(drive())
    assert ran == list(range(1, post_commit.MAX_IN_FLIGHT + 1))
    assert optional_executor.in_flight == 0
    assert optional_executor.drops == {"nested": 1}
    optional_executor.close()
    optional_executor.submit(nested)
    assert optional_executor.drops == {"nested": 2}
    assert len(ran) == post_commit.MAX_IN_FLIGHT
