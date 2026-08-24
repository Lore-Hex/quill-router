"""The fake transaction must give every statement ONE consistent row view.

Real Spanner read-write transactions are serializable: a concurrent commit
that invalidates what a transaction already read surfaces as Aborted at
commit time — never as a row changing value between two statements of the
same transaction. The fake models that with version validation at commit
plus row snapshots pinned at first read.

Without the pinned snapshots, a commit landing between a plan read and its
guarded DML made the guard evaluate against FRESHER state than the plan —
a mid-transaction inconsistency real Spanner cannot produce. Production's
rebalance turned exactly that into a phantom _RebalanceInvariantError
("credit shard changed or disappeared during rebalance"), seen as a rare
credit-shard stress flake under coverage on CI.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store

_DONOR_TRANSFER_SQL = (
    "UPDATE tr_credit_balance SET total_credits=total_credits-@move "
    "WHERE workspace_id=@ws AND shard=@donor "
    "AND (total_credits-total_usage-reserved)>=@move"
)


def _seed_shard(db, ws: str, shard: int, credits: int, usage: int) -> None:
    db.typed.setdefault("tr_credit_balance", {})[(ws, shard)] = {
        "workspace_id": ws,
        "shard": shard,
        "total_credits": credits,
        "total_usage": usage,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


def test_guarded_dml_sees_the_txn_pinned_view_and_conflict_aborts() -> None:
    store, db, _bt = make_fake_store()
    ws = "ws-txn-consistency"
    _seed_shard(db, ws, 0, credits=1_000, usage=0)

    pk = (ws, 0)
    attempts: list[int] = []

    def txn_body(txn):
        attempt = len(attempts)
        attempts.append(attempt)
        # Statement 1: the "plan read" pins the row (headroom 1000).
        rec = txn._typed_current("tr_credit_balance", pk)
        assert rec is not None
        if attempt == 0:
            assert rec["total_usage"] == 0
            # A concurrent transaction commits a usage bump between the plan
            # read and the guarded DML (headroom drops to 400).
            committed = db.typed["tr_credit_balance"][pk]
            committed["total_usage"] = 600
            db.typed_versions[("tr_credit_balance", pk)] = (
                db.typed_versions.get(("tr_credit_balance", pk), 0) + 1
            )
        # Statement 2: guarded DML planned against the FIRST read. On attempt 0
        # it must evaluate against the pinned view (headroom 1000) and succeed
        # in-txn; the external commit then surfaces as an ABORT at commit, not
        # as a mid-txn guard failure. On the retry the fresh read sees usage
        # 600 and a smaller move still fits.
        move = 500 if attempt == 0 else 300
        updated = txn.execute_update(
            _DONOR_TRANSFER_SQL,
            params={"move": move, "ws": ws, "donor": 0},
            param_types=None,
        )
        assert updated == 1, (
            f"attempt {attempt}: guarded DML must see the transaction's pinned "
            "view; a mid-txn guard failure is a phantom real Spanner cannot "
            "produce"
        )
        return updated

    assert db.run_in_transaction(txn_body) == 1
    # The conflict was surfaced as serializable abort-and-retry.
    assert len(attempts) == 2
    assert db.aborts == 1
    # Final state composes BOTH writes: the concurrent usage bump and the
    # retry's smaller transfer, with no lost update in either direction.
    final = db.typed["tr_credit_balance"][pk]
    assert final["total_usage"] == 600
    assert final["total_credits"] == 700


def test_repeat_read_in_one_txn_is_stable_despite_concurrent_commit() -> None:
    store, db, _bt = make_fake_store()
    ws = "ws-repeat-read"
    _seed_shard(db, ws, 0, credits=1_000, usage=0)
    pk = (ws, 0)
    ran = {"n": 0}

    def txn_body(txn):
        ran["n"] += 1
        if ran["n"] > 1:
            # Retry after the abort: fresh view, no further interference.
            return txn._typed_current("tr_credit_balance", pk)["total_usage"]
        first = txn._typed_current("tr_credit_balance", pk)
        committed = db.typed["tr_credit_balance"][pk]
        committed["total_usage"] = 999
        db.typed_versions[("tr_credit_balance", pk)] = (
            db.typed_versions.get(("tr_credit_balance", pk), 0) + 1
        )
        second = txn._typed_current("tr_credit_balance", pk)
        assert second["total_usage"] == first["total_usage"] == 0, (
            "repeatable read violated: one transaction observed two states"
        )
        return second["total_usage"]

    # The read-only body still aborts at commit (version drift) and retries.
    assert db.run_in_transaction(txn_body) == 999
    assert db.aborts == 1


def test_mf2_guard_zero_count_aborts_when_enqueue_commits_before_claim() -> None:
    """The MF2 lost-charge interlock's actual serialization race.

    settle_atomic's in-txn guard count reads an authorization's outbox rows —
    including their ABSENCE. If an enqueue commits between the zero-count and
    the claim commit, real Spanner aborts the claim; per-row versions cannot
    express "no row existed", so the fake tracks a per-authorization range
    version. Without it, the claim committed against a guard it never saw."""
    from trusted_router.storage_gcp_settle_outbox import GUARD_COUNT_SQL

    store, db, _bt = make_fake_store()
    aid = "gwa-mf2-race"
    counts: list[int] = []

    def txn_body(txn):
        attempt = len(counts)
        rows = list(
            txn.execute_sql(GUARD_COUNT_SQL, params={"aid": aid}, param_types=None)
        )
        counts.append(int(rows[0][0]))
        if attempt == 0:
            assert counts[0] == 0, "precondition: no guard row at first read"
            # Concurrent enqueue commits AFTER the zero-count, BEFORE our commit.
            pk = (aid, "settle")
            db.settle_outbox[pk] = {
                "authorization_id": aid,
                "intent_kind": "settle",
                "status": "pending",
            }
            db._global_version += 1
            db.settle_outbox_versions[pk] = db._global_version
            db.settle_outbox_auth_versions[aid] = db._global_version
        # Stand-in for the reservation claim write: any buffered write makes
        # this a read-write commit subject to validation.
        txn.pending_writes.append(
            (
                "update_typed",
                "tr_credit_balance",
                ("ws-mf2", 0),
                {
                    "workspace_id": "ws-mf2",
                    "shard": 0,
                    "total_credits": 1,
                    "total_usage": 0,
                    "reserved": 0,
                },
            )
        )
        return counts[-1]

    # The retry must OBSERVE the enqueue the first attempt raced with.
    assert db.run_in_transaction(txn_body) == 1
    assert counts == [0, 1]
    assert db.aborts == 1
