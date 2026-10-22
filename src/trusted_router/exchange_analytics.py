"""Small, bounded metadata collector for the owned static exchange sites."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator


class ExchangeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["exchange.landing_viewed", "exchange.landing_engaged", "nyte.cta_clicked", "nyte.video_played", "nyte.form_submitted", "nyte.outbound_clicked"]
    visitor_id: UUID4
    page: Literal["/", "/providers", "/badge", "/confidential", "/suppliers", "/index"] = "/"
    campaign: dict[str, str] = Field(default_factory=dict, max_length=5)

    cta: Literal["api_quick_start", "buyer_deck", "provider_deck", "api_key", "documentation", "apply_provider", "spare_capacity", "open_source_stack", "operator_guide", "index", "source"] | None = None
    position: Literal["header", "hero", "section", "close"] | None = None
    form: Literal["buyer_deck"] | None = None
    destination_host: Literal["trustedrouter.com", "github.com"] | None = None

    @model_validator(mode="after")
    def event_properties(self) -> ExchangeEvent:
        required = {
            "nyte.cta_clicked": {"cta", "position"},
            "nyte.video_played": set(),
            "nyte.form_submitted": {"form"},
            "nyte.outbound_clicked": {"destination_host", "cta"},
        }.get(self.event, set())
        supplied = {name for name in ("cta", "position", "form", "destination_host") if getattr(self, name) is not None}
        if supplied != required:
            raise ValueError("Invalid event properties")
        return self


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
