"""PR 2: observe the actual ASGI reply, not TestClient's background-task join."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio
import pytest

from clickhouse.ingest_operational_outbox import OperationalOutboxRow, drain_once
from tests.fakes.spanner import _FakeTransaction, make_fake_store
from tests.test_operational_analytics import _Source, _Writer
from tests.test_settle_outbox_drain import (
    _make_key,
    _seed_credit,
    _settle_json,
    _typed_authorization,
    _typed_credit,
)
from trusted_router import post_commit
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import ProviderBenchmarkSample


@pytest.fixture(autouse=True)
def optional_executor(monkeypatch: pytest.MonkeyPatch) -> Any:
    executor = post_commit.PostCommitExecutor()
    monkeypatch.setattr(post_commit, "POST_COMMIT", executor)
    yield executor
    executor.executor.shutdown(wait=True)
    assert executor.in_flight == 0


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> Any:
    store, db, bt = make_fake_store(
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
        request_record_write_mode="typed",
    )
    store.generation_store._analytics_outbox = SpannerAnalyticsOutbox(db, store._param_types)
    configure_store(store)
    ws = "ws-post-reply"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    # Unrelated bookkeeping is already covered separately. Keep operation
    # counts scoped to settlement and its mirrors, with broadcast enqueue real.
    monkeypatch.setattr(gateway, "record_successful_api_call_safely", lambda *a, **kw: None)
    monkeypatch.setattr(gateway, "should_drain_inline", lambda settings: False)
    monkeypatch.setattr(
        "trusted_router.services.budget_alerts.maybe_send_budget_alerts",
        lambda **kw: None,
    )
    app = create_app(
        Settings(environment="test", settle_outbox_enabled=True),
        configure_store_arg=False,
        init_observability=False,
    )
    # Observe the registered route response before BaseHTTPMiddleware
    # re-streams its already-sent body concurrently with background work.
    app.state.original_middleware = list(app.user_middleware)
    app.user_middleware.clear()
    return store, db, bt, auth, app


async def _request(
    app: Any, body: dict[str, Any], on_reply: Callable[[], None], *, refund: bool = False,
) -> dict[str, Any]:
    path = "/v1/internal/gateway/refund" if refund else "/v1/internal/gateway/settle"
    payload = json.dumps(body).encode()
    received = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            on_reply()

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }, receive, send)
    assert messages[0]["status"] == 200, messages
    return json.loads(b"".join(m.get("body", b"") for m in messages))


def _counts(db: Any) -> tuple[int, int, int, int]:
    return (db.snapshot_execute_sql_calls, db.transaction_execute_sql_calls,
            db.transaction_execute_update_calls, db.commits)


def test_reply_operation_count_and_exact_background_payloads(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, optional_executor: Any,
) -> None:
    store, db, bt, auth, app = scenario
    start = _counts(db)
    reply_counts: list[tuple[int, ...]] = []
    transactions: list[tuple[Any, str]] = []
    original = _FakeTransaction.execute_update

    def spy(self: Any, sql: str, **kwargs: Any) -> int:
        transactions.append((self, sql))  # retain objects: id reuse cannot alias commits
        return original(self, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", spy)

    def on_reply() -> None:
        reply_counts.append(tuple(a - b for a, b in zip(_counts(db), start, strict=True)))
        assert db.gateway_authorizations[auth.id]["settled"] is True
        assert db.settle_outbox[(auth.id, "settle")]["status"] == "done"
        assert len(db.generation_records) == len(db.operational_analytics_outbox) == 1
        assert db.analytics_outbox == []
        assert bt.committed == []

    response = asyncio.run(_request(app, _settle_json(auth.id), on_reply))
    assert response["data"]["disposition"] == "finalized"
    optional_executor.executor.shutdown(wait=True)
    # S1-S24 (S9's re-read is reused since #1331) plus S27; no T4 or
    # Bigtable calls before the response body.
    assert reply_counts == [(3, 4, 13, 2)]
    assert tuple(a - b for a, b in zip(_counts(db), start, strict=True)) == (4, 4, 14, 3)
    [activity_tx] = [tx for tx, sql in transactions if sql.startswith("INSERT INTO tr_operational_analytics_outbox")]
    [generation_tx] = [tx for tx, sql in transactions if sql.startswith("INSERT INTO tr_generation")]
    credit_tx = [tx for tx, sql in transactions if sql.startswith("UPDATE tr_credit_balance")]
    assert activity_tx is generation_tx and activity_tx in credit_tx
    [benchmark_tx] = [tx for tx, sql in transactions if sql.startswith("INSERT INTO tr_analytics_outbox")]
    assert benchmark_tx is not activity_tx
    [record] = db.generation_records.values()
    generation = store.get_generation(record["generation_id"])
    assert generation is not None
    expected = ProviderBenchmarkSample.from_generation(generation)
    [benchmark] = db.analytics_outbox
    assert benchmark["event_id"] == expected.id
    assert json.loads(benchmark["payload"]) == json.loads(json_body(expected))
    assert len(bt.committed) == 9
    activity_rows = [row for key, row in bt.rows.items() if not key.startswith(b"benchmark")]
    benchmark_rows = [row for key, row in bt.rows.items() if key.startswith(b"benchmark")]
    assert len(activity_rows) == 3 and len(benchmark_rows) == 6
    for row in activity_rows:
        assert json.loads(row[store.activity_family][b"body"][0].value) == json.loads(json_body(generation))
    for row in benchmark_rows:
        assert json.loads(row[store.benchmark_family][b"body"][0].value) == json.loads(json_body(expected))


@pytest.mark.parametrize("target", [
    "activity", "benchmark_outbox", "benchmark_mirror", "refund_outbox", "refund_mirror",
])
@pytest.mark.parametrize("fail", [False, True])
def test_stalled_or_failing_writes_do_not_hold_the_reply(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, target: str, fail: bool,
    optional_executor: Any,
) -> None:
    store, db, bt, auth, app = scenario
    # Exercise the full HTTP middleware stack for the latency guarantee.
    app.user_middleware = app.state.original_middleware
    replied, entered, release = threading.Event(), threading.Event(), threading.Event()
    if target in {"benchmark_outbox", "refund_outbox"}:
        owner, name = SpannerAnalyticsOutbox, "enqueue"
    else:
        from trusted_router import storage_gcp_generations
        owner = storage_gcp_generations
        name = "_bt_write_generation" if target == "activity" else "_bt_write_provider_benchmark"
    original = getattr(owner, name)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(30), "test did not release stalled write"
        if fail:
            raise TimeoutError("simulated write deadline")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, blocked)
    body = _settle_json(auth.id)
    if target.startswith("refund"):
        body.update(status="error", error_status=503, error_type="provider_error")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, _request(app, body, replied.set, refund=target.startswith("refund")))
        try:
            assert replied.wait(10), "settle reply waited for optional write"
            assert entered.wait(10)
            assert optional_executor.in_flight == 1, "optional write must still be stalled"
            assert future.result(timeout=10)["data"]["disposition"] == "finalized"
            assert len(db.operational_analytics_outbox) == (0 if target.startswith("refund") else 1)
        finally:
            release.set()
        response = future.result(timeout=10)
    optional_executor.executor.shutdown(wait=True)
    assert response["data"]["disposition"] == "finalized"
    assert db.reservations[auth.credit_reservation_id]["settled"] is True


class _ProcessStopped(BaseException):
    pass


def test_restart_after_reply_repairs_activity_and_preserves_durable_broadcast(scenario: Any) -> None:
    store, db, bt, auth, app = scenario
    destination = store.create_broadcast_destination(
        workspace_id=auth.workspace_id, type="webhook", name="durable",
        endpoint="https://example.invalid/events",
    )

    def stop_at_reply() -> None:
        assert db.gateway_authorizations[auth.id]["settled"] is True
        assert len(db.operational_analytics_outbox) == 1
        assert not bt.committed and not db.analytics_outbox
        raise _ProcessStopped()

    with pytest.raises(_ProcessStopped):
        asyncio.run(_request(app, _settle_json(auth.id), stop_at_reply))
    # The worker starts using only committed rows, with no response task alive.
    [event] = db.operational_analytics_outbox
    row = OperationalOutboxRow(
        shard=event["shard"], commit_ts=dt.datetime.now(dt.UTC),
        event_kind=event["event_kind"], event_id=event["event_id"], payload=event["payload"],
    )
    source, writer = _Source([row]), _Writer(failures=1)
    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        drain_once(source, writer, batch_size=10)
    assert not source.deleted
    result = drain_once(source, writer, batch_size=10)
    assert result.inserted == 1 and source.deleted == [row]
    assert writer.batches[0] == writer.batches[1]
    jobs = store.due_broadcast_deliveries()
    assert len(jobs) == 1
    assert jobs[0].destination_id == destination.id
    assert jobs[0].generation_id == event["event_id"]
    assert jobs[0].settle_body["request_id"] == "req-settle"
    assert "client" not in jobs[0].settle_body
    assert "price_tier_input_tokens" not in jobs[0].settle_body


def test_unexpected_background_error_does_not_abort_later_tasks(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    optional_executor: Any,
) -> None:
    store, db, bt, auth, app = scenario
    later: list[str] = []

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected mirror failure")

    monkeypatch.setattr(type(store.generation_store), "mirror_after_commit", fail)
    monkeypatch.setattr(gateway, "record_successful_api_call_safely", lambda *a, **kw: later.append("ran"))
    response = asyncio.run(_request(app, _settle_json(auth.id), lambda: None))
    assert response["data"]["disposition"] == "finalized"
    optional_executor.executor.shutdown(wait=True)
    assert later == ["ran"]
    assert "settle_post_commit_mirrors_failed" in caplog.text


@pytest.mark.parametrize("refund", [False, True])
def test_saturation_is_bounded_and_does_not_borrow_authorize_tokens(
    scenario: Any, monkeypatch: pytest.MonkeyPatch, optional_executor: Any, refund: bool,
) -> None:
    store, db, bt, auth, app = scenario
    release = threading.Event()
    entered = threading.Barrier(post_commit.WORKERS + 1)
    write_lock = threading.Lock()
    started: list[str] = []
    # Block each of the optional RPC stages. The first four chains remain
    # stalled throughout admission; every queued stage also sees the gate.
    from trusted_router import storage_gcp_generations

    def stall(original: Any) -> Any:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            with write_lock:
                started.append(threading.current_thread().name)
                first_workers = len(started) <= post_commit.WORKERS
            if first_workers:
                entered.wait(timeout=30)
            assert release.wait(30)
            # The in-memory transaction fake isn't a concurrent database.
            with write_lock:
                return original(*args, **kwargs)
        return blocked

    for name in ("_bt_write_generation", "_bt_write_provider_benchmark"):
        monkeypatch.setattr(storage_gcp_generations, name, stall(getattr(storage_gcp_generations, name)))
    monkeypatch.setattr(SpannerAnalyticsOutbox, "enqueue", stall(SpannerAnalyticsOutbox.enqueue))
    count = post_commit.MAX_IN_FLIGHT + 16
    _typed_credit(db, auth.workspace_id)["total_credits"] = 10**12
    key = _make_key(store, auth.workspace_id, limit=None)
    authorizations = [auth] + [
        _typed_authorization(store, workspace_id=auth.workspace_id, key_hash=key.hash)
        for _ in range(count - 1)
    ]
    replies: list[str] = []
    submitted_payloads: list[Any] = []
    original_submit = optional_executor.submit

    def spy(task: Any, *args: Any, **kwargs: Any) -> None:
        submitted_payloads.append(args[0])
        original_submit(task, *args, **kwargs)

    monkeypatch.setattr(optional_executor, "submit", spy)

    async def drive() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.borrowed_tokens == 0
        tasks = []
        try:
            # Replies, rather than ASGI completion, pace the next request:
            # this reproduces Cloud Run releasing its concurrency slots.
            for authorization in authorizations:
                replied = asyncio.Event()

                def on_reply(
                    authorization_id: str = authorization.id, event: asyncio.Event = replied,
                ) -> None:
                    replies.append(authorization_id)
                    event.set()

                body = _settle_json(authorization.id)
                if refund:
                    body.update(status="error", error_status=503, error_type="provider_error")
                tasks.append(asyncio.create_task(_request(app, body, on_reply, refund=refund)))
                await asyncio.wait_for(replied.wait(), timeout=5)
                await asyncio.sleep(0)
                assert optional_executor.in_flight <= post_commit.MAX_IN_FLIGHT
            assert len(replies) == count
            # Let unrelated, unchanged response tasks finish before measuring
            # the shared limiter. Optional RPCs remain blocked by release.
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert all(r["data"]["disposition"] == "finalized" for r in responses)
            assert limiter.borrowed_tokens == 0
            # Even if optional work consumes all 40 AnyIO tokens, the
            # authorize probe must start BEFORE we release the stalled RPCs.
            authorize_started = threading.Event()
            probe = asyncio.create_task(anyio.to_thread.run_sync(authorize_started.set))
            tasks.append(probe)
            await asyncio.wait_for(asyncio.shield(probe), timeout=1)
            assert authorize_started.is_set()
            assert limiter.borrowed_tokens == 0
            assert optional_executor.in_flight == post_commit.MAX_IN_FLIGHT
            assert sum(optional_executor.drops.values()) == 16
        finally:
            # Also clean up mutations that deliberately use the shared pool.
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    # Main test thread participates in the barrier without using AnyIO.
    with ThreadPoolExecutor(max_workers=1) as driver:
        future = driver.submit(asyncio.run, drive())
        try:
            entered.wait(timeout=30)
            future.result(timeout=30)
        finally:
            release.set()
    optional_executor.executor.shutdown(wait=True)
    assert optional_executor.in_flight == 0
    assert len(started) == post_commit.MAX_IN_FLIGHT * (2 if refund else 3)
    assert all(name.startswith("settle-post-commit") for name in started)
    admitted = submitted_payloads[:post_commit.MAX_IN_FLIGHT]
    expected = [
        payload if refund else ProviderBenchmarkSample.from_generation(payload)
        for payload in admitted
    ]
    assert len(db.analytics_outbox) == len(expected) == post_commit.MAX_IN_FLIGHT
    assert {row["event_id"]: json.loads(row["payload"]) for row in db.analytics_outbox} == {
        sample.id: json.loads(json_body(sample)) for sample in expected
    }
    assert len(bt.committed) == post_commit.MAX_IN_FLIGHT * (6 if refund else 9)
    expected_payloads = {sample.id: json.loads(json_body(sample)) for sample in expected}
    if not refund:
        expected_payloads.update({g.id: json.loads(json_body(g)) for g in admitted})
    for row in bt.rows.values():
        for columns in row.values():
            payload = json.loads(columns[b"body"][0].value)
            assert payload == expected_payloads[payload["id"]]


def test_full_executor_submission_does_not_wait_for_a_slot(
    optional_executor: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()

    def stalled() -> None:
        assert release.wait(30)

    try:
        for _ in range(post_commit.MAX_IN_FLIGHT):
            optional_executor.submit(stalled)
        with ThreadPoolExecutor(max_workers=1) as reply_thread:
            future = reply_thread.submit(optional_executor.submit, stalled)
            try:
                future.result(timeout=1)
                for _ in range(10):
                    optional_executor.submit(stalled)
                assert optional_executor.drops == {"stalled": 11}
                assert caplog.text.count("post_commit_dropped kind=stalled") == 1
            finally:
                release.set()
    finally:
        release.set()


def test_executor_releases_slots_on_task_and_submission_failure(
    optional_executor: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    def fail() -> None:
        raise RuntimeError("unexpected task failure")

    optional_executor.submit(fail)
    optional_executor.executor.shutdown(wait=True)
    assert optional_executor.in_flight == 0
    # A submission rejected during shutdown must also return its reserved slot.
    optional_executor.submit(fail)
    assert optional_executor.in_flight == 0
    assert optional_executor.drops == {"fail": 1}
    assert "post_commit_submission_failed kind=fail" in caplog.text
    assert all(optional_executor._slots.acquire(blocking=False) for _ in range(post_commit.MAX_IN_FLIGHT))
    assert not optional_executor._slots.acquire(blocking=False)
