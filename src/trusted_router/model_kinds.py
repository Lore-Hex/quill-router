"""What a catalog model makes, and the price facts the /models table shows for it.

The public model list groups models by output (text, images, video, speech,
embeddings). Image and video models are not billed per million tokens, so the
list shows their per-image or per-second price instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.image_generation import FIXED_IMAGE_PRICES_MICRODOLLARS
from trusted_router.media_discovery import _VIDEO
from trusted_router.pricing import customer_fixed_price_microdollars
from trusted_router.speech import SPEECH_MODELS
from trusted_router.transcription import TRANSCRIPTION_MODELS

MODEL_KINDS: Final[tuple[tuple[str, str], ...]] = (
    ("text", "Text"),
    ("image", "Images"),
    ("video", "Video"),
    ("speech", "Speech"),
    ("embeddings", "Embeddings"),
)
MODEL_KIND_IDS: Final[frozenset[str]] = frozenset(kind for kind, _ in MODEL_KINDS)

_RESOLUTION_ORDER: Final[tuple[str, ...]] = ("480p", "540p", "720p", "768p", "1080p", "1440p", "2k", "4k")


def model_kind(model: Model) -> str:
    outputs = set(model.output_modalities)
    if model.supports_video or "video" in outputs:
        return "video"
    if "image" in outputs:
        return "image"
    if model.id in SPEECH_MODELS or model.id in TRANSCRIPTION_MODELS or outputs & {"speech", "audio", "transcription"}:
        return "speech"
    if model.supports_embeddings or "embeddings" in outputs:
        return "embeddings"
    return "text"


def _dollars(value: Decimal) -> str:
    if value >= 1:
        text = f"{value:.2f}"
    elif value >= Decimal("0.01"):
        text = f"{value:.3f}".rstrip("0")
    else:
        text = f"{value:.4f}".rstrip("0")
    return f"${text.rstrip('.')}"


def _accepts(model: Model) -> str:
    names = {"text": "Text", "image": "Image", "audio": "Audio", "video": "Video"}
    return ", ".join(names.get(item, item.title()) for item in model.input_modalities) or "Text"


def _image_view(model: Model) -> dict[str, object]:
    fixed = FIXED_IMAGE_PRICES_MICRODOLLARS.get(model.id)
    if not fixed:
        return {"media_price": None, "media_price_sort": None, "media_accepts": _accepts(model)}
    lowest = min(customer_fixed_price_microdollars(value) for value in fixed.values())
    dollars = Decimal(lowest) / Decimal(1_000_000)
    return {
        "media_price": f"{_dollars(dollars)}/image",
        "media_price_sort": float(dollars),
        "media_accepts": _accepts(model),
    }


def _video_view(model: Model, endpoints: Sequence[ModelEndpoint]) -> dict[str, object]:
    spec = _VIDEO.get(model.id)
    if spec is None:
        return {"media_price": None, "media_price_sort": None}
    providers = {endpoint.provider for endpoint in endpoints}
    per_second: list[Decimal] = []
    for provider, examples in spec.get("examples", {}).items():
        if providers and provider not in providers:
            continue
        for example in examples:
            if example.get("usd_per_second"):
                per_second.append(Decimal(str(example["usd_per_second"])))
            elif example.get("cost_usd") and example.get("duration"):
                per_second.append(Decimal(str(example["cost_usd"])) / Decimal(example["duration"]))
    duration = spec["parameters"].get("duration", {})
    values = duration.get("values") or [v for v in (duration.get("min"), duration.get("max")) if v]
    length = f"{min(values)}–{min(max(values), 60)} s" if len(set(values)) > 1 else (f"{values[0]} s" if values else None)
    resolution = spec["parameters"].get("resolution", {})
    options = resolution.get("values") or ([resolution["default"]] if resolution.get("default") else [])
    known = [item for item in options if item.lower() in _RESOLUTION_ORDER]
    top = max(known, key=lambda item: _RESOLUTION_ORDER.index(item.lower())) if known else None
    lowest = min(per_second) if per_second else None
    return {
        "media_price": f"{_dollars(lowest)}/sec" if lowest is not None else None,
        "media_price_from": len(set(per_second)) > 1,
        "media_price_sort": float(lowest) if lowest is not None else None,
        "media_length": length,
        "media_resolution": top.upper() if top and top.lower().endswith("k") else top,
    }


def model_kind_view(model: Model, endpoints: Sequence[ModelEndpoint]) -> dict[str, object]:
    kind = model_kind(model)
    view: dict[str, object] = {"kind": kind}
    if kind == "image":
        view.update(_image_view(model))
    elif kind == "video":
        view.update(_video_view(model, endpoints))
    return view
