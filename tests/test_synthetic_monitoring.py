from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import random
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from trustedrouter import AsyncTrustedRouter

from trusted_router.catalog import (
    CHEAP_MODEL_ID,
    E2E_MODEL_ID,
    EU_MODEL_ID,
    FREE_MODEL_ID,
    MODELS,
    MONITOR_MODEL_ID,
    PRIVACY_TIER_CONFIDENTIAL,
    ZDR_MODEL_ID,
    endpoint_privacy_tier,
    endpoints_for_model,
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
from trusted_router.storage_models import ProviderBenchmarkSample, iso_now, utcnow
from trusted_router.synthetic.components import sample_slo_class_ids
from trusted_router.synthetic.inference_sdk import build_inference_sdk, close_inference_sdk
from trusted_router.synthetic.probes import (
    IMAGE_GENERATION_MODEL,
    IMAGE_GENERATION_PROVIDER,
    SyntheticTarget,
    _remaining_probe_seconds,
    _rotation_max_tokens,
    _rotation_omits_temperature,
    _server_timing_gateway,
    _sse_line_error,
    _sse_line_finish_reason,
    _sse_line_has_content,
    attestation_nonce_probe,
    choose_rotation_target,
    gateway_billing_probe,
    gateway_fallback_probe,
    gateway_latency_phase_probes,
    image_generation_probe,
    openai_chat_pong_probe,
    provider_rotation_probe,
    responses_pong_probe,
    rotation_candidates,
    run_synthetic_once,
    tls_health_probe,
)
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    sample_rollup_ids,
)
from trusted_router.synthetic.route_health import (
    RouteHealthFlag,
    evaluate_route_health,
    report_image_generation_failures,
    report_route_health,
)
from trusted_router.synthetic.status import history_payload, status_snapshot

# The SDK sessions' beacon destination in these tests. TCP port zero is
# reserved and can never be a listening service, so the reporter's close-time
# flush fails instantly without an import-time socket bind or a port race.
_NO_CONTROL_PLANE = "http://127.0.0.1:0"


@contextlib.asynccontextmanager
async def _monitor_sdk(
    client: httpx.AsyncClient, target: SyntheticTarget
) -> AsyncIterator[AsyncTrustedRouter]:
    """The production SDK session around a fake-gateway client, closed after use."""
    sdk = build_inference_sdk(
        target.api_base_url,
        api_key="sk-test",
        http_client=client,
        control_plane_base_url=_NO_CONTROL_PLANE,
    )
    try:
        yield sdk
    finally:
        await close_inference_sdk(sdk)


def test_catalog_exposes_free_cheap_and_monitor_meta_models() -> None:
    assert FREE_MODEL_ID in MODELS
    assert CHEAP_MODEL_ID in MODELS
    assert EU_MODEL_ID in MODELS
    assert ZDR_MODEL_ID in MODELS
    assert E2E_MODEL_ID in MODELS
    assert MONITOR_MODEL_ID in MODELS

    free = meta_candidate_models(FREE_MODEL_ID)
    cheap = meta_candidate_models(CHEAP_MODEL_ID)
    eu = meta_candidate_models(EU_MODEL_ID)
    zdr = meta_candidate_models(ZDR_MODEL_ID)
    e2e = meta_candidate_models(E2E_MODEL_ID)
    monitor = meta_candidate_models(MONITOR_MODEL_ID)

    assert all(model.id.endswith(":free") for model in free)
    assert len({model.provider for model in cheap}) >= 2
    assert eu and eu[0].provider == "mistral"
    assert zdr
    assert e2e
    assert all(
        any(
            endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_CONFIDENTIAL
            for endpoint in endpoints_for_model(model.id)
        )
        for model in e2e
    )
    assert len({model.provider for model in monitor}) >= 2
    assert all(not model.id.endswith(":free") for model in cheap + monitor)

    monitor_shape = model_to_openrouter_shape(MODELS[MONITOR_MODEL_ID])
    assert model_to_openrouter_shape(MODELS[ZDR_MODEL_ID])["trustedrouter"][
        "route_kind"
    ] == "zdr_pool"
    assert model_to_openrouter_shape(MODELS[EU_MODEL_ID])["trustedrouter"][
        "route_kind"
    ] == "eu_pool"
    assert model_to_openrouter_shape(MODELS[E2E_MODEL_ID])["trustedrouter"][
        "route_kind"
    ] == "e2e_pool"
    assert monitor_shape["trustedrouter"]["route_kind"] == "synthetic_monitor_pool"
    assert monitor_shape["trustedrouter"]["synthetic_monitor"] is True
    assert monitor_shape["trustedrouter"]["auto_candidates"]


def test_monitor_alias_expands_to_paid_rollover_candidates() -> None:
    candidates = chat_route_candidates(
        {"model": MONITOR_MODEL_ID},
        Settings(environment="test"),
    )

    assert len(candidates) >= 4
    # Lead with models that reliably put PONG in visible message.content.
    # DeepSeek V4 Flash is cheaper, but can burn a tiny monitor response budget
    # on hidden reasoning and return an empty visible message.
    assert [candidate.id for candidate in candidates[:4]] == [
        "openai/gpt-4.1-mini",
        "mistralai/mistral-small-2603",
        "google/gemini-2.5-flash",
        "anthropic/claude-haiku-4.5",
    ]
    assert all(not candidate.id.endswith(":free") for candidate in candidates)
    # Reasoning-by-default models (DeepSeek, kimi-k2.6, glm-4.6) are kept in the
    # rollover tail but not at the head, so the steady-state probe
    # path is reasoning-content-free and pong_mismatch noise stays low.
    head = [c.id for c in candidates[:4]]
    assert "deepseek/deepseek-v4-flash" not in head
    assert "moonshotai/kimi-k2.6" not in head
    assert "z-ai/glm-4.6" not in head


def test_monitor_alias_is_marked_internal_only() -> None:
    shape = model_to_openrouter_shape(MODELS[MONITOR_MODEL_ID])

    assert shape["trustedrouter"]["internal_only"] is True
    assert shape["trustedrouter"]["synthetic_monitor"] is True


def test_monitor_alias_is_hidden_from_public_v1_models(client: TestClient) -> None:
    """The synthetic-monitor pool is a system-internal routing target
    that user-facing clients (chat playground, third-party SDKs) must
    never see. The catalog marks it `internal_only: true` — the
    /v1/models endpoint must filter on that flag."""
    response = client.get("/v1/models")
    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()["data"]}
    assert MONITOR_MODEL_ID not in ids
    # The other meta-models stay user-visible.
    assert FREE_MODEL_ID in ids
    assert CHEAP_MODEL_ID in ids
    assert ZDR_MODEL_ID in ids
    assert E2E_MODEL_ID in ids


def test_models_count_excludes_internal_only(client: TestClient) -> None:
    response = client.get("/v1/models/count")
    full_count = response.json()["data"]["count"]
    list_count = len(client.get("/v1/models").json()["data"])
    assert full_count == list_count


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
        _sample(
            id="syn_gateway_canonical",
            probe_type="gateway_cold_path",
            status="up",
            gateway_processing_milliseconds=0,
            latency_milliseconds=20,
        ),
        _sample(
            id="syn_gateway_direct",
            target="us-central1",
            probe_type="gateway_cold_path",
            status="up",
            gateway_processing_milliseconds=0,
            latency_milliseconds=10,
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
    assert "In region gateway overhead p50" in page.text
    assert "Global endpoint" in page.text
    assert "US Central direct" in page.text
    assert "&lt;1 ms" in page.text
    assert "Error-Budget Burn" not in page.text
    assert "last 48 hour uptime history" in page.text
    text = status.text
    # Probe prompts must not leak to the public status surface.
    assert "reply exactly PONG" not in text
    assert "Respond with only the word PONG" not in text
    assert "sk-tr-" not in text
    payload = status.json()["data"]
    provider_sample = next(
        sample for sample in payload["samples"] if sample["probe_type"] == "openai_sdk_pong"
    )
    assert provider_sample["output_match"] is True
    assert payload["components"][0]["name"] == "Canonical API"
    assert len(payload["components"][0]["history"]) == 48
    assert all(
        "latency_breakdown" not in bucket
        for component in payload["components"]
        for bucket in component["history"]
    )
    assert payload["monitor_freshness"]["is_stale"] is False


def test_status_snapshot_calls_out_stale_monitor_data() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_stale_tls",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(minutes=12)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=25,
        )
    ]

    snapshot = status_snapshot(samples, now=now)

    assert snapshot["overall_status"] == "unknown"
    assert snapshot["summary"]["headline"] == "Monitor Data Stale"
    assert snapshot["monitor_freshness"]["is_stale"] is True
    assert snapshot["monitor_freshness"]["latest_sample_age_seconds"] >= 12 * 60


def test_status_snapshot_reports_cold_and_reused_latency_anatomy_without_affecting_slo() -> None:
    now = utcnow()
    created_at = (now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    samples = [
        _sample(
            id="tls",
            probe_type="tls_health",
            status="up",
            latency_milliseconds=57,
            created_at=created_at,
        ),
        _sample(
            id="cold",
            probe_type="gateway_cold_path",
            status="up",
            latency_milliseconds=57,
            dns_milliseconds=3,
            tcp_connect_milliseconds=12,
            tls_handshake_milliseconds=26,
            gateway_processing_milliseconds=1,
            created_at=created_at,
        ),
        _sample(
            id="reused",
            probe_type="gateway_reused_path",
            status="up",
            latency_milliseconds=15,
            gateway_processing_milliseconds=1,
            created_at=created_at,
        ),
    ]

    snapshot = status_snapshot(samples, now=now)
    anatomy = {
        row["probe_type"]: row for row in snapshot["headline_metrics"]["latency_anatomy"]
    }

    assert anatomy["gateway_cold_path"]["p50_dns_milliseconds"] == 3
    assert anatomy["gateway_cold_path"]["p50_tls_handshake_milliseconds"] == 26
    assert anatomy["gateway_reused_path"]["p50_latency_milliseconds"] == 15
    assert snapshot["windows"]["5m"]["sample_count"] == 1


def test_gateway_latency_anatomy_distinguishes_global_and_direct_targets() -> None:
    now = utcnow()
    created_at = (now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    samples = [
        _sample(
            id="global",
            target="canonical",
            target_region="us-central1",
            monitor_region="us-central1",
            probe_type="gateway_reused_path",
            status="up",
            latency_milliseconds=20,
            created_at=created_at,
        ),
        _sample(
            id="direct",
            target="us-central1",
            target_region="us-central1",
            monitor_region="us-central1",
            probe_type="gateway_reused_path",
            status="up",
            latency_milliseconds=10,
            created_at=created_at,
        ),
        _sample(
            id="sao-paulo",
            target="southamerica-east1",
            target_region="southamerica-east1",
            monitor_region="us-central1",
            probe_type="gateway_reused_path",
            status="up",
            latency_milliseconds=120,
            created_at=created_at,
        ),
    ]

    rows = status_snapshot(samples, now=now)["headline_metrics"]["latency_anatomy"]
    labels = {row["target"]: row["route_label"] for row in rows}

    assert labels == {
        "canonical": "Global endpoint · us-central1 -> us-central1",
        "us-central1": "US Central direct · us-central1 -> us-central1",
        "southamerica-east1": (
            "São Paulo direct · us-central1 -> southamerica-east1"
        ),
    }


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


def test_public_response_cache_is_bounded() -> None:
    import trusted_router.routes.public as public_routes

    with public_routes._STATUS_RESPONSE_CACHE_LOCK:
        public_routes._STATUS_RESPONSE_CACHE.clear()
        public_routes._STATUS_RESPONSE_REFRESHING.clear()
    try:
        for index in range(public_routes.PUBLIC_RESPONSE_CACHE_MAX_ENTRIES + 5):
            public_routes._cached_public_response(
                Settings(environment="local"),
                key=f"bounded-cache-{index}",
                media_type="application/json",
                ttl_seconds=60,
                stale_seconds=300,
                background_tasks=BackgroundTasks(),
                build=lambda index=index: f"payload-{index}".encode(),
            )

        with public_routes._STATUS_RESPONSE_CACHE_LOCK:
            assert (
                len(public_routes._STATUS_RESPONSE_CACHE)
                == public_routes.PUBLIC_RESPONSE_CACHE_MAX_ENTRIES
            )
            assert "bounded-cache-0" not in public_routes._STATUS_RESPONSE_CACHE
            assert (
                f"bounded-cache-{public_routes.PUBLIC_RESPONSE_CACHE_MAX_ENTRIES + 4}"
                in public_routes._STATUS_RESPONSE_CACHE
            )
    finally:
        with public_routes._STATUS_RESPONSE_CACHE_LOCK:
            public_routes._STATUS_RESPONSE_CACHE.clear()
            public_routes._STATUS_RESPONSE_REFRESHING.clear()


def test_status_cache_host_collapses_untrusted_host_headers() -> None:
    import trusted_router.routes.public as public_routes

    settings = Settings(environment="local", trusted_domain="trustedrouter.com")

    assert (
        public_routes._status_render_host(settings, "STATUS.TRUSTEDROUTER.COM:443")
        == "status.trustedrouter.com"
    )
    assert (
        public_routes._status_render_host(settings, "attacker-controlled.example")
        == "trustedrouter.com"
    )


def test_stale_public_refreshes_are_concurrency_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.routes.public as public_routes

    started = threading.Event()
    unblock = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(
        public_routes,
        "_STATUS_RESPONSE_REFRESH_SLOTS",
        threading.BoundedSemaphore(1),
    )

    def blocking_build() -> bytes:
        started.set()
        assert unblock.wait(timeout=2)
        finished.set()
        return b"refreshed"

    public_routes._schedule_cached_response_refresh(
        key="refresh-first",
        media_type="text/plain",
        cache_control="public, max-age=1",
        build=blocking_build,
        background_tasks=BackgroundTasks(),
    )
    assert started.wait(timeout=2)
    public_routes._schedule_cached_response_refresh(
        key="refresh-second",
        media_type="text/plain",
        cache_control="public, max-age=1",
        build=lambda: b"should-not-run",
        background_tasks=BackgroundTasks(),
    )

    with public_routes._STATUS_RESPONSE_CACHE_LOCK:
        assert public_routes._STATUS_RESPONSE_REFRESHING == {"refresh-first"}
    unblock.set()
    assert finished.wait(timeout=2)
    for _ in range(100):
        with public_routes._STATUS_RESPONSE_CACHE_LOCK:
            if not public_routes._STATUS_RESPONSE_REFRESHING:
                break
        time.sleep(0.01)
    with public_routes._STATUS_RESPONSE_CACHE_LOCK:
        assert not public_routes._STATUS_RESPONSE_REFRESHING
        public_routes._STATUS_RESPONSE_CACHE.pop("refresh-first", None)


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
    assert "48 hour status history" in history.text
    assert "Router Core latency and availability are broken out" in history.text
    assert "48 hour component timeline" in history.text
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
    assert "Precomputed Router Core reliability" in history.text
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
    assert payload["windows"]["48h"]["sample_count"] == 1
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
    assert "99.99%" in page.text
    assert "Availability target" in page.text
    assert "Provider Effective" not in page.text


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

    # This deployment runs pongs (monitor key set), so the Model Inference
    # component is published and the two down pongs must show.
    snapshot = status_snapshot(
        samples,
        now=now,
        settings=Settings(environment="test", synthetic_monitor_api_key="sk-tr-monitor-test"),
    )

    assert snapshot["current"]["checks"]
    # Both pong samples above are down and recent, so the Model Inference
    # component pulls the banner off green — while router_core (the SLO)
    # stays up because pong failures never burn it.
    assert snapshot["overall_status"] == "degraded"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"
    assert set(snapshot["slo_classes"]) == {"router_core", "control_plane"}
    assert snapshot["history_scope"] == "router_core"
    assert snapshot["windows"]["5m"]["sample_count"] == 1
    assert snapshot["windows"]["24h"]["sample_count"] == 1
    assert snapshot["windows"]["48h"]["sample_count"] == 1
    assert sum(row["sample_count"] for row in snapshot["daily"]) == 1
    assert snapshot["headline_metrics"]["gateway_overhead_p50_milliseconds"] == 25
    assert snapshot["headline_metrics"]["gateway_overhead_scope"] == "in_region"
    canonical = next(
        component for component in snapshot["components"] if component["id"] == "canonical_api"
    )
    assert canonical["status"] == "up"
    assert canonical["uptime_24h_percent"] == pytest.approx(100.0)
    assert canonical["p50_latency_milliseconds"] == 25
    assert canonical["end_to_end_p50_latency_milliseconds"] == 25
    assert len(canonical["history"]) == 48
    # The two recent pong failures now surface on the incident timeline,
    # attributed to the Model Inference component (pre-2026-08 they mapped
    # to no component and were silently dropped here).
    assert {event["id"] for event in snapshot["recent_events"]} == {"syn_down", "syn_down_2"}
    assert all(event["component"] == "Model Inference" for event in snapshot["recent_events"])


def test_status_keeps_provider_failures_out_of_global_slo_classes() -> None:
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

    snapshot = status_snapshot(
        samples,
        now=now,
        settings=Settings(environment="test", synthetic_monitor_api_key="sk-tr-monitor-test"),
    )

    # Provider-effective failures stay out of the SLO math (July decision),
    # but since 2026-08 they are no longer allowed to hide: the Model
    # Inference component goes down and pulls the banner to degraded.
    assert snapshot["overall_status"] == "degraded"
    assert snapshot["summary"]["headline"] == "Partial Outage: Model Inference"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"
    assert snapshot["slo_classes"]["router_core"]["windows"]["5m"]["bad_count"] == 0
    assert set(snapshot["slo_classes"]) == {"router_core", "control_plane"}
    assert {
        row["probe_type"] for row in snapshot["current"]["checks"]
    } == {
        "tls_health",
        "gateway_authorize_settle",
        "provider_fallback",
    }
    assert all(
        alert["slo_class"] != "provider_effective"
        for alert in snapshot["burn_rate_alerts"]
    )
    canonical = next(
        row for row in snapshot["components"] if row["id"] == "canonical_api"
    )
    assert canonical["status"] == "up"
    assert canonical["sample_count_24h"] == 1
    assert snapshot["windows"]["5m"]["sample_count"] == 3
    # Pong failures stay out of the SLO windows above but are no longer
    # hidden from the incident timeline: they surface as Model Inference
    # component events.
    pong_events = [
        event
        for event in snapshot["recent_events"]
        if event["probe_type"] in {"openai_sdk_pong", "responses_pong"}
    ]
    assert len(pong_events) == 2
    assert all(event["component"] == "Model Inference" for event in pong_events)


def test_regional_gateway_maintenance_never_reduces_global_router_core_slo() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_canonical_up",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_eu_tls_down",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
            status="down",
            created_at=(now - dt.timedelta(seconds=11)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_eu_attestation_down",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="attestation_nonce",
            status="trust_degraded",
            created_at=(now - dt.timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    snapshot = status_snapshot(samples, now=now)

    router_core = snapshot["slo_classes"]["router_core"]
    assert router_core["status"] == "up"
    assert router_core["windows"]["5m"]["sample_count"] == 1
    assert router_core["windows"]["5m"]["uptime_percent"] == 100.0
    eu_component = next(
        row for row in snapshot["components"] if row["id"] == "eu_regional_api"
    )
    assert eu_component["status"] == "trust_degraded"


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
    assert alert["burn_rate"] >= 10_000
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
    now = dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.UTC)
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


def test_historical_provider_pong_rollups_never_blend_into_router_core() -> None:
    now = utcnow()
    tls = _sample(
        id="syn_router_tls",
        probe_type="tls_health",
        status="up",
        created_at=(now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=25,
    )
    provider_pong = _sample(
        id="syn_provider_pong",
        probe_type="openai_sdk_pong",
        status="down",
        created_at=(now - dt.timedelta(hours=2, seconds=1))
        .isoformat()
        .replace("+00:00", "Z"),
        error_type="provider_error",
    )
    rollups = _rollups_for_samples([tls])
    rollups.extend(
        new_rollup_for_sample(
            provider_pong,
            period=period,
            component="canonical_api",
        )
        for period in ("hour", "day", "month")
    )

    snapshot = status_snapshot([], rollups=rollups, now=now)

    assert snapshot["windows"]["24h"]["sample_count"] == 1
    assert snapshot["windows"]["24h"]["uptime_percent"] == 100.0
    canonical = next(
        row for row in snapshot["components"] if row["id"] == "canonical_api"
    )
    assert canonical["sample_count_24h"] == 1
    assert sum(bucket["sample_count"] for bucket in canonical["history"]) == 1


def test_historical_attestation_rollup_counts_once_in_router_core() -> None:
    now = utcnow()
    attestation = _sample(
        id="syn_attestation_once",
        probe_type="attestation_nonce",
        status="up",
        created_at=(now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        latency_milliseconds=40,
    )

    snapshot = status_snapshot([], rollups=_rollups_for_samples([attestation]), now=now)

    assert snapshot["windows"]["24h"]["sample_count"] == 1
    assert snapshot["windows"]["24h"]["uptime_percent"] == 100.0
    assert sum(row["sample_count"] for row in snapshot["daily"]) == 1


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
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_eu",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
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


def test_status_tracks_image_generation_without_changing_slo_classes() -> None:
    now = utcnow()
    image_sample = _sample(
        id="syn_image",
        target="canonical",
        target_region="us-central1",
        monitor_region="us-central1",
        probe_type="image_generation",
        status="up",
        created_at=(now - dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        model=IMAGE_GENERATION_MODEL,
        provider=IMAGE_GENERATION_PROVIDER,
        latency_milliseconds=12_000,
    )

    snapshot = status_snapshot([image_sample], now=now)
    components = {component["id"]: component for component in snapshot["components"]}

    assert components["image_generation"]["status"] == "up"
    assert components["image_generation"]["sample_count_24h"] == 1
    assert all(
        slo["windows"]["24h"]["sample_count"] == 0
        for slo in snapshot["slo_classes"].values()
    )


def test_image_generation_component_stays_current_between_six_hour_checks() -> None:
    now = utcnow()
    image_sample = _sample(
        id="syn_image_between_checks",
        target="canonical",
        probe_type="image_generation",
        status="up",
        created_at=(now - dt.timedelta(hours=6, minutes=30))
        .isoformat()
        .replace("+00:00", "Z"),
    )

    snapshot = status_snapshot([image_sample], now=now)
    component = next(
        row for row in snapshot["components"] if row["id"] == "image_generation"
    )

    assert component["status"] == "up"
    stale_snapshot = status_snapshot(
        [image_sample],
        now=now + dt.timedelta(minutes=31),
    )
    stale_component = next(
        row for row in stale_snapshot["components"] if row["id"] == "image_generation"
    )
    assert stale_component["status"] == "unknown"


def test_image_generation_component_uses_fresh_rollup_when_raw_window_is_empty() -> None:
    now = utcnow()
    image_sample = _sample(
        id="syn_image_rollup_only",
        target="canonical",
        probe_type="image_generation",
        status="up",
        created_at=(now - dt.timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
    )
    rollup = new_rollup_for_sample(
        image_sample,
        period="hour",
        component="image_generation",
    )

    snapshot = status_snapshot([], rollups=[rollup], now=now)
    component = next(
        row for row in snapshot["components"] if row["id"] == "image_generation"
    )

    assert component["status"] == "up"
    assert component["last_checked_at"] == image_sample.created_at

    stale_snapshot = status_snapshot(
        [],
        rollups=[rollup],
        now=now + dt.timedelta(hours=4),
    )
    stale_component = next(
        row for row in stale_snapshot["components"] if row["id"] == "image_generation"
    )
    assert stale_component["status"] == "unknown"


def test_recent_events_use_rollups_for_failures_outside_live_sample_window() -> None:
    now = utcnow()
    failed = _sample(
        id="syn_image_historical_failure",
        target="canonical",
        probe_type="image_generation",
        status="down",
        created_at=(now - dt.timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        error_type="invalid_image_payload",
    )

    snapshot = status_snapshot([], rollups=_rollups_for_samples([failed]), now=now)

    assert len(snapshot["recent_events"]) == 1
    event = snapshot["recent_events"][0]
    assert event["component"] == "Image Generation"
    assert event["error_type"] == "invalid_image_payload"
    assert event["aggregate"] is True
    assert event["failure_count"] == 1
    assert event["sample_count"] == 1


def test_recent_events_do_not_duplicate_live_failure_and_its_rollup() -> None:
    now = utcnow()
    failed = _sample(
        id="syn_live_failure",
        target="canonical",
        probe_type="image_generation",
        status="down",
        created_at=(now - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        error_type="invalid_image_payload",
    )

    snapshot = status_snapshot(
        [failed],
        rollups=_rollups_for_samples([failed]),
        now=now,
    )

    assert [event["id"] for event in snapshot["recent_events"]] == [failed.id]
    assert snapshot["recent_events"][0]["aggregate"] is False


def test_status_component_current_uses_latest_sample_per_probe() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_old_down_1",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
            status="down",
            created_at=(now - dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_old_down_2",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
            status="down",
            created_at=(now - dt.timedelta(minutes=2, seconds=10))
            .isoformat()
            .replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_latest_up",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
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


def test_synthetic_rollup_retains_latency_phase_histograms() -> None:
    sample = _sample(
        id="syn_latency_phases",
        probe_type="gateway_cold_path",
        status="up",
        latency_milliseconds=57,
        dns_milliseconds=3,
        tcp_connect_milliseconds=12,
        tls_handshake_milliseconds=26,
        gateway_processing_milliseconds=1,
    )

    rollup = new_rollup_for_sample(sample, period="hour", component="uncategorized")

    assert rollup.latency_histogram == {"57": 1}
    assert rollup.dns_histogram == {"3": 1}
    assert rollup.tcp_connect_histogram == {"12": 1}
    assert rollup.tls_handshake_histogram == {"26": 1}
    assert rollup.gateway_processing_histogram == {"1": 1}


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
    assert table.read_filters[-1] == "CellsColumnLimitFilter"


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
    now = utcnow()
    created_at = now.isoformat().replace("+00:00", "Z")
    date = created_at[:10]
    sample = _sample(
        id="syn_1",
        probe_type="tls_health",
        status="up",
        created_at=created_at,
    )
    table = _FakeBigtable([_FakeReadRow(sample)])

    rows = _bt_synthetic_probe_samples(
        table,
        "m",
        date=date,
        target="canonical",
        probe_type="tls_health",
        monitor_region=None,
        limit=5,
    )

    assert [row.id for row in rows] == ["syn_1"]
    assert table.reads == [
        (
            f"synthetic_day#{date}#canonical#tls_health#".encode(),
            f"synthetic_day#{date}#canonical#tls_health#~".encode(),
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
    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=transport) as client:
        health = await tls_health_probe(client, target, monitor_region="us-central1")
        attestation = await attestation_nonce_probe(client, target, monitor_region="us-central1")
        async with _monitor_sdk(client, target) as sdk:
            chat = await openai_chat_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )
            responses = await responses_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )

    assert health.status == "up"
    assert attestation.status == "up"
    assert chat.status == "up"
    assert chat.output_match is True
    assert responses.status == "up"
    assert responses.output_match is True


@pytest.mark.asyncio
async def test_attestation_http_error_is_availability_failure_not_format_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "revision starting"}})

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await attestation_nonce_probe(client, target, monitor_region="europe-west4")

    assert sample.status == "down"
    assert sample.http_status == 500
    assert sample.error_type == "attestation_http_500"


@pytest.mark.asyncio
async def test_image_generation_probe_validates_binary_and_records_only_metadata() -> None:
    image = b"\xff\xd8\xff" + (b"\x00" * 2048) + b"\xff\xd9"
    data_url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-image",
                "model": IMAGE_GENERATION_MODEL,
                "choices": [{"message": {"content": data_url}}],
                "trustedrouter": {
                    "routing": {
                        "selected_provider": IMAGE_GENERATION_PROVIDER,
                        "selected_model": IMAGE_GENERATION_MODEL,
                    }
                },
                "usage": {
                    "cost_microdollars": 88_207,
                    "provider_usage": {"generation_id": "gen-image"},
                },
            },
        )

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await image_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
        )

    assert sample.status == "up"
    assert sample.output_match is True
    assert sample.selected_provider == IMAGE_GENERATION_PROVIDER
    assert sample.selected_model == IMAGE_GENERATION_MODEL
    assert sample.generation_id == "gen-image"
    assert sample.cost_microdollars == 88_207
    assert requests == [
        {
            "model": IMAGE_GENERATION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Generate and return an actual square image now, not a textual "
                        "description. Show one solid red circle centered on a white "
                        "background."
                    ),
                }
            ],
            "provider": {
                "only": [IMAGE_GENERATION_PROVIDER],
                "allow_fallbacks": False,
            },
            "max_tokens": 2048,
            "metadata": {
                "trustedrouter_synthetic": "true",
                "probe": "image_generation",
            },
        }
    ]
    public = json.dumps(sample.public_dict())
    assert "solid red circle" not in public
    assert data_url not in public
    assert "base64" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("plain text instead of an image", "invalid_image_payload"),
        ("data:image/jpeg;base64,not-base64!", "invalid_image_payload"),
        (
            "data:image/jpeg;base64,"
            + base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 2048).decode(),
            "invalid_image_payload",
        ),
    ],
)
async def test_image_generation_probe_rejects_missing_or_invalid_images(
    content: str,
    expected_error: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await image_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
        )

    assert sample.status == "down"
    assert sample.output_match is False
    assert sample.error_type == expected_error


@pytest.mark.asyncio
async def test_image_generation_probe_does_not_copy_provider_error_body() -> None:
    provider_error_marker = "provider response body must not be retained"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"message": provider_error_marker}})

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await image_generation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
        )

    assert sample.status == "down"
    assert sample.error_type == "image_generation_http_error"
    assert provider_error_marker not in json.dumps(sample.public_dict())


@pytest.mark.asyncio
async def test_pong_probe_accepts_reasoning_model_shapes() -> None:
    """Reasoning models (kimi-k2.6, glm-4.6, deepseek-v4) sometimes
    emit the visible answer inside `reasoning_content` while
    `message.content` arrives empty (or as a list of parts). Before
    this regression test the probe flagged `pong_mismatch` on every
    such reply even though the model actually responded with PONG.

    Status-page root cause from 2026-05-31: per-provider health was
    degraded by pong_mismatch on these models even though they returned
    the requested answer.
    """

    def chat_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": (
                                    "The user asked for PONG. I'll respond: PONG."
                                ),
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    def chat_list_handler(request: httpx.Request) -> httpx.Response:
        # Anthropic / multimodal-adapter list-of-parts shape
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "PONG"}
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    def responses_handler(request: httpx.Request) -> httpx.Response:
        # /responses with a reasoning item BEFORE the message item.
        if request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "reasoning",
                            "summary": [
                                {"text": "Need to reply with PONG."}
                            ],
                        },
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "PONG"}],
                        },
                    ]
                },
            )
        return httpx.Response(404)

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(chat_handler)) as client:
        async with _monitor_sdk(client, target) as sdk:
            chat = await openai_chat_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )
    assert chat.status == "up", chat.error_type
    assert chat.output_match is True

    async with httpx.AsyncClient(transport=httpx.MockTransport(chat_list_handler)) as client:
        async with _monitor_sdk(client, target) as sdk:
            chat_list = await openai_chat_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )
    assert chat_list.status == "up", chat_list.error_type
    assert chat_list.output_match is True

    async with httpx.AsyncClient(transport=httpx.MockTransport(responses_handler)) as client:
        async with _monitor_sdk(client, target) as sdk:
            responses = await responses_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )
    assert responses.status == "up", responses.error_type
    assert responses.output_match is True


@pytest.mark.asyncio
async def test_pong_probe_still_catches_real_mismatches() -> None:
    """Belt-and-suspenders: the more-permissive extractor should NOT
    upgrade a genuinely-wrong reply to pass."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "I'm sorry, I can't help with that."}}
                    ]
                },
            )
        return httpx.Response(404)

    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with _monitor_sdk(client, target) as sdk:
            chat = await openai_chat_pong_probe(
                sdk, target, monitor_region="us-central1", model=MONITOR_MODEL_ID
            )

    assert chat.status == "down"
    assert chat.output_match is False
    assert chat.error_type == "pong_mismatch"


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
    target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1")
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


def test_synthetic_gateway_settlement_metadata_does_not_pollute_provider_benchmarks(
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
    fallback = data["route_candidates"][0]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": data["authorization_id"],
            "input_tokens": 1,
            "output_tokens": 1,
            "request_id": "req_synthetic_metadata",
            "model": fallback["model"],
            "selected_endpoint": fallback["endpoint_id"],
            "metadata": {"trustedrouter_synthetic": "true"},
        },
    )

    assert settle.status_code == 200, settle.text
    assert STORE.activity_events(data["workspace_id"], limit=10)
    assert STORE.provider_benchmark_samples() == []


def test_synthetic_gateway_refund_metadata_does_not_pollute_provider_benchmarks(
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
    selected = data["route_candidates"][0]

    refund = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": data["authorization_id"],
            "input_tokens": 1,
            "output_tokens": 1,
            "request_id": "req_synthetic_refund_metadata",
            "model": selected["model"],
            "selected_endpoint": selected["endpoint_id"],
            "metadata": {"trustedrouter_synthetic": "true"},
            "error_status": 502,
            "error_type": "provider_error",
        },
    )

    assert refund.status_code == 200, refund.text
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
    provider: str | None = None,
    output_match: bool | None = None,
    created_at: str | None = None,
    latency_milliseconds: int | None = None,
    dns_milliseconds: int | None = None,
    tcp_connect_milliseconds: int | None = None,
    tls_handshake_milliseconds: int | None = None,
    gateway_processing_milliseconds: int | None = None,
    error_type: str | None = None,
    http_status: int | None = None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=id,
        probe_type=probe_type,
        target=target,
        target_url="https://api.trustedrouter.com/v1",
        monitor_region=monitor_region,
        target_region=target_region,
        status=status,
        model=model,
        provider=provider,
        output_match=output_match,
        latency_milliseconds=latency_milliseconds,
        dns_milliseconds=dns_milliseconds,
        tcp_connect_milliseconds=tcp_connect_milliseconds,
        tls_handshake_milliseconds=tls_handshake_milliseconds,
        gateway_processing_milliseconds=gateway_processing_milliseconds,
        error_type=error_type,
        http_status=http_status,
        created_at=created_at or iso_now(),
    )


def _jwt(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{body}.signature".encode()


def test_configured_targets_include_primary_regional_gateway() -> None:
    from trusted_router.synthetic.probes import configured_targets

    settings = Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
        regions="us-central1,us-east4,europe-west4,southamerica-east1",
        primary_region="us-central1",
    )

    targets = configured_targets(settings)
    by_name = {target.name: target for target in targets}

    assert by_name["canonical"].api_base_url == "https://api.trustedrouter.com/v1"
    assert by_name["canonical"].region == "us-central1"
    assert by_name["us-central1"].api_base_url == (
        "https://api-us-central1.quillrouter.com/v1"
    )
    assert by_name["us-central1"].region == "us-central1"
    assert by_name["us-east4"].api_base_url == "https://api-us-east4.quillrouter.com/v1"
    assert by_name["europe-west4"].api_base_url == (
        "https://api-europe-west4.quillrouter.com/v1"
    )
    assert by_name["southamerica-east1"].api_base_url == (
        "https://api-southamerica-east1.quillrouter.com/v1"
    )


def test_status_components_include_all_warm_regional_gateways() -> None:
    now = utcnow()
    samples = [
        _sample(
            id="syn_us_central",
            target="us-central1",
            target_region="us-central1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_us_east",
            target="us-east4",
            target_region="us-east4",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_eu",
            target="europe-west4",
            target_region="europe-west4",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
        _sample(
            id="syn_sa",
            target="southamerica-east1",
            target_region="southamerica-east1",
            probe_type="tls_health",
            status="up",
            created_at=(now - dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    components = {row["id"]: row for row in status_snapshot(samples, now=now)["components"]}

    assert components["us_central1_regional_api"]["status"] == "up"
    assert components["us_east4_regional_api"]["status"] == "up"
    assert components["eu_regional_api"]["status"] == "up"
    assert components["sa_regional_api"]["status"] == "up"


@pytest.mark.asyncio
async def test_run_synthetic_once_fans_out_targets_and_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.synthetic import probes as probe_module

    targets = [
        SyntheticTarget("canonical", "https://api.trustedrouter.com/v1", "us-central1"),
        SyntheticTarget("us-east4", "https://api-us-east4.quillrouter.com/v1", "us-east4"),
        SyntheticTarget(
            "europe-west4",
            "https://api-europe-west4.quillrouter.com/v1",
            "europe-west4",
            "https://trusted-router-control-eu.example",
        ),
    ]

    # Fan-out is proven STRUCTURALLY (mutual handshake), not by wall clock:
    # the old `elapsed < 0.18` bound flaked on CI at 0.1802s. Every unmetered
    # probe registers itself and then WAITS until all of them are in flight at
    # once, so a serialized implementation parks the first probe forever and
    # asyncio.wait_for fails the test deterministically, while a concurrent one
    # releases every probe the instant the last one arrives. There is no timing
    # margin left to erode.
    #
    # Ten unmetered coroutines: tls_health + attestation_nonce +
    # gateway_latency_phase_probes on each of the 3 targets, plus the single
    # control-plane probe on the one target that configures a control plane URL.
    # The six credit-bearing probes are deliberately EXCLUDED from the
    # handshake: DEFAULT_SYNTHETIC_BILLING_CONCURRENCY caps them at 2 in flight
    # (pinned by test_run_synthetic_once_bounds_credit_bearing_probe_concurrency),
    # so making them join a ten-way rendezvous would deadlock by design.
    unmetered_probe_count = 10
    in_flight = 0
    peak_in_flight = 0
    all_in_flight = asyncio.Event()

    async def rendezvous() -> None:
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        if in_flight >= unmetered_probe_count:
            all_in_flight.set()
        await asyncio.wait_for(all_in_flight.wait(), timeout=5)
        in_flight -= 1

    def fake_probe(probe_type: str, *, unmetered: bool = True) -> Any:
        async def run(
            _client: httpx.AsyncClient,
            target: SyntheticTarget,
            *,
            monitor_region: str,
            **_kwargs: Any,
        ) -> SyntheticProbeSample:
            if unmetered:
                await rendezvous()
            return _sample(
                id=f"{probe_type}-{target.name}",
                probe_type=probe_type,
                status="up",
                target=target.name,
                target_region=target.region,
                monitor_region=monitor_region,
            )

        return run

    monkeypatch.setattr(probe_module, "configured_targets", lambda _settings: targets)
    monkeypatch.setattr(probe_module, "tls_health_probe", fake_probe("tls_health"))
    monkeypatch.setattr(probe_module, "attestation_nonce_probe", fake_probe("attestation_nonce"))

    async def fake_phase_probes(
        target: SyntheticTarget,
        *,
        monitor_region: str,
        **_kwargs: Any,
    ) -> list[SyntheticProbeSample]:
        await rendezvous()
        return [
            _sample(
                id=f"{probe_type}-{target.name}",
                probe_type=probe_type,
                status="up",
                target=target.name,
                target_region=target.region,
                monitor_region=monitor_region,
            )
            for probe_type in ("gateway_cold_path", "gateway_reused_path")
        ]

    monkeypatch.setattr(probe_module, "gateway_latency_phase_probes", fake_phase_probes)
    monkeypatch.setattr(
        probe_module, "control_plane_health_probe", fake_probe("control_plane_health")
    )
    monkeypatch.setattr(
        probe_module, "openai_chat_pong_probe", fake_probe("openai_sdk_pong", unmetered=False)
    )
    monkeypatch.setattr(
        probe_module, "responses_pong_probe", fake_probe("responses_pong", unmetered=False)
    )

    samples = await run_synthetic_once(
        Settings(
            environment="test",
            api_base_url="https://api.trustedrouter.com/v1",
            synthetic_monitor_api_key="sk-tr-test",
        ),
        monitor_region="us-central1",
        api_key="sk-tr-test",
    )

    assert len(samples) == 19
    assert {sample.target for sample in samples} == {"canonical", "us-east4", "europe-west4"}
    # Every unmetered probe across every target was in flight at the same
    # instant. A pass that serialized targets, or serialized probes within a
    # target, could never reach this count — it would have timed out in
    # rendezvous() above rather than reaching this assertion.
    assert peak_in_flight == unmetered_probe_count


@pytest.mark.asyncio
async def test_run_synthetic_once_bounds_credit_bearing_probe_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import probes as probe_module

    targets = [
        SyntheticTarget(f"target-{index}", f"https://api-{index}.example/v1", "test")
        for index in range(4)
    ]
    active = 0
    peak = 0

    async def fake_health_probe(
        _client: httpx.AsyncClient,
        target: SyntheticTarget,
        *,
        monitor_region: str,
        **_kwargs: Any,
    ) -> SyntheticProbeSample:
        return _sample(
            id=f"health-{target.name}",
            probe_type="tls_health",
            status="up",
            target=target.name,
            monitor_region=monitor_region,
        )

    async def fake_billing_probe(
        _client: httpx.AsyncClient,
        target: SyntheticTarget,
        *,
        monitor_region: str,
        **_kwargs: Any,
    ) -> SyntheticProbeSample:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _sample(
            id=f"billing-{target.name}",
            probe_type="openai_sdk_pong",
            status="up",
            target=target.name,
            monitor_region=monitor_region,
        )

    monkeypatch.setattr(probe_module, "configured_targets", lambda _settings: targets)
    monkeypatch.setattr(probe_module, "tls_health_probe", fake_health_probe)
    monkeypatch.setattr(probe_module, "attestation_nonce_probe", fake_health_probe)

    async def no_phase_probes(
        _target: SyntheticTarget,
        *,
        monitor_region: str,
        **_kwargs: Any,
    ) -> list[SyntheticProbeSample]:
        assert monitor_region == "us-central1"
        return []

    monkeypatch.setattr(probe_module, "gateway_latency_phase_probes", no_phase_probes)
    monkeypatch.setattr(probe_module, "openai_chat_pong_probe", fake_billing_probe)
    monkeypatch.setattr(probe_module, "responses_pong_probe", fake_billing_probe)

    samples = await run_synthetic_once(
        Settings(environment="test", synthetic_monitor_api_key="sk-tr-test"),
        monitor_region="us-central1",
        api_key="sk-tr-test",
    )

    assert len(samples) == 16
    assert peak == 2


@pytest.mark.asyncio
async def test_rotation_pass_fans_out_model_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.synthetic import cli as cli_module

    # Concurrency is asserted STRUCTURALLY (peak in-flight), not by wall
    # clock: an elapsed-time bound flaked under contended CI CPU (xdist
    # workers sharing cores pushed 120ms to 128ms). The interleaving of an
    # in-flight counter is deterministic on one event loop — the second
    # probe's increment always runs while the first awaits — so this proves
    # the same fan-out with zero timing sensitivity, and additionally pins
    # the billing budget: peak must be exactly the semaphore's 2.
    active = 0
    peak = 0

    async def fake_rotation_probe(
        _client: httpx.AsyncClient,
        _target: SyntheticTarget,
        *,
        monitor_region: str,
        api_key: str,
        provider: str,
        model: str,
        default_timeout_seconds: float,
    ) -> tuple[str, str, str, str]:
        nonlocal active, peak
        assert monitor_region == "us-central1"
        assert api_key == "sk-tr-test"
        assert default_timeout_seconds == 20.0
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return (provider, model, monitor_region, api_key)

    monkeypatch.setattr(
        cli_module,
        "rotation_candidates",
        lambda: {"provider-a": ["model-a"], "provider-b": ["model-b"]},
    )
    monkeypatch.setattr(cli_module, "provider_rotation_probe", fake_rotation_probe)

    samples = await cli_module.rotation_pass(
        settings=Settings(environment="test", api_base_url="https://api.trustedrouter.com/v1"),
        monitor_region="us-central1",
        api_key="sk-tr-test",
        timeout=httpx.Timeout(1),
        count=4,
        rng=random.Random(0),  # noqa: S311 - deterministic test selection.
    )

    assert len(samples) == 4
    # Parallel within the billing budget, never beyond it.
    assert peak == 2


@pytest.mark.asyncio
async def test_probe_and_rotation_pass_runs_independent_blocks_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    # Overlap is proven by a mutual handshake, not wall clock (an elapsed
    # bound flaked under contended CI CPU): each block signals its start and
    # then WAITS for the other block to have started before it can finish.
    # Serialized execution deadlocks the first block until wait_for's timeout
    # fails the test deterministically; concurrent execution releases both
    # immediately. No timing margin exists to erode.
    probe_started = asyncio.Event()
    rotation_started = asyncio.Event()

    async def fake_one_probe_pass(**_kwargs: Any) -> list[SyntheticProbeSample]:
        probe_started.set()
        await asyncio.wait_for(rotation_started.wait(), timeout=5)
        return [
            _sample(id="tls", probe_type="tls_health", status="up"),
            _sample(id="settle", probe_type="gateway_authorize_settle", status="up"),
        ]

    async def fake_rotation_pass(**_kwargs: Any) -> list[str]:
        rotation_started.set()
        await asyncio.wait_for(probe_started.wait(), timeout=5)
        return ["rotation-a", "rotation-b"]

    monkeypatch.setattr(cli_module, "_one_probe_pass", fake_one_probe_pass)
    monkeypatch.setattr(cli_module, "rotation_pass", fake_rotation_pass)

    samples, rotation_samples = await cli_module._probe_and_rotation_pass(
        settings=Settings(environment="test", api_base_url="https://api.trustedrouter.com/v1"),
        monitor_region="us-central1",
        control_plane="https://trustedrouter.com",
        internal_token="internal",  # noqa: S106 - test placeholder.
        api_key="sk-tr-test",
        timeout=httpx.Timeout(1),
        rotation_enabled=True,
        rotation_per_pass=4,
        rotation_rng=random.Random(0),  # noqa: S311 - deterministic test selection.
    )

    assert [sample.probe_type for sample in samples] == [
        "tls_health",
        "gateway_authorize_settle",
    ]
    assert rotation_samples == ["rotation-a", "rotation-b"]


@pytest.mark.asyncio
async def test_probe_and_rotation_share_one_billing_concurrency_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    active = 0
    peak = 0

    async def bounded_work(semaphore: asyncio.Semaphore) -> None:
        nonlocal active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    async def fake_one_probe_pass(**kwargs: Any) -> list[SyntheticProbeSample]:
        semaphore = kwargs["billing_semaphore"]
        await asyncio.gather(*(bounded_work(semaphore) for _ in range(4)))
        return []

    async def fake_rotation_pass(**kwargs: Any) -> list[ProviderBenchmarkSample]:
        semaphore = kwargs["billing_semaphore"]
        await asyncio.gather(*(bounded_work(semaphore) for _ in range(4)))
        return []

    monkeypatch.setattr(cli_module, "_one_probe_pass", fake_one_probe_pass)
    monkeypatch.setattr(cli_module, "rotation_pass", fake_rotation_pass)

    await cli_module._probe_and_rotation_pass(
        settings=Settings(environment="test"),
        monitor_region="us-central1",
        control_plane="https://trustedrouter.com",
        internal_token="internal",  # noqa: S106 - test placeholder.
        api_key="sk-tr-test",
        timeout=httpx.Timeout(1),
        rotation_enabled=True,
        rotation_per_pass=4,
        rotation_rng=random.Random(0),  # noqa: S311 - deterministic test selection.
        billing_concurrency=2,
    )

    assert peak == 2


@pytest.mark.asyncio
async def test_one_probe_pass_keeps_gateway_accounting_probes_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    events: list[str] = []

    async def fake_run_synthetic_once(*_args: Any, **_kwargs: Any) -> list[SyntheticProbeSample]:
        events.append("synthetic-start")
        await asyncio.sleep(0.05)
        events.append("synthetic-end")
        return [_sample(id="tls", probe_type="tls_health", status="up")]

    async def fake_billing_probe(*_args: Any, **_kwargs: Any) -> list[SyntheticProbeSample]:
        events.append("billing-start")
        await asyncio.sleep(0.03)
        events.append("billing-end")
        return [_sample(id="billing", probe_type="gateway_authorize_settle", status="up")]

    async def fake_fallback_probe(*_args: Any, **_kwargs: Any) -> list[SyntheticProbeSample]:
        assert "billing-end" in events
        events.append("fallback-start")
        await asyncio.sleep(0.03)
        events.append("fallback-end")
        return [_sample(id="fallback", probe_type="provider_fallback", status="up")]

    async def fake_canary_probe(_client: object, **kwargs: object) -> SyntheticProbeSample:
        events.append("canary")
        return SyntheticProbeSample(
            id="syn_canary",
            probe_type="client_telemetry_ingest",
            target="control_plane",
            target_url="https://control.example/v1/client-events",
            monitor_region=str(kwargs["monitor_region"]),
            status="up",
            created_at="2026-08-17T03:00:00Z",
        )

    monkeypatch.setattr(cli_module, "run_synthetic_once", fake_run_synthetic_once)
    monkeypatch.setattr(cli_module, "gateway_billing_probe", fake_billing_probe)
    monkeypatch.setattr(cli_module, "gateway_fallback_probe", fake_fallback_probe)
    monkeypatch.setattr(cli_module, "client_telemetry_canary_probe", fake_canary_probe)

    samples = await cli_module._one_probe_pass(
        settings=Settings(environment="test", api_base_url="https://api.trustedrouter.com/v1"),
        monitor_region="us-central1",
        control_plane="https://trustedrouter.com",
        internal_token="internal",  # noqa: S106 - test placeholder.
        api_key="sk-tr-test",
        timeout=httpx.Timeout(1),
    )

    assert [sample.probe_type for sample in samples] == [
        "tls_health",
        "gateway_authorize_settle",
        "provider_fallback",
        "client_telemetry_ingest",
    ]
    assert events.index("billing-end") < events.index("fallback-start")
    # The canary is not a ledger probe; it runs after the ordered pair.
    assert events.index("fallback-start") < events.index("canary")


@pytest.mark.asyncio
async def test_route_health_caller_logs_flags_and_does_not_fail_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import cli as cli_module

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-trustedrouter-internal-token"] == "internal"
        if request.url.path.endswith("/failure"):
            return httpx.Response(500, json={"error": "unavailable"})
        return httpx.Response(200, json={"data": {"flagged": [{"provider": "dead"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await cli_module._post_route_health(
            client,
            url="https://trustedrouter.com/v1/internal/synthetic/route-health",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )
        await cli_module._post_route_health(
            client,
            url="https://trustedrouter.com/failure",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )

    output = capsys.readouterr()
    assert "route-health flagged: 1" in output.out
    assert "route-health check failed:" in output.err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("minute", "every_pass", "should_post"),
    [
        (1, None, True),
        (2, None, False),
        (37, "1", True),
    ],
)
async def test_route_health_caller_obeys_hourly_gate_and_override(
    monkeypatch: pytest.MonkeyPatch,
    minute: int,
    every_pass: str | None,
    should_post: bool,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    if every_pass is None:
        monkeypatch.delenv("TR_SYNTHETIC_ROUTE_HEALTH_EVERY_PASS", raising=False)
    else:
        monkeypatch.setenv("TR_SYNTHETIC_ROUTE_HEALTH_EVERY_PASS", every_pass)

    posted: list[tuple[str, str]] = []

    async def fake_post_route_health(
        _client: httpx.AsyncClient,
        *,
        url: str,
        internal_token: str,
    ) -> None:
        posted.append((url, internal_token))

    monkeypatch.setattr(cli_module, "_post_route_health", fake_post_route_health)
    now = dt.datetime(2026, 7, 17, 10, minute, tzinfo=dt.UTC)
    async with httpx.AsyncClient() as client:
        await cli_module._post_route_health_if_due(
            client,
            url="https://trustedrouter.com/v1/internal/synthetic/route-health",
            internal_token="internal",  # noqa: S106 - test placeholder.
            now=now,
        )

    expected = [
        (
            "https://trustedrouter.com/v1/internal/synthetic/route-health",
            "internal",
        )
    ]
    assert posted == (expected if should_post else [])


@pytest.mark.asyncio
async def test_remediator_caller_reports_success_and_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import cli as cli_module

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-trustedrouter-internal-token"] == "internal"
        assert request.extensions["timeout"]["read"] == 90.0
        if request.url.path.endswith("/failure"):
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"data": {"decisions": 2}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        succeeded = await cli_module._post_remediator(
            client,
            url="https://trustedrouter.com/v1/internal/synthetic/remediate",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )
        failed = await cli_module._post_remediator(
            client,
            url="https://trustedrouter.com/failure",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )

    output = capsys.readouterr()
    assert succeeded is True
    assert failed is False
    assert "remediator decisions: 2" in output.out
    assert "remediator check failed: HTTPStatusError:" in output.err


@pytest.mark.asyncio
async def test_remediator_caller_treats_only_active_overlap_as_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import cli as cli_module

    def handler(request: httpx.Request) -> httpx.Response:
        message = (
            "Synthetic operation is already in progress"
            if request.url.path.endswith("/overlap")
            else "Synthetic operation rate limit exceeded"
        )
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "message": message,
                    "type": "rate_limited",
                    "source": "router",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        overlap = await cli_module._post_remediator(
            client,
            url="https://trustedrouter.com/overlap",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )
        rate_limited = await cli_module._post_remediator(
            client,
            url="https://trustedrouter.com/rate-limit",
            internal_token="internal",  # noqa: S106 - test placeholder.
        )

    output = capsys.readouterr()
    assert overlap is True
    assert rate_limited is False
    assert "remediator skipped: another pass is already in progress" in output.out
    assert output.err.count("remediator check failed: HTTPStatusError:") == 1


@pytest.mark.asyncio
async def test_primary_synthetic_job_invokes_scheduled_remediator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    seen_urls: list[str] = []
    remediator_started = asyncio.Event()

    async def empty_pass(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        assert kwargs["internal_token"] is None
        # A sequential implementation deadlocks here until the test timeout;
        # the remediator must begin while independent probes are in flight.
        await asyncio.wait_for(remediator_started.wait(), timeout=1.0)
        return [], []

    class _Response:
        status_code = 200
        text = '{"data":{"decisions":0}}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": {"decisions": 0}}

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _Response:
            assert (
                kwargs["headers"]["x-trustedrouter-internal-token"]
                == "observer-only"
            )
            assert kwargs["timeout"].read == 75.0
            seen_urls.append(url)
            remediator_started.set()
            return _Response()

    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
        observer_internal_token="observer-only",  # noqa: S106 - test placeholder.
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "_probe_and_rotation_pass", empty_pass)
    monkeypatch.setattr(cli_module.httpx, "AsyncClient", _Client)
    monkeypatch.setenv(
        "TR_SYNTHETIC_REMEDIATOR_URL",
        "https://trustedrouter.com/v1/internal/synthetic/remediate",
    )
    monkeypatch.setenv("TR_SYNTHETIC_REMEDIATOR_TIMEOUT_SECONDS", "75")
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ONLY", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ENABLED", raising=False)

    assert await cli_module.run() == 0
    assert seen_urls == ["https://trustedrouter.com/v1/internal/synthetic/remediate"]


def test_synthetic_credential_selection_uses_gateway_only_for_combined_bridge() -> None:
    from trusted_router.synthetic.internal_auth import (
        synthetic_observer_token,
        synthetic_transaction_token,
    )

    both = Settings(
        environment="test",
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
        observer_internal_token="observer-only",  # noqa: S106 - test placeholder.
    )
    billing_only = Settings(
        environment="test",
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
    )
    bridged = Settings(
        environment="test",
        service_surface="combined",
        allow_deployed_combined_surface=True,
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
    )

    assert synthetic_observer_token(both) == "observer-only"
    assert synthetic_observer_token(billing_only) is None
    assert synthetic_observer_token(bridged) == "billing-only"
    assert synthetic_transaction_token(both) is None
    assert synthetic_transaction_token(billing_only) is None
    assert synthetic_transaction_token(bridged) == "billing-only"


@pytest.mark.asyncio
async def test_combined_bridge_job_keeps_transaction_probe_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    transaction_tokens: list[str | None] = []

    async def empty_pass(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        transaction_tokens.append(kwargs["internal_token"])
        return [], []

    settings = Settings(
        environment="test",
        service_surface="combined",
        allow_deployed_combined_surface=True,
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "_probe_and_rotation_pass", empty_pass)
    monkeypatch.setenv("TR_SYNTHETIC_RUNS_PER_INVOCATION", "1")
    monkeypatch.delenv("TR_SYNTHETIC_REMEDIATOR_URL", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_ROTATION_ENABLED", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ENABLED", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ONLY", raising=False)

    assert await cli_module.run() == 0
    assert transaction_tokens == ["billing-only"]


@pytest.mark.asyncio
async def test_synthetic_job_ingests_with_observer_token_and_never_runs_ledger_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import cli as cli_module

    ingested: list[dict[str, Any]] = []

    async def fake_run_synthetic_once(
        *_args: Any, **_kwargs: Any
    ) -> list[SyntheticProbeSample]:
        return [_sample(id="tls", probe_type="tls_health", status="up")]

    async def fake_canary(
        _client: httpx.AsyncClient, **kwargs: Any
    ) -> SyntheticProbeSample:
        return _sample(
            id="canary",
            probe_type="client_telemetry_ingest",
            status="up",
            monitor_region=str(kwargs["monitor_region"]),
        )

    async def forbidden_ledger_probe(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("synthetic job attempted a billing gateway probe")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/internal/synthetic/samples"
        assert request.headers["x-trustedrouter-internal-token"] == "observer-only"
        ingested.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"recorded": 2}})

    settings = Settings(
        environment="test",
        service_surface="observer",
        internal_gateway_token="billing-only",  # noqa: S106 - test placeholder.
        observer_internal_token="observer-only",  # noqa: S106 - test placeholder.
        synthetic_monitor_api_key="sk-tr-test",
    )
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "run_synthetic_once", fake_run_synthetic_once)
    monkeypatch.setattr(cli_module, "client_telemetry_canary_probe", fake_canary)
    monkeypatch.setattr(cli_module, "gateway_billing_probe", forbidden_ledger_probe)
    monkeypatch.setattr(cli_module, "gateway_fallback_probe", forbidden_ledger_probe)
    monkeypatch.setattr(cli_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setenv(
        "TR_SYNTHETIC_INGEST_URL",
        "https://trustedrouter.com/v1/internal/synthetic/samples",
    )
    monkeypatch.setenv("TR_SYNTHETIC_RUNS_PER_INVOCATION", "1")
    monkeypatch.delenv("TR_SYNTHETIC_REMEDIATOR_URL", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_ROTATION_ENABLED", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ENABLED", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ONLY", raising=False)

    assert await cli_module.run() == 0
    assert len(ingested) == 1
    assert {sample["probe_type"] for sample in ingested[0]["samples"]} == {
        "tls_health",
        "client_telemetry_ingest",
    }


def test_synthetic_deploy_targets_public_api_and_private_internal_ingest() -> None:
    deploy_script = Path(__file__).resolve().parents[1] / "scripts/deploy/synthetic.sh"
    body = deploy_script.read_text()

    assert '"TR_ENVIRONMENT=worker"' in body
    assert '"TR_ENVIRONMENT=production"' not in body
    assert '"TR_SERVICE_SURFACE=observer"' in body
    assert '"TR_SERVICE_SURFACE=internal"' not in body
    assert (
        '"TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:latest"'
        in body
    )
    assert (
        '"TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"'
        in body
    )
    assert "TR_API_BASE_URL=https://api.trustedrouter.com/v1" in body
    assert "TR_API_BASE_URL=https://api.quillrouter.com/v1" not in body
    assert 'throughput_job_name="trusted-router-throughput-${throughput_region}"' in body
    assert '"TR_SYNTHETIC_THROUGHPUT_ONLY=true"' in body
    assert '"TR_SYNTHETIC_THROUGHPUT_ONLY=false"' in body
    assert '"TR_SYNTHETIC_THROUGHPUT_ENABLED=false"' in body
    assert 'scheduler_name="${job_name}-every-three-minutes"' in body
    assert '"${job_name}-every-minute"' in body
    assert '"${job_name}-every-five-minutes"' in body
    assert '"TR_SYNTHETIC_ROTATION_PER_PASS=2"' in body
    assert '"*/3 * * * *"' in body
    assert '"*/5 * * * *"' in body
    assert 'throughput_scheduler_name="${throughput_job_name}-every-five-minutes"' in body
    assert '"TR_SYNTHETIC_THROUGHPUT_INTERVAL_SECONDS=300"' in body
    assert '"TR_SYNTHETIC_BILLING_CONCURRENCY=2"' in body
    assert '"TR_SYNTHETIC_START_DELAY_SECONDS=$((monitor_index * 20))"' in body
    assert '"TR_SYNTHETIC_START_DELAY_SECONDS=0"' in body
    assert '"TR_SYNTHETIC_THROUGHPUT_TIMEOUT_CEILING_SECONDS=210"' in body
    assert '"${throughput_job_name}-every-minute"' in body
    assert '"${throughput_job_name}-every-two-minutes"' in body
    assert 'image_job_name="trusted-router-image-generation-${image_region}"' in body
    assert (
        'regional_ingest_base="https://${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}.'
        '${monitor_region}.run.app"'
        in body
    )
    assert (
        'SYNTHETIC_INGEST_SERVICE="$TR_BILLING_SERVICE"'
    ) in body
    assert "TR_SYNTHETIC_INGEST_SERVICE" not in body
    assert "TR_BILLING_SERVICE must be separate from legacy SERVICE" in body
    assert (
        '"TR_SYNTHETIC_INGEST_URL=${regional_ingest_base}/v1/internal/synthetic/samples"'
        in body
    )
    assert (
        '"TR_SYNTHETIC_BENCHMARK_INGEST_URL=${regional_ingest_base}/v1/internal/synthetic/benchmark"'
        in body
    )
    assert (
        '"TR_SYNTHETIC_ROUTE_HEALTH_URL=${regional_ingest_base}/v1/internal/synthetic/route-health"'
        in body
    )
    assert (
        '"TR_SYNTHETIC_REMEDIATOR_URL=${regional_ingest_base}/v1/internal/synthetic/remediate"'
        in body
    )
    assert 'if [ "$monitor_region" = "$TR_PRIMARY_REGION" ]' in body
    rollout = (Path(__file__).resolve().parents[1] / "scripts/deploy/rollout.sh").read_text()
    assert '"TR_REMEDIATOR_IN_PROCESS_ENABLED=false"' in rollout
    assert (
        '"TR_SYNTHETIC_INGEST_URL=${throughput_ingest_base}/v1/internal/synthetic/samples"'
        in body
    )
    assert (
        '"TR_SYNTHETIC_INGEST_URL=${image_ingest_base}/v1/internal/synthetic/samples"'
        in body
    )
    assert '"TR_SYNTHETIC_IMAGE_MODEL=google/gemini-3.1-flash-image-preview"' in body
    assert '"TR_SYNTHETIC_IMAGE_PROVIDER=google-ai-studio"' in body
    assert '"TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS=2"' in body
    assert '--args="-m,trusted_router.synthetic.image_generation"' in body
    image_job = body.split("deploying isolated image-generation", maxsplit=1)[1]
    assert "--task-timeout 300s" in image_job
    assert '"17 */6 * * *"' in body


@pytest.mark.asyncio
async def test_image_generation_job_runs_one_probe_and_ingests_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import image_generation as image_job

    image = b"\xff\xd8\xff" + (b"\x00" * 2048) + b"\xff\xd9"
    data_url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"
    seen_paths: list[str] = []
    ingested: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-image",
                    "model": IMAGE_GENERATION_MODEL,
                    "choices": [{"message": {"content": data_url}}],
                    "trustedrouter": {
                        "routing": {"selected_provider": IMAGE_GENERATION_PROVIDER}
                    },
                    "usage": {"provider_usage": {"generation_id": "gen-image-job"}},
                },
            )
        if request.url.path == "/v1/internal/synthetic/samples":
            assert (
                request.headers["x-trustedrouter-internal-token"]
                == "observer-test"
            )
            ingested.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"recorded": 1}})
        return httpx.Response(404)

    settings = Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
        internal_gateway_token="billing-test",  # noqa: S106 - test placeholder.
        observer_internal_token="observer-test",  # noqa: S106 - test placeholder.
        synthetic_monitor_api_key="sk-tr-test",
    )
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(image_job, "get_settings", lambda: settings)
    monkeypatch.setattr(image_job.httpx, "AsyncClient", client_factory)
    monkeypatch.setenv(
        "TR_SYNTHETIC_INGEST_URL",
        "https://trustedrouter.com/v1/internal/synthetic/samples",
    )

    result = await image_job.run()

    assert result == 0
    assert seen_paths == ["/v1/chat/completions", "/v1/internal/synthetic/samples"]
    assert len(ingested) == 1
    sample = ingested[0]["samples"][0]
    assert sample["probe_type"] == "image_generation"
    assert sample["status"] == "up"
    assert data_url not in json.dumps(ingested)
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "up"
    assert output["generation_id"] == "gen-image-job"


@pytest.mark.asyncio
async def test_image_generation_job_confirms_a_text_only_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trusted_router.synthetic import image_generation as image_job

    image = b"\xff\xd8\xff" + (b"\x00" * 2048) + b"\xff\xd9"
    data_url = f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"
    chat_calls = 0
    ingested: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/v1/chat/completions":
            chat_calls += 1
            if chat_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-text-only",
                        "choices": [{"message": {"content": "I cannot create an image."}}],
                        "usage": {"cost_microdollars": 82},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-image-confirmed",
                    "choices": [{"message": {"content": data_url}}],
                    "usage": {"cost_microdollars": 90_000},
                },
            )
        if request.url.path == "/v1/internal/synthetic/samples":
            assert (
                request.headers["x-trustedrouter-internal-token"]
                == "observer-test"
            )
            ingested.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"recorded": 2}})
        return httpx.Response(404)

    settings = Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
        internal_gateway_token="billing-test",  # noqa: S106 - test placeholder.
        observer_internal_token="observer-test",  # noqa: S106 - test placeholder.
        synthetic_monitor_api_key="sk-tr-test",
    )
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(image_job, "get_settings", lambda: settings)
    monkeypatch.setattr(image_job.httpx, "AsyncClient", client_factory)
    monkeypatch.setenv(
        "TR_SYNTHETIC_INGEST_URL",
        "https://trustedrouter.com/v1/internal/synthetic/samples",
    )
    monkeypatch.setenv("TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS", "0")

    result = await image_job.run()

    assert result == 0
    assert chat_calls == 2
    assert len(ingested) == 1
    assert [sample["status"] for sample in ingested[0]["samples"]] == ["down", "up"]
    assert data_url not in json.dumps(ingested)
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "up"
    assert output["attempts"] == 2
    assert output["generation_id"] == "chatcmpl-image-confirmed"


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
        self.read_filters: list[str | None] = []
        self.committed: list[bytes] = []

    def read_rows(
        self,
        *,
        start_key: bytes,
        end_key: bytes,
        limit: int,
        filter_: Any | None = None,
    ) -> list[_FakeReadRow]:
        self.reads.append((start_key, end_key, limit))
        self.read_filters.append(filter_.__class__.__name__ if filter_ is not None else None)
        keyed_rows = [
            row for key, row in sorted(self.rows_by_key.items()) if start_key <= key < end_key
        ]
        return (keyed_rows + self.rows)[:limit]

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self)


# ---------------------------------------------------------------------------
# Provider/model rotation probe (Phase 1 of the performance-dataset effort).
# ---------------------------------------------------------------------------


def test_rotation_candidates_cover_credits_endpoints() -> None:
    from trusted_router.catalog import _PROVIDER_DEPRECATED_UPSTREAM_MODELS

    pool = rotation_candidates()
    assert pool, "expected at least one provider with a prepaid endpoint"
    for provider, models in pool.items():
        assert models, f"{provider} has no models"
        assert len(models) == len(set(models)), f"{provider} has duplicate models"
    # Both a snapshot provider and a supplemental-manifest provider are
    # reachable — coverage is endpoint-driven, not the prepaid_available flag.
    assert "openai" in pool
    assert "novita" in pool
    assert "google/gemma-4-26b-a4b-it" not in pool.get("gmi", [])
    assert "google/gemma-4-31b-it" not in pool.get("gmi", [])
    assert "moonshotai/kimi-k2.7-code" in pool.get("kimi", [])
    assert "moonshotai/kimi-k2.7-code-highspeed" in pool.get("kimi", [])
    assert "minimax/minimax-m2.1" not in pool.get("minimax", [])
    assert "minimax/minimax-m2.5" not in pool.get("minimax", [])
    assert "deepseek/deepseek-v3.2" not in pool.get("parasail", [])
    assert "moonshotai/kimi-k2.5" not in pool.get("parasail", [])
    assert "qwen/qwen3-235b-a22b-2507" not in pool.get("parasail", [])
    assert "stepfun/step-3.5-flash" not in pool.get("parasail", [])
    assert "z-ai/glm-4.7" not in pool.get("parasail", [])
    assert "z-ai/glm-5" not in pool.get("parasail", [])
    assert "z-ai/glm-5.1" in pool.get("parasail", [])
    assert "deepseek/deepseek-prover-v2-671b" not in pool.get("novita", [])
    assert "meta-llama/llama-3-8b-instruct" not in pool.get("novita", [])
    assert "qwen/qwen2.5-vl-72b-instruct" not in pool.get("novita", [])
    assert "qwen/qwen3-4b-fp8" not in pool.get("novita", [])
    assert not (
        set(pool.get("nebius", [])) & _PROVIDER_DEPRECATED_UPSTREAM_MODELS["nebius"]
    )
    assert not (
        set(pool.get("tinfoil", [])) & _PROVIDER_DEPRECATED_UPSTREAM_MODELS["tinfoil"]
    )
    assert "z-ai/glm-5.2" in pool.get("tinfoil", [])
    assert "google/gemma-4-31b-it" in pool.get("tinfoil", [])


def test_choose_rotation_target_two_stage_pick() -> None:
    pool = {"openai": ["openai/gpt-5.4-nano"], "novita": ["novita/a", "novita/b"]}
    rng = random.Random(0)  # noqa: S311 - deterministic test selection, not cryptographic
    picks = {choose_rotation_target(pool, rng) for _ in range(50)}
    for provider, model in picks:
        assert model in pool[provider]
    # Both providers get sampled despite novita having more models — equal
    # airtime per provider, not per model.
    assert {provider for provider, _ in picks} == {"openai", "novita"}


def test_choose_rotation_target_empty_pool_is_none() -> None:
    assert choose_rotation_target({}, random.Random(0)) is None  # noqa: S311 - test rng
    assert (
        choose_rotation_target({"openai": []}, random.Random(0)) is None  # noqa: S311 - test rng
    )


def test_sse_line_has_content_detects_visible_deltas() -> None:
    assert _sse_line_has_content('data: {"choices":[{"delta":{"content":"PONG"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"delta":{"reasoning_content":"x"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"delta":{"reasoning":"x"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"delta":{"thinking":"x"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"delta":{"text":"x"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"message":{"content":"PONG"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"message":{"reasoning":"PONG"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"message":{"thinking":"PONG"}}]}')
    assert _sse_line_has_content('data: {"choices":[{"text":"PONG"}]}')
    # role-only opener, [DONE], and non-data lines are NOT first content.
    assert not _sse_line_has_content('data: {"choices":[{"delta":{"role":"assistant"}}]}')
    assert not _sse_line_has_content(
        'data: {"choices":[],"trustedrouter":{"synth":{"event":"panel.thinking_delta","text":"x"}}}'
    )
    assert not _sse_line_has_content("data: [DONE]")
    assert not _sse_line_has_content(": keep-alive")


def test_sse_line_error_detects_openai_error_frames() -> None:
    assert _sse_line_error(
        'data: {"error":{"message":"provider error","type":"provider_error"}}'
    ) == ("provider_error", None, "provider error")
    assert _sse_line_error(
        'data: {"error":{"message":"rate limited","type":"rate_limit","status":429}}'
    ) == ("rate_limit", 429, "rate limited")
    assert _sse_line_error(
        'data: {"error":{"message":"model does not exist","type":"provider_error","status":404}}'
    ) == ("unsupported_route", 404, "model does not exist")
    assert _sse_line_error('data: {"choices":[{"delta":{"content":"PONG"}}]}') is None
    assert _sse_line_error("data: [DONE]") is None


def test_sse_line_error_distinguishes_router_failure_from_provider_failure() -> None:
    assert _sse_line_error(
        'data: {"error":{"message":"transient database contention",'
        '"type":"service_unavailable","source":"router","status":503}}'
    ) == ("router_database_contention", 503, "transient database contention")


def test_gateway_server_timing_parser_is_bounded_and_optional() -> None:
    assert _server_timing_gateway({"server-timing": "cache;dur=4, gateway;dur=0.7"}) == 1
    assert _server_timing_gateway({"server-timing": "gateway;dur=invalid"}) is None
    assert _server_timing_gateway({}) is None


def test_gateway_latency_phase_probe_uses_one_total_deadline() -> None:
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        _remaining_probe_seconds(time.perf_counter() - 2.0, 1.0)


@pytest.mark.asyncio
async def test_gateway_latency_phase_probe_rejects_non_https_without_network() -> None:
    samples = await gateway_latency_phase_probes(
        SyntheticTarget("bad", "http://api.example/v1", "us-central1"),
        monitor_region="us-central1",
    )

    assert [sample.probe_type for sample in samples] == [
        "gateway_cold_path",
        "gateway_reused_path",
    ]
    assert all(sample.status == "down" for sample in samples)
    assert all(sample.error_type == "invalid_health_url" for sample in samples)
    assert all(sample_slo_class_ids(sample) == [] for sample in samples)


@pytest.mark.asyncio
async def test_gateway_billing_probe_reports_authorize_and_settle_separately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authorize"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "authorization_id": "auth-1",
                        "model": "deepseek/deepseek-v4-flash",
                        "provider": "deepseek",
                        "endpoint_id": "deepseek-primary",
                    }
                },
            )
        assert request.url.path.endswith("/settle")
        return httpx.Response(
            200,
            json={
                "data": {
                    "settled": True,
                    "model": "deepseek/deepseek-v4-flash",
                    "provider": "deepseek",
                    "generation_id": "gen-1",
                    "cost_microdollars": 1,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        samples = await gateway_billing_probe(
            client,
            control_plane_base_url="https://trustedrouter.com",
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            internal_token="internal-test",  # noqa: S106 - test placeholder.
            model="trustedrouter/monitor",
        )

    assert [sample.probe_type for sample in samples] == [
        "gateway_authorize",
        "gateway_settle",
    ]
    assert all(sample.status == "up" for sample in samples)
    assert all(sample.latency_milliseconds is not None for sample in samples)
    assert all(sample_slo_class_ids(sample) == ["router_core"] for sample in samples)
    assert samples[1].cost_microdollars == 1


@pytest.mark.asyncio
async def test_gateway_billing_probe_classifies_monitor_workspace_pause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/internal/gateway/authorize")
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "message": "Workspace billing is paused",
                    "type": "service_unavailable",
                    "source": "router",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        samples = await gateway_billing_probe(
            client,
            control_plane_base_url="https://trustedrouter.com",
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            internal_token="internal-test",  # noqa: S106 - test placeholder.
            model="trustedrouter/monitor",
        )

    assert samples[0].probe_type == "gateway_authorize"
    assert samples[0].error_type == "monitor_workspace_paused"
    assert sample_slo_class_ids(samples[0]) == []
    assert {component for _period, component in sample_rollup_ids(samples[0])} == {
        "uncategorized"
    }


@pytest.mark.asyncio
async def test_gateway_fallback_probe_classifies_router_contention() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/internal/gateway/authorize")
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "message": (
                        "The request was aborted due to transient database "
                        "contention; retry."
                    ),
                    "type": "service_unavailable",
                    "source": "router",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        samples = await gateway_fallback_probe(
            client,
            control_plane_base_url="https://trustedrouter.com",
            monitor_region="europe-west4",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            internal_token="internal-test",  # noqa: S106 - test placeholder.
            model="trustedrouter/monitor",
        )

    assert samples[0].error_type == "router_database_contention"
    assert sample_slo_class_ids(samples[0]) == ["router_core"]


def test_sse_line_finish_reason_detects_length_stop() -> None:
    assert (
        _sse_line_finish_reason(
            'data: {"choices":[{"delta":{},"finish_reason":"length","index":0}]}'
        )
        == "length"
    )
    assert (
        _sse_line_finish_reason(
            'data: {"choices":[{"delta":{"content":"PONG"},"finish_reason":null}]}'
        )
        is None
    )
    assert _sse_line_finish_reason("data: [DONE]") is None


def test_rotation_probe_uses_reasoning_safe_request_budget() -> None:
    assert _rotation_max_tokens("google-ai-studio", "google/gemini-2.5-flash") == 2048
    assert _rotation_max_tokens("google-vertex", "google/gemini-3.5-flash") == 2048
    assert (
        _rotation_max_tokens("google-ai-studio", "google/gemini-3-flash-preview")
        == 2048
    )
    assert _rotation_max_tokens("cerebras", "cerebras/gpt-oss-120b") == 512
    assert _rotation_max_tokens("cerebras", "z-ai/glm-4.7") == 512
    assert _rotation_max_tokens("zai", "z-ai/glm-4.6") == 512
    assert _rotation_max_tokens("openai", "openai/o3") == 512
    assert _rotation_max_tokens("openai", "openai/gpt-5.5") == 512
    assert _rotation_max_tokens("baseten", "nvidia/nemotron-120b-a12b") == 512
    # Claude 5 thinking models emit no visible token within 16 (adaptive
    # thinking burns the budget), so they get the reasoning-safe budget too.
    assert _rotation_max_tokens("anthropic", "anthropic/claude-fable-5") == 512
    assert _rotation_max_tokens("anthropic", "anthropic/claude-sonnet-5") == 512
    # Non-thinking Claude models keep the small budget.
    assert _rotation_max_tokens("anthropic", "anthropic/claude-haiku-4.5") == 16
    assert (
        _rotation_max_tokens("together", "meta-llama/llama-3.1-8b-instruct") == 16
    )
    assert _rotation_omits_temperature("openai", "openai/o3")
    assert _rotation_omits_temperature("openai", "openai/gpt-5.5")
    # Canary contract: probes mirror customer payloads. The enclave strips
    # temperature for known deprecated Anthropic generations, so future missing
    # enclave updates surface here as direct probe 400s instead of being hidden.
    assert not _rotation_omits_temperature("anthropic", "anthropic/claude-fable-5")
    assert not _rotation_omits_temperature("anthropic", "anthropic/claude-sonnet-5")
    assert _rotation_omits_temperature("anthropic", "anthropic/claude-opus-4.7")
    assert _rotation_omits_temperature("anthropic", "anthropic/claude-opus-4.8")
    assert not _rotation_omits_temperature("openai", "openai/gpt-4.1-mini")


@pytest.mark.asyncio
async def test_provider_rotation_probe_measures_ttfb_and_ttft() -> None:
    body = (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"PONG"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            headers={
                "x-trustedrouter-provider": "openai",
                "x-trustedrouter-served-model": "openai/gpt-5.4-nano",
            },
            content=body,
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-5.4-nano",
        )

    assert sample.status == "success"
    assert sample.source == "synthetic"
    assert sample.provider == "openai"
    assert sample.model == "openai/gpt-5.4-nano"
    assert sample.streamed is True
    assert sample.ttfb_milliseconds is not None
    assert sample.first_token_milliseconds is not None
    assert sample.elapsed_milliseconds is not None


@pytest.mark.asyncio
async def test_provider_rotation_probe_counts_reasoning_as_token_flow() -> None:
    body = (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        b'data: {"choices":[{"delta":{"thinking":"checking PONG"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            headers={
                "x-trustedrouter-provider": "zai",
                "x-trustedrouter-served-model": "z-ai/glm-5.2",
            },
            content=body,
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="zai",
            model="z-ai/glm-5.2",
        )

    assert sample.status == "success"
    assert sample.first_token_milliseconds is not None
    assert sample.error_type is None


@pytest.mark.asyncio
async def test_provider_rotation_probe_records_sse_error_frame() -> None:
    body = (
        b'data: {"error":{"message":"provider error","type":"provider_error"}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-trustedrouter-provider": "kimi",
                "x-trustedrouter-served-model": "moonshotai/kimi-k2.6",
            },
            content=body,
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="kimi",
            model="moonshotai/kimi-k2.6",
        )

    assert sample.status == "error"
    assert sample.source == "synthetic"
    assert sample.provider == "kimi"
    assert sample.model == "moonshotai/kimi-k2.6"
    assert sample.error_type == "provider_error"
    assert sample.error_status == 502
    assert sample.error_message == "provider error"
    assert sample.first_token_milliseconds is None


@pytest.mark.asyncio
async def test_provider_rotation_probe_excludes_length_only_stream() -> None:
    body = b'data: {"choices":[{"delta":{},"finish_reason":"length","index":0}]}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
        )

    assert sample.status == "unsupported"
    assert sample.error_type == "probe_config_error"
    assert sample.error_status is None
    assert sample.source == "synthetic"
    assert sample.first_token_milliseconds is None


@pytest.mark.asyncio
async def test_provider_rotation_probe_records_empty_stream() -> None:
    body = b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="cerebras",
            model="cerebras/gpt-oss-120b",
        )

    assert sample.status == "error"
    assert sample.error_type == "empty_stream"
    assert sample.error_status is None
    assert sample.source == "synthetic"
    assert sample.first_token_milliseconds is None


@pytest.mark.asyncio
async def test_provider_rotation_probe_uses_provider_safe_request_shape() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"PONG"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="kimi",
            model="moonshotai/kimi-k2.6",
        )
        await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-4o-mini",
        )

    assert captured[0]["max_tokens"] == 128
    assert "temperature" not in captured[0]
    assert captured[1]["max_tokens"] == 16
    assert captured[1]["temperature"] == 0


@pytest.mark.asyncio
async def test_provider_rotation_probe_records_http_error() -> None:
    upstream_message = (
        "provider quota exhausted Bearer live-secret sk-live1234 rk-route5678 "
        + ("x" * 400)
    )
    scrubbed_message = "provider quota exhausted Bearer *** sk-*** sk-*** " + ("x" * 400)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"type": "provider_error", "message": upstream_message}},
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-5.4-nano",
        )

    assert sample.status == "error"
    assert sample.error_type == "provider_error"
    assert sample.error_status == 500
    assert sample.error_message == scrubbed_message[:300]
    assert "live-secret" not in str(sample.error_message)
    assert "sk-live1234" not in str(sample.error_message)
    assert "rk-route5678" not in str(sample.error_message)
    assert sample.first_token_milliseconds is None
    assert sample.source == "synthetic"


@pytest.mark.asyncio
async def test_provider_rotation_probe_excludes_router_failure_from_uptime() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "message": "Workspace billing is paused",
                    "type": "service_unavailable",
                    "source": "router",
                }
            },
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-5.4-nano",
        )

    assert sample.status == "unsupported"
    assert sample.error_type == "monitor_workspace_paused"
    assert sample.error_status == 503


@pytest.mark.asyncio
async def test_provider_rotation_probe_records_httpx_error_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway connection failed", request=request)

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-5.4-nano",
        )

    assert sample.status == "error"
    assert sample.error_type == "ConnectError"
    assert sample.error_message == "gateway connection failed"
    assert sample.first_token_milliseconds is None
    assert sample.source == "synthetic"


@pytest.mark.asyncio
async def test_provider_rotation_probe_classifies_unsupported_routes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"type": "provider_error", "message": "model does not exist"}},
        )

    target = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="openai",
            model="openai/gpt-5.4-nano",
        )

    assert sample.status == "unsupported"
    assert sample.error_type == "unsupported_route"
    assert sample.error_status == 400
    assert sample.finish_reason == "unsupported"


def _benchmark_ingest_settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="test-billing-secret",  # noqa: S106 - test fixture.
        observer_internal_token="test-observer-secret",  # noqa: S106 - test fixture.
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


def test_internal_benchmark_ingest_records_sample() -> None:
    client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))
    # Key-shaped + bearer material up front proves the ingest boundary scrubs
    # server-side (not just the probe); the x-padding proves [:300] truncation.
    error_message = "denied for Bearer SK-LIVE-abcd1234 upstream said no " + ("x" * 400)
    payload = {
        "samples": [
            {
                "id": "bench-ingest-test-1",
                "model": "openai/gpt-5.4-nano",
                "provider": "openai",
                "provider_name": "OpenAI",
                "status": "success",
                "usage_type": "Credits",
                "streamed": True,
                "elapsed_milliseconds": 300,
                "first_token_milliseconds": 150,
                "ttfb_milliseconds": 90,
                "error_message": error_message,
                "source": "synthetic",
                "created_at": "2026-06-04T00:00:00Z",
            }
        ]
    }
    resp = client.post(
        "/v1/internal/synthetic/benchmark",
        headers={"x-trustedrouter-internal-token": "test-observer-secret"},
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["recorded"] == 1
    rows = STORE.provider_benchmark_samples(
        date=None, provider="openai", model="openai/gpt-5.4-nano", limit=50
    )
    matched = [row for row in rows if row.id == "bench-ingest-test-1"]
    assert matched, "ingested benchmark sample not found in store"
    assert matched[0].ttfb_milliseconds == 90
    stored_message = str(matched[0].error_message)
    assert len(stored_message) <= 300
    assert "SK-LIVE-abcd1234" not in stored_message
    assert "Bearer ***" in stored_message
    assert stored_message.endswith("x" * 50)
    assert matched[0].source == "synthetic"


def test_internal_benchmark_ingest_requires_token() -> None:
    client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))
    resp = client.post(
        "/v1/internal/synthetic/benchmark",
        headers={"x-trustedrouter-internal-token": "wrong"},
        json={"samples": []},
    )
    assert resp.status_code in (401, 403)


class _RouteHealthStore:
    def __init__(self, samples: list[ProviderBenchmarkSample]) -> None:
        self.samples = samples
        self.calls: list[dict[str, Any]] = []

    def provider_benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        self.calls.append(
            {"date": date, "provider": provider, "model": model, "limit": limit}
        )
        samples = [
            sample
            for sample in self.samples
            if (provider is None or sample.provider == provider)
            and (model is None or sample.model == model)
        ]
        samples.sort(key=lambda sample: sample.created_at, reverse=True)
        return samples[:limit]


def _route_health_sample(
    sample_id: str,
    *,
    provider: str,
    model: str,
    status: str,
    age_hours: int = 0,
    error_type: str | None = None,
    error_status: int | None = None,
    error_message: str | None = None,
    source: str = "synthetic",
) -> ProviderBenchmarkSample:
    created_at = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)).isoformat()
    return ProviderBenchmarkSample(
        id=sample_id,
        model=model,
        provider=provider,
        provider_name=provider,
        status=status,
        usage_type="Credits",
        streamed=True,
        error_type=error_type,
        error_status=error_status,
        error_message=error_message,
        source=source,
        created_at=created_at,
    )


def test_evaluate_route_health_ignores_transient_failures() -> None:
    # A route that is 100% failing on transient/capacity errors (rate limit,
    # gateway/no-upstream, timeout) is NOT alert-worthy — it may recover and
    # quarantining would stop us re-probing it. It must not flag.
    transient = []
    transient.extend(
        _route_health_sample(
            f"rl-{i}", provider="busy", model="m", status="error",
            error_type="provider_error", error_status=429,
        )
        for i in range(4)
    )
    transient.extend(
        _route_health_sample(
            f"to-{i}", provider="busy", model="m", status="error",
            error_type="ReadTimeout",
        )
        for i in range(4)
    )
    transient.extend(
        _route_health_sample(
            f"gw-{i}", provider="busy", model="m", status="error",
            error_type="provider_error", error_status=502,
        )
        for i in range(4)
    )
    assert evaluate_route_health(  # type: ignore[arg-type]
        _RouteHealthStore(transient), routes=[("busy", "m")]
    ) == []

    # A structurally-dead route (404 model-not-found) still flags.
    structural = [
        _route_health_sample(
            f"nf-{i}", provider="dead", model="m", status="error",
            error_type="provider_error", error_status=404,
        )
        for i in range(6)
    ]
    flags = evaluate_route_health(  # type: ignore[arg-type]
        _RouteHealthStore(structural), routes=[("dead", "m")]
    )
    assert len(flags) == 1 and flags[0].failure_rate == 1.0


def test_evaluate_route_health_flags_dead_route_but_not_healthy_or_thin_routes() -> None:
    samples = [
        _route_health_sample(
            f"dead-{index}",
            provider="dead",
            model="model-dead",
            status="error",
            age_hours=index,
            error_type="provider_error",
            error_message="latest failure" if index == 0 else "older failure",
        )
        for index in range(6)
    ]
    samples.extend(
        _route_health_sample(
            f"healthy-{index}",
            provider="healthy",
            model="model-healthy",
            status="error" if index == 0 else "success",
            error_type="provider_error" if index == 0 else None,
        )
        for index in range(6)
    )
    samples.extend(
        _route_health_sample(
            f"thin-{index}",
            provider="thin",
            model="model-thin",
            status="error",
        )
        for index in range(5)
    )
    store = _RouteHealthStore(samples)

    routes = [
        ("dead", "model-dead"),
        ("healthy", "model-healthy"),
        ("thin", "model-thin"),
    ]
    flags = evaluate_route_health(store, routes=routes)  # type: ignore[arg-type]

    assert flags == [
        RouteHealthFlag(
            provider="dead",
            model="model-dead",
            samples=6,
            failures=6,
            failure_rate=1.0,
            newest_error_type="provider_error",
            newest_error_message="latest failure",
        )
    ]
    assert store.calls == [
        {"date": None, "provider": provider, "model": model, "limit": 48}
        for provider, model in routes
    ]


def test_evaluate_route_health_excludes_unsupported_samples_from_rate() -> None:
    samples = [
        _route_health_sample(
            f"error-{index}",
            provider="reseller",
            model="model-a",
            status="error",
            error_type="provider_error",
        )
        for index in range(6)
    ]
    samples.extend(
        _route_health_sample(
            f"unsupported-{index}",
            provider="reseller",
            model="model-a",
            status="unsupported",
            error_type="unsupported_route",
        )
        for index in range(10)
    )

    flags = evaluate_route_health(  # type: ignore[arg-type]
        _RouteHealthStore(samples),
        routes=[("reseller", "model-a")],
    )

    assert len(flags) == 1
    assert flags[0].samples == 6
    assert flags[0].failures == 6
    assert flags[0].failure_rate == 1.0


def test_evaluate_route_health_ignores_organic_samples() -> None:
    samples = [
        _route_health_sample(
            f"organic-error-{index}",
            provider="reseller",
            model="model-a",
            status="error",
            error_type="provider_error",
            source="organic",
        )
        for index in range(10)
    ]
    samples.extend(
        _route_health_sample(
            f"synthetic-success-{index}",
            provider="reseller",
            model="model-a",
            status="success",
        )
        for index in range(2)
    )

    flags = evaluate_route_health(  # type: ignore[arg-type]
        _RouteHealthStore(samples),
        routes=[("reseller", "model-a")],
        failure_threshold=0.8,
    )

    assert flags == []


def test_evaluate_route_health_derives_routes_from_rotation_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import route_health as route_health_module

    candidates = {
        "provider-b": ["model-2", "model-1"],
        "provider-a": ["model-3"],
    }
    monkeypatch.setattr(route_health_module, "rotation_candidates", lambda: candidates)
    samples = [
        _route_health_sample(
            f"error-{index}",
            provider="provider-a",
            model="model-3",
            status="error",
            error_type="provider_error",
        )
        for index in range(6)
    ]
    store = _RouteHealthStore(samples)

    flags = evaluate_route_health(store)  # type: ignore[arg-type]

    assert [(flag.provider, flag.model) for flag in flags] == [("provider-a", "model-3")]
    assert store.calls == [
        {"date": None, "provider": provider, "model": model, "limit": 48}
        for provider, models in candidates.items()
        for model in models
    ]


def test_report_route_health_uses_one_sentry_fingerprint_per_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    class CapturedScope:
        def __init__(self) -> None:
            self.fingerprint: list[str] = []
            self.tags: dict[str, str] = {}

        def set_tag(self, key: str, value: str) -> None:
            self.tags[key] = value

    class ScopeManager:
        def __init__(self, scope: CapturedScope) -> None:
            self.scope = scope

        def __enter__(self) -> CapturedScope:
            return self.scope

        def __exit__(self, *_args: Any) -> None:
            return None

    scopes: list[CapturedScope] = []
    captured: list[tuple[str, str]] = []

    def push_scope() -> ScopeManager:
        scope = CapturedScope()
        scopes.append(scope)
        return ScopeManager(scope)

    def capture_message(message: str, *, level: str) -> None:
        captured.append((message, level))

    monkeypatch.setattr(sentry_sdk, "push_scope", push_scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", capture_message)
    flags = [
        RouteHealthFlag("gmi", "model-a", 6, 6, 1.0, "provider_error", "missing"),
        RouteHealthFlag("phala", "model-b", 8, 8, 1.0, "provider_error", "missing"),
    ]

    report_route_health(flags)

    assert len(captured) == len(flags)
    assert all(level == "error" for _, level in captured)
    assert [scope.fingerprint for scope in scopes] == [
        ["route-health", "gmi", "model-a"],
        ["route-health", "phala", "model-b"],
    ]
    assert scopes[0].tags == {
        "route_provider": "gmi",
        "route_model": "model-a",
        "failure_rate": "1.0000",
    }


def test_image_generation_failure_alert_is_fingerprinted_and_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    class CapturedScope:
        def __init__(self) -> None:
            self.fingerprint: list[str] = []
            self.tags: dict[str, str] = {}

        def set_tag(self, key: str, value: str) -> None:
            self.tags[key] = value

    class ScopeManager:
        def __init__(self, scope: CapturedScope) -> None:
            self.scope = scope

        def __enter__(self) -> CapturedScope:
            return self.scope

        def __exit__(self, *_args: Any) -> None:
            return None

    scopes: list[CapturedScope] = []
    captured: list[tuple[str, str]] = []

    def push_scope() -> ScopeManager:
        scope = CapturedScope()
        scopes.append(scope)
        return ScopeManager(scope)

    def capture_message(message: str, *, level: str) -> None:
        captured.append((message, level))

    monkeypatch.setattr(sentry_sdk, "push_scope", push_scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", capture_message)
    failed = _sample(
        id="syn_image_failed",
        probe_type="image_generation",
        status="down",
        provider=IMAGE_GENERATION_PROVIDER,
        model=IMAGE_GENERATION_MODEL,
        error_type="invalid_image_payload",
        http_status=200,
    )
    report_image_generation_failures([failed])

    assert captured == [
        (
            (
                "image-generation-canary: google-ai-studio/"
                "google/gemini-3.1-flash-image-preview failed "
                "(invalid_image_payload, HTTP 200)"
            ),
            "error",
        )
    ]
    assert scopes[0].fingerprint == [
        "image-generation-canary",
        IMAGE_GENERATION_PROVIDER,
        IMAGE_GENERATION_MODEL,
    ]
    assert "prompt" not in json.dumps(scopes[0].tags).lower()
    assert "output" not in json.dumps(scopes[0].tags).lower()


def test_image_generation_confirmation_success_does_not_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    captured: list[str] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, *, level: captured.append(f"{level}:{message}"),
    )
    failed = _sample(
        id="syn_image_first_failed",
        probe_type="image_generation",
        status="down",
        provider=IMAGE_GENERATION_PROVIDER,
        model=IMAGE_GENERATION_MODEL,
        error_type="invalid_image_payload",
        http_status=200,
    )
    confirmed = _sample(
        id="syn_image_confirmation_up",
        probe_type="image_generation",
        status="up",
        provider=IMAGE_GENERATION_PROVIDER,
        model=IMAGE_GENERATION_MODEL,
        http_status=200,
    )

    report_image_generation_failures([failed, confirmed])

    assert captured == []


def test_image_generation_double_failure_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    captured: list[str] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, *, level: captured.append(f"{level}:{message}"),
    )
    failures = [
        _sample(
            id=f"syn_image_failed_{attempt}",
            probe_type="image_generation",
            status="down",
            provider=IMAGE_GENERATION_PROVIDER,
            model=IMAGE_GENERATION_MODEL,
            error_type="invalid_image_payload",
            http_status=200,
        )
        for attempt in range(2)
    ]

    report_image_generation_failures(failures)

    assert len(captured) == 1


def test_internal_route_health_reports_flags_and_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    flag = RouteHealthFlag(
        "gmi",
        "anthropic/claude-fable-5",
        6,
        6,
        1.0,
        "provider_error",
        "model unavailable",
    )
    reported: list[RouteHealthFlag] = []
    monkeypatch.setattr(synthetic_routes, "evaluate_route_health", lambda _store: [flag])
    monkeypatch.setattr(synthetic_routes, "report_route_health", reported.extend)
    client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))

    unauthorized = client.post("/v1/internal/synthetic/route-health")
    response = client.post(
        "/v1/internal/synthetic/route-health",
        headers={"x-trustedrouter-internal-token": "test-observer-secret"},
    )

    assert unauthorized.status_code in (401, 403)
    assert response.status_code == 200
    assert response.json() == {"data": {"flagged": [asdict(flag)]}}
    assert reported == [flag]


def test_internal_remediator_runs_between_heartbeats_and_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.routes.internal import synthetic as synthetic_routes

    events: list[str] = []

    def fake_heartbeat(name: str, *, settings: Settings) -> None:
        assert name == "scheduler:remediator"
        assert settings.environment == "test"
        events.append("heartbeat")

    def fake_remediator(settings: Settings) -> list[object]:
        assert settings.environment == "test"
        events.append("remediate")
        return [object(), object()]

    monkeypatch.setattr(synthetic_routes, "record_heartbeat", fake_heartbeat)
    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", fake_remediator)
    client = TestClient(create_app(_benchmark_ingest_settings(), init_observability=False))

    unauthorized = client.post("/v1/internal/synthetic/remediate")
    response = client.post(
        "/v1/internal/synthetic/remediate",
        headers={"x-trustedrouter-internal-token": "test-observer-secret"},
    )

    assert unauthorized.status_code in (401, 403)
    assert response.status_code == 200
    assert response.json() == {"data": {"decisions": 2}}
    assert events == ["heartbeat", "remediate", "heartbeat"]
