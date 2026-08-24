"""Control-plane concurrency slots for gated user-model dispatch."""

from __future__ import annotations

from trusted_router.storage import STORE
from trusted_router.user_model_rules import dispatch_budget

# Grace on top of the kind's total dispatch budget before an unreleased slot
# (enclave died between authorize and settle) stops counting.
SLOT_GRACE_SECONDS = 120


def slot_ttl_seconds(kind: str) -> int:
    return dispatch_budget(kind).total + SLOT_GRACE_SECONDS


def acquire_user_model_slot(
    model_id: str,
    authorization_id: str,
    *,
    limit: int,
    kind: str,
) -> bool:
    return STORE.acquire_user_model_slot(
        model_id,
        authorization_id,
        limit=limit,
        ttl_seconds=slot_ttl_seconds(kind),
    )


def release_user_model_slot(model_id: str, authorization_id: str) -> None:
    STORE.release_user_model_slot(model_id, authorization_id)
