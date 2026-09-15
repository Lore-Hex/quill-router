"""Bounded admission and outcome-independent delay for key introspection."""

import asyncio
import secrets
import threading


class LookupGate:
    def __init__(self, capacity: int = 16) -> None:
        self.slots = threading.BoundedSemaphore(capacity)

    async def delay(self) -> None:
        # Neither response status nor key validity affects this delay. Async
        # waiting does not occupy Starlette's request worker threads.
        await asyncio.sleep(3 + secrets.randbelow(2001) / 1000)
