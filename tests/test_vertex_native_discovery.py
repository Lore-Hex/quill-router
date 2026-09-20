from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.pricing import refresh, vertex_prices
from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import google_vertex as vertex

MODEL = "google/gemini-3.8-flash"


def _table(rows: list[list[str]], *, tier: str = "", region: bool = True) -> str:
    headers = (
        ["Model", "Type"]
        + (["Region"] if region else [])
        + [
            f"Price (/1M tokens) <= 200K input tokens{tier}",
            f"Price (/1M tokens) > 200K input tokens{tier}",
            f"Price (/1M tokens) <= 200K cached input tokens{tier}",
            f"Price (/1M tokens) > 200K cached input tokens{tier}",
        ]
    )
    return (
        "<table>"
        + "".join(
            "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in [headers, *rows]
        )
        + "</table>"
    )


def _rates(
    label: str,
    *,
    inp: str = "$0.75",
    out: str = "$3.75",
    cached: str = "$0.075",
    region: str = "Global",
) -> list[list[str]]:
    return [
        [label, "Input (text, image, video, audio)", region, inp, inp, cached, cached],
        ["", "Text output (response and reasoning)", region, out, out, "N/A", "N/A"],
    ]


@pytest.mark.parametrize(
    "day,expected", [(date(2026, 12, 31), 750_000), (date(2027, 1, 1), 1_500_000)]
)
def test_vertex_current_dates_global_standard_only(day: date, expected: int) -> None:
    current = _rates("Gemini 3.8 Flash* through December 31, 2026")
    future = _rates(
        "Gemini 3.8 Flash Starting January 1, 2027", inp="$1.50", out="$7.50", cached="$0.15"
    )
    nonglobal = _rates("Gemini 3.8 Flash", inp="$8", out="$9", cached="$1", region="Non-global")
    html = _table(current + future + nonglobal)
    html += _table(_rates("Gemini 3.8 Flash", inp="$9"), tier=" with Priority")
    html += _table(_rates("Gemini 3.8 Flash", inp="$0.10"), tier=" with Flex/Batch")
    price = vertex_prices.parse(html, as_of=day)[MODEL]
    assert price.prompt_micro_per_m == expected
    assert price.completion_micro_per_m == expected * 5
    assert price.tiers[0].prompt_cached_micro_per_m == expected // 10


def test_vertex_tiers_cached_reads_and_audio_not_blended() -> None:
    rows = [
        [
            "Gemini 2.5 Pro",
            "Input (text, image, video, audio)",
            "$1.25",
            "$2.50",
            "$0.125",
            "$0.25",
        ],
        ["", "Input (audio)", "$9", "$10", "$1", "$2"],
        ["", "Text output (response and reasoning)", "$10", "$15", "N/A", "N/A"],
    ]
    price = vertex_prices.parse(_table(rows, region=False))["google/gemini-2.5-pro"]
    assert [
        (
            t.max_prompt_tokens,
            t.prompt_micro_per_m,
            t.completion_micro_per_m,
            t.prompt_cached_micro_per_m,
        )
        for t in price.tiers
    ] == [(200_000, 1_250_000, 10_000_000, 125_000), (None, 2_500_000, 15_000_000, 250_000)]


def test_vertex_conflicting_current_rates_fail_closed() -> None:
    with pytest.raises(ValueError, match="conflicting current"):
        vertex_prices.parse(
            _table(_rates("Gemini 3.8 Flash") + _rates("Gemini 3.8 Flash", inp="$2"))
        )


def test_vertex_partial_sku_never_borrows_next_model_output() -> None:
    html = _table(_rates("Gemini 3 Flash Preview")[:1] + _rates("Gemini 3.8 Flash"))
    assert set(vertex_prices.parse(html)) == {MODEL}


def test_vertex_future_model_names_need_no_release_mapping() -> None:
    assert "google/gemini-4.2-flash-lite" in vertex_prices.parse(
        _table(_rates("Gemini 4.2 Flash-Lite"))
    )


def test_vertex_discovery_paginates_and_rejects_incomplete_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[str] = []

    def fetch(url: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["extra_headers"] == {"Authorization": "Bearer test"}
        assert kwargs["follow_redirects"] is False
        pages.append(url)
        if "pageToken" not in url:
            return {
                "publisherModels": [{"name": "publishers/google/models/gemini-3.8-flash"}],
                "nextPageToken": "next",
            }
        return {"publisherModels": [{"name": "publishers/google/models/gemini-3.7-flash"}]}

    monkeypatch.setattr(vertex, "fetch_json", fetch)
    assert vertex._live_model_ids({"Authorization": "Bearer test"}) == {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
    }
    assert len(pages) == 2
    monkeypatch.setattr(
        vertex, "fetch_json", lambda *a, **kw: {"publisherModels": [], "nextPageToken": "next"}
    )
    with pytest.raises(RuntimeError, match="did not advance"):
        vertex._live_model_ids({})
    monkeypatch.setattr(vertex, "fetch_json", lambda *a, **kw: {"error": {}})
    with pytest.raises(RuntimeError, match="list missing"):
        vertex._live_model_ids({})


@pytest.mark.parametrize(
    "ok,status", [(True, 200), (False, 403), (False, 404), (False, 429), (False, None)]
)
def test_vertex_only_admits_its_own_successful_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: bool, status: int | None
) -> None:
    manifest = tmp_path / "google-vertex.json"
    manifest.write_text(json.dumps({"provider": "google-vertex", "models": []}))
    monkeypatch.setattr(vertex, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(
        vertex, "_authorization", lambda: ({"Authorization": "Bearer test"}, "test-project")
    )
    monkeypatch.setattr(
        vertex,
        "_live_model_ids",
        lambda headers: {"gemini-3.8-flash", "gemini-omni-1.1-flash-preview"},
    )
    monkeypatch.setattr(
        vertex,
        "_metadata",
        lambda: {MODEL: {"context_length": 1_048_576, "max_output_tokens": 65_536}},
    )
    monkeypatch.setattr(vertex, "fetch_html", lambda url: _table(_rates("Gemini 3.8 Flash")))
    monkeypatch.setattr(vertex, "_probe", lambda headers, project, mid: (ok, status))
    result = vertex.fetch()
    vertex.write_provider_manifest(result)
    rows = {r["id"]: r for r in json.loads(manifest.read_text())["models"]}
    assert rows[MODEL]["routable"] is ok
    assert rows[MODEL]["canary_status_code"] == status
    assert rows[MODEL]["cached_input_token_price_per_m"] == 75_000
    assert rows["google/gemini-omni-1.1-flash-preview"]["routable"] is False
    assert (MODEL in refresh._index_provider_prices({"google_vertex": result})) is ok


@pytest.mark.parametrize(
    "mutation", ["thought_only", "empty", "no_usage", "bad_usage", "truncated", "invalid_json"]
)
def test_vertex_canary_rejects_false_positives(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    data: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": "PONG"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6},
    }
    if mutation == "thought_only":
        data["candidates"][0]["content"]["parts"][0]["thought"] = True
    elif mutation == "empty":
        data["candidates"] = []
    elif mutation == "no_usage":
        data.pop("usageMetadata")
    elif mutation == "bad_usage":
        data["usageMetadata"]["promptTokenCount"] = True
    elif mutation == "truncated":
        data["candidates"][0]["finishReason"] = "MAX_TOKENS"
    response = (
        httpx.Response(200, json=data)
        if mutation != "invalid_json"
        else httpx.Response(200, text="invalid")
    )
    monkeypatch.setattr(vertex.httpx, "post", lambda *args, **kwargs: response)
    assert vertex._probe({}, "project", "gemini-3.8-flash")[0] is False


def test_vertex_and_ai_studio_prices_stay_independent() -> None:
    from scripts.pricing.base import ProviderPricingResult

    results = {
        "gemini": ProviderPricingResult(
            slug="gemini", prices={MODEL: ModelPrice(10, 20)}, source="api", fetched_url="studio"
        ),
        "google_vertex": ProviderPricingResult(
            slug="google-vertex",
            prices={MODEL: ModelPrice(30, 40)},
            source="api",
            fetched_url="vertex",
        ),
    }
    indexed = refresh._index_provider_prices(results)[MODEL]
    assert indexed["google-ai-studio"].prompt_micro_per_m == 10
    assert indexed["google-vertex"].prompt_micro_per_m == 30


@pytest.mark.parametrize("hold", sorted(vertex._RECOVERABLE_HOLDS) + [None, "operator-hold"])
def test_vertex_retry_machine_holds_but_never_clear_operator_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: str | None
) -> None:
    manifest = tmp_path / "google-vertex.json"
    existing: dict[str, Any] = {
        "id": MODEL,
        "context_length": 1_048_576,
        "max_output_tokens": 65_536,
    }
    if hold:
        existing.update(routable=False, routable_reason=hold)
    manifest.write_text(json.dumps({"provider": "google-vertex", "models": [existing]}))
    monkeypatch.setattr(vertex, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(vertex, "_authorization", lambda: ({}, "project"))
    monkeypatch.setattr(vertex, "_live_model_ids", lambda headers: {"gemini-3.8-flash"})
    monkeypatch.setattr(vertex, "fetch_html", lambda url: _table(_rates("Gemini 3.8 Flash")))
    probes: list[str] = []

    def probe(headers: dict[str, str], project: str, model: str) -> tuple[bool, int]:
        probes.append(model)
        return True, 200

    monkeypatch.setattr(vertex, "_probe", probe)
    result = vertex.fetch()
    vertex.write_provider_manifest(result)
    row = json.loads(manifest.read_text())["models"][0]
    assert probes == (["gemini-3.8-flash"] if hold in vertex._RECOVERABLE_HOLDS else [])
    if hold == "operator-hold":
        assert row["routable"] is False
        assert row["routable_reason"] == hold
        assert not result.price_index_model_ids
    else:
        assert row.get("routable") is not False
        assert "routable_reason" not in row
        assert result.price_index_model_ids == frozenset({MODEL})


def test_vertex_current_chat_manifest_routes_are_published() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS

    manifest = vertex._manifest_rows(vertex.MANIFEST_PATH)
    for model_id, row in manifest.items():
        endpoint = MODEL_ENDPOINTS.get(f"{model_id}@google-vertex/prepaid")
        if row.get("routable") is False:
            assert endpoint is None, model_id
        else:
            assert endpoint is not None, model_id
            assert endpoint.upstream_id == row["upstream_id"]
            assert endpoint.prompt_price_microdollars_per_million_tokens > 0
            assert endpoint.completion_price_microdollars_per_million_tokens > 0
    assert f"{MODEL}@google-vertex/prepaid" in MODEL_ENDPOINTS
    assert "google/gemini-3.8-flash-cyber@google-vertex/prepaid" not in MODEL_ENDPOINTS
