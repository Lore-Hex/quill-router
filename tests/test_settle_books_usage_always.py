"""Settle must book usage even when rows it expects have vanished.

THE BUG: `finalize_gateway_authorization`'s credit UPDATE had no rowcount
check and the whole booking block was skipped when the reservation entity was
missing. Either way the authorization was still marked settled — usage
silently vanished. `reserve()` proves the balance row existed at authorize
time, but retention jobs and cleanup run between reserve and settle, and a
ledger that loses debits when a row is missing is the "reports success
without measuring" failure shape this codebase keeps re-finding.

THE FIX: the booking is an upsert. A vanished balance row is recreated with
zero credits, so the spend lands as a negative balance — a visible, billable
fact — and a vanished reservation entity skips only the RELEASE (there is
nothing to release on a recreated row), never the booking.

These run against the SQLite psycopg fake, which executes the store's REAL
SQL — the guarantee under test lives in the statement text, not in Python.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn


@pytest.fixture
def harness() -> tuple[Any, Any]:
    conn = sqlite_postgres_conn()
    return postgres_store_on(conn), conn


WS = "ws-vanish-1"
KEY = "kh-vanish-1"


def _seed_balance(conn: Any, total_credits: int = 1_000_000) -> None:
    conn.execute(
        "INSERT INTO tr_credit_balance"
        " (workspace_id, shard, total_credits, total_usage, reserved, updated_at)"
        " VALUES (%s, 0, %s, 0, 0, CURRENT_TIMESTAMP)",
        (WS, total_credits),
    )


def _balance_row(conn: Any) -> dict[str, int] | None:
    row = conn.execute(
        "SELECT total_credits, total_usage, reserved FROM tr_credit_balance"
        " WHERE workspace_id = %s AND shard = 0",
        (WS,),
    ).fetchone()
    if row is None:
        return None
    return {"total_credits": row[0], "total_usage": row[1], "reserved": row[2]}


def _authorize_with_reservation(store: Any, estimate: int) -> Any:
    reservation = store.reserve(WS, KEY, estimate)
    return store.create_gateway_authorization(
        workspace_id=WS,
        key_hash=KEY,
        model_id="anthropic/claude-opus-4.7",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=estimate,
        credit_reservation_id=reservation.id,
    )


def test_normal_settle_books_and_releases_exactly(harness: tuple[Any, Any]) -> None:
    """The fix must not change the arithmetic of the healthy path."""
    store, conn = harness
    _seed_balance(conn)
    auth = _authorize_with_reservation(store, estimate=100)

    assert _balance_row(conn) == {
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 100,
    }

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=40, selected_usage_type="Credits"
    ) is True
    assert _balance_row(conn) == {
        "total_credits": 1_000_000,
        "total_usage": 40,
        "reserved": 0,
    }

    # Replay is a no-op: nothing double-booked, nothing double-released.
    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=40, selected_usage_type="Credits"
    ) is False
    assert _balance_row(conn) == {
        "total_credits": 1_000_000,
        "total_usage": 40,
        "reserved": 0,
    }


def test_settle_books_usage_when_balance_row_was_deleted(
    harness: tuple[Any, Any],
) -> None:
    """A retention job eats the balance row between reserve and settle.

    The spend must land anyway — as a NEGATIVE balance on a recreated row,
    which an operator can see and bill, not as a silent no-op that leaves
    the authorization "settled" and the money gone.
    """
    store, conn = harness
    _seed_balance(conn)
    auth = _authorize_with_reservation(store, estimate=100)

    conn.execute(
        "DELETE FROM tr_credit_balance WHERE workspace_id = %s AND shard = 0", (WS,)
    )
    assert _balance_row(conn) is None

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=70, selected_usage_type="Credits"
    ) is True

    row = _balance_row(conn)
    assert row is not None, "settle must recreate the row, not skip the booking"
    assert row["total_usage"] == 70, "the debit landed"
    assert row["total_credits"] == 0, "no credits were invented"
    assert row["reserved"] == 0

    settled = store.get_gateway_authorization(auth.id)
    assert settled is not None and settled.settled is True


def test_settle_books_usage_when_reservation_entity_is_gone(
    harness: tuple[Any, Any],
) -> None:
    """Only the RELEASE depends on the reservation entity, never the booking.

    With the entity gone the release amount is unknown, so `reserved` stays
    inflated (conservative: blocks spend rather than minting headroom) — but
    the usage still books.
    """
    store, conn = harness
    _seed_balance(conn)
    auth = _authorize_with_reservation(store, estimate=100)

    conn.execute(
        "DELETE FROM tr_entities WHERE kind = 'reservation' AND id = %s",
        (auth.credit_reservation_id,),
    )

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=55, selected_usage_type="Credits"
    ) is True

    row = _balance_row(conn)
    assert row is not None
    assert row["total_usage"] == 55, "booking must not be skipped"
    assert row["reserved"] == 100, "unknown release amount stays held, not guessed"

    settled = store.get_gateway_authorization(auth.id)
    assert settled is not None and settled.settled is True


def test_local_settle_releases_hold_under_the_reserved_type(
    harness: tuple[Any, Any],
) -> None:
    """Mixed Credits/BYOK authorization, BYOK endpoint selected, on a key that
    excludes BYOK from its limits.

    The key-limit hold was reserved under CREDITS (a credit candidate
    existed). Releasing under the SELECTED type made _release_key_hold_tx's
    early-return skip the release entirely on include_byok=false keys:
    `reserved` stranded forever, the key's effective cap shrinking with every
    mixed request that landed on BYOK. The Spanner typed path is immune — it
    releases the EXACT recorded hold and only books by settled type — so this
    pins the Postgres local path to the same semantics.
    """
    store, conn = harness
    _seed_balance(conn)
    conn.execute(
        "INSERT INTO tr_key_limit"
        " (workspace_id, key_hash, shard, limit_micro, usage, byok_usage,"
        "  reserved, include_byok, updated_at)"
        " VALUES (%s, %s, 0, 10000000, 0, 0, 0, 0, CURRENT_TIMESTAMP)",
        (WS, KEY),
    )
    store.reserve_key_limit(KEY, 100, usage_type="Credits")
    auth = _authorize_with_reservation(store, estimate=100)

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=40, selected_usage_type="BYOK"
    ) is True

    row = conn.execute(
        "SELECT reserved, day_usage, byok_usage FROM tr_key_limit"
        " WHERE key_hash = %s AND shard = 0",
        (KEY,),
    ).fetchone()
    assert row[0] == 0, "the Credits-typed hold must release regardless of selection"
    assert (row[1] or 0) == 0, (
        "an include_byok=false key must not roll BYOK spend into its windows"
    )
    assert row[2] == 40, "lifetime attribution still keys off the SELECTED type"


def test_failed_settle_on_deleted_row_books_nothing_but_recreates(
    harness: tuple[Any, Any],
) -> None:
    """success=False books zero; the recreated row is empty, not negative."""
    store, conn = harness
    _seed_balance(conn)
    auth = _authorize_with_reservation(store, estimate=100)
    conn.execute(
        "DELETE FROM tr_credit_balance WHERE workspace_id = %s AND shard = 0", (WS,)
    )

    assert store.finalize_gateway_authorization(
        auth.id, success=False, actual_microdollars=0, selected_usage_type="Credits"
    ) is True

    row = _balance_row(conn)
    assert row is not None
    assert row == {"total_credits": 0, "total_usage": 0, "reserved": 0}
