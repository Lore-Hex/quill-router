from __future__ import annotations

import json

from scripts.pricing.providers import parasail


def _row(display: str, *nums: str) -> str:
    cells = "".join(
        f'<div data-x><span class="num" data-x>${n}</span></div>' for n in nums
    )
    return (
        f'<div class="ptbl-row" data-x> <div class="mdl" data-x>'
        f'<span class="ep" data-x>{display}</span></div> {cells} </div>'
    )


def _page(*rows: str, batch_rows: str = "") -> str:
    return (
        "<html><h2>Per-token model pricing</h2>"
        + "".join(rows)
        + "<h2>Reserved GPU pricing</h2>"
        + "<h2>Self-service batch pricing</h2>"
        + batch_rows
        + "</html>"
    )


class FakeResponse:
    def __init__(self, *, payload: dict | None = None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload or {}


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, headers: dict | None = None) -> FakeResponse:
        return self._responses[url]


def _fake_clients(monkeypatch, page_html: str, models_payload: dict) -> None:  # noqa: ANN001
    responses = {
        parasail.PRICING_URL: FakeResponse(text=page_html),
        parasail.URL: FakeResponse(payload=models_payload),
    }
    monkeypatch.setattr(parasail, "_http_client", lambda: FakeClient(responses))


def test_parse_pricing_page_reads_rows_and_skips_variants() -> None:
    html = _page(
        _row("Kimi K2.7 Code", "0.75", "3.50", "0.16"),
        _row("gpt-oss-120b", "0.10", "0.75", "0.055"),
        _row("gpt-oss-120b (Fast)", "0.15", "0.60"),
        _row("Resemble TTS (English)", "18.50"),
        _row("BGE-M3", "0.01"),
        # batch tables reuse the row markup and must not be parsed
        batch_rows=_row("0 – 4.1B params", "0.02", "0.04", "0.01"),
    )
    rows, notes = parasail._parse_pricing_page(html)
    assert rows == {
        "Kimi K2.7 Code": (0.75, 3.50, 0.16),
        "gpt-oss-120b": (0.10, 0.75, 0.055),
    }
    assert notes == []


def test_fetch_prices_only_models_on_both_page_and_api(monkeypatch) -> None:  # noqa: ANN001
    html = _page(
        _row("Kimi K2.7 Code", "0.75", "3.50", "0.16"),
        _row("MiniMax M3", "0.30", "1.20", "0.06"),
        # page-priced but not on /v1/models: must be skipped with a note
        _row("Nemotron 3 Ultra 550B (NVFP4)", "0.50", "2.50", "0.10"),
        # page row with no mapping: must land in notes, never crash
        _row("Brand New Model 9000", "1.00", "2.00", "0.50"),
    )
    models_payload = {
        "data": [
            {"id": "moonshotai/Kimi-K2.7-Code"},
            {"id": "parasail-kimi-k27-code"},
            {"id": "MiniMaxAI/MiniMax-M3"},
            {"id": "MiniMaxAI/Minimax-M3"},
            # API-only model with a known mapping: unpriced note
            {"id": "zai-org/GLM-5.2"},
        ]
    }
    _fake_clients(monkeypatch, html, models_payload)

    result = parasail.fetch()

    assert set(result.prices) == {"moonshotai/kimi-k2.7-code", "minimax/minimax-m3"}
    kimi = result.prices["moonshotai/kimi-k2.7-code"].tiers[0]
    assert kimi.prompt_micro_per_m == 750_000
    assert kimi.completion_micro_per_m == 3_500_000
    assert kimi.prompt_cached_micro_per_m == 160_000

    joined = "\n".join(result.notes)
    assert "nvidia/nvidia-nemotron-3-ultra-550b-a55b" in joined  # page-only
    assert "Brand New Model 9000" in joined  # unmapped page row
    assert "z-ai/glm-5.2" in joined  # api-only, page missing


def test_fetch_case_variant_native_ids_map_to_one_model(monkeypatch) -> None:  # noqa: ANN001
    html = _page(_row("MiniMax M3", "0.30", "1.20", "0.06"))
    models_payload = {
        "data": [
            {"id": "MiniMaxAI/MiniMax-M3"},
            {"id": "MiniMaxAI/Minimax-M3"},
            {"id": "parasail-minimax-m3"},
        ]
    }
    _fake_clients(monkeypatch, html, models_payload)
    result = parasail.fetch()
    assert list(result.prices) == ["minimax/minimax-m3"]


def test_write_provider_manifest_appends_new_models(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    manifest = {
        "_about": "old",
        "provider": "parasail",
        "source": "old",
        "generated_at": "2026-06-22T00:00:00Z",
        "model_count": 1,
        "models": [
            {
                "id": "z-ai/glm-5.2",
                "upstream_id": "parasail-glm-52",
                "input_token_price_per_m": 1,
                "output_token_price_per_m": 2,
            }
        ],
    }
    path = tmp_path / "parasail.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(parasail, "MANIFEST_PATH", path)

    html = _page(
        _row("GLM-5.2", "1.40", "4.40", "0.26"),
        _row("Qwen3.5 397B-A17B", "0.50", "3.60", "0.30"),
        _row("Kimi K2.7 Code", "0.75", "3.50", "0.16"),
        _row("MiniMax M3", "0.30", "1.20", "0.06"),
    )
    models_payload = {
        "data": [
            {"id": "zai-org/GLM-5.2"},
            {"id": "Qwen/Qwen3.5-397B-A17B"},
            {"id": "moonshotai/Kimi-K2.7-Code"},
            {"id": "MiniMaxAI/MiniMax-M3"},
        ]
    }
    _fake_clients(monkeypatch, html, models_payload)
    result = parasail.fetch()

    notes = parasail.write_provider_manifest(result)
    saved = json.loads(path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in saved["models"]}

    # existing row updated in place
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 1_400_000
    # new ahead-of-snapshot rows appended from templates with prices
    assert by_id["moonshotai/kimi-k2.7-code"]["input_token_price_per_m"] == 750_000
    assert by_id["moonshotai/kimi-k2.7-code"]["upstream_id"] == "parasail-kimi-k27-code"
    assert by_id["minimax/minimax-m3"]["context_length"] == 1_048_576
    assert saved["model_count"] == len(saved["models"])
    assert any("appended" in n for n in notes)


def test_parse_pricing_page_raises_on_layout_change() -> None:
    try:
        parasail._parse_pricing_page("<html>totally different page</html>")
    except ValueError as exc:
        assert "section marker" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on missing section")
