from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import (
    alibaba,
    anthropic,
    arcee,
    atlas_cloud,
    chutes,
    cloudflare_workers_ai,
    crusoe,
    databricks,
    deepinfra,
    digitalocean,
    friendli,
    gemini,
    inception,
    lyceum,
    mistral,
    nebius,
    novita,
    openai,
    sail_research,
    scaleway,
    system1models,
    system1models_eu,
    telnyx,
    tencent,
    tinfoil,
    together,
    xiaomi,
)
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@dataclass(frozen=True)
class Case:
    provider: str
    model: str
    native: str
    cutoff: datetime
    replacement: str | None


_CASES = [
    Case("anthropic", "anthropic/claude-sonnet-4.5", "claude-sonnet-4-5-20250929",
         datetime(2026, 11, 30, 17, tzinfo=UTC), "anthropic/claude-sonnet-5.5"),
    Case("google-ai-studio", "google/gemini-2.5-flash", "gemini-2.5-flash",
         datetime(2026, 12, 1, tzinfo=UTC), "google/gemini-3.5-flash-lite"),
    Case("friendli", "z-ai/glm-5.1", "zai-org/GLM-5.1",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "nvidia/nemotron-3-120b-a12b", "@cf/nvidia/nemotron-3-120b-a12b",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "moonshotai/kimi-k2.6", "@cf/moonshotai/kimi-k2.6",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "moonshotai/kimi-k2.7-code", "@cf/moonshotai/kimi-k2.7-code",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "z-ai/glm-5.2", "@cf/zai-org/glm-5.2",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "z-ai/glm-5.3-flash", "@cf/zai-org/glm-5.3-flash",
         datetime(2026, 11, 20, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("cloudflare-workers-ai", "deepseek/deepseek-v4-pro-0813", "@cf/deepseek-ai/deepseek-v4-pro-0813",
         datetime(2026, 11, 20, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("cloudflare-workers-ai", "deepseek/deepseek-v4-flash-0731", "@cf/deepseek-ai/deepseek-v4-flash-0731",
         datetime(2026, 11, 20, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("cloudflare-workers-ai", "deepseek/deepseek-r1-distill-qwen-32b", "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
         datetime(2026, 11, 20, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("cloudflare-workers-ai", "qwen/qwq-32b", "@cf/qwen/qwq-32b",
         datetime(2026, 11, 20, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("cloudflare-workers-ai", "qwen/qwen3-30b-a3b-fp8", "@cf/qwen/qwen3-30b-a3b-fp8",
         datetime(2026, 11, 20, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("cloudflare-workers-ai", "meta-llama/llama-2-7b-chat-hf-lora", "@cf/meta-llama/llama-2-7b-chat-hf-lora",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("cloudflare-workers-ai", "google/gemma-2b-it-lora", "@cf/google/gemma-2b-it-lora",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("cloudflare-workers-ai", "google/gemma-7b-it-lora", "@cf/google/gemma-7b-it-lora",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("cloudflare-workers-ai", "meta-llama/llama-3.1-8b-instruct-fp8", "@cf/meta/llama-3.1-8b-instruct-fp8",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("cloudflare-workers-ai", "meta-llama/llama-3.2-1b-instruct", "@cf/meta/llama-3.2-1b-instruct",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("cloudflare-workers-ai", "meta-llama/llama-3.2-3b-instruct", "@cf/meta/llama-3.2-3b-instruct",
         datetime(2026, 11, 20, tzinfo=UTC), "google/gemma-4-26b-a4b-it"),
    Case("tencent", "tencent/hy4-preview", "hy4-preview",
         datetime(2026, 12, 6, 16, tzinfo=UTC), "tencent/hy4"),
    Case("databricks", "z-ai/glm-5.2", "databricks-glm-5-2",
         datetime(2027, 2, 4, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("openai", "openai/gpt-5", "gpt-5",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-sol"),
    Case("openai", "openai/gpt-5-mini", "gpt-5-mini",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-terra"),
    Case("openai", "openai/gpt-5-nano", "gpt-5-nano",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-luna"),
    Case("openai", "openai/gpt-5-pro", "gpt-5-pro",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-sol"),
    Case("openai", "openai/o3", "o3",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-sol"),
    Case("openai", "openai/o3-pro", "o3-pro",
         datetime(2026, 12, 11, tzinfo=UTC), "openai/gpt-5.6-sol"),
    Case("openai", "openai/gpt-5.3-codex", "gpt-5.3-codex",
         datetime(2027, 4, 1, tzinfo=UTC), "openai/gpt-6-sol"),
    Case("openai", "openai/gpt-5.1", "gpt-5.1",
         datetime(2027, 4, 1, tzinfo=UTC), "openai/gpt-6-sol"),
    Case("openai", "openai/gpt-5.4-nano", "gpt-5.4-nano",
         datetime(2027, 4, 1, tzinfo=UTC), "openai/gpt-6-luna"),
    Case("sail-research", "openai/gpt-oss-120b", "openai/gpt-oss-120b",
         datetime(2026, 11, 19, tzinfo=UTC), None),
    Case("atlas-cloud", "minimax/minimax-m3", "minimaxai/minimax-m3",
         datetime(2026, 11, 14, 8, tzinfo=UTC), None),
    Case("atlas-cloud", "minimax/minimax-m2.7", "minimaxai/minimax-m2.7",
         datetime(2026, 11, 14, 8, tzinfo=UTC), None),
    Case("deepinfra", "inclusionai/ling-3.1-flash", "inclusionAI/Ling-3.1-flash",
         datetime(2026, 11, 15, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("tinfoil", "moonshotai/kimi-k3", "kimi-k3",
         datetime(2026, 11, 12, tzinfo=UTC), "moonshotai/kimi-k3.1-pro"),
    Case("novita", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "deepseek/deepseek-v4-flash-0731"),
    Case("novita", "qwen/qwen3-235b-a22b-instruct-2507", "qwen/qwen3-235b-a22b-instruct-2507",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "qwen/qwen3.5-397b-a17b"),
    Case("tinfoil", "z-ai/glm-5.3-flash", "glm-5-3-flash",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("chutes", "mistralai/mistral-nemo", "unsloth/Mistral-Nemo-Instruct-2407-TEE",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "google/gemma-4-31b-turbo"),
    Case("chutes", "qwen/qwen3-235b-a22b-thinking-2507", "Qwen/Qwen3-235B-A22B-Thinking-2507-TEE",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "qwen/qwen3.5-397b-a17b"),
    Case("chutes", "qwen/qwen3-32b", "Qwen/Qwen3-32B-TEE",
         datetime(2026, 11, 6, 19, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("lyceum", "moonshotai/kimi-k2.7-code", "moonshotai/kimi-k2.7-code",
         datetime(2026, 11, 4, tzinfo=UTC), "moonshotai/kimi-k3"),
    Case("mistral", "mistralai/mistral-medium-3-5", "mistral-medium-3.5",
         datetime(2027, 4, 30, tzinfo=UTC), "mistralai/mistral-large-4"),
    Case("nebius", "zai-org/GLM-5.1", "zai-org/GLM-5.1",
         datetime(2026, 11, 9, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("nebius", "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-super-120b-a12b",
         datetime(2026, 11, 9, tzinfo=UTC), "nvidia/nemotron-3.5-super-vl"),
    Case("nebius", "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Pro",
         datetime(2026, 11, 9, tzinfo=UTC), "deepseek/deepseek-v4-pro-0813"),
    Case("nebius", "deepseek/deepseek-v4-flash-0731", "deepseek-ai/DeepSeek-V4-Flash-0731",
         datetime(2026, 11, 9, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("nebius", "deepseek/deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash",
         datetime(2026, 11, 9, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("nebius", "Qwen/Qwen3.5-397B-A17B", "Qwen/Qwen3.5-397B-A17B",
         datetime(2026, 11, 9, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("nebius", "Qwen/Qwen3-235B-A22B-Instruct-2507", "Qwen/Qwen3-235B-A22B-Instruct-2507",
         datetime(2026, 11, 9, tzinfo=UTC), "qwen/qwen3.8-27b"),
    Case("nebius", "moonshotai/Kimi-K2.6", "moonshotai/Kimi-K2.6",
         datetime(2026, 11, 9, tzinfo=UTC), "moonshotai/kimi-k3"),
    Case("nebius", "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.7-Code",
         datetime(2026, 11, 9, tzinfo=UTC), "moonshotai/kimi-k3"),
    Case("nebius", "openbmb/MiniCPM-V-4_5", "openbmb/MiniCPM-V-4_5",
         datetime(2026, 11, 9, tzinfo=UTC), "nvidia/nemotron-3.5-super-vl"),
    Case("decisionmodelsio", "decisionmodelsio/s1-pro-2", "s1-pro-2",
         datetime(2026, 11, 30, 23, tzinfo=UTC), "decisionmodelsio/s1-pro"),
    Case("decisionmodelsio", "decisionmodelsio/s1-pro-3", "s1-pro-3",
         datetime(2026, 11, 30, 23, tzinfo=UTC), "decisionmodelsio/s1-pro"),
    Case("decisionmodelsio-eu", "decisionmodelsio-eu/s1-pro-2", "s1-pro-2",
         datetime(2026, 11, 30, 23, tzinfo=UTC), "decisionmodelsio-eu/s1-pro"),
    Case("decisionmodelsio-eu", "decisionmodelsio-eu/s1-pro-3", "s1-pro-3",
         datetime(2026, 11, 30, 23, tzinfo=UTC), "decisionmodelsio-eu/s1-pro"),
    Case("digitalocean", "meta-llama/llama-4-maverick", "llama-4-maverick",
         datetime(2026, 11, 9, tzinfo=UTC), "deepseek/deepseek-v4.1-flash"),
    Case("digitalocean", "deepseek/deepseek-v4-flash", "deepseek-4-flash",
         datetime(2026, 11, 9, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("digitalocean", "qwen/qwen3.5-397b-a17b", "qwen3.5-397b-a17b",
         datetime(2026, 11, 9, tzinfo=UTC), "qwen/qwen3.8-max"),
    Case("digitalocean", "google/gemma-4-31b-it", "gemma-4-31B-it",
         datetime(2026, 11, 9, tzinfo=UTC), "z-ai/glm-5.3-flash"),
    Case("together", "qwen/qwen3.8-2.4t-a95b", "Qwen/Qwen3.8-2.4T-A95B",
         datetime(2026, 11, 4, tzinfo=UTC), "qwen/qwen3.7-max"),
    Case("deepinfra", "stepfun-ai/step-5-preview", "stepfun-ai/Step-5-Preview",
         datetime(2026, 11, 4, tzinfo=UTC), "z-ai/glm-5.3-flash"),
    Case("deepinfra", "meta-llama/llama-3.1-70b-instruct", "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
         datetime(2026, 11, 3, tzinfo=UTC), "meta-llama/llama-3.3-70b-instruct"),
    Case("inception", "inception/mercury-2", "mercury-2",
         datetime(2026, 11, 15, tzinfo=UTC), "inception/mercury-3"),
    Case("inception", "inception/mercury-2.5", "mercury-2.5",
         datetime(2026, 11, 15, tzinfo=UTC), "inception/mercury-3"),
    Case("telnyx", "meta-llama/llama-3.1-70b-instruct", "meta-llama/Meta-Llama-3.1-70B-Instruct",
         datetime(2026, 11, 2, tzinfo=UTC), "meta-llama/llama-3.3-70b-instruct"),
    Case("telnyx", "meta-llama/llama-3.1-8b-instruct", "meta-llama/Meta-Llama-3.1-8B-Instruct",
         datetime(2026, 11, 2, tzinfo=UTC), "meta-llama/llama-3.3-70b-instruct"),
    Case("telnyx", "google/gemma-2b-it", "google/gemma-2b-it",
         datetime(2026, 11, 2, tzinfo=UTC), "meta-llama/llama-3.3-70b-instruct"),
    Case("telnyx", "minimax/minimax-m2.7", "MiniMaxAI/MiniMax-M2.7",
         datetime(2026, 11, 2, tzinfo=UTC), "minimax/minimax-m3"),
    Case("telnyx", "moonshotai/kimi-k2.5", "moonshotai/Kimi-K2.5",
         datetime(2026, 11, 2, tzinfo=UTC), "moonshotai/kimi-k2.6"),
    Case("telnyx", "z-ai/glm-5.1", "zai-org/GLM-5.1-FP8",
         datetime(2026, 11, 2, tzinfo=UTC), "z-ai/glm-5.3-flash"),
    Case("together", "minimax/minimax-m3", "MiniMaxAI/MiniMax-M3",
         datetime(2026, 11, 6, tzinfo=UTC), "minimax/minimax-m3.1-flash"),
    Case("together", "thinkingmachines/inkling", "thinkingmachines/Inkling",
         datetime(2026, 11, 6, tzinfo=UTC), "meta-models/muse-spark-open"),
    Case("crusoe", "deepseek/deepseek-v4-pro", "deepseek-ai/DeepSeek-V4-Pro",
         datetime(2026, 10, 30, 17, tzinfo=UTC), "deepseek/deepseek-v4.1-flash"),
    Case("crusoe", "deepseek/deepseek-v4-flash", "deepseek-ai/Deepseek-V4-Flash",
         datetime(2026, 10, 30, 17, tzinfo=UTC), "deepseek/deepseek-v4.1-flash"),
    Case("crusoe", "nvidia/nemotron-3-super-120b-a12b", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
         datetime(2026, 10, 30, 17, tzinfo=UTC), "nvidia/nemotron-3.5-super-vl"),
    Case("crusoe", "nvidia/nemotron-3-nano-omni-reasoning-30b-a3b", "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B",
         datetime(2026, 10, 30, 17, tzinfo=UTC), "nvidia/nemotron-3.5-lightning-30b-a3b"),
    Case("deepinfra", "gryphe/mythomax-l2-13b", "Gryphe/MythoMax-L2-13b",
         datetime(2026, 10, 29, tzinfo=UTC), "sao10k/l3-8b-lunaris-v1-turbo"),
    Case("arcee", "arcee-ai/trinity-large-thinking", "trinity-large-thinking",
         datetime(2026, 10, 26, tzinfo=UTC), "moonshotai/kimi-k3"),
    Case("novita", "xiaomimimo/mimo-v2.5", "xiaomimimo/mimo-v2.5",
         datetime(2026, 10, 23, tzinfo=UTC), "xiaomi/mimo-v2.6-flash"),
    Case("novita", "xiaomimimo/mimo-v2.5-pro", "xiaomimimo/mimo-v2.5-pro",
         datetime(2026, 10, 23, tzinfo=UTC), "xiaomi/mimo-v2.6-pro"),
    Case("deepinfra", "nousresearch/hermes-3-llama-3.1-405b", "NousResearch/Hermes-3-Llama-3.1-405B",
         datetime(2026, 11, 2, tzinfo=UTC), "deepseek/deepseek-v4.1-flash-1017"),
    Case("alibaba", "qwen/qwen3.5-plus", "qwen3.5-plus",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("alibaba", "qwen/qwen3.5-plus-2026-02-15", "qwen3.5-plus-2026-02-15",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("alibaba", "qwen/qwen3.5-397b-a17b", "qwen3.5-397b-a17b",
         datetime(2027, 1, 19, 2, tzinfo=UTC), "qwen/qwen3.8-flash"),
    Case("deepinfra", "openai/gpt-oss-120b-turbo", "openai/gpt-oss-120b-Turbo",
         datetime(2026, 10, 20, tzinfo=UTC), "openai/gpt-oss-120b"),
    Case("scaleway", "z-ai/glm-5.2", "glm-5.2",
         datetime(2026, 11, 16, tzinfo=UTC), "z-ai/glm-5.3"),
    Case("deepinfra", "thinkingmachines/inkling", "thinkingmachines/Inkling",
         datetime(2026, 10, 22, tzinfo=UTC), "thinkingmachines/inkling-small"),
    Case("deepinfra", "ibm-granite/granite-4.2-3b", "ibm-granite/granite-4.2-3b",
         datetime(2026, 10, 18, tzinfo=UTC), "ibm-granite/granite-4.2-8b"),
    Case("deepinfra", "ibm-granite/granite-4.2-30b", "ibm-granite/granite-4.2-30b",
         datetime(2026, 10, 18, tzinfo=UTC), "ibm-granite/granite-4.2-8b"),
    Case("deepinfra", "qwen/qwen3-max", "Qwen/Qwen3-Max",
         datetime(2026, 10, 10, tzinfo=UTC), "qwen/qwen3.8-max"),
    Case("deepinfra", "qwen/qwen3-max-thinking", "Qwen/Qwen3-Max-Thinking",
         datetime(2026, 10, 10, tzinfo=UTC), "qwen/qwen3.8-max"),
    Case("xiaomi", "xiaomi/mimo-v2.5-pro", "mimo-v2.5-pro",
         datetime(2026, 10, 14, 10, tzinfo=UTC), "xiaomi/mimo-v2.6-pro"),
    Case("xiaomi", "xiaomi/mimo-v2.5", "mimo-v2.5",
         datetime(2026, 10, 14, 10, tzinfo=UTC), "xiaomi/mimo-v2.6-flash"),
]
_MANIFESTS = {
    "decisionmodelsio": system1models.MANIFEST_PATH,
    "decisionmodelsio-eu": system1models_eu.MANIFEST_PATH,
    "digitalocean": digitalocean.MANIFEST_PATH,
    "friendli": friendli.MANIFEST_PATH,
    "google-ai-studio": gemini.MANIFEST_PATH,
    "inception": inception.MANIFEST_PATH,
    "lyceum": lyceum.MANIFEST_PATH,
    "mistral": mistral.MANIFEST_PATH,
    "nebius": nebius.MANIFEST_PATH,
    "telnyx": telnyx.MANIFEST_PATH,
    "tinfoil": tinfoil.MANIFEST_PATH,
    "together": together.MANIFEST_PATH,
    "crusoe": crusoe.MANIFEST_PATH,
    "arcee": arcee.MANIFEST_PATH,
    "cloudflare-workers-ai": cloudflare_workers_ai.MANIFEST_PATH,
    "chutes": chutes.MANIFEST_PATH,
    "novita": novita.MANIFEST_PATH,
    "alibaba": alibaba.MANIFEST_PATH,
    "anthropic": anthropic.MANIFEST_PATH,
    "deepinfra": deepinfra.MANIFEST_PATH,
    "scaleway": scaleway.MANIFEST_PATH,
    "xiaomi": xiaomi.MANIFEST_PATH,
    "tencent": tencent.MANIFEST_PATH,
    "databricks": databricks.MANIFEST_PATH,
    "openai": openai.MANIFEST_PATH,
    "sail-research": sail_research.MANIFEST_PATH,
    "atlas-cloud": atlas_cloud.MANIFEST_PATH,
}


def test_xiaomi_cutoff_is_the_substitution_start_not_the_shutoff() -> None:
    # 18:00 in UTC+8 on October 14; the IDs only error from October 21.
    assert provider_lifecycle.XIAOMI_MIMO_V25_RETIREMENT_AT == datetime(2026, 10, 14, 10, tzinfo=UTC)
    assert provider_lifecycle.DEEPINFRA_QWEN3_MAX_THINKING_RETIREMENT_AT == datetime(
        2026, 10, 10, tzinfo=UTC,
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_route_routed_before_substitution_retired_after(
    monkeypatch: pytest.MonkeyPatch, case: Case,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    before = case.cutoff - timedelta(microseconds=1)
    assert not retired(case.provider, case.model, case.native, at=before)
    assert retired(case.provider, case.model, at=case.cutoff)
    assert retired(case.provider, "unknown-canonical", case.native, at=case.cutoff)
    assert not retired("unaffected-provider", case.model, case.native, at=case.cutoff)
    if case.replacement is not None:
        assert not retired(case.provider, case.replacement, at=case.cutoff)
    endpoints = {}
    for slug in (case.provider, "unaffected-provider"):
        endpoint = ModelEndpoint(
            id=f"{case.model}@{slug}/Credits", model_id=case.model, provider=slug,
            usage_type="Credits", upstream_id=case.native,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: before)
    assert len(catalog.endpoints_for_model(case.model)) == 2
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: case.cutoff)
    assert [row.provider for row in catalog.endpoints_for_model(case.model)] == [
        "unaffected-provider",
    ]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.provider}:{case.model}")
def test_replacement_is_quoted_and_manifest_annotated(case: Case) -> None:
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == case.provider and case.model in entry.model_ids
        and entry.effective_at == case.cutoff
    ]
    assert len(entries) == 1
    expected = () if case.replacement is None else (case.replacement,)
    assert entries[0].replacement_model_ids == expected
    rows = {row["id"]: row for row in json.loads(_MANIFESTS[case.provider].read_text())["models"]}
    assert rows[case.model]["upstream_id"] == case.native
    assert rows[case.model]["retirement_at"] == case.cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rows[case.model].get("replacement_model_id") == case.replacement
