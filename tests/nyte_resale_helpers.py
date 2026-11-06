from __future__ import annotations

from typing import Any
from unittest.mock import patch

from trusted_router.storage_resale import REFUND_WINDOW, PositionRepairRequired
from trusted_router.store_protocol import Store


def call(store: Store, op: str, **args: Any) -> dict[str, Any]:
    observations = {}
    while True:
        try:
            return store.reserved_resale({"op": op, "now": REFUND_WINDOW + 101, **args, "_position_states": observations})
        except PositionRepairRequired as repair:
            assert repair.session_id not in observations
            observations[repair.session_id] = None  # Transaction-only helper: no provider I/O.



def inspect_position(store: Store, session: str) -> dict[str, Any]:
    """Observe persisted test state without invoking an application repair read."""
    with patch("trusted_router.storage_resale.execute", side_effect=lambda read, write, command: dict(read("nyte_position", session))):
        return store.reserved_resale({"op": "test_observation", "now": 0})


def purchase(store: Store, name: str, *, lots: int = 3, price: int = 100, allocate: bool = False, grade: str | None = None, total_cents: int | None = None) -> dict[str, Any]:
    return call(store, "purchase", position={"id": name, "session_id": name, "grade": grade or (name[:12] if len(name) >= 12 else "A"), "lots": lots,
                "per_lot_cents": price, "payment_intent": "pi_" + name, "holder": name,
                "purchase_completed_at": 100, "terms_version": "2026-10-27-v5" if total_cents is None else "2026-10-28-v6",
                **({"total_cents": total_cents} if total_cents is not None else {})}, event_id="evt_" + name, allocate=allocate, fee_bps=500)


def listing(store: Store, name: str, *, lots: int = 3, request: str = "list") -> str:
    return call(store, "list", position=name, holder=name, lots=lots, request_id=request)["listing_id"]


