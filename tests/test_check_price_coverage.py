"""Tests for scripts/check_price_coverage.py (price-source coverage audit)."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import urllib.error
from pathlib import Path

import pytest

from scripts import check_price_coverage
from scripts.check_price_coverage import audit

_NEW_AUTOMATIC_FEED_MODELS = {
    "aion-labs/aion-3.0",
    "arcee-ai/trinity-large-thinking",
    "openai/gpt-5.6-sol",
    "openai/gpt-oss-120b",
    "upstage/solar-pro4",
    "reka/reka-edge-2603",
    "inception/mercury-2",
    "x-ai/grok-4.6",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-flash-0731",
    "mistralai/mistral-small-2603",
    "z-ai/glm-5.2",
    "sakana-ai/fugu-ultra-v1.1",
    "sakana-ai/sakana-namazu-v1.0",
}
_NEW_AUTOMATIC_FEED_ALIASES = {
    "deepseek-v4-flash",
    "glm-5.2",
    "gpt-5.6-sol",
    "grok-4.6",
    "mistral-small-2603",
}
_NEW_AUTOMATIC_FEED_ROWS = _NEW_AUTOMATIC_FEED_MODELS | _NEW_AUTOMATIC_FEED_ALIASES


def _known_provider_model_payload(url: str, _env_names: tuple[str, ...]) -> dict:
    if "api.openai.com" in url:
        return {"data": [{"id": "gpt-5.6-sol"}]}
    if "api.x.ai" in url:
        return {"models": [{"id": "grok-4.6"}]}
    if "api.deepseek.com" in url:
        return {"data": [{"id": "deepseek-v4-flash"}]}
    if "api.mistral.ai" in url:
        return {"data": [{"id": "mistral-small-2603"}]}
    if "api.z.ai" in url:
        return {"data": [{"id": "glm-5.2"}]}
    if "api.moonshot.ai" in url:
        return {"data": [{"id": "kimi-k2.7-code"}]}
    if "cerebras.ai" in url:
        return {"data": [{"id": "gpt-oss-120b"}]}
    if "generativelanguage.googleapis.com" in url:
        return {"models": [{"name": "models/gemini-3.5-flash"}]}
    if "api.minimax.io" in url:
        return {"data": [{"id": "MiniMax-M3"}]}
    if "api.fireworks.ai" in url:
        return {"data": [{"id": "accounts/fireworks/models/gpt-oss-120b"}]}
    if "tokenfactory.nebius.com" in url:
        return {"data": [{"id": "meta-llama/Llama-3.3-70B-Instruct"}]}
    if "api.novita.ai" in url:
        return {"data": [{"id": "deepseek/deepseek-v4-flash"}]}
    if "api.friendli.ai" in url:
        return {"data": [{"id": "meta-llama-3.3-70b-instruct"}]}
    if "inference.baseten.co" in url:
        return {"data": [{"id": "zai-org/GLM-5.2"}]}
    if "api.telnyx.com" in url:
        return {"data": [{"id": "moonshotai/Kimi-K3"}]}
    if "pass.wafer.ai" in url:
        return {"data": [{"id": "GLM-5.2"}]}
    if "api.inference.crusoecloud.com" in url:
        return {"data": [{"id": "zai/GLM-5.2"}]}
    if "maas.aliyuncs.com" in url:
        return {"data": [{"id": "glm-5.2"}]}
    if "inference.makora.com" in url:
        return {"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash"}]}
    if "wharf.neurometric.ai" in url:
        return {"data": [{"id": "ibm-granite/granite-4.1-8b"}]}
    if "api.engy.ai" in url:
        return {"data": [{"id": "glm-5.2"}]}
    if "inference.pearlresearch.ai" in url:
        return {"data": [{"id": "deepseek/deepseek-v4-flash"}]}
    if "api.upstage.ai" in url:
        return {"data": [{"id": "solar-pro4"}]}
    if "api.sailresearch.com" in url:
        return {"data": [{"id": "zai-org/GLM-5.2-FP8"}]}
    if "api.reka.ai" in url:
        return {"data": [{"id": "reka-edge-2603"}]}
    if "api.nextbit256.com" in url:
        return {"data": [{"id": "deepseek:v4-flash-0731"}]}
    if "api.akashml.com" in url:
        return {"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash-0731"}]}
    if "mancer.tech" in url:
        return {"data": [{"id": "deepseek-v4-flash-0731"}]}
    if "api.aionlabs.ai" in url:
        return {"data": [{"id": "aion-labs/aion-3.0"}]}
    if "api.sambanova.ai" in url:
        return {"data": [{"id": "gpt-oss-120b"}]}
    if "api.arcee.ai" in url:
        return {"data": [{"id": "trinity-large-thinking"}]}
    if "api.inceptionlabs.ai" in url:
        return {"data": [{"id": "mercury-2"}]}
    if "api.intelligence.io.solutions" in url:
        return {"data": [{"id": "deepseek-ai/DeepSeek-V4-Flash-0731"}]}
    if "api.scaleway.ai" in url:
        return {"data": [{"id": "glm-5.2"}]}
    if "api.featherless.ai" in url:
        return {"data": [{"id": "zai-org/GLM-5.2"}]}
    if "api.sakana.ai" in url:
        return {
            "data": [
                {"id": "fugu-ultra-v1.1"},
                {"id": "sakana-namazu-v1.0"},
            ]
        }
    if "api.inference.wandb.ai" in url:
        return {"data": [{"id": "zai-org/GLM-5.2"}]}
    if "cloud-api.near.ai" in url:
        return {"data": [{"id": "z-ai/glm-5.2"}]}
    return {"data": []}


def test_audit_reports_embedding_provider_scrapers_as_covered() -> None:
    now = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
    warnings, info, hard_failures = audit(
        max_age_days=14,
        now=now,
        check_model_discovery=False,
    )
    assert hard_failures == []
    assert not any("cohere" in warning for warning in warnings), warnings
    assert not any("voyage" in warning for warning in warnings), warnings
    assert "cohere: live scraper ✓" in info
    assert "voyage: live scraper ✓" in info
    # Live-scraped providers are reported as covered.
    assert any("openai" in i for i in info), info


def test_shared_gemini_scraper_covers_ai_studio_and_vertex() -> None:
    now = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
    warnings, info, hard_failures = audit(
        max_age_days=14,
        now=now,
        check_model_discovery=False,
    )
    assert hard_failures == []

    assert not any("google-vertex: NO price source" in warning for warning in warnings)
    assert "google-ai-studio: live scraper ✓" in info
    assert "google-vertex: live scraper ✓" in info


@pytest.mark.parametrize("manifest_state", ["missing", "invalid", "stale"])
def test_prepaid_provider_without_current_price_source_is_a_hard_failure(
    manifest_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import trusted_router.catalog as catalog

    monkeypatch.setattr(catalog, "GATEWAY_PREPAID_PROVIDER_SLUGS", {"test-provider"})
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(check_price_coverage, "_scraper_slugs", lambda: set())
    monkeypatch.setattr(check_price_coverage, "_OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS", set())
    monkeypatch.setattr(check_price_coverage, "EXPIRING_PROVIDER_MANIFEST_SLUGS", set())
    monkeypatch.setattr(check_price_coverage, "_RUNTIME_ONLY_DISCOVERY_SLUGS", set())
    monkeypatch.setattr(check_price_coverage, "_DIRECT_OPENAI_DISCOVERY_SLUGS", frozenset())

    manifest = tmp_path / "test-provider.json"
    if manifest_state == "invalid":
        manifest.write_text('{"models": []}', encoding="utf-8")
    elif manifest_state == "stale":
        manifest.write_text(
            '{"generated_at": "2026-07-01T00:00:00+00:00", "models": []}',
            encoding="utf-8",
        )

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        check_model_discovery=False,
    )

    warning = next(item for item in warnings if item.startswith("test-provider:"))
    assert warning in hard_failures


def test_scraper_slugs_use_public_provider_slug_format() -> None:
    scrapers = check_price_coverage._scraper_slugs()

    assert "cloudflare-workers-ai" in scrapers
    assert "atlas-cloud" in scrapers
    assert "zero-g" in scrapers
    assert "cloudflare_workers_ai" not in scrapers


def test_authenticated_model_discovery_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_fetch_json(
        url: str,
        *,
        extra_headers: dict[str, str],
        follow_redirects: bool,
    ) -> dict[str, list[object]]:
        seen.update(
            url=url,
            authorization=extra_headers.get("Authorization"),
            follow_redirects=follow_redirects,
        )
        return {"data": []}

    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "secret-token")
    monkeypatch.setattr(check_price_coverage, "fetch_provider_json", fake_fetch_json)

    assert check_price_coverage._fetch_json(
        "https://provider.example/v1/models",
        ("TEST_PROVIDER_API_KEY",),
    ) == {"data": []}
    assert seen == {
        "url": "https://provider.example/v1/models",
        "authorization": "Bearer secret-token",
        "follow_redirects": False,
    }


def test_gemini_model_discovery_keeps_api_key_out_of_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_fetch_json(
        url: str,
        *,
        extra_headers: dict[str, str],
        follow_redirects: bool,
    ) -> dict[str, list[object]]:
        seen.update(url=url, headers=extra_headers, follow_redirects=follow_redirects)
        return {"models": []}

    monkeypatch.setenv("GEMINI_TEST_KEY", "secret-token")
    monkeypatch.setattr(check_price_coverage, "fetch_provider_json", fake_fetch_json)
    url = "https://generativelanguage.googleapis.com/v1beta/models"

    assert check_price_coverage._fetch_json(url, ("GEMINI_TEST_KEY",)) == {"models": []}
    assert seen["url"] == url
    assert seen["follow_redirects"] is False
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-goog-api-key"] == "secret-token"


def test_stale_fallback_manifests_are_age_gated_even_with_live_scrapers() -> None:
    raw = json.loads(
        check_price_coverage.MANIFEST_DIR.joinpath("upstage.json").read_text(encoding="utf-8")
    )
    generated = dt.datetime.fromisoformat(raw["generated_at"].replace("Z", "+00:00"))

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        generated + dt.timedelta(days=15),
        check_model_discovery=False,
    )

    warning = next(item for item in warnings if item.startswith("upstage:"))
    assert "live scraper fallback manifest is 15d stale" in warning
    assert warning not in hard_failures


def test_discovery_only_non_runtime_manifest_warns_without_global_freeze() -> None:
    raw = json.loads(
        check_price_coverage.MANIFEST_DIR.joinpath("stepfun.json").read_text(encoding="utf-8")
    )
    generated = dt.datetime.fromisoformat(raw["generated_at"].replace("Z", "+00:00"))

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        generated + dt.timedelta(days=15),
        check_model_discovery=False,
    )

    warning = next(item for item in warnings if item.startswith("stepfun:"))
    assert "live scraper fallback manifest is 15d stale" in warning
    assert warning not in hard_failures


def test_discovery_only_fallback_manifests_are_age_gated() -> None:
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS

    assert "nvidia-nim" not in GATEWAY_PREPAID_PROVIDER_SLUGS
    raw = json.loads(
        check_price_coverage.MANIFEST_DIR.joinpath("nvidia-nim.json").read_text(encoding="utf-8")
    )
    generated = dt.datetime.fromisoformat(raw["generated_at"].replace("Z", "+00:00"))

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        generated + dt.timedelta(days=15),
        check_model_discovery=False,
    )

    warning = next(item for item in warnings if item.startswith("nvidia-nim:"))
    assert "live scraper fallback manifest fails runtime route validity checks" in warning
    assert warning not in hard_failures


def test_manifest_expiry_warns_before_provider_routes_go_dark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    manifest = json.loads(
        check_price_coverage.MANIFEST_DIR.joinpath("upstage.json").read_text(encoding="utf-8")
    )
    manifest["generated_at"] = generated.isoformat()
    tmp_path.joinpath("upstage.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    warning, covered = check_price_coverage._audit_fallback_manifest(
        "upstage",
        max_age_days=14,
        now=generated + dt.timedelta(days=12),
    )

    assert warning is not None
    assert "expires in 2d" in warning
    assert covered is None


def test_manifest_audit_rejects_media_price_runtime_would_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.joinpath("bfl.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-22T00:00:00+00:00",
                "models": [
                    {
                        "id": "black-forest-labs/bad-image-price",
                        "model_type": "image",
                        "routable": True,
                        "fixed_output_price_microdollars": {"1k": 0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    warning, covered = check_price_coverage._audit_fallback_manifest(
        "bfl",
        max_age_days=14,
        now=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
    )

    assert warning == ("bfl: live scraper fallback manifest fails runtime route validity checks")
    assert covered is None


def test_manifest_audit_rejects_naive_timestamp_runtime_would_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = json.loads(
        check_price_coverage.MANIFEST_DIR.joinpath("upstage.json").read_text(encoding="utf-8")
    )
    manifest["generated_at"] = "2026-08-22T00:00:00"
    tmp_path.joinpath("upstage.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    warning, covered = check_price_coverage._audit_fallback_manifest(
        "upstage",
        max_age_days=14,
        now=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
    )

    assert warning == (
        "upstage: live scraper fallback manifest fails runtime route validity checks"
    )
    assert covered is None


def test_transient_docs_fetch_failure_does_not_block_price_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = "zai: model discovery fetch failed (HTTPError status=503 host=docs.z.ai)"
    monkeypatch.setattr(
        check_price_coverage,
        "_model_discovery_audit",
        lambda **_kwargs: ([warning], []),
    )

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        check_model_discovery=True,
        fetch_text=lambda _url: "",
    )

    assert warning in warnings
    assert warning not in hard_failures


def test_every_manifest_fallback_provider_is_age_gated() -> None:
    expected = set()
    for path in check_price_coverage.PROVIDERS_DIR.glob("*.py"):
        if path.name == "__init__.py" or path.stem.startswith("_"):
            continue
        module = importlib.import_module(f"scripts.pricing.providers.{path.stem}")
        if bool(getattr(module, "MANIFEST_STALE_FALLBACK", False)):
            expected.add(module.SLUG)

    assert check_price_coverage._OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS == expected
    assert check_price_coverage.EXPIRING_PROVIDER_MANIFEST_SLUGS == expected


def test_runtime_only_discovery_is_always_backed_by_prepaid_age_gate() -> None:
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS

    assert check_price_coverage._RUNTIME_ONLY_DISCOVERY_SLUGS <= (
        set(GATEWAY_PREPAID_PROVIDER_SLUGS)
        & set(check_price_coverage._OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS)
    )


def test_invalid_runtime_fallback_manifest_is_quarantined_without_global_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)
    (tmp_path / "upstage.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-22T00:00:00Z",
                "models": [
                    {
                        "id": "upstage/bad-price",
                        "routable": True,
                        "input_token_price_per_m": 0,
                        "output_token_price_per_m": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    warnings, _info, hard_failures = check_price_coverage._run_audit(
        14,
        dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC),
        check_model_discovery=False,
    )

    warning = next(item for item in warnings if item.startswith("upstage:"))
    assert "manifest fails runtime route validity checks" in warning
    assert warning not in hard_failures


def test_runtime_only_discovery_without_credentials_is_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in check_price_coverage._DIRECT_OPENAI_DISCOVERY_MODULES:
        for env_name in module.CATALOG.api_key_envs:
            monkeypatch.delenv(env_name, raising=False)

    fetched_urls: list[str] = []

    def fake_fetch_json(url: str, env_names: tuple[str, ...]) -> dict:
        fetched_urls.append(url)
        return _known_provider_model_payload(url, env_names)

    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-5.2"} | _NEW_AUTOMATIC_FEED_ROWS,
    )

    for module in check_price_coverage._DIRECT_OPENAI_DISCOVERY_MODULES:
        assert any(
            item.startswith(f"{module.SLUG}: authenticated discovery intentionally disabled")
            for item in info
        )
        assert not any(module.SLUG in item and "fetch failed" in item for item in warnings)
        expected_url = module.CATALOG.spec.catalog_url or (
            f"{module.CATALOG.spec.base_url.rstrip('/')}/models"
        )
        assert expected_url not in fetched_urls


def test_discovery_errors_never_publish_exception_details() -> None:
    sensitive_value = "signed-token-that-must-not-appear"
    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: (_ for _ in ()).throw(RuntimeError(sensitive_value)),
        fetch_json=_known_provider_model_payload,
        published_model_ids=_NEW_AUTOMATIC_FEED_ROWS,
    )

    assert sensitive_value not in "\n".join(warnings)
    assert any("RuntimeError host=docs.z.ai" in item for item in warnings)


def test_urllib_discovery_errors_keep_status_without_url_details() -> None:
    url = "https://docs.z.ai/models?signature=secret"
    error = urllib.error.HTTPError(url, 403, "forbidden", hdrs=None, fp=None)

    assert check_price_coverage._safe_fetch_error(url, error) == (
        "HTTPError status=403 host=docs.z.ai"
    )


def test_zai_model_discovery_extracts_glm_ids_from_docs() -> None:
    text = """
    The GLM Coding Plan now supports GLM-5.2.
    Use `ANTHROPIC_DEFAULT_OPUS_MODEL`: `glm-5.2[1m]`.
    Fallbacks: GLM-4.7 and GLM-4.5-Air.
    """

    assert check_price_coverage._discover_zai_coding_plan_models(text) == {
        "z-ai/glm-4.5-air",
        "z-ai/glm-4.7",
        "z-ai/glm-5.2",
    }


def test_provider_glm_model_discovery_normalizes_native_ids() -> None:
    payload = {
        "data": [
            {"id": "zai-org/GLM-5.2"},
            {"id": "zai-org/GLM-5.2-FP8"},
            {"id": "accounts/fireworks/models/glm-5p2"},
            {"id": "zai-org/glm-5.1"},
            {"id": "not-a-glm-model"},
        ]
    }

    assert check_price_coverage._provider_glm_model_ids(payload) == {
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }


def test_kimi_discovery_ignores_unpriced_auto_alias() -> None:
    assert check_price_coverage._kimi_model_id("moonshot-v1-auto") is None
    assert check_price_coverage._kimi_model_id("kimi-k2.7-code") == "moonshotai/kimi-k2.7-code"


def test_cerebras_discovery_uses_canonical_ids_and_ignores_unknown_models() -> None:
    assert check_price_coverage._cerebras_model_id("gpt-oss-120b") == ("openai/gpt-oss-120b")
    assert check_price_coverage._cerebras_model_id("zai-glm-4.7") == "z-ai/glm-4.7"
    assert check_price_coverage._cerebras_model_id("gemma-4-31b") == ("google/gemma-4-31b-it")
    assert check_price_coverage._cerebras_model_id("qwen-3.8-27b") == "qwen/qwen3.8-27b"
    assert check_price_coverage._cerebras_model_id("qwen3.8-27b") == "qwen/qwen3.8-27b"
    assert check_price_coverage._cerebras_model_id("unknown") is None


def test_baseten_discovery_maps_glm_fast_native_id() -> None:
    assert check_price_coverage._baseten_model_id("zai-org/GLM-5.2-Fast") == "z-ai/glm-5.2-fast"


def test_fireworks_discovery_maps_kimi_k3_native_id() -> None:
    assert (
        check_price_coverage._fireworks_model_id("accounts/fireworks/models/kimi-k3")
        == "moonshotai/kimi-k3"
    )


def test_novita_discovery_ignores_internal_aliases_but_catches_public_families() -> None:
    assert check_price_coverage._novita_model_id("ai_infer_test_1") is None
    assert check_price_coverage._novita_model_id("bunny") is None
    assert check_price_coverage._novita_model_id("gt-4p") is None
    assert check_price_coverage._novita_model_id("gpt-image-2-oai") is None
    assert check_price_coverage._novita_model_id("Kimi-K3") == "moonshotai/kimi-k3"
    assert check_price_coverage._novita_model_id("vendor/Future-Text-1") == ("vendor/future-text-1")


def test_provider_glm_required_gate_targets_current_flagships() -> None:
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.2")
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.3")
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-6")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.1")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5-turbo")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-4.7-h")


def test_direct_provider_discovery_uses_route_canonical_ids() -> None:
    normalizers = {
        slug: normalize
        for slug, _url, _env_names, normalize in (
            check_price_coverage._DISCOVERABLE_MANIFEST_PROVIDERS
        )
    }

    assert normalizers["upstage"]("solar-pro4") == "upstage/solar-pro4"
    assert normalizers["sail-research"]("zai-org/GLM-5.2-FP8") == "z-ai/glm-5.2"
    assert normalizers["reka"]("reka-edge-2603") == "reka/reka-edge-2603"
    assert normalizers["nextbit"]("deepseek:v4-flash-0731") == ("deepseek/deepseek-v4-flash-0731")
    assert normalizers["mancer"]("future-model") is None
    assert normalizers["arcee"]("trinity-large-thinking") == ("arcee-ai/trinity-large-thinking")


def test_direct_provider_discovery_configuration_comes_from_each_catalog() -> None:
    modules = (
        check_price_coverage.upstage,
        check_price_coverage.sail_research,
        check_price_coverage.reka,
        check_price_coverage.nextbit,
        check_price_coverage.akashml,
        check_price_coverage.mancer,
        check_price_coverage.aion_labs,
        check_price_coverage.sambanova,
        check_price_coverage.arcee,
        check_price_coverage.inception,
    )
    configured = {
        slug: (url, env_names, normalize)
        for slug, url, env_names, normalize in (
            check_price_coverage._DISCOVERABLE_MANIFEST_PROVIDERS
        )
    }
    for module in modules:
        expected_url = module.CATALOG.spec.catalog_url or (
            f"{module.CATALOG.spec.base_url.rstrip('/')}/models"
        )
        url, env_names, normalize = configured[module.SLUG]
        assert url == expected_url
        assert env_names == module.CATALOG.api_key_envs
        assert normalize("future-model") == module.CATALOG.model_id("future-model")


def test_model_discovery_warns_when_docs_mention_unpublished_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_price_coverage, "_GLM_DISCOVERABLE_PROVIDER_APIS", ())
    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2, GLM-4.7",
        fetch_json=_known_provider_model_payload,
        published_model_ids={"z-ai/glm-4.7"} | (_NEW_AUTOMATIC_FEED_ROWS - {"z-ai/glm-5.2"}),
    )

    assert any(item.startswith("cerebras: model discovery matched catalog") for item in info)
    assert len(warnings) == 1
    assert "z-ai/glm-5.2" in warnings[0]


def test_zai_fetch_failure_does_not_skip_other_provider_discovery() -> None:
    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: (_ for _ in ()).throw(OSError("temporary DNS failure")),
        fetch_json=_known_provider_model_payload,
        published_model_ids={
            "moonshotai/kimi-k2.7-code",
            "openai/gpt-oss-120b",
            "google/gemini-3.5-flash",
            "minimax/minimax-m3",
            "z-ai/glm-5.2",
        },
    )

    assert any(warning.startswith("zai: model discovery fetch failed") for warning in warnings)
    assert any(item.startswith("cerebras: model discovery matched") for item in info)
    assert any(item.startswith("kimi: model discovery matched") for item in info)


def test_model_discovery_reports_match_when_docs_models_are_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_price_coverage, "_GLM_DISCOVERABLE_PROVIDER_APIS", ())
    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2, GLM-4.7",
        fetch_json=_known_provider_model_payload,
        published_model_ids={"z-ai/glm-5.2", "z-ai/glm-4.7"} | _NEW_AUTOMATIC_FEED_ROWS,
    )

    assert warnings == []
    assert "zai: model discovery matched catalog (2 docs model(s)) ✓" in info
    assert any(item.startswith("minimax: model discovery matched catalog") for item in info)


def test_provider_model_discovery_warns_on_unpublished_manifest_model() -> None:
    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "api.minimax.io" in url:
            return {"data": [{"id": "MiniMax-M9"}]}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-5.2"},
    )

    assert any("minimax/minimax-m9" in warning for warning in warnings)


def test_existing_provider_native_alias_does_not_reappear_as_canonical_launch(
    monkeypatch,
    tmp_path,
) -> None:  # noqa: ANN001
    manifest_dir = tmp_path / "provider_models"
    manifest_dir.mkdir()
    (manifest_dir / "novita.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "zai-org/glm-4.6",
                        "upstream_id": "zai-org/glm-4.6",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", manifest_dir)

    def fake_fetch_json(url: str, env_names: tuple[str, ...]) -> dict:
        if "api.novita.ai" in url:
            return {
                "data": [
                    {
                        "id": "zai-org/glm-4.6",
                        "status": 1,
                        "endpoints": ["chat/completions"],
                        "output_modalities": ["text"],
                    }
                ]
            }
        return _known_provider_model_payload(url, env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-5.2"},
    )

    assert not any("novita: live model API lists required unpublished" in item for item in warnings)


def test_new_awaiting_price_row_is_not_hidden_by_global_model(
    monkeypatch,
    tmp_path,
) -> None:  # noqa: ANN001
    manifest_dir = tmp_path / "provider_models"
    manifest_dir.mkdir()
    (manifest_dir / "novita.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "vendor/new-model",
                        "upstream_id": "vendor/new-model",
                        "routable": False,
                        "routable_reason": "awaiting-price",
                        "unresolved_since": "2026-07-21",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", manifest_dir)

    def fake_fetch_json(url: str, env_names: tuple[str, ...]) -> dict:
        if "api.novita.ai" in url:
            return {
                "data": [
                    {
                        "id": "vendor/new-model",
                        "status": 1,
                        "endpoints": ["chat/completions"],
                        "output_modalities": ["text"],
                    }
                ]
            }
        return _known_provider_model_payload(url, env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=fake_fetch_json,
        published_model_ids={"vendor/new-model", "z-ai/glm-5.2"},
    )

    assert any(
        warning.startswith("novita: newly discovered required model(s) still await a price")
        for warning in warnings
    )


def test_inactive_provider_rows_do_not_trigger_model_discovery() -> None:
    assert not check_price_coverage._active_discovery_row(
        {
            "id": "vendor/retired",
            "status": 4,
            "endpoints": ["chat/completions"],
            "output_modalities": ["text"],
        }
    )
    assert not check_price_coverage._active_discovery_row(
        {
            "model": "zai-org/GLM-5.2-FP8-Lora",
            "type": "serverless",
            "state": "STOPPED",
        }
    )
    assert not check_price_coverage._active_discovery_row(
        {
            "id": "vendor/image-only",
            "status": 1,
            "endpoints": ["chat/completions"],
            "output_modalities": ["image"],
        }
    )


def test_provider_glm_discovery_warns_on_unpublished_route(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "deepinfra.com" in url:
            return {"data": [{"id": "zai-org/GLM-5.2"}]}
        if "fireworks.ai" in url:
            return {"data": [{"id": "accounts/fireworks/models/glm-5p2"}]}
        if "novita.ai" in url:
            return {"data": [{"id": "zai-org/glm-5.2"}]}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-4.7",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-4.7"},
    )

    assert any(
        "deepinfra: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )
    assert any(
        "fireworks: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )
    assert any(
        "novita: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )


def test_provider_glm_discovery_warns_when_live_catalog_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_price_coverage,
        "_GLM_DISCOVERABLE_PROVIDER_APIS",
        (("deepinfra", "https://api.deepinfra.com/v1/openai/models", ()),),
    )

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=lambda _url, _env_names: {"data": []},
        published_model_ids={"z-ai/glm-5.2"} | _NEW_AUTOMATIC_FEED_ROWS,
    )

    assert "deepinfra: GLM model discovery returned no model ids" in warnings


def test_provider_glm_discovery_accepts_nonempty_catalog_without_glm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_price_coverage,
        "_GLM_DISCOVERABLE_PROVIDER_APIS",
        (("together", "https://api.together.xyz/v1/endpoints?type=serverless", ()),),
    )

    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=lambda _url, _env_names: {
            "data": [
                {
                    "model": "openai/gpt-oss-120b",
                    "type": "serverless",
                    "state": "STARTED",
                }
            ]
        },
        published_model_ids={"z-ai/glm-5.2"} | _NEW_AUTOMATIC_FEED_ROWS,
    )

    assert not any("together: GLM model discovery" in warning for warning in warnings)
    assert "together: live model catalog currently lists no GLM routes ✓" in info


def test_provider_glm_discovery_warns_when_all_catalog_routes_are_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_price_coverage,
        "_GLM_DISCOVERABLE_PROVIDER_APIS",
        (("together", "https://api.together.xyz/v1/endpoints?type=serverless", ()),),
    )

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=lambda _url, _env_names: {
            "data": [
                {
                    "model": "zai-org/GLM-5.2-FP8-Lora",
                    "type": "serverless",
                    "state": "STOPPED",
                }
            ]
        },
        published_model_ids={"z-ai/glm-5.2"} | _NEW_AUTOMATIC_FEED_ROWS,
    )

    assert "together: GLM model discovery returned no model ids" in warnings


def test_together_glm_discovery_uses_serverless_models_and_collapses_deployment_aliases() -> None:
    together_urls = [
        url
        for slug, url, _env_names in check_price_coverage._GLM_DISCOVERABLE_PROVIDER_APIS
        if slug == "together"
    ]

    assert together_urls == ["https://api.together.xyz/v1/endpoints?type=serverless"]
    assert check_price_coverage._provider_glm_model_ids(
        {
            "data": [
                {
                    "model": "zai-org/GLM-5.2-FP8-Lora",
                    "type": "serverless",
                    "state": "STARTED",
                }
            ]
        }
    ) == {"z-ai/glm-5.2"}


def test_provider_glm_discovery_keeps_legacy_variants_visibility_only(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "novita.ai" in url:
            return {"data": [{"id": "zai-org/glm-4.7-h"}]}
        if "inference.baseten.co" in url or "pass.wafer.ai" in url:
            return {"data": []}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-4.7",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-4.7"},
    )

    assert any(
        "novita: live GLM variant model API lists unpublished model(s) z-ai/glm-4.7-h" in warning
        for warning in warnings
    )
    assert not any("current model API" in warning for warning in warnings)


def test_strict_model_discovery_fails_glm_provider_warnings(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        warning = (
            "deepinfra: live GLM current model API lists unpublished model(s) "
            "z-ai/glm-5.3 — add/update provider_models/deepinfra.json"
        )
        return ([warning], ["openai: live scraper ✓"], [warning])

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    rc = check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])

    assert rc == 1
    assert "z-ai/glm-5.3" in capsys.readouterr().out


def test_strict_model_discovery_fails_only_discovery_warnings(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (
            [
                "cohere: NO price source",
                "zai: Coding Plan docs mention unpublished model(s) z-ai/glm-5.3",
            ],
            ["openai: live scraper ✓"],
            ["zai: Coding Plan docs mention unpublished model(s) z-ai/glm-5.3"],
        )

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    rc = check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])

    assert rc == 1
    assert "z-ai/glm-5.3" in capsys.readouterr().out


def test_strict_model_discovery_allows_visibility_only_price_warnings(
    monkeypatch,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (["cohere: NO price source"], ["openai: live scraper ✓"], [])

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    assert (
        check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])
        == 0
    )


def test_strict_model_discovery_does_not_fail_provider_api_visibility_warning(
    monkeypatch,
) -> None:
    def fake_model_discovery_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (
            ["novita: live model API lists unpublished model(s) test/model"],
            ["zai: model discovery matched catalog (1 docs model(s)) ✓"],
        )

    monkeypatch.setattr(
        check_price_coverage,
        "_model_discovery_audit",
        fake_model_discovery_audit,
    )

    assert (
        check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])
        == 0
    )
