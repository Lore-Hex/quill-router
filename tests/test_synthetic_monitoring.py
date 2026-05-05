from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest
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
from trusted_router.storage_models import iso_now, utcnow
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    attestation_nonce_probe,
    openai_chat_pong_probe,
    responses_pong_probe,
    tls_health_probe,
)
from trusted_router.synthetic.status import status_snapshot


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
    sample = _sample(
        id="syn_1",
        probe_type="openai_sdk_pong",
        status="up",
        model=MONITOR_MODEL_ID,
        output_match=True,
    )
    resp = client.post("/v1/internal/synthetic/samples", json=sample.public_dict())
    assert resp.status_code == 200, resp.text

    status = client.get("/status.json")
    page = client.get("/status")
    history = client.get("/status/history?window=5m")

    assert status.status_code == 200
    assert page.status_code == 200
    assert history.status_code == 200
    assert "Current Status" in page.text
    text = status.text
    assert "reply exactly PONG" not in text
    assert "sk-tr-" not in text
    payload = status.json()["data"]
    assert payload["samples"][0]["probe_type"] == "openai_sdk_pong"
    assert payload["samples"][0]["output_match"] is True


def test_chat_monitor_model_requires_configured_monitor_key() -> None:
    monitor_key = "sk-tr-monitor-test"  # noqa: S105 - test key.
    app = create_app(
        Settings(environment="test", synthetic_monitor_api_key=monitor_key),
        init_observability=False,
    )
    local_client = TestClient(app)
    normal = local_client.post("/v1/keys", headers={"x-trustedrouter-user": "alice@example.com"}, json={"name": "normal"})
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
    now = utcnow()
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
            created_at=(now - dt.timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=500,
        ),
        _sample(
            id="syn_old",
            probe_type="responses_pong",
            status="up",
            created_at=(now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            latency_milliseconds=120,
        ),
    ]

    snapshot = status_snapshot(samples)

    assert snapshot["current"]["checks"]
    assert snapshot["windows"]["5m"]["sample_count"] == 2
    assert snapshot["windows"]["24h"]["sample_count"] == 3
    assert snapshot["daily"][0]["sample_count"] == 3


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
    assert table.committed == [
        f"synthetic_recent#{reverse}#syn_1".encode(),
        f"synthetic_target_recent#canonical#{reverse}#syn_1".encode(),
        f"synthetic_probe_target_recent#attestation_nonce#canonical#{reverse}#syn_1".encode(),
        f"synthetic_monitor_recent#us-central1#{reverse}#syn_1".encode(),
        f"synthetic_day#2026-05-05#canonical#attestation_nonce#{reverse}#syn_1".encode(),
        f"synthetic_day_recent#2026-05-05#{reverse}#syn_1".encode(),
    ]
    assert b"sk-tr" not in b"".join(table.committed)
    assert b"prompt" not in b"".join(table.committed)


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


def test_gateway_monitor_model_requires_configured_monitor_key() -> None:
    monitor_key = "sk-tr-monitor-gateway"  # noqa: S105 - test key.
    app = create_app(
        Settings(environment="test", synthetic_monitor_api_key=monitor_key),
        init_observability=False,
    )
    local_client = TestClient(app)
    normal = local_client.post("/v1/keys", headers={"x-trustedrouter-user": "alice@example.com"}, json={"name": "normal"})
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


def _sample(
    *,
    id: str,
    probe_type: str,
    status: str,
    model: str | None = None,
    output_match: bool | None = None,
    created_at: str | None = None,
    latency_milliseconds: int | None = None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=id,
        probe_type=probe_type,
        target="canonical",
        target_url="https://api.quillrouter.com/v1",
        monitor_region="us-central1",
        target_region="us-central1",
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
        self.value = json.dumps(asdict(value), separators=(",", ":"), sort_keys=True).encode()


class _FakeReadRow:
    def __init__(self, value: Any) -> None:
        self.cells = {"m": {b"body": [_FakeCell(value)]}}


class _FakeDirectRow:
    def __init__(self, key: bytes, committed: list[bytes]) -> None:
        self.key = key
        self.committed = committed

    def set_cell(self, *_args: Any) -> None:
        return None

    def commit(self) -> None:
        self.committed.append(self.key)


class _FakeBigtable:
    def __init__(self, rows: list[_FakeReadRow] | None = None) -> None:
        self.rows = rows or []
        self.reads: list[tuple[bytes, bytes, int]] = []
        self.committed: list[bytes] = []

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int) -> list[_FakeReadRow]:
        self.reads.append((start_key, end_key, limit))
        return self.rows[:limit]

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self.committed)
