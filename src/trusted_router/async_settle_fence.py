"""Internal capability for applying a frozen intent through unchanged money arithmetic."""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import ParamSpec, TypeVar

from trusted_router.storage_models import SettleOutboxRow

P = ParamSpec("P")
T = TypeVar("T")
APPLY_PAYLOAD: ContextVar[tuple[str, str] | None] = ContextVar("async_apply_payload", default=None)


def frozen_apply(func: Callable[[SettleOutboxRow], str]) -> Callable[[SettleOutboxRow], str]:
    @wraps(func)
    def apply(row: SettleOutboxRow) -> str:
        token = APPLY_PAYLOAD.set((row.payload_hash, row.intent_kind)
                                  if row.async_version == 1 and row.payload_hash else None)
        try:
            return func(row)
        finally:
            APPLY_PAYLOAD.reset(token)
    return apply
