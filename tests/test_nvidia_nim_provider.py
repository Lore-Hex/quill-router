from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from scripts.pricing import openai_catalog
from scripts.pricing.providers import nvidia_nim
from trusted_router.catalog_data import (
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PROVIDERS,
)

LLM_REFERENCE_HTML = """
<section>
  <h2>Large Language models</h2>
  <a class="Sidebar-link_parent"><span>deepseek-ai / deepseek-v4-flash-0731</span></a>
  <a class="Sidebar-link_parent"><span>meta / llama-3.1-8b-instruct</span></a>
  <a class="Sidebar-link_parent"><span>thinking machines / inkling</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-0</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-1</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-2</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-3</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-4</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-5</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-6</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-7</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-8</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-9</span></a>
  <a class="Sidebar-link_parent"><span>vendor / model-10</span></a>
</section>
"""


def _install_catalog(monkeypatch, rows: list[dict[str, str]]) -> list[str]:
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"data": rows})

    class FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(nvidia_nim.httpx, "Client", FakeClient)
    monkeypatch.setattr(nvidia_nim, "fetch_html", lambda _url: LLM_REFERENCE_HTML)
    return seen_auth


def test_parse_llm_reference_normalizes_provider_names() -> None:
    assert nvidia_nim.parse_llm_reference_model_ids(LLM_REFERENCE_HTML) >= {
        "deepseek/deepseek-v4-flash-0731",
        "meta-llama/llama-3.1-8b-instruct",
        "thinkingmachines/inkling",
    }


def test_nvidia_nim_candidate_filter_keeps_new_chat_and_blocks_specialists() -> None:
    assert nvidia_nim._looks_like_chat_model("moonshotai/kimi-k3") is True
    assert nvidia_nim._looks_like_chat_model("nvidia/nemotron-safety-guard") is False
    assert nvidia_nim._looks_like_chat_model("nvidia/nemotron-content-safety") is False
    assert nvidia_nim._looks_like_chat_model("nvidia/riva-translate-4b") is False
    assert nvidia_nim._looks_like_chat_model("nvidia/nv-embed-v2") is False


def test_nvidia_nim_discovers_canaries_and_holds_non_chat_models(monkeypatch, tmp_path) -> None:
    rows = [{"id": f"vendor/model-{index}", "object": "model"} for index in range(11)]
    rows.extend(
        [
            {"id": "deepseek-ai/deepseek-v4-flash-0731", "object": "model"},
            {"id": "nvidia/nv-embed-v1", "object": "model"},
        ]
    )
    seen_auth = _install_catalog(monkeypatch, rows)
    probed: list[str] = []

    def probe(**kwargs) -> bool:
        probed.append(kwargs["model"])
        assert kwargs["require_message"] is True
        return kwargs["model"] != "vendor/model-1"

    manifest = tmp_path / "nvidia-nim.json"
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")
    monkeypatch.setattr(nvidia_nim, "probe_openai_chat", probe)
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", manifest)

    result = nvidia_nim.fetch()
    nvidia_nim.write_provider_manifest(result)

    assert seen_auth == ["Bearer nim-test-key"]
    assert "nvidia/nv-embed-v1" not in probed
    assert "deepseek-ai/deepseek-v4-flash-0731" in probed
    payload = json.loads(manifest.read_text())
    by_id = {row["id"]: row for row in payload["models"]}
    assert payload["model_count"] == 13
    assert by_id["deepseek/deepseek-v4-flash-0731"].get("routable", True)
    assert by_id["vendor/model-1"]["routable_reason"] == "provider-canary-failed"
    non_chat = by_id["nvidia/nv-embed-v1"]
    assert non_chat["model_type"] == "discovery"
    assert non_chat["endpoints"] == []
    assert non_chat["routable"] is False
    assert non_chat["routable_reason"] == "unsupported-chat-endpoint"
    assert non_chat["input_token_price_per_m"] == 2_000_000
    assert non_chat["output_token_price_per_m"] == 10_000_000
    assert result.prices["vendor/model-0"] == nvidia_nim._CONSERVATIVE_HOSTED_PRICE
    assert result.prices["nvidia/nv-embed-v1"] == nvidia_nim._CONSERVATIVE_HOSTED_PRICE


@pytest.mark.parametrize(
    "legacy_reason",
    ["production-entitlement-required", "unsupported-chat-endpoint"],
)
def test_nvidia_nim_recanaries_machine_held_chat_routes(
    monkeypatch, tmp_path, legacy_reason: str
) -> None:
    rows = [{"id": f"vendor/model-{index}", "object": "model"} for index in range(12)]
    _install_catalog(monkeypatch, rows)
    manifest = tmp_path / "nvidia-nim.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "nvidia-nim",
                "models": [
                    {
                        "id": "vendor/model-0",
                        "routable": False,
                        "routable_reason": legacy_reason,
                    }
                ],
            }
        )
    )
    probed: list[str] = []
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

    def probe(**kwargs) -> bool:
        probed.append(kwargs["model"])
        return True

    monkeypatch.setattr(nvidia_nim, "probe_openai_chat", probe)
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", manifest)

    result = nvidia_nim.fetch()
    nvidia_nim.write_provider_manifest(result)

    assert "vendor/model-0" in probed
    payload = json.loads(manifest.read_text())
    migrated = next(row for row in payload["models"] if row["id"] == "vendor/model-0")
    assert migrated.get("routable", True) is True
    assert "routable_reason" not in migrated


def test_nvidia_nim_rechecks_each_routable_model_once_per_day(monkeypatch, tmp_path) -> None:
    model_id = "vendor/model-0"
    manifest = tmp_path / "nvidia-nim.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "nvidia-nim",
                "models": [{"id": model_id, "model_type": "chat"}],
            }
        )
    )
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", manifest)
    bucket = nvidia_nim._scheduled_canary_bucket(model_id)

    scheduled = nvidia_nim._models_to_canary(
        {model_id},
        now=datetime(2026, 8, 24, bucket, tzinfo=UTC),
    )
    unscheduled = nvidia_nim._models_to_canary(
        {model_id},
        now=datetime(2026, 8, 24, (bucket + 1) % 24, tzinfo=UTC),
    )

    assert scheduled == {model_id}
    assert unscheduled == frozenset()


def test_nvidia_nim_scheduled_failure_quarantines_only_that_route(monkeypatch, tmp_path) -> None:
    rows = [{"id": f"vendor/model-{index}", "object": "model"} for index in range(11)]
    _install_catalog(monkeypatch, rows)
    manifest = tmp_path / "nvidia-nim.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "nvidia-nim",
                "models": [{"id": row["id"], "model_type": "chat"} for row in rows],
            }
        )
    )
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        nvidia_nim,
        "_models_to_canary",
        lambda _candidates: frozenset({"vendor/model-0"}),
    )
    monkeypatch.setattr(nvidia_nim, "probe_openai_chat", lambda **_kwargs: False)

    result = nvidia_nim.fetch()
    nvidia_nim.write_provider_manifest(result)

    payload = json.loads(manifest.read_text())
    by_id = {row["id"]: row for row in payload["models"]}
    assert by_id["vendor/model-0"]["routable"] is False
    assert by_id["vendor/model-0"]["routable_reason"] == "provider-canary-failed"
    assert by_id["vendor/model-1"].get("routable", True) is True


def test_nvidia_nim_rejects_canonical_model_collisions(monkeypatch, tmp_path) -> None:
    rows = [{"id": f"vendor/model-{index}", "object": "model"} for index in range(11)]
    rows.extend(
        [
            {"id": "meta/collision", "object": "model"},
            {"id": "meta-llama/collision", "object": "model"},
        ]
    )
    _install_catalog(monkeypatch, rows)
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", tmp_path / "nvidia-nim.json")

    with pytest.raises(RuntimeError, match="canonical model collision"):
        nvidia_nim.fetch()


def test_openai_probe_can_require_nonempty_message(monkeypatch) -> None:
    payload: dict[str, object] = {"choices": [{"message": {"content": ""}}]}

    def post(*_args, **_kwargs) -> httpx.Response:
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(openai_catalog.httpx, "post", post)
    kwargs = {
        "base_url": "https://provider.example/v1",
        "api_key": "test-key",
        "model": "vendor/model",
        "require_message": True,
    }
    assert openai_catalog.probe_openai_chat(**kwargs) is False

    payload["choices"] = [{"message": {"reasoning_content": "working"}}]
    assert openai_catalog.probe_openai_chat(**kwargs) is True

    monkeypatch.setattr(
        openai_catalog.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(503),
    )
    assert openai_catalog.probe_openai_chat(**kwargs) is False

    def fail(*_args, **_kwargs) -> httpx.Response:
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(openai_catalog.httpx, "post", fail)
    assert openai_catalog.probe_openai_chat(**kwargs) is False


def test_nvidia_nim_is_a_standard_prepaid_provider() -> None:
    provider = PROVIDERS["nvidia-nim"]
    assert "nvidia-nim" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert provider.supports_chat is True
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.provider_zero_data_retention is False


def test_routable_nvidia_manifest_rows_create_prepaid_endpoints() -> None:
    from trusted_router.catalog import endpoints_for_model

    raw = json.loads(nvidia_nim.MANIFEST_PATH.read_text())
    routable = [
        row
        for row in raw["models"]
        if row.get("model_type") == "chat" and row.get("routable") is not False
    ]
    for row in routable:
        if row["id"] == DEEPSEEK_V4_PRO_0813_MODEL_ID:
            # Versioned DeepSeek releases have an explicitly pinned provider
            # set. Provider discovery may prove that NVIDIA serves the exact
            # release, but it must not mutate that published route set.
            continue
        endpoints = [
            endpoint
            for endpoint in endpoints_for_model(row["id"])
            if endpoint.provider == "nvidia-nim" and str(endpoint.usage_type) == "Credits"
        ]
        assert len(endpoints) == 1
        assert endpoints[0].upstream_id == row["upstream_id"]
        assert endpoints[0].prompt_price_microdollars_per_million_tokens == 2_110_000
        assert endpoints[0].completion_price_microdollars_per_million_tokens == 10_550_000

    assert not any(
        endpoint.provider == "nvidia-nim"
        for endpoint in endpoints_for_model(DEEPSEEK_V4_PRO_0813_MODEL_ID)
    )

    unroutable_ids = {
        row["id"]
        for row in raw["models"]
        if row.get("model_type") == "chat" and row.get("routable") is False
    }
    assert all(
        not any(endpoint.provider == "nvidia-nim" for endpoint in endpoints_for_model(model_id))
        for model_id in unroutable_ids
    )
