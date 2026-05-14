from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from trusted_router.catalog import (
    CHEAP_MODEL_ID,
    FREE_MODEL_ID,
    MODELS,
    MONITOR_MODEL_ID,
    meta_candidate_models,
    model_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routing import chat_route_candidates
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE, SyntheticProbeSample
from trusted_router.storage_gcp_codec import reverse_time_key as _reverse_time_key
from trusted_router.storage_gcp_synthetic_index import (
    synthetic_probe_samples as _bt_synthetic_probe_samples,
)
from trusted_router.storage_gcp_synthetic_index import (
    write_synthetic_probe_sample as _bt_write_synthetic_probe_sample,
)
from trusted_router.storage_gcp_synthetic_rollups import (
    synthetic_rollups as _bt_synthetic_rollups,
)
from trusted_router.storage_models import iso_now, utcnow
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    attestation_nonce_probe,
    openai_chat_pong_probe,
    responses_pong_probe,
    tls_health_probe,
)
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    sample_rollup_ids,
)
from trusted_router.synthetic.status import history_payload, status_snapshot


def test_catalog_exposes_free_cheap_and_monitor_meta_models() -> None:
    assert FREE_MODEL_ID in MODELS
    assert CHEAP_MODEL_ID in MODELS
    assert MONITOR_MODEL_ID in MODELS

    free = meta_candidate_models(FREE_MODEL_ID)
    cheap = meta_candidate_models(CHEAP_MODEL_ID)
    monitor = meta_candidate_models(MONITOR_MODEL_ID)

    assert any(model.id == "z-ai/glm-4.5-air:free" for model in free)
    assert free
    assert all(model.id.endswith(":free") for model in free)
    assert len({model.provider for model in cheap}) >= 2
    assert len({model.provider for model in monitor}) >= 2
    assert all(not model.id.endswith(":free") for model in cheap + monitor)

    monitor_shape = model_to_openrouter_shape(MODELS[MONITOR_MODEL_ID])
    assert monitor_shape["trustedrouter"]["route_kind"] == "synthetic_monitor_pool"
    assert monitor_shape["trustedrouter"]["synthetic_monitor"] is True
    assert monitor_shape["trustedrouter"]["auto_candidates"]


def test_monitor_alias_expands_to_paid_rollover_candidates() -> None:
    candidates = chat_route_candidates(
        {"model": MONITOR_MODEL_ID},
        Settings(environment="test"),
    )

    assert len(candidates) >= 2
    assert [candidate.id for candidate in candidates[:2]] == [
        "anthropic/claude-haiku-4.5",
        "z-ai/glm-4.5-air",
    ]
    assert all(not candidate.id.endswith(":free") for candidate in candidates)


def test_monitor_alias_is_marked_internal_only() -> None:
    shape = model_to_openrouter_shape(MODELS[MONITOR_MODEL_ID])

    assert shape["trustedrouter"]["internal_only"] is True
    assert shape["trustedrouter"]["synthetic_monitor"] is True


def test_status_json_is_public_metadata_only(client: TestClient) -> None:
    samples = [
        _sample(
            id="syn_router_core_1",
            probe_type="tls_health",
            status="up",
            latency_milliseconds=25,
        ),
        _sample(
            id="syn_1",
            probe_type="openai_sdk_pong",
            status="up",
            model=MONITOR_MODEL_ID,
            output_match=True,
        ),
    ]
    resp = client.post(
        "/v1/internal/synthetic/samples",
        json={"samples": [sample.public_dict() for sample in samples]},
    )
    assert resp.status_code == 200, resp.text

    status = client.get("/status.json")
    page = client.get("/status")
    history = client.get("/status/history?window=5m")

    assert status.status_code == 200
    assert page.status_code == 200
    assert history.status_code == 200
    assert "s-maxage" in status.headers["cache-control"]
    assert "stale-while-revalidate" in status.headers["cache-control"]
    assert "All Systems Operational" in page.text
    assert "Components" in page.text
    assert "In-region gateway overhead p50" in page.text
    assert "last-48-hour uptime history" in page.text
    text = status.text
    assert "reply exactly PONG" not in text
    assert "sk-tr-" not in text
    payload = status.json()["data"]
    provider_sample = next(
        sample for sample in payload["samples"] if sample["probe_type"] == "openai_sdk_pong"
    )
    assert provider_sample["output_match"] is True
    assert payload["components"][0]["name"] == "Canonical API"
    assert len(payload["components"][0]["history"]) == 48


def test_public_status_response_cache_reuses_rendered_body() -> None:
    import trusted_router.routes.public as public_routes

    with public_routes._STATUS_RESPONSE_CACHE_LOCK:
        public_routes._STATUS_RESPONSE_CACHE.clear()
        public_routes._STATUS_RESPONSE_REFRESHING.clear()
    calls = 0

    def build() -> bytes:
        nonlocal calls
        calls += 1
        return f"payload-{calls}".encode()

    try:
        first = public_routes._cached_public_response(
            Settings(environment="local"),
            key="test:status-cache",
            media_type="application/json",
            ttl_seconds=60,
            stale_seconds=300,
            background_tasks=BackgroundTasks(),
            build=build,
        )
        second = public_routes._cached_public_response(
            Settings(environment="local"),
            key="test:status-cache",
            media_type="application/json",
            ttl_seconds=60,
            stale_seconds=300,
            background_tasks=BackgroundTasks(),
            build=build,
        )
    finally:
        with public_routes._STATUS_RESPONSE_CACHE_LOCK:
            public_routes._STATUS_RESPONSE_CACHE.clear()
            public_routes._STATUS_RESPONSE_REFRESHING.clear()

    assert first.body == b"payload-1"
    assert first.headers["x-tr-cache"] == "miss"
    assert second.body == b"payload-1"
    assert second.headers["x-tr-cache"] == "hit"
    assert calls == 1


def test_status_history_monthly_uses_public_rollups(client: TestClient) -> None:
    sample = _sample(
        id="syn_monthly",
        probe_type="tls_health",
        status="up",
        latency_milliseconds=88,
    )
    assert (
        client.post("/v1/internal/synthetic/samples", json=sample.public_dict()).status_code == 200
    )

    history = client.get("/status/history?window=monthly")

    assert history.status_code == 200
    payload = history.json()["data"]
    assert payload["window"] == "monthly"
    assert payload["data"][0]["sample_count"] == 1
    assert payload["data"][0]["uptime_percent"] == 100.0
    assert "sk-tr-" not in history.text
    assert "reply exactly PONG" not in history.text


def test_status_history_browser_requests_render_48h_visual_page(client: TestClient) -> None:
    sample = _sample(
        id="syn_48h_visual",
        probe_type="tls_health",
        status="up",
        latency_milliseconds=33,
    )
    assert (
        client.post("/v1/internal/synthetic/samples", json=sample.public_dict()).status_code == 200
    )

    history = client.get("/status/history?window=48h", headers={"accept": "text/html"})

    assert history.status_code == 200
    assert history.headers["content-type"].startswith("text/html")
    assert "48-hour status history" in history.text
    assert (
        "Latency is broken out by target, probe, monitor region, and target region" in history.text
    )
    assert "48-hour component timeline" in history.text
    assert "View JSON" in history.text
    assert "reply exactly PONG" not in history.text
    assert "sk-tr-" not in history.text


def test_status_history_browser_requests_render_monthly_visual_page(client: TestClient) -> None:
    sample = _sample(
        id="syn_monthly_visual",
        probe_type="tls_health",
        status="up",
        latency_milliseconds=77,
    )
    assert (
        client.post("/v1/internal/synthetic/samples", json=sample.public_dict()).status_code == 200
    )

    history = client.get("/status/history?window=monthly", headers={"accept": "text/html"})

    assert history.status_code == 200
    assert history.headers["content-type"].startswith("text/html")
    assert "Monthly status history" in history.text
    assert "Monthly rollups" in history.text
    assert "Precomputed reliability history" in history.text
    assert "Latency breakdown" in history.text
    assert "View JSON" in history.text
    assert "reply exactly PONG" not in history.text
    assert "sk-tr-" not in history.text


def test_status_history_format_json_overrides_browser_accept(client: TestClient) -> None:
    sample = _sample(
        id="syn_json_override",
        probe_type="tls_health",
        status="up",
        latency_milliseconds=42,
    )
    assert (
        client.post("/v1/internal/synthetic/samples", json=sample.public_dict()).status_code == 200
    )

    history = client.get(
        "/status/history?window=48h&format=json",
        headers={"accept": "text/html"},
    )

    assert history.status_code == 200
    assert history.headers["content-type"].startswith("application/json")
    assert history.json()["data"]["window"] == "48h"


def test_public_status_snapshot_uses_live_samples_plus_precomputed_rollups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.routes.public as public_routes

    now = utcnow()
    recent = _sample(
        id="syn_live",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=31,
    )
    old = _sample(
        id="syn_rollup_old",
        probe_type="responses_pong",
        status="up",
        created_at=(now - dt.timedelta(hours=26)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=99,
    )
    rollups = _rollups_for_samples([recent, old])
    sample_calls: list[dict[str, Any]] = []
    rollup_calls: list[dict[str, Any]] = []

    class FakeStatusStore:
        def synthetic_probe_samples(self, **kwargs: Any) -> list[SyntheticProbeSample]:
            sample_calls.append(kwargs)
            return [recent]

        def synthetic_rollups(self, **kwargs: Any) -> list[Any]:
            rollup_calls.append(kwargs)
            period = kwargs["period"]
            since = kwargs.get("since")
            return [
                rollup
                for rollup in rollups
                if rollup.period == period and (since is None or rollup.period_start >= since)
            ]

    monkeypatch.setattr(public_routes, "STORE", FakeStatusStore())

    payload = public_routes._status_snapshot(Settings(environment="test"))

    assert sample_calls == [{"limit": public_routes.STATUS_LIVE_SAMPLE_LIMIT}]
    assert [call["period"] for call in rollup_calls] == ["hour"]
    assert all(call["since"] for call in rollup_calls)
    assert all("until" not in call for call in rollup_calls)
    assert payload["windows"]["5m"]["sample_count"] == 1
    assert payload["windows"]["48h"]["sample_count"] == 2
    assert payload["headline_metrics"]["gateway_overhead_p50_milliseconds"] == 31


def test_status_subdomain_root_renders_status_page(client: TestClient) -> None:
    samples = [
        _sample(
            id="syn_status_host_router_core",
            probe_type="tls_health",
            status="up",
            latency_milliseconds=30,
        ),
        _sample(
            id="syn_status_host",
            probe_type="openai_sdk_pong",
            status="up",
            model=MONITOR_MODEL_ID,
            output_match=True,
        ),
    ]
    assert (
        client.post(
            "/v1/internal/synthetic/samples",
            json={"samples": [sample.public_dict() for sample in samples]},
        ).status_code
        == 200
    )

    page = client.get("/", headers={"host": "status.trustedrouter.com"})

    assert page.status_code == 200
    assert "TrustedRouter Status" in page.text
    assert "All Systems Operational" in page.text


def test_chat_monitor_model_requires_configured_monitor_key() -> None:
    monitor_key = "sk-tr-monitor-test"  # noqa: S105 - test key.
    app = create_app(
        Settings(environment="test", synthetic_monitor_api_key=monitor_key),
        init_observability=False,
    )
    local_client = TestClient(app)
    normal = local_client.post(
        "/v1/keys", headers={"x-trustedrouter-user": "alice@example.com"}, json={"name": "normal"}
    )
    assert normal.status_code == 201, normal.text
    normal_key = normal.json()["key"]
    monitor_user = STORE.ensure_user("monitor", email="monitor@trustedrouter.local")
    monitor_workspace = STORE.list_workspaces_for_user(monitor_user.id)[0]
    STORE.create_api_key(
        workspace_id=monitor_workspace.id,
        name="Synthetic monitor",
        creator_user_id=monitor_user.id,
        raw_key=monitor_key,
    )

    body = {
        "model": MONITOR_MODEL_ID,
        "messages": [{"role": "user", "content": "reply exactly PONG"}],
        "max_tokens": 4,
    }
    denied = local_client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {normal_key}"},
        json=body,
    )
    allowed = local_client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {monitor_key}"},
        json=body,
    )

    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == (
        "trustedrouter/monitor is restricted to the synthetic monitor key"
    )
    assert allowed.status_code == 200, allowed.text


def test_status_rollups_cover_current_5m_24h_and_daily_windows() -> None:
    # Pin `now` to mid-day UTC so the `now - 2h` sample lands in the
    # same daily bucket as `now - 30s`. With wall-clock `now`, running
    # this test near 00:00 UTC pushed the 2h-old sample into the
    # previous day, splitting the daily rollup and intermittently
    # tripping `sum(... daily ...) == 4`.
    now = dt.datetime(2026, 5, 7, 12, 0, 0, tzinfo=dt.UTC)
    samples = [
        _sample(
            id="syn_up",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=25,
        ),
        _sample(
            id="syn_down",
            probe_type="responses_pong",
            status="down",
            created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=500,
        ),
        _sample(
            id="syn_down_2",
            probe_type="openai_sdk_pong",
            status="down",
            created_at=(now - dt.timedelta(minutes=2, seconds=10))
            .isoformat()
            .replace("+00:00", "Z"),
            latency_milliseconds=510,
        ),
        _sample(
            id="syn_old",
            probe_type="responses_pong",
            status="up",
            created_at=(now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=120,
        ),
    ]

    snapshot = status_snapshot(samples, now=now)

    assert snapshot["current"]["checks"]
    assert snapshot["overall_status"] == "up"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"
    assert snapshot["slo_classes"]["provider_effective"]["status"] == "down"
    assert snapshot["windows"]["5m"]["sample_count"] == 3
    assert snapshot["windows"]["24h"]["sample_count"] == 4
    assert snapshot["windows"]["48h"]["sample_count"] == 4
    assert sum(row["sample_count"] for row in snapshot["daily"]) == 4
    assert snapshot["headline_metrics"]["gateway_overhead_p50_milliseconds"] == 25
    assert snapshot["headline_metrics"]["gateway_overhead_scope"] == "in_region"
    canonical = next(
        component for component in snapshot["components"] if component["id"] == "canonical_api"
    )
    assert canonical["status"] == "down"
    assert canonical["uptime_24h_percent"] == pytest.approx(50.0)
    assert canonical["p50_latency_milliseconds"] == 25
    assert canonical["end_to_end_p50_latency_milliseconds"] == 120
    assert len(canonical["history"]) == 48
    assert snapshot["recent_events"][0]["component"] == "Canonical API"


def test_status_slo_classes_do_not_blend_provider_failures_into_router_core() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_tls_ok",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=22,
        ),
        _sample(
            id="syn_auth_ok",
            target="control-plane",
            target_region=None,
            probe_type="gateway_authorize_settle",
            status="up",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_fallback_ok",
            target="control-plane",
            target_region=None,
            probe_type="provider_fallback",
            status="up",
            created_at=(now - dt.timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_chat_provider_down",
            probe_type="openai_sdk_pong",
            status="down",
            created_at=(now - dt.timedelta(seconds=13)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_responses_provider_down",
            probe_type="responses_pong",
            status="down",
            created_at=(now - dt.timedelta(seconds=14)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    snapshot = status_snapshot(samples, now=now)

    assert snapshot["overall_status"] == "up"
    assert snapshot["summary"]["headline"] == "All Systems Operational"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"
    assert snapshot["slo_classes"]["provider_effective"]["status"] == "down"
    assert snapshot["slo_classes"]["router_core"]["windows"]["5m"]["bad_count"] == 0
    assert snapshot["slo_classes"]["provider_effective"]["windows"]["5m"]["bad_count"] == 2


def test_status_router_core_burn_rate_alerts_on_short_window_failures() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_tls_down",
            probe_type="tls_health",
            status="down",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_settle_down",
            target="control-plane",
            target_region=None,
            probe_type="gateway_authorize_settle",
            status="down",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    snapshot = status_snapshot(samples, now=now)

    assert snapshot["slo_classes"]["router_core"]["status"] == "down"
    alert = next(
        item
        for item in snapshot["burn_rate_alerts"]
        if item["slo_class"] == "router_core" and item["window"] == "5m"
    )
    assert alert["level"] == "critical"
    assert alert["burn_rate"] >= 100_000
    assert alert["bad_count"] == 2


def test_status_headline_prefers_in_region_gateway_overhead() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_us_in_region",
            target="us-central1",
            target_region="us-central1",
            monitor_region="us-central1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=30,
        ),
        _sample(
            id="syn_eu_from_us",
            target="europe-west4",
            target_region="europe-west4",
            monitor_region="us-central1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=400,
        ),
        _sample(
            id="syn_us_from_eu",
            target="us-central1",
            target_region="us-central1",
            monitor_region="europe-west4",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=500,
        ),
    ]

    metrics = status_snapshot(samples)["headline_metrics"]

    assert metrics["gateway_overhead_scope"] == "in_region"
    assert metrics["in_region_gateway_overhead_p50_milliseconds"] == 30
    assert metrics["global_gateway_overhead_p50_milliseconds"] == 400
    assert metrics["gateway_overhead_p50_milliseconds"] == 30


def test_status_detail_latency_groups_are_not_region_blended() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_us_fast",
            target="canonical",
            target_region="us-central1",
            monitor_region="us-central1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=25,
        ),
        _sample(
            id="syn_eu_slow",
            target="canonical",
            target_region="us-central1",
            monitor_region="europe-west4",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=450,
        ),
    ]

    payload = history_payload(samples, "5m")

    groups = payload["data"]["groups"]
    assert len(groups) == 2
    assert {
        (group["monitor_region"], group["target_region"], group["p50_latency_milliseconds"])
        for group in groups
    } == {
        ("us-central1", "us-central1", 25),
        ("europe-west4", "us-central1", 450),
    }


def test_monthly_history_carries_per_region_latency_breakdown() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_month_us",
            target="canonical",
            target_region="us-central1",
            monitor_region="us-central1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=31,
        ),
        _sample(
            id="syn_month_eu",
            target="canonical",
            target_region="us-central1",
            monitor_region="europe-west4",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=420,
        ),
    ]
    rollups = _rollups_for_samples(samples)

    payload = history_payload([], "monthly", rollups=rollups)

    groups = payload["data"][0]["groups"]
    assert len(groups) == 2
    assert {
        (
            group["component_name"],
            group["monitor_region"],
            group["target_region"],
            group["p50_latency_milliseconds"],
        )
        for group in groups
    } == {
        ("Canonical API", "us-central1", "us-central1", 31),
        ("Canonical API", "europe-west4", "us-central1", 420),
    }


def test_status_uses_hourly_rollups_for_48h_history_when_raw_samples_are_recent_only() -> None:
    now = utcnow()
    recent = _sample(
        id="syn_recent",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=25,
    )
    old = _sample(
        id="syn_26h",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(hours=26)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=55,
    )
    rollups = _rollups_for_samples([recent, old])

    snapshot = status_snapshot([recent], rollups=rollups)

    assert snapshot["windows"]["24h"]["sample_count"] == 1
    assert snapshot["windows"]["48h"]["sample_count"] == 2
    canonical = next(
        component for component in snapshot["components"] if component["id"] == "canonical_api"
    )
    assert canonical["sample_count_24h"] == 1
    assert sum(bucket["sample_count"] for bucket in canonical["history"]) == 2


def test_status_history_fills_missing_rollup_hours_from_raw_samples() -> None:
    now = utcnow()
    recent = _sample(
        id="syn_rollup_recent",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=25,
    )
    old_raw_only = _sample(
        id="syn_raw_only_26h",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(hours=26)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=55,
    )
    rollups = _rollups_for_samples([recent])

    snapshot = status_snapshot([recent, old_raw_only], rollups=rollups)

    assert snapshot["windows"]["24h"]["sample_count"] == 1
    assert snapshot["windows"]["48h"]["sample_count"] == 2
    canonical = next(
        component for component in snapshot["components"] if component["id"] == "canonical_api"
    )
    non_empty_buckets = [bucket for bucket in canonical["history"] if bucket["sample_count"]]
    assert len(non_empty_buckets) == 2
    assert {bucket["p50_latency_milliseconds"] for bucket in non_empty_buckets} == {25, 55}


def test_status_components_group_regions_and_control_plane() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_canonical",
            probe_type="responses_pong",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_eu",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="openai_sdk_pong",
            status="up",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_settle",
            target="control-plane",
            target_region=None,
            probe_type="gateway_authorize_settle",
            status="up",
            created_at=(now - dt.timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_fallback",
            target="control-plane",
            target_region=None,
            probe_type="provider_fallback",
            status="routing_degraded",
            created_at=(now - dt.timedelta(seconds=13)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    snapshot = status_snapshot(samples)
    components = {component["id"]: component for component in snapshot["components"]}

    assert components["canonical_api"]["status"] == "up"
    assert components["eu_regional_api"]["status"] == "up"
    assert components["billing_settlement"]["status"] == "up"
    assert components["provider_fallback"]["status"] == "routing_degraded"
    assert snapshot["overall_status"] == "routing_degraded"


def test_status_component_current_uses_latest_sample_per_probe() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_old_down_1",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="openai_sdk_pong",
            status="down",
            created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_old_down_2",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="openai_sdk_pong",
            status="down",
            created_at=(now - dt.timedelta(minutes=2, seconds=10))
            .isoformat()
            .replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_latest_up",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="openai_sdk_pong",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    snapshot = status_snapshot(samples)
    eu = next(
        component for component in snapshot["components"] if component["id"] == "eu_regional_api"
    )

    assert eu["status"] == "up"
    assert eu["uptime_24h_percent"] == pytest.approx(33.3333)
    assert snapshot["recent_events"][0]["id"] == "syn_old_down_1"


def test_gcp_synthetic_index_uses_privacy_safe_recency_keys() -> None:
    sample = _sample(
        id="syn_1",
        probe_type="attestation_nonce",
        status="up",
        created_at="2026-05-05T12:00:00Z",
    )
    table = _FakeBigtable()

    _bt_write_synthetic_probe_sample(table, "m", sample)

    reverse = _reverse_time_key(sample.created_at)
    raw_keys = [
        f"synthetic_recent#{reverse}#syn_1".encode(),
        f"synthetic_target_recent#canonical#{reverse}#syn_1".encode(),
        f"synthetic_probe_target_recent#attestation_nonce#canonical#{reverse}#syn_1".encode(),
        f"synthetic_monitor_recent#us-central1#{reverse}#syn_1".encode(),
        f"synthetic_day#2026-05-05#canonical#attestation_nonce#{reverse}#syn_1".encode(),
        f"synthetic_day_recent#2026-05-05#{reverse}#syn_1".encode(),
    ]
    assert table.committed[:6] == raw_keys
    assert any(
        key.startswith(b"synthetic_rollup#hour#2026-05-05T12:00:00Z#") for key in table.committed
    )
    assert any(
        key.startswith(b"synthetic_rollup#day#2026-05-05T00:00:00Z#") for key in table.committed
    )
    assert any(
        key.startswith(b"synthetic_rollup#month#2026-05-01T00:00:00Z#") for key in table.committed
    )
    assert b"sk-tr" not in b"".join(table.committed)
    assert b"prompt" not in b"".join(table.committed)


def test_synthetic_rollups_are_idempotent_and_monthly_queryable() -> None:
    sample = _sample(
        id="syn_rollup",
        probe_type="tls_health",
        status="up",
        created_at="2026-05-05T12:00:00Z",
        latency_milliseconds=123,
    )
    table = _FakeBigtable()

    _bt_write_synthetic_probe_sample(table, "m", sample)
    _bt_write_synthetic_probe_sample(table, "m", sample)
    month = _bt_synthetic_rollups(table, "m", period="month", limit=20)
    canonical = next(row for row in month if row.component == "canonical_api")

    assert canonical.sample_count == 1
    assert canonical.up_count == 1
    assert canonical.latency_histogram == {"123": 1}


def test_gcp_synthetic_rollups_use_period_start_range() -> None:
    old = _sample(
        id="syn_rollup_old_range",
        probe_type="tls_health",
        status="up",
        created_at="2026-05-05T11:10:00Z",
        latency_milliseconds=80,
    )
    recent = _sample(
        id="syn_rollup_recent_range",
        probe_type="tls_health",
        status="up",
        created_at="2026-05-05T12:10:00Z",
        latency_milliseconds=40,
    )
    table = _FakeBigtable()
    _bt_write_synthetic_probe_sample(table, "m", old)
    _bt_write_synthetic_probe_sample(table, "m", recent)

    rows = _bt_synthetic_rollups(
        table,
        "m",
        period="hour",
        since="2026-05-05T12:00:00Z",
        limit=20,
    )

    assert {row.period_start for row in rows} == {"2026-05-05T12:00:00Z"}
    assert table.reads[-1] == (
        b"synthetic_rollup#hour#2026-05-05T12:00:00Z",
        b"synthetic_rollup#hour#~",
        20,
    )


def test_raw_synthetic_samples_expire_before_rollups() -> None:
    old = _sample(
        id="syn_old_raw",
        probe_type="tls_health",
        status="up",
        created_at=(utcnow() - dt.timedelta(days=20)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=75,
    )

    STORE.record_synthetic_probe_sample(old)

    assert STORE.synthetic_probe_samples(limit=10) == []
    monthly = STORE.synthetic_rollups(period="month", limit=10)
    assert monthly
    assert monthly[0].sample_count == 1


def test_gcp_synthetic_reads_daily_probe_target_index() -> None:
    sample = _sample(
        id="syn_1",
        probe_type="tls_health",
        status="up",
        created_at="2026-05-05T12:00:00Z",
    )
    table = _FakeBigtable([_FakeReadRow(sample)])

    rows = _bt_synthetic_probe_samples(
        table,
        "m",
        date="2026-05-05",
        target="canonical",
        probe_type="tls_health",
        monitor_region=None,
        limit=5,
    )

    assert [row.id for row in rows] == ["syn_1"]
    assert table.reads == [
        (
            b"synthetic_day#2026-05-05#canonical#tls_health#",
            b"synthetic_day#2026-05-05#canonical#tls_health#~",
            5,
        )
    ]


@pytest.mark.asyncio
async def test_synthetic_http_probes_parse_success_shapes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/attestation":
            nonce = request.url.params["nonce"]
            return httpx.Response(200, content=_jwt({"nonces": [nonce]}))
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "PONG"}}]},
            )
        if request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                json={"output": [{"content": [{"text": "PONG"}]}]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    target = SyntheticTarget("canonical", "https://api.quillrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=transport) as client:
        health = await tls_health_probe(client, target, monitor_region="us-central1")
        attestation = await attestation_nonce_probe(client, target, monitor_region="us-central1")
        chat = await openai_chat_pong_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            model=MONITOR_MODEL_ID,
        )
        responses = await responses_pong_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            model=MONITOR_MODEL_ID,
        )

    assert health.status == "up"
    assert attestation.status == "up"
    assert chat.status == "up"
    assert chat.output_match is True
    assert responses.status == "up"
    assert responses.output_match is True


@pytest.mark.asyncio
async def test_synthetic_http_probes_accept_gateway_auth_health_and_gcp_nonce() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        if request.url.path == "/attestation":
            nonce = request.url.params["nonce"]
            return httpx.Response(200, content=_jwt({"eat_nonce": ["tls-fp", nonce]}))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    target = SyntheticTarget("canonical", "https://api.quillrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=transport) as client:
        health = await tls_health_probe(client, target, monitor_region="us-central1")
        attestation = await attestation_nonce_probe(client, target, monitor_region="us-central1")

    assert health.status == "up"
    assert health.http_status == 401
    assert attestation.status == "up"


def test_synthetic_gateway_settlement_does_not_pollute_provider_benchmarks(
    client: TestClient,
    inference_key: str,
) -> None:
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(inference_key),
            "model": CHEAP_MODEL_ID,
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert len(data["route_candidates"]) >= 2
    fallback = data["route_candidates"][1]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": data["authorization_id"],
            "input_tokens": 1,
            "output_tokens": 1,
            "request_id": "req_synthetic",
            "app": "TrustedRouter Synthetic",
            "model": fallback["model"],
            "selected_endpoint": fallback["endpoint_id"],
        },
    )

    assert settle.status_code == 200, settle.text
    assert settle.json()["data"]["endpoint_id"] == fallback["endpoint_id"]
    assert STORE.activity_events(data["workspace_id"], limit=10)
    assert STORE.provider_benchmark_samples() == []


def test_internal_generation_activity_reconciliation_endpoint_is_guarded_and_callable(
    client: TestClient,
) -> None:
    user = STORE.ensure_user("ops", email="ops@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]

    response = client.post(
        "/v1/internal/reconcile/generation-activity",
        json={"workspace_id": workspace.id, "limit": 10},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "workspace_id": workspace.id,
        "date": None,
        "limit": 10,
        "rewritten": 0,
    }


def test_gateway_monitor_model_requires_configured_monitor_key() -> None:
    monitor_key = "sk-tr-monitor-gateway"  # noqa: S105 - test key.
    app = create_app(
        Settings(environment="test", synthetic_monitor_api_key=monitor_key),
        init_observability=False,
    )
    local_client = TestClient(app)
    normal = local_client.post(
        "/v1/keys", headers={"x-trustedrouter-user": "alice@example.com"}, json={"name": "normal"}
    )
    assert normal.status_code == 201, normal.text
    normal_key = normal.json()["key"]
    monitor_user = STORE.ensure_user("monitor", email="monitor@trustedrouter.local")
    monitor_workspace = STORE.list_workspaces_for_user(monitor_user.id)[0]
    STORE.create_api_key(
        workspace_id=monitor_workspace.id,
        name="Synthetic monitor",
        creator_user_id=monitor_user.id,
        raw_key=monitor_key,
    )

    denied = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(normal_key),
            "model": MONITOR_MODEL_ID,
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    allowed = local_client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(monitor_key),
            "model": MONITOR_MODEL_ID,
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200, allowed.text


def _rollups_for_samples(samples: list[SyntheticProbeSample]) -> list[Any]:
    rollups = {}
    seen = set()
    for sample in samples:
        for period, component in sample_rollup_ids(sample):
            candidate = new_rollup_for_sample(sample, period=period, component=component)
            seen_key = (candidate.id, sample.id)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            existing = rollups.get(candidate.id)
            if existing is None:
                rollups[candidate.id] = candidate
            else:
                apply_sample_to_rollup(existing, sample)
    return list(rollups.values())


def _sample(
    *,
    id: str,
    probe_type: str,
    status: str,
    target: str = "canonical",
    target_region: str | None = "us-central1",
    monitor_region: str = "us-central1",
    model: str | None = None,
    output_match: bool | None = None,
    created_at: str | None = None,
    latency_milliseconds: int | None = None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=id,
        probe_type=probe_type,
        target=target,
        target_url="https://api.quillrouter.com/v1",
        monitor_region=monitor_region,
        target_region=target_region,
        status=status,
        model=model,
        output_match=output_match,
        latency_milliseconds=latency_milliseconds,
        created_at=created_at or iso_now(),
    )


def _jwt(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{body}.signature".encode()


class _FakeCell:
    def __init__(self, value: Any) -> None:
        if isinstance(value, bytes):
            self.value = value
        elif hasattr(value, "__dataclass_fields__"):
            self.value = json.dumps(asdict(value), separators=(",", ":"), sort_keys=True).encode()
        else:
            self.value = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class _FakeReadRow:
    def __init__(self, value: Any) -> None:
        self.cells = {"m": {b"body": [_FakeCell(value)]}}


class _FakeDirectRow:
    def __init__(self, key: bytes, table: _FakeBigtable) -> None:
        self.key = key
        self.table = table
        self.value: bytes | None = None

    def set_cell(self, _family: str, _qualifier: bytes, value: bytes) -> None:
        self.value = value
        return None

    def commit(self) -> None:
        self.table.committed.append(self.key)
        if self.value is not None:
            self.table.rows_by_key[self.key] = _FakeReadRow(self.value)


class _FakeBigtable:
    def __init__(self, rows: list[_FakeReadRow] | None = None) -> None:
        self.rows = rows or []
        self.rows_by_key: dict[bytes, _FakeReadRow] = {}
        self.reads: list[tuple[bytes, bytes, int]] = []
        self.committed: list[bytes] = []

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int) -> list[_FakeReadRow]:
        self.reads.append((start_key, end_key, limit))
        keyed_rows = [
            row for key, row in sorted(self.rows_by_key.items()) if start_key <= key < end_key
        ]
        return (keyed_rows + self.rows)[:limit]

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self)
