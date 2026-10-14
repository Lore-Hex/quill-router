"""Each pause cause stays where its owner writes it.

The legacy "migration" quiesce (``update_workspace(billing_paused=...)``) is
owned by the workspace document. The abuse latch and principal recovery write
the replicated shard set and rebuild the document from it. These tests pin,
per backend and against the persisted rows, that such a rebuild keeps the
document's "migration", never puts it on the shards (where the document-only
unpause could not remove it), and drops a copy an earlier writer left there
without resurrecting a pause the document no longer holds.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router import storage_gcp
from trusted_router.storage_gcp_counter_dml import release_credit
from trusted_router.storage_legacy_trust import postgres_pause, recover_released_postgres
from trusted_router.storage_models import (
    AdverseTrustEvent,
    CreditProvenance,
    document_pause_causes,
    shard_pause_causes,
)

NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.UTC)
SHARDS = 4


def _refund(amount: int) -> AdverseTrustEvent:
    # A refund claims principal pro rata (a dispute claims all of it), so a
    # small refund leaves a debt one small release can settle.
    return AdverseTrustEvent(
        event_id="evt:re_pause",
        provider="stripe",
        kind="refund",
        adverse_ref="re_pause",
        original_payment_ref="pi_pause",
        amount_micro=amount,
        provider_subtype="charge.refund.updated",
        lifecycle_status="succeeded",
        occurred_at=NOW,
        provider_ordering_watermark="00000000000000000001:evt_1",
        payload="{}",
    )


def test_the_document_owns_migration_and_the_shards_never_hold_it() -> None:
    assert shard_pause_causes(["migration", "abuse", "abuse"]) == ["abuse"]
    # A copy left on the shards neither survives there nor decides the document.
    assert document_pause_causes(["abuse", "migration"], ["abuse"]) == ["abuse"]
    assert document_pause_causes(["abuse"], ["migration", "resharding"]) == [
        "abuse",
        "migration",
    ]
    assert document_pause_causes([], ["migration"]) == ["migration"]


def _latch(store: Any, workspace_id: str) -> None:
    assert store.record_workspace_abuse_and_demote(
        workspace_id, abuse_ref="latch", operator_identity="ops@example.com", reason="abuse"
    )


def _clear(store: Any, workspace_id: str) -> None:
    assert store.clear_workspace_abuse_pause(
        workspace_id, abuse_ref="clear", operator_identity="ops@example.com", reason="appeal"
    )


def _latch_and_clear(store: Any, workspace_id: str, *, migration_first: bool) -> None:
    if migration_first:
        store.update_workspace(workspace_id, billing_paused=True)
    _latch(store, workspace_id)
    if not migration_first:
        store.update_workspace(workspace_id, billing_paused=True)
    _clear(store, workspace_id)


def _pay(
    store: Any,
    workspace_id: str,
    event_id: str = "evt_payment",
    amount: int = 1_000,
    payment_ref: str = "pi_pause",
) -> None:
    assert store.credit_workspace_typed_direct(
        workspace_id,
        amount,
        event_id,
        provenance=CreditProvenance("checkout", "stripe", payment_ref, NOW),
        payment_amount_microdollars=amount,
        currency="usd",
    )


# --- Spanner -----------------------------------------------------------------


@pytest.fixture
def spanner(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, str]:
    monkeypatch.setattr(storage_gcp, "DEFAULT_NEW_BILLING_SHARDS", SHARDS)
    store, database = make_fake_store()
    workspace = store.create_workspace("owner", "pause", trial_credit_microdollars=0)
    return store, database, workspace.id


def _spanner_shards(database: Any, workspace_id: str) -> list[dict[str, Any]]:
    return [
        row
        for (owner, _shard), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    ]


def _spanner_document(database: Any, workspace_id: str) -> dict[str, Any]:
    return dict(json.loads(database.rows[("workspace", workspace_id)].body))


def _assert_spanner(database: Any, workspace_id: str, *, document: list[str], shards: list[str]) -> None:
    rows = _spanner_shards(database, workspace_id)
    assert len(rows) == SHARDS
    assert [list(row["billing_pause_causes"] or []) for row in rows] == [shards] * SHARDS
    body = _spanner_document(database, workspace_id)
    # The stored body omits empty fields; read them as their defaults.
    assert body.get("billing_pause_causes", []) == document
    assert body.get("billing_paused", False) is bool(document)


@pytest.mark.parametrize("migration_first", [True, False])
def test_spanner_abuse_clear_keeps_migration_on_the_document_only(
    spanner: tuple[Any, Any, str], migration_first: bool
) -> None:
    store, database, workspace_id = spanner

    _latch_and_clear(store, workspace_id, migration_first=migration_first)

    _assert_spanner(database, workspace_id, document=["migration"], shards=[])
    store.update_workspace(workspace_id, billing_paused=False)
    _assert_spanner(database, workspace_id, document=[], shards=[])


def test_spanner_clear_drops_a_copied_migration_without_resurrecting_it(
    spanner: tuple[Any, Any, str],
) -> None:
    store, database, workspace_id = spanner
    _latch(store, workspace_id)
    # An earlier writer copied "migration" onto the shards; the document's own
    # pause has since been lifted.
    for row in _spanner_shards(database, workspace_id):
        row["billing_pause_causes"] = ["abuse", "migration"]

    _clear(store, workspace_id)

    _assert_spanner(database, workspace_id, document=[], shards=[])


def test_spanner_recovery_through_the_entity_reader_keeps_migration(
    spanner: tuple[Any, Any, str],
) -> None:
    store, database, workspace_id = spanner
    _pay(store, workspace_id)
    for row in _spanner_shards(database, workspace_id):
        row["total_usage"] = row["total_credits"]
    store.update_workspace(workspace_id, billing_paused=True)
    assert store.record_adverse_trust_event(_refund(50)).unrecovered_micro == 50
    _assert_spanner(
        database, workspace_id, document=["migration", "principal_recovery"],
        shards=["principal_recovery"],
    )

    _pay(store, workspace_id, "evt_later_payment", 1_200, "pi_later")

    _assert_spanner(database, workspace_id, document=["migration"], shards=[])


def test_spanner_recovery_on_the_settle_path_keeps_migration(
    spanner: tuple[Any, Any, str],
) -> None:
    """The settle hot path rewrites the document without an entity reader."""
    store, database, workspace_id = spanner
    _pay(store, workspace_id)
    for row in _spanner_shards(database, workspace_id):
        row["total_usage"] = row["total_credits"]
    shard = database.typed["tr_credit_balance"][(workspace_id, 0)]
    shard["reserved"] = 50
    shard["total_usage"] = shard["total_credits"] - 50
    store.update_workspace(workspace_id, billing_paused=True)
    assert store.record_adverse_trust_event(_refund(50)).unrecovered_micro == 50

    database.run_in_transaction(
        lambda transaction: release_credit(
            transaction, store._param_types, workspace_id, 50, 0, shard=0
        )
    )

    _assert_spanner(database, workspace_id, document=["migration"], shards=[])


# --- Postgres ----------------------------------------------------------------


@pytest.fixture
def postgres() -> tuple[Any, Any, str]:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "pause", trial_credit_microdollars=0)
    return store, conn, workspace.id


def _postgres_shards(conn: Any, workspace_id: str) -> list[list[str]]:
    rows = conn.execute(
        "SELECT billing_pause_causes FROM tr_credit_balance WHERE workspace_id = %s ORDER BY shard",
        (workspace_id,),
    ).fetchall()
    return [json.loads(row[0]) if isinstance(row[0], str) else list(row[0] or []) for row in rows]


def _assert_postgres(conn: Any, workspace_id: str, *, document: list[str], shards: list[str]) -> None:
    rows = _postgres_shards(conn, workspace_id)
    assert rows and rows == [shards] * len(rows)
    body = json.loads(
        conn.execute(
            "SELECT body FROM tr_entities WHERE kind = 'workspace' AND id = %s", (workspace_id,)
        ).fetchone()[0]
    )
    assert body.get("billing_pause_causes", []) == document
    assert body.get("billing_paused", False) is bool(document)


@pytest.mark.parametrize("migration_first", [True, False])
def test_postgres_abuse_clear_never_copies_migration_onto_the_shards(
    postgres: tuple[Any, Any, str], migration_first: bool
) -> None:
    store, conn, workspace_id = postgres

    _latch_and_clear(store, workspace_id, migration_first=migration_first)

    _assert_postgres(conn, workspace_id, document=["migration"], shards=[])
    store.update_workspace(workspace_id, billing_paused=False)
    _assert_postgres(conn, workspace_id, document=[], shards=[])
    assert store._run_transaction(lambda c: postgres_pause(c, workspace_id))[0] is False


def test_postgres_clear_drops_a_copied_migration_without_resurrecting_it(
    postgres: tuple[Any, Any, str],
) -> None:
    store, conn, workspace_id = postgres
    _latch(store, workspace_id)
    conn.execute(
        "UPDATE tr_credit_balance SET billing_pause_causes = %s WHERE workspace_id = %s",
        (json.dumps(["abuse", "migration"]), workspace_id),
    )

    _clear(store, workspace_id)

    _assert_postgres(conn, workspace_id, document=[], shards=[])
    assert store._run_transaction(lambda c: postgres_pause(c, workspace_id))[0] is False


def test_postgres_recovery_keeps_migration_on_the_document_only(
    postgres: tuple[Any, Any, str],
) -> None:
    store, conn, workspace_id = postgres
    _pay(store, workspace_id)
    conn.execute(
        "UPDATE tr_credit_balance SET total_usage = total_credits WHERE workspace_id = %s",
        (workspace_id,),
    )
    store.update_workspace(workspace_id, billing_paused=True)
    assert store.record_adverse_trust_event(_refund(50)).unrecovered_micro == 50
    _assert_postgres(
        conn, workspace_id, document=["migration", "principal_recovery"],
        shards=["principal_recovery"],
    )

    conn.execute(
        "UPDATE tr_credit_balance SET total_usage = 0 WHERE workspace_id = %s",
        (workspace_id,),
    )
    store._run_transaction(lambda c: recover_released_postgres(c, workspace_id, store))

    _assert_postgres(conn, workspace_id, document=["migration"], shards=[])
