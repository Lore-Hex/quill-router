from decimal import Decimal

import pytest

from scripts.pricing.transcription import parse_price


def test_audio_eur_prices_are_duration_not_tokens() -> None:
    html = """Prices are per audio minute.
    <table><tr><th>Model</th><th>Audio</th></tr>
    <tr><td>Voxtral Mini 3B</td><td>EUR 0.004</td></tr>
    <tr><td>Whisper large-v3</td><td>EUR 0.014</td></tr></table>"""
    assert parse_price("mistralai/voxtral-mini-3b-2507", html, usd_eur_rate=Decimal("1.1208")) == 4708
    assert parse_price("openai/whisper-large-v3", html, usd_eur_rate=Decimal("1.1208")) == 16476
    for bad in (html.replace("per audio minute", "per million tokens"), html + html, html.replace("EUR", "USD")):
        with pytest.raises(ValueError):
            parse_price("openai/whisper-large-v3", bad, usd_eur_rate=Decimal("1.1208"))
    with pytest.raises(ValueError):
        parse_price("openai/whisper-large-v3", html)
