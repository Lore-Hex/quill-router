import json
from decimal import Decimal

from scripts.pricing.providers import privatemode, tinfoil


def test_tinfoil_discovers_reviewed_safety_embedding_and_chat(monkeypatch, tmp_path):
    path = tmp_path / "tinfoil.json"
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", path)
    rows = [
        {"id": native, "type": kind, "endpoints": endpoints,
         "context_window": 8192,
         "pricing": {"inputTokenPricePer1M": 0.05,
                     "outputTokenPricePer1M": output, "requestPrice": 0}}
        for native, kind, endpoints, output in (
            ("gpt-oss-safeguard-120b", "safety", ["/v1/chat/completions", "/v1/responses"], 0.6),
            ("nomic-embed-text", "embedding", ["/v1/embeddings"], 0),
            ("gpt-oss-120b", "chat", ["/v1/chat/completions"], 0.6),
            ("llama3-3-70b", "chat", ["/v1/chat/completions"], 2.75),
        )
    ]
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": rows})
    result = tinfoil.fetch()
    tinfoil.write_provider_manifest(result)
    saved = {row["upstream_id"]: row for row in json.loads(path.read_text())["models"]}
    assert set(saved) == {row["id"] for row in rows}
    assert saved["gpt-oss-safeguard-120b"]["model_type"] == "chat"
    assert saved["nomic-embed-text"]["model_type"] == "embedding"
    assert saved["nomic-embed-text"]["endpoints"] == ["embeddings"]
    assert saved["nomic-embed-text"]["output_modalities"] == ["embeddings"]
    assert saved["nomic-embed-text"]["output_token_price_per_m"] == 0


def test_tinfoil_never_publishes_request_fees_as_zero_token_prices(monkeypatch, tmp_path):
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", tmp_path / "tinfoil.json")
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [
        {"id": "gpt-oss-120b", "type": "chat", "endpoints": ["/v1/chat/completions"],
         "pricing": {"inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0, "requestPrice": 0.01}},
        {"id": "whisper-large-v3-turbo", "type": "audio", "endpoints": ["/v1/audio/transcriptions"],
         "pricing": {"inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0, "requestPrice": 0.01}},
    ]})
    result = tinfoil.fetch()
    assert not result.prices
    rows = tinfoil._DISCOVERED_MANIFEST_ROWS
    assert rows["openai/gpt-oss-120b"]["routable"] is False
    transcription = rows["openai/whisper-large-v3-turbo"]
    assert transcription["routable"] is True
    assert transcription["billing_unit"] == "request"
    assert transcription["fixed_request_price_microdollars"] == 10000
    assert "input_token_price_per_m" not in transcription


def test_privatemode_prices_ocr_and_input_only_embedding(monkeypatch, httpx_mock, tmp_path):
    html = """<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
    <tr><td>DeepSeek OCR 2</td><td>EUR 0.77</td><td>EUR 1.27</td><td>EUR 0.08</td></tr></table>
    <table><tr><th>Model</th><th>Input</th></tr>
    <tr><td>Qwen3-Embedding 4B</td><td>EUR 0.13</td></tr></table>"""
    prices = privatemode.parse_prices(html, Decimal("1.20"))
    assert prices["deepseek/deepseek-ocr-2"].prompt_micro_per_m == 970200
    assert prices["qwen/qwen3-embedding-4b"].prompt_micro_per_m == 163800
    assert prices["qwen/qwen3-embedding-4b"].completion_micro_per_m == 0
    monkeypatch.setenv("PRIVATEMODE_API_KEY", "synthetic-key")
    httpx_mock.add_response(url=privatemode.CATALOG_URL, json={"data": [
        {"id": "deepseek-ocr-2"}, {"id": "qwen3-embedding-4b"},
    ]})
    monkeypatch.setattr(privatemode, "fetch_html", lambda url: html if url == privatemode.PRICING_URL
                        else "<Cube currency='USD' rate='1.2'/>")
    monkeypatch.setattr(privatemode, "MANIFEST_PATH", tmp_path / "privatemode.json")
    result = privatemode.fetch()
    privatemode.write_provider_manifest(result)
    saved = {row["id"]: row for row in json.loads(privatemode.MANIFEST_PATH.read_text())["models"]}
    ocr = saved["deepseek/deepseek-ocr-2"]
    assert ocr["supports_tools"] is False
    assert ocr["supports_reasoning"] is False
    assert ocr["input_modalities"] == ["text", "image"]
    embedding = saved["qwen/qwen3-embedding-4b"]
    assert embedding["model_type"] == "embedding"
    assert embedding["endpoints"] == ["embeddings"]
    # Discovery must not enable a workload before the matching enclave rollout.
    for row in (ocr, embedding):
        assert row["routable"] is False
        assert row["routable_reason"] == "attested-enclave-rollout-pending"
