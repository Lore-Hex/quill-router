"""Local consumer experiment: real projections, in-memory database/HTTP I/O."""

# ruff: noqa: S608 - local SQLite executes fixed production query syntax on fixtures.
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import re
import sqlite3
from types import SimpleNamespace

import httpx

from tests.test_operational_analytics import _generation
from trusted_router.config import Settings
from trusted_router.storage import InMemoryStore
from trusted_router.storage_operational_analytics import activity_payload

# One table feeds the regression and the PR report. A generation is the only
# additional input; no synthetic samples or SDK counter batches are invented.
CONSUMERS = {
    "generation records and lookup": "changes",
    "dedicated workspace activity": "changes",
    "dedicated workspace usage": "changes",
    "other workspace activity": "unchanged",
    "operational outbox": "changes",
    "delivery heartbeat": "changes",
    "public analytics freshness": "changes",
    "archive": "changes",
    "delivery repair": "changes",
    "delivery reconciliation and fingerprints": "changes",
    "retirement discovery": "changes",
    "all-traffic coverage": "changes",
    "fleet freshness/release input": "changes",
    "fleet freshness/release decision": "unchanged",
    "client telemetry evaluator input": "changes",
    "client telemetry freshness fields": "unchanged",
    "client telemetry canary fields": "unchanged",
    "client telemetry freshness/canary decision": "unchanged",
    "calibration publication inputs": "unchanged",
    "calibration report (without attempt join)": "unchanged",
    "calibration publication decision": "unchanged",
    "synthetic samples": "unchanged",
    "status components": "unchanged",
    "history": "unchanged",
    "SLOs": "unchanged",
    "burn rates": "unchanged",
    "failure streaks": "unchanged",
    "remediator": "unchanged",
    "provider benchmarks": "unchanged",
    "provider analytics": "unchanged",
    "rankings": "unchanged",
    "leaderboards": "unchanged",
    "route health": "unchanged",
    "drift": "unchanged",
    "apps directory": "unchanged",
    "client-event rollups": "unchanged",
    "primary fleet coverage": "unchanged",
    "client watch": "unchanged",
}


class ActivityDatabase:
    """Execute the shipped tenant/retirement SELECTs with SQLite I/O.

    Translate only ClickHouse placeholder, time and FINAL/FORMAT syntax.
    Compound columns round-trip as JSON; predicates and limits remain real SQL.
    """

    def __init__(self, payload, query_log):
        self.query_log = query_log
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.compound = {k for k, value in payload.items() if isinstance(value, (dict, list))}
        self.connection.execute(
            "CREATE TABLE activity_generations (" + ", ".join('"' + k + '"' for k in payload) + ")"
        )
        from trusted_router.storage_models import ProviderBenchmarkSample

        self.benchmark_fields = [
            field.name for field in dataclasses.fields(ProviderBenchmarkSample)
        ]
        self.connection.execute(
            "CREATE TABLE provider_benchmark_samples ("
            + ", ".join('"' + k + '"' for k in self.benchmark_fields)
            + ")"
        )
        self.fields = list(payload)
        self.client = __import__(
            "trusted_router.operational_analytics", fromlist=["OperationalAnalyticsClient"]
        ).OperationalAnalyticsClient(
            base_url="http://clickhouse.test",
            user="fixture",
            password="",
            transport=httpx.MockTransport(self.query),
        )

    def add(self, payload):
        self.connection.execute(
            "INSERT INTO activity_generations VALUES (" + ",".join("?" for _ in self.fields) + ")",  # noqa: S608 - placeholders over fixed activity columns
            [json.dumps(payload[k]) if k in self.compound else payload[k] for k in self.fields],
        )

    def replace_benchmarks(self, samples):
        self.connection.execute("DELETE FROM provider_benchmark_samples")
        for sample in samples:
            row = dataclasses.asdict(sample)
            self.connection.execute(
                "INSERT INTO provider_benchmark_samples VALUES ("
                + ",".join("?" for _ in self.benchmark_fields)
                + ")",  # noqa: S608 - fixed column placeholders
                [
                    json.dumps(row[k]) if isinstance(row[k], (list, dict)) else row[k]
                    for k in self.benchmark_fields
                ],
            )

    def replace_table(self, name, rows):
        """Raw source fixture or actual evaluator output, never invented coverage."""
        self.connection.execute(f'DROP TABLE IF EXISTS {name}')
        self.connection.execute(
            f'CREATE TABLE {name} (' + ', '.join('"' + k + '"' for k in rows[0]) + ')'
        )
        for row in rows:
            self.connection.execute(
                f'INSERT INTO {name} VALUES (' + ','.join('?' for _ in row) + ')',
                [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()],
            )

    def select(self, sql, params=None):
        self.query_log.append(sql)
        sql = sql.replace(" FINAL", "").replace("FORMAT JSONEachRow", "").replace("FORMAT JSON", "")
        sql = re.sub(r"\btr\.", "", sql)
        sql = re.sub(r"parseDateTime64BestEffort\((\{\w+:String\}), 3\)", r"\1", sql)
        sql = re.sub(r"\{(\w+):\w+\}", r":\1", sql)
        sql = re.sub(r"toDateTime(?:64)?\('([^']+)'(?:, 3)?, 'UTC'\)",
                     lambda m: "'" + dt.datetime.fromisoformat(m[1]).replace(tzinfo=dt.UTC).isoformat() + "'", sql)
        sql = sql.replace("toDateTime64(bucket_start, 3, 'UTC') + INTERVAL 6 HOUR",
                          "shift_hours(bucket_start, 6)")
        sql = re.sub(r"now\(\) - INTERVAL (\d+) HOUR", r"shift_hours(now(), -\1)", sql)
        # SELECT * EXCEPT is column projection only; no predicate rewriting.
        match = re.search(r"SELECT \* EXCEPT (\([^)]+\)|\w+)\s+FROM (\w+)", sql)
        if match:
            excluded = {x.strip() for x in match[1].strip('()').split(',')}
            fields = [r[1] for r in self.connection.execute(f'PRAGMA table_info({match[2]})')]
            sql = sql[:match.start()] + 'SELECT ' + ', '.join('"' + k + '"' for k in fields if k not in excluded) + ' FROM ' + match[2] + sql[match.end():]
        rows = [dict(row) for row in self.connection.execute(sql, params or {})]
        for row in rows:
            for key, value in row.items():
                if key in self.compound or key.endswith('_hist'):
                    row[key] = json.loads(value)
        return rows

    def query_bytes(self, sql):
        return '\n'.join(json.dumps(row) for row in self.select(sql)).encode()

    def query(self, request):
        params = {k.removeprefix("param_"): v for k, v in request.url.params.items()}
        return httpx.Response(200, json={"data": self.select(request.content.decode(), params)})


def consumer_snapshots(monkeypatch, query_log, *, now=None):
    from clickhouse import build_public_snapshots as public_builder
    from clickhouse import calibrate_client_availability as calibration
    from clickhouse import ingest_operational_outbox as worker
    from clickhouse.archive_daily import ClickHouseDailyExporter, SourceFingerprint, archive_day
    from clickhouse.build_public_snapshots import build_snapshots
    from clickhouse.check_client_telemetry_freshness import evaluate as evaluate_telemetry
    from clickhouse.check_fleet_analytics_freshness import evaluate as evaluate_fleet
    from clickhouse.operational_fingerprint import canonical_fingerprint
    from clickhouse.rollup_client_events import aggregate_client_rollups, fetch_inputs
    from clickhouse.verify_spanner_delivery import verify_delivery
    from tests.test_clickhouse_archive import FakeExporter, MemoryStore
    from tests.test_clickhouse_client_rollup import _counter
    from trusted_router.client_reliability import client_observed_status_section
    from trusted_router.operational_analytics_freshness import analytics_status_from_reading
    from trusted_router.provider_ranking import build_ranks
    from trusted_router.storage_gcp import SpannerStore
    from trusted_router.storage_gcp_generations import SpannerGenerations
    from trusted_router.storage_gcp_operational_analytics_outbox import (
        SpannerOperationalAnalyticsOutbox,
    )
    from trusted_router.synthetic import alerts, drift, remediator, status
    from trusted_router.synthetic.client_watch import evaluate_client_watch
    from trusted_router.synthetic.route_health import evaluate_route_health

    now = now or dt.datetime.now(dt.UTC)
    from trusted_router import storage_gcp

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    now = Clock.fromisoformat(now.isoformat())
    # Freeze only this module's clock, not datetime shared by other consumers.
    monkeypatch.setattr(storage_gcp, "dt", SimpleNamespace(**(vars(dt) | {"datetime": Clock})))
    generation = _generation()
    generation.id = "gen-" + "e" * 32
    generation.synthetic = True
    generation.client_source = "tr"
    generation.app = "TrustedRouter Synthetic"
    generation.workspace_id = "dedicated-probe"
    window_start = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    generation.created_at = window_start.isoformat()
    payload = activity_payload(generation)
    cfg = Settings(environment="test")
    database = ActivityDatabase(payload, query_log)
    class CountIf:
        def __init__(self):
            self.total = 0

        def step(self, value):
            self.total += bool(value)

        def finalize(self):
            return self.total

    class MaxIf:
        def __init__(self):
            self.values = []

        def step(self, value, condition):
            if condition:
                self.values.append(value)

        def finalize(self):
            return max(self.values) if self.values else None

    database.connection.create_aggregate("countIf", 1, CountIf)
    database.connection.create_aggregate("maxIf", 2, MaxIf)
    database.connection.create_function("now", 0, lambda: now.isoformat())
    database.connection.create_function("shift_hours", 2, lambda text, hours: (
        dt.datetime.fromisoformat(text) + dt.timedelta(hours=hours)).isoformat())
    for func, minutes in (("toStartOfMinute", 1), ("toStartOfFiveMinutes", 5),
                          ("toStartOfHour", 60), ("toStartOfDay", 1440)):
        def floor(text, minutes=minutes):
            timestamp = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
            midnight = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            return (midnight + dt.timedelta(minutes=((timestamp.hour * 60 + timestamp.minute) // minutes) * minutes)).isoformat()
        database.connection.create_function(func, 1, floor)
    # Raw client source fixtures are identical before/after. No cohort SDK
    # telemetry is invented. Both shipped rollup SELECTs execute locally.
    database.replace_table("client_minute_counters", [_counter(
        activity_payload(_generation())["tenant_id"], requests=1, first_attempt_success=1,
        bucket_start=window_start.isoformat(), received_at=window_start.isoformat(),
        event_id="ordinary-event", ingest_version=1, sdk_version="1.0",
        total_ms_hist={"lt200": 1}, first_event_ms_hist={"lt100": 1})])
    database.connection.execute("CREATE TABLE client_request_events (created_at, synthetic)")
    monkeypatch.setattr(public_builder, "_query", lambda password, sql: database.query_bytes(sql).decode())
    store = InMemoryStore()
    from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample

    ordinary = _generation()
    ordinary.created_at = generation.created_at
    ordinary.client_source = "tr"
    ordinary_payload = activity_payload(ordinary)
    # Two real baseline activity rows establish the coverage denominator.
    database.add(ordinary_payload)
    database.add(dict(ordinary_payload, generation_id="ordinary-second"))
    store.generation_store.add(ordinary)
    store.record_synthetic_probe_sample(
        SyntheticProbeSample(
            id="ordinary-monitor",
            probe_type="gateway_authorize",
            target="canonical",
            target_url="https://fixture.invalid",
            monitor_region="us-central1",
            status="down",
            created_at=generation.created_at,
        )
    )
    # Module attributes, never STORE proxy methods. No global store pollution.
    import trusted_router.storage as storage
    from trusted_router import storage_models

    monkeypatch.setattr(storage, "STORE", store)
    monkeypatch.setattr(storage_models, "utcnow", lambda: now)
    monkeypatch.setattr(status, "utcnow", lambda: now)
    monkeypatch.setattr(status, "iso_now", lambda: now.isoformat())
    monkeypatch.setattr(status, "get_settings", lambda: cfg)
    monkeypatch.setattr(alerts, "STREAK_THRESHOLD", 3)
    monkeypatch.setattr(remediator, "_detect_stale_heartbeats", lambda _: [])
    monkeypatch.setattr(remediator, "_detect_route_quarantine", lambda _: [])
    # The experiment uses the cited sample detectors, not cloud remedial actions.
    operational = object.__new__(SpannerStore)
    operational._operational_analytics = database.client
    operational._operational_analytics_outbox = object()
    rows, bodies, delivered = [], [], []
    benchmarks = [ProviderBenchmarkSample.from_generation(ordinary)]
    from trusted_router import provider_lifecycle
    from trusted_router.services import retirement_notices as notices

    monkeypatch.setattr(notices, "STORE", store)
    monkeypatch.setattr(
        provider_lifecycle,
        "_RETIREMENTS",
        (
            provider_lifecycle._Retirement(
                generation.provider,
                frozenset({generation.model}),
                frozenset(),
                now + dt.timedelta(days=14),
            ),
        ),
    )
    notice_settings = Settings(environment="test", retirement_notices_mode="send")

    def no_send(message):
        raise AssertionError("unverified cohort must not receive a notice")

    notice_email = SimpleNamespace(can_receive_product_email=lambda _: True, send=no_send)
    source = SimpleNamespace(
        fetch=lambda **kw: list(rows),
        delete=lambda batch: rows.clear(),
        oldest_commit_ts=lambda: None,
        publish_heartbeat=bodies.append,
    )
    publisher = worker.OutboxHeartbeatPublisher(source, min_interval_seconds=0, now=lambda: now)
    operational._read_outbox_heartbeat_row = lambda: (bodies[-1], now)
    writer = SimpleNamespace(insert=delivered.extend)
    outbox = SpannerOperationalAnalyticsOutbox(
        None, SimpleNamespace(INT64="INT64", STRING="STRING")
    )
    entities = {}

    class IO:
        database = SimpleNamespace(run_in_transaction=lambda fn: fn(None))

        def write_entity_tx(self, tx, kind, key, body):
            entities[kind, key] = body

        def read_entity(self, kind, key, cls):
            return entities.get((kind, key))

    def enqueue(tx, item):
        _, params, _ = outbox.activity_insert_statement(item)
        rows.append(worker.OperationalOutboxRow(commit_ts=now, **params))

    adapter = SpannerGenerations(
        IO(),
        add_usage_to_key=lambda *a, **k: None,
        analytics_outbox=SimpleNamespace(enqueue=benchmarks.append),
        operational_analytics_outbox=SimpleNamespace(enqueue_activity_tx=enqueue),
    )
    generations = []
    archive_store = MemoryStore()

    def snapshot():
        queued = [dataclasses.asdict(row) for row in rows]
        publisher.after_pass(worker.drain_once(source, writer, batch_size=200))
        samples = store.synthetic_probe_samples(limit=35000)
        status_result = status.status_snapshot(samples, now=now, settings=cfg)
        database.replace_benchmarks(benchmarks)
        benchmark_rows = database.client.benchmark_samples()
        counters, cov = fetch_inputs(SimpleNamespace(query=database.query_bytes), start=window_start, end=now + dt.timedelta(seconds=1))
        client = aggregate_client_rollups(
            counters, cov, period="5m", period_start=window_start, computed_at=now,
        )
        client += aggregate_client_rollups(
            counters, cov, period="hour", period_start=window_start.replace(minute=0), computed_at=now,
        )
        database.replace_table("client_availability_rollups", client)
        public = build_snapshots(
            benchmark_rows, generated_at=now.isoformat(), status_samples=samples,
            client_reliability_rows=public_builder._client_reliability_rows("", now=now),
            client_reliability_all_traffic_rows=public_builder._client_reliability_rows("", now=now, scope="fleet_all"),
            client_reliability_signals=public_builder._client_reliability_signals("", now=now),
        )
        client_section = client_observed_status_section(public["client_reliability"], now=now)
        analytics = analytics_status_from_reading(operational.operational_analytics_outbox_freshness(), now=now)
        start = window_start.replace(hour=0, minute=0)
        end = start + dt.timedelta(days=1)
        # Shipped activity/counter/canary queries run. The ClickHouse ARRAY JOIN
        # is not executed here: the report's attempt-join output is out of scope.
        # Synthetic input comes from the real local sample/rollup store, not a
        # manufactured exclusion; the remote synthetic SELECT is not exercised.
        cal_inputs = {
            "counter_rows": database.select(calibration.counter_query(start=start, end=end)),
            "activity_rows": database.select(calibration.activity_success_query(start=start, end=end)),
            "canary_rows": database.select(calibration.canary_query(start=start, end=end)),
            "synthetic_rows": [dataclasses.asdict(r) for r in store.synthetic_rollups(period="5m")],
        }
        cal_report = calibration.build_calibration_report(**cal_inputs, attempt_rows=[], start=start, end=end)
        verdict = calibration.build_publication_verdict(**cal_inputs, start=start, end=end)
        assert verdict == cal_report["publication"]
        # Archive I/O is fake; canonical payload, fingerprints and archive planner are real.
        hashes = [
            int(canonical_fingerprint(event.row, surface="activity")[:16], 16)
            for event in delivered
        ]
        reader = ClickHouseDailyExporter(password="", table="activity_generations")

        def archive_query(sql):
            assert "FROM tr.activity_generations FINAL" in sql
            where = sql.split(" WHERE ", 1)[1].removesuffix(" FORMAT JSONEachRow")
            where = re.sub(
                r"toDateTime64\('([^']+)', 3, 'UTC'\)",
                lambda match: "'" + dt.datetime.fromisoformat(match[1]).isoformat() + "'",
                where,
            )
            selected = list(
                database.connection.execute("SELECT * FROM activity_generations WHERE " + where)
            )  # noqa: S608 - execute production predicates on local fixtures
            selected_hashes = [
                int(canonical_fingerprint(dict(row), surface="activity")[:16], 16)
                for row in selected
            ]
            times = [row["created_at"] for row in selected]
            value = SourceFingerprint(
                len(selected),
                sum(selected_hashes) % (1 << 64),
                __import__("functools").reduce(int.__xor__, selected_hashes, 0),
                min(times) if times else None,
                max(times) if times else None,
            )
            return json.dumps(value.as_dict()).encode()

        reader._client = archive_query
        exporter = FakeExporter(reader.source_fingerprint(now.date()))
        exporter.source_fingerprint = reader.source_fingerprint
        archived = archive_day(
            exporter,
            archive_store,
            now.date(),
            now=now + dt.timedelta(days=1),
            dataset="activity_generations",
        )
        stored_rows = SimpleNamespace(
            query=lambda *a, **k: "\n".join(json.dumps(e.row) for e in delivered)
        )
        verified = verify_delivery(
            SimpleNamespace(fetch=lambda **kw: list(generations)),
            stored_rows,
            start=now - dt.timedelta(days=1),
            end=now,
            limit=20,
        )
        repairs = adapter.reconcile_activity(generation_id=generation.id, detailed=True)
        rows.clear()  # Repair is inspected separately, not delivered a second time.
        activity = dict(
            api_key_hash=None, date=None, tag_key=None, tag_value=None, group_by_tag=None
        )
        return {
            "generation records and lookup": adapter.get(generation.id),
            "dedicated workspace activity": operational._clickhouse_activity_result(
                generation.workspace_id, **activity
            ),
            "other workspace activity": operational._clickhouse_activity_result(
                "other", **activity
            ),
            "dedicated workspace usage": operational._clickhouse_usage_series(
                generation.workspace_id,
                start_day=now.date().isoformat(),
                end_day=now.date().isoformat(),
                granularity="hour",
                api_key_hash=None,
                by_model=True,
                min_created_at=None,
            ),
            "operational outbox": queued,
            "delivery heartbeat": json.loads(bodies[-1])["last_delivery_at"],
            "public analytics freshness": analytics,
            "fleet freshness/release input": {"analytics": analytics},
            "fleet freshness/release decision": evaluate_fleet({"analytics": analytics}, now=now),
            "client telemetry evaluator input": {"client_observed": client_section},
            "client telemetry freshness fields": public["client_reliability"]["freshness"],
            "client telemetry canary fields": client_section["canary"],
            "client telemetry freshness/canary decision": evaluate_telemetry(
                {"client_observed": client_section}, now=now, max_age_seconds=3600,
                max_canary_age_seconds=3600, min_canary_24h=200),
            "calibration publication inputs": cal_inputs,
            "calibration report (without attempt join)": {k: v for k, v in cal_report.items() if k != "gateway_request_id_join"},
            "calibration publication decision": verdict,
            "archive": archived.rows,
            "delivery repair": repairs.durable_repaired,
            "delivery reconciliation and fingerprints": (verified, hashes),
            "retirement discovery": notices.run_retirement_notice_pass(
                notice_settings, now=now, analytics=database.client, email_service=notice_email
            ).workspaces,
            "all-traffic coverage": client_section["all_traffic"],
            "synthetic samples": samples,
            "status components": status_result["components"],
            "history": status.history_payload(samples, "daily"),
            "SLOs": status_result["slo_classes"],
            "burn rates": status_result["burn_rate_alerts"],
            "failure streaks": alerts.alert_on_failure_streak(
                store,
                SimpleNamespace(status="down", probe_type="gateway_authorize", target="canonical"),
            ),
            "remediator": remediator._detect_monitor_stale(cfg)
            + remediator._detect_transaction_probe_stale(cfg),
            "provider benchmarks": benchmarks,
            "provider analytics": benchmark_rows,
            "rankings": build_ranks(
                [
                    dict(
                        row,
                        samples=row["sample_count"],
                        throughput_samples=row["throughput_sample_count"],
                        tokens_per_second=row["p50_tokens_per_second"],
                    )
                    for row in public["leaderboard"]["providers"]
                ]
            ),
            "leaderboards": public["leaderboard"],
            "route health": evaluate_route_health(
                store, routes=[(generation.provider, generation.model)]
            ),
            "drift": (
                drift.aggregate(benchmark_rows),
                drift.detect_drift(drift.aggregate(benchmark_rows), {}),
            ),
            "apps directory": public["apps"],
            "client-event rollups": [r for r in client if r["scope"] == "tenant"],
            "primary fleet coverage": [r for r in client if r["scope"] == "fleet"],
            "client watch": evaluate_client_watch(
                public["client_reliability"], router_core_up=True, now=now
            ),
        }

    try:
        before = copy.deepcopy(snapshot())
        adapter.add(generation)
        store.generation_store.add(generation)
        generations.append(generation)
        database.add(payload)
        after = snapshot()
        return before, after
    finally:
        database.connection.close()
