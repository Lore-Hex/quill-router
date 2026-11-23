import pytest

from scripts.pricing.providers import nito


def row(tier="confidential"):
    return {
        "id": f"org/model:{tier}",
        "type": "chat",
        "privacy_tier": tier,
        "supported_endpoints": ["/v1/chat/completions"],
        "pricing": {
            "currency_code": "USD",
            "unit": "nanos",
            "input_token_nanos": 360,
            "output_token_nanos": 1440,
            "cache_read_input_token_nanos": 8,
        },
    }


def test_nito_prices_are_held_and_encrypted_siblings_are_not_plaintext_routes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NITO_API_KEY", "test")
    monkeypatch.setattr(nito, "MANIFEST_PATH", tmp_path / "nito.json")

    def catalog(url, *, extra_headers, follow_redirects):
        assert url == "https://app.trynito.ai/api/v1/models" and not follow_redirects
        assert extra_headers == {"Authorization": "Bearer test"}
        return {"data": [row(), row("encrypted")]}

    monkeypatch.setattr(nito, "fetch_json", catalog)
    snapshot = nito.fetch()
    assert not snapshot.include_in_price_index
    price = snapshot.prices["org/model"].tiers[0]
    assert (
        price.prompt_micro_per_m,
        price.completion_micro_per_m,
        price.prompt_cached_micro_per_m,
    ) == (360000, 1440000, 8000)
    assert nito.UPSTREAM_ID_MAP == {"org/model": "org/model:confidential"}
    nito.write_provider_manifest(snapshot)
    import json

    (record,) = json.loads(nito.MANIFEST_PATH.read_text())["models"]
    assert record["routable"] is False and record["routable_reason"] == nito.HOLD


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency_code", "EUR"),
        ("unit", "usd"),
        ("input_token_nanos", True),
        ("output_token_nanos", -1),
        ("cache_read_input_token_nanos", 361),
        ("reasoning_output_token_nanos", 1500),
    ],
)
def test_nito_rejects_ambiguous_price(field, value):
    item = row()
    item["pricing"][field] = value
    with pytest.raises(ValueError):
        nito.discover({"data": [item]})


def test_nito_refuses_duplicate_or_mismatched_tiers():
    for items in ([row(), row()], [dict(row(), id="org/model:encrypted")], [row("encrypted")]):
        with pytest.raises(ValueError):
            nito.discover({"data": items})
