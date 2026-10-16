import json

import pytest

from scripts.pricing.speech import parse_tinfoil_request_prices
from trusted_router.pricing import customer_fixed_price_microdollars
from trusted_router.speech import SPEECH_MODELS


@pytest.mark.parametrize("model,cost", [("qwen/qwen3-tts", 10000), ("mistralai/voxtral-4b-tts-2603", 2000)])
def test_request_quote_is_independent_of_length(model, cost):
    spec = SPEECH_MODELS[model]
    assert spec.billing_unit == "request"
    for count in (1, 100, 4096):
        assert spec.quote(count) == customer_fixed_price_microdollars(cost)
    for count in (0, 4097, -1, True, 1.5):
        with pytest.raises(ValueError):
            spec.quote(count)


def tariff_rows():
    return [{"id": name, "type": "tts", "endpoints": ["/v1/audio/speech"],
             "pricing": {"inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0, "requestPrice": price}}
            for name, price in (("qwen3-tts", 0.01), ("voxtral-tts", 0.002))]


def test_request_price_parser():
    assert parse_tinfoil_request_prices(json.dumps({"data": tariff_rows()})) == {
        "qwen/qwen3-tts": 10000, "mistralai/voxtral-4b-tts-2603": 2000,
    }


@pytest.mark.parametrize("change", ["missing", "duplicate", "endpoint", "type", "tokens", "negative", "zero", "bool", "precision", "nan"])
def test_request_price_parser_fails_closed(change):
    rows = tariff_rows()
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(rows[0])
    elif change == "endpoint":
        rows[0]["endpoints"] = ["/v1/chat/completions"]
    elif change == "type":
        rows[0]["type"] = "chat"
    elif change == "tokens":
        rows[0]["pricing"]["inputTokenPricePer1M"] = 1
    else:
        rows[0]["pricing"]["requestPrice"] = {"negative": -1, "zero": 0, "bool": True, "precision": 0.0000001, "nan": float("nan")}[change]
    with pytest.raises(ValueError):
        parse_tinfoil_request_prices(json.dumps({"data": rows}))


def test_speech_discovery_does_not_publish_free_token_routes(monkeypatch, tmp_path):
    from scripts.pricing.providers import tinfoil
    path = tmp_path / "tinfoil.json"
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": tariff_rows()})
    result = tinfoil.fetch()
    assert not result.prices
    tinfoil.write_provider_manifest(result)
    rows = json.loads(path.read_text())["models"]
    assert len(rows) == 2
    for row in rows:
        assert row["routable"] is True
        assert row["billing_unit"] == "request"
        assert row["fixed_request_price_microdollars"] > 0
        assert row["id"] in SPEECH_MODELS
        assert "input_token_price_per_m" not in row
        assert row["endpoints"] == ["audio/speech"]


@pytest.mark.parametrize("change", ["type", "endpoint", "unit", "zero", "bool", "tokens"])
def test_request_manifest_contract_cannot_enable_unpriced_chat(change):
    from trusted_router.provider_manifest_policy import request_billed_speech_price_is_valid
    row = {"model_type": "tts", "endpoints": ["audio/speech"], "billing_unit": "request", "fixed_request_price_microdollars": 10000}
    assert request_billed_speech_price_is_valid(row)
    field, value = {"type": ("model_type", "chat"), "endpoint": ("endpoints", ["chat/completions"]),
                    "unit": ("billing_unit", "tokens"), "zero": ("fixed_request_price_microdollars", 0),
                    "bool": ("fixed_request_price_microdollars", True), "tokens": ("input_token_price_per_m", 0)}[change]
    row[field] = value
    assert not request_billed_speech_price_is_valid(row)


@pytest.mark.parametrize("model", ["qwen/qwen3-tts", "mistralai/voxtral-4b-tts-2603"])
def test_public_speech_price_is_per_request(client, model):
    result = client.get("/v1/models?output_modalities=speech")
    row = next(row for row in result.json()["data"] if row["id"] == model)
    assert set(row["pricing"]) == {"request"}
    assert float(row["pricing"]["request"]) > 0
    assert row["trustedrouter"]["speech"]["billing_unit"] == "request"
    assert row["trustedrouter"]["speech"]["default_response_format"] == "wav"
    assert row["trustedrouter"]["capabilities"]["confidential"] is True
    page = client.get("/models/" + model)
    assert page.status_code == 200
    assert "/request" in page.text
    assert "/M characters" not in page.text
    assert "Prices are per one million tokens" not in page.text
    pricing = client.get("/models/" + model + "/pricing")
    assert pricing.status_code == 200
    assert "USD per million tokens" not in pricing.text
    api = client.get("/models/" + model + "/api")
    assert api.status_code == 200
    assert "client.audio.speech.with_streaming_response.create" in api.text
    assert 'response_format="wav"' in api.text
    assert "client.chat.completions.create" not in api.text
