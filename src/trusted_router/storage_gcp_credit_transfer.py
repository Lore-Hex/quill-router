"""Cross-plane credit transfer on the native Spanner (GCP) plane.

See `trusted_router.credit_transfer` for the state machine, which plane holds
the value in each state, and the conservation invariant. This module is the
Spanner implementation of the five Store methods that move value off (and onto)
this plane; `storage_postgres.py` is the same contract on the SQL planes, and
`tests/conformance/test_store_semantics.py` asserts both against one suite.

WHY THIS IS NOT A TRANSLITERATION OF THE POSTGRES CODE
-----------------------------------------------------
Two things differ, and both land on money:

1. **The balance is SHARDED.** Postgres keeps one authoritative row per
   workspace (``shard = 0``) and can debit escrow with a single conditional
   UPDATE. Spanner spreads the workspace's budget over ``shard_count``
   independent sub-budgets, none of which individually knows the total. So the
   escrow debit is a PLAN over shards, built exactly like
   `storage_gcp_credit_rebalance.rebalance_credit_for_estimate`: read every
   shard's headroom in the transaction, refuse unless the SIGNED sum covers the
   amount, then debit greedily through `debit_credit_shard` — the same
   conditional statement the shard rebalancer already runs in production. Every
   step stays conditional, so the plan can never blind-decrement, and any step
   failing rolls the whole transaction back.

2. **There is no ``SELECT ... FOR UPDATE``.** The Postgres refund path locks the
   escrow row so two resolvers serialize. Spanner has no such statement, so that
   guarantee is re-derived from read-set validation plus abort-and-RE-INVOKE,
   which forces the losing resolver to recompute its decision instead of
   applying a stale one. Spelled out — including which guard is actually
   load-bearing, and how that was verified — in `resolve_credit_transfer`.

MUTATIONS ARE NOT USED HERE
---------------------------
Every write goes through DML (`insert_entity_dml_at`, `update_entity_body_dml`,
`delete_entity_dml`) rather than the store's mutation helpers. Spanner forbids
mixing DML and mutations in one transaction, and each transition here MUST pair
its insert-once row with its balance change in a single transaction — that
pairing is the whole conservation argument. `insert_entity_dml_at` (client
timestamp, not PENDING_COMMIT_TIMESTAMP) is used because these transactions
write more than one `tr_entities` row.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from trusted_router import credit_transfer
from trusted_router.credit_transfer import (
    CreditTransferConflict,
    validate_amount,
    validate_outcome,
    validate_transfer_id,
)
from trusted_router.storage_codec import json_body as _json_body
from trusted_router.storage_errors import is_duplicate_key_error
from trusted_router.storage_gcp_counter_dml import (
    credit_credit_shard,
    debit_credit_shard,
    delete_entity_dml,
    insert_entity_dml_at,
    update_entity_body_dml,
)
from trusted_router.storage_gcp_counters import (
    credit_shard_count,
    distribute_credit_amount,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_trust import insert_credit_trust_event
from trusted_router.storage_models import CreditAccount, CreditProvenance, CreditTransfer, iso_now
from trusted_router.trust_tiers import payment_or_grant_event

# Entity kinds. Deliberately identical to the Postgres backend's so an operator
# reading either plane's `tr_entities` sees one vocabulary.
CREDIT_TRANSFER_KIND = "credit_transfer"
#: Bounded recovery queue: written at escrow, DELETED at resolution, so "which
#: transfers are still unresolved?" is a PK-prefix range that shrinks to empty.
CREDIT_TRANSFER_OPEN_KIND = "credit_transfer_open"
#: DESTINATION side. Insert-once; the row IS the verdict and is never rewritten.
CREDIT_TRANSFER_CLAIM_KIND = "credit_transfer_claim"
#: SOURCE side. Insert-once; the row IS the authority to apply a verdict's
#: balance change, exactly once.
CREDIT_TRANSFER_RESOLUTION_KIND = "credit_transfer_resolution"

log = logging.getLogger(__name__)


class EscrowPlanError(RuntimeError):
    """A guarded shard DML in a completed plan did not affect exactly one row.

    Raised only when the transaction's own read said the plan was affordable
    and a write then disagreed — i.e. state moved under us in a way the
    read-set check should have caught. Raising rolls the transaction back, so
    the sum of ``total_credits`` cannot change on this path.
    """


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _shard_count_tx(
    transaction: Any, read_entity_tx: Any, workspace_id: str
) -> int | None:
    """The workspace's authoritative shard count, read INSIDE the transaction.
    None when the workspace has no credit account on this plane.

    Deliberately not `CreditShardCountCache`: that cache is explicitly
    allow-stale, which is fine for choosing which shard to TRY but not for
    deciding which rows constitute the whole balance. A stale-low count here
    would make the affordability sum ignore real shards (refusing a transfer
    the customer can afford, or worse, planning a debit against a subset while
    calling it the total).

    "Missing" is returned rather than raised because the three callers owe the
    caller three DIFFERENT errors for it, and those are contract, not taste —
    `storage_postgres.py` and `storage.py` both already spell them this way and
    one conformance suite holds all three backends to them:

      * open   -> ValueError("insufficient credits"). A workspace with no
        balance can afford nothing; Postgres reaches this as a 0-row
        conditional UPDATE and the in-memory store as `money is None`.
      * claim  -> ValueError("no credit balance for workspace ... on this
        plane"), which the push protocol reads as "not federated here yet, keep
        the value escrowed and retry" rather than as a refusal.
      * resolve's refund -> RuntimeError. The escrow was debited from a balance
        that has since vanished, so this is corruption, not a business outcome.
    """
    account = read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
    if account is None:
        return None
    return credit_shard_count(account)


def _read_shard_headroom(
    transaction: Any, param_types: Any, workspace_id: str, shard_count: int
) -> dict[int, int] | None:
    """Per-shard ``total_credits - total_usage - reserved``, or None if the
    shard set is incomplete.

    An incomplete set fails CLOSED (None). A missing shard row is not a shard
    with zero budget — it is a balance we cannot account for, and planning a
    debit against the shards that happen to exist would be spending against an
    unknown total. Same predicate and same verdict as `rebalance_precheck`.

    An incomplete set is LOGGED, because the caller cannot distinguish it from
    a genuine refusal and the operator would otherwise never learn. `open`'s
    error contract is fixed by the conformance suite at
    ValueError("insufficient credits") -> HTTP 402, and it must stay that way:
    all three backends have to answer alike, and Postgres reaches the same
    verdict as a 0-row conditional UPDATE. But 402 tells an operator the
    customer is broke, so a damaged balance table reads as insolvency — they
    top the workspace up, get 402 again, and nothing anywhere says why. The
    sibling rebalancer separates INCOMPLETE from INSUFFICIENT for this exact
    reason (`storage_gcp_credit_rebalance.py`), and the authorize path raises
    on it outright (`storage_gcp.py`). Nothing is at risk either way — this
    path fails closed and moves no money — so the fix is the missing signal,
    not a different error.
    """
    rows = list(
        transaction.execute_sql(
            "SELECT shard, total_credits, total_usage, reserved "
            "FROM tr_credit_balance WHERE workspace_id=@pk "
            "AND shard>=0 AND shard<@shard_count ORDER BY shard",
            params={"pk": workspace_id, "shard_count": shard_count},
            param_types={"pk": param_types.STRING, "shard_count": param_types.INT64},
        )
    )
    observed = [int(row[0]) for row in rows]
    if observed != list(range(shard_count)):
        log.error(
            "credit transfer escrow refused: workspace %s has an INCOMPLETE "
            "tr_credit_balance shard set (configured shard_count=%d, present "
            "shards=%s). This is balance-table drift, NOT insufficient credits "
            "— the caller is told 402 because the cross-backend error contract "
            "requires it, and topping the workspace up will not clear it.",
            workspace_id,
            shard_count,
            observed,
        )
        return None
    return {
        int(shard): int(total_credits) - int(total_usage) - int(reserved)
        for shard, total_credits, total_usage, reserved in rows
    }


def _debit_escrow(
    transaction: Any,
    param_types: Any,
    *,
    workspace_id: str,
    amount: int,
    shard_count: int,
) -> bool:
    """Conditionally debit `amount` from the SHARDED balance. False = refused.

    Affordability is the SIGNED sum over every shard, negatives included — the
    same rule `rebalance_credit_for_estimate` uses and for the same reason: an
    over-spent shard's debt must count against what the workspace can move, or
    a transfer could drain the healthy shards while the workspace is globally
    overdrawn. Donors below contribute only POSITIVE headroom, and when the
    signed sum passes, positive headroom provably covers `amount`, so a plan
    that starts always completes.
    """
    headroom = _read_shard_headroom(transaction, param_types, workspace_id, shard_count)
    if headroom is None or sum(headroom.values()) < amount:
        return False

    remaining = amount
    donors = sorted(
        ((available, shard) for shard, available in headroom.items() if available > 0),
        reverse=True,
    )
    for available, shard in donors:
        take = min(available, remaining)
        if not debit_credit_shard(
            transaction, param_types, workspace_id, take, shard=shard
        ):
            raise EscrowPlanError(
                f"credit shard {shard} changed under an escrow debit for "
                f"workspace {workspace_id}"
            )
        remaining -= take
        if remaining == 0:
            break
    if remaining != 0:  # pragma: no cover - guarded by the signed-sum check above
        raise EscrowPlanError("escrow debit plan did not satisfy the amount")
    return True


def _credit_across_shards(
    transaction: Any,
    param_types: Any,
    *,
    workspace_id: str,
    amount: int,
    shard_count: int,
    now: dt.datetime,
    missing: Callable[[], Exception],
) -> None:
    """Add `amount` back to the sharded balance, spread by the standard rule.

    The spread need not mirror how the debit was taken: only the SUM is the
    conserved quantity, and per-shard skew is exactly what the shard rebalancer
    exists to correct. `distribute_credit_amount` totals to `amount` by
    construction, so this returns precisely what escrow removed.

    Every shard must exist; a 0 row-count raises `missing`, rolling back the
    verdict row with it, because a refund that lands nowhere would destroy the
    escrow. The exception TYPE is the caller's to choose (see `_shard_count_tx`),
    so it is passed in rather than decided here.
    """
    for shard, delta in enumerate(distribute_credit_amount(amount, shard_count)):
        if (
            credit_credit_shard(
                transaction, param_types, workspace_id, delta, shard=shard, now=now
            )
            != 1
        ):
            raise missing()


# --------------------------------------------------------------------------
# The five Store methods
# --------------------------------------------------------------------------


def open_credit_transfer(
    *,
    database: Any,
    param_types: Any,
    read_entity_tx: Any,
    transfer_id: str,
    workspace_id: str,
    amount_microdollars: int,
    destination: str,
) -> CreditTransfer:
    """SOURCE side: debit into escrow. Value becomes held by THIS plane.

    Idempotent on `transfer_id`: a redelivered open returns the existing record
    and debits nothing. Raises ValueError("insufficient credits") when the
    workspace cannot cover the amount.

    The insert-once row goes in FIRST and the debit second, both in one
    transaction, so a retry re-runs an insert that loses and therefore moves
    nothing. A refused debit rolls the inserts back too, leaving the id free
    for a genuine retry after a top-up — "a refused transfer must leave no
    trace", which the conformance suite checks directly.
    """
    transfer_id = validate_transfer_id(transfer_id)
    amount = validate_amount(amount_microdollars)
    transfer = CreditTransfer(
        id=transfer_id,
        workspace_id=workspace_id,
        amount_microdollars=amount,
        destination=str(destination or ""),
        state=credit_transfer.ESCROWED,
    )

    def escrow(transaction: Any) -> CreditTransfer:
        now = _now()
        # Insert-once FIRST: ALREADY_EXISTS here means somebody already owns
        # this id, and the debit below must not run. Spanner invalidates a
        # transaction whose DML failed, so the duplicate is handled by the
        # replay transaction outside rather than by continuing in this one —
        # the same shape `storage_gcp_authorize` uses for its idempotency race.
        insert_entity_dml_at(
            transaction,
            param_types,
            CREDIT_TRANSFER_KIND,
            transfer_id,
            _json_body(transfer),
            now,
        )
        insert_entity_dml_at(
            transaction,
            param_types,
            CREDIT_TRANSFER_OPEN_KIND,
            transfer_id,
            _json_body({"transfer_id": transfer_id}),
            now,
        )
        shard_count = _shard_count_tx(transaction, read_entity_tx, workspace_id)
        # A workspace with no credit account can afford nothing. Postgres
        # reaches this same verdict as a 0-row conditional UPDATE and the
        # in-memory store as `money is None`; all three must raise the SAME
        # error, because one conformance suite asserts it for all of them.
        if shard_count is None or not _debit_escrow(
            transaction,
            param_types,
            workspace_id=workspace_id,
            amount=amount,
            shard_count=shard_count,
        ):
            raise ValueError("insufficient credits")
        return transfer

    def replay(transaction: Any) -> CreditTransfer:
        existing = read_entity_tx(
            transaction, CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer
        )
        if existing is None:  # pragma: no cover - winner must exist post-conflict
            raise RuntimeError("credit transfer row disappeared after conflict")
        # An id is not an agreement. Handing back a transfer escrowed for
        # destination A to a caller holding a client for destination B lets
        # push/cancel ask the WRONG plane to rule on value held for A, and a
        # REJECTED tombstone from B releases an escrow A may already have
        # accepted. Workspace and amount are checked for the same reason: an
        # operator funding workspace B with an id already spent on A would
        # otherwise be told "delivered" for a transfer that moved nothing.
        credit_transfer.require_matching_transfer(
            transfer_id,
            existing,
            workspace_id=workspace_id,
            amount_microdollars=amount,
            destination=destination,
        )
        return existing

    try:
        return run_in_transaction_with_retry(database, escrow)
    except Exception as exc:
        if not is_duplicate_key_error(exc):
            raise
        return run_in_transaction_with_retry(database, replay)


def get_credit_transfer(
    *, read_entity: Any, transfer_id: str
) -> CreditTransfer | None:
    return read_entity(CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer)


def list_open_credit_transfers(
    *,
    database: Any,
    param_types: Any,
    read_entity_tx: Any,
    limit: int = 100,
    after_id: str = "",
) -> list[CreditTransfer]:
    """Transfers still in ESCROWED — the recovery queue.

    PAGED, because not every row leaves the queue by being resolved. A transfer
    escrowed for a DIFFERENT destination is skipped on every recovery pass and
    stays in the index; once `limit` of those sort ahead of the live escrows,
    an unpaged "first N" would return nothing but skips forever and silently
    stop recovering anything else. `after_id` lets the driver walk past them.
    """
    bounded = max(1, min(int(limit), 500))
    cursor_id = str(after_id or "")

    def read(transaction: Any) -> list[CreditTransfer]:
        rows = list(
            transaction.execute_sql(
                "SELECT id FROM tr_entities WHERE kind=@kind AND id>@after "
                "ORDER BY id LIMIT @limit",
                params={
                    "kind": CREDIT_TRANSFER_OPEN_KIND,
                    "after": cursor_id,
                    "limit": bounded,
                },
                param_types={
                    "kind": param_types.STRING,
                    "after": param_types.STRING,
                    "limit": param_types.INT64,
                },
            )
        )
        transfers: list[CreditTransfer] = []
        for row in rows:
            entity_id = str(row[0])
            transfer = read_entity_tx(
                transaction, CREDIT_TRANSFER_KIND, entity_id, CreditTransfer
            )
            if transfer is not None and transfer.state == credit_transfer.ESCROWED:
                transfers.append(transfer)
                continue
            # An index row whose transfer is resolved (or absent) is garbage:
            # resolve deletes both in one transaction, so this only exists after
            # a partial repair. Dropping it keeps "a row in the index means an
            # escrowed transfer is returned" true, which is what lets the driver
            # page on the last id without a filtered-out row stalling the walk.
            delete_entity_dml(
                transaction, param_types, CREDIT_TRANSFER_OPEN_KIND, entity_id
            )
        return transfers

    return run_in_transaction_with_retry(database, read)


def resolve_credit_transfer(
    *,
    database: Any,
    param_types: Any,
    read_entity_tx: Any,
    transfer_id: str,
    outcome: str,
) -> CreditTransfer:
    """SOURCE side: record the DESTINATION's verdict, and only that.

    ACCEPTED -> DELIVERED: the destination holds the value; this plane's balance
    is untouched (it was debited at escrow). REJECTED -> RETURNED: the escrowed
    amount is credited back here in the same transaction that records the
    state, so it cannot be returned twice. A repeat of the SAME verdict is a
    no-op; a DISAGREEING one raises rather than applying a second change.

    WHY THE DOUBLE-REFUND WINDOW IS CLOSED WITHOUT ``FOR UPDATE``
    ------------------------------------------------------------
    The Postgres path takes the escrow read ``FOR UPDATE`` because two resolvers
    that both read ESCROWED would both fall through and both refund — which
    MINTS. The hazard is real here too (an operator cancel racing the recovery
    pass is a supported, documented interleaving), but Spanner has no
    ``SELECT ... FOR UPDATE`` to copy, so the guarantee has to be re-derived
    rather than transliterated. It is NOT the transaction wrapper: retrying
    ABORTED guarantees nothing by itself, since two transactions touching
    disjoint keys both commit happily.

    What closes it is that this transaction READS the ``credit_transfer`` row
    and then REWRITES it. The read puts the row in the transaction's read set;
    the winner's commit bumps its version, so every loser fails Spanner's
    commit-time read-set validation, aborts, and — this is the load-bearing
    part — `run_in_transaction_with_retry` RE-INVOKES this whole function. The
    retry re-reads the transfer, now DELIVERED or RETURNED, and takes the
    terminal branch above. The stale decision is not merely delayed, it is
    recomputed.

    That is a strictly stronger primitive than Postgres offers, and it is why
    the two backends need different guards for the same hazard. Under Postgres
    READ COMMITTED the loser's UPDATE only BLOCKS and then proceeds against the
    new row version, while the Python-level `state == ESCROWED` decision made
    from the old snapshot still stands — nothing forces a re-read, so Postgres
    genuinely needs `FOR UPDATE` plus the insert-once row. Spanner aborts and
    re-runs instead, which re-derives the decision from committed state.

    THE INSERT-ONCE ``credit_transfer_resolution`` ROW IS THEREFORE NOT WHAT
    SAVES THIS PATH ON SPANNER, and saying otherwise would be a comfortable
    lie. Verified by mutation, not by reasoning alone: deleting that insert
    leaves `tests/test_credit_transfer_spanner.py` — including the genuinely
    contending four-resolver race, which is asserted to produce real aborts —
    entirely green. It is kept for three reasons that are worth its cost:

      * it is the record `replay` below reads to learn the winner's verdict —
        but note that on THIS backend `replay` is effectively unreachable, and
        it would be another comfortable lie to leave that unsaid. A duplicate
        resolve is answered by the terminal-state branch above, not by the
        duplicate-key path: a loser aborts on the transfer row and re-runs, so
        it re-reads a row that is already DELIVERED or RETURNED. Measured, not
        assumed — 25 six-way accept-vs-reject races (150 resolvers, 125 real
        aborts) entered the duplicate-key branch ZERO times. Reaching it needs
        a resolution row whose transfer row is somehow still ESCROWED, i.e.
        prior corruption. This matters because the row IS genuinely the guard
        on Postgres (`storage_postgres.py`), so identical vocabulary across the
        two planes hides the fact that only one of them leans on it;
      * it keeps this plane's `tr_entities` vocabulary identical to the
        Postgres plane's, which one conformance suite and any cross-plane audit
        both depend on;
      * it is the only guard that survives a future refactor moving the
        transfer-row rewrite out of this transaction. That rewrite is load-
        bearing today and nothing else in the file says so — which is precisely
        why removing this row must stay a deliberate act, not a tidy-up.
    """
    transfer_id = validate_transfer_id(transfer_id)
    outcome = validate_outcome(outcome)
    target_state = credit_transfer.STATE_FOR_OUTCOME[outcome]

    def resolve(transaction: Any) -> CreditTransfer:
        now = _now()
        existing = read_entity_tx(
            transaction, CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer
        )
        if existing is None:
            raise KeyError(transfer_id)
        if existing.state != credit_transfer.ESCROWED:
            if existing.state != target_state:
                raise CreditTransferConflict(
                    f"transfer {transfer_id} is {existing.state}; "
                    f"cannot re-resolve it as {target_state}"
                )
            return existing
        resolved = dataclasses.replace(
            existing, state=target_state, resolved_at=iso_now()
        )
        # Insert-once FIRST, exactly as at escrow: if this loses, the balance
        # change below must not run.
        insert_entity_dml_at(
            transaction,
            param_types,
            CREDIT_TRANSFER_RESOLUTION_KIND,
            transfer_id,
            _json_body(
                {
                    "outcome": outcome,
                    "workspace_id": existing.workspace_id,
                    "amount_microdollars": existing.amount_microdollars,
                    "resolved_at": resolved.resolved_at,
                }
            ),
            now,
        )
        update_entity_body_dml(
            transaction,
            param_types,
            CREDIT_TRANSFER_KIND,
            transfer_id,
            _json_body(resolved),
            now,
        )
        # Leaves the recovery queue only now: while this row exists the fate is
        # unknown and a recovery pass must keep asking.
        delete_entity_dml(
            transaction, param_types, CREDIT_TRANSFER_OPEN_KIND, transfer_id
        )
        if outcome == credit_transfer.REJECTED:
            def balance_vanished() -> Exception:
                return RuntimeError(
                    "missing authoritative tr_credit_balance for "
                    f"workspace {existing.workspace_id}"
                )

            refund_shards = _shard_count_tx(
                transaction, read_entity_tx, existing.workspace_id
            )
            if refund_shards is None:
                raise balance_vanished()
            _credit_across_shards(
                transaction,
                param_types,
                workspace_id=existing.workspace_id,
                amount=existing.amount_microdollars,
                shard_count=refund_shards,
                now=now,
                missing=balance_vanished,
            )
        return resolved

    def replay(transaction: Any) -> CreditTransfer:
        existing = read_entity_tx(
            transaction, CREDIT_TRANSFER_KIND, transfer_id, CreditTransfer
        )
        if existing is None:  # pragma: no cover - it existed a moment ago
            raise KeyError(transfer_id)
        recorded = read_entity_tx(
            transaction, CREDIT_TRANSFER_RESOLUTION_KIND, transfer_id, dict
        )
        if recorded is None:  # pragma: no cover - winner must exist post-conflict
            raise RuntimeError("credit transfer resolution disappeared")
        decided = str(recorded["outcome"])
        if decided != outcome:
            raise CreditTransferConflict(
                f"transfer {transfer_id} was already resolved as "
                f"{decided}; cannot re-resolve it as {outcome}"
            )
        # Same verdict, already applied by the winner. Report the settled shape
        # without touching a balance.
        return dataclasses.replace(
            existing,
            state=credit_transfer.STATE_FOR_OUTCOME[decided],
            resolved_at=str(recorded.get("resolved_at") or "") or None,
        )

    try:
        return run_in_transaction_with_retry(database, resolve)
    except Exception as exc:
        if not is_duplicate_key_error(exc):
            raise
        return run_in_transaction_with_retry(database, replay)


def claim_credit_transfer(
    *,
    database: Any,
    param_types: Any,
    read_entity_tx: Any,
    transfer_id: str,
    workspace_id: str,
    amount_microdollars: int,
    source: str,
    accept: bool,
) -> str:
    """DESTINATION side: decide a transfer's fate, exactly once.

    Returns the DECIDED outcome, which may differ from `accept` — the first
    writer wins and every later caller learns that verdict instead of overriding
    it. That single insert-once row is what makes a duplicate delivery credit
    once, and what makes an accept that races a cancel resolve one way for both
    planes. On ACCEPTED the local balance is credited in the same transaction as
    the row, so the row existing and the money existing are one fact.
    """
    transfer_id = validate_transfer_id(transfer_id)
    amount = validate_amount(amount_microdollars)
    requested = credit_transfer.ACCEPTED if accept else credit_transfer.REJECTED

    def claim(transaction: Any) -> str:
        now = _now()
        insert_entity_dml_at(
            transaction,
            param_types,
            CREDIT_TRANSFER_CLAIM_KIND,
            transfer_id,
            _json_body(
                {
                    "outcome": requested,
                    "workspace_id": workspace_id,
                    "amount_microdollars": amount,
                    "source": str(source or ""),
                    "created_at": iso_now(),
                }
            ),
            now,
        )
        if requested == credit_transfer.REJECTED:
            return credit_transfer.REJECTED
        # A missing balance (or missing shard row) means no such workspace
        # here. `_credit_across_shards` and `_shard_count_tx` both raise
        # ValueError, rolling back the claim row too, so the source can retry
        # once the workspace has been federated rather than being told the
        # transfer was accepted by a plane that never credited it.
        def not_on_this_plane() -> Exception:
            return ValueError(
                f"no credit balance for workspace {workspace_id} on this plane"
            )

        claim_shards = _shard_count_tx(transaction, read_entity_tx, workspace_id)
        if claim_shards is None:
            raise not_on_this_plane()
        _credit_across_shards(
            transaction,
            param_types,
            workspace_id=workspace_id,
            amount=amount,
            shard_count=claim_shards,
            now=now,
            missing=not_on_this_plane,
        )
        insert_credit_trust_event(
            transaction,
            param_types,
            payment_or_grant_event(
                workspace_id,
                transfer_id,
                amount,
                CreditProvenance(
                    source="grant",
                    provider="system",
                    external_ref=None,
                    occurred_at=now,
                ),
                recorded_at=now,
            ),
        )
        return credit_transfer.ACCEPTED

    def replay(transaction: Any) -> str:
        recorded = read_entity_tx(
            transaction, CREDIT_TRANSFER_CLAIM_KIND, transfer_id, dict
        )
        if recorded is None:  # pragma: no cover - winner must exist post-conflict
            raise RuntimeError("credit transfer claim disappeared after conflict")
        # The recorded verdict answers for the (workspace, amount, source) it
        # was written with, and for NO other. Replaying it blindly is how a
        # second source plane gets "accepted" for free: it debited, nothing here
        # was credited, and both planes report success. Two AWS regions pushing
        # an operator-chosen id like "topup-2026-08" is enough to reach it.
        credit_transfer.require_matching_transfer(
            transfer_id,
            recorded,
            workspace_id=workspace_id,
            amount_microdollars=amount,
            source=str(source or ""),
        )
        return str(recorded["outcome"])

    try:
        return run_in_transaction_with_retry(database, claim)
    except Exception as exc:
        if not is_duplicate_key_error(exc):
            raise
        return run_in_transaction_with_retry(database, replay)
