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


def test_spanner_apply_clamps_before_writing() -> None:
    store, database = _spanner_home()
    assert _apply(store, "gwa-s4", CAP) == "applied"
    assert _apply(store, "gwa-s5", 1) == "clamped"
    row = database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]
    assert row["total_usage"] == CAP, "the clamped row booked nothing"


# --------------------------------------------------------------------------
# The forwarder against a stub home, on the real peer SQL
# --------------------------------------------------------------------------


class _Settings:
    federation_home_base_url = "https://home.test"
    federation_settlement_home_token = "tok-settlement-abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105 - test fixture
    federation_deferred_settlement_enabled = True


def _peer_with_debt(*rows: tuple[str, int]) -> tuple[Any, Any]:
    """A fake-postgres peer store holding pending settlement rows + counter."""
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
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
    rows = peer.pending_home_settlements()
    assert len(rows) == 2, "nothing dead-lettered by a transport-shaped 404"
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


def test_forwarder_leaves_clamped_rows_pending(monkeypatch: Any) -> None:
    peer, _conn = _peer_with_debt(("gwa-cl1", 100), ("gwa-cl2", 100))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"code": 429, "type": "settlement_clamped"}}
        )

    counts = _drain_with(monkeypatch, peer, handler)
    assert counts["clamped"] == 2, "clamp is per-row backpressure, not an outage"
    assert len(peer.pending_home_settlements()) == 2
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


def test_dead_letter_is_terminal_and_forward_cannot_follow() -> None:
    peer, _conn = _peer_with_debt(("gwa-m2", 250))
    assert peer.mark_home_settlement_dead_letter("gwa-m2", reason="terms conflict") is True
    assert peer.mark_home_settlement_forwarded("gwa-m2") is False
    out = peer.deferred_outstanding(WS)
    assert out == {"outstanding": 0, "dead_lettered": 250}
