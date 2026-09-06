"""OpenAI launch hints cannot invalidate independently verified routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing import base
from scripts.pricing.parsers import openai as parser
from scripts.pricing.providers import openai
from trusted_router import catalog_ingest
from trusted_router.catalog_capabilities import manifest_supported_parameters

# Standard row fetched from https://developers.openai.com/api/docs/pricing
# on 2026-09-06. No Astra Pro row was published there at capture time.
ASTRA_PRICING = (
    "| gpt-6-astra | $10.00 | $1.00 | $12.50 | $50.00 | "
    "$20.00 | $2.00 | $25.00 | $75.00 |"
)


def test_astra_pro_id_is_already_recognized_without_inventing_a_price() -> None:
    assert parser._canonical_id("gpt-6-astra-pro") == "openai/gpt-6-astra-pro"
    assert "openai/gpt-6-astra-pro" not in parser.parse(ASTRA_PRICING)


@pytest.mark.parametrize("native_id", ["gpt-6-astra-pro", "gpt-99-novel"])
@pytest.mark.parametrize("available", [False, True])
def test_unknown_openai_model_does_not_strip_other_models_supported_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_id: str,
    available: bool,
) -> None:
    manifest = tmp_path / "openai.json"
    existing = {
        "id": "openai/gpt-4.1",
        "upstream_id": "gpt-4.1",
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_parameters": ["tools", "seed"],
    }
    manifest.write_text(json.dumps({"models": [existing]}), encoding="utf-8")
    monkeypatch.setattr(openai, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(openai, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(openai, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: ASTRA_PRICING)
    model_id = f"openai/{native_id}"
    monkeypatch.setattr(base, "_RUNTIME_REQUIRED_MODELS", {"openai": frozenset({model_id})})
    rows = [{"id": "gpt-6-astra"}, {"id": "gpt-4.1"}]
    if available:
        rows.append({"id": native_id})
    monkeypatch.setattr(openai, "fetch_json", lambda *_args, **_kwargs: {"data": rows})

    def unexpected_self_heal(**_kwargs: object) -> str:
        pytest.fail("An unpriced launch hint must not trigger provider-wide self-heal")

    monkeypatch.setattr(base, "self_heal_parser", unexpected_self_heal)
    probes: list[str] = []

    def probe(**kwargs: object) -> bool:
        probes.append(str(kwargs["model"]))
        return True

    monkeypatch.setattr(openai, "probe_openai_chat", probe)
    result = openai.fetch()
    openai.write_provider_manifest(result)

    assert result.heal_diff is None
    assert model_id not in result.prices
    assert any(model_id in note and "no official price" in note for note in result.notes)
    assert probes == ["gpt-6-astra"]
    written = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert manifest_supported_parameters(written[existing["id"]]) == (
        manifest_supported_parameters(existing)
    )
    assert written[existing["id"]]["input_modalities"] == ["text", "image"]
    assert model_id not in written

    # Exercise the real manifest-to-catalog capability translation as well.
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    astra = models["openai/gpt-6-astra"]
    assert astra.context_length == 1_050_000
    assert set(astra.input_modalities) == {"text", "image"}
    assert astra.output_modalities == ("text",)
    assert set(astra.supported_parameters) == {
        "tools", "max_tokens", "reasoning", "reasoning_effort",
        "include_reasoning", "structured_outputs",
    }
    assert endpoints["openai/gpt-6-astra@openai/prepaid"].upstream_id == "gpt-6-astra"
    assert model_id not in models


def test_openai_isolation_does_not_accept_invalid_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        base, "fetch_html", lambda *_args, **_kwargs: "| gpt-6-astra | $99999 | - | $50 |"
    )
    monkeypatch.setattr(
        base, "_RUNTIME_REQUIRED_MODELS", {"openai": frozenset({"openai/gpt-99-novel"})}
    )

    def fail_self_heal(**_kwargs: object) -> str:
        raise RuntimeError("invalid price still requires repair")

    monkeypatch.setattr(base, "self_heal_parser", fail_self_heal)
    with pytest.raises(RuntimeError, match="invalid price still requires repair"):
        openai.fetch()
