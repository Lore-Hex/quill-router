"""Recurring, durable proof of the fleet-wide owner trust mutation budget.

The backfill marker schema cannot hold the budget and operator diagnostics.
Use one versioned, environment-scoped entity and the marker freshness policy;
only the tier job scans owners. Admission reads this entity by its full key.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from trusted_router.trust_ownership import (
    TRUST_OWNER_MUTATION_BUDGET,
    OwnerTrustMutationBudgetExceeded,
    owner_trust_mutations,
    require_owner_trust_budget,
)

log = logging.getLogger(__name__)
OWNER_BUDGET_KIND = "trust_owner_budget"
OWNER_BUDGET_VERSION = "owner-budget-v1"


def owner_budget_id(environment: str) -> str:
    return f"{OWNER_BUDGET_VERSION}:{environment}"


def recompute_owner_budget(
    store: Any, *, environment: str, now: datetime | None = None
) -> dict[str, Any]:
    # Stamp the beginning, not completion: a slow scan must not rejuvenate old
    # evidence. All owners and credit accounts come from one strong snapshot.
    computed_at = now or datetime.now(UTC)
    maximum = 0
    violating: list[str] = []
    scan_complete = False
    try:
        with store._database.snapshot(multi_use=True) as reader:
            owners = {
                str(row[0])
                for row in reader.execute_sql(
                    "SELECT owner_user_id, workspace_id FROM tr_owner_workspace "
                    "ORDER BY owner_user_id, workspace_id"
                )
            }
            for owner in sorted(owners):
                _ids, counts = store._owner_shard_counts_tx(reader, owner)
                maximum = max(maximum, owner_trust_mutations(counts))
                try:
                    require_owner_trust_budget(counts)
                except OwnerTrustMutationBudgetExceeded:
                    violating.append(owner)
            scan_complete = True
    except Exception:
        # Invalidate the previous success even on a partial/failed scan.
        log.exception("trust.owner_budget_scan_failed")
    verdict = {
        "source_version": OWNER_BUDGET_VERSION,
        "environment": environment,
        "computed_at": computed_at.isoformat(),
        "max_observed_mutations": maximum,
        "mutation_budget": TRUST_OWNER_MUTATION_BUDGET,
        "violating_owners": violating,
        "scan_complete": scan_complete,
    }

    def save(transaction: Any) -> None:
        previous = store._read_entity_tx(
            transaction, OWNER_BUDGET_KIND, owner_budget_id(environment), dict
        )
        # Overlapping scheduler retries cannot replace newer evidence.
        if previous and datetime.fromisoformat(previous["computed_at"]) > computed_at:
            return
        store._write_entity_tx(
            transaction, OWNER_BUDGET_KIND, owner_budget_id(environment), verdict
        )

    store._run_in_transaction(save)
    log.info("trust.owner_budget verdict=%s", verdict)
    return verdict
