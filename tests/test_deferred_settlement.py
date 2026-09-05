"""Deferred settlement on a peer plane: the cap, the reaper, the outbox.

Deferred settlement lets a peer plane serve a federated key's CREDITS traffic
while the home plane is unreachable. The spend is admitted against a
transactional cap, recorded as debt, and forwarded to the home ledger later.

Three properties carry the whole design, and each has a test here that fails
if it is weakened:

* **The cap is a real bound, not advice.** It is a conditional UPDATE, so N
  concurrent authorizes cannot all read one stale total and all admit. A
  read-then-check implementation passes a single-threaded test and bounds
  nothing in production.
* **An abandoned authorization is reclaimed.** The enclave dying between
  authorize and settle is routine — every deploy does it. Without the reaper
  each one leaks its estimate into the counter permanently, and once the leaks
  reach the cap the workspace 402s forever on a healthy plane.
* **Settle records debt instead of debiting locally**, exactly once, and the
  counter trues up to the frozen actual.

These run against the SQLite psycopg fake, which executes the store's REAL
SQL: the guarantees live in the statement text (`ON CONFLICT`, the conditional
`WHERE outstanding + %s <= %s`, rowcount), and a Python twin would re-implement
them and prove nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.storage_errors import DeferredSettlementCapReached
from trusted_router.storage_models import iso_now

WS = "ws-deferred-1"
KEY = "kh-deferred-1"
CAP = 25_000_000  # $25, the shipped default


@pytest.fixture
def harness() -> tuple[Any, Any]:
    conn = sqlite_postgres_conn()
    return postgres_store_on(conn), conn


def _authorize(
    store: Any,
    *,
    estimate: int,
    expires_at: str | None = None,
    cap: int | None = CAP,
    idempotency_key: str | None = None,
    key_reserved_microdollars: int = 0,
) -> Any:
    return store.create_gateway_authorization(
        workspace_id=WS,
        key_hash=KEY,
        model_id="anthropic/claude-opus-4.7",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=estimate,
        credit_reservation_id=None,
        key_reserved_microdollars=key_reserved_microdollars,
        idempotency_key=idempotency_key,
        settlement="deferred_home",
        expires_at=expires_at or _in(hours=2),
        deferred_cap_microdollars=cap,
    )


def _in(*, hours: float) -> str:
    from trusted_router.spend_windows import utcnow

    return (utcnow() + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _outbox(conn: Any) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT authorization_id, workspace_id, cost_microdollars, state"
        " FROM tr_home_settlement_outbox ORDER BY authorization_id"
    ).fetchall()


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------


def test_cap_admits_up_to_the_limit_then_refuses(harness: tuple[Any, Any]) -> None:
    store, conn = harness
    for _ in range(5):
        _authorize(store, estimate=5_000_000)
    assert store.deferred_outstanding(WS)["outstanding"] == CAP

    with pytest.raises(DeferredSettlementCapReached):
        _authorize(store, estimate=1)

    assert store.deferred_outstanding(WS)["outstanding"] == CAP, "refusal changed nothing"


def test_cap_refusal_writes_no_authorization(harness: tuple[Any, Any]) -> None:
    """The counter move and the authorization insert share a transaction.

    If they did not, a refusal could leave an authorization the reaper would
    later 'reclaim', decrementing a counter that was never incremented.
    """
    store, conn = harness
    _authorize(store, estimate=CAP)
    before = conn.execute(
        "SELECT COUNT(*) FROM tr_entities WHERE kind = 'gateway_authorization'"
    ).fetchone()[0]

    with pytest.raises(DeferredSettlementCapReached):
        _authorize(store, estimate=1_000_000)

    after = conn.execute(
        "SELECT COUNT(*) FROM tr_entities WHERE kind = 'gateway_authorization'"
    ).fetchone()[0]
    assert after == before, "a refused authorize must not leave an authorization behind"


def test_cap_is_enforced_by_a_conditional_update_not_a_read(
    harness: tuple[Any, Any],
) -> None:
    """The statement itself must carry the bound.

    This is the mutation guard for the panel's TOCTOU finding: authorize is
    the ONLY place the counter grows, and it must grow under a predicate. A
    read-then-write implementation passes every other test in this file (they
    are sequential) and admits unboundedly under concurrency.
    """
    store, _conn = harness
    seen: list[str] = []
    original = store._reserve_deferred_outstanding_tx

    def spy(conn: Any, workspace_id: str, amount: int, **kw: Any) -> None:
        class Recorder:
            def execute(self, sql: str, params: tuple[Any, ...] = (), **kwargs: Any) -> Any:
                seen.append(" ".join(sql.split()))
                return conn.execute(sql, params, **kwargs)

        return original(Recorder(), workspace_id, amount, **kw)  # type: ignore[arg-type]

    store._reserve_deferred_outstanding_tx = spy  # type: ignore[method-assign]
    _authorize(store, estimate=1_000_000)

    update = next((s for s in seen if s.startswith("UPDATE tr_deferred_outstanding")), "")
    assert update, "authorize did not move the counter"
    assert "WHERE workspace_id = %s AND outstanding + %s <= %s" in update, (
        "the cap must be a predicate ON the UPDATE; a read-then-check bounds "
        "nothing once two requests are in flight"
    )
    assert not any(
        s.startswith("SELECT outstanding") for s in seen
    ), "admission must not consult a prior read"


def test_idempotent_replay_does_not_double_count(harness: tuple[Any, Any]) -> None:
    """A replay returns the existing authorization and writes nothing.

    It must therefore consume no additional cap — otherwise a client retrying
    a request walks a workspace into a lockout it never spent.
    """
    store, _conn = harness
    first = _authorize(store, estimate=5_000_000, idempotency_key="idem-1")
    second = _authorize(store, estimate=5_000_000, idempotency_key="idem-1")

    assert first.id == second.id
    assert store.deferred_outstanding(WS)["outstanding"] == 5_000_000


# --------------------------------------------------------------------------
# Settle
# --------------------------------------------------------------------------


def test_settle_enqueues_debt_and_leaves_the_local_balance_alone(
    harness: tuple[Any, Any],
) -> None:
    store, conn = harness
    conn.execute(
        "INSERT INTO tr_credit_balance"
        " (workspace_id, shard, total_credits, total_usage, reserved, updated_at)"
        " VALUES (%s, 0, 0, 0, 0, CURRENT_TIMESTAMP)",
        (WS,),
    )
    auth = _authorize(store, estimate=1_000_000)

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=400_000, selected_usage_type="Credits"
    ) is True

    rows = _outbox(conn)
    assert rows == [(auth.id, WS, 400_000, "pending")]

    balance = conn.execute(
        "SELECT total_usage FROM tr_credit_balance WHERE workspace_id = %s AND shard = 0",
        (WS,),
    ).fetchone()
    assert balance[0] == 0, "deferred spend is debt at home, never a local debit"

    # The counter trues up from the ESTIMATE to the FROZEN ACTUAL.
    assert store.deferred_outstanding(WS)["outstanding"] == 400_000


def test_replayed_settle_enqueues_once(harness: tuple[Any, Any]) -> None:
    store, conn = harness
    auth = _authorize(store, estimate=1_000_000)
    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=400_000, selected_usage_type="Credits"
    ) is True
    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=400_000, selected_usage_type="Credits"
    ) is False

    assert len(_outbox(conn)) == 1, "a redelivered settle must not double the debt"
    assert store.deferred_outstanding(WS)["outstanding"] == 400_000


def test_failed_request_enqueues_nothing_and_returns_the_whole_estimate(
    harness: tuple[Any, Any],
) -> None:
    store, conn = harness
    _authorize(store, estimate=3_000_000)
    auth = _authorize(store, estimate=2_000_000)
    assert store.deferred_outstanding(WS)["outstanding"] == 5_000_000

    assert store.finalize_gateway_authorization(
        auth.id, success=False, actual_microdollars=0, selected_usage_type="Credits"
    ) is True

    assert _outbox(conn) == [], "a failed request owes nothing"
    assert store.deferred_outstanding(WS)["outstanding"] == 3_000_000


def test_byok_selected_settle_owes_home_nothing(harness: tuple[Any, Any]) -> None:
    """Mixed candidates: authorize went deferred for the CREDITS candidates,
    but the enclave picked a BYOK endpoint — the customer's own provider key
    paid for the tokens. Enqueuing that as home debt charges them twice.
    """
    store, conn = harness
    auth = _authorize(store, estimate=1_000_000)

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=400_000, selected_usage_type="BYOK"
    ) is True

    assert _outbox(conn) == [], "BYOK spend must never become home debt"
    assert store.deferred_outstanding(WS)["outstanding"] == 0, (
        "the whole estimate comes back: nothing is owed"
    )


def test_byok_selected_settle_releases_the_credits_hold(
    harness: tuple[Any, Any],
) -> None:
    """The hold was reserved under CREDITS (a credit candidate existed); the
    release must use that same type. Releasing under the SELECTED type made
    _release_key_hold_tx's early-return skip the release entirely on
    include_byok=false keys — reserved stranded forever, and the key's cap
    shrinking with every mixed-candidate request that landed on BYOK.
    """
    store, conn = harness
    conn.execute(
        "INSERT INTO tr_key_limit"
        " (workspace_id, key_hash, shard, limit_micro, usage, byok_usage,"
        "  reserved, include_byok, updated_at)"
        " VALUES (%s, %s, 0, 10000000, 0, 0, 1000000, 0, CURRENT_TIMESTAMP)",
        (WS, KEY),
    )
    auth = _authorize(store, estimate=1_000_000, key_reserved_microdollars=1_000_000)

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=400_000, selected_usage_type="BYOK"
    ) is True

    row = conn.execute(
        "SELECT reserved, day_usage FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
        (KEY,),
    ).fetchone()
    assert row[0] == 0, "the Credits-typed hold must release regardless of selection"
    assert (row[1] or 0) == 0, (
        "an include_byok=false key must not have BYOK spend rolled into windows"
    )


# --------------------------------------------------------------------------
# The reaper
# --------------------------------------------------------------------------


def test_reaper_reclaims_an_abandoned_authorization(harness: tuple[Any, Any]) -> None:
    """The enclave died between authorize and settle — every deploy does this.

    Without reclamation the estimate is stuck in the counter forever, and
    enough of them permanently 402 a workspace on a healthy plane.
    """
    store, _conn = harness
    _authorize(store, estimate=20_000_000, expires_at=_in(hours=-1))
    assert store.deferred_outstanding(WS)["outstanding"] == 20_000_000

    # The cap is genuinely blocking before the reap.
    with pytest.raises(DeferredSettlementCapReached):
        _authorize(store, estimate=10_000_000)

    result = store.reap_expired_deferred_authorizations()
    assert result["reaped"] == 1
    assert store.deferred_outstanding(WS)["outstanding"] == 0

    # ...and the workspace can spend again.
    _authorize(store, estimate=10_000_000)
    assert store.deferred_outstanding(WS)["outstanding"] == 10_000_000


def test_reaper_leaves_unexpired_authorizations_alone(harness: tuple[Any, Any]) -> None:
    store, _conn = harness
    _authorize(store, estimate=5_000_000, expires_at=_in(hours=2))
    assert store.reap_expired_deferred_authorizations()["reaped"] == 0
    assert store.deferred_outstanding(WS)["outstanding"] == 5_000_000


def test_reaper_never_touches_an_authorization_that_already_settled(
    harness: tuple[Any, Any],
) -> None:
    """OUTBOX-GUARDED. Settle has already trued the counter to the actual;
    reaping would hand back an estimate that no longer exists."""
    store, conn = harness
    auth = _authorize(store, estimate=5_000_000, expires_at=_in(hours=-1))
    store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=1_000_000, selected_usage_type="Credits"
    )
    assert store.deferred_outstanding(WS)["outstanding"] == 1_000_000

    result = store.reap_expired_deferred_authorizations()
    assert result["reaped"] == 0
    assert store.deferred_outstanding(WS)["outstanding"] == 1_000_000
    assert len(_outbox(conn)) == 1


def test_late_settle_after_a_reap_is_a_no_op(harness: tuple[Any, Any]) -> None:
    """Reaper and settle race on ONE insert-once marker: first writer wins.

    A settle that arrives after its authorization was reaped must book
    nothing and enqueue nothing — otherwise the counter is decremented twice
    and debt is invented from a request nobody is holding money for.
    """
    store, conn = harness
    auth = _authorize(store, estimate=5_000_000, expires_at=_in(hours=-1))
    assert store.reap_expired_deferred_authorizations()["reaped"] == 1
    assert store.deferred_outstanding(WS)["outstanding"] == 0

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=4_000_000, selected_usage_type="Credits"
    ) is False
    assert _outbox(conn) == []
    assert store.deferred_outstanding(WS)["outstanding"] == 0


def test_settle_clears_the_expiry(harness: tuple[Any, Any]) -> None:
    """Settled work is not reclaimable, so it must leave the reaper's scan.

    `expires_at IS NOT NULL` is the reaper's "still outstanding" predicate.
    Leaving a past expiry on a settled row makes every future pass re-read
    rows it can never act on — the scan grows without bound while the reap
    count stays at zero, which reads as a healthy idle reaper.
    """
    store, _conn = harness
    auth = _authorize(store, estimate=1_000_000, expires_at=_in(hours=-1))
    store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=500_000, selected_usage_type="Credits"
    )

    settled = store.get_gateway_authorization(auth.id)
    assert settled is not None
    assert settled.settled is True
    assert settled.expires_at is None, "a settled row must leave the reaper's scan"
    assert store.reap_expired_deferred_authorizations()["examined"] == 0


def test_reaper_refuses_a_row_that_already_owes_debt(harness: tuple[Any, Any]) -> None:
    """The outbox guard, exercised directly.

    Reachable only from a TORN state — an expired, apparently-unsettled
    authorization that nonetheless has debt enqueued. Two upstream guards
    (the cleared expiry and the settled flag) mean the normal paths never
    produce it, so the state is built by hand here. That is the point: the
    guard's job is to fail safe if either of those is ever weakened, and a
    guard nothing exercises is a guard nobody knows is broken. Reaping such a
    row would return an estimate settle has already trued to the actual,
    crediting the counter twice.
    """
    store, conn = harness
    auth = _authorize(store, estimate=5_000_000, expires_at=_in(hours=-1))
    conn.execute(
        "INSERT INTO tr_home_settlement_outbox"
        " (authorization_id, workspace_id, cost_microdollars, state, attempts,"
        "  enqueued_at, updated_at)"
        " VALUES (%s, %s, %s, 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (auth.id, WS, 4_000_000),
    )

    result = store.reap_expired_deferred_authorizations()
    assert result["examined"] == 1
    assert result["reaped"] == 0
    assert result["skipped_settled"] == 1
    assert store.deferred_outstanding(WS)["outstanding"] == 5_000_000, (
        "the guard must not hand back an estimate whose debt is already owed"
    )


def test_reaper_ignores_local_authorizations(harness: tuple[Any, Any]) -> None:
    """Only deferred authorizations carry an expiry and a counter to reclaim.
    A local one is settled by the enclave or not at all, exactly as before."""
    store, _conn = harness
    local = store.create_gateway_authorization(
        workspace_id=WS,
        key_hash=KEY,
        model_id="anthropic/claude-opus-4.7",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=1_000_000,
        credit_reservation_id=None,
        key_reserved_microdollars=0,
    )
    assert local.settlement == "local"
    assert local.expires_at is None
    assert store.reap_expired_deferred_authorizations()["reaped"] == 0
    assert store.get_gateway_authorization(local.id).settled is False


def test_counter_never_goes_negative(harness: tuple[Any, Any]) -> None:
    """An actual larger than its estimate is normal; the clamp is what keeps
    an underflow from handing a workspace headroom it never earned."""
    store, _conn = harness
    auth = _authorize(store, estimate=1_000_000)
    store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=0, selected_usage_type="Credits"
    )
    assert store.deferred_outstanding(WS)["outstanding"] == 0

    store.release_deferred_outstanding(WS, 5_000_000)
    assert store.deferred_outstanding(WS)["outstanding"] == 0


def test_unknown_workspace_reads_zero(harness: tuple[Any, Any]) -> None:
    store, _conn = harness
    assert store.deferred_outstanding("ws-never-seen") == {
        "outstanding": 0,
        "dead_lettered": 0,
    }


def test_expires_at_is_stored_as_an_iso_string(harness: tuple[Any, Any]) -> None:
    """The reaper's WHERE clause compares expires_at as TEXT, so the format
    has to sort lexicographically the same way it sorts chronologically."""
    store, _conn = harness
    auth = _authorize(store, estimate=1_000_000)
    assert auth.expires_at is not None
    assert auth.expires_at.endswith("Z")
    assert auth.expires_at > iso_now()
