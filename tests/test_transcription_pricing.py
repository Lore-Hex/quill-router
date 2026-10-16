import json

import pytest

from scripts.pricing.transcription import parse_price
from trusted_router.provider_manifest_policy import request_billed_transcription_price_is_valid


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
