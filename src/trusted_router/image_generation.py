"""Public image-generation capability metadata shared by catalog and routing.

Keep this module data-only.  The control plane must describe exactly the
capabilities the attested gateway enforces; duplicating these values in route
handlers makes capability discovery drift into a security bug (callers can no
longer tell whether a parameter was honored).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from trusted_router.pricing import customer_fixed_price_microdollars

IMAGE_MODEL_IDS: Final[tuple[str, ...]] = (
    "google/gemini-3.1-flash-image",
    "google/gemini-3.1-flash-image-preview",
    "recraft/recraftv4_1_pro",
    "recraft/recraftv4_1_utility_pro",
    "recraft/recraftv4_1",
    "recraft/recraftv4_1_utility",
    "recraft/recraftv4_pro",
    "recraft/recraftv4",
    "recraft/recraftv3",
    "recraft/recraftv2",
    "black-forest-labs/flux-2-klein-4b",
    "black-forest-labs/flux-2-klein-9b",
    "black-forest-labs/flux-2-pro",
    "black-forest-labs/flux-2-max",
    "black-forest-labs/flux-2-flex",
    "black-forest-labs/flux.1-schnell",
    "decart/lucy-image-2",
    "krea/krea-2-medium",
    "riverflow/riverflow-2-fast",
)
IMAGE_MODEL_ID_SET: Final[frozenset[str]] = frozenset(IMAGE_MODEL_IDS)

GEMINI_IMAGE_MODEL_IDS: Final[frozenset[str]] = frozenset(
    {
        "google/gemini-3.1-flash-image",
        "google/gemini-3.1-flash-image-preview",
    }
)

FIXED_IMAGE_PRICES_MICRODOLLARS: Final[dict[str, dict[str, int]]] = {
    "recraft/recraftv4_1_pro": {"2k": 210_000},
    "recraft/recraftv4_1_utility_pro": {"2k": 210_000},
    "recraft/recraftv4_1": {"1k": 35_000},
    "recraft/recraftv4_1_utility": {"1k": 35_000},
    "recraft/recraftv4_pro": {"2k": 250_000},
    "recraft/recraftv4": {"1k": 40_000},
    "recraft/recraftv3": {"1k": 40_000},
    "recraft/recraftv2": {"1k": 22_000},
    "black-forest-labs/flux-2-klein-4b": {"1k": 14_000},
    "black-forest-labs/flux-2-klein-9b": {"1k": 15_000},
    "black-forest-labs/flux-2-pro": {"1k": 30_000},
    "black-forest-labs/flux-2-max": {"1k": 70_000},
    "black-forest-labs/flux-2-flex": {"1k": 50_000},
    "black-forest-labs/flux.1-schnell": {"1k": 1_364},
    "decart/lucy-image-2": {"480p": 10_000, "720p": 20_000},
    "krea/krea-2-medium": {"1k": 30_000},
    "riverflow/riverflow-2-fast": {"1k": 20_000},
}

IMAGE_RESOLUTIONS: Final[tuple[str, ...]] = ("512", "1K", "2K", "4K")
IMAGE_ASPECT_RATIOS: Final[tuple[str, ...]] = (
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
)

# Gemini bills generated pixels as image-output tokens.  These are the
# provider-published token counts and therefore the same values the gateway
# reserves before the request and settles from the provider usage afterwards.
IMAGE_OUTPUT_TOKENS: Final[dict[str, int]] = {
    "512": 747,
    "1K": 1120,
    "2K": 1680,
    "4K": 2520,
}

# Native dimensions accepted by the normalized ``size`` parameter.  Explicit
# sizes are intentionally limited to provider-native outputs: silently scaling
# an image inside the privacy boundary would make cost and output semantics
# differ from capability discovery.
IMAGE_NATIVE_SIZES: Final[dict[str, tuple[str, str]]] = {
    "512x512": ("512", "1:1"),
    "256x1024": ("512", "1:4"),
    "192x1536": ("512", "1:8"),
    "424x632": ("512", "2:3"),
    "632x424": ("512", "3:2"),
    "448x600": ("512", "3:4"),
    "1024x256": ("512", "4:1"),
    "600x448": ("512", "4:3"),
    "464x576": ("512", "4:5"),
    "576x464": ("512", "5:4"),
    "1536x192": ("512", "8:1"),
    "384x688": ("512", "9:16"),
    "688x384": ("512", "16:9"),
    "792x168": ("512", "21:9"),
    "1024x1024": ("1K", "1:1"),
    "512x2048": ("1K", "1:4"),
    "384x3072": ("1K", "1:8"),
    "848x1264": ("1K", "2:3"),
    "1264x848": ("1K", "3:2"),
    "896x1200": ("1K", "3:4"),
    "2048x512": ("1K", "4:1"),
    "1200x896": ("1K", "4:3"),
    "928x1152": ("1K", "4:5"),
    "1152x928": ("1K", "5:4"),
    "3072x384": ("1K", "8:1"),
    "768x1376": ("1K", "9:16"),
    "1376x768": ("1K", "16:9"),
    "1584x672": ("1K", "21:9"),
    "2048x2048": ("2K", "1:1"),
    "1024x4096": ("2K", "1:4"),
    "768x6144": ("2K", "1:8"),
    "1696x2528": ("2K", "2:3"),
    "2528x1696": ("2K", "3:2"),
    "1792x2400": ("2K", "3:4"),
    "4096x1024": ("2K", "4:1"),
    "2400x1792": ("2K", "4:3"),
    "1856x2304": ("2K", "4:5"),
    "2304x1856": ("2K", "5:4"),
    "6144x768": ("2K", "8:1"),
    "1536x2752": ("2K", "9:16"),
    "2752x1536": ("2K", "16:9"),
    "3168x1344": ("2K", "21:9"),
    "4096x4096": ("4K", "1:1"),
    "2048x8192": ("4K", "1:4"),
    "1536x12288": ("4K", "1:8"),
    "3392x5056": ("4K", "2:3"),
    "5056x3392": ("4K", "3:2"),
    "3584x4800": ("4K", "3:4"),
    "8192x2048": ("4K", "4:1"),
    "4800x3584": ("4K", "4:3"),
    "3712x4608": ("4K", "4:5"),
    "4608x3712": ("4K", "5:4"),
    "12288x1536": ("4K", "8:1"),
    "3072x5504": ("4K", "9:16"),
    "5504x3072": ("4K", "16:9"),
    "6336x2688": ("4K", "21:9"),
}


def image_supported_parameters(model_id: str) -> dict[str, dict[str, object]]:
    """OpenRouter-compatible machine-readable capability descriptors."""

    parameters: dict[str, dict[str, object]] = {
        "n": {"type": "range", "min": 1, "max": 1, "default": 1},
    }
    if model_id in GEMINI_IMAGE_MODEL_IDS:
        parameters.update(
            {
                "resolution": {
                    "type": "enum",
                    "values": list(IMAGE_RESOLUTIONS),
                    "default": "1K",
                },
                "aspect_ratio": {
                    "type": "enum",
                    "values": list(IMAGE_ASPECT_RATIOS),
                    "default": "1:1",
                },
                "size": {
                    "type": "enum",
                    "values": list(IMAGE_NATIVE_SIZES),
                    "default": "1024x1024",
                },
                "input_references": {"type": "range", "min": 0, "max": 14},
            }
        )
    elif model_id == "decart/lucy-image-2":
        parameters.update(
            {
                "resolution": {
                    "type": "enum",
                    "values": ["480p", "720p"],
                    "default": "720p",
                },
                "input_references": {"type": "range", "min": 1, "max": 2},
            }
        )
    else:
        fixed_prices = FIXED_IMAGE_PRICES_MICRODOLLARS.get(model_id)
        if not fixed_prices:
            raise ValueError(f"missing fixed image pricing for {model_id}")
        resolutions = [variant.upper() for variant in fixed_prices]
        parameters.update(
            {
                "resolution": {
                    "type": "enum",
                    "values": resolutions,
                    "default": resolutions[0],
                },
                "aspect_ratio": {
                    "type": "enum",
                    "values": ["1:1"],
                    "default": "1:1",
                },
                "input_references": {"type": "range", "min": 0, "max": 0},
            }
        )
    return parameters


def image_input_modalities(model_id: str) -> list[str]:
    if model_id == "decart/lucy-image-2" or model_id in GEMINI_IMAGE_MODEL_IDS:
        return ["text", "image"]
    return ["text"]


def image_pricing_by_resolution(
    model_id: str,
    prompt_price_microdollars_per_million_tokens: int,
    completion_price_microdollars_per_million_tokens: int,
) -> list[dict[str, object]]:
    """Return input-token and exact resolution-tier output prices."""

    fixed = FIXED_IMAGE_PRICES_MICRODOLLARS.get(model_id)
    if fixed is not None:
        return [
            {
                "billable": "output_image",
                "unit": "image",
                "variant": variant,
                "cost_usd": customer_fixed_price_microdollars(cost) / 1_000_000,
            }
            for variant, cost in fixed.items()
        ]

    input_price_per_token = Decimal(prompt_price_microdollars_per_million_tokens) / Decimal(
        1_000_000_000_000
    )
    price_per_token = Decimal(completion_price_microdollars_per_million_tokens) / Decimal(
        1_000_000_000_000
    )
    return [
        {
            "billable": "input_text",
            "unit": "token",
            "cost_usd": float(input_price_per_token),
        },
        {
            "billable": "input_image",
            "unit": "token",
            "cost_usd": float(input_price_per_token),
        },
    ] + [
        {
            "billable": "output_image",
            "unit": "image",
            "variant": resolution.lower(),
            "cost_usd": float(price_per_token * token_count),
        }
        for resolution, token_count in IMAGE_OUTPUT_TOKENS.items()
    ]
