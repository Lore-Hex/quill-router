"""Fail-closed lease policy and the durable trust arm gate (decisions 75–76)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage_trust_reconciliation import MARKER_COLUMNS, _marker_from_row
from trusted_router.trust_ownership import require_owner_trust_budget
from trusted_router.trust_reconciliation import (
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
    MarkerRequirement,
    completed_marker_satisfies,
    reconciliation_is_fresh,
)
from trusted_router.trust_tiers import effective_trust_tier

log = logging.getLogger(__name__)


def tier_cap(settings: Settings, tier: int) -> int:
    return (0, settings.spend_lease_tier1_cap_microdollars,
            settings.spend_lease_tier2_cap_microdollars,
            settings.spend_lease_tier3_cap_microdollars)[max(0, min(3, tier))]


def spend_cap(settings: Settings, tier: int | None) -> int:
    if not settings.spend_lease_trust_eligibility_enabled:
        return settings.spend_lease_max_microdollars
    return min(max(settings.spend_lease_max_microdollars,
                   settings.spend_lease_tier3_cap_microdollars), settings.spend_lease_max_microdollars)


@dataclass(frozen=True)
class LeaseTrustState:
    tier: int
    latched_at: datetime | None
    pause_causes: str
    pause_epoch: int
    reconciled_through: datetime | None

    def refusal(self, *, now: datetime, max_age_seconds: int) -> str | None:
        if self.pause_causes not in ('', '[]'):
            return 'billing_paused'
        if self.tier < 1 or self.latched_at is not None:
            return 'unpaid_workspace'
        if not reconciliation_is_fresh(self.reconciled_through, now=now,
                                       max_age_seconds=max_age_seconds):
            return 'reconciliation_stale'
        return None


def read_lease_trust(reader: Any, pt: Any, workspace_id: str,
                     *, shard: int | None = None) -> LeaseTrustState | None:
    params: dict[str, Any] = {'ws': workspace_id}
    types = {'ws': pt.STRING}
    suffix = ''
    if shard is not None:
        suffix = ' AND shard=@shard'
        params['shard'] = shard
        types['shard'] = pt.INT64
    rows = list(reader.execute_sql(
        'SELECT trust_tier, trust_latched_at, billing_pause_causes, pause_epoch, '  # noqa: S608 - fixed shard clause
        'trust_reconciled_through FROM tr_credit_balance WHERE workspace_id=@ws' + suffix,
        params=params, param_types=types))
    if not rows or any(tuple(row) != tuple(rows[0]) for row in rows):
        return None
    tier, latch, causes, epoch, through = rows[0]
    # trust_tier is the effective tier materialized by the all-shard identity /
    # override transaction. Re-applying the raw override here could undo demotion.
    return LeaseTrustState(effective_trust_tier(int(tier or 0), trust_latched_at=latch),
                           latch, str(causes or ''), int(epoch or 0), through)


def billing_paused_tx(reader: Any, pt: Any, workspace_id: str) -> bool:
    rows = list(reader.execute_sql(
        'SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance '
        'WHERE workspace_id=@ws', params={'ws': workspace_id},
        param_types={'ws': pt.STRING}))
    return any(str(row[0] or '') not in ('', '[]') for row in rows)


def trust_gate_failure(store: Any, settings: Settings, *, reader: Any,
                       now: datetime) -> str | None:
    if settings.storage_backend not in {'spanner-bigtable', 'spanner-clickhouse'} or settings.request_record_write_mode != 'typed':
        return 'typed_spanner_required'
    if getattr(store, 'request_record_write_mode', None) != 'typed':
        return 'typed_spanner_required'
    accounts = settings.trust_provider_account_map
    requirements = []
    for provider in sorted(settings.trust_qualifying_provider_set):
        if provider not in {'stripe', 'x402'} or not accounts.get(provider):
            return 'provider_not_configured'
        requirements.append(MarkerRequirement(provider, accounts[provider], settings.environment,
                                              STRIPE_TRUST_SOURCE, STRIPE_TRUST_SOURCE_VERSION))
    requirements.append(MarkerRequirement('owner_inventory', 'local', settings.environment,
                                          'tr_entities.workspace', settings.trust_owner_inventory_source_version))
    rows = list(reader.execute_sql('SELECT ' + ', '.join(MARKER_COLUMNS) + ' FROM tr_trust_backfill'))  # noqa: S608 - fixed columns
    markers = [_marker_from_row(row) for row in rows]
    for requirement in requirements:
        marker = next((m for m in markers if completed_marker_satisfies(m, requirement)), None)
        if marker is None:
            return 'marker_incomplete'
        if marker.completed_at is None or marker.completed_at > now or marker.history_start > marker.closed_through:
            return 'marker_incomplete'
        if requirement.provider != 'owner_inventory':
            if marker.consistency_delay_seconds < 900 or settings.trust_reconcile_max_age_seconds < (
                marker.consistency_delay_seconds + 2 * settings.trust_reconcile_interval_seconds
            ):
                return 'consistency_delay'
            if not reconciliation_is_fresh(marker.closed_through, now=now,
                                           max_age_seconds=settings.trust_reconcile_max_age_seconds):
                return 'marker_stale'
    owners = {str(row[0]) for row in reader.execute_sql(
        'SELECT owner_user_id, workspace_id FROM tr_owner_workspace ORDER BY owner_user_id, workspace_id')}
    for owner in owners:
        _ids, counts = store._owner_shard_counts_tx(reader, owner)
        require_owner_trust_budget(counts)
    return None


def lease_eligibility(store: Any, settings: Settings, workspace_id: str | None = None,
                      *, reader: Any = None, now: datetime | None = None) -> tuple[int | None, str | None]:
    if not settings.spend_lease_trust_eligibility_enabled:
        return None, None
    now = now or datetime.now(UTC)
    if reader is None:
        try:
            with store._database.snapshot(multi_use=True) as snapshot:
                return lease_eligibility(store, settings, workspace_id, reader=snapshot, now=now)
        except Exception:
            log.exception('trust.gate_unarmed')
            _alert_unarmed('read_failed')
            return None, 'trust_gate_unarmed'
    try:
        failure = trust_gate_failure(store, settings, reader=reader, now=now)
    except Exception:
        log.exception('trust.gate_unarmed')
        _alert_unarmed('read_failed')
        return None, 'trust_gate_unarmed'
    if failure:
        _alert_unarmed(failure)
        return None, 'trust_gate_unarmed'
    if workspace_id is None:
        return None, None
    from trusted_router.storage_gcp_counters import credit_shard_count
    from trusted_router.storage_models import CreditAccount
    account = store._read_entity_tx(reader, "credit", workspace_id, CreditAccount)
    shards = list(reader.execute_sql("SELECT shard FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
                                    params={"ws": workspace_id}, param_types={"ws": store._param_types.STRING}))
    if account is None or sorted(int(row[0]) for row in shards) != list(range(credit_shard_count(account))):
        return None, 'reconciliation_stale'
    state = read_lease_trust(reader, store._param_types, workspace_id)
    if state is None:
        return None, 'reconciliation_stale'
    reason = state.refusal(now=now, max_age_seconds=settings.trust_reconcile_max_age_seconds)
    return state.tier, reason


def _alert_unarmed(condition: str) -> None:
    from trusted_router.synthetic.alerts import ops_alert
    ops_alert(f"trust.gate_unarmed condition={condition}",
              fingerprint=["trust.gate_unarmed", condition])
