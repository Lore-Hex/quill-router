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


def _table_page(*rows: tuple[str, str, str, str]) -> str:
    body = "".join(
        f"<tr><th scope='row'>{display}</th><td>${prompt}</td>"
        f"<td>${completion}</td><td>${cached}</td></tr>"
        for display, prompt, completion, cached in rows
    )
    return (
        "<html><div id='pricing-panel-serverless'><table><tbody>"
        f"{body}</tbody></table></div>"
        "<div id='pricing-panel-dedicated'><table><tbody>"
        "<tr><th scope='row'>Do Not Parse</th><td>$99</td><td>$99</td>"
        "<td>$99</td></tr></tbody></table></div></html>"
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

    def __enter__(self) -> FakeClient:
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


def test_parse_pricing_page_reads_current_serverless_table_only() -> None:
    html = _table_page(
        ("GLM-5.3 Flash", "0.15", "0.50", "0.03"),
        ("GLM-5.3", "1.40", "4.40", "0.26"),
    )

    rows, notes = parasail._parse_pricing_page(html)

    assert rows == {
        "GLM-5.3 Flash": (0.15, 0.50, 0.03),
        "GLM-5.3": (1.40, 4.40, 0.26),
    }
    assert "Do Not Parse" not in rows
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
            {"id": "zai-org/GLM-5.3-Flash"},
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
    assert "nvidia/nemotron-3-ultra-550b-a55b" in joined  # page-only
    assert "Brand New Model 9000" in joined  # unmapped page row
    assert "z-ai/glm-5.2" in joined  # api-only, page missing
    assert "z-ai/glm-5.3-flash" in joined


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


def test_fetch_skips_mistral_priced_on_page_but_missing_from_live_api(
    monkeypatch,
) -> None:  # noqa: ANN001
    html = _page(_row("Mistral Small 3.2 24B", "0.09", "0.30", "0.05"))
    _fake_clients(monkeypatch, html, {"data": []})

    result = parasail.fetch()

    assert "mistralai/mistral-small-3.2-24b-instruct" not in result.prices
    assert any(
        "mistralai/mistral-small-3.2-24b-instruct" in note
        and "/v1/models doesn't list it" in note
        for note in result.notes
    )


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
        _row("GLM-5.3", "1.40", "4.40", "0.26"),
        _row("GLM-5.2", "1.40", "4.40", "0.26"),
        _row("Qwen3.5 397B-A17B", "0.50", "3.60", "0.30"),
        _row("Kimi K2.7 Code", "0.75", "3.50", "0.16"),
        _row("MiniMax M3", "0.30", "1.20", "0.06"),
    )
    models_payload = {
        "data": [
            {"id": "parasail-glm-53"},
            {"id": "zai-org/GLM-5.3"},
            {"id": "zai-org/GLM-5.2"},
            {"id": "Qwen/Qwen3.5-397B-A17B"},
            {"id": "moonshotai/Kimi-K2.7-Code"},
            {"id": "MiniMaxAI/MiniMax-M3"},
            {"id": "zai-org/GLM-5.3-Flash"},
        ]
    }
    _fake_clients(monkeypatch, html, models_payload)
    result = parasail.fetch()

    notes = parasail.write_provider_manifest(result)
    saved = json.loads(path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in saved["models"]}

    # existing row updated in place
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 1_400_000
    assert by_id["z-ai/glm-5.3"]["input_token_price_per_m"] == 1_400_000
    assert by_id["z-ai/glm-5.3"]["output_token_price_per_m"] == 4_400_000
    assert by_id["z-ai/glm-5.3"]["upstream_id"] == "parasail-glm-53"
    # new ahead-of-snapshot rows appended from templates with prices
    assert by_id["moonshotai/kimi-k2.7-code"]["input_token_price_per_m"] == 750_000
    assert by_id["moonshotai/kimi-k2.7-code"]["upstream_id"] == "parasail-kimi-k27-code"
    assert by_id["minimax/minimax-m3"]["context_length"] == 1_048_576
    # Live discovery without a price remains visible but impossible to route.
    unresolved = by_id["z-ai/glm-5.3-flash"]
    assert unresolved["upstream_id"] == "zai-org/GLM-5.3-Flash"
    assert unresolved["routable"] is False
    assert unresolved["routable_reason"] == "awaiting-price"
    assert "input_token_price_per_m" not in unresolved
    assert saved["model_count"] == len(saved["models"])
    assert any("appended" in n for n in notes)


def test_write_provider_manifest_promotes_glm_53_only_after_official_price(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    manifest = {
        "provider": "parasail",
        "models": [
            {
                **parasail._MANIFEST_ROW_TEMPLATES["z-ai/glm-5.3-flash"],
                "routable": False,
                "routable_reason": "awaiting-price",
                "unresolved_since": "2026-08-27",
            }
        ],
    }
    path = tmp_path / "parasail.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(parasail, "MANIFEST_PATH", path)
    monkeypatch.setattr(parasail, "_MANIFEST_EXPECTED", [])

    html = _page(_row("GLM-5.3 Flash", "0.15", "0.50", "0.03"))
    _fake_clients(
        monkeypatch,
        html,
        {"data": [{"id": "zai-org/GLM-5.3-Flash"}]},
    )

    parasail.write_provider_manifest(parasail.fetch())

    row = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert row["input_token_price_per_m"] == 150_000
    assert row["output_token_price_per_m"] == 500_000
    assert row["cached_input_token_price_per_m"] == 30_000
    assert "routable" not in row
    assert "routable_reason" not in row
    assert "unresolved_since" not in row


def test_fetch_maps_full_glm_53_live_ids_and_prices(monkeypatch) -> None:  # noqa: ANN001
    html = _table_page(("GLM-5.3", "1.40", "4.40", "0.26"))
    _fake_clients(
        monkeypatch,
        html,
        {"data": [{"id": "parasail-glm-53"}, {"id": "zai-org/GLM-5.3"}]},
    )

    result = parasail.fetch()

    price = result.prices["z-ai/glm-5.3"].tiers[0]
    assert price.prompt_micro_per_m == 1_400_000
    assert price.completion_micro_per_m == 4_400_000
    assert price.prompt_cached_micro_per_m == 260_000
    assert parasail.UPSTREAM_ID_MAP["z-ai/glm-5.3"] == "parasail-glm-53"


def test_write_provider_manifest_does_not_invent_unseen_glm_53(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "parasail.json"
    path.write_text(json.dumps({"provider": "parasail", "models": []}), encoding="utf-8")
    monkeypatch.setattr(parasail, "MANIFEST_PATH", path)
    monkeypatch.setattr(parasail, "_MANIFEST_EXPECTED", [])

    html = _page(_row("MiniMax M3", "0.30", "1.20", "0.06"))
    _fake_clients(monkeypatch, html, {"data": [{"id": "MiniMaxAI/MiniMax-M3"}]})

    parasail.write_provider_manifest(parasail.fetch())

    ids = {row["id"] for row in json.loads(path.read_text(encoding="utf-8"))["models"]}
    assert "z-ai/glm-5.3-flash" not in ids


def test_parse_pricing_page_raises_on_layout_change() -> None:
    try:
        parasail._parse_pricing_page("<html>totally different page</html>")
    except ValueError as exc:
        assert "section marker" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on missing section")
