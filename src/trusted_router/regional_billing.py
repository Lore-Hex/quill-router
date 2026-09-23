"""Exact regional charge split, frozen before either ledger is mutated."""
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_models import GatewayAuthorization

LOCAL_CHARGE_FIELD = "regional_local_microdollars"
GLOBAL_CHARGE_FIELD = "regional_global_microdollars"


@dataclass(frozen=True)
class RegionalCharge:
    local: int
    global_: int

    def payload(self) -> dict[str, int]:
        return {LOCAL_CHARGE_FIELD: self.local, GLOBAL_CHARGE_FIELD: self.global_}


def regional_charge(auth: GatewayAuthorization, actual: int, success: bool) -> RegionalCharge:
    if actual < 0 or auth.estimated_microdollars < 0:
        raise ValueError("negative regional charge")
    total = actual if success else 0
    local = min(total, auth.estimated_microdollars)
    return RegionalCharge(local, total - local)


def frozen_regional_charge(
    auth: GatewayAuthorization, actual: int, success: bool, body: dict[str, Any],
) -> RegionalCharge:
    expected = regional_charge(auth, actual, success)
    # Rolling compatibility: old intents contain only the exact total. The
    # authorization estimate is immutable, so their split is deterministic.
    if LOCAL_CHARGE_FIELD not in body and GLOBAL_CHARGE_FIELD not in body:
        return expected
    if any(type(body.get(k)) is not int or body[k] != v for k, v in expected.payload().items()):
        raise ValueError("invalid frozen regional charge split")
    return expected


def record_regional_settlement(store: Any, auth: GatewayAuthorization, actual: int, success: bool, *, hold_unknown: bool) -> None:
    """R1 coverage stream, one stable settlement identity (including drain)."""
    import logging

    from trusted_router.spend_leases import build_spend_lease_shadow_event
    from trusted_router.storage_models import iso_now

    charge = regional_charge(auth, actual, success)
    try:
        event = build_spend_lease_shadow_event(
            event_id=f"regional-settle:{auth.id}", created_at=iso_now(),
            workspace_id=auth.workspace_id, key_hash=auth.key_hash,
            boot_kid="", boot_verified=False, no_lease_reason=None, echo=None,
            server_estimate_micro=auth.estimated_microdollars, server_verdict="accepted",
            authorization_id=auth.id, regional_outcome="settled" if success else "refunded",
            regional_resolved_region=auth.region,
        )
        payload = event.payload()
        payload.update(
            regional_actual_microdollars=charge.local + charge.global_,
            regional_local_microdollars=0 if hold_unknown else charge.local,
            regional_global_microdollars=charge.local + charge.global_ if hold_unknown else charge.global_,
            regional_overrun_microdollars=charge.global_,
        )
        store.record_spend_lease_shadow(event.event_id, payload)
    except Exception:
        logging.getLogger(__name__).exception("regional settlement observation failed")
