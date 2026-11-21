"""Public request controls and customer prices; never fetch or submit a generation."""

import json
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.image_generation import (
    IMAGE_MODEL_ID_SET,
    image_pricing_by_resolution,
    image_supported_parameters,
)
from trusted_router.money import microdollars_to_decimal
from trusted_router.speech import SPEECH_MODELS
from trusted_router.transcription import TRANSCRIPTION_MODELS

_VIDEO: dict[str, Any] = json.loads(
    (Path(__file__).parent / "data" / "video_discovery.json").read_bytes()
)["models"]


def _video(model_id: str, prepaid: list[ModelEndpoint]) -> dict[str, Any]:
    spec = _VIDEO[model_id]
    params = deepcopy(spec["parameters"])
    duration = params["duration"]
    if "max" in duration:
        # The job preparation API caps duration at 60, even for source video edits.
        duration["max"] = min(60, duration["max"])
    # Some gateway specs omit an enum. Advertise only the verified default
    # unless an active route has a more specific resolution tariff below.
    params["resolution"].setdefault("values", [params["resolution"]["default"]])
    routes: dict[str, Any] = {}
    for ep in prepaid:
        if ep.completion_price_microdollars_per_million_tokens > 0:
            rates = ep.output_token_price_per_m_by_resolution or dict.fromkeys(
                ("480p", "720p"), ep.completion_price_microdollars_per_million_tokens,
            )
            routes[ep.provider] = {
                "unit": "million_output_tokens",
                "usd_by_resolution": {k: microdollars_to_decimal(v) for k, v in rates.items()},
            }
        elif examples := spec["examples"].get(ep.provider):
            routes[ep.provider] = {"unit": "job", "examples": examples}
        elif ep.provider == "venice":
            routes[ep.provider] = {"unit": "job", "status": "live_quote"}
        else:
            routes[ep.provider] = {"status": "price_not_published"}
    if set(routes) == {"byteplus"} and "usd_by_resolution" in routes["byteplus"]:
        params["resolution"]["values"] = list(routes["byteplus"]["usd_by_resolution"])
        duration["max"] = min(duration["max"], 15)
        params["frame_images"]["frame_types"] = ["first_frame"]
        params.pop("input_references", None)
    if set(routes) == {"runway"} and model_id == "runway/gen-4.5":
        params["frame_images"]["frame_types"] = ["first_frame"]
    if seed_routes := sorted(set(routes).intersection(spec["seed_providers"])):
        params["seed"] = {"type": "integer", "providers": seed_routes}
    return {
        "endpoint": "/v1/videos", "parameters": params,
        "pricing": {"currency": "USD", "routes": routes},
    }


def media_discovery(
    model_id: str, endpoints: Sequence[ModelEndpoint], prices: dict[str, str],
) -> dict[str, Any] | None:
    if model_id in _VIDEO:
        return _video(model_id, [ep for ep in endpoints if ep.usage_type == "Credits"])
    if model_id in IMAGE_MODEL_ID_SET:
        params = {k: v for k, v in image_supported_parameters(model_id).items()
                  if not (k == "input_references" and v.get("max") == 0)}
        return {
            "endpoint": "/v1/images", "parameters": params,
            "pricing": {"currency": "USD", "routes": {
                ep.provider: image_pricing_by_resolution(
                    model_id, ep.prompt_price_microdollars_per_million_tokens,
                    ep.completion_price_microdollars_per_million_tokens,
                    ep.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
                    if ep.price_tiers else None,
                ) for ep in endpoints if ep.usage_type == "Credits"
            }},
        }
    if model_id in SPEECH_MODELS:
        voice = SPEECH_MODELS[model_id]
        fmt = voice.default_response_format
        return {
            "endpoint": "/v1/audio/speech", "parameters": {
                "input": {"type": "string", "required": True, "max_characters": voice.max_characters},
                "voice": {"values": list(voice.voices), "required": True},
                "response_format": {"values": list(voice.formats),
                                    **({"default": fmt} if fmt is not None else {"required": True})},
                "speed": {"min": voice.min_speed, "max": voice.max_speed, "default": 1},
            }, "pricing": {"currency": "USD", "rates": prices},
        }
    if model_id in TRANSCRIPTION_MODELS:
        trans = TRANSCRIPTION_MODELS[model_id]
        controls: dict[str, Any] = {}
        if "verbose_json" in trans.response_formats:
            controls["timestamp_granularities"] = {
                "items": ["word", "segment"], "requires_response_format": "verbose_json",
                "max_items": 1 if trans.provider == "mistral" else 2,
                **({"incompatible_with": ["language"]} if trans.provider == "mistral" else {}),
            }
        if trans.provider == "mistral":
            controls["diarize"] = {"type": "boolean", "requires_response_format": "verbose_json"}
            controls["keyterms"] = {"type": "array", "max_items": 100, "max_item_bytes": 100}
        else:
            controls["prompt"] = {"type": "string", "max_bytes": 2048}
            controls["temperature"] = {"min": 0, "max": 1}
        return {
            "endpoint": "/v1/audio/transcriptions", "parameters": {
                **controls,
                "file": {"encoding": "multipart", "formats": ["mp3", "wav"], "max_bytes": 7 * 1024 * 1024},
                "input_audio": {"encoding": "base64", "alternative_to": "file", "formats": ["mp3", "wav"]},
                "response_format": {"values": list(trans.response_formats), "default": "json"},
                "language": {"format": "two_lowercase_letters"},
            }, "pricing": {"currency": "USD", "rates": prices},
        }
    return None


def video_list_fields(media: dict[str, Any]) -> dict[str, Any]:
    params = media["parameters"]
    duration = params["duration"]
    return {
        "supported_durations": duration["values"] if "values" in duration
        else list(range(duration["min"], duration["max"] + 1)),
        "supported_resolutions": params["resolution"].get("values", []),
        "supported_aspect_ratios": params.get("aspect_ratio", {}).get("values", []),
        "supported_frame_images": params.get("frame_images", {}).get("frame_types", []),
    }


def dedicated_video_row(row: dict[str, Any]) -> dict[str, Any]:
    model_id = row["id"]
    legacy = _VIDEO[model_id]["legacy"]
    media = row["trustedrouter"]["media"]
    return {
        "id": model_id, "canonical_slug": model_id, "name": row["name"],
        "architecture": legacy["architecture"], "description": legacy["description"],
        "supported_parameters": list(media["parameters"]), **video_list_fields(media),
        "pricing": media["pricing"],
        "trustedrouter": {**legacy["trustedrouter"], "media": media,
                          "prepaid_available": row["trustedrouter"]["prepaid_available"]},
    }
