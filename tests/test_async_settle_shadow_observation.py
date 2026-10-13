"""P3 pure allocator, real Observer, native SDK faults and fake storage witnesses."""
from __future__ import annotations

import copy
import datetime as dt
import itertools
import time
from contextlib import contextmanager
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase
from tests.test_async_settle_shadow_sdk_retry import sdk_database as sdk_base
from tests.test_async_settle_shadow_v2_contract import fixture as p1_fixture
from tests.test_async_settle_shadow_v2_evidence import (  # noqa: F401 - grid fixture
    exhaustive,
    untraced,
)
from trusted_router.async_settle_shadow_health_state import Slots, summarize
from trusted_router.async_settle_shadow_journal import (
    CLAIM_KIND,
    COUNTER_KIND,
    HEALTH_KIND,
    Journal,
    PageState,
    ReceiptPage,
    Row,
    reserve_cap,
)
from trusted_router.async_settle_shadow_v2_evidence import RESERVATION_BLOCK
from trusted_router.config import Settings
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    decode_health,
    parse_health_record,
)
from trusted_router.services.async_settle_shadow_admission import Observer
from trusted_router.services.async_settle_shadow_health import Service
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore, UnknownCommit

NOW_US = int(dt.datetime(2026, 10, 12, tzinfo=dt.UTC).timestamp()*1_000_000)


class LogicalClock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


logical_time = LogicalClock()


def install_scheduler(monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace

    from trusted_router.services import async_settle_shadow_health as module
    logical_time.now = 100.

    async def sleep(seconds):
        logical_time.advance(seconds)
        await asyncio.sleep(0)

    scheduler = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    scheduler.sleep = sleep
    # Logical deadlines are enforced by Budget and the explicit loop clock.
    # No wall timer is allowed to race CPU-only fake-store work.
    scheduler.timeout = lambda seconds: nullcontext()
    monkeypatch.setattr(module, "asyncio", scheduler)
    return scheduler


@pytest.fixture(autouse=True)
def deterministic_scheduler(monkeypatch):
    created = []
    initialize = Service.__init__
    def tracked(service, *args, **kwargs):
        initialize(service, *args, **kwargs)
        created.append(service)
    monkeypatch.setattr(Service, "__init__", tracked)
    previous_clock = logical_time.now
    scheduler = install_scheduler(monkeypatch)
    try:
        yield scheduler
    finally:
        # Structural tests own these offline workers. Production shutdown may
        # use wait=False to honor its deadline; tests always join before leaving.
        for service in created:
            service.publisher_executor.shutdown(wait=True, cancel_futures=True)
            service.evidence_executor.shutdown(wait=True, cancel_futures=True)
            if service.observer.executor is not None:
                service.observer.executor.shutdown(wait=True, cancel_futures=True)
        logical_time.now = previous_clock


def fixture(name: str) -> Any:
    if name in {"counter", "closure", "controls", "health", "health_claim"}:
        import json
        from pathlib import Path
        return json.loads((Path(__file__).parent / "fixtures/async_settlement/shadow_v2_a1" / (name+".json")).read_text())["variants"]
    return p1_fixture(name)


@pytest.fixture
def sdk_database(monkeypatch: Any, shadow_deadline_clock: Any) -> Any:
    from google.cloud.spanner_v1.types import PartialResultSet
    db, clock, sleeps = sdk_base.__wrapped__(monkeypatch, shadow_deadline_clock)
    old = db.query
    def query(**kwargs: Any) -> Any:
        if "CURRENT_TIMESTAMP() AS claim_at" not in kwargs["request"].sql:
            return old(**kwargs)
        duration = db.read_durations.pop(0) if db.read_durations else db.read_duration
        db.rpc("read", kwargs, duration, db.read_errors)
        response = PartialResultSet(metadata=dict(transaction=dict(id=b"offline"), row_type=dict(fields=[
            dict(name="body", type=dict(code="STRING")), dict(name="claim_at", type=dict(code="TIMESTAMP"))])))
        response._pb.values.add(null_value=0)
        response._pb.values.add(string_value=dt.datetime.fromtimestamp(db.server_ns/1e9, dt.UTC).isoformat().replace("+00:00", "Z"))
        return iter([response])
    db.server_ns = NOW_US*1000
    db.spanner_api.execute_streaming_sql = query
    return db, clock, sleeps


def event_for(reservation: Any) -> dict[str, Any]:
    value = copy.deepcopy(next(v["object"] for v in fixture("receipt_pages") if v["schema"] == "AUTHORIZE_EVENT" and v["expected"] == "accept"))
    value.update(seq=f"{reservation.event_seq:020}", event_at_us=reservation.observed_us,
                 receipt_at_us=reservation.observed_us, authorization_day=reservation.day,
                 receipt_locator=reservation.coordinates)
    return value


def journal() -> Journal:
    result = Journal("issuer")
    result.grant("2026-10-12", RESERVATION_BLOCK)
    return result


def fake_store() -> tuple[ObservationStore, FakeSpannerDatabase]:
    class Reader:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped
        def insert(self, *, table: str, columns: Any, values: Any) -> None:
            for row in values:
                assert (row[0], row[1]) not in db.rows
            self.wrapped.insert_or_update(table=table, columns=columns, values=values)
        def __getattr__(self, name: str) -> Any:
            return getattr(self.wrapped, name)
        def execute_sql(self, sql: str, *, params: Any, param_types: Any, **options: Any) -> Any:
            assert options["retry"] is None
            assert 0 < options["timeout"] <= 2
            assert options["request_options"]["priority"] == "PRIORITY_LOW"
            from types import SimpleNamespace

            from google.api_core.datetime_helpers import DatetimeWithNanoseconds
            from google.protobuf.timestamp_pb2 import Timestamp

            from trusted_router.storage_gcp_shadow_observation import entity_point_statement
            stamp = DatetimeWithNanoseconds.from_timestamp_pb(Timestamp(seconds=db.server_ns//10**9, nanos=db.server_ns%10**9))
            if "CURRENT_TIMESTAMP() AS claim_at" in sql:
                base = self.wrapped.execute_sql(entity_point_statement(params["kind"], params["id"])[0], params=params, param_types=param_types)
                return [(list(base)[0][0] if (params["kind"], params["id"]) in db.rows else None, stamp)]
            if "SELECT body, updated_at" in sql:
                base = list(self.wrapped.execute_sql(entity_point_statement(params["kind"], params["id"])[0], params=params, param_types=param_types))
                values = [(r[0], stamp) for r in base]
            else:
                values = list(self.wrapped.execute_sql(sql, params=params, param_types=param_types))
            class Stream(list):
                metadata = SimpleNamespace(transaction=SimpleNamespace(read_timestamp=stamp))
            return Stream(values)
    class Database(FakeSpannerDatabase):
        _trusted_router_rpc_deadlines = True
        def run_in_transaction(self, callback: Any, **options: Any) -> Any:
            assert options.pop("commit_request_options") == {"priority": "PRIORITY_LOW"}
            return super().run_in_transaction(lambda tx: callback(Reader(tx)), **options)
        @contextmanager
        def snapshot(self, **kwargs: Any) -> Any:
            with super().snapshot(**kwargs) as reader:
                yield Reader(reader)
    db = Database()
    db.server_ns = NOW_US*1000
    db._trusted_router_rpc_deadlines = True
    return ObservationStore(db, clock=logical_time, wall=lambda: NOW_US/1_000_000+100), db


def health(*, now_us: int = NOW_US, rows: list[Any] | None = None) -> dict[str, Any]:
    return summarize(rows or [], boot="publisher", region="us-central1", revision="a"*40,
        slot=now_us//2_000_000, observed_ns=now_us*1000, age_seconds=now_us/1e6, row_limit=10000)


def installed(body: dict[str, Any], *, read_ns: int | None = None, reply_at: float = 10.) -> dict[str, Any]:
    return dict(body=body, committed_at_ns=body["observed_at_ns"],
                read_at_ns=body["observed_at_ns"] if read_ns is None else read_ns,
                read_elapsed_ns=0, reply_at=reply_at)


def service() -> Service:
    store, _ = fake_store()
    return Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)


def test_reservation_order_freeze_and_hwm() -> None:
    j = journal()
    a, b = j.reserve(NOW_US), j.reserve(NOW_US+1)
    assert a is not None and b is not None
    j.finalize(b, event_for(b))
    assert j.freeze(a.day) == []
    assert j.days[a.day].hwm == 0
    j.finalize(a, event_for(a))
    rows = j.freeze(a.day)
    assert len(rows) == 1
    j.acknowledge(rows)
    assert j.days[a.day].hwm == 0
    j.complete(b)
    j.verify_complete(b, [p.row for p in j.pages if p.row is not None])
    assert j.days[a.day].hwm == 0
    j.complete(a)
    j.verify_complete(a, [p.row for p in j.pages if p.row is not None])
    assert j.days[a.day].hwm == 2 and not j.pages
    c = j.reserve(NOW_US+2)
    assert c is not None and c.first_seq == 3
    j.finalize(c, event_for(c))
    assert j.freeze(c.day)[0].value()["previous_page_sha256"] == rows[0].digest


def test_four_pages_are_shared_across_three_days() -> None:
    j = journal()
    for offset in range(3):
        stamp = NOW_US+offset*86_400_000_000
        day = dt.datetime.fromtimestamp(stamp/1e6, dt.UTC).date().isoformat()
        if offset:
            j.grant(day, RESERVATION_BLOCK)
        r = j.reserve(stamp)
        assert r is not None
        j.freeze(day)  # unfinished, counts against global page cap
    r = j.reserve(NOW_US)
    assert r is not None
    j.freeze(r.day)
    assert len(j.pages) == 4
    assert j.reserve(NOW_US) is None
    assert j.losses == 1 and len(j.pages) == 4
    assert j.reserve(NOW_US+3*86_400_000_000) is None
    assert len(j.days) == 3


def test_full_slot_reservations_include_wrapper() -> None:
    j = journal()
    reservations = [j.reserve(NOW_US) for _ in range(32)]
    assert all(r is not None for r in reservations)
    assert len(j.pages) == 2
    assert len(j.pages[0].slots) == 31
    assert j.pages[0].retained_bytes < 65536
    assert j.retained_bytes < 4*65536
    assert all(s.body is None for p in j.pages for s in p.slots)
    assert j.freeze("2026-10-12") == []


def test_immutable_finalize_and_native_validation() -> None:
    j = journal()
    r = j.reserve(NOW_US)
    assert r is not None
    ev = event_for(r)
    original = j.finalize(r, ev)
    assert j.finalize(r, ev) == original
    ev["diagnostic"] = dict(reason="failed", required_bytes=0, candidate_count=0)
    with pytest.raises(ValueError, match="slot_conflict"):
        j.finalize(r, ev)
    ev = event_for(r)
    ev["event_at_us"] = True
    with pytest.raises(ValueError):
        j.finalize(r, ev)
    assert j.slot(r).body == original


@exhaustive
def test_page_transition_grid() -> None:
    for state, operation in itertools.product(PageState, ("seal", "freeze", "unknown", "acknowledge")):
        p = ReceiptPage("2026-10-12", "issuer", 1, state=state)
        legal = {"seal": {PageState.OPEN}, "freeze": {PageState.SEALED},
                 "unknown": {PageState.FROZEN}, "acknowledge": set()}[operation]
        if operation == "freeze" and state in legal:
            # Empty pages cannot be frozen into valid evidence.
            with pytest.raises((ValueError, IndexError)):
                p.freeze(None)
        elif state in legal:
            getattr(p, operation)()
        else:
            with pytest.raises(ValueError):
                getattr(p, operation)(*( [None] if operation == "freeze" else [b""] if operation == "acknowledge" else []))


def test_unknown_commit_needs_exact_body_and_contiguous_references() -> None:
    j = journal()
    r = j.reserve(NOW_US)
    assert r is not None
    j.finalize(r, event_for(r))
    rows = j.freeze(r.day)
    j.checkpoint(r.day)
    j.failed(r.day, unknown=True)
    assert j.pages[0].state == PageState.UNKNOWN
    with pytest.raises(ValueError, match="page_conflict"):
        j.pages[0].acknowledge(b"wrong")
    assert j.days[r.day].hwm == 0
    j.acknowledge(rows)
    j.complete(r)
    j.verify_complete(r, [p.row for p in j.pages if p.row is not None])
    j.days[r.day].closing = True
    assert not j.can_close(r.day, inflight=0, pending=0, interval_end_us=NOW_US, folded_us=NOW_US)
    j.reconciled_commit(r.day)
    assert j.can_close(r.day, inflight=0, pending=0, interval_end_us=NOW_US, folded_us=NOW_US)
    j.loss(r.day)
    assert not j.can_close(r.day, inflight=0, pending=0, interval_end_us=NOW_US, folded_us=NOW_US)


@pytest.mark.parametrize("field,limit", [("terminal_reserved", 100000), ("event_reserved", 400000)])
def test_cap_last_block_and_overflow(field: str, limit: int) -> None:
    value = dict(v=2, policy_version="shadow-v2", contract_revision="a1", day="2026-10-12", terminal_reserved=0, event_reserved=0)
    value[field] = limit-100
    value = reserve_cap(value, terminal=field == "terminal_reserved", event=field == "event_reserved")
    assert value[field] == limit
    with pytest.raises(ValueError, match="daily_cap"):
        reserve_cap(value, terminal=field == "terminal_reserved", event=field == "event_reserved")


@exhaustive
def test_slot_offsets_and_reversals() -> None:
    for offset in (0, 1, 249_999, 250_000, 250_001, 999_999, 1_000_000, 1_999_998, 1_999_999):
        slots = Slots()
        slot, missed = slots.tick(NOW_US+offset, busy=False)
        assert (slot is not None) == (offset <= 250_000)
        assert missed == int(offset > 250_000)
        assert slots.tick(NOW_US+offset, busy=False) == (None, 0)
        assert slots.tick(NOW_US-2_000_000, busy=False) == (None, 0)
    slots = Slots()
    assert slots.tick(NOW_US, busy=True)[1] == 1
    assert slots.tick(NOW_US+6_000_000, busy=False)[1] == 2
    slots.stopped = True
    assert slots.tick(NOW_US+8_000_000, busy=False) == (None, 0)


@pytest.mark.parametrize("field,value", [("complete", False), ("sample_count", 10001),
    ("observed_at_us", NOW_US+1), ("publisher_heartbeat_us", NOW_US+1), ("v", True)])
def test_real_observer_unknown_health(field: str, value: Any) -> None:
    observer = Observer(frozenset({"ws"}), lambda _: Admission(0, 2), lambda: None,
        policy="shadow-v2", clock=lambda: 10., wall=lambda: NOW_US/1e6)
    observer.install_workspace("ws", 10., Admission(0, 2))
    h = health()
    h[field] = value
    if field == "sample_count":
        h["backlog_count"] = value
    observer.install_health(10., installed(h))
    assert observer.peek("ws")["prediction"] == "unknown"


def test_real_observer_yes_no_and_five_second_boundary() -> None:
    wall = [NOW_US/1e6]
    observer = Observer(frozenset({"ws"}), lambda _: Admission(0, 2), lambda: None,
        policy="shadow-v2", clock=lambda: 10., wall=lambda: wall[0])
    observer.install_workspace("ws", 10., Admission(0, 2))
    observer.install_health(10., installed(health()))
    assert observer.peek("ws")["prediction"] == "yes"
    assert observer.peek("ws")["prediction_basis"] == "counterfactual_backlog_v2"
    h = health(rows=[(dt.datetime.fromtimestamp((NOW_US-6_000_000)/1e6, dt.UTC), 1, "dead")])
    observer.install_health(10., installed(h))
    assert observer.peek("ws")["prediction"] == "no"
    observer.install_health(10., installed(h, read_ns=h["observed_at_ns"]+5_000_000_000))
    assert observer.peek("ws")["prediction"] == "unknown"
    assert observer.health["body"] == h  # no timestamp rejuvenation


def test_shadow_body_rejected_by_money_reader() -> None:
    h = health()
    assert parse_health_record(Row.build(HEALTH_KIND, "local/fleet", h).body.decode()) is None
    assert decode_health(h, now=10, wall=NOW_US/1e6) is None
    cache = AdmissionCache(lambda _: Admission(0, 2), health_read=lambda: h)
    assert cache.health_read is not None
    assert decode_health(cache.health_read(), now=10, wall=NOW_US/1e6) is None


@pytest.mark.parametrize("created,status,complete", [(None, "pending", False),
    (dt.datetime(1970, 1, 1, tzinfo=dt.UTC), "dead", False),
    (dt.datetime(2026, 10, 12, tzinfo=dt.UTC), "dead", True),
    (dt.datetime(2026, 10, 12, tzinfo=dt.UTC), "pending", True),
    (dt.datetime(2026, 10, 12, tzinfo=dt.UTC), "alien", False)])
def test_health_membership_and_unknown_age(created: Any, status: str, complete: bool) -> None:
    value = health(rows=[(created, 4, status)])
    assert value["complete"] == complete
    assert value["backlog_count"] == 1
    assert value["dead_count"] == int(status == "dead")


def test_claim_fence_and_delayed_winner_fake() -> None:
    store, db = fake_store()
    budget = Budget(logical_time()+.5, logical_time)
    slot = NOW_US//2_000_000
    assert store.claim("first", slot, NOW_US, budget)
    assert not store.claim("second", slot, NOW_US, budget)
    h = health()
    h["publisher_boot_id"] = "first"
    assert store.publish(h, budget)
    assert all(kind in (HEALTH_KIND, CLAIM_KIND) for kind, _ in db.rows)
    found = store.read_shadow_health()
    assert found is not None and found["body"] == h
    db.server_ns += 2_000_000_000
    assert store.claim("second", slot+1, NOW_US+2_000_000, budget)
    assert not store.publish(h, budget)
    assert all(kind in (HEALTH_KIND, CLAIM_KIND) for kind, _ in db.rows)
    assert db.transaction_execute_update_calls == 0


def test_idle_checkpoint_close_and_recovery() -> None:
    svc = service()
    day = "2026-10-12"
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[day].attempts == 1
    svc.journal.failed(day, unknown=False)
    svc.termination_deadline = svc.store.clock()+9
    svc.closing_reason, svc.interval_end_us = "shutdown", NOW_US
    svc.flush(svc.store.clock()+4)
    assert not svc.journal.days
    row = svc.store.read(COUNTER_KIND, day+"/"+svc.journal.boot, Budget(logical_time()+1, logical_time))
    assert row is not None
    assert row.value()["checkpoint_recoveries"] == row.value()["checkpoint_failures"] == 1
    assert row.value()["closed"]


def test_exact_unknown_commit_readback_late_landing() -> None:
    store, db = fake_store()
    row = Row.build(HEALTH_KIND, "local/fleet", health())
    assert not store.reconcile([row], Budget(logical_time()+1, logical_time))
    with db.batch() as batch:
        store.write(batch, [row], Budget(logical_time()+1, logical_time))
    assert store.reconcile([row], Budget(logical_time()+1, logical_time))
    different = Row.build(HEALTH_KIND, row.key, {**health(), "publisher_boot_id": "other"})
    with pytest.raises(ValueError, match="unknown_commit_conflict"):
        store.reconcile([different], Budget(logical_time()+1, logical_time))


@pytest.mark.parametrize("fault", ["abort", "reset", "lost_reply", "deadline"])
def test_native_sdk_no_retry_shared_deadline(sdk_database: Any, fault: str) -> None:  # noqa: F811
    from google.api_core.exceptions import Aborted, InternalServerError, ServiceUnavailable
    db, clock, sleeps = sdk_database
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    if fault == "deadline":
        db.commit_duration = 1.
    else:
        db.errors = [{"abort": Aborted("abort"), "reset": InternalServerError("RST_STREAM"),
                      "lost_reply": ServiceUnavailable("lost")}[fault]]
    started = clock.monotonic()
    with pytest.raises(UnknownCommit):
        store.claim("issuer", NOW_US//2_000_000, NOW_US, Budget(started+.5, clock.monotonic))
    assert not sleeps
    assert clock.monotonic()-started <= .5
    assert sum(name == "commit" for name, _ in db.calls) == 1
    assert all(timeout <= .5 for _, timeout in db.calls)


def maximum_rows(boot: str = "b"*64) -> tuple[list[Row], Row, Row]:
    import hashlib

    from tests.test_async_settle_shadow_v2_evidence import padded, page_with_events
    from trusted_router.async_settle_shadow_journal import CLOSURE_KIND, PAGE_KIND
    pages = []
    previous = None
    for i in range(4):
        value = page_with_events(31)
        value["boot"] = boot
        value["first_seq"] = f"{31*i+1:020}"
        value["last_seq"] = f"{31*(i+1):020}"
        value["previous_page_sha256"] = previous
        for j, event in enumerate(value["events"]):
            event["seq"] = f"{31*i+j+1:020}"
            event["receipt_locator"].update(boot=boot, first_seq=value["first_seq"], event_seq=event["seq"])
        raw = padded(value, 65536)
        pages.append(Row(PAGE_KIND, f'{value["day"]}/{boot}/{value["first_seq"]}', raw))
        previous = hashlib.sha256(raw).hexdigest()
    value = copy.deepcopy(fixture("counter")[0]["object"])
    value.update(boot=boot, event_seq=124, durable_event_hwm=124, receipt_count=124, receipt_chain_sha256=previous)
    counter = Row(COUNTER_KIND, f'{value["day"]}/{boot}', padded(value, 65536))
    value = copy.deepcopy(fixture("closure")[0]["object"])
    value.update(boot=boot, final_event_seq=124, durable_event_hwm=124,
                 receipt_chain_sha256=previous, counter_sha256=counter.digest)
    closure = Row(CLOSURE_KIND, counter.key, padded(value, 4096))
    for row in [*pages, counter, closure]:
        row.value()
    return pages, counter, closure


@exhaustive
def test_native_encoding_six_maximum_rows(tmp_path: Any) -> None:
    import json
    import resource
    import tracemalloc
    from types import SimpleNamespace

    from google.cloud.spanner_v1.transaction import Transaction
    from google.cloud.spanner_v1.types import CommitRequest
    store, db = fake_store()
    tracemalloc.start()
    try:
        pages, counter, closure = maximum_rows()
        rows = [*pages, counter, closure]
        from trusted_router.async_settle_shadow_journal import SAMPLE_KIND
        from trusted_router.detached_jws import canonical
        retained_work = []
        for index in range(32):
            value = copy.deepcopy(fixture("sample")[0]["object"])
            value["authorization_id"] = f"auth-{index:04d}"
            raw = canonical(value)
            record = Row(SAMPLE_KIND, value["authorization_day"]+"/"+value["authorization_id"], raw+b" "*(8192-len(raw)))
            record.value()
            retained_work.append(record)
        assert sum(len(r.body) for r in retained_work) == 256*1024
        assert sum(len(r.body) for r in rows) == 331776
        assert max(len(r.key) for r in pages) == 96
        retained, retained_peak = tracemalloc.get_traced_memory()
        tx = Transaction(SimpleNamespace())
        store.write(tx, rows, Budget(logical_time()+2, logical_time))
        request = CommitRequest(session="projects/"+"p"*30+"/instances/"+"i"*64+"/databases/"+"d"*30+"/sessions/"+"s"*128,
            transaction_id=b"t"*128, mutations=tx._mutations,
            request_options={"priority": "PRIORITY_LOW", "transaction_tag": "tr_shadow_v2"})
        encoded = CommitRequest.serialize(request)
        import json as json_module
        from pathlib import Path

        from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND
        result_value = json_module.loads((Path(__file__).parent / "fixtures/async_settlement/shadow_v2_a1/close_result.json").read_text())["variants"][0]["object"]
        result_value.update(boot="b"*64, closure_sha256=closure.digest)
        result_raw = canonical(result_value)
        result_row = Row(CLOSE_RESULT_KIND, result_value["day"]+"/"+result_value["boot"], result_raw+b" "*(4096-len(result_raw)))
        result_row.value()
        result_tx = Transaction(SimpleNamespace())
        result_tx.insert(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                         values=[(result_row.kind, result_row.key, result_row.body.decode(), dt.datetime.now(dt.UTC))])
        result_request = CommitRequest(session=request.session, transaction_id=b"t"*128,
            mutations=result_tx._mutations, request_options=request.request_options)
        result_encoded = CommitRequest.serialize(result_request)
        # Both maximum bounded attachment lanes, including encoded/native copies.
        attachments = [memoryview(b"x"*65536).tobytes() for _ in range(8)]
        assert sum(map(len, attachments)) == 512*1024
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert retained >= 331776+256*1024
    assert peak >= retained + len(encoded) + len(result_encoded) + sum(map(len, attachments))
    measurements = dict(body_bytes=331776, close_result_body_bytes=4096,
        close_result_key_bytes=len(result_row.key), close_result_native_bytes=len(result_encoded),
        close_result_native_overhead_bytes=len(result_encoded)-4096, attachment_retained_body_bytes=sum(map(len, attachments)), key_bytes=sum(len(r.kind)+len(r.key) for r in rows),
        native_request_bytes=len(encoded), native_overhead_bytes=len(encoded)-331776,
        python_encoding_peak_bytes=peak, retained_body_bytes=331776+256*1024,
        retained_work_body_bytes=256*1024,
        retained_python_bytes=retained, construction_peak_bytes=retained_peak,
        process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    assert len(tx._mutations) == 1
    started = time.monotonic()
    store.flush(pages, counter, closure, Budget(logical_time()+4, logical_time))
    measurements["six_row_flush_seconds"] = time.monotonic()-started
    measurements["flush_select_calls"] = db.transaction_execute_sql_calls
    measurements["flush_commit_calls"] = db.commits
    (tmp_path / "measurement.json").write_text(json.dumps(measurements))
    print("P3_SERIALIZATION", json.dumps(measurements, sort_keys=True))
    assert store.reconcile(rows, Budget(logical_time()+4, logical_time))
    assert db.transaction_execute_sql_calls == 7  # fence + six complete-key reads
    assert db.commits == 1


def test_lifecycle_pins_and_no_money_calls(monkeypatch: Any) -> None:
    from unittest.mock import Mock

    from trusted_router.services import settle_outbox_worker
    svc = service()
    money = Mock(side_effect=AssertionError("money operation"))
    monkeypatch.setattr(settle_outbox_worker, "drain_pass", money)
    assert svc.enabled()
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    assert svc.contender()
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    assert money.call_count == 0
    svc.settings.async_settle_enabled = True
    assert not svc.enabled() and not svc.contender()
    svc.settings.async_settle_enabled = False
    svc.settings.async_settle_shadow_policy = "other"
    assert not svc.enabled()
    svc.settings.async_settle_shadow_policy = "shadow-v2"
    svc.settings._async_settle_shadow_workspace_ids = frozenset()
    assert not svc.enabled()


@pytest.mark.asyncio
async def test_shutdown_closes_idle_and_stops_owned_tasks() -> None:
    svc = service()
    await svc.close()
    assert not svc.journal.days
    assert not svc.unclosed
    assert svc.observer.stopped and svc.slots.stopped and svc.journal.stopped


@pytest.mark.asyncio
async def test_shutdown_retained_work_stays_unclosed() -> None:
    svc = service()
    svc.inflight = 1
    await svc.close()
    assert svc.unclosed and svc.journal.days
    assert svc.inflight == 1  # shadow must never cancel a money operation


def test_counter_equal_sequence_conflict_and_regression() -> None:
    svc = service()
    svc.flush(svc.store.clock()+4)
    svc.flush(svc.store.clock()+4)
    row = svc.counter_row("2026-10-12", int(svc.store.wall()*1e6), closed=False)
    found = svc.store.read(COUNTER_KIND, row.key, Budget(logical_time()+1, logical_time))
    assert found is not None
    different = found.value()
    different["diagnostics"]["fault"] = 1
    with pytest.raises(ValueError, match="checkpoint_conflict"):
        svc.store.flush([], Row.build(COUNTER_KIND, row.key, different), None, Budget(logical_time()+4, logical_time))
    different.update(sequence=1, checkpoint_seq=1)
    with pytest.raises(ValueError, match="checkpoint_regression"):
        svc.store.flush([], Row.build(COUNTER_KIND, row.key, different), None, Budget(logical_time()+4, logical_time))


def test_slot_boundary_ordinary_traced() -> None:
    slots = Slots()
    assert slots.tick(NOW_US+250000, busy=False) == (NOW_US//2000000, 0)
    assert slots.tick(NOW_US+2250001, busy=False) == (None, 1)
    assert slots.tick(NOW_US, busy=False) == (None, 0)
    assert slots.tick(NOW_US+6000000, busy=True) == (None, 2)
    slots.stopped = True
    assert slots.tick(NOW_US+8000000, busy=False) == (None, 0)


def test_empty_policy_live_modules_absent_or_present() -> None:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    import trusted_router
    roots = os.pathsep.join(str(Path(path).parent) for path in trusted_router.__path__)
    root = Path(__file__).parents[1]
    script = '''
import asyncio, importlib.abc, json, sys
from pathlib import Path
names = {"async_settle_shadow_journal", "async_settle_shadow_health_state", "async_settle_shadow_health", "storage_gcp_shadow_observation", "storage_gcp_shadow_io", "storage_gcp_shadow_attachments", "async_settle_shadow_attachments"}
names.update(p.stem for p in Path("src/trusted_router").glob("*shadow*v2*.py"))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.rsplit(".", 1)[-1] in names:
            raise AssertionError("dormant module imported")
if sys.argv[1] == "absent":
    sys.meta_path.insert(0, Block())
from trusted_router import storage_gcp_io as shared_adapter
shared_before = {k: id(v) for k,v in vars(shared_adapter).items()}
import trusted_router.main as main
assert {k: id(v) for k,v in vars(shared_adapter).items()} == shared_before
assert not any(n.rsplit(".", 1)[-1] in names for n in sys.modules)
def label(f): return f.__module__+"."+f.__qualname__
a=main.app
async def lifecycle():
    await a.router._startup()
    assert not any(n.rsplit(".", 1)[-1] in names for n in sys.modules)
    assert not any("shadow_health" in repr(t.get_coro()) for t in asyncio.all_tasks())
    assert {k: id(v) for k,v in vars(shared_adapter).items()} == shared_before
    await a.router._shutdown()
asyncio.run(lifecycle())
print(json.dumps({"routes": [(getattr(r,"path",None),sorted(getattr(r,"methods",None) or [])) for r in a.routes],"startup":[label(f) for f in a.router.on_startup],"shutdown":[label(f) for f in a.router.on_shutdown],"lifespan":label(type(a.router.lifespan_context))},sort_keys=True))
'''
    results = []
    for mode in ("present",):
        result = subprocess.run([sys.executable, "-c", script, mode], cwd=root,  # noqa: S603
            env={**os.environ, "PYTHONPATH": roots, "TR_ASYNC_SETTLE_SHADOW_POLICY": "",
                 "TR_STORAGE_BACKEND": "memory", "TR_ENVIRONMENT": "test"},
            capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout.splitlines()[-1]))
    assert results[0]["routes"]


def test_sentinel_and_aggregate_overflow_are_unknown() -> None:
    stamp = dt.datetime.fromtimestamp(NOW_US/1e6, dt.UTC)
    assert health(rows=[(stamp, 0, "pending")]*10000)["complete"]
    assert not health(rows=[(stamp, 0, "pending")]*10001)["complete"]
    assert not health(rows=[(stamp, (1<<63)-1, "pending")]*2)["complete"]


def test_hwm_does_not_jump_an_unfinished_later_page() -> None:
    j = journal()
    first = j.reserve(NOW_US)
    assert first is not None
    j.finalize(first, event_for(first))
    j.complete(first)
    rows = j.freeze(first.day)
    j.verify_complete(first, rows)
    later = j.reserve(NOW_US+1)
    assert later is not None
    j.acknowledge(rows)
    assert j.days[first.day].hwm == 1
    assert j.days[first.day].next_seq == 3


def test_sticky_loss_survives_later_success() -> None:
    j = journal()
    first = j.reserve(NOW_US)
    assert first is not None
    j.loss(first.day)
    j.finalize(first, event_for(first))
    j.complete(first)
    rows = j.freeze(first.day)
    j.acknowledge(rows)
    j.verify_complete(first, rows)
    assert j.losses == j.days[first.day].losses == 1


@pytest.mark.parametrize("fault", ["abort", "reset", "deadline"])
def test_native_first_rpc_fault_never_commits(sdk_database: Any, fault: str) -> None:  # noqa: F811
    from google.api_core.exceptions import Aborted, DeadlineExceeded, InternalServerError
    db, clock, sleeps = sdk_database
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    if fault == "deadline":
        db.read_duration = 1.
    else:
        db.read_errors = [{"abort": Aborted("abort"), "reset": InternalServerError("RST_STREAM")}[fault]]
    with pytest.raises((RuntimeError, DeadlineExceeded)):
        store.claim("issuer", NOW_US//2_000_000, NOW_US, Budget(clock.monotonic()+.5, clock.monotonic))
    assert not sleeps
    assert not any(name == "commit" for name, _ in db.calls)


def test_same_owner_later_slot_fences_unpublished_winner() -> None:
    store, _ = fake_store()
    slot = NOW_US//2_000_000
    assert store.claim("publisher", slot, NOW_US, Budget(logical_time()+.5, logical_time))
    store.database.server_ns += 2_000_000_000
    assert store.claim("publisher", slot+1, NOW_US+2_000_000, Budget(logical_time()+.5, logical_time))
    assert not store.publish(health(), Budget(logical_time()+.5, logical_time))
    assert store.read_shadow_health() is None


@pytest.mark.parametrize("field", ["publisher", "diagnostics", "admission_observer", "loss_count", "reset_count"])
def test_all_cumulative_count_families_cannot_regress(field: str) -> None:
    from trusted_router.storage_gcp_shadow_observation import monotonic_counter
    previous = copy.deepcopy(fixture("counter")[0]["object"])
    previous["closed"] = False
    if field == "publisher":
        previous[field]["claims"] = 1
    elif field == "diagnostics":
        previous[field]["fault"] = 1
    elif field == "admission_observer":
        previous[field]["health_reads"] = 1
    else:
        previous[field] = 1
    value = copy.deepcopy(previous)
    if field == "publisher":
        value[field]["claims"] = 0
    elif field == "diagnostics":
        value[field] = {}
    elif field == "admission_observer":
        value[field]["health_reads"] = 0
    else:
        value[field] = 0
    with pytest.raises(ValueError, match="counter_"):
        monotonic_counter(previous, value)


def test_late_health_commit_is_failure_but_reader_uses_observation_age(monkeypatch: Any) -> None:
    from trusted_router.async_settle_shadow_v2_a1_health import health_usable

    svc = service()
    from types import SimpleNamespace

    from trusted_router import storage_gcp_io
    clock = [0.]
    monkeypatch.setattr(storage_gcp_io, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    svc.store.clock = lambda: clock[0]
    svc.store.wall = lambda: NOW_US/1e6 + clock[0]
    landed: list[dict[str, Any]] = []
    monkeypatch.setattr(svc.store, "claim", lambda *args: True)
    monkeypatch.setattr(svc.store, "observe", lambda *args: health())
    def publish(value: dict[str, Any], budget: Budget) -> bool:
        clock[0] = .501
        landed.append(value)
        row = Row.build(HEALTH_KIND, "local/fleet", value)
        with svc.store.database.batch() as batch:
            svc.store.write(batch, [row], Budget(clock[0]+1, svc.store.clock))
        raise UnknownCommit("lost reply; commit lands late")
    monkeypatch.setattr(svc.store, "publish", publish)
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    counts = svc.counters["2026-10-12"]["publisher"]
    assert (counts["claims"], counts["publications"], counts["failures"]) == (1, 0, 1)
    observer = Observer(frozenset({"ws"}), lambda _: Admission(0, 2), lambda: None,
        policy="shadow-v2", clock=lambda: 10., wall=svc.store.wall)
    observer.install_workspace("ws", 10., Admission(0, 2))
    observer.install_health(10., svc.store.read_shadow_health())
    for elapsed in (.501, 4.75, 4.999999, 5., 5.000001):
        clock[0] = elapsed
        metadata = dict(committed_at_ns=NOW_US*1000+501_000_000, read_at_ns=NOW_US*1000+round(elapsed*1e9))
        observer.install_health(10., dict(installed(landed[0]), **metadata))
        usable = health_usable(landed[0], metadata, 0, 0)
        assert (observer.peek("ws")["prediction"] == "yes") == usable


def test_success_reply_over_budget_is_not_publication(monkeypatch: Any) -> None:
    svc = service()
    clock = [0.]
    svc.store.clock = lambda: clock[0]
    svc.store.wall = lambda: NOW_US/1e6
    monkeypatch.setattr(svc.store, "claim", lambda *args: True)
    monkeypatch.setattr(svc.store, "observe", lambda *args: health())
    def publish(*args: Any) -> bool:
        clock[0] = .500001
        return True
    monkeypatch.setattr(svc.store, "publish", publish)
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    counts = svc.counters["2026-10-12"]["publisher"]
    assert counts["publications"] == 0 and counts["failures"] == 1


def test_every_prediction_reader_uses_health_usable(monkeypatch: Any) -> None:
    from trusted_router import async_settle_shadow_health_state as state

    calls = []
    def reject(body: Any, metadata: Any, q: int, e: int) -> bool:
        calls.append((body, metadata, q, e))
        return False
    monkeypatch.setattr(state, "health_usable", reject)
    observer = Observer(frozenset({"ws"}), lambda _: Admission(0, 2), lambda: health(),
        policy="shadow-v2", clock=lambda: 10., wall=lambda: NOW_US/1e6)
    observer.install_workspace("ws", 10., Admission(0, 2))
    observer.install_health(10., installed(health()))
    assert observer.peek("ws")["prediction"] == "unknown"
    assert state.prediction(observer, "ws")["prediction"] == "unknown"
    assert len(calls) == 2 and all(metadata["read_at_ns"] == NOW_US*1000 for _, metadata, _, _ in calls)


@exhaustive
def test_publisher_slot_product(monkeypatch: Any) -> None:
    # Every scheduler state/event product, including backwards and repeated time.
    for last, stopped, busy, delta, offset in itertools.product(
            (None, 10), (False, True), (False, True), (-1, 0, 1, 3), (0, 250000, 250001, 1999999)):
        slot = 10 + delta
        slots = Slots(last_slot=last, stopped=stopped)
        actual = slots.tick(slot*2_000_000+offset, busy=busy)
        if stopped or last is not None and slot <= last:
            expected = (None, 0)
        else:
            missed = max(0, slot-last-1) if last is not None else 0
            expected = (None, missed+1) if busy or offset > 250000 else (slot, missed)
        assert actual == expected
    for claim, scan, publish in itertools.product((False, True, "error"), (True, "error"), (False, True, "error", "late")):
        svc = service()
        clock = [0.]
        svc.store.clock = lambda clock=clock: clock[0]
        svc.store.wall = lambda: NOW_US/1e6
        def outcome(value: Any, clock: Any = clock) -> Any:
            if value == "error":
                raise RuntimeError("fault")
            if value == "late":
                clock[0] = .501
                return True
            return value
        monkeypatch.setattr(svc.store, "claim", lambda *a, claim=claim: outcome(claim))
        monkeypatch.setattr(svc.store, "observe", lambda *a, scan=scan: health() if outcome(scan) else None)
        monkeypatch.setattr(svc.store, "publish", lambda *a, publish=publish: outcome(publish))
        svc.publish_cycle(NOW_US//2_000_000, NOW_US)
        p = svc.counters["2026-10-12"]["publisher"]
        assert p["claims"] <= p["claim_attempts"] == 1
        from trusted_router.async_settle_shadow_v2_a1_health import validate_publisher_final
        validate_publisher_final(p)
        assert p["publications"] + p["failures"] + p["lost_claims"] == 1
        assert p["scans"] == p["claims"]
        assert p["publications"] == int(claim is True and scan is True and publish is True)
        assert sum(p["cycle_us"]["counts"]) == 1


def test_rpc_histogram_native_success_failure_and_stream(sdk_database: Any) -> None:  # noqa: F811
    from trusted_router.storage_gcp_shadow_io import observe_spanner_rpcs
    db, clock, _ = sdk_database
    svc = service()
    store = ObservationStore(db, clock=clock.monotonic)
    day = "2026-10-12"
    db.server_ns = 2_000_000_000
    with observe_spanner_rpcs(lambda elapsed: svc.histogram(day, "rpc_us", elapsed)):
        assert store.claim("issuer", 1, 2_000_000, Budget(clock.monotonic()+.5, clock.monotonic))
    h = svc.counters[day]["publisher"]["rpc_us"]
    assert sum(h["counts"]) == len(db.calls) == 2
    assert h["counts"][5] == 1 and h["counts"][8] == 1  # 10ms read, 450ms commit
    db.server_ns = 4_000_000_000
    db.errors = [RuntimeError("lost reply")]
    with observe_spanner_rpcs(lambda elapsed: svc.histogram(day, "rpc_us", elapsed)):
        with pytest.raises(UnknownCommit):
            store.claim("issuer", 2, 4_000_000, Budget(clock.monotonic()+.5, clock.monotonic))
    assert sum(h["counts"]) == len(db.calls) == 4


@exhaustive
def test_histogram_exact_boundaries() -> None:
    from trusted_router.async_settle_shadow_evidence import HIST_BOUNDS
    svc = service()
    for name, boundary, delta in itertools.product(("rpc_us", "cycle_us"), HIST_BOUNDS, (-1, 0, 1)):
        counts = svc.counters["2026-10-12"]["publisher"][name]["counts"]
        before = list(counts)
        svc.histogram("2026-10-12", name, boundary+delta)
        expected = [int(i == sum(boundary+delta > b for b in HIST_BOUNDS)) for i in range(9)]
        assert [a-b for a, b in zip(counts, before, strict=True)] == expected


@exhaustive
def test_allocator_product() -> None:
    from trusted_router.async_settle_shadow_v2_schema import INT64
    for stopped, closing, permits, seq, pages in itertools.product(
            (False, True), (False, True), (0, 1), (1, INT64, INT64+1), range(5)):
        j = journal()
        day = j.days["2026-10-12"]
        for _ in range(pages):
            r = j.reserve(NOW_US)
            assert r is not None
            j.freeze(r.day)
        day.next_seq = seq
        day.closing = closing
        j.stopped = stopped
        j.permits["2026-10-12"] = permits
        success = not stopped and not closing and permits > 0 and seq <= INT64 and pages < 4
        r = j.reserve(NOW_US)
        assert (r is not None) == success
        assert day.next_seq == seq + int(success)
        assert j.losses == int(not success)


@exhaustive
def test_nonempty_page_transition_product() -> None:
    for state, operation, exact, finished in itertools.product(PageState,
            ("seal", "freeze", "unknown", "acknowledge"), (False, True), (False, True)):
        j = journal()
        r = j.reserve(NOW_US)
        assert r is not None
        j.finalize(r, event_for(r))
        j.freeze(r.day)
        page = j.pages[0]
        assert page.row is not None
        page.state = state
        if not finished:
            page.slots[0].body = None
        valid = (operation == "seal" and state == PageState.OPEN or
                 operation == "freeze" and state == PageState.SEALED or
                 operation == "unknown" and state == PageState.FROZEN or
                 operation == "acknowledge" and state in (PageState.FROZEN, PageState.UNKNOWN, PageState.DURABLE) and exact)
        args = [None] if operation == "freeze" else [page.row.body if exact else b"wrong"] if operation == "acknowledge" else []
        if valid:
            result = getattr(page, operation)(*args)
            if operation == "freeze":
                assert result == finished
        else:
            with pytest.raises(ValueError):
                getattr(page, operation)(*args)


def test_complete_requires_exact_durable_reference() -> None:
    j = journal()
    r = j.reserve(NOW_US)
    assert r is not None
    j.finalize(r, event_for(r))
    rows = j.freeze(r.day)
    j.acknowledge(rows)
    j.complete(r)
    assert j.days[r.day].hwm == 0
    with pytest.raises(ValueError, match="reference_missing"):
        j.verify_complete(r, [])
    assert j.days[r.day].hwm == 0
    value = rows[0].value()
    value["events"][0]["diagnostic"] = dict(reason="changed", required_bytes=0, candidate_count=0)
    with pytest.raises(ValueError, match="reference_conflict"):
        j.verify_complete(r, [Row.build(rows[0].kind, rows[0].key, value)])
    j.verify_complete(r, rows)
    assert j.days[r.day].hwm == 1


def test_request_facade_threads_do_not_wait_for_flush(monkeypatch: Any) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    svc = service()
    day = svc.ensure_day(NOW_US)
    svc.journal.grant(day, RESERVATION_BLOCK)
    entered, release = threading.Event(), threading.Event()
    real_flush = svc.store.flush
    def slow(*args: Any) -> None:
        entered.set()
        release.wait()
        real_flush(*args)
    monkeypatch.setattr(svc.store, "flush", slow)
    with ThreadPoolExecutor(max_workers=9) as pool:
        flushing = pool.submit(svc.flush, svc.store.clock()+4)
        entered.wait()
        try:
            reservations = list(pool.map(svc.port.reserve, [NOW_US+i for i in range(8)]))
            assert not release.is_set() and not flushing.done()
            # Lock contention may reject immediately with sticky loss; storage latency
            # must not determine request completion. Successful reservations are unique.
            accepted = [r for r in reservations if r is not None]
            assert len(set(accepted)) == len(accepted)
            assert accepted
            for r in accepted:
                assert svc.port.finalize(r, event_for(r))
                assert svc.port.complete(r)
        finally:
            release.set()
        flushing.result()


@pytest.mark.asyncio
async def test_shutdown_full_pages_requires_second_pass() -> None:
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    for _ in range(4):
        for i in range(2):
            r = svc.port.reserve(NOW_US+i)
            assert r is not None
            assert svc.port.finalize(r, event_for(r))
            assert svc.port.complete(r)
        svc.journal.freeze("2026-10-12")
    assert len(svc.journal.pages) == 4
    await svc.close()
    assert not svc.unclosed and not svc.journal.days
    assert svc.store.database.commits == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["before_first_flush", "during_commit", "before_closure", "after_closure", "full_queue"])
async def test_shutdown_fault_points(monkeypatch: Any, point: str) -> None:
    svc = service()
    if point == "full_queue":
        svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
        for _ in range(4):
            assert svc.port.reserve(NOW_US)
            svc.journal.freeze("2026-10-12")
        assert svc.port.reserve(NOW_US) is None
    real = svc.store.flush
    def fault(pages: Any, counter: Any, closure: Any, budget: Any) -> None:
        if point in ("before_first_flush", "during_commit", "before_closure"):
            raise UnknownCommit("kill during commit") if point == "during_commit" else RuntimeError("kill")
        real(pages, counter, closure, budget)
        if point == "after_closure":
            raise UnknownCommit("reply lost")
    monkeypatch.setattr(svc.store, "flush", fault)
    await svc.close()
    assert svc.unclosed  # A1 permanently vetoes a lost original final reply.
    if point == "after_closure":
        assert svc.close_failures["2026-10-12"] == 1


@pytest.mark.asyncio
async def test_unknown_final_close_miss_then_exact_late_landing(monkeypatch: Any) -> None:
    svc = service()
    retained = []
    real = svc.store.flush
    def lost(*args: Any) -> None:
        retained.append(args[:3])
        raise UnknownCommit("late close")
    monkeypatch.setattr(svc.store, "flush", lost)
    await svc.close()
    assert svc.unclosed and svc.journal.days and svc.unknown is not None
    pages, counter, closure = retained[0]
    real(pages, counter, closure, Budget(logical_time()+4, logical_time))
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days and svc.unknown is None and svc.last_closure_ack is None
    assert svc.close_failures["2026-10-12"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["", "other"])
async def test_policy_disabled_optout_closes(policy: str) -> None:
    svc = service()
    svc.settings.async_settle_shadow_policy = policy
    await svc.run()
    assert not svc.journal.days and not svc.unclosed


def test_empty_closure_digest_one_boundary() -> None:
    from trusted_router.async_settle_shadow_journal import CLOSURE_KIND, EMPTY_CHAIN
    svc = service()
    day = "2026-10-12"
    svc.journal.checkpoint(day)
    svc.folded_us = NOW_US
    counter = svc.counter_row(day, NOW_US+100_000_000, closed=True)
    closure = svc.closure_row(day, counter, NOW_US, "shutdown")
    assert counter.value()["receipt_chain_sha256"] is None
    assert closure.value()["receipt_chain_sha256"] == EMPTY_CHAIN
    value = closure.value()
    value["receipt_chain_sha256"] = "0"*64
    with pytest.raises(ValueError):
        Row.build(CLOSURE_KIND, closure.key, value)
    svc.store.flush([], counter, closure, Budget(logical_time()+4, logical_time))


def test_cross_midnight_retained_work_and_completion() -> None:
    from trusted_router.async_settle_shadow_journal import CLOSURE_KIND
    svc = service()
    day = "2026-10-12"
    svc.journal.grant(day, RESERVATION_BLOCK)
    r = svc.port.reserve(NOW_US+86_399_000_000)
    assert r is not None
    original = event_for(r)
    assert svc.port.finalize(r, original)
    svc.inflight = 1
    svc.store.wall = lambda: NOW_US/1e6+86401
    svc.observer.wall = svc.store.wall
    svc.observer.evidence_wall = svc.store.wall
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    svc.flush(svc.store.clock()+4)
    assert day in svc.journal.days and "2026-10-13" in svc.journal.days
    page = svc.journal.pages[0].row
    assert page is not None
    assert svc.port.complete(r)
    svc.inflight = 0
    svc.flush(svc.store.clock()+4)
    svc.flush(svc.store.clock()+4)
    assert day not in svc.journal.days
    previous = svc.store.read(CLOSURE_KIND, day+"/"+svc.journal.boot, Budget(logical_time()+1, logical_time))
    assert previous is not None
    assert svc.last_closure_ack is not None
    assert svc.last_closure_ack["closure_sha256"] == previous.digest
    assert svc.last_closure_ack["physical_commit_ack_at_us"] > previous.value()["interval_end_us"]
    svc.journal.grant("2026-10-13", RESERVATION_BLOCK)
    c = svc.port.reserve(NOW_US+86_401_000_000)
    assert c is not None and c.event_seq == r.event_seq == 1
    completion = dict(original, phase="completion", seq=f"{c.event_seq:020}",
        event_at_us=c.observed_us, receipt_at_us=c.observed_us, receipt_locator=None,
        original_receipt=None, certificate_sha256=None, payload_sha256=None,
        proof_kind="none", comparison=None, prediction=None,
        booking=dict(outcome="not_attempted", reference=None, winner=None),
        retry_original=dict(day=r.day, boot=r.boot, event_seq=f"{r.event_seq:020}"), completion_of=f"{r.event_seq:020}")
    # A1 removes authorize-original completion: closure commits the obligation.
    assert svc.port.complete_event(c, page, completion) is None
    assert svc.journal.days[c.day].hwm == 0
    assert previous.value()["authorization_obligations"] == "receipt_chain"


def retained_case(kind: str, *, pending: bool = False) -> tuple[Service, Any, Row]:
    from trusted_router.async_settle_shadow_journal import CERTIFICATE_KIND, SAMPLE_KIND
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    r = svc.port.reserve(NOW_US+100_000_000)
    assert r is not None
    event = copy.deepcopy(next(v["object"] for v in fixture("receipt_pages") if v["schema"] == "TERMINAL_EVENT" and v["expected"] == "accept"))
    event.update(seq=f"{r.event_seq:020}", event_at_us=r.observed_us, receipt_at_us=r.observed_us)
    if kind == "sample":
        sample = copy.deepcopy(fixture("sample")[0]["object"])
        sample["original_terminal_receipt"] = dict(day=r.day, boot=r.boot, event_seq=f"{r.event_seq:020}")
        event.update(partition="A", eligibility="eligible", proof_kind="compared", comparison="exact",
            payload_sha256=sample["payload_hash"], original_receipt=None, certificate_sha256=None,
            prediction=copy.deepcopy(fixture("sample")[1]["object"]))
        row = Row.build(SAMPLE_KIND, sample["authorization_day"]+"/"+sample["authorization_id"], sample)
    else:
        cert = copy.deepcopy(fixture("certificate_record")[0]["object"])
        row = Row.build(CERTIFICATE_KIND, cert["authorization_day"]+"/"+cert["claims"]["authorization_id"], cert)
        # Independent issuer's exact immutable page already in durable storage.
        value = copy.deepcopy(fixture("receipt_pages")[0]["object"])
        from trusted_router.async_settle_shadow_journal import PAGE_KIND
        page = Row.build(PAGE_KIND, "/".join(value[k] for k in ("day", "boot", "first_seq")), value)
        svc.store.transaction(lambda tx: svc.store.write(tx, [page], Budget(logical_time()+2, logical_time)), Budget(logical_time()+2, logical_time), 2)
    if pending:
        event.update(partition="C", comparison=None,
                     booking=dict(outcome="pending", reference=None, winner="settle"))
    assert svc.port.finalize(r, event)
    assert svc.port.complete(r, () if pending else (row,))
    return svc, r, row


@pytest.mark.parametrize("kind", ["sample", "certificate"])
@pytest.mark.parametrize("fault", ["before", "lost_reply", "late"])
def test_retained_batch_exact_recovery(monkeypatch: Any, kind: str, fault: str) -> None:
    svc, r, row = retained_case(kind)
    real = svc.store.retain
    attempts = []
    def fail(rows: Any, budget: Any) -> None:
        attempts.append(rows)
        if fault == "lost_reply":
            real(rows, budget)
        raise RuntimeError("before commit") if fault == "before" else UnknownCommit("reply lost")
    monkeypatch.setattr(svc.store, "retain", fail)
    svc.flush(svc.store.clock()+4)
    assert svc.journal.batches[r] == (row,)
    assert svc.journal.days[r.day].hwm == 0
    monkeypatch.setattr(svc.store, "retain", real)
    if fault == "late":
        svc.flush(svc.store.clock()+4)
        assert r in svc.journal.batches
        real((row,), Budget(logical_time()+4, logical_time))
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[r.day].hwm == 1
    assert not svc.journal.batches and not svc.unknown_batches
    assert svc.store.read(row.kind, row.key, Budget(logical_time()+1, logical_time)) == row
    assert len(attempts) == 1
    assert svc.journal.days[r.day].failures >= 1


def test_retained_batch_conflict_does_not_advance(monkeypatch: Any) -> None:
    svc, r, row = retained_case("sample")
    value = row.value()
    value["timing"]["comparator_us"] += 1
    conflict = Row.build(row.kind, row.key, value)
    svc.store.retain((conflict,), Budget(logical_time()+2, logical_time))
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[r.day].hwm == 0 and r in svc.journal.batches
    assert svc.store.read(row.kind, row.key, Budget(logical_time()+1, logical_time)) == conflict


def test_reference_omission_and_wrong_writer_do_not_reconcile() -> None:
    svc, r, row = retained_case("sample")
    pages = svc.journal.freeze(r.day)
    svc.journal.acknowledge(pages)
    with pytest.raises(ValueError, match="reference_conflict"):
        svc.journal.verify_complete(r, pages)
    value = row.value()
    value["original_terminal_receipt"]["boot"] = "foreign"
    other = Row.build(row.kind, row.key, value)
    with pytest.raises(ValueError):
        svc.journal.verify_complete(r, [*pages, other])
    assert svc.journal.days[r.day].hwm == 0


def test_unreconciled_decrease_needs_exact_readback() -> None:
    from trusted_router.storage_gcp_shadow_observation import monotonic_counter
    svc = service()
    day = "2026-10-12"
    svc.journal.checkpoint(day)
    svc.journal.failed(day, unknown=True)
    before = svc.counter_row(day, NOW_US+100_000_000, closed=False).value()
    after = copy.deepcopy(before)
    after["unreconciled_commits"] = 0
    with pytest.raises(ValueError, match="unproven_reconciliation"):
        monotonic_counter(before, after)
    monotonic_counter(before, after, reconciled=1)
    after["boot"] = "foreign"
    with pytest.raises(ValueError, match="writer_conflict"):
        monotonic_counter(before, after, reconciled=1)


def test_port_counter_demand_and_terminal_permits() -> None:
    from trusted_router.async_settle_shadow_v2_evidence import COUNTS
    svc = service()
    svc.maintain()
    day = "2026-10-12"
    assert svc.journal.terminal_permits[day] == RESERVATION_BLOCK
    r = svc.port.reserve(NOW_US+100_000_000, terminal=True)
    assert r is not None and svc.journal.terminal_permits[day] == RESERVATION_BLOCK-1
    bucket = dict(phase="settle", dimensions=dict(adapter="openai", route_type="chat.completions", streamed=False),
        **dict.fromkeys(COUNTS, 0), eligible_demand=1)
    bucket.update(settle_attempts=1, C=1, requested_eligible=1)
    assert svc.port.fold_counts(day, bucket)
    assert svc.counters[day]["eligible_demand"] == svc.counters[day]["settle_attempts"] == 1
    svc.journal.checkpoint(day)
    svc.counter_row(day, NOW_US+100_000_000, closed=False)
    bucket["eligible_demand"] = None
    assert svc.port.fold_counts(day, bucket)
    assert svc.counters[day]["eligible_demand"] is None
    bucket["settle_attempts"] = -1
    assert not svc.port.fold_counts(day, bucket)
    assert svc.port.lost.is_set()


def test_all_four_money_primitives_are_unreachable(monkeypatch: Any) -> None:
    from unittest.mock import Mock

    from trusted_router import storage_gcp_authorize
    from trusted_router.services import settle_outbox_apply
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    calls = []
    for owner, name in ((SpannerSettleOutbox, "claim_shard"), (settle_outbox_apply, "apply_frozen_settle"),
                        (storage_gcp_authorize, "typed_finalize_atomic"), (storage_gcp_authorize, "reap_expired_reservations_result")):
        spy = Mock(side_effect=AssertionError("money primitive"))
        monkeypatch.setattr(owner, name, spy)
        calls.append(spy)
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6+100
    svc.store.database.server_ns = (NOW_US+100_000_000)*1000
    svc.publish_cycle((NOW_US+100_000_000)//2_000_000, NOW_US+100_000_000)
    svc.maintain()
    assert svc.counters["2026-10-12"]["publisher"]["publications"] == 1
    assert all(spy.call_count == 0 for spy in calls)


def test_failed_pages_and_resets_are_sticky() -> None:
    j = journal()
    r = j.reserve(NOW_US)
    assert r is not None
    j.finalize(r, event_for(r))
    rows = j.freeze(r.day)
    j.checkpoint(r.day)
    j.failed(r.day, unknown=False)
    assert j.freeze(r.day) == rows
    j.days[r.day].resets = 1
    j.acknowledge(rows)
    j.complete(r)
    j.verify_complete(r, rows)
    j.days[r.day].closing = True
    assert not j.can_close(r.day, inflight=0, pending=0, interval_end_us=NOW_US, folded_us=NOW_US)
    assert j.days[r.day].failures == j.days[r.day].resets == 1


def test_v1_drop_is_not_v2_recovery() -> None:
    from trusted_router.detached_jws import canonical
    svc = service()
    value = copy.deepcopy(svc.legacy._day())
    value.update(sequence=1, flushed_at_us=NOW_US+100_000_000)
    value["drops"] = [dict(phase="worker", adapter=None, route_type=None, streamed=None,
                           reason="store_unavailable", count=1)]
    row = Row(COUNTER_KIND, "2026-10-12/"+svc.journal.boot, canonical(value))
    with pytest.raises(ValueError):
        row.value()


@exhaustive
def test_transaction_total_includes_counter_and_closure() -> None:
    pages, counter, closure = maximum_rows()
    store, _ = fake_store()
    store.flush(pages, counter, closure, Budget(logical_time()+4, logical_time))
    assert sum(len(row.body) for row in [*pages, counter, closure]) == 331776
    assert sum(len(row.body) for row in [*pages, counter]) == 327680


def test_health_names_never_touch_fleet_v1() -> None:
    from trusted_router.storage_gcp_shadow_observation import (
        read_shadow_health_statement,
        shadow_health_claim_read,
        shadow_health_previous,
        shadow_health_publish_fence,
    )
    for builder, kind, key in ((read_shadow_health_statement, HEALTH_KIND, "local/fleet"),
                              (shadow_health_previous, HEALTH_KIND, "local/fleet"),
                              (shadow_health_claim_read, CLAIM_KIND, "local/publish"),
                              (shadow_health_publish_fence, CLAIM_KIND, "local/publish")):
        _, params, _ = builder()
        assert params == dict(kind=kind, id=key)
    test_claim_fence_and_delayed_winner_fake()


@exhaustive
def test_retained_task_and_byte_boundaries() -> None:
    from trusted_router.async_settle_shadow_journal import SAMPLE_KIND
    from trusted_router.detached_jws import canonical
    for size, count in ((8192, 32), (8191, 32)):
        j = journal()
        for i in range(count+1):
            r = j.reserve(NOW_US+i)
            assert r is not None
            j.finalize(r, event_for(r))
            value = copy.deepcopy(fixture("sample")[0]["object"])
            raw = canonical(value)
            row = Row(SAMPLE_KIND, value["authorization_day"]+"/"+value["authorization_id"], raw+b" "*(size-len(raw)))
            accepted = j.complete(r, (row,))
            assert accepted == (i < 32)
        assert j.work_bytes == size*32 <= 256*1024
        assert j.losses == 1 and len(j.batches) == 32


def test_unknown_counter_readback_is_idempotent_and_bounded() -> None:
    svc = service()
    svc.flush(svc.store.clock()+4)
    row = svc.counter_row("2026-10-12", int(svc.store.wall()*1e6), closed=False)
    for _ in range(10):
        assert svc.store.reconcile([row], Budget(logical_time()+1, logical_time))
    assert len(svc.store.reconciliations[row.key]) == 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_owned_reservation_with_flush_grace(monkeypatch) -> None:
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    r = svc.port.reserve(NOW_US+100_000_000)
    assert r is not None
    from trusted_router.services import async_settle_shadow_health as module
    waited = []
    async def finish(seconds):
        waited.append(seconds)
        assert svc.journal.stopped
        assert svc.port.finalize(r, event_for(r))
        assert svc.port.complete(r)
        logical_time.advance(seconds)
    monkeypatch.setattr(module.asyncio, "sleep", finish)
    await svc.close()
    assert waited == [.01]
    assert not svc.unclosed and not svc.journal.days


@pytest.mark.parametrize("error", ["reset", "abort"])
def test_native_stream_iteration_never_restarts(sdk_database: Any, error: str) -> None:  # noqa: F811
    from google.api_core.exceptions import Aborted, InternalServerError

    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines
    from trusted_router.storage_gcp_shadow_io import observe_spanner_rpcs
    db, clock, sleeps = sdk_database
    db._trusted_router_rpc_deadlines = False
    original = db.query
    started = []
    def broken(**kwargs: Any) -> Any:
        started.append(1)
        yield from original(**kwargs)
        raise Aborted("abort") if error == "abort" else InternalServerError("RST_STREAM")
    db.spanner_api.execute_streaming_sql = broken
    configure_spanner_rpc_deadlines(db)
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    measured = []
    with observe_spanner_rpcs(measured.append):
        with pytest.raises(RuntimeError):
            store.claim("issuer", NOW_US//2_000_000, NOW_US, Budget(clock.monotonic()+.5, clock.monotonic))
    assert len(started) == 1 and not sleeps
    assert not any(name == "commit" for name, _ in db.calls)
    assert measured and len(measured) >= len(started)


def test_manifest_never_truncates_supplied_inventory() -> None:
    from trusted_router.async_settle_shadow_journal import CONTROL_KIND
    value = copy.deepcopy(fixture("controls")[0]["object"])
    writer = value["router_writers"]["issuer"]
    value["router_writers"] = {f"boot-{i}": copy.deepcopy(writer) for i in range(4096)}
    with pytest.raises(ValueError):
        Row.build(CONTROL_KIND, value["day"]+"/manifest-v2", value)
    assert len(value["router_writers"]) == 4096


@exhaustive
def test_closure_condition_product() -> None:
    # Each independent guard has both truth values in the full Cartesian product.
    for closing, closed, inflight, pending, folded, unknown, loss, reset, hole, oldest in itertools.product((False, True), repeat=10):
        j = journal()
        day = j.days["2026-10-12"]
        day.closing, day.closed = closing, closed
        day.unreconciled, day.losses, day.resets = int(unknown), int(loss), int(reset)
        day.next_seq = 2 if hole else 1
        if not oldest:
            j.open_day("2026-10-11")
        actual = j.can_close("2026-10-12", inflight=int(inflight), pending=int(pending),
                            interval_end_us=NOW_US, folded_us=NOW_US if folded else NOW_US-1)
        expected = closing and not any((closed, inflight, pending, unknown, loss, reset, hole)) and folded and oldest
        assert actual == expected


@exhaustive
def test_cap_allocation_product() -> None:
    for terminal, event, terminals, events in itertools.product((False, True), (False, True),
            (0, 99900, 100000), (0, 399900, 400000)):
        value = dict(v=2, policy_version="shadow-v2", contract_revision="a1", day="2026-10-12",
                     terminal_reserved=terminals, event_reserved=events)
        legal = (not terminal or terminals <= 99900) and (not event or events <= 399900)
        if legal:
            result = reserve_cap(value, terminal=terminal, event=event)
            assert result["terminal_reserved"] == terminals+RESERVATION_BLOCK*terminal
            assert result["event_reserved"] == events+RESERVATION_BLOCK*event
        else:
            with pytest.raises(ValueError, match="daily_cap"):
                reserve_cap(value, terminal=terminal, event=event)
        assert value["terminal_reserved"] == terminals and value["event_reserved"] == events


@exhaustive
@pytest.mark.asyncio
async def test_maximum_queue_shutdown_measurements(tmp_path: Any, monkeypatch: Any) -> None:
    import json
    import resource

    from trusted_router.async_settle_shadow_journal import Reservation, Slot
    from trusted_router.async_settle_shadow_v2_evidence import EVENT
    svc = service()
    pages, _, _ = maximum_rows()
    day = pages[0].value()["day"]
    svc.journal = Journal(pages[0].value()["boot"])
    svc.counters.clear()
    svc.legacy.days.clear()
    stamp = pages[0].value()["events"][0]["event_at_us"]
    svc.store.wall = lambda: (stamp+100_000_000)/1e6
    svc.observer.wall = svc.observer.evidence_wall = svc.store.wall
    svc.ensure_day(stamp)
    for row in pages:
        value = row.value()
        page = ReceiptPage(day, svc.journal.boot, int(value["first_seq"]), state=PageState.FROZEN, row=row)
        for event in value["events"]:
            r = Reservation(day, svc.journal.boot, page.first_seq, int(event["seq"]), event["event_at_us"])
            page.slots.append(Slot(r, EVENT.serialize(event)))
        svc.journal.pages.append(page)
    svc.journal.days[day].next_seq = 125
    for page in svc.journal.pages:
        for slot in page.slots:
            svc.journal.complete(slot.reservation)
    counter_row, closure_row = svc.counter_row, svc.closure_row
    def padded(row: Row, size: int) -> Row:
        assert len(row.body) <= size
        return Row(row.kind, row.key, row.body+b" "*(size-len(row.body)))
    monkeypatch.setattr(svc, "counter_row", lambda *a, **kw: padded(counter_row(*a, **kw), 65536))
    monkeypatch.setattr(svc, "closure_row", lambda *a, **kw: padded(closure_row(*a, **kw), 4096))
    from tests.test_async_settle_shadow_v2_evidence import no_line_tracing
    flush = svc.flush
    def measured_flush(deadline: float) -> None:
        with no_line_tracing():
            flush(deadline)
    monkeypatch.setattr(svc, "flush", measured_flush)
    started = time.monotonic()
    await svc.close()
    elapsed = time.monotonic()-started
    assert not svc.unclosed and not svc.journal.days, (svc.journal.days, len(svc.journal.batches), svc.unknown, svc.journal.losses)
    assert svc.store.database.commits == 3
    measurement = dict(shutdown_seconds=elapsed, page_bytes=4*65536, counter_bytes=65536, closure_bytes=4096,
        transaction_body_bytes=[327680, 69632], commits=svc.store.database.commits,
        total_selects=svc.store.database.transaction_execute_sql_calls+svc.store.database.snapshot_execute_sql_calls,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    (tmp_path/"shutdown-measurement.json").write_text(json.dumps(measurement))
    print("P3_SHUTDOWN", json.dumps(measurement, sort_keys=True))


@exhaustive
def test_process_kill_boundaries(tmp_path: Any) -> None:
    import json
    import os
    import subprocess
    import sys
    script = r'''
import asyncio, json, os, pathlib, signal
from tests.test_async_settle_shadow_observation import service, NOW_US, RESERVATION_BLOCK, install_scheduler
from pytest import MonkeyPatch
install_scheduler(MonkeyPatch())
root = pathlib.Path(os.environ["P3_KILL_OUTPUT"])
points = ("before_first_flush", "during_commit", "before_closure", "after_closure", "full_queue")
for point in points:
    pid = os.fork()
    if pid == 0:
        svc = service()
        db = svc.store.database
        original = db.run_in_transaction
        path = root / (point + ".json")
        def kill():
            os.kill(os.getpid(), signal.SIGKILL)
            signal.pause()  # Do not cross the boundary while signal delivery is pending.
            raise AssertionError("SIGKILL returned")
        def commit(callback, **options):
            def inside(tx):
                result = callback(tx)
                if point == "during_commit": kill()
                return result
            result = original(inside, **options)
            staged = path.with_suffix(".tmp")
            staged.write_text(json.dumps([[k, i, row.body] for (k, i), row in db.rows.items()]))
            staged.replace(path)
            return result
        db.run_in_transaction = commit
        real = svc.store.flush
        def flush(pages, counter, closure, budget):
            if point == "before_closure" and closure is not None: kill()
            real(pages, counter, closure, budget)
            if point == "after_closure" and closure is not None: kill()
            if point == "full_queue": kill()
        svc.store.flush = flush
        if point == "before_first_flush": kill()
        if point == "full_queue":
            svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
            for _ in range(4):
                assert svc.port.reserve(NOW_US)
                svc.journal.freeze("2026-10-12")
            svc.flush(svc.store.clock()+4)
        asyncio.run(svc.close())
        (root / (point + ".ack")).write_text("unexpected success")
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, (point,status)
'''
    subprocess.run([sys.executable, "-c", script], check=True, timeout=300,  # noqa: S603 - fixed test program
                   env={**os.environ, "P3_KILL_OUTPUT": str(tmp_path)})
    for point in ("before_first_flush", "during_commit", "before_closure", "after_closure", "full_queue"):
        assert not (tmp_path/(point+".ack")).exists()
        path = tmp_path/(point+".json")
        rows = json.loads(path.read_text()) if path.exists() else []
        closed = [json.loads(body) for kind, _, body in rows if kind.endswith("closure_v2")]
        assert bool(closed) == (point == "after_closure")


@pytest.mark.asyncio
async def test_sigterm_chains_host_and_preserves_remaining_grace(monkeypatch: Any) -> None:
    import signal
    svc = service()
    calls = []
    def host(signum: int, frame: Any) -> None:
        calls.append((signum, svc.journal.stopped, svc.stopped.is_set(), svc.termination_deadline))
    current = [host]
    monkeypatch.setattr(signal, "getsignal", lambda sig: current[0])
    monkeypatch.setattr(signal, "signal", lambda sig, handler: current.__setitem__(0, handler))
    svc.trap_sigterm()
    before = svc.store.clock()
    current[0](signal.SIGTERM, None)
    assert calls[0][:3] == (signal.SIGTERM, True, True)
    assert before+9 <= calls[0][3] <= svc.store.clock()+9
    assert svc.close_task is not None
    await svc.close_task
    assert current[0] is host and not svc.unclosed
    await svc.close()  # the host's later lifespan shutdown is idempotent


@pytest.mark.asyncio
async def test_shutdown_cannot_restart_expired_grace() -> None:
    svc = service()
    await svc.close(deadline=svc.store.clock()-1)
    assert svc.unclosed and svc.journal.days


@pytest.mark.parametrize("elapsed,accepted", [(.499999, True), (.5, False), (.500001, False)])
def test_publisher_exact_budget_boundary(monkeypatch: Any, elapsed: float, accepted: bool) -> None:
    svc = service()
    clock = [0.]
    svc.store.clock = lambda: clock[0]
    svc.store.wall = lambda: NOW_US/1e6
    monkeypatch.setattr(svc.store, "claim", lambda *args: True)
    monkeypatch.setattr(svc.store, "observe", lambda *args: health())
    def publish(*args: Any) -> bool:
        clock[0] = elapsed
        return True
    monkeypatch.setattr(svc.store, "publish", publish)
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    counts = svc.counters["2026-10-12"]["publisher"]
    assert counts["publications"] == int(accepted)
    assert counts["failures"] == int(not accepted)


def test_disabled_publisher_never_counts_unattempted_claim(monkeypatch: Any) -> None:
    from unittest.mock import Mock
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6
    claim = Mock(side_effect=AssertionError("disabled claim"))
    monkeypatch.setattr(svc.store, "claim", claim)
    svc.settings.async_settle_shadow_policy = ""
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    p = svc.counters["2026-10-12"]["publisher"]
    assert p["claim_attempts"] == p["claims"] == p["failures"] == p["publications"] == 0
    assert p["missed_slots"] == 1 and not claim.called


def test_retained_transaction_cannot_exceed_checkpoint_row_bound() -> None:
    svc, _, row = retained_case("sample")
    with pytest.raises(ValueError, match="batch_bound"):
        svc.store.retain((row,)*7, Budget(logical_time()+4, logical_time))
    assert svc.store.database.commits == 0


@pytest.mark.parametrize("fault", ["none", "before_commit", "lost_reply"])
@pytest.mark.parametrize("next_day", [False, True])
def test_durable_completion_releases_pending_original(monkeypatch: Any, fault: str, next_day: bool) -> None:
    svc, original, sample = retained_case("sample", pending=True)
    real = svc.store.flush
    def fail(*args: Any) -> None:
        if fault == "lost_reply":
            real(*args)
            raise UnknownCommit("lost original-page reply")
        raise RuntimeError("pre-commit failure")
    if fault != "none":
        monkeypatch.setattr(svc.store, "flush", fail)
        svc.flush(svc.store.clock()+4)
        monkeypatch.setattr(svc.store, "flush", real)
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[original.day].hwm == 0
    page = svc.journal.pages[0].row
    assert page is not None
    at_us = original.observed_us + (86_400_000_000 if next_day else 1)
    svc.store.wall = lambda: at_us/1e6
    svc.observer.wall = svc.observer.evidence_wall = svc.store.wall
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    day = svc.ensure_day(at_us)
    if next_day:
        svc.journal.grant(day, RESERVATION_BLOCK)
    completion = svc.port.reserve(at_us)
    assert completion is not None
    event = copy.deepcopy(page.value()["events"][0])
    event.update(phase="completion", seq=f"{completion.event_seq:020}", event_at_us=at_us, receipt_at_us=at_us,
        completion_of=f"{original.event_seq:020}", prediction=None, partition="A", comparison="exact",
        booking=dict(outcome="settled", reference="0"*64, winner="settle"),
        retry_original=dict(day=original.day, boot=original.boot, event_seq=f"{original.event_seq:020}"))
    assert svc.port.complete_event(completion, page, event)
    assert svc.port.complete(completion, (sample,))
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[original.day].hwm == (1 if next_day else 2)
    assert svc.journal.days[completion.day].hwm == (1 if next_day else 2)
    assert not svc.journal.pages and not svc.journal.batches
    assert page.value()["events"][0]["booking"]["outcome"] == "pending"  # immutable history
    assert svc.journal.days[original.day].failures == int(fault != "none")
