from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.pricing import vertex_claude
from scripts.pricing.base import reconcile_manifest_tombstones
from scripts.pricing.providers import google_vertex as vertex

FIXTURE = Path(__file__).parent / "fixtures/pricing/vertex_claude.html"


@pytest.mark.parametrize("reason,priced", [
    ("vertex-metadata-unavailable", True),
    ("vertex-price-context-incomplete", False),
    ("unsupported-vertex-api", True),
])
@pytest.mark.parametrize("old_reason", [None, "awaiting-price", "delisted-upstream"])
def test_recovery_never_overrides_a_fresh_vertex_admission_failure(
    reason: str, priced: bool, old_reason: str | None,
) -> None:
    model = "anthropic/claude-opus-5.5"
    old = [{"id": model, "routable": False, "routable_reason": old_reason}] if old_reason else []
    present = {model: {"id": model, "routable": False, "routable_reason": reason}}
    rows = reconcile_manifest_tombstones(old, present, priced_ids={model} if priced else set(), source="api")
    assert rows[0]["routable"] is False
    assert rows[0]["routable_reason"] == reason


def test_claude_native_ids_default_and_dated_are_order_independent() -> None:
    rows = [
        {"name": "publishers/anthropic/models/claude-haiku-5-5", "versionId": version}
        for version in ("20261001", "default", "20261005")
    ] + [{"name": "publishers/anthropic/models/claude-haiku-4-5", "versionId": "20251001"}]
    expected = {
        "anthropic/claude-haiku-5.5": {
            "id": "anthropic/claude-haiku-5.5", "upstream_id": "claude-haiku-5-5",
        },
        "anthropic/claude-haiku-4.5": {
            "id": "anthropic/claude-haiku-4.5", "upstream_id": "claude-haiku-4-5@20251001",
        },
    }
    assert vertex_claude.native_rows(rows) == expected
    assert vertex_claude.native_rows(list(reversed(rows))) == expected
    assert vertex_claude.native_rows([
        {"name": "publishers/google/models/claude-opus-5-5", "versionId": "default"},
        {"name": "publishers/anthropic/models/claude-opus-5-5", "versionId": "unknown"},
    ]) == {}


def test_claude_vertex_global_rates_and_context_boundaries() -> None:
    prices = vertex_claude.parse(FIXTURE.read_text())
    sonnet = prices["anthropic/claude-sonnet-5.5"].price
    assert (sonnet.prompt_micro_per_m, sonnet.completion_micro_per_m) == (2_000_000, 10_000_000)
    assert sonnet.tiers[0].prompt_cached_micro_per_m == 100_000
    haiku = prices["anthropic/claude-haiku-5.5"].price
    assert [t.max_prompt_tokens for t in haiku.tiers] == [100_000, None]
    assert [t.prompt_micro_per_m for t in haiku.tiers] == [100_000, 500_000]
    assert [t.completion_micro_per_m for t in haiku.tiers] == [500_000, 2_500_000]
    assert [t.prompt_cached_micro_per_m for t in haiku.tiers] == [10_000, 50_000]
    assert prices["anthropic/claude-opus-5.5"].context_limit == 200_000
    assert prices["anthropic/claude-opus-4.5"].context_limit == 200_000
    assert prices["anthropic/claude-opus-4.8"].context_limit is None
    assert prices["anthropic/claude-fable-5.1"].price.tiers[0].prompt_cached_micro_per_m == 250_000


@pytest.mark.parametrize("query,held", [
    ("dataSharing=true", True), ("zdrExemption=912", True),
    ("advanceAISafetyAddendum=true", True),
    ("optionalAdvanceAISafetyAddendum=true&dataSharing=false", False),
])
def test_publisher_policy_exceptions_do_not_automatically_inherit_zdr(query: str, held: bool) -> None:
    rows = vertex_claude.native_rows([{
        "name": "publishers/anthropic/models/claude-opus-5", "versionId": "default",
        "supportedActions": {"requestAccess": {"references": {
            "global": {"uri": f"https://console.cloud.google.com/vertex-ai/model-garden/questionnaire?{query}"},
        }}},
    }])
    row = rows["anthropic/claude-opus-5"]
    assert (row.get("routable") is False) is held
    if held:
        assert row["routable_reason"] == "vertex-publisher-policy-review"


@pytest.mark.parametrize("mutation", ["regional", "conflict", "cache-write", "columns"])
def test_claude_prices_fail_closed(mutation: str) -> None:
    html = FIXTURE.read_text()
    if mutation == "regional":
        html = html.replace("Global", "US multi-region")
    elif mutation == "conflict":
        html += FIXTURE.read_text().replace("$2.00", "$7.00")
    elif mutation == "cache-write":
        html = html.replace("$0.125", "$0.126")
    else:
        html = html.replace("</tr>", "<td>unexpected</td></tr>")
    with pytest.raises(ValueError):
        vertex_claude.parse(html)


@pytest.mark.parametrize("ok,status", [(True, 200), (False, 403), (False, 404), (False, 429)])
def test_claude_admission_requires_own_access_price_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: bool, status: int,
) -> None:
    ids = ["anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "anthropic/claude-haiku-4.5"]
    manifest = tmp_path / "google-vertex.json"
    manifest.write_text(json.dumps({"provider": "google-vertex", "models": []}))
    monkeypatch.setattr(vertex, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(vertex, "_authorization", lambda: ({}, "project"))
    monkeypatch.setattr(vertex, "_live_model_ids", lambda headers: set())
    monkeypatch.setattr(vertex, "_live_claude_rows", lambda headers: vertex_claude.native_rows([
        {"name": "publishers/anthropic/models/claude-sonnet-5-5", "versionId": "default"},
        {"name": "publishers/anthropic/models/claude-opus-5-5", "versionId": "default"},
        {"name": "publishers/anthropic/models/claude-haiku-4-5", "versionId": "20251001"},
    ]))
    monkeypatch.setattr(vertex, "_metadata", lambda: {
        mid: {"context_length": 1_000_000, "max_output_tokens": 128_000} for mid in ids[:2]
    })
    monkeypatch.setattr(vertex, "fetch_html", lambda url: FIXTURE.read_text())
    monkeypatch.setattr(vertex, "parse", lambda html: {})
    probes: list[str] = []

    def probe(headers: dict[str, str], project: str, native_id: str) -> tuple[bool, int]:
        probes.append(native_id)
        return ok, status

    monkeypatch.setattr(vertex_claude, "probe", probe)
    result = vertex.fetch()
    vertex.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert probes == ["claude-sonnet-5-5"]
    assert rows[ids[0]]["routable"] is ok
    assert rows[ids[0]]["canary_status_code"] == status
    assert rows[ids[0]]["cached_input_token_price_per_m"] == 100_000
    assert rows[ids[1]]["routable"] is False
    assert rows[ids[1]]["routable_reason"] == "vertex-price-context-incomplete"
    assert rows[ids[2]]["routable_reason"] == "vertex-metadata-unavailable"
    assert result.price_index_model_ids == (frozenset({ids[0]}) if ok else frozenset())


@pytest.mark.parametrize("mutation", [None, "empty", "thinking-only", "bad-usage", "truncated", "invalid-json", "404"])
def test_claude_probe_checks_visible_output_usage_and_native_auth(
    monkeypatch: pytest.MonkeyPatch, mutation: str | None,
) -> None:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": "PONG"}],
        "usage": {"input_tokens": 11, "output_tokens": 3}, "stop_reason": "end_turn",
    }
    if mutation == "empty":
        payload["content"] = []
    elif mutation == "thinking-only":
        payload["content"][0]["type"] = "thinking"
    elif mutation == "bad-usage":
        payload["usage"]["output_tokens"] = True
    elif mutation == "truncated":
        payload["stop_reason"] = "max_tokens"

    def post(url: str, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/locations/global/publishers/anthropic/models/claude-sonnet-5-5:rawPredict")
        assert kwargs["headers"] == {"Authorization": "Bearer test"}
        assert kwargs["json"]["anthropic_version"] == "vertex-2023-10-16"
        assert "model" not in kwargs["json"]
        if mutation == "invalid-json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(404 if mutation == "404" else 200, json=payload)

    monkeypatch.setattr(vertex_claude.httpx, "post", post)
    assert vertex_claude.probe({"Authorization": "Bearer test"}, "project", "claude-sonnet-5-5") == (
        mutation is None, 404 if mutation == "404" else None if mutation == "invalid-json" else 200,
    )
