"""Control startup deadlines independently of host scheduling in ordering tests."""

import asyncio
import time
from unittest.mock import Mock

from trusted_router.services import async_settle_shadow_startup_harness as harness


class StartupClock:
    def __init__(self, monkeypatch):
        self.now = time.monotonic()
        self.resumed = None
        # The listener and the transport worker have separate asyncio loops.
        # Keep both deadline clocks and the harness budget on the same timeline.
        monkeypatch.setattr(asyncio.BaseEventLoop, "time", lambda loop: self.read())
        monkeypatch.setattr(harness, "time", Mock(wraps=time, monotonic=self.read))

    def read(self):
        return self.now if self.resumed is None else self.now + (time.monotonic() - self.resumed)

    async def advance(self, seconds):
        self.now += seconds
        loop = asyncio.get_running_loop()
        dispatched = loop.create_future()
        loop.call_at(self.now, dispatched.set_result, None)
        await dispatched
        await asyncio.sleep(0)  # Let tasks cancelled by a due timer handle the exception.

    def resume(self):
        self.resumed = time.monotonic()
