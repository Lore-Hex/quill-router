"""Owner-inventory and trust-demotion invariants.

The constants in this module are deliberately not settings.  They describe
the shape of one deployed schema and the conservative transaction budget the
trust design was proved against; changing either is a design/migration event,
not a rollout knob.
"""

from __future__ import annotations

from collections.abc import Iterable

TRUST_REPLICATED_COLUMN_COUNT = 7
TRUST_OWNER_MUTATION_BUDGET = 20_000


class WorkspaceOwnerLimitExceeded(ValueError):
    """A new workspace would exceed the configured per-owner count."""


class OwnerTrustMutationBudgetExceeded(ValueError):
    """An owner transition would exceed the pinned all-shard write budget."""


def owner_trust_mutations(shard_counts: Iterable[int]) -> int:
    """Return the conservative trust rewrite cost for one owner."""

    counts = tuple(int(count) for count in shard_counts)
    if any(count < 1 for count in counts):
        raise ValueError("owner inventory contains an invalid shard count")
    return sum(counts) * TRUST_REPLICATED_COLUMN_COUNT


def require_owner_trust_budget(shard_counts: Iterable[int]) -> int:
    """Validate and return an owner's conservative trust rewrite cost."""

    mutations = owner_trust_mutations(shard_counts)
    if mutations > TRUST_OWNER_MUTATION_BUDGET:
        raise OwnerTrustMutationBudgetExceeded(
            "owner trust fan-out exceeds the pinned 20000-mutation budget"
        )
    return mutations
