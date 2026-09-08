"""Decision 76: one fail-closed policy for all lease admission paths."""

from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.config import Settings
from trusted_router.trust_owner_budget import (
    OWNER_BUDGET_KIND,
    OWNER_BUDGET_VERSION,
    owner_budget_id,
)
from trusted_router.trust_ownership import TRUST_OWNER_MUTATION_BUDGET
from trusted_router.trust_reconciliation import (
    OWNER_INVENTORY_ACCOUNT_ID,
    OWNER_INVENTORY_PROVIDER,
    OWNER_INVENTORY_SOURCE,
    OWNER_INVENTORY_SOURCE_VERSION,
    STRIPE_CONSISTENCY_DELAY_SECONDS,
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
    MarkerRequirement,
    completed_marker_satisfies,
    reconciliation_is_fresh,
)
from trusted_router.trust_tiers import effective_trust_tier

log = logging.getLogger(__name__)


def tier_cap(settings: Settings, tier: int) -> int:
    return (
        0,
        settings.spend_lease_tier1_cap_microdollars,
        settings.spend_lease_tier2_cap_microdollars,
        settings.spend_lease_tier3_cap_microdollars,
    )[max(0, min(3, tier))]


def spend_cap(settings: Settings, tier: int | None) -> int:
    if not settings.spend_lease_trust_eligibility_enabled:
        return settings.spend_lease_max_microdollars
    ceiling = max(
        settings.spend_lease_max_microdollars, settings.spend_lease_tier3_cap_microdollars
    )
    return min(ceiling, tier_cap(settings, tier or 0))


@dataclass(frozen=True)
class LeaseTrustState:
    tier: int
    latched_at: datetime | None
    pause_causes: str
    pause_epoch: int
    reconciled_through: datetime | None

    def refusal(self, *, now: datetime, max_age_seconds: int) -> str | None:
        if self.pause_causes not in ("", "[]"):
            return "billing_paused"
        if self.tier < 1 or self.latched_at is not None:
            return "unpaid_workspace"
        if not reconciliation_is_fresh(
            self.reconciled_through,
            now=now,
            max_age_seconds=max_age_seconds,
        ):
            return "reconciliation_stale"
        return None


def read_lease_trust(
    reader: Any,
    pt: Any,
    workspace_id: str,
    *,
    shard: int | None = None,
) -> LeaseTrustState | None:
    params: dict[str, Any] = {"ws": workspace_id}
    types = {"ws": pt.STRING}
    suffix = ""
    if shard is not None:
        suffix = " AND shard=@shard"
        params["shard"] = shard
        types["shard"] = pt.INT64
    rows = list(
        reader.execute_sql(
            "SELECT trust_tier, trust_latched_at, billing_pause_causes, pause_epoch, "  # noqa: S608 - fixed shard clause
            "trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws" + suffix,
            params=params,
            param_types=types,
        )
    )
    return _lease_trust_from_rows(rows)


def _lease_trust_from_rows(rows: list[Any]) -> LeaseTrustState | None:
    if not rows or any(tuple(row) != tuple(rows[0]) for row in rows):
        return None
    tier, latch, causes, epoch, through = rows[0]
    return LeaseTrustState(
        effective_trust_tier(int(tier or 0), trust_latched_at=latch),
        latch,
        str(causes or ""),
        int(epoch or 0),
        through,
    )


@dataclass(frozen=True)
class WorkspaceLeaseTrust:
    shards: tuple[int, ...]
    billing_paused: bool
    state: LeaseTrustState | None


def read_workspace_lease_trust(
    reader: Any, pt: Any, workspace_id: str
) -> WorkspaceLeaseTrust:
    """Read workspace-wide evidence once; never substitute for a shard-scoped read."""
    rows = list(reader.execute_sql(
        "SELECT shard, trust_tier, trust_latched_at, billing_pause_causes, pause_epoch, "
        "trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
        params={"ws": workspace_id},
        param_types={"ws": pt.STRING},
    ))
    return WorkspaceLeaseTrust(
        shards=tuple(int(row[0]) for row in rows),
        billing_paused=any(str(row[3] or "") not in ("", "[]") for row in rows),
        # Shard identifiers legitimately differ; only trust columns must agree.
        state=_lease_trust_from_rows([row[1:] for row in rows]),
    )


def billing_paused_tx(
    reader: Any, pt: Any, workspace_id: str, *, shard: int | None = None
) -> bool:
    # Reading the epoch establishes a conflict even if a pause was cleared
    # before this transaction retries. A new authorization takes no holds.
    params: dict[str, Any] = {"ws": workspace_id}
    types = {"ws": pt.STRING}
    suffix = ""
    if shard is not None:
        suffix = " AND shard=@shard"
        params["shard"] = shard
        types["shard"] = pt.INT64
    rows = reader.execute_sql(
        "SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance WHERE workspace_id=@ws" + suffix,  # noqa: S608 - fixed shard clause
        params=params,
        param_types=types,
    )
    return any(str(row[0] or "") not in ("", "[]") for row in rows)


def trust_gate_failure(
    store: Any,
    settings: Settings,
    *,
    reader: Any,
    now: datetime,
    deadlines: list[tuple[datetime, str]],
) -> str | None:
    from trusted_router.storage_trust_reconciliation import MARKER_COLUMNS, _marker_from_row

    if (
        settings.storage_backend not in {"spanner-bigtable", "spanner-clickhouse"}
        or settings.request_record_write_mode != "typed"
        or getattr(store, "request_record_write_mode", None) != "typed"
    ):
        return "typed_spanner_required"
    providers = settings.trust_qualifying_provider_set
    if not providers or not providers <= {"stripe", "x402"}:
        # Additive provider support must supply its pinned source contract.
        return "provider_not_configured"
    account = settings.trust_stripe_account_id
    if not account.startswith("acct_") or len(account) <= 5:
        return "provider_not_configured"
    requirements = [
        MarkerRequirement(
            provider,
            account,
            settings.environment,
            STRIPE_TRUST_SOURCE,
            STRIPE_TRUST_SOURCE_VERSION,
        )
        for provider in sorted(providers)
    ]
    requirements.append(
        MarkerRequirement(
            OWNER_INVENTORY_PROVIDER,
            OWNER_INVENTORY_ACCOUNT_ID,
            settings.environment,
            OWNER_INVENTORY_SOURCE,
            OWNER_INVENTORY_SOURCE_VERSION,
        )
    )
    for requirement in requirements:
        params = asdict(requirement)
        markers = [
            _marker_from_row(row)
            for row in reader.execute_sql(
                "SELECT " + ", ".join(MARKER_COLUMNS) + " FROM tr_trust_backfill "  # noqa: S608 - fixed columns
                "WHERE provider=@provider AND account_id=@account_id "
                "AND environment=@environment AND source=@source AND source_version=@source_version",
                params=params,
                param_types={key: store._param_types.STRING for key in params},
            )
        ]
        marker = next((m for m in markers if completed_marker_satisfies(m, requirement)), None)
        if (
            marker is None
            or marker.completed_at is None
            or marker.completed_at > now
            or marker.history_start > marker.closed_through
        ):
            return "marker_incomplete"
        if requirement.provider != OWNER_INVENTORY_PROVIDER:
            if (
                marker.consistency_delay_seconds < STRIPE_CONSISTENCY_DELAY_SECONDS
                or settings.trust_reconcile_max_age_seconds
                < (marker.consistency_delay_seconds + 2 * settings.trust_reconcile_interval_seconds)
            ):
                return "consistency_delay"
            if not reconciliation_is_fresh(
                marker.closed_through,
                now=now,
                max_age_seconds=settings.trust_reconcile_max_age_seconds,
            ):
                return "marker_stale"
            deadlines.append((
                marker.closed_through + timedelta(seconds=settings.trust_reconcile_max_age_seconds),
                "marker_stale",
            ))
    budget = store._read_entity_tx(
        reader, OWNER_BUDGET_KIND, owner_budget_id(settings.environment), dict
    )
    if budget is None:
        return "owner_budget_missing"
    if (
        budget["source_version"] != OWNER_BUDGET_VERSION
        or budget["environment"] != settings.environment
        or budget["mutation_budget"] != TRUST_OWNER_MUTATION_BUDGET
        or budget["scan_complete"] is not True
        or type(budget["max_observed_mutations"]) is not int
        or budget["max_observed_mutations"] < 0
    ):
        return "read_failed"
    computed_at = datetime.fromisoformat(budget["computed_at"])
    if not reconciliation_is_fresh(
        computed_at, now=now, max_age_seconds=settings.trust_reconcile_max_age_seconds
    ):
        return "owner_budget_stale"
    deadlines.append((
        computed_at + timedelta(seconds=settings.trust_reconcile_max_age_seconds),
        "owner_budget_stale",
    ))
    if budget["violating_owners"] or budget["max_observed_mutations"] > TRUST_OWNER_MUTATION_BUDGET:
        # The old require_owner_trust_budget exception was exposed as read_failed.
        return "read_failed"
    return None


GLOBAL_TRUST_TTL_SECONDS = 15


def _global_key(store: Any, settings: Settings) -> tuple[Any, ...]:
    return (
        id(getattr(store, "_database", None)),
        getattr(store, "request_record_write_mode", None),
        settings.storage_backend,
        settings.request_record_write_mode,
        settings.spend_lease_trust_eligibility_enabled,
        settings.trust_qualifying_provider_set,
        settings.trust_stripe_account_id,
        settings.environment,
        settings.trust_reconcile_max_age_seconds,
        settings.trust_reconcile_interval_seconds,
    )


@dataclass(frozen=True)
class GlobalTrustVerdict:
    key: tuple[Any, ...]
    failure: str | None
    evaluated_at: datetime
    expires_monotonic: float
    deadlines: tuple[tuple[datetime, str], ...] = ()

    def refusal(self, store: Any, settings: Settings, now: datetime) -> str | None:
        # Pure consumption, including after a transaction retry or a delayed
        # binding plan. Never refresh from within the caller's transaction.
        if self.key != _global_key(store, settings) or time.monotonic() >= self.expires_monotonic:
            return "global_verdict_expired"
        if now < self.evaluated_at:
            return "read_failed"
        if self.failure:
            return self.failure
        for deadline, reason in self.deadlines:
            if now > deadline:
                return reason
        return None


@dataclass
class _GlobalCache:
    lock: Any = field(default_factory=threading.Lock)
    verdict: GlobalTrustVerdict | None = None


_caches: weakref.WeakKeyDictionary[Any, _GlobalCache] = weakref.WeakKeyDictionary()
_caches_lock = threading.Lock()


def global_trust_verdict(
    store: Any, settings: Settings, *, now: datetime | None = None
) -> GlobalTrustVerdict:
    """Lazy single-flight refresh outside transactions; cache refusals too.

    Evidence deadlines are checked again at consumption, so a TTL never extends
    a marker's max age. Configuration changes get a separate evaluation.
    """
    with _caches_lock:
        cache = _caches.setdefault(store, _GlobalCache())
    with cache.lock:
        key = _global_key(store, settings)
        if (
            cache.verdict is not None
            and cache.verdict.key == key
            and time.monotonic() < cache.verdict.expires_monotonic
        ):
            return cache.verdict
        evaluated_at = now or datetime.now(UTC)
        started = time.monotonic()
        deadlines: list[tuple[datetime, str]] = []
        try:
            with store._database.snapshot(multi_use=True) as snapshot:
                failure = trust_gate_failure(
                    store, settings, reader=snapshot, now=evaluated_at, deadlines=deadlines
                )
        except Exception:
            log.exception("trust.gate_unarmed read_failed")
            failure = "read_failed"
        verdict = GlobalTrustVerdict(
            key, failure, evaluated_at, started + GLOBAL_TRUST_TTL_SECONDS, tuple(deadlines)
        )
        cache.verdict = verdict
        return verdict


def _unarmed(failure: str) -> tuple[int | None, str | None]:
    from trusted_router.synthetic.alerts import ops_alert

    log.error("trust.gate_unarmed condition=%s", failure)
    ops_alert(
        f"trust.gate_unarmed condition={failure}", fingerprint=["trust.gate_unarmed", failure]
    )
    return None, "trust_gate_unarmed"


def lease_eligibility(
    store: Any,
    settings: Settings,
    workspace_id: str | None = None,
    *,
    reader: Any = None,
    now: datetime | None = None,
    global_verdict: GlobalTrustVerdict | None = None,
    workspace_trust: WorkspaceLeaseTrust | None = None,
) -> tuple[int | None, str | None]:
    if not settings.spend_lease_trust_eligibility_enabled:
        return None, None
    now = now or datetime.now(UTC)
    if reader is None:
        try:
            global_verdict = global_verdict or global_trust_verdict(store, settings, now=now)
        except Exception:
            log.exception("trust.gate_unarmed read_failed")
            return _unarmed("read_failed")
    # A caller supplying a transaction MUST also supply the global verdict.
    # Missing/expired evidence refuses without opening a snapshot or doing I/O.
    failure = (
        global_verdict.refusal(store, settings, now)
        if global_verdict is not None else "global_verdict_missing"
    )
    if failure:
        return _unarmed(failure)
    if workspace_id is None:
        return None, None
    if reader is None:
        try:
            with store._database.snapshot(multi_use=True) as snapshot:
                return lease_eligibility(
                    store, settings, workspace_id, reader=snapshot, now=now,
                    global_verdict=global_verdict,
                )
        except Exception:
            # Preserve the non-transactional admission refusal on snapshot or
            # workspace-read failure. Transaction callers still own retries.
            log.exception("trust.gate_unarmed read_failed")
            return _unarmed("read_failed")
    from trusted_router.storage_gcp_counters import credit_shard_count
    from trusted_router.storage_models import CreditAccount

    account = store._read_entity_tx(reader, "credit", workspace_id, CreditAccount)
    # Reuse only evidence read for this workspace in this caller's transaction.
    if workspace_trust is None:
        workspace_trust = read_workspace_lease_trust(reader, store._param_types, workspace_id)
    if account is None or sorted(workspace_trust.shards) != list(
        range(credit_shard_count(account))
    ):
        return None, "reconciliation_stale"
    state = workspace_trust.state
    if state is None:
        return None, "reconciliation_stale"
    return state.tier, state.refusal(
        now=now, max_age_seconds=settings.trust_reconcile_max_age_seconds
    )


def artifact_trust_tier(artifact: Any) -> int | None:
    """Read the tier from an already trusted/verified artifact's signed payload."""
    import json

    from trusted_router.receipt_keys import b64url_decode

    try:
        tier = json.loads(b64url_decode(artifact.token.split(".")[1])).get("trust_tier")
    except (ValueError, IndexError, TypeError, AttributeError):
        return None
    return tier if type(tier) is int and 1 <= tier <= 3 else None
