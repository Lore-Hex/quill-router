"""The direct ClickHouse sink: canonical parity, bounded loss, honest retries.

The sink exists to take operational telemetry OFF the billing database (the
outbox drain measured ~25% of the production Spanner instance while idle,
2026-08-25). Its contracts:

  * rows are byte-identical to what the outbox drainer produced (the
    canonicalisation is extracted verbatim; the parity test here is what
    keeps the frozen drainer copy honest until it is deleted),
  * the buffer is bounded and drops OLDEST with a count -- never blocks a
    request thread, never grows without limit,
  * a failed flush retains rows for the next attempt; nothing is silently
    lost while ClickHouse is down until the bound forces counted drops.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trusted_router.config import operational_analytics_sink_problems
from trusted_router.operational_analytics_direct import (
    DirectOperationalAnalyticsSink,
    OperationalOutboxRow,
    normalise_operational_event,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_drainer():
    spec = importlib.util.spec_from_file_location(
        "ingest_operational_outbox",
        REPO_ROOT / "clickhouse" / "ingest_operational_outbox.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNTHETIC_PAYLOAD = {
    "id": "sample-1",
    "probe_type": "tls_health",
    "target": "canonical",
    "target_url": "https://api.trustedrouter.com/health",
    "monitor_region": "us-central1",
    "status": "up",
    "target_region": "us-central1",
    "latency_milliseconds": 42,
    "ttfb_milliseconds": 12,
    "dns_milliseconds": 1,
    "tcp_connect_milliseconds": 2,
    "tls_handshake_milliseconds": 3,
    "gateway_processing_milliseconds": 4,
    "connection_reused": 1,
    "protocol": "h2",
    "http_status": 200,
    "error_type": "",
    "provider": "",
    "model": "",
    "selected_provider": "",
    "selected_model": "",
    "generation_id": "",
    "attestation_digest": "",
    "source_commit": "abc1234",
    "cost_microdollars": 0,
    "output_match": 1,
    "created_at": "2026-08-25T22:00:00+00:00",
}

COMMIT_TS = dt.datetime(2026, 8, 25, 22, 0, 5, tzinfo=dt.UTC)


def _row(kind: str, payload: dict) -> OperationalOutboxRow:
    return OperationalOutboxRow(
        shard=0,
        commit_ts=COMMIT_TS,
        event_kind=kind,
        event_id="evt-1",
        payload=json.dumps(payload),
    )


class TestCanonicalParityWithTheFrozenDrainer:
    """The extraction and the drainer must emit identical rows until the
    drainer is deleted. If this fails, one copy was edited without the
    other -- fix the drift, do not relax the comparison."""

    def test_synthetic_rows_identical(self) -> None:
        drainer = _load_drainer()
        ours = normalise_operational_event(_row("synthetic", SYNTHETIC_PAYLOAD))
        theirs = drainer.normalise_operational_event(
            drainer.OperationalOutboxRow(
                shard=0,
                commit_ts=COMMIT_TS,
                event_kind="synthetic",
                event_id="evt-1",
                payload=json.dumps(SYNTHETIC_PAYLOAD),
            )
        )
        assert [(e.event_kind, e.row) for e in ours] == [(e.event_kind, e.row) for e in theirs]

    def test_bad_payload_raises_in_both(self) -> None:
        drainer = _load_drainer()
        broken = dict(SYNTHETIC_PAYLOAD)
        del broken["status"]
        with pytest.raises(ValueError):
            normalise_operational_event(_row("synthetic", broken))
        with pytest.raises(ValueError):
            drainer.normalise_operational_event(
                drainer.OperationalOutboxRow(
                    shard=0,
                    commit_ts=COMMIT_TS,
                    event_kind="synthetic",
                    event_id="evt-1",
                    payload=json.dumps(broken),
                )
            )


class _CapturingPost:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.fail_times = fail_times

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("clickhouse down")
        self.calls.append((url, body))


def _sink(post, **kwargs) -> DirectOperationalAnalyticsSink:
    return DirectOperationalAnalyticsSink(
        url="http://clickhouse.internal:8123",
        database="tr",
        user="tr",
        password="pw",  # noqa: S106
        post=post,
        start_thread=False,
        **kwargs,
    )


class TestSinkDelivery:
    def test_flush_groups_rows_into_the_right_table_as_jsoneachrow(self) -> None:
        post = _CapturingPost()
        sink = _sink(post)
        sink._publish("synthetic", "evt-1", SYNTHETIC_PAYLOAD)
        assert sink.flush() == 1
        (url, body) = post.calls[0]
        assert "INSERT%20INTO%20tr.synthetic_probe_samples" in url
        parsed = [json.loads(line) for line in body.decode().splitlines()]
        assert parsed[0]["id"] == "sample-1"
        assert sink.stats.inserted == 1

    def test_failed_flush_retains_rows_and_the_next_attempt_delivers(self) -> None:
        post = _CapturingPost(fail_times=1)
        sink = _sink(post)
        sink._publish("synthetic", "evt-1", SYNTHETIC_PAYLOAD)
        with pytest.raises(OSError):
            sink.flush()
        assert sink.stats.inserted == 0
        assert sink.flush() == 1
        assert sink.stats.inserted == 1

    def test_overflow_drops_oldest_and_counts(self) -> None:
        post = _CapturingPost()
        sink = _sink(post, buffer_rows=2)
        for index in range(4):
            payload = dict(SYNTHETIC_PAYLOAD, id=f"sample-{index}")
            sink._publish("synthetic", f"evt-{index}", payload)
        assert sink.stats.dropped == 2
        assert sink.flush() == 2
        rows = [json.loads(line) for _, body in post.calls for line in body.decode().splitlines()]
        # newest retained, oldest gone
        assert [row["id"] for row in rows] == ["sample-2", "sample-3"]

    def test_unparseable_payload_is_quarantined_not_lost(self) -> None:
        post = _CapturingPost()
        sink = _sink(post)
        sink._publish("synthetic", "evt-bad", {"id": "x"})
        assert sink.stats.quarantined == 1
        assert sink.flush() == 1
        (url, _) = post.calls[0]
        assert "operational_outbox_quarantine" in url

    def test_duck_type_surface_matches_the_outbox_writer(self) -> None:
        sink = _sink(_CapturingPost())
        # _tx variants ignore the transaction handle by design (documented
        # dup/phantom trade); freshness reads as empty.
        assert sink.oldest_enqueued_at() is None
        assert sink.oldest_enqueued_at(timeout=1.0) is None
        for name in (
            "enqueue_activity",
            "enqueue_activity_tx",
            "enqueue_synthetic",
            "enqueue_synthetic_tx",
            "enqueue_client_events",
        ):
            assert callable(getattr(sink, name))


def _stub(**overrides) -> SimpleNamespace:
    base = dict(
        operational_analytics_sink="outbox",
        operational_analytics_outbox_enabled=True,
        operational_analytics_clickhouse_url="http://ch:8123",
        operational_analytics_clickhouse_write_password="pw",  # noqa: S106
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestConfigValidation:
    def test_direct_without_write_password_is_refused(self) -> None:
        problems = " ".join(
            operational_analytics_sink_problems(
                _stub(
                    operational_analytics_sink="direct",
                    operational_analytics_clickhouse_write_password="",
                )
            )
        )
        assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_WRITE_PASSWORD" in problems

    def test_direct_satisfies_the_outbox_requirement(self) -> None:
        problems = operational_analytics_sink_problems(
            _stub(
                operational_analytics_sink="direct",
                operational_analytics_outbox_enabled=False,
            )
        )
        assert problems == []

    def test_outbox_disabled_without_direct_is_refused(self) -> None:
        problems = " ".join(
            operational_analytics_sink_problems(_stub(operational_analytics_outbox_enabled=False))
        )
        assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in problems

    def test_unknown_sink_value_is_refused(self) -> None:
        problems = " ".join(
            operational_analytics_sink_problems(_stub(operational_analytics_sink="carrier-pigeon"))
        )
        assert "TR_OPERATIONAL_ANALYTICS_SINK" in problems
