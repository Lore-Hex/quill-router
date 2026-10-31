"""Process lifecycle, real-fake progress/cadence, and frozen-main inventories."""
# ruff: noqa: F811, F401 - reuse the established Spanner fixture
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import inspect
import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.route_inventory import effective_routes
from tests.test_async_settle_drain import seed
from tests.test_service_surface_secret_isolation import _SENSITIVE_TEST_VALUES
from tests.test_settle_outbox_drain import fake_store
from trusted_router.config import SERVICE_SURFACE_SECRET_OWNERS, Settings
from trusted_router.main import create_app
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services import settle_outbox_worker as worker
from trusted_router.services.async_settle import Admission, AdmissionCache
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage_gcp_async_admission import read_health
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

PINS = dict(settle_outbox_poll_interval_seconds=1, settle_outbox_pass_budget_seconds=2,
            settle_outbox_lease_seconds=30, settle_outbox_worker_concurrency=4,
            settle_outbox_claim_batch=4, settle_outbox_health_publish_interval_seconds=1)


def config(**overrides):
    return Settings(**dict(dict(environment="test", service_surface="settle-drain",
                               settle_outbox_fast_drain_enabled=True), **dict(PINS, **overrides)))


def production_config(**overrides):
    return config(**dict(dict(environment="production", storage_backend="spanner-clickhouse",
        spanner_instance_id="test-instance", spanner_database_id="test-database",
        async_settle_protection=True, settle_outbox_enabled=True,
        generation_records_enabled=True, request_record_write_mode="typed",
        analytics_outbox_enabled=True, operational_analytics_outbox_enabled=True,
        operational_analytics_sink="outbox", sentry_dsn="https://public@example.com/1"), **overrides))


def inventory_settings(surface, enabled):
    # model_copy avoids unrelated secret/signer validation; no startup is run.
    cfg = Settings(environment="test", service_surface=surface)
    return cfg.model_copy(update=dict(
        environment="canary", allow_deployed_combined_surface=surface == "combined",
        speculative_provider_shadow_enabled=enabled,
        routing_snapshot_publish_enabled=enabled, edge_policy_publish_enabled=enabled,
        federation_deferred_settlement_enabled=enabled,
        federation_settlement_home_token="inventory-only" if enabled else "",
        federation_home_base_url="https://example.com" if enabled else "",
        settle_outbox_enabled=enabled, activation_reminder_interval_seconds=60 if enabled else 0,
        retirement_notices_mode="observe" if enabled else "off",
        synthetic_scheduler_interval_seconds=60 if enabled else 0,
        remediator_mode="observe" if enabled else "off", remediator_in_process_enabled=enabled,
    ))


def inventory(app):
    routes = sorted((path, sorted(getattr(route, "methods", []) or []),
                     getattr(getattr(route, "endpoint", None), "__qualname__", ""))
                    for path, route in effective_routes(app))
    return dict(routes_sha256=hashlib.sha256(json.dumps(routes).encode()).hexdigest(),
                route_count=len(routes), startup=[f.__qualname__ for f in app.router.on_startup],
                shutdown=[f.__qualname__ for f in app.router.on_shutdown],
                state=sorted(vars(app.state)["_state"]))


@pytest.mark.parametrize("surface", ["combined", "public", "actions", "control", "internal", "observer"])
@pytest.mark.parametrize("enabled", [False, True])
def test_existing_surfaces_match_main_inventory(surface, enabled):
    frozen = json.loads((Path(__file__).parent / "fixtures/settle_drain_surface_inventory.json").read_text())
    assert frozen["revision"] == "ddfbd1e62"
    app = create_app(inventory_settings(surface, enabled), configure_store_arg=False, init_observability=False)
    assert inventory(app) == frozen[f"{surface}/{enabled}"]


def test_worker_surface_excludes_every_other_starter(monkeypatch):
    # Trap every installer create_app can delegate to, even when flags are ON.
    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated worker initialization")
    for module in ["speculation_shadow", "routing_snapshot_publisher", "edge_policy_publisher",
                   "async_settle_shadow", "async_settle_shadow_cohort_runtime",
                   "async_settle_shadow_health", "async_settle_shadow_startup_harness"]:
        monkeypatch.setattr(f"trusted_router.services.{module}.install", forbidden)
    for name in ["init_sentry", "init_axiom", "create_analytics_sink", "report_refused_manifest_rows",
                 "validate_auto_model_order", "register_http_middleware", "StageDPolicyResolver"]:
        monkeypatch.setattr(f"trusted_router.main.{name}", forbidden)
    app = create_app(inventory_settings("settle-drain", True), configure_store_arg=False)
    assert [r.path for r in app.routes] == ["/health"]
    assert [f.__name__ for f in app.router.on_startup] == ["_start_settle_drain"]
    assert [f.__name__ for f in app.router.on_shutdown] == ["_stop_settle_drain"]
    assert sorted(vars(app.state)["_state"]) == ["settings"]
    assert not app.user_middleware


def test_worker_production_pins_need_only_alerting_secret():
    cfg = production_config()
    assert cfg.settle_outbox_pass_budget_seconds < cfg.settle_outbox_lease_seconds
    assert {name for name, owners in SERVICE_SURFACE_SECRET_OWNERS.items() if "settle-drain" in owners} == {"sentry_dsn"}


@pytest.mark.parametrize("name,value", [(k, v) for k, v in _SENSITIVE_TEST_VALUES.items() if k != "sentry_dsn"])
def test_worker_rejects_other_surface_credentials(name, value):
    with pytest.raises(ValueError):
        production_config(**{name: value})


@pytest.mark.parametrize("overrides", [dict(settle_outbox_poll_interval_seconds=300),
    dict(settle_outbox_lease_seconds=60), dict(settle_outbox_health_publish_interval_seconds=5),
    dict(settle_outbox_stall_seconds=25), dict(settle_outbox_stall_seconds=31),
    dict(settle_outbox_pass_budget_seconds=30), dict(settle_outbox_pass_budget_seconds=31)])
def test_worker_rejects_incompatible_pins(overrides):
    with pytest.raises(ValueError):
        config(**overrides)


def test_fast_off_is_idle_healthy_and_logs_once(monkeypatch, caplog):
    def forbidden(*args, **kwargs):
        raise AssertionError("idle worker touched the outbox")
    monkeypatch.setattr(drain, "spanner_settle_outbox", forbidden)
    monkeypatch.setattr("trusted_router.main.create_store", forbidden)
    with caplog.at_level(logging.INFO), TestClient(create_app(config(settle_outbox_fast_drain_enabled=False),
            init_observability=False)) as client:
        for _ in range(3):
            assert client.get("/health").json() == {"status": "idle"}
    assert sum("async_drain.idle" in r.message for r in caplog.records) == 1


def test_worker_runs_publishes_and_shutdown_is_prompt(fake_store, monkeypatch):
    published = threading.Event()
    original = worker.publish_health
    def publish(db):
        result = original(db)
        published.set()
        return result
    monkeypatch.setattr(worker, "publish_health", publish)
    with TestClient(create_app(config(), configure_store_arg=False, init_observability=False)) as client:
        assert published.wait(2)
        assert time.time() - read_health(fake_store[1])["observed_at"] < 2
        assert client.get("/health").status_code == 200
        started = time.monotonic()
    assert time.monotonic() - started < .5


def test_failed_pass_recovers_without_publishing_failure(fake_store, monkeypatch):
    stop = threading.Event()
    calls = []
    original = worker.drain_pass
    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            assert read_health(fake_store[1]) is None
            raise TimeoutError
        result = original(*args, **kwargs)
        stop.set()
        return result
    monkeypatch.setattr(worker, "drain_pass", run)
    worker.run_worker(config(settle_outbox_poll_interval_seconds=.01), stop)
    assert len(calls) == 2
    assert read_health(fake_store[1]) is not None


def test_busy_pass_publishes_without_hiding_backlog(fake_store, monkeypatch):
    seed(fake_store[0], 32)
    for row in fake_store[1].settle_outbox.values():
        row["created_at"] = "2020-01-01T00:00:00Z"
    original = worker.publish_health
    observations = []
    def publish(db):
        value = original(db)
        observations.append((time.monotonic(), value))
        return value
    def apply(row):
        time.sleep(.025)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(worker, "publish_health", publish)
    monkeypatch.setattr(drain, "apply_frozen_settle", apply)
    worker.drain_pass(32, settings=config(settle_outbox_health_publish_interval_seconds=.04))
    assert len(observations) >= 3
    assert all(b[0] - a[0] < .2 for a, b in zip(observations, observations[1:], strict=False))
    assert observations[0][1]["p95_age_seconds"] > 5
    assert observations[0][1]["backlog_count"] > 0


def test_stalled_apply_stops_publication_and_bounded_shutdown(fake_store, monkeypatch):
    seed(fake_store[0], 2)
    for i, row in enumerate(fake_store[1].settle_outbox.values()):
        row["queue_shard"] = i
    entered, release = threading.Event(), threading.Event()
    calls = []
    def apply(row):
        calls.append(row)
        if len(calls) == 2:
            entered.set()
            assert release.wait(5)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(drain, "apply_frozen_settle", apply)
    monkeypatch.setattr(worker, "SHUTDOWN_JOIN_SECONDS", .05)
    monkeypatch.setattr(worker, "monitor_worker", lambda *args: None)
    app = create_app(config(settle_outbox_worker_concurrency=1).model_copy(update={"settle_outbox_stall_seconds": .1}), configure_store_arg=False, init_observability=False)
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            before = read_health(fake_store[1])
            assert before is not None
            time.sleep(.15)
            assert read_health(fake_store[1]) == before
            assert client.get("/health").status_code == 503
            cache = AdmissionCache(lambda ws: Admission(0, 2),
                health_read=lambda: read_health(fake_store[1]), wall_clock=lambda: before["observed_at"] + 5)
            assert not cache.eligible("test", 0)
            started = time.monotonic()
        assert time.monotonic() - started < .5
    finally:
        release.set()
        # Let the actual fenced apply/resolve finish before fixture teardown.
        deadline = time.monotonic() + 2
        while any(t.name == "settle-drain-loop" for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(.01)
    assert len(calls) == 2
    assert all(r["status"] == "done" for r in fake_store[1].settle_outbox.values())


def test_local_cadence_avoids_transactions_when_not_due(fake_store, monkeypatch, caplog):
    ticks = [0.]
    monkeypatch.setattr(worker.time, "monotonic", lambda: ticks[0])
    health = []
    def forbidden(*a, **kw):
        raise AssertionError("worker ran housekeeping")
    monkeypatch.setattr(drain, "housekeeping", forbidden)
    monkeypatch.setattr(SpannerSettleOutbox, "purge_done", forbidden)
    monkeypatch.setattr(worker, "claim_health_publish", lambda *a: health.append(ticks[0]) or False)
    cadence = worker.WorkerCadence()
    with caplog.at_level(logging.INFO):
        for tick in range(21):
            ticks[0] = tick / 10
            worker.drain_pass(1, settings=config(), cadence=cadence)
    assert health == [0., 1., 2.]
    queries = fake_store[1].snapshot_sql_params
    assert sum("shard" in params for params in queries) == 21 * 16
    assert not any("async_drain.pass " in r.message for r in caplog.records)
    assert sum("async_drain.summary" in r.message for r in caplog.records) == 1


def test_legacy_watchdog_never_leases_async_rows_even_without_health(fake_store):
    box = seed(fake_store[0], 2)
    db = fake_store[1]
    db.settle_outbox[("fast-0", "settle")]["async_version"] = 1
    db.settle_outbox[("fast-0", "settle")]["queue_shard"] = 0
    assert read_health(db) is None
    assert [row.authorization_id for row in box.claim(limit=500, lease_seconds=300)] == ["fast-1"]
    assert db.settle_outbox[("fast-0", "settle")]["leased_until"] is None
    [row] = box.claim_shard(shard=0, lease_seconds=30)
    assert row.authorization_id == "fast-0"


def test_worker_skips_router_sigterm_delay():
    from trusted_router.serve import drain_seconds_from_env
    assert drain_seconds_from_env({"TR_SERVICE_SURFACE": "settle-drain"}) == 0


@pytest.mark.parametrize("field", ["async_settle_ticket_private_key_file", "routing_snapshot_private_key_file",
                                   "speculation_shadow_private_key_file"])
def test_worker_rejects_signing_keys(field):
    with pytest.raises(ValueError, match=f"unset TR_{field.upper()}"):
        production_config(**{field: "/not-mounted/key.pem"})


def test_stopped_claim_is_reclaimable_at_worker_lease_expiry(fake_store, monkeypatch):
    from trusted_router import storage_gcp_settle_outbox as adapter
    box = seed(fake_store[0], 1)
    fake_store[1].settle_outbox[("fast-0", "settle")]["queue_shard"] = 0
    stamp = [dt.datetime.now(dt.UTC)]
    monkeypatch.setattr(adapter, "_iso_now", lambda: stamp[0].replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    stop = threading.Event()
    original = SpannerSettleOutbox.claim_candidates
    def claim(self, candidates, **kwargs):
        result = original(self, candidates, **kwargs)
        stop.set()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(SpannerSettleOutbox, "claim_candidates", claim)
        assert worker._apply_slot(box, 0, 30, time.monotonic() + 2, stop) == (1, "deferred", 0)
    [pending] = list(fake_store[1].settle_outbox.values())
    assert pending["status"] == "pending"
    assert box.claim_shard(shard=0, lease_seconds=30) == []
    stamp[0] += dt.timedelta(seconds=31)
    [reclaimed] = box.claim_shard(shard=0, lease_seconds=30)
    assert reclaimed.authorization_id == "fast-0"


def test_existing_factory_code_matches_frozen_main_below_worker_branch():
    # Covers every combination of shadow v1/v2 and other gated installers,
    # beyond the concrete on/off inventories. Only the early worker branch
    # differs from the pinned main factory; no existing surface code moved.
    frozen = json.loads((Path(__file__).parent / "fixtures/settle_drain_surface_inventory.json").read_text())
    node = ast.parse(inspect.getsource(create_app)).body[0]
    node.body = [statement for statement in node.body if not (
        isinstance(statement, ast.If) and ast.unparse(statement.test) == "surface == 'settle-drain'")]
    assert hashlib.sha256(ast.dump(node).encode()).hexdigest() == frozen["factory_ast_sha256"]


@pytest.mark.parametrize("outcome", [ApplyOutcome.SETTLED_NOW, ApplyOutcome.INVALID_ROW,
                                   ApplyOutcome.ERROR, ApplyOutcome.RESERVATION_MISSING])
def test_worker_primitive_logs_have_no_identifiers(fake_store, monkeypatch, caplog, outcome):
    box = seed(fake_store[0], 1)
    fake_store[1].settle_outbox[("fast-0", "settle")]["queue_shard"] = 0
    def apply(row):
        # Exercise both helper logging and the actual resolver, including
        # alert/exception paths; exception text must not leak identifiers.
        try:
            raise RuntimeError("private-key private-workspace")
        except RuntimeError:
            drain.logger.exception("authorization_id=%s", row.authorization_id)
        return outcome
    monkeypatch.setattr(drain, "apply_frozen_settle", apply)
    factory = logging.getLogRecordFactory()
    from trusted_router.services.settle_drain_observability import install_logging
    cleanup = install_logging()
    with caplog.at_level(logging.INFO):
        assert worker._apply_slot(box, 0, 30, time.monotonic() + 2)[0] == 1
    cleanup()
    assert logging.getLogRecordFactory() is factory
    assert caplog.records
    assert not any(value in caplog.text for value in ["fast-0", "private-key", "private-workspace"])
    assert all(record.exc_info is None for record in caplog.records)
    caplog.clear()
    drain.logger.warning("legacy watchdog diagnostic remains unchanged")
    assert "legacy watchdog diagnostic remains unchanged" in caplog.text




def test_stall_across_multiple_intervals_never_publishes(fake_store, monkeypatch):
    seed(fake_store[0], 1)
    next(iter(fake_store[1].settle_outbox.values()))['queue_shard'] = 0
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    worker.publish_health(fake_store[1])
    before = read_health(fake_store[1])
    def apply(row):
        entered.set()
        assert release.wait(5)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(drain, 'apply_frozen_settle', apply)
    cfg = config(settle_outbox_health_publish_interval_seconds=.03,
                 settle_outbox_poll_interval_seconds=.01, settle_outbox_worker_concurrency=1)
    thread = threading.Thread(target=worker.run_worker, args=(cfg, stop))
    thread.start()
    try:
        assert entered.wait(2)
        # Six publication intervals, while a real claimed row remains in apply.
        time.sleep(.18)
        assert read_health(fake_store[1]) == before
    finally:
        stop.set()
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_repeated_claim_failures_cannot_refresh_durable_health(fake_store, monkeypatch):
    failed, recover, stop = threading.Event(), threading.Event(), threading.Event()
    # A claim RPC runs only for a due candidate: every shard has one, so every
    # slot fails its claim instead of completing an empty scan.
    _workspace_rows(fake_store, [f"ws-{i}" for i in range(16)])
    worker.publish_health(fake_store[1])
    before = read_health(fake_store[1])
    original = SpannerSettleOutbox.claim_candidates
    calls = []
    def claim(self, candidates, **kw):
        if not recover.is_set():
            calls.append(1)
            if len(calls) >= 3:
                failed.set()
            raise TimeoutError('private-reservation')
        return original(self, candidates, **kw)
    monkeypatch.setattr(SpannerSettleOutbox, 'claim_candidates', claim)
    cfg = config(settle_outbox_health_publish_interval_seconds=.03,
                 settle_outbox_poll_interval_seconds=.01, settle_outbox_worker_concurrency=1)
    thread = threading.Thread(target=worker.run_worker, args=(cfg, stop))
    thread.start()
    try:
        assert failed.wait(2)
        time.sleep(.18)
        assert len(calls) >= 3
        assert read_health(fake_store[1]) == before
        recover.set()
        deadline = time.monotonic() + 2
        while read_health(fake_store[1]) == before and time.monotonic() < deadline:
            time.sleep(.01)
        assert read_health(fake_store[1]) != before
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()


def test_monitor_dumps_all_stacks_then_exits_without_shutdown_delay(monkeypatch):
    calls = []
    cfg = config().model_copy(update={'settle_outbox_stall_seconds': .02})
    cadence = worker.WorkerCadence(progress_at=time.monotonic() - 1)
    monkeypatch.setattr(worker, 'stall_dump_fd', lambda fd: 99)
    monkeypatch.setattr(worker.faulthandler, 'dump_traceback', lambda **kw: calls.append(('stacks', kw)))
    monkeypatch.setattr(worker.os, '_exit', lambda code: calls.append(('exit', code)))
    monkeypatch.setattr(worker, 'MONITOR_INTERVAL_SECONDS', .001)
    worker.monitor_worker(cfg, threading.Event(), cadence)
    assert calls == [('stacks', {'file': 99, 'all_threads': True}), ('exit', 70)]


def test_rpc_progress_only_updates_liveness_not_fleet_health(fake_store, monkeypatch):
    ticks = [0.]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: ticks[0])
    from trusted_router.storage_gcp_io import _SPANNER_RPC_COUNTER
    cadence = worker.WorkerCadence()
    with cadence.observe_io():
        counter = _SPANNER_RPC_COUNTER.get()
        assert counter is not None
        for tick in (20., 40., 60.):
            ticks[0] = tick
            counter.increment()
            assert cadence.stall_age() == 0
            assert read_health(fake_store[1]) is None
        ticks[0] += 25
        assert cadence.stall_age() == 25


def test_fast_failures_keep_monitor_alive_without_publication(fake_store, monkeypatch):
    exits = []
    monkeypatch.setattr(worker, '_exit_stalled_worker', lambda: exits.append(1))
    monkeypatch.setattr(worker, 'MONITOR_INTERVAL_SECONDS', .005)
    def fail(*a, **kw):
        raise TimeoutError('private-reservation')
    monkeypatch.setattr(SpannerSettleOutbox, 'claim_candidates', fail)
    # Every shard has a due row, so every slot reaches the failing claim.
    _workspace_rows(fake_store, [f"ws-{i}" for i in range(16)])
    cfg = config(settle_outbox_poll_interval_seconds=.005).model_copy(
        update={'settle_outbox_stall_seconds': .05})
    with TestClient(create_app(cfg, configure_store_arg=False, init_observability=False)) as client:
        time.sleep(.2)
        assert client.get('/health').status_code == 200
        assert exits == []
        assert read_health(fake_store[1]) is None


def _workspace_rows(store, workspaces):
    box = seed(store[0], len(workspaces))
    for i, workspace in enumerate(workspaces):
        row = store[1].settle_outbox[(f"fast-{i}", "settle")]
        row["queue_shard"], row["workspace_id"] = i % 16, workspace
    return box


def test_parallel_slots_apply_one_row_per_workspace_at_a_time(fake_store, monkeypatch):
    # A backlog on one workspace must not run several finalizes against its
    # single credit row at once; other workspaces keep their parallel slots.
    box = _workspace_rows(fake_store, ["hot"] * 8 + ["cold-a", "cold-b"])
    lock, inflight, peak, applied = threading.Lock(), Counter(), Counter(), []
    def apply(row):
        with lock:
            inflight[row.workspace_id] += 1
            peak[row.workspace_id] = max(peak[row.workspace_id], inflight[row.workspace_id])
        time.sleep(.02)
        with lock:
            inflight[row.workspace_id] -= 1
            applied.append(row.workspace_id)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(drain, "apply_frozen_settle", apply)
    for _ in range(20):
        if len(applied) == 10:
            break
        worker.drain_pass(10, settings=config())
    assert sorted(applied) == sorted(["hot"] * 8 + ["cold-a", "cold-b"])
    assert peak["hot"] == 1
    assert worker._WORKSPACES._busy == set()
    assert {row["status"] for row in fake_store[1].settle_outbox.values()} == {"done"}
    assert box.claim_shard(shard=0, lease_seconds=30) == []


def test_busy_workspace_rows_stay_unleased_for_the_next_pass(fake_store, monkeypatch):
    # One shard, both rows inside its two-candidate scan.
    box = _workspace_rows(fake_store, ["hot", "cold"])
    for row in fake_store[1].settle_outbox.values():
        row["queue_shard"] = 0
    monkeypatch.setattr(drain, "apply_frozen_settle", lambda row: ApplyOutcome.SETTLED_NOW)
    assert worker._WORKSPACES.enter("hot")
    try:
        assert worker._apply_slot(box, 0, 30, time.monotonic() + 2)[:2] == (1, ApplyOutcome.SETTLED_NOW)
        assert worker._apply_slot(box, 0, 30, time.monotonic() + 2) == (0, "", 0)
    finally:
        worker._WORKSPACES.leave("hot")
    statuses = {aid: row["status"] for (aid, _), row in fake_store[1].settle_outbox.items()}
    assert statuses == {"fast-0": "pending", "fast-1": "done"}
    # The skipped row was never leased, so the next pass claims it at once.
    assert fake_store[1].settle_outbox[("fast-0", "settle")]["lease_owner"] is None
    assert worker._apply_slot(box, 0, 30, time.monotonic() + 2)[0] == 1
    assert worker._WORKSPACES._busy == set()


def test_failed_claim_releases_the_workspace(fake_store, monkeypatch):
    box = _workspace_rows(fake_store, ["hot"])
    def fail(self, candidates, **kwargs):
        raise TimeoutError
    with monkeypatch.context() as patch:
        patch.setattr(SpannerSettleOutbox, "claim_candidates", fail)
        with pytest.raises(TimeoutError):
            worker._apply_slot(box, 0, 30, time.monotonic() + 2)
    assert worker._WORKSPACES._busy == set()
    def lost(self, candidates, **kwargs):
        return []
    with monkeypatch.context() as patch:
        patch.setattr(SpannerSettleOutbox, "claim_candidates", lost)
        assert worker._apply_slot(box, 0, 30, time.monotonic() + 2) == (0, "", 0)
    assert worker._WORKSPACES._busy == set()


def test_apply_failure_releases_the_workspace(fake_store, monkeypatch):
    box = _workspace_rows(fake_store, ["hot"])
    def resolve(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(drain, "apply_frozen_settle", lambda row: ApplyOutcome.SETTLED_NOW)
    monkeypatch.setattr(drain, "_resolve_row", resolve)
    with pytest.raises(KeyboardInterrupt):
        worker._apply_slot(box, 0, 30, time.monotonic() + 2)
    assert worker._WORKSPACES._busy == set()
