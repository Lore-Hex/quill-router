"""Small, bounded metadata collector for the owned static exchange sites."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field


class ExchangeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["exchange.landing_viewed", "exchange.landing_engaged"]
    visitor_id: UUID4
    page: Literal["/", "/providers", "/badge"] = "/"
    campaign: dict[str, str] = Field(default_factory=dict, max_length=5)


_LOCK = threading.Lock()
_WINDOW = -1
_TOTAL = 0
_CLIENTS: OrderedDict[str, int] = OrderedDict()


def accept_event(client: str, *, now: float | None = None) -> bool:
    """Per-process safety caps; no database, network calls, or unbounded IP map."""
    global _WINDOW, _TOTAL
    window = int((time.monotonic() if now is None else now) // 60)
    with _LOCK:
        if window != _WINDOW:
            _CLIENTS.clear()
            _TOTAL, _WINDOW = 0, window
        if _TOTAL >= 6000 or _CLIENTS.get(client, 0) >= 120:
            return False
        _CLIENTS[client] = _CLIENTS.get(client, 0) + 1
        _CLIENTS.move_to_end(client)
        if len(_CLIENTS) > 4096:
            _CLIENTS.popitem(last=False)
        _TOTAL += 1
        return True
