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

    def __init__(self, payload):
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

    def query(self, request):
        sql = request.content.decode().replace(" FINAL", "").replace("FORMAT JSON", "")
        sql = re.sub(r"parseDateTime64BestEffort\((\{\w+:String\}), 3\)", r"\1", sql)
        sql = re.sub(r"\{(\w+):\w+\}", r":\1", sql)
        params = {k.removeprefix("param_"): v for k, v in request.url.params.items()}
        rows = [dict(row) for row in self.connection.execute(sql, params)]
        for row in rows:
            for key in self.compound & row.keys():
                row[key] = json.loads(row[key])
        return httpx.Response(200, json={"data": rows})


def consumer_snapshots(monkeypatch):
    from clickhouse import ingest_operational_outbox as worker
    from clickhouse.archive_daily import ClickHouseDailyExporter, SourceFingerprint, archive_day
    from clickhouse.build_public_snapshots import build_snapshots
    from clickhouse.operational_fingerprint import canonical_fingerprint
    from clickhouse.rollup_client_events import aggregate_client_rollups
    from clickhouse.verify_spanner_delivery import verify_delivery
    from tests.test_clickhouse_archive import FakeExporter, MemoryStore
    from tests.test_clickhouse_client_rollup import _counter, _coverage
    from trusted_router.client_reliability import _all_traffic_section, _status_all_traffic
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

    now = dt.datetime.now(dt.UTC)
    generation = _generation()
    generation.id = "gen-" + "e" * 32
    generation.synthetic = True
    generation.app = "TrustedRouter Synthetic"
    generation.workspace_id = "dedicated-probe"
    window_start = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    generation.created_at = window_start.isoformat()
    payload = activity_payload(generation)
    cfg = Settings(environment="test")
    database = ActivityDatabase(payload)
    store = InMemoryStore()
    from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample

    ordinary = _generation()
    ordinary.created_at = generation.created_at
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

    monkeypatch.setattr(storage, "STORE", store)
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
        public = build_snapshots(
            benchmark_rows, generated_at=now.isoformat(), status_samples=samples
        )
        cov = [_coverage("ordinary", requests=2)]
        cov += [
            _coverage(activity_payload(g)["tenant_id"], requests=1, synthetic=int(g.synthetic))
            for g in generations
        ]
        for coverage in cov:
            coverage["bucket_start"] = window_start.isoformat()
        client = aggregate_client_rollups(
            [
                _counter(
                    "ordinary", requests=1, first_attempt_success=1, bucket_start=window_start.isoformat(), total_ms_hist={"lt200": 1}, first_event_ms_hist={"lt100": 1}
                )
            ],
            cov,
            period="5m",
            period_start=window_start,
            computed_at=now,
        )
        all_traffic = _status_all_traffic(
            _all_traffic_section({"24h": [r for r in client if r["scope"] == "fleet_all"]})
        )
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
                lambda match: "'" + match[1].replace(" ", "T") + "'",
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
            "public analytics freshness": analytics_status_from_reading(
                operational.operational_analytics_outbox_freshness(), now=now
            ),
            "archive": archived.rows,
            "delivery repair": repairs.durable_repaired,
            "delivery reconciliation and fingerprints": (verified, hashes),
            "retirement discovery": notices.run_retirement_notice_pass(
                notice_settings, now=now, analytics=database.client, email_service=notice_email
            ).workspaces,
            "all-traffic coverage": all_traffic,
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
