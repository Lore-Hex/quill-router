"""Gateway-side health evidence for gated user-model dispatch."""

from __future__ import annotations

from trusted_router.storage import STORE


def record_user_model_gateway_result(model_id: str, *, success: bool) -> None:
    """Persist one production dispatch result after a winning finalize."""
    STORE.record_user_model_dispatch_result(model_id, success=success)
