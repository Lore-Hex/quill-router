import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import _direct_openai, nextbit
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.provider_contracts import provider_model_operator_held

MODEL = "thedrummer/unslopnemo-12b-v4.1"
NATIVE_ID = "unslopnemo:12b"


@pytest.mark.parametrize("model", [MODEL, "undi95/remm-slerp-l2-13b", "sao10k/l3.3-70b-euryale-v2.3"])
def test_unavailable_nextbit_alias_is_not_routable(model: str) -> None:
    assert not any(
        endpoint.provider == "nextbit" and endpoint.model_id == model
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert provider_model_operator_held("nextbit", model)
    # The hold is this route's alone: the model elsewhere, and nextbit's other
    # models, are not held.
    assert not provider_model_operator_held("novita", model)
    assert not provider_model_operator_held("nextbit", "qwen/qwen3-14b")


def test_nextbit_manifest_records_alias_hold() -> None:
    rows = json.loads(nextbit.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    row = next(row for row in rows if row["id"] == MODEL)
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-alias-unavailable"


@pytest.mark.parametrize("old_state", ["routable", "failed-canary", "delisted"])
@pytest.mark.parametrize("model,native", [(MODEL, NATIVE_ID),
    ("undi95/remm-slerp-l2-13b", "remm-slerp:l2-13b"),
    ("sao10k/l3.3-70b-euryale-v2.3", "euryale:33-70b")])
def test_nextbit_refresh_cannot_clear_alias_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old_state: str, model: str, native: str,
) -> None:
    manifest = tmp_path / "nextbit.json"
    old_row: dict[str, object] = {
        "id": model, "upstream_id": native, "routable": old_state == "routable",
        "input_token_price_per_m": 400_000, "output_token_price_per_m": 400_000,
    }
    if old_state != "routable":
        old_row["routable_reason"] = (
            "delisted-upstream" if old_state == "delisted" else "provider-canary-failed"
        )
    manifest.write_text(json.dumps({"provider": "nextbit", "models": [old_row]}))
    monkeypatch.setenv("NEXTBIT_API_KEY", "fake")
    monkeypatch.setattr(
        _direct_openai, "fetch_json",
        lambda *a, **k: {"data": [{"id": native}, {"id": "qwen3:14b"}]},
    )
    probed: list[str] = []

    def probe(**kwargs: object) -> bool:
        probed.append(str(kwargs["model"]))
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    spec = replace(
        nextbit.CATALOG.spec,
        static_prices={model: ModelPrice(400_000, 400_000), "qwen/qwen3-14b": ModelPrice(1, 2)},
        expected_models=(),
    )
    catalog = _direct_openai.DirectOpenAIProvider(spec, manifest_path=manifest)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)
    published = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert probed == ["qwen3:14b"]
    assert published[model]["upstream_id"] == native
    assert published[model]["routable"] is False
    assert published[model]["routable_reason"] == "provider-alias-unavailable"
    if old_state == "routable":
        # Holding the only baseline route trips the bulk-removal safeguard:
        # apply the hold, but discard unrelated discovery changes.
        assert set(published) == {model}
    else:
        assert published["qwen/qwen3-14b"]["routable"] is True
