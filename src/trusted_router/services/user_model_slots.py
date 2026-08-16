"""Control-plane concurrency slots for gated user-model dispatch."""

from __future__ import annotations

from trusted_router.storage import STORE


def acquire_user_model_slot(
    model_id: str,
    authorization_id: str,
    *,
    limit: int,
) -> bool:
    return STORE.acquire_user_model_slot(model_id, authorization_id, limit=limit)


def release_user_model_slot(model_id: str, authorization_id: str) -> None:
    STORE.release_user_model_slot(model_id, authorization_id)
