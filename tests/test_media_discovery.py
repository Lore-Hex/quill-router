from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.video_vehicles import archived_video_routes  # noqa: F401 - fixture
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.image_generation import IMAGE_MODEL_ID_SET
from trusted_router.media_discovery import media_discovery
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS
from trusted_router.speech import SPEECH_MODELS, speech_metadata
from trusted_router.transcription import TRANSCRIPTION_MODELS


def test_two_catalogs_agree_on_every_video(client: TestClient) -> None:
    resp = client.get("/v1/videos/models")
    assert resp.status_code == 200
    videos = {r["id"]: r for r in resp.json()["data"]}
    listed = {r["id"]: r for r in client.get("/v1/models").json()["data"]
              if "video" in r["architecture"]["output_modalities"]}
    assert videos and videos.keys() == listed.keys()
    for key, row in videos.items():
        assert row["trustedrouter"]["media"] == listed[key]["trustedrouter"]["media"]
        assert {"prompt", "completion"} <= listed[key]["pricing"].keys()
        assert row["pricing"] == listed[key]["trustedrouter"]["media"]["pricing"]
        assert row["supported_durations"] and row["supported_resolutions"]
        media = row["trustedrouter"]["media"]
        assert set(media["parameters"]) <= set(row["supported_parameters"])
        assert media["pricing"]["currency"] == "USD"
        assert set(media["pricing"]["routes"]) == {
            ep.provider for ep in endpoints_for_model(key) if ep.usage_type == "Credits"
        }
    assert videos["google/veo-3.1"]["trustedrouter"]["media"]["parameters"]["resolution"][
        "requires_duration_seconds"
    ]["1080p"] == 8


@pytest.mark.usefixtures("archived_video_routes")
def test_job_token_live_and_unavailable_prices_are_distinct(client: TestClient) -> None:
    rows = {r["id"]: r for r in client.get("/v1/videos/models").json()["data"]}
    ltx = rows["lightricks/ltx-2.3-fast"]["trustedrouter"]["media"]["pricing"]["routes"]
    assert any(ex["duration"] == 6 and ex["resolution"] == "1080p"
               and Decimal(ex["cost_usd"]) == Decimal("0.432")
               and Decimal(ex["usd_per_second"]) == Decimal("0.072")
               and 8 in ex["priced_durations"] for ex in ltx["ltx"]["examples"])
    assert ltx["venice"] == {"status": "live_quote", "unit": "job"}
    seed = rows["bytedance/seedance-2.0-fast"]
    token = seed["trustedrouter"]["media"]["pricing"]["routes"]["byteplus"]
    assert token["unit"] == "million_output_tokens"
    assert set(token["usd_by_resolution"]) == {"480p", "720p"}
    assert all(Decimal(v) > 0 for v in token["usd_by_resolution"].values())
    # Model metadata is built from routable hosts. A BytePlus hold leaves the
    # borrowed route out of it, so BytePlus's own limits apply only while live.
    if "byteplus" not in PREPAID_PROVIDER_HOLD_REASONS:
        assert set(seed["supported_resolutions"]) == set(token["usd_by_resolution"])
        assert 16 not in seed["supported_durations"] and seed["supported_frame_images"] == ["first_frame"]
    empty = media_discovery("google/veo-3.1", [], {})
    assert empty and empty["pricing"]["routes"] == {}


def test_image_and_audio_controls_and_units(client: TestClient) -> None:
    rows = {r["id"]: r for r in client.get("/v1/models").json()["data"]}
    for key in (IMAGE_MODEL_ID_SET | SPEECH_MODELS.keys() | TRANSCRIPTION_MODELS.keys()) & MODELS.keys():
        media = rows[key]["trustedrouter"]["media"]
        assert set(media["parameters"]) <= set(rows[key]["supported_parameters"])
        assert media["pricing"]["currency"] == "USD"
    whisper = rows["openai/whisper-1"]["trustedrouter"]["media"]
    assert whisper["parameters"]["timestamp_granularities"]["requires_response_format"] == "verbose_json"
    assert Decimal(whisper["pricing"]["rates"]["input_audio_minute"]) > 0
    mistral = rows["mistralai/voxtral-mini-2602"]["trustedrouter"]["media"]["parameters"]
    assert "temperature" not in mistral and "diarize" in mistral


def test_mistral_speech_requires_an_explicit_mp3_format() -> None:
    model_id = "mistralai/voxtral-mini-tts-2603"
    speech = speech_metadata(model_id)
    assert speech["default_response_format"] is None
    assert speech["example_response_format"] == "mp3"
    media = media_discovery(model_id, [], {})
    assert media and media["parameters"]["response_format"] == {"values": ["mp3"], "required": True}
