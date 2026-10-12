"""Pure slot decisions and non-authorizing backlog observations."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from trusted_router.async_settle_shadow_v2_evidence import HEALTH, health_usable
from trusted_router.async_settle_shadow_v2_schema import INT64, POLICY

SLOT_US = 2_000_000
JITTER_US = 250_000
CYCLE_SECONDS = .5


@dataclass
class Slots:
    last_slot: int | None = None
    stopped: bool = False

    def tick(self, now_us: int, *, busy: bool) -> tuple[int | None, int]:
        """Consume every encountered slot once, even when late or busy."""
        if self.stopped:
            return None, 0
        slot, offset = divmod(now_us, SLOT_US)
        if self.last_slot is not None and slot <= self.last_slot:
            return None, 0
        missed = max(0, slot - self.last_slot - 1) if self.last_slot is not None else 0
        self.last_slot = slot
        if busy or offset > JITTER_US:
            return None, missed + 1
        return slot, missed


def claim_allowed(previous: dict[str, Any] | None, slot: int) -> bool:
    return previous is None or previous["slot"] < slot


def publish_allowed(claim: dict[str, Any] | None, previous: dict[str, Any] | None,
                    value: dict[str, Any]) -> bool:
    return (claim is not None and claim["slot"] == value["slot"]
            and claim["owner_boot_id"] == value["publisher_boot_id"]
            and (previous is None or (previous["slot"] < value["slot"]
                                     and previous["observed_at_us"] < value["observed_at_us"])))


def summarize(rows: list[Any], *, boot: str, region: str, revision: str, slot: int,
              observed_us: int, heartbeat_us: int, row_limit: int) -> dict[str, Any]:
    complete = len(rows) <= row_limit
    ages: list[int] = []
    amount = dead = 0
    for created, micro, status in rows:
        if type(micro) is not int or not 0 <= micro <= INT64:
            complete, micro = False, 0
        amount += micro
        if amount > INT64:
            complete, amount = False, INT64
        if type(status) is not str or status not in ("pending", "dead"):
            complete = False
        dead += status == "dead"
        try:
            stamp = created if isinstance(created, dt.datetime) else dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            created_us = int(stamp.timestamp() * 1_000_000)
            if stamp.tzinfo is None or not 0 < created_us <= observed_us:
                raise ValueError("unknown_age")
            age = observed_us - created_us
        except (AttributeError, TypeError, ValueError, OverflowError):
            complete, age = False, observed_us
        ages.append(age)
    ages.sort()
    def percentile(numerator: int) -> int:
        return ages[max(0, (len(ages)*numerator+99)//100-1)] if ages else 0
    value = dict(v=2, policy_version=POLICY, authority="local", source="shadow_observation",
                 publisher_boot_id=boot, publisher_region=region, router_revision=revision,
                 slot=slot, observed_at_us=observed_us, publisher_heartbeat_us=heartbeat_us,
                 complete=complete, sample_count=len(rows), backlog_count=len(rows), frozen_micro=amount,
                 p50_age_us=percentile(50), p95_age_us=percentile(95),
                 oldest_unresolved_age_us=max(ages, default=0), dead_count=dead)
    HEALTH(value)
    return value


def prediction(observer: Any, workspace: str) -> dict[str, Any]:
    """Called by the real Observer under its lock, only for the exact v2 pin."""
    from trusted_router.services.async_settle import TIER_CAPS
    from trusted_router.services.async_settle_shadow_admission import unknown

    result = unknown()
    result.update(prediction_basis="counterfactual_backlog_v2", health_source="shadow_observation", health_slot=None)
    item, health = observer.entries.get(workspace), observer.health
    if item is None or health is None:
        return result
    try:
        usable = health_usable(health, int(observer.wall()*1_000_000))
    except (ValueError, TypeError):
        result["reason"] = "invalid_data"
        return result
    started, data = item
    age = observer.clock() - started
    result.update(health_slot=health["slot"], tier=data.tier, pending_micro=data.pending_micro,
                  workspace_age_us=max(0, int(age*1_000_000)),
                  health_age_us=max(0, int(observer.wall()*1_000_000)-health["observed_at_us"]),
                  health_p95_us=health["p95_age_us"])
    if not usable or not 0 <= age < 5 or workspace in observer.stale or None in observer.stale:
        result["reason"] = "cache_stale"
        return result
    if type(data.pending_micro) is not int or data.pending_micro < 0 or type(data.tier) is not int:
        result["reason"] = "invalid_data"
        return result
    cap = observer.cap or TIER_CAPS.get(data.tier)
    result["cap_micro"] = cap
    reason = ("ineligible_tier" if data.tier not in TIER_CAPS else
              "cap_exceeded" if cap is not None and data.pending_micro > cap else
              "drain_unhealthy" if health["p95_age_us"] > 5_000_000 else "eligible")
    result.update(prediction="yes" if reason == "eligible" else "no", reason=reason)
    return result
