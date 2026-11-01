import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from scripts.pricing import transcription


@pytest.fixture
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "transcription.json"
    path.write_text(transcription.SNAPSHOT.read_text())
    snapshot = json.loads(path.read_text())
    snapshot["checked_on"] = "2026-10-20"
    path.write_text(json.dumps(snapshot))
    pages = {
        transcription.ECB_FX_URL: '<Envelope><Cube><Cube time="2026-10-30"><Cube currency="USD" rate="1.1"/></Cube></Cube></Envelope>',
    }
    for spec in transcription.TRANSCRIPTION_MODELS.values():
        if spec.provider == "openai":
            pages[spec.pricing_source] = "Transcription Cost / minute $0.0065"
        elif spec.provider == "mistral":
            pages[spec.pricing_source] = "voxtral-mini-2602 $0.003 /Min"
        elif spec.provider == "privatemode":
            pages[spec.pricing_source] = """per audio minute <table>
            <tr><th>Model</th><th>Audio</th></tr>
            <tr><td>Whisper large-v3</td><td>EUR 0.014</td></tr>
            <tr><td>Voxtral Mini 3B</td><td>EUR 0.004</td></tr></table>"""
        elif spec.provider == "tinfoil":
            pages[spec.pricing_source] = json.dumps([
                {"id": "whisper-large-v3-turbo", "type": "audio", "endpoints": ["/v1/audio/transcriptions"],
                 "pricing": {"requestPrice": 0.011, "inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0}},
                {"id": "voxtral-small-24b", "type": "audio", "endpoints": ["/v1/chat/completions"],
                 "pricing": {"requestPrice": 0, "inputTokenPricePer1M": 0.21, "outputTokenPricePer1M": 0.61}},
            ])

    calls = []

    def get(url, **kwargs):
        calls.append(url)
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        response = requests.Response()
        response.status_code = 200
        response._content = value.encode()
        return response

    monkeypatch.setattr(transcription, "SNAPSHOT", path)
    monkeypatch.setattr(transcription.requests, "get", get)
    return path, pages, calls


def url(model):
    return transcription.TRANSCRIPTION_MODELS[model].pricing_source


def test_missing_deprecated_tariff_holds_only_mistral(sources, capsys):
    path, pages, _ = sources
    previous = json.loads(path.read_text())
    # Reduced from Mistral's official 26.02 model page on 2026-11-01:
    # the deprecated model's price is absent, not its successor's tariff.
    pages[url("mistralai/voxtral-mini-2602")] = """<h1>Voxtral Mini Transcribe 2</h1>
    <p>voxtral-mini-2602</p><p>Deprecated</p><p>Deprecation date 10/31/2026</p>
    <a href="/models/voxtral-mini-transcribe-3-0">Voxtral Mini Transcribe 3</a>"""
    assert transcription.refresh() == 0
    after = json.loads(path.read_text())
    assert after["minute_prices"]["mistralai/voxtral-mini-2602"] == previous["minute_prices"]["mistralai/voxtral-mini-2602"]
    assert after["minute_prices"]["openai/whisper-1"] == 6500
    assert after["request_prices"]["openai/whisper-large-v3-turbo"] == 11000
    assert after["checked_on"] == previous["checked_on"]
    output = capsys.readouterr().out
    assert "Held for price review (last published prices kept):" in output
    assert "mistral" in output
    assert "Missing or conflicting transcription tariff" in output


@pytest.mark.parametrize("price", ["0.012", "0.003"])
def test_twofold_price_change_is_held_without_publishing_it(sources, price, capsys):
    path, pages, _ = sources
    previous = json.loads(path.read_text())
    pages[url("openai/whisper-1")] = f"Transcription Cost / minute ${price}"
    assert transcription.refresh() == 0
    after = json.loads(path.read_text())
    assert after["minute_prices"]["openai/whisper-1"] == previous["minute_prices"]["openai/whisper-1"]
    assert after["token_prices"]["mistralai/voxtral-small-24b"] == {"input": 210000, "output": 610000}
    assert "requires review" in capsys.readouterr().out


def test_shared_provider_is_atomic_and_fetched_once(sources):
    path, pages, calls = sources
    previous = json.loads(path.read_text())
    source = url("openai/whisper-large-v3-turbo")
    rows = json.loads(pages[source])
    rows[1]["pricing"]["outputTokenPricePer1M"] = 99
    pages[source] = json.dumps(rows)
    assert transcription.refresh() == 0
    after = json.loads(path.read_text())
    assert after["request_prices"] == previous["request_prices"]
    assert after["token_prices"] == previous["token_prices"]
    assert calls.count(source) == 1


def test_fx_failure_holds_only_euro_source_and_redacts_transport_error(sources, capsys):
    path, pages, _ = sources
    previous = json.loads(path.read_text())
    pages[transcription.ECB_FX_URL] = requests.ConnectionError("secret-in-transport-url")
    assert transcription.refresh() == 0
    after = json.loads(path.read_text())
    for model in ("openai/whisper-large-v3", "mistralai/voxtral-mini-3b-2507"):
        assert after["minute_prices"][model] == previous["minute_prices"][model]
    assert after["minute_prices"]["openai/whisper-1"] == 6500
    output = capsys.readouterr().out
    assert "privatemode" in output
    assert "secret-in-transport-url" not in output


@pytest.mark.parametrize("count,expected", [(2, 0), (3, 1), (4, 1)])
def test_strict_majority_source_failures_publish_nothing(sources, count, expected, capsys):
    path, pages, _ = sources
    previous = path.read_bytes()
    models = ["openai/whisper-1", "mistralai/voxtral-mini-2602", "openai/whisper-large-v3-turbo", "openai/whisper-large-v3"]
    for model in models[:count]:
        pages[url(model)] = requests.Timeout("private transport data")
    assert transcription.refresh() == expected
    if expected:
        assert path.read_bytes() == previous
        assert "nothing published" in capsys.readouterr().out


def test_all_sources_refresh_and_update_checked_date(sources):
    path, _, _ = sources
    assert transcription.refresh() == 0
    assert json.loads(path.read_text())["checked_on"] == datetime.now(UTC).date().isoformat()


def test_workflow_reports_transcription_holds_and_preserves_pipefail():
    workflow = (Path(__file__).parents[1] / ".github/workflows/refresh-prices.yml").read_text()
    assert "uv run python -m scripts.pricing.transcription | tee /tmp/transcription-summary.txt" in workflow
    loop = next(line for line in workflow.splitlines() if "for summary in" in line)
    assert "/tmp/transcription-summary.txt" in loop  # noqa: S108 - inspect the workflow, no file access
    assert "set -o pipefail" in workflow
