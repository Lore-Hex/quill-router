import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.pricing.transcription import parse_price, parse_token_prices
from trusted_router.provider_manifest_policy import request_billed_transcription_price_is_valid
from trusted_router.transcription import TRANSCRIPTION_MODELS


def test_duration_prices_require_the_right_units() -> None:
    assert parse_price("openai/whisper-1", "Transcription Cost / minute $0.006") == 6000
    assert parse_price("mistralai/voxtral-mini-2602", "voxtral-mini-2602 $0.003 /Min") == 3000
    for model in ("openai/whisper-1", "mistralai/voxtral-mini-2602"):
        with pytest.raises(ValueError):
            parse_price(model, "$0.0003 per million tokens")
        with pytest.raises(ValueError):
            parse_price(model, "Transcription Cost / minute $0.006 Transcription Cost / minute $0.010 voxtral-mini-2602 $0.003 /Min $0.010 /Min")


def test_request_prices_and_manifest_reject_mixed_tariffs() -> None:
    row = {"id": "whisper-large-v3-turbo", "type": "audio", "endpoints": ["/v1/audio/transcriptions"], "pricing": {"requestPrice": 0.01, "inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0}}
    assert parse_price("openai/whisper-large-v3-turbo", json.dumps([row])) == 10000
    row["pricing"]["inputTokenPricePer1M"] = 1
    with pytest.raises(ValueError):
        parse_price("openai/whisper-large-v3-turbo", json.dumps([row]))
    manifest = {"model_type": "audio", "endpoints": ["audio/transcriptions"], "billing_unit": "request", "fixed_request_price_microdollars": 10000}
    assert request_billed_transcription_price_is_valid(manifest)
    manifest["input_token_price_per_m"] = 0
    assert not request_billed_transcription_price_is_valid(manifest)


def test_audio_token_tariff_is_exact_and_requires_compatible_endpoint() -> None:
    row = {"id": "voxtral-small-24b", "type": "audio", "endpoints": ["/v1/chat/completions"], "pricing": {"requestPrice": 0, "inputTokenPricePer1M": 0.2, "outputTokenPricePer1M": 0.6}}
    assert parse_token_prices(json.dumps([row])) == {"input": 200000, "output": 600000}
    for changes in ({"requestPrice": 0.01}, {"inputTokenPricePer1M": True}, {"inputTokenPricePer1M": -1}, {"outputTokenPricePer1M": 0}, {"outputTokenPricePer1M": "0.6"}):
        with pytest.raises(ValueError):
            parse_token_prices(json.dumps([{**row, "pricing": {**row["pricing"], **changes}}]))
    for rows in ([], [row, row], [{**row, "endpoints": ["/v1/audio/transcriptions"]}]):
        with pytest.raises(ValueError):
            parse_token_prices(json.dumps(rows))


@pytest.mark.parametrize("unit,prices", [
    ("audio_minute", {"audio_minute_price_microdollars": 4708}),
    ("tokens", {"input_token_price_per_m": 200000, "output_token_price_per_m": 600000}),
])
def test_transcription_manifest_billing_units_cannot_mix(unit: str, prices: dict[str, int]) -> None:
    row = {"model_type": "audio", "endpoints": ["audio/transcriptions"], "billing_unit": unit, **prices}
    assert request_billed_transcription_price_is_valid(row)
    assert not request_billed_transcription_price_is_valid({**row, "fixed_request_price_microdollars": 1})
    assert not request_billed_transcription_price_is_valid({**row, "endpoints": ["chat/completions"]})


def test_token_audio_snapshot_manifest_and_billing_prices_agree() -> None:
    from trusted_router import catalog

    model_id = "mistralai/voxtral-small-24b"
    spec = TRANSCRIPTION_MODELS[model_id]
    manifest = json.loads((Path(__file__).parents[1] / "src/trusted_router/data/provider_models/tinfoil.json").read_text())
    row = next(row for row in manifest["models"] if row["id"] == model_id)
    assert row["input_token_price_per_m"] == spec.input_token_cost
    assert row["output_token_price_per_m"] == spec.output_token_cost
    for endpoint in catalog.endpoints_for_model(model_id):
        assert Decimal(spec.pricing["prompt"]) * 10**12 == endpoint.prompt_price_microdollars_per_million_tokens
        assert Decimal(spec.pricing["completion"]) * 10**12 == endpoint.completion_price_microdollars_per_million_tokens
