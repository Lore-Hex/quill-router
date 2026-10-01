"""One-commit settle: the intent, the charge, the done-mark and the benchmark in ONE commit.

The durable settle used to commit three times on the success path (the
settle-outbox enqueue, the finalize with its folded done-mark, and the
post-response benchmark INSERT). The one-commit settle writes the intent row
already resolved inside the finalize commit and batches the benchmark into it.
Any deviation from the happy path rolls that transaction back and runs the
unchanged two-commit flow, so these tests pin four things: the commit count,
state equivalence with the two-commit flow, the fallback, and the
unknown-commit-outcome case.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import re
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import anyio
import pytest
from fastapi import BackgroundTasks
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded
from google.cloud.spanner_v1 import param_types
from starlette.requests import Request

from tests.fakes.spanner import (
    FakeSpannerDatabase,
    _FakeBatch,
    _FakeTransaction,
    make_fake_store,
)
from tests.fakes.spanner_order import credit_before_key, record_statements, transaction_statements
from tests.test_settle_speculative_batch import clone, invoke
from tests.test_settle_speculative_batch import state as finalize_state
from tests.test_spanner_batch_dml import NOW, _authorization, _authorize, _database
from trusted_router import storage_gcp_authorize, storage_gcp_io
from trusted_router import storage_gcp_settle_outbox as outbox
from trusted_router.catalog import MODEL_ENDPOINTS, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.post_commit import POST_COMMIT
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.services import settle_outbox_drain as drain_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage import CreditAccount, InMemoryStore, Workspace, configure_store
from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox
from trusted_router.storage_gcp_authorize import OneCommitSettleDeclined, typed_finalize_atomic
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_settle_outbox import ENQ_INSERTED, SpannerSettleOutbox
from trusted_router.storage_models import Generation, ProviderBenchmarkSample, SettleOutboxRow
from trusted_router.types import UsageType

NOW_Z = NOW.isoformat().replace("+00:00", "Z")
MODEL = "anthropic/claude-haiku-4.5"
TOTAL_CREDIT = 50_000_000
KEY_LIMIT = 40_000_000
INTERNAL = Settings(environment="test", service_surface="internal", settle_outbox_enabled=True)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def frozen_outbox_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_iso_now", lambda: NOW_Z)
    monkeypatch.setattr(
        outbox, "_iso_after_seconds", lambda seconds: _iso(NOW + timedelta(seconds=seconds)),
    )


# ── storage level: one commit == the two-commit settle, statement for statement ──


def _one_commit_fixture(
    *, success: bool, refill: bool,
) -> tuple[FakeSpannerDatabase, dict[str, Any], SettleOutboxRow, ProviderBenchmarkSample]:
    db = _database()
    db.now = NOW
    accepted = _authorize(db)
    aid, rid = accepted["authorization_id"], accepted["reservation_id"]
    auth = _authorization(aid, rid)
    generation = None
    if success:
        generation = Generation.from_settle_body(
            authorization=auth, provider_name="provider", model_id="model", usage_type="Credits",
            provider="provider", body={}, input_tokens=5, output_tokens=7,
            actual_cost_microdollars=70,
        )
        generation.id, generation.created_at = "stable-generation", NOW.isoformat()
    auth.record_finalization(
        success=success, actual_microdollars=70 if success else 0,
        selected_usage_type="Credits", generation=generation,
    )
    intent = SettleOutboxRow(
        authorization_id=aid, reservation_id=rid, intent_kind="settle" if success else "refund",
        settle_origin="typed", actual_cost_micro=70 if success else 0,
        selected_endpoint_id="model@provider/prepaid", model_id="model",
        selected_usage_type="Credits", settle_body='{"repair":"frozen inputs"}',
        auto_refill_workspace_id="workspace" if refill else None,
    )
    sample = (
        ProviderBenchmarkSample.from_generation(generation)
        if generation is not None
        else ProviderBenchmarkSample(
            id="bench-refund", model="model", provider="provider", provider_name="provider",
            status="error", usage_type=UsageType.CREDITS, streamed=False,
        )
    )
    options = dict(
        reservation_id=rid, authorization_id=aid, success=success,
        actual_micro=70 if success else 0, settled_usage_type="Credits", now=NOW,
        outbox_available=True, authorization=auth, auth_body_settled=json_body(auth),
        generation=generation, persist_generation_record=True, generation_writes=[],
    )
    return db, options, intent, sample


def _full_state(db: FakeSpannerDatabase) -> Any:
    return finalize_state(db), copy.deepcopy(db.analytics_outbox)


@pytest.mark.usefixtures("frozen_outbox_clock")
@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("sibling", ["absent", "pending", "dead", "done", "release_approved"])
@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("refill", [False, True])
def test_one_commit_leaves_exactly_the_two_commit_state(
    success: bool, sibling: str, armed: bool, refill: bool,
) -> None:
    """Every row both flows write is identical: money, claim, authorization,
    intent row (status, attempts, body, due times, refill attachment) and the
    retention clock on all three records, including a deferred clock while a
    sibling intent is outstanding. The two-commit reference is today's code:
    ``enqueue`` then ``typed_finalize_atomic(settle_outbox_done=...)`` then the
    post-response benchmark."""
    initial, options, intent, sample = _one_commit_fixture(success=success, refill=refill)
    aid, rid = options["authorization_id"], options["reservation_id"]
    if armed:
        # Rows only reach here through repair/rolling data; the enqueue's
        # retention clears are what make both flows re-arm from scratch.
        initial.gateway_authorizations[aid]["terminal_at"] = NOW - timedelta(days=1)
        initial.reservations[rid]["terminal_at"] = NOW - timedelta(days=1)
    if sibling != "absent":
        other = "refund" if success else "settle"
        SpannerSettleOutbox(initial, param_types).enqueue(
            SettleOutboxRow(
                authorization_id=aid, reservation_id=rid, intent_kind=other,
                settle_origin="typed", actual_cost_micro=0, settle_body="{}",
            ),
        )
        initial.settle_outbox[(aid, other)]["status"] = sibling
        if armed:
            initial.gateway_authorizations[aid]["terminal_at"] = NOW - timedelta(days=1)
            initial.reservations[rid]["terminal_at"] = NOW - timedelta(days=1)

    two = clone(initial)
    two.analytics_outbox = []
    commits = two.commits
    assert SpannerSettleOutbox(two, param_types).enqueue(intent, initial_delay_seconds=60) == ENQ_INSERTED
    two_result = invoke(two, dict(options, settle_outbox_done=(aid, intent.intent_kind)))
    SpannerAnalyticsOutbox(two, param_types).enqueue(sample)
    assert two.commits - commits == 3

    one = clone(initial)
    one.analytics_outbox = []
    commits = one.commits
    one_result = invoke(one, dict(
        options,
        settle_outbox_intent=intent,
        intent_initial_delay_seconds=60,
        benchmark_statement=SpannerAnalyticsOutbox(one, param_types).enqueue_statement(sample),
    ))
    assert one.commits - commits == 1
    assert one.transaction_tags[-1] == ("tr_settle_one_commit" if success else "tr_refund_one_commit")

    for result in (two_result, one_result):
        result.pop("attempts")
    assert one_result == two_result
    assert one_result["outcome"] == "settled" and one_result["outbox_marked"] is True
    assert _full_state(one) == _full_state(two)
    row = one.settle_outbox[(aid, intent.intent_kind)]
    assert (row["status"], row["attempts"], row["settle_body"], row["next_attempt_at"]) == (
        "done", 1, None, None,
    )
    assert row["terminal_at"] == NOW_Z
    outstanding = sibling in {"pending", "dead"}
    expected_clock = None if outstanding else NOW_Z
    assert one.gateway_authorizations[aid]["terminal_at"] == expected_clock
    assert one.reservations[rid]["terminal_at"] == expected_clock
    assert (row["auto_refill_status"], row["auto_refill_next_attempt_at"]) == (
        ("pending", _iso(NOW + timedelta(seconds=60))) if refill else (None, None)
    )
    assert len(one.analytics_outbox) == 1


@pytest.mark.usefixtures("frozen_outbox_clock")
def test_one_commit_is_one_ordered_dml_transaction_credit_before_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, options, intent, sample = _one_commit_fixture(success=True, refill=True)
    calls = record_statements(monkeypatch)
    batches: list[list[tuple[str, str, dict[str, Any]]]] = []
    original_batch = _FakeTransaction.batch_update

    def batch(tx: Any, statements: Any, **kwargs: Any) -> Any:
        batches.append([
            (sql.split()[0], re.search(r"(?:INTO|UPDATE) (tr_\w+)", sql)[1], dict(params))
            for sql, params, _ in statements
        ])
        return original_batch(tx, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "batch_update", batch)
    invoke(db, dict(
        options, settle_outbox_intent=intent, intent_initial_delay_seconds=60,
        benchmark_statement=SpannerAnalyticsOutbox(db, param_types).enqueue_statement(sample),
    ))
    statements = transaction_statements(calls)
    credit_before_key(statements, key_last=True)
    assert statements[0].startswith("select reservation_id, workspace_id")
    [only] = batches  # one RPC carries every non-counter write
    assert [(verb, table) for verb, table, _ in only] == [
        ("UPDATE", "tr_reservation"),  # first-writer-wins claim
        ("UPDATE", "tr_gateway_authorization"),  # settled=false -> true
        ("INSERT", "tr_settle_outbox"),  # the intent, already resolved
        ("UPDATE", "tr_gateway_authorization"),  # the enqueue's retention clears
        ("UPDATE", "tr_reservation"),
        ("UPDATE", "tr_gateway_authorization"),  # the done-mark's retention arming
        ("UPDATE", "tr_reservation"),
        ("INSERT", "tr_generation"),
        ("INSERT", "tr_operational_analytics_outbox"),
        ("INSERT", "tr_analytics_outbox"),  # PENDING_COMMIT_TIMESTAMP: its only touch
    ]
    inserted = only[2][2]
    assert (inserted["status"], inserted["attempts"], inserted["terminal_at"]) == ("done", 1, NOW_Z)
    assert inserted["settle_body"] is None and inserted["next_attempt_at"] is None


@pytest.mark.parametrize("outbox_available", [False, True])
@pytest.mark.parametrize("defer_retention", [False, True])
def test_only_a_claim_that_arms_retention_pays_for_the_outbox_subquery(
    outbox_available: bool, defer_retention: bool,
) -> None:
    """Before: every finalize claim (all defer retention) evaluated a correlated
    EXISTS over tr_settle_outbox whose result could not change the NULL it
    wrote; in production it was the costliest statement (~6.5 ms CPU, 36% of
    query CPU). After: deferred claims are the plain point UPDATE."""
    from trusted_router.storage_gcp_counter_dml import claim_reservation_statement

    sql, params, _types = claim_reservation_statement(
        param_types, "rid", actual_micro=1, settled_usage_type="Credits", terminal_at=NOW,
        defer_retention=defer_retention, outbox_available=outbox_available,
    )
    assert ("tr_settle_outbox" in sql) == (outbox_available and not defer_retention)
    assert params["terminal_at"] == (None if defer_retention else NOW)
    assert sql.endswith("WHERE reservation_id=@rid AND settled=false")


@pytest.mark.usefixtures("frozen_outbox_clock")
@pytest.mark.parametrize("deviation", ["claimed", "missing", "intent_exists", "typed_absent"])
def test_one_commit_rolls_back_every_deviation_and_commits_nothing(deviation: str) -> None:
    db, options, intent, sample = _one_commit_fixture(success=True, refill=False)
    aid, rid = options["authorization_id"], options["reservation_id"]
    if deviation == "claimed":
        db.reservations[rid]["settled"] = True
    elif deviation == "missing":
        del db.reservations[rid]
    elif deviation == "intent_exists":
        SpannerSettleOutbox(db, param_types).enqueue(intent, initial_delay_seconds=60)
    else:
        del db.gateway_authorizations[aid]
    before, commits = _full_state(db), db.commits
    expected: type[Exception] = AlreadyExists if deviation == "intent_exists" else OneCommitSettleDeclined
    with pytest.raises(expected):
        invoke(db, dict(
            options, settle_outbox_intent=intent,
            benchmark_statement=SpannerAnalyticsOutbox(db, param_types).enqueue_statement(sample),
        ))
    assert _full_state(db) == before
    assert db.commits == commits


def test_one_commit_release_row_count_failure_declines_without_writes(
    frozen_outbox_clock: None,
) -> None:
    db, options, intent, _sample = _one_commit_fixture(success=True, refill=False)
    # A zero-row credit release (the recorded hold no longer covered) must not
    # commit settled=true with nothing booked; it rolls the whole commit back.
    db.typed["tr_credit_balance"][("workspace", 0)]["reserved"] = 0
    before, commits = _full_state(db), db.commits
    with pytest.raises(OneCommitSettleDeclined, match="release_row_count"):
        invoke(db, dict(options, settle_outbox_intent=intent))
    assert _full_state(db) == before and db.commits == commits


def test_one_commit_rejects_inputs_that_cannot_be_resolved_in_commit() -> None:
    db, options, intent, sample = _one_commit_fixture(success=True, refill=False)
    statement = SpannerAnalyticsOutbox(db, param_types).enqueue_statement(sample)
    with pytest.raises(ValueError, match="one-commit"):
        typed_finalize_atomic(db, param_types, benchmark_statement=statement, **options)
    with pytest.raises(ValueError, match="not both"):
        invoke(db, dict(options, settle_outbox_intent=intent,
                        settle_outbox_done=(intent.authorization_id, "settle")))
    with pytest.raises(ValueError, match="durable activity"):
        # No in-commit activity delivery: a done row would hide pending repair.
        typed_finalize_atomic(db, param_types, settle_outbox_intent=intent, **options)
    for wrong in (
        dict(intent_kind="refund"),
        dict(reservation_id="other"),
        dict(authorization_id="other"),
    ):
        with pytest.raises(ValueError, match="does not describe"):
            invoke(db, dict(options, settle_outbox_intent=dataclasses.replace(intent, **wrong)))


# ── route level: the authorize+settle round trip ──


@pytest.fixture
def fixed_catalog(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fixed prepaid route so hourly catalog changes cannot move the counts."""
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == MODEL:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    endpoint = ModelEndpoint(
        id=f"{MODEL}@anthropic/prepaid",
        model_id=MODEL,
        provider="anthropic",
        usage_type="Credits",
        upstream_id="claude-haiku-4-5-20251001",
        supported_parameters=("max_tokens",),
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=5_000_000,
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    return endpoint.id


@pytest.fixture
def prod_store(fixed_catalog: str) -> Iterator[tuple[Any, FakeSpannerDatabase, Any]]:
    """The production store shape: typed request records, generation records,
    and both analytics outboxes."""
    store, db = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
        analytics_outbox_enabled=True,
    )
    workspace = Workspace(id="ws-one-commit", name="One commit", owner_user_id="user-one-commit")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": TOTAL_CREDIT,
        "total_usage": 0, "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id, name="capped", creator_user_id=workspace.owner_user_id,
        limit_microdollars=KEY_LIMIT,
    )
    configure_store(store)
    try:
        yield store, db, key
    finally:
        configure_store(InMemoryStore())


class CommitLog:
    """Every read-write commit the fake applies: transactions and mutation batches."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.commits: list[str | None] = []
        self._tag: list[str | None] = [None]
        original_run = FakeSpannerDatabase.run_in_transaction
        original_commit = FakeSpannerDatabase._try_commit
        original_exit = _FakeBatch.__exit__
        log = self

        def run(db: Any, fn: Any, **kwargs: Any) -> Any:
            log._tag.append(kwargs.get("transaction_tag"))
            try:
                return original_run(db, fn, **kwargs)
            finally:
                log._tag.pop()

        def commit(db: Any, txn: Any) -> bool:
            committed = original_commit(db, txn)
            if committed:
                log.commits.append(log._tag[-1] or "untagged")
            return committed

        def batch_exit(batch: Any, exc_type: Any, *rest: Any) -> None:
            original_exit(batch, exc_type, *rest)
            if exc_type is None and batch.pending_writes:
                log.commits.append("mutation_batch")

        monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
        monkeypatch.setattr(FakeSpannerDatabase, "_try_commit", commit)
        monkeypatch.setattr(_FakeBatch, "__exit__", batch_exit)

    def take(self) -> list[str | None]:
        taken, self.commits = self.commits, []
        return taken


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _authorize_route(key: Any, idempotency_key: str = "one-commit") -> dict[str, Any]:
    return gateway._authorize_gateway_sync(
        _request(),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, idempotency_key=idempotency_key, model=MODEL,
            estimated_input_tokens=100, max_output_tokens=100,
        ),
        INTERNAL,
    )["data"]


def _settle_body(authorized: dict[str, Any], *, success: bool = True) -> GatewaySettleRequest:
    return GatewaySettleRequest(
        authorization_id=authorized["authorization_id"], actual_input_tokens=14,
        actual_output_tokens=7, request_id="req-one-commit", finish_reason="stop",
        status="success" if success else "error", streamed=True, elapsed_seconds=2.0,
        selected_model=MODEL, selected_endpoint=authorized["endpoint_id"],
        error_status=None if success else 502,
        error_type=None if success else "provider_error",
    )


def _settle_route(
    authorized: dict[str, Any], *, success: bool = True,
) -> tuple[dict[str, Any], BackgroundTasks]:
    tasks = BackgroundTasks()
    body = _settle_body(authorized, success=success)
    if success:
        data = gateway._settle_gateway_with_admission_sync(
            body, settings=INTERNAL, background_tasks=tasks,
        )["data"]
    else:
        data = gateway._settle_gateway_authorization(
            body, success=False, settings=INTERNAL, background_tasks=tasks,
        )["data"]
    return data, tasks


def _run_background(tasks: BackgroundTasks) -> None:
    anyio.run(tasks)
    assert POST_COMMIT.wait_idle(10)


def _money(db: FakeSpannerDatabase, ws: str, key_hash: str) -> tuple[int, int, int, int]:
    credit = db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]
    key = db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]
    return credit["total_usage"], credit["reserved"], key["usage"], key["reserved"]


def test_authorize_settle_round_trip_commits_twice(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One authorize+settle round trip = 2 read-write commits.

    Before the one-commit settle the same round trip committed 4 times:
    ``tr_authorize``; the settle-outbox enqueue; ``tr_finalize`` (the finalize
    with the done-mark folded in by #1027); and the post-response
    ``tr_analytics_outbox`` benchmark INSERT. Commits per request is what
    Spanner CPU scales with, so this halves the settle path's write load.
    """
    store, db, key = prod_store
    log = CommitLog(monkeypatch)

    authorized = _authorize_route(key)
    assert log.take() == ["tr_authorize"]

    data, tasks = _settle_route(authorized)
    assert data["disposition"] == "finalized" and data["settled"] is True
    assert log.take() == ["tr_settle_one_commit"]
    _run_background(tasks)
    assert log.take() == []  # the benchmark rode in the money commit

    cost = data["cost_microdollars"]
    assert cost > 0
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    aid = authorized["authorization_id"]
    rid = authorized["credit_reservation_id"]
    row = db.settle_outbox[(aid, "settle")]
    assert (row["status"], row["attempts"], row["settle_body"], row["next_attempt_at"]) == (
        "done", 1, None, None,
    )
    assert row["terminal_at"] is not None
    assert (row["auto_refill_workspace_id"], row["auto_refill_status"]) == ("ws-one-commit", "pending")
    assert db.gateway_authorizations[aid]["settled"] is True
    assert db.gateway_authorizations[aid]["terminal_at"] is not None
    assert db.reservations[rid]["settled"] is True and db.reservations[rid]["terminal_at"] is not None
    assert len(db.generation_records) == 1
    assert [r["event_kind"] for r in db.operational_analytics_outbox] == ["activity"]
    [benchmark] = db.analytics_outbox
    assert benchmark["event_id"] == ProviderBenchmarkSample.from_generation(
        store.get_generation(data["generation_id"]),
    ).id


def test_refund_round_trip_commits_twice(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refunds take the same path. Before: 4 commits (authorize, enqueue,
    finalize, post-response provider-error benchmark)."""
    _store, db, key = prod_store
    log = CommitLog(monkeypatch)
    authorized = _authorize_route(key, "one-commit-refund")
    assert log.take() == ["tr_authorize"]

    data, tasks = _settle_route(authorized, success=False)
    assert data["disposition"] == "finalized" and data["finalization_outcome"] == "refunded"
    assert log.take() == ["tr_refund_one_commit"]
    _run_background(tasks)
    assert log.take() == []
    assert _money(db, "ws-one-commit", key.hash) == (0, 0, 0, 0)
    row = db.settle_outbox[(authorized["authorization_id"], "refund")]
    assert (row["status"], row["auto_refill_status"]) == ("done", None)
    [benchmark] = db.analytics_outbox
    assert '"status":"error"' in benchmark["payload"]


def _refuse_one_commit(monkeypatch: pytest.MonkeyPatch, db: FakeSpannerDatabase) -> list[int]:
    """Every attempt of the one-commit transaction aborts at commit, so the
    fake's runner exhausts its retries and raises; nothing it staged lands."""
    refused: list[int] = []
    original_run = FakeSpannerDatabase.run_in_transaction
    original_commit = FakeSpannerDatabase._try_commit
    active: list[bool] = [False]

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        active.append(kwargs.get("transaction_tag") == "tr_settle_one_commit")
        try:
            return original_run(database, fn, **kwargs)
        finally:
            active.pop()

    def commit(database: Any, txn: Any) -> bool:
        if active[-1]:
            refused.append(1)
            return False
        return original_commit(database, txn)

    monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
    monkeypatch.setattr(FakeSpannerDatabase, "_try_commit", commit)
    return refused


def test_aborted_one_commit_falls_back_to_durable_enqueue_then_drain_finalizes_once(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-aborts")

    def inline_finalize_lost(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("inline finalize lost")

    with pytest.MonkeyPatch.context() as patch:
        refused = _refuse_one_commit(patch, db)
        # The fallback's inline finalize fails too, so only the durable intent
        # remains: exactly the crash window the drain exists for.
        patch.setattr(
            type(store), "typed_finalize_gateway_authorization_result", inline_finalize_lost,
        )
        log = CommitLog(patch)

        data, _tasks = _settle_route(authorized)

        assert refused, "the one-commit transaction never reached its commit"
        assert data["disposition"] == "intent_durable" and data["settled"] is False
        # Only the durable enqueue committed; the refused attempt wrote nothing.
        assert log.take() == ["untagged"]
    aid = authorized["authorization_id"]
    assert db.settle_outbox[(aid, "settle")]["status"] == "pending"
    assert _money(db, "ws-one-commit", key.hash)[0] == 0
    assert db.analytics_outbox == [] and db.generation_records == {}

    db.settle_outbox[(aid, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
    first = drain_mod.drain_settle_outbox(10)
    assert first["outcomes"] == {ApplyOutcome.SETTLED_NOW: 1}
    second = drain_mod.drain_settle_outbox(10)
    assert second["claimed"] == 0

    row = db.settle_outbox[(aid, "settle")]
    assert row["status"] == "done"
    cost = row["actual_cost_micro"]
    assert cost > 0
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    assert len(db.generation_records) == 1
    assert len(db.operational_analytics_outbox) == 1


def test_aborted_one_commit_falls_back_to_the_two_commit_settle(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-fallback")
    refused = _refuse_one_commit(monkeypatch, db)
    log = CommitLog(monkeypatch)

    data, tasks = _settle_route(authorized)
    _run_background(tasks)

    assert refused
    assert data["disposition"] == "finalized" and data["settled"] is True
    # Today's flow, unchanged: enqueue, finalize (done-mark folded), benchmark.
    assert log.take() == ["untagged", "tr_finalize", "untagged"]
    cost = data["cost_microdollars"]
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    assert db.settle_outbox[(authorized["authorization_id"], "settle")]["status"] == "done"
    assert len(db.analytics_outbox) == 1


def test_unknown_outcome_one_commit_falls_back_without_double_booking(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit lands but the caller sees a deadline. The fallback must
    resolve as a replay: the enqueue finds the committed done row, the
    finalize claims 0 rows, and nothing books twice, including the drain."""
    _store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-unknown")
    original_run = FakeSpannerDatabase.run_in_transaction

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        result = original_run(database, fn, **kwargs)
        if kwargs.get("transaction_tag") == "tr_settle_one_commit":
            raise DeadlineExceeded("commit outcome unknown")
        return result

    monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
    log = CommitLog(monkeypatch)

    data, tasks = _settle_route(authorized)
    _run_background(tasks)

    aid = authorized["authorization_id"]
    committed = log.take()
    assert committed[0] == "tr_settle_one_commit"  # it did land
    assert "tr_finalize" in committed  # the fallback finalize ran and claimed nothing
    assert data["already_settled"] is True and data["settled"] is True
    assert data["finalization_outcome"] == "settled"
    cost = data["cost_microdollars"]
    assert cost > 0
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    row = db.settle_outbox[(aid, "settle")]
    assert (row["status"], row["attempts"]) == ("done", 1)
    assert row["auto_refill_status"] == "pending"
    assert len(db.generation_records) == 1
    assert len(db.operational_analytics_outbox) == 1
    assert len(db.analytics_outbox) == 1

    drained = drain_mod.drain_settle_outbox(10)
    assert drained["claimed"] == 0
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)


def test_intent_created_after_a_committed_one_commit_drains_to_a_replay(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    """A refund delivered concurrently (its authorization read predates the
    settle commit) records its own intent AFTER the one-commit settle. It must
    drain to a replay of the charged settle, never a refund or a second charge."""
    store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-then-refund")
    stale = copy.deepcopy(store.get_gateway_authorization(authorized["authorization_id"]))
    data, tasks = _settle_route(authorized)
    _run_background(tasks)
    cost = data["cost_microdollars"]

    refund = gateway._settle_gateway_authorization(
        _settle_body(authorized, success=False), success=False, settings=INTERNAL,
        background_tasks=BackgroundTasks(), _authorization=stale,
    )["data"]

    aid = authorized["authorization_id"]
    assert refund["already_settled"] is True and refund["finalization_outcome"] == "settled"
    assert db.settle_outbox[(aid, "refund")]["status"] == "pending"
    db.settle_outbox[(aid, "refund")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
    drained = drain_mod.drain_settle_outbox(10)
    assert drained["outcomes"] == {ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE: 1}
    assert db.settle_outbox[(aid, "refund")]["status"] == "done"
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)


def test_concurrent_duplicate_deliveries_charge_exactly_once(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two deliveries of one settle both pass the settled check and both stage a
    one-commit claim; the loser's commit conflicts, its retry finds the claim
    taken, and it resolves through the two-commit flow as a replay."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-duplicate")
    barrier = threading.Barrier(2)
    original_run = FakeSpannerDatabase.run_in_transaction

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") != "tr_settle_one_commit":
            return original_run(database, fn, **kwargs)
        first = [True]

        def staged_then_meet(txn: Any) -> Any:
            result = fn(txn)
            if first[0]:
                first[0] = False
                barrier.wait(timeout=10)  # both claims staged before either commits
            return result

        return original_run(database, staged_then_meet, **kwargs)

    monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
    body = _settle_body(authorized)

    def deliver() -> dict[str, Any]:
        return gateway._settle_gateway_authorization(
            body, success=True, settings=INTERNAL, background_tasks=BackgroundTasks(),
        )["data"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: deliver(), range(2)))

    dispositions = sorted(result["disposition"] for result in results)
    assert dispositions == ["already_finalized", "finalized"]
    assert all(result["settled"] is True for result in results)
    cost = next(r for r in results if r["disposition"] == "finalized")["cost_microdollars"]
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    row = db.settle_outbox[(authorized["authorization_id"], "settle")]
    assert (row["status"], row["attempts"]) == ("done", 1)
    assert len(db.generation_records) == len(db.operational_analytics_outbox) == 1
    assert len(db.analytics_outbox) == 1


def test_refund_that_wins_before_the_one_commit_declines_it_and_books_nothing(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-commit counterpart of test_request_snapshot_loses_to_refund_after_s1:
    a refund commits after this request read its authorization and before its
    one commit. The claim finds settled=true, the attempt rolls back, and the
    two-commit fallback records the settle intent for the drain to classify."""
    store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-refund-race")
    aid = authorized["authorization_id"]
    original = type(store).typed_settle_one_commit_result

    def refund_first(self: Any, authorization_id: str, **kwargs: Any) -> Any:
        assert kwargs["authorization_snapshot"].settled is False
        assert self.typed_finalize_gateway_authorization_result(
            authorization_id, success=False, actual_microdollars=0,
            selected_usage_type="Credits",
        ).finalized
        return original(self, authorization_id, **kwargs)

    monkeypatch.setattr(type(store), "typed_settle_one_commit_result", refund_first)
    log = CommitLog(monkeypatch)

    data, _tasks = _settle_route(authorized)

    committed = log.take()
    assert committed[0] == "tr_refund_finalize"  # the injected refund won
    assert "tr_settle_one_commit" not in committed
    assert data["already_settled"] is True and data["finalization_outcome"] == "refunded"
    assert _money(db, "ws-one-commit", key.hash) == (0, 0, 0, 0)
    assert db.reservations[authorized["credit_reservation_id"]]["actual_micro"] == 0
    assert db.gateway_authorizations[aid]["finalization_outcome"] == "refunded"
    # Left pending on purpose: only the drain can tell a lost charge from a replay.
    assert db.settle_outbox[(aid, "settle")]["status"] == "pending"
    assert db.generation_records == {} and db.analytics_outbox == []


def test_a_pending_intent_from_an_earlier_delivery_uses_the_two_commit_flow(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier delivery durably enqueued and then lost its inline finalize.
    The retry's one-commit INSERT meets ALREADY_EXISTS, so the existing pending
    intent is refreshed and resolved by the two-commit flow, exactly once."""
    _store, db, key = prod_store
    authorized = _authorize_route(key, "one-commit-retry")
    body = _settle_body(authorized)
    SpannerSettleOutbox(db, param_types).enqueue(
        SettleOutboxRow(
            authorization_id=authorized["authorization_id"], intent_kind="settle",
            settle_origin="typed", actual_cost_micro=1,
            reservation_id=authorized["credit_reservation_id"],
            selected_endpoint_id=authorized["endpoint_id"], model_id=MODEL,
            selected_usage_type="Credits", settle_body=body.model_dump_json(),
        ),
        initial_delay_seconds=60,
    )
    log = CommitLog(monkeypatch)
    data, tasks = _settle_route(authorized)
    _run_background(tasks)
    assert data["disposition"] == "finalized"
    committed = log.take()
    assert "tr_settle_one_commit" not in committed and "tr_finalize" in committed
    cost = data["cost_microdollars"]
    assert _money(db, "ws-one-commit", key.hash) == (cost, 0, cost, 0)
    row = db.settle_outbox[(authorized["authorization_id"], "settle")]
    assert row["status"] == "done" and row["actual_cost_micro"] == cost


def test_one_commit_attempt_is_budgeted_and_leaves_the_fallback_its_budget(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _db, key = prod_store
    authorized = _authorize_route(key, "one-commit-budget")
    remaining: dict[str, float] = {}
    original_run = FakeSpannerDatabase.run_in_transaction

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        deadline = storage_gcp_io._SPANNER_RPC_DEADLINE.get()
        assert deadline is not None
        remaining.setdefault(kwargs.get("transaction_tag") or "enqueue", deadline - time.monotonic())
        if kwargs.get("transaction_tag") == "tr_settle_one_commit":
            raise DeadlineExceeded("one-commit attempt used its budget")
        return original_run(database, fn, **kwargs)

    monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
    data, _tasks = _settle_route(authorized)

    assert data["disposition"] == "finalized"
    assert remaining["tr_settle_one_commit"] <= storage_gcp_authorize.ONE_COMMIT_SETTLE_BUDGET_SECONDS
    # The durable fallback still has (nearly) the whole route budget.
    assert remaining["enqueue"] > 15.0 and remaining["tr_finalize"] > 15.0


@pytest.mark.usefixtures("fixed_catalog")
def test_shapes_without_in_commit_durability_never_attempt_one_commit() -> None:
    """Rolling legacy request records and a store without in-commit activity
    delivery keep today's two-commit flow without spending an attempt."""
    for options in (
        dict(request_record_write_mode="legacy", operational_analytics_outbox_enabled=True),
        dict(request_record_write_mode="typed", operational_analytics_outbox_enabled=False),
    ):
        store, db = make_fake_store(generation_records_enabled=True, **options)
        workspace = Workspace(id="ws-shape", name="Shape", owner_user_id="user-shape")
        store._write_entity("workspace", workspace.id, workspace)
        store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
        db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
            "workspace_id": workspace.id, "shard": 0, "total_credits": TOTAL_CREDIT,
            "total_usage": 0, "reserved": 0, "source_updated_at": None, "updated_at": None,
        }
        _raw, key = store.api_keys.create(
            workspace_id=workspace.id, name="k", creator_user_id=workspace.owner_user_id,
        )
        configure_store(store)
        try:
            authorized = _authorize_route(key, f"shape-{options['request_record_write_mode']}")
            data, _tasks = _settle_route(authorized)
        finally:
            configure_store(InMemoryStore())
        assert data["disposition"] == "finalized"
        assert "tr_settle_one_commit" not in db.transaction_tags
        assert db.settle_outbox[(authorized["authorization_id"], "settle")]["status"] == "done"
