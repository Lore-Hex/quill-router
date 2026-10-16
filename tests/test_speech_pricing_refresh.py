from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import requests

from scripts.pricing import speech

OLD_TOKEN_PRICES = {
    "google/gemini-3.8-flash-tts": {"input": 500_000, "output": 9_000_000},
    "google/gemini-3.8-flash-lite-tts": {"input": 500_000, "output": 6_000_000},
}
NEW_TOKEN_PRICES = {
    "google/gemini-3.8-flash-tts": {"input": 1_000_000, "output": 18_000_000},
    "google/gemini-3.8-flash-lite-tts": {"input": 1_000_000, "output": 12_000_000},
}
MICROSOFT_ARTICLE = Path(__file__).parent / "fixtures/pricing/microsoft_speech_article_2026-10-12.html"


def _freeze_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(today, datetime.min.time(), tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(speech, "datetime", Clock)


@pytest.fixture
def refresh_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    snapshot = tmp_path / "speech.json"
    snapshot.write_text(json.dumps({
        "checked_on": "2026-12-30",
        "unit": "microdollars_per_million_input_characters",
        "prices": {
            "x-ai/grok-voice-tts-1.0": 15_000_000,
            "mistralai/voxtral-mini-tts-2603": 16_000_000,
            "elevenlabs/eleven-v3": 80_000_000,
            "elevenlabs/eleven-multilingual-v2": 80_000_000,
            "elevenlabs/eleven-flash-v2.5": 40_000_000,
            "elevenlabs/eleven-turbo-v2.5": 40_000_000,
            "microsoft/mai-voice-2.1": 22_000_000,
            "microsoft/mai-voice-2.1-flash": 15_000_000,
        },
        "token_prices": OLD_TOKEN_PRICES,
        "request_price_unit": "microdollars_per_request",
        "request_prices": {"qwen/qwen3-tts": 10000, "mistralai/voxtral-4b-tts-2603": 2000},
    }))
    pages = {
        speech.TINFOIL_URL: json.dumps({"data": [
            {"id": model, "type": "tts", "endpoints": ["/v1/audio/speech"],
             "pricing": {"inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0, "requestPrice": price}}
            for model, price in (("qwen3-tts", 0.01), ("voxtral-tts", 0.002))
        ]}),
        speech.SOURCES["x-ai/grok-voice-tts-1.0"]:
            "<table><tr><td>Pricing</td><td>$15 / 1M chars</td></tr></table>",
        speech.SOURCES["mistralai/voxtral-mini-tts-2603"]:
            "<table><tr><td>Voxtral TTS</td><td>-</td><td>-</td><td>$16 /M Chars</td></tr></table>",
        speech.ELEVENLABS_URL: "".join(
            f"<section><h3>{name}</h3><p>Text to Speech</p><p>${price}</p><p>Price per 1K characters</p></section>"
            for name, price in (("v3", "0.08"), ("v2 Multilingual", "0.08"), ("Flash / Turbo", "0.04"))
        ),
        speech.MICROSOFT_URL: MICROSOFT_ARTICLE.read_text(),
        speech.GEMINI_URL: "".join(
            f"""<h2>{name}</h2><h3>Standard</h3><table>
            <tr><th></th><th>Free</th><th>Paid Tier, per 1M tokens in USD</th></tr>
            <tr><td>Input price</td><td>Free</td><td>$0.50 (text) through December 31, 2026. $1.00 (text) starting January 1, 2027.</td></tr>
            <tr><td>Output price</td><td>Free</td><td>${rate} (audio) through December 31, 2026. ${rate * 2} (audio) starting January 1, 2027.</td></tr>
            </table>"""
            for name, rate in (("Gemini 3.8 Flash TTS", 9), ("Gemini 3.8 Flash-Lite TTS", 6))
        ),
    }

    def get(url: str, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = pages[url].encode()
        return response

    monkeypatch.setattr(speech, "SNAPSHOT", snapshot)
    monkeypatch.setattr(speech.requests, "get", get)
    return snapshot, pages


def test_tinfoil_request_tariffs_refresh(refresh_sources):
    path, pages = refresh_sources
    speech.refresh()
    snapshot = json.loads(path.read_text())
    assert snapshot["request_price_unit"] == "microdollars_per_request"
    assert snapshot["request_prices"] == {"qwen/qwen3-tts": 10000, "mistralai/voxtral-4b-tts-2603": 2000}
    assert "qwen/qwen3-tts" not in snapshot["prices"]
    before = path.read_bytes()
    pages[speech.TINFOIL_URL] = pages[speech.TINFOIL_URL].replace('"requestPrice": 0.01', '"requestPrice": 0.03')
    with pytest.raises(ValueError, match="requires review"):
        speech.refresh()
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["unit", "missing", "duplicate", "conflicting", "zero", "precision", "unlabelled"])
def test_microsoft_article_fail_closed(change: str) -> None:
    html = MICROSOFT_ARTICLE.read_text()
    if change == "unit":
        html = html.replace("1M characters", "1M tokens")
    elif change == "missing":
        html = html.replace("MAI-Voice-2.1-Flash:", "Another model:")
    elif change in {"duplicate", "conflicting"}:
        html += MICROSOFT_ARTICLE.read_text().replace("$22", "$23" if change == "conflicting" else "$22")
    elif change == "zero":
        html = html.replace("$22", "$0")
    elif change == "precision":
        html = html.replace("$22", "$22.0000001")
    else:
        html = html.replace('class="text-lead"', 'class="description"')
    with pytest.raises(ValueError):
        speech.parse_microsoft_prices(html)


def test_microsoft_fetch_failure_preserves_snapshot(
    refresh_sources: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _ = refresh_sources
    before = snapshot.read_bytes()
    get = speech.requests.get

    def reject(url: str, **kwargs: object) -> requests.Response:
        if url == speech.MICROSOFT_URL:
            response = requests.Response()
            response.status_code = 403
            return response
        return get(url, **kwargs)

    monkeypatch.setattr(speech.requests, "get", reject)
    with pytest.raises(requests.HTTPError):
        speech.refresh()
    assert snapshot.read_bytes() == before


def test_microsoft_missing_price_preserves_snapshot(refresh_sources: tuple[Path, dict[str, str]]) -> None:
    snapshot, pages = refresh_sources
    before = snapshot.read_bytes()
    pages[speech.MICROSOFT_URL] = pages[speech.MICROSOFT_URL].replace("$15", "Unavailable")
    with pytest.raises(ValueError):
        speech.refresh()
    assert snapshot.read_bytes() == before


@pytest.mark.parametrize("today", [date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)])
def test_refresh_publishes_dated_gemini_transition(
    refresh_sources: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch, today: date,
) -> None:
    snapshot, _ = refresh_sources
    before = json.loads(snapshot.read_text())
    _freeze_today(monkeypatch, today)
    # Start from the old snapshot even after the boundary: a missed refresh
    # must catch up. A subsequent refresh must remain publishable too.
    for _ in range(2):
        speech.refresh()
        after = json.loads(snapshot.read_text())
        assert after == {
            **before,
            "checked_on": today.isoformat(),
            "token_prices": OLD_TOKEN_PRICES if today.year == 2026 else NEW_TOKEN_PRICES,
        }


@pytest.mark.parametrize("change", ["early", "input_only", "output_only", "wrong_old", "next_doubling", "character"])
def test_refresh_rejects_unapproved_doubling(
    refresh_sources: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    snapshot, pages = refresh_sources
    today = date(2026, 12, 31) if change == "early" else date(2027, 1, 1)
    _freeze_today(monkeypatch, today)
    if change == "early":
        pages[speech.GEMINI_URL] = pages[speech.GEMINI_URL].replace(
            "December 31, 2026", "December 30, 2026"
        ).replace("January 1, 2027", "December 31, 2026")
    elif change == "input_only":
        pages[speech.GEMINI_URL] = pages[speech.GEMINI_URL].replace("$18 (audio)", "$9 (audio)")
    elif change == "output_only":
        pages[speech.GEMINI_URL] = pages[speech.GEMINI_URL].replace("$1.00 (text)", "$0.50 (text)")
    elif change == "wrong_old":
        before = json.loads(snapshot.read_text())
        before["token_prices"]["google/gemini-3.8-flash-tts"]["output"] = 8_000_000
        snapshot.write_text(json.dumps(before))
    elif change == "next_doubling":
        before = json.loads(snapshot.read_text())
        before["token_prices"] = NEW_TOKEN_PRICES
        snapshot.write_text(json.dumps(before))
        pages[speech.GEMINI_URL] = pages[speech.GEMINI_URL].replace(
            "$1.00 (text)", "$2.00 (text)"
        ).replace("$18 (audio)", "$36 (audio)").replace("$12 (audio)", "$24 (audio)")
    else:
        url = speech.SOURCES["x-ai/grok-voice-tts-1.0"]
        pages[url] = pages[url].replace("$15", "$30")
    before_bytes = snapshot.read_bytes()
    with pytest.raises(ValueError, match="Speech price change requires review"):
        speech.refresh()
    assert snapshot.read_bytes() == before_bytes


@pytest.mark.parametrize("today, change", [
    (date(2026, 12, 31), "scheduled"),
    (date(2027, 1, 1), "scheduled"),
    (date(2027, 2, 1), "scheduled"),
    (date(2026, 12, 31), "early"),
    (date(2027, 1, 1), "chat"),
    (date(2027, 1, 1), "input_only"),
    (date(2027, 1, 1), "output_only"),
    (date(2027, 1, 1), "different"),
    (date(2027, 1, 1), "wrong_old"),
    (date(2027, 1, 1), "next_doubling"),
])
def test_gemini_tariff_manifest_spike_gate_and_publication(
    refresh_sources: tuple[Path, dict[str, str]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    today: date, change: str,
) -> None:
    from copy import deepcopy
    from dataclasses import replace

    from scripts import check_price_spike
    from scripts.pricing import refresh
    from scripts.pricing.base import ModelPrice, ProviderPricingResult
    from scripts.pricing.providers import gemini
    from trusted_router import pricing_approvals

    speech_path, _ = refresh_sources
    _freeze_today(monkeypatch, today)
    monkeypatch.setattr(pricing_approvals, "datetime", speech.datetime)
    speech.refresh()
    tariffs = json.loads(speech_path.read_text())["token_prices"]
    old_tariffs = deepcopy(OLD_TOKEN_PRICES)
    flash = "google/gemini-3.8-flash-tts"
    chat = "google/gemini-3.8-flash"
    discovered = "google/gemini-test-new-chat"
    should_hold = change != "scheduled"
    # Simulate bad downstream data independently of the speech refresh guard:
    # the manifest/workflow gates must not rely on that earlier validation.
    if change == "early":
        tariffs = deepcopy(NEW_TOKEN_PRICES)
    elif change == "input_only":
        tariffs[flash]["output"] = OLD_TOKEN_PRICES[flash]["output"]
    elif change == "output_only":
        tariffs[flash]["input"] = OLD_TOKEN_PRICES[flash]["input"]
    elif change == "different":
        tariffs[flash]["output"] += 1
    elif change == "wrong_old":
        old_tariffs[flash]["output"] -= 1
    elif change == "next_doubling":
        old_tariffs = deepcopy(NEW_TOKEN_PRICES)
        tariffs = {model: {k: v * 2 for k, v in pair.items()} for model, pair in tariffs.items()}
    # The workflow starts refresh in a new process after speech.refresh();
    # emulate its import of the freshly written speech snapshot without reload
    # side effects on other tests' runtime catalog objects.
    monkeypatch.setattr(gemini, "SPEECH_MODELS", {
        model: replace(gemini.SPEECH_MODELS[model], input_token_cost=pair["input"],
                       output_token_cost=pair["output"])
        for model, pair in tariffs.items()
    })
    manifest_dir = tmp_path / "provider_models"
    manifest_dir.mkdir()
    manifest = manifest_dir / "google-ai-studio.json"
    manifest.write_text(json.dumps({
        "provider": "google-ai-studio", "price_scale": "microdollars_per_million",
        "models": [
            {"id": model, "upstream_id": model.removeprefix("google/"),
             "input_token_price_per_m": pair["input"],
             "output_token_price_per_m": pair["output"]}
            for model, pair in {**old_tariffs, chat: {"input": 1_000_000, "output": 2_000_000}}.items()
        ],
    }))
    snapshot = tmp_path / "openrouter_snapshot.json"
    snapshot.write_text(json.dumps({"models": [{
        "id": chat, "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "endpoints": [{"tr_provider_slug": "google-ai-studio", "tag": "google-ai-studio",
                       "model_id": chat.removeprefix("google/"),
                       "pricing": {"prompt": "0.000001", "completion": "0.000002"}}],
    }]}))
    parsers = tmp_path / "parsers"
    parsers.mkdir()
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", ("gemini",))
    monkeypatch.setattr(gemini, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(gemini, "_DISCOVERED_MANIFEST_ROWS", {
        model: {"id": model, "upstream_id": model.removeprefix("google/")}
        for model in (*tariffs, chat, discovered)
    })
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: json.loads(snapshot.read_text()))
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda _requirements: None)
    chat_price = 2_000_000 if change == "chat" else 1_100_000

    def fetch():
        return {"gemini": ProviderPricingResult(slug="gemini", source="api", prices={
            chat: ModelPrice(chat_price, 2_000_000),
            discovered: ModelPrice(1_000_000, 2_000_000),
        })}, []

    monkeypatch.setattr(refresh, "_fetch_all_providers", fetch)
    real_spiking_results = refresh._spiking_results
    holds = []

    def inspect_candidate(baseline, results, held):
        detected = real_spiking_results(baseline, results, held)
        holds.append(detected)
        # Exercise the workflow's CLI gate as well as the orchestrator's gate,
        # before rollback can hide an incorrect approval or rejection.
        assert check_price_spike.main([
            str(baseline / snapshot.name), str(snapshot),
            "--before-provider-manifests", str(baseline / manifest_dir.name),
            "--after-provider-manifests", str(manifest_dir),
        ]) == (1 if detected else 0)
        return detected

    monkeypatch.setattr(refresh, "_spiking_results", inspect_candidate)
    manifest_before = manifest.read_bytes()
    # A late first run must catch up, and a repeated run must not freeze Gemini.
    for _ in range(2):
        holds.clear()
        assert refresh.main([]) == 0
        output = capsys.readouterr().out
        assert bool(holds[0]) == should_hold
        assert (refresh.HELD_FOR_REVIEW_HEADING in output) == should_hold
        rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
        published_chat = json.loads(snapshot.read_text())["models"][0]["endpoints"][0]["pricing"]
        if should_hold:
            assert "gemini" in holds[0]
            assert manifest.read_bytes() == manifest_before
            assert discovered not in rows
            assert published_chat["prompt"] == "0.000001"
        else:
            assert discovered in rows
            assert published_chat["prompt"] == "0.0000011"
            assert rows[chat]["input_token_price_per_m"] == chat_price
            for model, pair in tariffs.items():
                assert rows[model]["input_token_price_per_m"] == pair["input"]
                assert rows[model]["output_token_price_per_m"] == pair["output"]
