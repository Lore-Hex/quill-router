"""Shared, fail-closed admission for non-public operator probe models."""

from __future__ import annotations

from typing import Any

from trusted_router.catalog import MODELS, MONITOR_MODEL_ID
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.model_aliases import canonical_router_model_id
from trusted_router.regions import cloud_for_region
from trusted_router.security import lookup_hash_api_key
from trusted_router.types import ErrorType


def _requested_ids(body: dict[str, Any]) -> list[str]:
    ids = [body.get("model")]
    if isinstance(body.get("models"), list):
        ids.extend(body["models"])
    return [canonical_router_model_id(str(value or "").strip()) for value in ids]


def internal_model_ids() -> frozenset[str]:
    """Every catalog model id ``requests_internal_model`` refuses to a key
    that is not an internal probe key: the monitor model and every
    ``internal_only`` model, read from the catalog now. The fast path's edge
    policy publishes this set (services/edge_policy_publisher.py)."""
    return frozenset(
        {MONITOR_MODEL_ID} | {model_id for model_id, model in MODELS.items() if model.internal_only}
    )


def requests_internal_model(body: dict[str, Any]) -> bool:
    for model_id in _requested_ids(body):
        model = MODELS.get(model_id)
        if model_id == MONITOR_MODEL_ID or (model is not None and model.internal_only):
            return True
    return False


def require_internal_model_key(
    body: dict[str, Any], caller_hash: str | None, settings: Settings,
) -> None:
    if not requests_internal_model(body):
        return
    if "trustedrouter/fortanix-test" in _requested_ids(body) and cloud_for_region(settings.effective_serving_region) not in {"gcp", "aws", "azure"}:
        raise api_error(503, "Internal test provider is not enabled in this cloud", ErrorType.MODEL_NOT_SUPPORTED)
    for expected in (settings.synthetic_monitor_api_key, settings.stage_d_probe_api_key):
        if expected and caller_hash == lookup_hash_api_key(expected):
            return
    raise api_error(
        403,
        # Preserve the existing monitor API's error contract.
        "trustedrouter/monitor is restricted to the synthetic monitor key"
        if MONITOR_MODEL_ID in _requested_ids(body)
        else "Internal test models require a configured internal probe key",
        ErrorType.FORBIDDEN,
    )
