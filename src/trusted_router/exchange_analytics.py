"""Small, bounded metadata collector for the owned static exchange sites."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, field_validator, model_validator

# Canonical exchange domain -> market slug, mirrored from sites/token-exchange/markets.json.
EXCHANGE_MARKETS: dict[str, str] = json.loads(
    (Path(__file__).parent / "data" / "token_exchange_markets.json").read_text()
)
EXCHANGE_DOMAINS = frozenset(EXCHANGE_MARKETS)
EXCHANGE_MARKET_SLUGS = frozenset(EXCHANGE_MARKETS.values())
# Hosts an exchange page may report an outbound click to: the product, the code,
# the sales calendar and the sister markets.
OUTBOUND_HOSTS = frozenset({"trustedrouter.com", "github.com", "calendly.com"}) | EXCHANGE_DOMAINS
# One short slug names a page or a button, so a new page or button needs no list
# change here, in the browser script or in the export worker. Anything else is rejected.
PAGE_PATTERN = r"^/(?:[a-z0-9][a-z0-9-]{0,39})?$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,39}$"
HOST_PATTERN = r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
PAGE_RE = re.compile(PAGE_PATTERN)
HOST_RE = re.compile(HOST_PATTERN)


def market_for(host: str) -> str:
    """The market slug of an exchange hostname, or empty for anything else."""
    return EXCHANGE_MARKETS.get(host.lower(), "")


class ExchangeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["exchange.landing_viewed", "exchange.landing_engaged", "nyte.cta_clicked", "nyte.video_played", "nyte.form_submitted", "nyte.outbound_clicked", "nyte.buy_step_1_viewed", "nyte.buy_step_2_viewed", "nyte.buy_step_3_viewed", "nyte.buy_step_4_viewed", "nyte.buy_completed", "nyte.reserve_checkout_started", "nyte.reserve_checkout_succeeded", "nyte.resale_listed", "nyte.resale_withdrawn"]
    visitor_id: UUID4
    page: str = Field("/", pattern=PAGE_PATTERN)
    campaign: dict[str, str] = Field(default_factory=dict, max_length=5)
    # The referring site's hostname only, never a URL; absent on a direct visit.
    referrer_host: str | None = Field(None, pattern=HOST_PATTERN, max_length=253)

    cta: str | None = Field(None, pattern=SLUG_PATTERN)
    position: Literal["header", "hero", "section", "close"] | None = None
    form: str | None = Field(None, pattern=SLUG_PATTERN)
    destination_host: str | None = Field(None, max_length=253)

    unit: Literal["blocks", "throughput", "unsure"] | None = None
    underlying_kind: Literal["index", "model", "facility"] | None = None
    grade: Literal["AAA", "A", "B", "C", "none"] | None = None

    @field_validator("destination_host")
    @classmethod
    def known_destination(cls, value: str | None) -> str | None:
        if value is not None and value not in OUTBOUND_HOSTS:
            raise ValueError("Unknown outbound destination")
        return value

    @model_validator(mode="after")
    def event_properties(self) -> ExchangeEvent:
        required = {
            "nyte.cta_clicked": {"cta", "position"},
            "nyte.video_played": set(),
            "nyte.form_submitted": {"form"},
            "nyte.outbound_clicked": {"destination_host", "cta"},
            "nyte.buy_step_1_viewed": {"unit", "underlying_kind", "grade"},
            "nyte.buy_step_2_viewed": {"unit", "underlying_kind", "grade"},
            "nyte.buy_step_3_viewed": {"unit", "underlying_kind", "grade"},
            "nyte.buy_step_4_viewed": {"unit", "underlying_kind", "grade"},
            "nyte.reserve_checkout_started": {"unit", "underlying_kind", "grade"},
            "nyte.reserve_checkout_succeeded": set(),
            "nyte.resale_listed": {"grade"},
            "nyte.resale_withdrawn": {"grade"},
            "nyte.buy_completed": {"unit", "underlying_kind", "grade"},
        }.get(self.event, set())
        fields = (("cta", self.cta), ("position", self.position), ("form", self.form),
                  ("destination_host", self.destination_host), ("unit", self.unit),
                  ("underlying_kind", self.underlying_kind), ("grade", self.grade))
        supplied = {name for name, value in fields if value is not None}
        if supplied != required:
            raise ValueError("Invalid event properties")
        if self.event.startswith("nyte.resale_") and (self.page != "/manage-reservation" or self.grade not in {"AAA", "A", "B", "C"}):
            raise ValueError("Invalid resale event")
        if self.event.startswith("nyte.reserve_"):
            if self.page != "/buy":
                raise ValueError("Reservation events belong to /buy")
            if self.event == "nyte.reserve_checkout_started" and (
                self.unit != "blocks" or self.underlying_kind != "index" or self.grade not in {"AAA", "A", "B", "C"}
            ):
                raise ValueError("Only index-grade lots have card checkout")
        if self.event.startswith("nyte.buy_"):
            if self.underlying_kind == "facility" and self.unit != "blocks":
                raise ValueError("Facility requests use token lots")
            if self.page != "/buy" or (self.underlying_kind in {"model", "facility"}) != (self.grade == "none"):
                raise ValueError("Invalid capacity request metadata")
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
