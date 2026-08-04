"""Deferred settlement 2c: home applies recorded debt; the peer forwards it.

The classifier is where the money-safety argument lives on the peer side:
dead-lettering is reserved for STRUCTURED verdicts, and everything
unparseable is an OUTAGE — a home plane rolled back past this deploy answers
apply-usage with a bare 404, and classifying that as a verdict would destroy
the whole backlog on exactly the day home is having problems.

On the home side, apply is insert-once per (source_plane, authorization_id),
debit-only, clamped per (plane, workspace, day) BEFORE any write.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.services import home_settlement
from trusted_router.services.home_settlement import (
    CLAMPED,
    DEAD_LETTER,
    FORWARDED,
    RETRY,
    classify_apply_response,
    drain_home_settlements,
)
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, Workspace

WS = "ws-home-settle"
PLANE = "aws-eu"
CAP = 100_000_000


# --------------------------------------------------------------------------
# Config: the token doctrine must be ENFORCED, not just documented
# --------------------------------------------------------------------------


def _settings(**kw: Any) -> Any:
    from trusted_router.config import Settings

    base = dict(
        environment="test",
        federation_home_base_url="https://home.test",
    )
    base.update(kw)
    return Settings(**base)


def test_settlement_token_reusing_the_resolve_key_token_fails_startup() -> None:
    """The whole token doctrine is that no single secret grants both directory
    reads and money movement. A config that reuses the resolve-key token as a
    settlement value would hand every peer holding the directory secret the
    power to debit workspaces — so it must fail construction, not just be
    discouraged in a docstring."""
    shared = "x" * 40
    with pytest.raises(ValueError, match="reuses the value of TR_FEDERATION_PEER_TOKEN"):
        _settings(
            federation_peer_token=shared,
            federation_settlement_inbound_tokens=f"aws-eu={shared}",
        )


def test_settlement_token_reusing_the_internal_gateway_token_fails_startup() -> None:
    shared = "y" * 40
    with pytest.raises(ValueError, match="reuses the value of TR_INTERNAL_GATEWAY_TOKEN"):
        _settings(
            internal_gateway_token=shared,
            federation_settlement_inbound_tokens=f"aws-eu={shared}",
        )


def test_distinct_settlement_tokens_are_accepted() -> None:
    s = _settings(
        federation_peer_token="p" * 40,
        internal_gateway_token="i" * 40,
        federation_settlement_inbound_tokens="aws-eu=" + "a" * 40 + ",azure-uae=" + "z" * 40,
    )
    assert s.federation_peer_token == "p" * 40


# --------------------------------------------------------------------------
# The classifier — pure, and the most attack-worthy function in 2c
# --------------------------------------------------------------------------


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def test_classifier_forwards_only_recognized_200_outcomes() -> None:
    assert classify_apply_response(200, _body({"data": {"outcome": "applied"}}))[0] == FORWARDED
    assert classify_apply_response(200, _body({"data": {"outcome": "already"}}))[0] == FORWARDED
    assert classify_apply_response(200, _body({"data": {"outcome": "weird"}}))[0] == RETRY
    assert classify_apply_response(200, _body({}))[0] == RETRY


def test_classifier_dead_letters_only_structured_verdicts() -> None:
    conflict = _body({"error": {"code": 409, "type": "settlement_terms_conflict"}})
    unknown = _body({"error": {"code": 404, "type": "workspace_unknown"}})
    assert classify_apply_response(409, conflict)[0] == DEAD_LETTER
    assert classify_apply_response(404, unknown)[0] == DEAD_LETTER


def test_classifier_treats_a_bare_404_as_an_outage() -> None:
    """A home rolled back past 2c answers with route-not-found; a proxy
    answers with HTML. Neither is a verdict about this row."""
    assert classify_apply_response(404, b"<html>not found</html>")[0] == RETRY
    assert classify_apply_response(404, _body({"error": {"type": "not_found"}}))[0] == RETRY
    assert classify_apply_response(404, _body({"error": {"type": "http_error"}}))[0] == RETRY
    assert classify_apply_response(404, b"")[0] == RETRY


def test_classifier_clamp_and_garbage() -> None:
    clamped = _body({"error": {"code": 429, "type": "settlement_clamped"}})
    assert classify_apply_response(429, clamped)[0] == CLAMPED
    assert classify_apply_response(429, _body({"error": {"type": "rate_limited"}}))[0] == RETRY
    assert classify_apply_response(500, _body({"error": {"type": "internal_error"}}))[0] == RETRY
    assert classify_apply_response(200, b"\xff\xfe garbage")[0] == RETRY
    assert classify_apply_response(200, b"[1,2,3]")[0] == RETRY


# --------------------------------------------------------------------------
# Home apply — InMemory
# --------------------------------------------------------------------------


@pytest.fixture
def home() -> InMemoryStore:
    store = InMemoryStore()
    store.workspaces[WS] = Workspace(id=WS, name="w", owner_user_id="u-1")
    return store


def _apply(store: Any, auth_id: str, cost: int, *, plane: str = PLANE, ws: str = WS) -> str:
    return store.apply_federated_usage(
        source_plane=plane,
        authorization_id=auth_id,
        workspace_id=ws,
        cost_microdollars=cost,
        daily_cap_microdollars=CAP,
    )


def test_apply_books_usage_exactly_once(home: InMemoryStore) -> None:
    assert _apply(home, "gwa-1", 400_000) == "applied"
    assert home.credit_money[WS].total_usage_microdollars == 400_000

    assert _apply(home, "gwa-1", 400_000) == "already"
    assert home.credit_money[WS].total_usage_microdollars == 400_000, "replay books nothing"


def test_apply_conflict_on_different_terms(home: InMemoryStore) -> None:
    assert _apply(home, "gwa-2", 400_000) == "applied"
    assert _apply(home, "gwa-2", 500_000) == "conflict"
    assert _apply(home, "gwa-2", 400_000, ws="ws-other") == "conflict"
    assert home.credit_money[WS].total_usage_microdollars == 400_000


def test_apply_unknown_workspace(home: InMemoryStore) -> None:
    assert _apply(home, "gwa-3", 100, ws="ws-nope") == "workspace_unknown"


def test_apply_clamp_leaves_no_residue(home: InMemoryStore) -> None:
    """A clamped row records NOTHING, so it can apply cleanly next window —
    and smaller rows still fit under the cap after a big one is refused."""
    assert _apply(home, "gwa-big", CAP - 10) == "applied"
    assert _apply(home, "gwa-over", 11) == "clamped"
    # The clamped attempt must not have consumed cap or recorded a claim:
    assert _apply(home, "gwa-over", 10) == "applied"
    assert home.credit_money[WS].total_usage_microdollars == CAP


def test_apply_clamp_is_per_plane_and_workspace(home: InMemoryStore) -> None:
    home.workspaces["ws-2"] = Workspace(id="ws-2", name="w2", owner_user_id="u-1")
    assert _apply(home, "gwa-a", CAP) == "applied"
    assert _apply(home, "gwa-b", 1) == "clamped"
    assert _apply(home, "gwa-c", 1, plane="azure-uae") == "applied", "another plane's budget"
    assert _apply(home, "gwa-d", 1, ws="ws-2") == "applied", "another workspace's budget"


def test_apply_rejects_nonpositive_cost(home: InMemoryStore) -> None:
    with pytest.raises(ValueError):
        _apply(home, "gwa-z", 0)
    with pytest.raises(ValueError):
        _apply(home, "gwa-z", -5)


# --------------------------------------------------------------------------
# Home apply — native Spanner store (fake), the plane that runs it in prod
# --------------------------------------------------------------------------


def _spanner_home() -> tuple[Any, Any]:
    store, database, _ = make_fake_store()
    store._write_entity("workspace", WS, Workspace(id=WS, name="w", owner_user_id="u-1"))
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(WS, 0)] = {
        "workspace_id": WS,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    return store, database


def test_spanner_apply_books_once_and_replays_recorded_verdict() -> None:
    store, database = _spanner_home()
    assert _apply(store, "gwa-s1", 400_000) == "applied"
    assert _apply(store, "gwa-s1", 400_000) == "already"
    assert _apply(store, "gwa-s1", 500_000) == "conflict"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == 400_000


def test_spanner_apply_books_into_negative_available() -> None:
    """The spend already happened on the peer; refusing to book it would lose
    the debit. total_usage may exceed total_credits."""
    store, database = _spanner_home()
    assert _apply(store, "gwa-s2", 5_000_000) == "applied"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == 5_000_000
    assert row["total_credits"] == 1_000_000, "no credits invented"


def test_spanner_apply_unknown_workspace_records_nothing() -> None:
    store, _database = _spanner_home()
    assert _apply(store, "gwa-s3", 100, ws="ws-none") == "workspace_unknown"
    # And the claim must NOT exist: a later retry after the workspace is
    # created (support restores it) must be able to apply.
    assert _apply(store, "gwa-s3", 100, ws="ws-none") == "workspace_unknown"


def test_spanner_apply_books_when_workspace_exists_but_balance_row_is_gone() -> None:
    """A live workspace whose shard-0 balance row was deleted must still book.

    The row count of the usage UPDATE is NOT the workspace-existence check —
    conflating them turned drift (a missing balance row) into a terminal
    'workspace_unknown', which the peer dead-letters, dropping a valid debit
    forever. Existence is checked against the workspace ENTITY; the debit
    upserts into a recreated zero-credit row.
    """
    store, database = _spanner_home()
    # The workspace entity exists (seeded by _spanner_home's credit write path
    # is not enough — write the workspace entity explicitly).
    from trusted_router.storage_models import Workspace as _WS

    store._write_entity("workspace", WS, _WS(id=WS, name="w", owner_user_id="u-1"))
    database.typed[CREDIT_BALANCE_TABLE].pop((WS, 0))  # balance row vanishes

    assert _apply(store, "gwa-s6", 700_000) == "applied"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == 700_000, "the debit landed on a recreated row"
    assert row["total_credits"] == 0, "no credits invented"


def test_spanner_apply_clamps_before_writing() -> None:
    store, database = _spanner_home()
    assert _apply(store, "gwa-s4", CAP) == "applied"
    assert _apply(store, "gwa-s5", 1) == "clamped"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == CAP, "the clamped row booked nothing"


def test_spanner_apply_recreates_a_missing_balance_row() -> None:
    """A live workspace whose shard-0 balance row is gone must still book.

    Workspace existence is checked against the workspace ENTITY; the balance
    UPDATE's row count is drift to repair, not a verdict. Conflating them
    returned workspace_unknown, which the peer dead-letters TERMINALLY — a
    valid debit dropped forever, the exact silent-vanish shape 2a fixed in
    the local settle path.
    """
    store, database = _spanner_home()
    store._write_entity("workspace", WS, Workspace(id=WS, name="w", owner_user_id="u-1"))
    del database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]

    assert _apply(store, "gwa-s6", 250_000) == "applied"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == 250_000, "the debit landed in a recreated row"
    assert row["total_credits"] == 0, "no credits were invented"

    # And a replay still answers from the claim.
    assert _apply(store, "gwa-s6", 250_000) == "already"


def test_spanner_apply_unknown_workspace_means_the_entity_is_absent() -> None:
    """workspace_unknown is reserved for a genuinely absent workspace ENTITY.

    A leftover balance row for a deleted workspace is NOT existence — booking
    against it would resurrect spend for an account that no longer exists.
    Existence is the workspace entity; the balance row is where usage lands
    once existence is confirmed.
    """
    store, database = _spanner_home()
    # Delete the workspace entity but leave the balance row behind — the
    # deleted-but-not-cleaned-up state. Entities live in `rows`, keyed
    # (kind, id).
    database.rows.pop(("workspace", WS), None)

    assert _apply(store, "gwa-s7", 100) == "workspace_unknown"
    # And nothing booked: a later real retry (if the workspace is restored)
    # must be able to apply, so no claim may have been recorded.
    assert _apply(store, "gwa-s7", 100) == "workspace_unknown"


class _Settings:
    federation_home_base_url = "https://home.test"
    federation_settlement_home_token = "tok-settlement-abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105 - test fixture
    federation_deferred_settlement_enabled = True


_PEER_CONNS: dict[int, Any] = {}


def _peer_with_debt(*rows: tuple[str, int]) -> tuple[Any, Any]:
    """A fake-postgres peer store holding pending settlement rows + counter."""
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    _PEER_CONNS[id(store)] = conn
    total = 0
    for auth_id, cost in rows:
        conn.execute(
            "INSERT INTO tr_home_settlement_outbox"
            " (authorization_id, workspace_id, cost_microdollars, state, attempts,"
            "  enqueued_at, updated_at)"
            " VALUES (%s, %s, %s, 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (auth_id, WS, cost),
        )
        total += cost
    conn.execute(
        "INSERT INTO tr_deferred_outstanding (workspace_id, outstanding, dead_lettered)"
        " VALUES (%s, %s, 0)",
        (WS, total),
    )
    return store, conn


def _conn_pending_count(store: Any) -> int:
    """Rows still in state='pending' regardless of backoff eligibility."""
    conn = _PEER_CONNS[id(store)]
    return conn.execute(
        "SELECT COUNT(*) FROM tr_home_settlement_outbox WHERE state = 'pending'"
    ).fetchone()[0]


def _drain_with(monkeypatch: Any, store: Any, handler: Any, *, limit: int = 50) -> dict[str, Any]:
    configure_store(store)
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched(**kwargs: Any) -> httpx.Client:
        kwargs.pop("timeout", None)
        return real_client(transport=transport)

    monkeypatch.setattr(home_settlement.httpx, "Client", patched)
    try:
        return drain_home_settlements(_Settings(), limit=limit)
    finally:
        configure_store(InMemoryStore())


def test_forwarder_end_to_end_applies_once(monkeypatch: Any) -> None:
    """Peer debt -> home ledger, exactly once, counter to zero — and a
    re-drain after home already recorded everything changes nothing."""
    peer, _conn = _peer_with_debt(("gwa-e1", 300_000), ("gwa-e2", 200_000))
    home_store = InMemoryStore()
    home_store.workspaces[WS] = Workspace(id=WS, name="w", owner_user_id="u-1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-trustedrouter-federation-settlement-token"]
        body = json.loads(request.content)
        outcome = home_store.apply_federated_usage(
            source_plane=PLANE,
            authorization_id=body["authorization_id"],
            workspace_id=body["workspace_id"],
            cost_microdollars=body["cost_microdollars"],
            daily_cap_microdollars=CAP,
        )
        return httpx.Response(200, json={"data": {"outcome": outcome}})

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["forwarded"] == 2
    assert home_store.credit_money[WS].total_usage_microdollars == 500_000
    assert peer.deferred_outstanding(WS)["outstanding"] == 0
    assert peer.pending_home_settlements() == []

    # Re-drain: nothing pending, nothing double-applied.
    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["examined"] == 0
    assert home_store.credit_money[WS].total_usage_microdollars == 500_000


def test_forwarder_parks_the_pass_on_a_bare_404(monkeypatch: Any) -> None:
    """Home rolled back past 2c: EVERY row must survive for a later pass."""
    peer, _conn = _peer_with_debt(("gwa-p1", 100), ("gwa-p2", 200))

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="<html>Not Found</html>")

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["outage"] == 1
    assert calls["n"] == 1, "one outage signal parks the pass; no per-row hammering"
    # The bumped row backs off; the untouched one stays eligible. Both remain
    # PENDING — nothing was dead-lettered by a transport-shaped 404.
    eligible = peer.pending_home_settlements()
    assert [r["authorization_id"] for r in eligible] == ["gwa-p2"]
    assert _conn_pending_count(peer) == 2
    assert peer.deferred_outstanding(WS)["outstanding"] == 300


def test_forwarder_dead_letters_structured_conflict_and_restores_headroom(
    monkeypatch: Any,
) -> None:
    peer, _conn = _peer_with_debt(("gwa-c1", 400))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": 409, "type": "settlement_terms_conflict",
                            "message": "recorded against different terms"}},
        )

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["dead_lettered"] == 1
    outstanding = peer.deferred_outstanding(WS)
    assert outstanding["outstanding"] == 0, "workspace headroom restored"
    assert outstanding["dead_lettered"] == 400, "the debt stays visible"
    assert peer.pending_home_settlements() == []


def test_forwarder_counts_only_transitions_it_actually_made(monkeypatch: Any) -> None:
    """Another drainer wins a row between this pass's read and its mark.

    The mark returns False; the count must NOT increment. A count that
    includes the race loser's no-op tells an operator two instances forwarded
    the same debt — the drain endpoint is the surface they trust during an
    incident, so its numbers have to be transitions that happened here."""
    peer, conn = _peer_with_debt(("gwa-r1", 100), ("gwa-r2", 200))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Simulate the OTHER drainer resolving gwa-r1 first, right before our
        # mark runs — home still answers applied (insert-once), but our
        # conditional flip will hit 0 rows.
        if body["authorization_id"] == "gwa-r1":
            conn.execute(
                "UPDATE tr_home_settlement_outbox SET state = 'forwarded'"
                " WHERE authorization_id = 'gwa-r1'"
            )
        return httpx.Response(200, json={"data": {"outcome": "applied"}})

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["forwarded"] == 1, "only gwa-r2 was actually transitioned by this pass"
    assert counts["raced"] == 1, "gwa-r1 was already resolved; counted as raced, not forwarded"


def test_forwarder_leaves_clamped_rows_pending(monkeypatch: Any) -> None:
    peer, _conn = _peer_with_debt(("gwa-cl1", 100), ("gwa-cl2", 100))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"code": 429, "type": "settlement_clamped"}}
        )

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["clamped"] == 2, "clamp is per-row backpressure, not an outage"
    # Clamped rows wait out home's DAILY window instead of being re-presented
    # every 20-45s pass from every instance while eligible rows behind them
    # starve. The backoff must be substantial — a 1s backoff clears before the
    # next pass and is no backoff at all — so assert next_attempt_at is well
    # into the future, not merely non-empty.
    assert peer.pending_home_settlements() == [], "clamped rows are deferred, not eligible"
    assert _conn_pending_count(peer) == 2, "...but they are still pending, never lost"
    conn = _PEER_CONNS[id(peer)]
    soon = (
        dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
    ).isoformat().replace("+00:00", "Z")
    still_deferred = conn.execute(
        "SELECT COUNT(*) FROM tr_home_settlement_outbox"
        " WHERE state = 'pending' AND next_attempt_at > %s",
        (soon,),
    ).fetchone()[0]
    assert still_deferred == 2, "a clamped row must back off far past the next pass, not ~1s"
    assert peer.deferred_outstanding(WS)["outstanding"] == 200


def test_forwarder_skips_without_config() -> None:
    class Bare:
        federation_home_base_url = ""
        federation_settlement_home_token = ""

    configure_store(InMemoryStore())
    assert "skipped" in drain_home_settlements(Bare())


# --------------------------------------------------------------------------
# Outbox state machine on the real SQL
# --------------------------------------------------------------------------


def test_mark_forwarded_decrements_exactly_once() -> None:
    peer, _conn = _peer_with_debt(("gwa-m1", 250))
    assert peer.mark_home_settlement_forwarded("gwa-m1") is True
    assert peer.deferred_outstanding(WS)["outstanding"] == 0
    assert peer.mark_home_settlement_forwarded("gwa-m1") is False, "race loser is a no-op"
    assert peer.deferred_outstanding(WS)["outstanding"] == 0, "no second decrement"


def test_mark_forwarded_race_loser_never_decrements() -> None:
    """The interleaving the conditional UPDATE exists for.

    Two drainers both SELECT the pending row; one flips it first. On real
    Postgres the loser's UPDATE re-evaluates its predicate after the lock
    and hits 0 rows; on DSQL the loser's transaction aborts and replays.
    Either way the loser must not decrement — a decrement outside the
    rowcount gate double-frees the workspace's cap. The fake is a single
    connection, so the interleave is staged by flipping the row between the
    loser's SELECT and its UPDATE.
    """
    peer, conn = _peer_with_debt(("gwa-race", 250))

    original_execute = conn.execute
    state = {"armed": True}

    def racing_execute(sql: str, params: tuple[Any, ...] = (), **kwargs: Any) -> Any:
        result = original_execute(sql, params, **kwargs)
        if state["armed"] and sql.lstrip().startswith(
            "SELECT workspace_id, cost_microdollars FROM tr_home_settlement_outbox"
        ):
            state["armed"] = False
            # The OTHER drainer wins between our SELECT and our UPDATE.
            original_execute(
                "UPDATE tr_home_settlement_outbox SET state = 'forwarded'"
                " WHERE authorization_id = %s",
                params,
            )
        return result

    conn.execute = racing_execute  # type: ignore[method-assign]
    try:
        assert peer.mark_home_settlement_forwarded("gwa-race") is False
    finally:
        conn.execute = original_execute  # type: ignore[method-assign]

    assert peer.deferred_outstanding(WS)["outstanding"] == 250, (
        "the race loser must not decrement; the winner's own call does that"
    )


def test_backoff_defers_a_row_then_makes_it_eligible_again() -> None:
    """A bumped row waits out its backoff, then reappears in the eligible set.

    Proven by bumping with a past next_attempt_at directly — the drain's
    eligibility predicate is next_attempt_at <= now, so a row scheduled in
    the past is eligible and one scheduled in the future is not."""
    peer, conn = _peer_with_debt(("gwa-bk1", 100), ("gwa-bk2", 200))
    peer.bump_home_settlement_attempt("gwa-bk1", error="clamped", retry_in_seconds=3600)
    assert [r["authorization_id"] for r in peer.pending_home_settlements()] == ["gwa-bk2"]

    # Rewind its schedule to the past: now eligible again, still pending.
    conn.execute(
        "UPDATE tr_home_settlement_outbox SET next_attempt_at = %s"
        " WHERE authorization_id = %s",
        ("2000-01-01T00:00:00Z", "gwa-bk1"),
    )
    assert {r["authorization_id"] for r in peer.pending_home_settlements()} == {
        "gwa-bk1",
        "gwa-bk2",
    }


def test_dead_letter_is_terminal_and_forward_cannot_follow() -> None:
    peer, _conn = _peer_with_debt(("gwa-m2", 250))
    assert peer.mark_home_settlement_dead_letter("gwa-m2", reason="terms conflict") is True
    assert peer.mark_home_settlement_forwarded("gwa-m2") is False
    out = peer.deferred_outstanding(WS)
    assert out == {"outstanding": 0, "dead_lettered": 250}
