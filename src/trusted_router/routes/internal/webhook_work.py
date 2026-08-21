"""Per-provider admission for blocking webhook verification and Store work."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from starlette.concurrency import run_in_threadpool

from trusted_router.errors import api_error
from trusted_router.types import ErrorType

WEBHOOK_MAX_BLOCKING_TASKS_PER_PROVIDER = 4

_T = TypeVar("_T")
_PROVIDER_SLOTS = {
    provider: threading.BoundedSemaphore(WEBHOOK_MAX_BLOCKING_TASKS_PER_PROVIDER)
    for provider in ("adyen", "paypal", "ses", "stripe", "veriff")
}


async def run_provider_webhook_work(
    provider: str,
    func: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run blocking provider work without exhausting Starlette's threadpool."""

    slot = _PROVIDER_SLOTS[provider]
    if not slot.acquire(blocking=False):
        raise api_error(
            503,
            f"{provider.title()} webhook processor is busy; retry",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
        )
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    finally:
        slot.release()
