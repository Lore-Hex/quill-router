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
    }))
    pages = {
        speech.SOURCES["x-ai/grok-voice-tts-1.0"]:
            "<table><tr><td>Pricing</td><td>$15 / 1M chars</td></tr></table>",
        speech.SOURCES["mistralai/voxtral-mini-tts-2603"]:
            "<table><tr><td>Voxtral TTS</td><td>-</td><td>-</td><td>$16 /M Chars</td></tr></table>",
        speech.ELEVENLABS_URL: "".join(
            f"<section><h3>{name}</h3><p>Text to Speech</p><p>${price}</p><p>Price per 1K characters</p></section>"
            for name, price in (("v3", "0.08"), ("v2 Multilingual", "0.08"), ("Flash / Turbo", "0.04"))
        ),
        speech.MICROSOFT_URL:
            "MAI-Voice-2.1 is available at $22 per 1M characters; MAI-Voice-2.1 Flash is available at $15 per 1M characters",
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
