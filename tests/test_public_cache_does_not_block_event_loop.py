"""A slow public-page render must not stall the worker's event loop.

`_cached_public_response`'s `build` reaches ClickHouse through a SYNCHRONOUS
httpx client with a 20-second timeout (`OperationalAnalytics._query`). It used
to be called inline from `async def` route handlers, so one cold /status render
against a slow ClickHouse blocked the entire event loop: with
containerConcurrency=8 every other in-flight request on that instance queued
behind it, including /internal/gateway/authorize and /settle on the paid
inference path.

Observed in production 2026-09-01 14:41-14:42Z: a burst of 503s, all from a
single instanceId, with authorize latencies of exactly 20.017s -- the
`timeout_seconds: float = 20.0` default -- and victims queued behind at 43-78s,
with NO traffic spike (51-146 req/min, flat).

The test asserts the property (the loop keeps turning during a slow build),
not the call shape, so it still fails if someone reintroduces the inline call
by a different route.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.background import BackgroundTasks

from trusted_router.config import Settings
from trusted_router.routes import public as public_routes

BUILD_SECONDS = 0.5


@pytest.mark.anyio
async def test_slow_build_does_not_block_the_event_loop() -> None:
    settings = Settings(environment="test")

    def slow_build() -> bytes:
        # Stands in for the blocking ClickHouse round trip.
        time.sleep(BUILD_SECONDS)
        return b"payload"

    ticks = 0
    stop = False

    async def heartbeat() -> None:
        # A co-running task the event loop must keep servicing. It ticks until
        # told to stop, and the assertion reads the count taken the moment the
        # build returned -- NOT after awaiting this task, which would let the
        # ticks accrue afterwards and make the test unable to fail.
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    # Let the heartbeat actually start before the build begins.
    await asyncio.sleep(0.02)
    ticks_before = ticks

    response = await public_routes._cached_public_response(
        settings,
        key="test:evloop",
        media_type="text/plain",
        ttl_seconds=1,
        stale_seconds=1,
        background_tasks=BackgroundTasks(),
        build=slow_build,
    )
    ticks_during_build = ticks - ticks_before

    stop = True
    await beat

    assert response.status_code == 200
    # A blocked loop yields ~0 ticks while the build runs; a healthy one
    # yields tens (BUILD_SECONDS / 0.01).
    assert ticks_during_build > 10, (
        f"event loop was starved during build (ticks={ticks_during_build})"
    )


@pytest.mark.anyio
async def test_concurrent_slow_builds_overlap_instead_of_serializing() -> None:
    """Two cold renders must run concurrently, not one-after-the-other.

    Serialization is what turned one slow ClickHouse query into a
    whole-instance stall.
    """
    settings = Settings(environment="test")

    def slow_build() -> bytes:
        time.sleep(BUILD_SECONDS)
        return b"payload"

    async def one(key: str) -> None:
        await public_routes._cached_public_response(
            settings,
            key=key,
            media_type="text/plain",
            ttl_seconds=1,
            stale_seconds=1,
            background_tasks=BackgroundTasks(),
            build=slow_build,
        )

    started = time.monotonic()
    await asyncio.gather(one("test:a"), one("test:b"))
    elapsed = time.monotonic() - started

    assert elapsed < BUILD_SECONDS * 1.8, (
        f"cold renders serialized ({elapsed:.2f}s for two {BUILD_SECONDS}s builds)"
    )
