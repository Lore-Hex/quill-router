from __future__ import annotations

from typing import Any

from trusted_router.storage_resale import REFUND_WINDOW
from trusted_router.store_protocol import Store


def call(store: Store, op: str, **args: Any) -> dict[str, Any]:
    return store.reserved_resale({"op": op, "now": REFUND_WINDOW + 101, **args})


def purchase(store: Store, name: str, *, lots: int = 3, price: int = 100, allocate: bool = False, grade: str | None = None) -> dict[str, Any]:
    return call(store, "purchase", position={"id": name, "session_id": name, "grade": grade or (name[:12] if len(name) >= 12 else "A"), "lots": lots,
                "per_lot_cents": price, "payment_intent": "pi_" + name, "holder": name,
                "purchase_completed_at": 100}, event_id="evt_" + name, allocate=allocate, fee_bps=500)


def listing(store: Store, name: str, *, lots: int = 3, request: str = "list") -> str:
    return call(store, "list", position=name, holder=name, lots=lots, request_id=request)["listing_id"]


