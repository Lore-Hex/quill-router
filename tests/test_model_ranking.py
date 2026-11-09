from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from clickhouse import build_public_snapshots as worker
from trusted_router import model_ranking as ranking
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)


def row(provider, *, model="unit/workload", bucket=0, ttft=1000, tps=100000):
    return dict(model=model, provider=provider, input_bucket=bucket, ttft_samples=30,
                ttft_ms=ttft, throughput_samples=30, tps_milli=tps)


def payload(*rows, at=NOW):
    return dict(version=1, window="7d", generated_at=at.isoformat(), routes=list(rows))


def install(monkeypatch, *rows):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(ranking, "datetime", Clock)
    monkeypatch.setattr(ranking, "_RANKS", ranking.compile_ranks(payload(*rows)))


def candidates(monkeypatch, *, zdr=("tinfoil",), health=None):
    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    model = replace(MODELS[base.model_id], id="unit/workload", supports_chat=True)
    items = []
    for provider in ("akashml", "tinfoil"):
        monkeypatch.setitem(PROVIDERS, provider, replace(
            PROVIDERS[provider], stores_content=provider not in zdr,
            provider_zero_data_retention=provider in zdr,
            provider_confidential_compute=False, provider_e2ee=False,
        ))
        items.append((model, replace(base, model_id=model.id, id=provider, provider=provider,
                                    prompt_price_microdollars_per_million_tokens=100 if provider == "akashml" else 200,
                                    completion_price_microdollars_per_million_tokens=100)))
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank",
                        lambda provider, sort: (health or {}).get(provider, 0))
    return items


def first(items, **kwargs):
    return _sort_endpoint_candidates(items, RoutePreferences(**kwargs))[0][1].provider


@pytest.mark.parametrize("tokens,expected", [(0, 0), (4096, 0), (4097, 1),
    (32768, 1), (32769, 2), (131072, 2), (131073, 3)])
def test_prompt_boundaries(tokens, expected):
    assert ranking.bucket(tokens, ranking.INPUT_LIMITS) == expected


def test_same_model_different_workload_can_change_winner(monkeypatch):
    items = candidates(monkeypatch, zdr=())
    install(monkeypatch, row("akashml", ttft=500), row("tinfoil", ttft=3000),
            row("akashml", bucket=3, ttft=30000), row("tinfoil", bucket=3, ttft=2000))
    assert first(items, estimated_input_tokens=100) == "akashml"
    assert first(items, estimated_input_tokens=150000) == "tinfoil"


def test_output_size_uses_sustained_throughput(monkeypatch):
    items = candidates(monkeypatch, zdr=())
    install(monkeypatch, row("akashml", ttft=500, tps=50000),
            row("tinfoil", ttft=4000, tps=500000))
    assert first(items, estimated_input_tokens=100, estimated_output_tokens=128) == "akashml"
    assert first(items, estimated_input_tokens=100, estimated_output_tokens=16000) == "tinfoil"


def test_zdr_breaks_comparable_performance_tie(monkeypatch):
    items = candidates(monkeypatch)
    install(monkeypatch, row("akashml", ttft=1000), row("tinfoil", ttft=1100))
    assert first(items, estimated_input_tokens=100) == "tinfoil"
    assert first(items, estimated_input_tokens=100, sort="latency") == "akashml"
    assert first(items, estimated_input_tokens=100, sort="price") == "akashml"
    assert first(items, estimated_input_tokens=100, order=("akashml",)) == "akashml"


def test_zdr_does_not_override_large_performance_gap_or_health(monkeypatch):
    items = candidates(monkeypatch)
    install(monkeypatch, row("akashml", ttft=500), row("tinfoil", ttft=30000))
    assert first(items, estimated_input_tokens=100) == "akashml"
    items = candidates(monkeypatch, health={"tinfoil": 2000})
    install(monkeypatch, row("akashml", ttft=1000), row("tinfoil", ttft=900))
    assert first(items, estimated_input_tokens=100) == "akashml"


def test_other_models_never_supply_speed_prior(monkeypatch):
    items = candidates(monkeypatch)
    install(monkeypatch, row("akashml", model="other/model", ttft=1))
    assert first(items, estimated_input_tokens=100) == "tinfoil"
    assert ranking.workload_ranks("unit/workload", 100, 512, now=NOW) == {}


def test_unknown_sizes_and_stale_evidence_are_neutral(monkeypatch):
    install(monkeypatch, row("akashml"))
    assert ranking.workload_ranks("unit/workload", None, 512, now=NOW) == {}
    for at in (NOW - timedelta(microseconds=1), NOW + timedelta(days=7, microseconds=1)):
        assert ranking.workload_ranks("unit/workload", 10, 512, now=at) == {}
    assert ranking.workload_ranks("unit/workload", 10, 512, now=NOW)


def test_anchor_hysteresis_accumulates_against_published_measurement():
    previous = payload(row("akashml", ttft=1000))
    small = payload(row("akashml", ttft=1150), at=NOW + timedelta(days=1))
    retained = ranking.stabilize(small, previous)
    assert retained["routes"][0]["ttft_ms"] == 1000
    moved = ranking.stabilize(payload(row("akashml", ttft=1400), at=NOW + timedelta(days=2)), retained)
    assert moved["routes"][0]["ttft_ms"] == 1400
    assert small["routes"][0]["ttft_ms"] == 1150
    assert ranking.stabilize(payload(row("akashml", ttft=1150), at=NOW + timedelta(days=8)), previous)["routes"][0]["ttft_ms"] == 1150


def test_small_latency_crossovers_remain_a_tie():
    before = payload(row("akashml", ttft=1000), row("tinfoil", ttft=1050))
    after = payload(row("akashml", ttft=1100), row("tinfoil", ttft=980), at=NOW + timedelta(days=1))
    assert ranking.compile_ranks(before)["workloads"] == ranking.compile_ranks(ranking.stabilize(after, before))["workloads"]


@pytest.mark.parametrize("field,value", [("ttft_samples", 24), ("ttft_samples", True),
    ("input_bucket", 4), ("ttft_ms", -1), ("ttft_ms", float("nan")), ("ttft_ms", None),
    ("tps_milli", float("inf")), ("throughput_samples", 0), ("model", "")])
def test_bad_or_insufficient_evidence_rejected(field, value):
    item = row("akashml")
    item[field] = value
    with pytest.raises(ValueError):
        ranking.validate_evidence(payload(item))


def test_no_user_fields_or_duplicate_cells_allowed():
    with pytest.raises(ValueError):
        ranking.validate_evidence(payload({**row("akashml"), "tenant_id": "secret"}))
    with pytest.raises(ValueError):
        ranking.validate_evidence(payload(row("akashml"), row("akashml")))


def test_worker_is_bounded_aggregated_and_failure_isolated(monkeypatch):
    calls, published = [], []
    def query(password, sql, **kwargs):
        calls.append(sql)
        return json.dumps({"ms": 0}) if "max(generated_at)" in sql else json.dumps(row("akashml"))
    monkeypatch.setattr(worker, "_query", query)
    monkeypatch.setattr(worker, "_insert_snapshots", lambda password, data, **kwargs: published.append(data))
    worker._publish_model_routing("unused", now=NOW, generated_at=NOW.isoformat())
    assert published[0]["model_routing_evidence"]["routes"] == [row("akashml")]
    sql = calls[1]
    for clause in (" FINAL", "synthetic = 0", "status = 'success'", "streamed = 1", "usage_type = 'Credits'", "max_execution_time=30", "max_memory_usage=1073741824", "LIMIT 20001", "HAVING ttft_samples >= 25"):
        assert clause in sql
    assert "tenant_id" not in sql and "request_id" not in sql
    before = copy.deepcopy(published)
    monkeypatch.setattr(worker, "_query", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    worker._publish_model_routing("unused", now=NOW, generated_at=NOW.isoformat())
    assert published == before


def test_worker_uses_six_hour_cadence(monkeypatch):
    calls = []
    def query(*args, **kwargs):
        calls.append(args)
        return json.dumps({"ms": int((NOW - timedelta(hours=1)).timestamp() * 1000)})
    monkeypatch.setattr(worker, "_query", query)
    worker._publish_model_routing("unused", now=NOW, generated_at=NOW.isoformat())
    assert len(calls) == 1


def test_snapshot_failure_and_atomic_write_preserve_old_file(monkeypatch, tmp_path):
    from scripts import update_model_routing_evidence as refresh

    now = datetime.now(UTC)
    destination, source = tmp_path / "evidence.json", tmp_path / "source.json"
    destination.write_text(json.dumps(payload(row("akashml"), at=now - timedelta(days=2))))
    source.write_text(json.dumps(payload(row("akashml", ttft=1400), at=now)))
    monkeypatch.setattr(refresh, "EVIDENCE_PATH", destination)
    monkeypatch.setattr("sys.argv", ["refresh", "--json", str(source), "--write"])
    before = destination.read_bytes()
    def fail(*args):
        raise OSError("write failed")
    with monkeypatch.context() as patch:
        patch.setattr(type(destination), "replace", fail)
        with pytest.raises(OSError):
            refresh.main()
    assert destination.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evidence.json", "source.json"]
    refresh.main()
    assert json.loads(destination.read_text())["routes"][0]["ttft_ms"] == 1400


@pytest.mark.parametrize("age,expected", [(1, 200), (13, 503), (-1, 503)])
def test_public_evidence_endpoint_serves_only_fresh_aggregates(client, monkeypatch, age, expected):
    from trusted_router.storage import InMemoryStore

    data = payload(row("akashml"), at=datetime.now(UTC) - timedelta(hours=age))
    calls = []
    def read(self, name):
        calls.append(name)
        return data
    monkeypatch.setattr(InMemoryStore, "public_analytics_snapshot", read, raising=False)
    response = client.get("/routing-evidence.json")
    assert response.status_code == expected
    assert calls == ["model_routing_evidence"]
    if expected == 200:
        assert response.json() == data
        assert response.headers["cache-control"] == "public, max-age=300"


def test_unknown_endpoint_never_inherits_other_models_latency_rank(monkeypatch):
    items = candidates(monkeypatch)
    install(monkeypatch, row("akashml", model="other/model", ttft=1))
    assert _sort_endpoint_candidates(list(reversed(items)), RoutePreferences(
        estimated_input_tokens=100, sort="latency",
    ))[0][1].provider == "tinfoil"
