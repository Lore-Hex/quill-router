"""/status.json on Spanner reads the poller's heartbeat, never the outbox.

The per-shard head read (``oldest_enqueued_at``) walks seven days of
deleted-row versions and cost 823-957 ms of Spanner CPU per execution in
production (2026-09-05), once per Cloud Run instance per minute. These tests pin
the replacement end to end: the VM poller writes one ``tr_entities`` row after
a pass (rate-limited, never fatal), the store turns that row into a reading with
one point read and fails closed to ``poller_stale`` when it is absent or old,
the public projection and the fleet checker treat ``poller_stale`` as not
fresh, and no production request path calls the scan any more.
"""

from __future__ import annotations

import datetime as dt
import inspect
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clickhouse import ingest_operational_outbox as worker
from clickhouse.check_fleet_analytics_freshness import _UNAVAILABLE_EXPLANATION, evaluate
from trusted_router import storage_gcp
from trusted_router.config import Settings
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    BACKEND_SPANNER,
    HEARTBEAT_FETCHED_FIELD,
    HEARTBEAT_LAST_DELIVERY_AT_FIELD,
    HEARTBEAT_OBSERVED_AT_FIELD,
    HEARTBEAT_OLDEST_LIVE_COMMIT_TS_FIELD,
    HEARTBEAT_POLLER_FIELD,
    HEARTBEAT_SCHEMA_VERSION_FIELD,
    OUTBOX_HEARTBEAT_ID,
    OUTBOX_HEARTBEAT_KIND,
    OUTBOX_HEARTBEAT_SCHEMA_VERSION,
    PUBLISHABLE_REASONS,
    REASON_POLLER_STALE,
    REASON_UNREACHABLE,
    OutboxFreshness,
    OutboxHeartbeat,
    analytics_status_from_reading,
    analytics_status_unavailable,
    publishable_reason,
)
from trusted_router.routes import public as public_routes
from trusted_router.storage import STORE
from trusted_router.storage_gcp import SpannerBigtableStore

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
OLDEST = NOW - dt.timedelta(seconds=42)


class _ParamTypes:
    INT64 = "INT64"
    STRING = "STRING"
    TIMESTAMP = "TIMESTAMP"


# ---------------------------------------------------------------------------
# (a) the poller publishes
# ---------------------------------------------------------------------------


class _PollerSnapshot:
    def __init__(self, database: _PollerDatabase) -> None:
        self._database = database

    def __enter__(self) -> _PollerSnapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute_sql(self, sql: str, *, params: Any, param_types: Any) -> list[tuple[Any, ...]]:
        self._database.head_reads.append(sql)
        if self._database.head_read_error is not None:
            raise self._database.head_read_error
        return [] if self._database.oldest is None else [(self._database.oldest,)]


class _PollerTransaction:
    def __init__(self, database: _PollerDatabase) -> None:
        self._database = database

    def execute_update(self, sql: str, *, params: Any, param_types: Any) -> int:
        if self._database.write_error is not None:
            raise self._database.write_error
        self._database.writes.append((sql, params, param_types))
        return 1


class _PollerDatabase:
    """What ``SpannerOperationalOutboxSource`` sees: shard heads and a txn."""

    def __init__(
        self,
        *,
        oldest: dt.datetime | None = OLDEST,
        write_error: Exception | None = None,
        head_read_error: Exception | None = None,
    ) -> None:
        self.oldest = oldest
        self.write_error = write_error
        self.head_read_error = head_read_error
        self.head_reads: list[str] = []
        self.writes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def snapshot(self, **_kwargs: object) -> _PollerSnapshot:
        return _PollerSnapshot(self)

    def run_in_transaction(self, func: Any) -> Any:
        return func(_PollerTransaction(self))


def _poller_source(database: _PollerDatabase) -> worker.SpannerOperationalOutboxSource:
    source = object.__new__(worker.SpannerOperationalOutboxSource)
    source._database = database
    source._pt = _ParamTypes()
    source._shard_count = 32
    source._after = None
    return source


def _result(fetched: int) -> worker.DrainResult:
    return worker.DrainResult(fetched=fetched, inserted=fetched, rows_per_second=1.0)


def _published(database: _PollerDatabase) -> OutboxHeartbeat:
    """Decode the LAST heartbeat with the control plane's own parser.

    This is the wire contract between two modules that cannot import each
    other on the VM: the poller's literal body must be what the store reads.
    """
    sql, params, param_types = database.writes[-1]
    assert sql.startswith("INSERT OR UPDATE INTO tr_entities (kind, id, body, updated_at)")
    assert "PENDING_COMMIT_TIMESTAMP()" in sql
    assert params["kind"] == OUTBOX_HEARTBEAT_KIND
    assert params["id"] == OUTBOX_HEARTBEAT_ID
    assert param_types == {"kind": "STRING", "id": "STRING", "body": "STRING"}
    return OutboxHeartbeat.parse(params["body"])


def test_poller_publishes_the_pass_oldest_commit_ts_and_observed_at() -> None:
    database = _PollerDatabase(oldest=OLDEST)
    publisher = worker.OutboxHeartbeatPublisher(
        _poller_source(database), monotonic=lambda: 100.0, now=lambda: NOW, poller="vm:7",
    )

    assert publisher.after_pass(_result(3)) is True

    assert len(database.writes) == 1
    heartbeat = _published(database)
    assert heartbeat.oldest_live_commit_ts == OLDEST
    assert heartbeat.observed_at == NOW
    assert heartbeat.last_delivery_at == NOW
    assert heartbeat.fetched == 3
    assert heartbeat.poller == "vm:7"
    # The head read is the source's own floored per-shard seek, not a new scan.
    assert len(database.head_reads) == 32
    assert all("ORDER BY commit_ts LIMIT 1" in sql for sql in database.head_reads)


def test_an_empty_pass_publishes_none_without_reading_any_shard() -> None:
    """A fetch that returned nothing IS the observation: the head is None."""
    database = _PollerDatabase(oldest=OLDEST)
    publisher = worker.OutboxHeartbeatPublisher(
        _poller_source(database), monotonic=lambda: 0.0, now=lambda: NOW, poller="vm:7",
    )

    assert publisher.after_pass(_result(0)) is True

    heartbeat = _published(database)
    assert heartbeat.oldest_live_commit_ts is None
    assert heartbeat.last_delivery_at is None
    assert heartbeat.fetched == 0
    assert database.head_reads == []


def test_heartbeat_writes_are_rate_limited_to_one_per_interval() -> None:
    clock = [1000.0]
    database = _PollerDatabase(oldest=OLDEST)
    publisher = worker.OutboxHeartbeatPublisher(
        _poller_source(database),
        min_interval_seconds=15.0,
        monotonic=lambda: clock[0],
        # Wall clock tied to the fake monotonic one, so every stamp below is
        # readable as "the pass at N seconds".
        now=lambda: NOW + dt.timedelta(seconds=clock[0] - 1000.0),
        poller="vm:7",
    )

    outcomes: list[bool] = []
    for elapsed, fetched in ((0.0, 1), (2.0, 2), (14.999, 0), (15.0, 0), (16.0, 5), (30.0, 0)):
        clock[0] = 1000.0 + elapsed
        outcomes.append(publisher.after_pass(_result(fetched)))

    assert outcomes == [True, False, False, True, False, True]
    assert len(database.writes) == 3
    # A delivery that happened during a skipped pass still reaches the next
    # heartbeat: the write at 15.0 fetched nothing itself, but the pass at
    # 2.0 delivered two rows.
    second = OutboxHeartbeat.parse(database.writes[1][1]["body"])
    assert second.observed_at == NOW + dt.timedelta(seconds=15)
    assert second.fetched == 0
    assert second.oldest_live_commit_ts is None
    assert second.last_delivery_at == NOW + dt.timedelta(seconds=2)


@pytest.mark.parametrize("failure", ["write", "head_read"])
def test_a_heartbeat_failure_is_logged_and_never_raises(
    caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    database = _PollerDatabase(
        oldest=OLDEST,
        write_error=RuntimeError("PERMISSION_DENIED: tr_entities") if failure == "write" else None,
        head_read_error=RuntimeError("DEADLINE_EXCEEDED") if failure == "head_read" else None,
    )
    source = _poller_source(database)
    source._after = OLDEST
    clock = [0.0]
    publisher = worker.OutboxHeartbeatPublisher(
        source,
        min_interval_seconds=15.0,
        monotonic=lambda: clock[0],
        now=lambda: NOW + dt.timedelta(seconds=clock[0]),
        poller="vm:7",
    )

    def failures() -> list[logging.LogRecord]:
        return [
            r for r in caplog.records if r.message == "operational_analytics_outbox.heartbeat_failed"
        ]

    with caplog.at_level(logging.ERROR, logger=worker.log.name):
        assert publisher.after_pass(_result(1)) is False

    assert database.writes == []
    [record] = failures()
    assert record.exc_info is not None
    # An advisory read that failed proves nothing about committed deletes: the
    # drain's scan floor is untouched (the drain-side proof, including the next
    # fetch, is test_spanner_head_read_failure_keeps_the_warm_floor).
    assert source._after == OLDEST

    # The attempt is stamped BEFORE it runs: a Spanner that rejects every write
    # must not become 32 head reads plus a traceback per 2 s poll. The next
    # pass inside the interval is skipped outright -- no read, no log line.
    head_reads_after_first_attempt = len(database.head_reads)
    with caplog.at_level(logging.ERROR, logger=worker.log.name):
        clock[0] = 1.0
        assert publisher.after_pass(_result(1)) is False
    assert len(database.head_reads) == head_reads_after_first_attempt
    assert len(failures()) == 1

    # ...and once the interval has elapsed the publisher tries again (and
    # fails again, once), so a recovered Spanner gets its heartbeat back.
    with caplog.at_level(logging.ERROR, logger=worker.log.name):
        clock[0] = 15.0
        assert publisher.after_pass(_result(1)) is False
    assert database.writes == []
    assert len(database.head_reads) == 2 * head_reads_after_first_attempt
    assert len(failures()) == 2


def test_the_drain_loop_publishes_after_every_pass_and_survives_a_failing_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat hangs off the loop, not off ``drain_once``.

    A publisher whose Spanner write raises must neither stop the loop nor
    reach the next drain: the loop keeps calling ``drain_once`` until the
    fake batches run out, and every completed pass was offered to it.
    """
    passes = [_result(1), _result(0), _result(4)]
    pending = iter(passes)
    offered: list[worker.DrainResult] = []
    database = _PollerDatabase(oldest=OLDEST, write_error=RuntimeError("write denied"))
    source = _poller_source(database)

    class _Publisher(worker.OutboxHeartbeatPublisher):
        def after_pass(self, result: worker.DrainResult) -> bool:
            offered.append(result)
            return super().after_pass(result)

    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--poll-seconds", "0.01"])
    monkeypatch.setattr(worker, "SpannerOperationalOutboxSource", lambda **_: source)
    monkeypatch.setattr(worker, "ClickHouseOperationalWriter", lambda **_: object())
    monkeypatch.setattr(worker, "sd_notify", lambda _: None)
    monkeypatch.setattr(worker, "drain_once", lambda *_, **__: next(pending))
    monkeypatch.setattr(
        worker, "OutboxHeartbeatPublisher",
        lambda src, **kw: _Publisher(src, min_interval_seconds=0.0, poller="vm:7", **kw),
    )
    monkeypatch.setattr(worker, "time", SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0))

    with pytest.raises(StopIteration):
        worker.main()

    assert offered == passes
    assert database.writes == []


# ---------------------------------------------------------------------------
# (b) the store reads
# ---------------------------------------------------------------------------


class _ScanSpy:
    """Stands in for the Spanner outbox; the MIN scan must never be reached."""

    calls: list[dict[str, Any]]

    def __init__(self) -> None:
        self.calls = []

    def oldest_enqueued_at(self, **kwargs: Any) -> dt.datetime | None:
        self.calls.append(kwargs)
        raise AssertionError("the per-shard MIN(commit_ts) scan ran on the /status path")


class _StoreSnapshot:
    def __init__(self, database: _StoreDatabase) -> None:
        self._database = database

    def __enter__(self) -> _StoreSnapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute_sql(
        self, sql: str, *, params: Any = None, param_types: Any = None, timeout: float | None = None,
    ) -> list[list[Any]]:
        self._database.reads.append((sql, params or {}, timeout))
        if self._database.error is not None:
            raise self._database.error
        if self._database.body is None:
            return []
        return [[self._database.body, self._database.updated_at]]


class _StoreDatabase:
    """The heartbeat row as Spanner returns it: ``body`` plus its commit time.

    ``updated_at`` defaults to "just now", which is what a live poller's
    PENDING_COMMIT_TIMESTAMP() row looks like; ``_UNSET`` lets a test hand the
    store a NULL or garbage commit time.
    """

    def __init__(
        self,
        body: str | None,
        *,
        updated_at: Any = "now",
        error: Exception | None = None,
    ) -> None:
        self.body = body
        self.updated_at = dt.datetime.now(dt.UTC) if updated_at == "now" else updated_at
        self.error = error
        self.reads: list[tuple[str, dict[str, Any], float | None]] = []

    def snapshot(self, **_kwargs: object) -> _StoreSnapshot:
        return _StoreSnapshot(self)


def _store(database: _StoreDatabase) -> tuple[SpannerBigtableStore, _ScanSpy]:
    store = object.__new__(SpannerBigtableStore)
    spy = _ScanSpy()
    store._operational_analytics_outbox = spy  # type: ignore[assignment]
    store._database = database
    store._param_types = _ParamTypes()
    return store, spy


def _body(**overrides: Any) -> str:
    """A heartbeat exactly as the poller writes it, relative to real now."""
    now = dt.datetime.now(dt.UTC)
    fields: dict[str, Any] = {
        "oldest_live_commit_ts": now - dt.timedelta(seconds=42),
        "observed_at": now - dt.timedelta(seconds=5),
        "last_delivery_at": now - dt.timedelta(seconds=9),
        "fetched": 3,
        "poller": "vm:7",
    }
    fields.update(overrides)
    return worker.heartbeat_body(**fields)


def test_a_fresh_heartbeat_is_the_reading_and_the_scan_never_runs() -> None:
    oldest = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=42)
    database = _StoreDatabase(_body(oldest_live_commit_ts=oldest))
    store, spy = _store(database)

    reading = store.operational_analytics_outbox_freshness()

    assert reading.available is True
    assert reading.backend == BACKEND_SPANNER
    assert reading.oldest_enqueued_at == oldest
    assert reading.seconds_since_last_delivery is not None
    assert 8.5 <= reading.seconds_since_last_delivery <= 10.5
    assert spy.calls == []
    [(sql, params, timeout)] = database.reads
    assert sql == "SELECT body, updated_at FROM tr_entities WHERE kind=@kind AND id=@id"
    assert params == {"kind": OUTBOX_HEARTBEAT_KIND, "id": OUTBOX_HEARTBEAT_ID}
    assert timeout == storage_gcp.OUTBOX_FRESHNESS_TIMEOUT_SECONDS


def test_a_drained_heartbeat_publishes_zero_lag() -> None:
    store, spy = _store(_StoreDatabase(_body(oldest_live_commit_ts=None, fetched=0)))

    reading = store.operational_analytics_outbox_freshness()
    section = analytics_status_from_reading(reading, now=dt.datetime.now(dt.UTC))

    assert reading.available is True
    assert reading.oldest_enqueued_at is None
    assert section["drain_lag_seconds"] == 0.0
    assert spy.calls == []


def test_an_absent_heartbeat_is_poller_stale_not_a_scan(caplog: pytest.LogCaptureFixture) -> None:
    store, spy = _store(_StoreDatabase(None))

    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp"):
        reading = store.operational_analytics_outbox_freshness()

    assert reading == OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE)
    assert spy.calls == []
    assert any(
        r.message == "spanner.operational_analytics_outbox_heartbeat_missing" for r in caplog.records
    )


def test_a_heartbeat_older_than_the_max_age_is_poller_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_gcp, "OUTBOX_HEARTBEAT_MAX_AGE_SECONDS", 180.0)
    now = dt.datetime.now(dt.UTC)

    old = now - dt.timedelta(seconds=181)
    stale, spy = _store(_StoreDatabase(_body(observed_at=old), updated_at=old))
    assert stale.operational_analytics_outbox_freshness() == OutboxFreshness.unavailable(
        BACKEND_SPANNER, REASON_POLLER_STALE
    )
    assert spy.calls == []

    # Ten seconds inside the bound is still a reading; the age is a bound, not
    # a rounding.
    recent = now - dt.timedelta(seconds=170)
    fresh, _ = _store(_StoreDatabase(_body(observed_at=recent), updated_at=recent))
    assert fresh.operational_analytics_outbox_freshness().available is True


def test_staleness_is_judged_by_the_row_commit_time_not_the_poller_clock(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """``updated_at`` is PENDING_COMMIT_TIMESTAMP(); ``observed_at`` is whatever
    the VM's clock said. Only the first is evidence of a recent write."""
    monkeypatch.setattr(storage_gcp, "OUTBOX_HEARTBEAT_MAX_AGE_SECONDS", 180.0)
    now = dt.datetime.now(dt.UTC)

    # A body stamped "just now" on a row Spanner committed 181s ago: the poller
    # has not written since, whatever its clock claimed at the time.
    stale, spy = _store(
        _StoreDatabase(
            _body(observed_at=now - dt.timedelta(seconds=1)),
            updated_at=now - dt.timedelta(seconds=181),
        )
    )
    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp"):
        reading = stale.operational_analytics_outbox_freshness()
    assert reading == OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE)
    assert spy.calls == []
    [record] = [
        r for r in caplog.records
        if r.message == "spanner.operational_analytics_outbox_heartbeat_stale"
    ]
    # Both clocks are in the log so the poller's write lag can be read off it.
    assert 180.5 <= record.age_seconds <= 182.5
    assert record.committed_at and record.observed_at

    # The converse: a skewed VM clock stamping the body 181s in the past on a
    # row committed one second ago is a live poller, and stays a reading.
    fresh, _ = _store(
        _StoreDatabase(
            _body(observed_at=now - dt.timedelta(seconds=181)),
            updated_at=now - dt.timedelta(seconds=1),
        )
    )
    assert fresh.operational_analytics_outbox_freshness().available is True


@pytest.mark.parametrize("updated_at", [None, "2026-09-05T12:00:00Z", 1_757_000_000])
def test_a_heartbeat_without_a_commit_time_is_poller_stale_and_logged(
    caplog: pytest.LogCaptureFixture, updated_at: Any,
) -> None:
    """A NULL or non-timestamp ``updated_at`` is a hand-written row, not a
    connectivity failure: `poller_stale`, with the invalid-row log line."""
    store, spy = _store(_StoreDatabase(_body(), updated_at=updated_at))

    with caplog.at_level(logging.ERROR, logger="trusted_router.storage_gcp"):
        reading = store.operational_analytics_outbox_freshness()

    assert reading == OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE)
    assert spy.calls == []
    assert any(
        r.message == "spanner.operational_analytics_outbox_heartbeat_invalid" for r in caplog.records
    )


def test_the_max_age_is_overridable_from_the_environment() -> None:
    script = "import trusted_router.storage_gcp as s; print(s.OUTBOX_HEARTBEAT_MAX_AGE_SECONDS)"
    env = dict(os.environ, TR_OUTBOX_HEARTBEAT_MAX_AGE_SECONDS="42", PYTHONPATH="src")
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inert script.
        [sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42.0"
    assert storage_gcp.OUTBOX_HEARTBEAT_MAX_AGE_SECONDS == 180.0


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"schema_version": 2, "observed_at": "2026-09-05T12:00:00+00:00"}',
        '{"schema_version": 1}',
        '{"schema_version": 1, "observed_at": "yesterday"}',
        '{"schema_version": 1, "observed_at": "2026-09-05T12:00:00Z", "fetched": "3"}',
    ],
)
def test_an_unreadable_heartbeat_is_poller_stale_and_logged(
    caplog: pytest.LogCaptureFixture, body: str,
) -> None:
    store, spy = _store(_StoreDatabase(body))

    with caplog.at_level(logging.ERROR, logger="trusted_router.storage_gcp"):
        reading = store.operational_analytics_outbox_freshness()

    assert reading == OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE)
    assert spy.calls == []
    assert any(
        r.message == "spanner.operational_analytics_outbox_heartbeat_invalid" for r in caplog.records
    )


def test_a_failing_point_read_is_unreachable_not_stale() -> None:
    """Could not look and looked-but-nobody-wrote are different failures."""
    store, spy = _store(_StoreDatabase(None, error=RuntimeError("session pool exhausted")))

    reading = store.operational_analytics_outbox_freshness()

    assert reading == OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_UNREACHABLE)
    assert spy.calls == []


# ---------------------------------------------------------------------------
# (c) the projection and the fleet checker
# ---------------------------------------------------------------------------


def test_poller_stale_reaches_the_public_section_by_name() -> None:
    reading = OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE)

    section = analytics_status_from_reading(reading, now=NOW)

    assert section == {"available": False, "reason": REASON_POLLER_STALE}
    assert publishable_reason(REASON_POLLER_STALE) == REASON_POLLER_STALE
    assert analytics_status_unavailable(REASON_POLLER_STALE)["reason"] == REASON_POLLER_STALE


def test_status_json_renders_poller_stale_as_the_unavailable_section(monkeypatch) -> None:
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness.unavailable(BACKEND_SPANNER, REASON_POLLER_STALE),
        raising=False,
    )
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    monkeypatch.setattr(public_routes, "_STATUS_ANALYTICS_CACHE", None)

    payload = public_routes._status_snapshot(Settings(environment="local"))

    assert payload[ANALYTICS_STATUS_KEY] == {"available": False, "reason": REASON_POLLER_STALE}


@pytest.mark.parametrize("expects_outbox", [True, False])
def test_the_fleet_checker_treats_poller_stale_as_not_fresh(expects_outbox: bool) -> None:
    payload = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_POLLER_STALE)}

    problems = evaluate(payload, now=NOW, expects_outbox=expects_outbox)

    assert len(problems) == 1
    assert f"reason={REASON_POLLER_STALE!r}" in problems[0]
    assert "heartbeat" in problems[0]
    assert "narrowed" not in problems[0]


def test_every_publishable_reason_has_an_operator_explanation() -> None:
    """``evaluate`` indexes the explanation table by recognised reason.

    A reason the publisher can emit and the checker cannot explain is a
    KeyError in the job that was supposed to report the problem.
    """
    assert set(_UNAVAILABLE_EXPLANATION) == PUBLISHABLE_REASONS


# ---------------------------------------------------------------------------
# (d) nothing on a production request path scans the outbox
# ---------------------------------------------------------------------------

_SCAN_CALL = re.compile(r"\.oldest_enqueued_at\(")


def test_no_production_request_path_calls_the_shard_head_scan() -> None:
    """Static, and stated as such: the behavioural proof is the spy above.

    ``oldest_enqueued_at`` stays for operator tooling. What this pins is that
    nothing under ``src/trusted_router`` -- the routes and the store the
    /status.json build path goes through -- calls it, so the 16-shard MIN
    scan cannot come back on a request path without failing this test.
    """
    freshness = inspect.getsource(SpannerBigtableStore.operational_analytics_outbox_freshness)
    assert not _SCAN_CALL.search(freshness)

    routes = sorted((ROOT / "src/trusted_router/routes").rglob("*.py"))
    assert routes, "the routes package moved; point this test at it"
    for path in routes:
        assert not _SCAN_CALL.search(path.read_text()), path

    callers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "src/trusted_router").rglob("*.py")
        if _SCAN_CALL.search(path.read_text())
    )
    assert callers == []


# ---------------------------------------------------------------------------
# the two copies of the wire contract, and the "no DDL" claim
# ---------------------------------------------------------------------------


def test_the_poller_and_the_store_agree_on_the_heartbeat_row() -> None:
    """Two literal copies, because the poller cannot import the store's."""
    assert worker.HEARTBEAT_TABLE == SpannerBigtableStore.entity_table == "tr_entities"
    assert worker.HEARTBEAT_KIND == OUTBOX_HEARTBEAT_KIND
    assert worker.HEARTBEAT_ID == OUTBOX_HEARTBEAT_ID
    assert worker.HEARTBEAT_SCHEMA_VERSION == OUTBOX_HEARTBEAT_SCHEMA_VERSION
    assert worker.HEARTBEAT_SCHEMA_VERSION_FIELD == HEARTBEAT_SCHEMA_VERSION_FIELD
    assert worker.HEARTBEAT_OLDEST_LIVE_COMMIT_TS_FIELD == HEARTBEAT_OLDEST_LIVE_COMMIT_TS_FIELD
    assert worker.HEARTBEAT_OBSERVED_AT_FIELD == HEARTBEAT_OBSERVED_AT_FIELD
    assert worker.HEARTBEAT_LAST_DELIVERY_AT_FIELD == HEARTBEAT_LAST_DELIVERY_AT_FIELD
    assert worker.HEARTBEAT_FETCHED_FIELD == HEARTBEAT_FETCHED_FIELD
    assert worker.HEARTBEAT_POLLER_FIELD == HEARTBEAT_POLLER_FIELD


def test_the_spanner_poller_still_imports_nothing_from_trusted_router() -> None:
    """Its systemd unit has no PYTHONPATH; an import of trusted_router would
    crash the drain on the VM at start, silently, under Restart=always."""
    source = (ROOT / "clickhouse/ingest_operational_outbox.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+trusted_router", source, re.MULTILINE)


def test_the_heartbeat_needs_no_ddl() -> None:
    """The row lives in the entity table every deployment already has."""
    infra = (ROOT / "scripts/deploy/infra.sh").read_text()
    ddl = re.search(r"CREATE TABLE tr_entities \((.*?)\) PRIMARY KEY \(kind, id\)", infra)
    assert ddl is not None
    for column in (
        "kind STRING",
        "id STRING",
        "body STRING(MAX)",
        # The poller writes updated_at as PENDING_COMMIT_TIMESTAMP(); Spanner
        # rejects that on a column without allow_commit_timestamp, and every
        # heartbeat would then fail and /status.json read poller_stale forever.
        "updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)",
    ):
        assert column in ddl.group(1)
