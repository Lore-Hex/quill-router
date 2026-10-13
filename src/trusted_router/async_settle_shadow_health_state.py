"""Pure slot decisions and non-authorizing backlog observations."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from trusted_router.async_settle_shadow_v2_a1_health import (
    HEALTH,
    HEALTH_BASIS,
    health_usable,
    validate_publication,
)
from trusted_router.async_settle_shadow_v2_a1_numbers import age_microseconds
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
    if claim is None:
        return False
    try:
        validate_publication(claim, value, previous)
        return True
    except ValueError:
        return False


def summarize(rows: list[Any], *, boot: str, region: str, revision: str, slot: int,
              observed_ns: int, age_seconds: float, row_limit: int) -> dict[str, Any]:
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
            created_seconds = stamp.timestamp()
            if stamp.tzinfo is None or not 0 < created_seconds <= age_seconds:
                raise ValueError("unknown_age")
            age = age_microseconds(age_seconds - created_seconds)
        except (AttributeError, TypeError, ValueError, OverflowError):
            complete, age = False, age_microseconds(age_seconds)
        ages.append(age)
    ages.sort()
    def percentile(numerator: int) -> int:
        return ages[max(0, (len(ages)*numerator+99)//100-1)] if ages else 0
    value = dict(v=2, policy_version=POLICY, contract_revision="a1", authority="local", source="shadow_observation",
                 publisher_boot_id=boot, publisher_region=region, router_revision=revision,
                 slot=slot, observed_at_ns=observed_ns, publisher_heartbeat_ns=observed_ns,
                 backlog_age_at_seconds=age_seconds,
                 complete=complete, sample_count=len(rows), backlog_count=len(rows), frozen_micro=amount,
                 p50_age_us=percentile(50), p95_age_us=percentile(95),
                 oldest_unresolved_age_us=max(ages, default=0), dead_count=dead)
    HEALTH(value)
    return value


def prediction(observer: Any, workspace: str, *, at: float | None = None) -> dict[str, Any]:
    """Called by the real Observer under its lock, only for the exact v2 pin."""
    from trusted_router.services.async_settle import TIER_CAPS
    from trusted_router.services.async_settle_shadow_admission import unknown

    now = observer.clock() if at is None else at
    result = unknown()
    result.update(prediction_basis="counterfactual_backlog_v2", health_source="shadow_observation", health_slot=None)
    item, health = observer.entries.get(workspace), observer.health
    if item is None or health is None:
        return result
    try:
        body = health["body"]
        elapsed = round((now-health["reply_at"])*1_000_000_000)
        usable = health_usable(body, {k: health[k] for k in ("committed_at_ns", "read_at_ns")},
                               health["read_elapsed_ns"], elapsed)
    except (KeyError, ValueError, TypeError):
        result["reason"] = "invalid_data"
        return result
    started, data = item
    age = now - started
    result.update(health_slot=body["slot"], tier=data.tier, pending_micro=data.pending_micro,
                  workspace_age_us=max(0, int(age*1_000_000)),
                  health_age_us=max(0, (health["read_at_ns"]-body["observed_at_ns"]+health["read_elapsed_ns"]+elapsed)//1000),
                  health_p95_us=body["p95_age_us"])
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
              "drain_unhealthy" if body["p95_age_us"] > 5_000_000 else "eligible")
    result.update(prediction="yes" if reason == "eligible" else "no", reason=reason)
    return result


def health_basis(observer: Any, boot: str, *, at: float | None = None) -> dict[str, Any]:
    """Called under the observer cache lock at the prediction's capture time."""
    now = observer.clock() if at is None else at
    item = observer.health
    value = dict(v=2, policy_version=POLICY, contract_revision="a1", reader_boot=boot,
        source_state="read_failed" if None in observer.stale else "empty", body=None,
        committed_at_ns=None, read_at_ns=None, read_elapsed_ns=0, since_reply_ns=0)
    if item is not None and None not in observer.stale:
        q, e = item["read_elapsed_ns"], round((now-item["reply_at"])*1e9)
        usable = health_usable(item["body"], {k: item[k] for k in ("committed_at_ns", "read_at_ns")}, q, e)
        value.update(source_state="installed" if usable else "unusable", body=item["body"],
            committed_at_ns=item["committed_at_ns"], read_at_ns=item["read_at_ns"], read_elapsed_ns=q, since_reply_ns=e)
    HEALTH_BASIS(value)
    return value


def validate_startup_observation(observed: dict[str, Any]) -> None:
    """Parse the actual install and publication buffers with the native readers."""
    basis = HEALTH_BASIS.parse(observed["health_basis_raw"])
    if observed["contender"]:
        claim = json.loads(observed["claim_raw"])
        publication = HEALTH.parse(observed["publication_raw"])
        validate_publication(claim, publication)
        if publication != basis["body"]:
            raise ValueError("startup_publication_join")
