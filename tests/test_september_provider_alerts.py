import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import _direct_openai, inceptron
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.provider_contracts import provider_model_operator_held


@pytest.mark.parametrize(("provider", "model"), [
    ("morph", "qwen/qwen3.6-27b"),
    ("morph", "qwen/qwen3.5-397b-a17b"),
    ("morph", "minimax/minimax-m2.7"),
    ("morph", "minimax/minimax-m3"),
    ("nextbit", "gryphe/mythomax-l2-13b"),
    ("inceptron", "minimax/minimax-m2.5"),
    ("phala", "z-ai/glm-5.3"),
    ("phala", "z-ai/glm-5.3-flash"),
])
def test_confirmed_unavailable_routes_not_advertised(provider: str, model: str) -> None:
    assert not any(
        endpoint.provider == provider and endpoint.model_id == model
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert any(
        endpoint.provider != provider and endpoint.model_id == model
        for endpoint in MODEL_ENDPOINTS.values()
    ), "An unavailable reseller must not retire the model on other providers"


def test_morph_shared_edit_models_remain_available() -> None:
    for model in ("morph/morph-v3-fast", "morph/morph-v3-large"):
        assert any(e.provider == "morph" and e.model_id == model for e in MODEL_ENDPOINTS.values())


def test_phala_hold_does_not_change_reviewed_confidential_routes() -> None:
    assert not provider_model_operator_held("phala", "openai/gpt-oss-120b")
    assert not provider_model_operator_held("phala", "moonshotai/kimi-k2.6")


def test_inceptron_canaries_only_discovered_routes_and_isolates_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    manifest = tmp_path / "inceptron.json"
    rows = [{"id": "zai-org/GLM-5.3"}, {"id": "moonshotai/Kimi-K2.6"}]
    monkeypatch.setenv("INCEPTRON_API_KEY", "fake")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **k: {"data": rows})
    probed: list[str] = []

    def probe(**kwargs: object) -> bool:
        model = str(kwargs["model"])
        probed.append(model)
        return model == "zai-org/GLM-5.3"

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    spec = _direct_openai.DirectOpenAIProviderSpec(
        slug=inceptron.SLUG, base_url=inceptron.CATALOG.spec.base_url,
        api_key_env="INCEPTRON_API_KEY", explicit_model_map=inceptron._NATIVE_TO_MODEL_ID,
        static_prices={"z-ai/glm-5.3": ModelPrice(1, 2), "moonshotai/kimi-k2.6": ModelPrice(3, 4)},
    )
    catalog = _direct_openai.DirectOpenAIProvider(spec, manifest_path=manifest)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)
    published = {r["id"]: r for r in json.loads(manifest.read_text())["models"]}
    assert set(probed) == {row["id"] for row in rows}
    assert published["z-ai/glm-5.3"]["routable"] is True
    assert published["moonshotai/kimi-k2.6"]["routable"] is False
    assert published["moonshotai/kimi-k2.6"]["routable_reason"] == "provider-canary-failed"
