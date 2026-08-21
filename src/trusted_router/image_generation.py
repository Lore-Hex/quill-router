"""Normalized image-generation model and endpoint contracts.

This registry is the control-plane source of truth for image discovery,
routing, and public pricing.  Provider-specific capability switches belong
here rather than in route handlers so a newly added model cannot advertise a
parameter that the attested gateway does not enforce.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Final, Literal

Capability = Mapping[str, object]
PricingKind = Literal["gemini_tokens", "openai_tokens", "fixed"]
_FIXED_PRICE_MARKUP_RATIO: Final = Decimal("1.055")


@dataclass(frozen=True)
class ImageModelSpec:
    id: str
    provider: str
    upstream_id: str
    supported_parameters: Mapping[str, Capability]
    supports_streaming: bool
    allowed_passthrough_parameters: tuple[str, ...] = ()
    pricing_kind: PricingKind = "fixed"
    # Provider list prices in microdollars. Fixed-price images are reserved
    # and settled through the additional-cost ledger; token-priced models use
    # the normal endpoint tariff instead.
    fixed_output_prices: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    fixed_input_image_price: int = 0

    def parameters(self) -> dict[str, dict[str, object]]:
        return {
            name: deepcopy(dict(descriptor))
            for name, descriptor in self.supported_parameters.items()
        }


def _enum(values: tuple[str, ...], default: str | None = None) -> Capability:
    result: dict[str, object] = {"type": "enum", "values": list(values)}
    if default is not None:
        result["default"] = default
    return MappingProxyType(result)


def _range(minimum: int, maximum: int, default: int | None = None) -> Capability:
    result: dict[str, object] = {"type": "range", "min": minimum, "max": maximum}
    if default is not None:
        result["default"] = default
    return MappingProxyType(result)


def _parameters(**parameters: Capability) -> Mapping[str, Capability]:
    return MappingProxyType(parameters)


GOOGLE_RESOLUTIONS: Final[tuple[str, ...]] = ("512", "1K", "2K", "4K")
GOOGLE_ASPECT_RATIOS: Final[tuple[str, ...]] = (
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
XAI_ASPECT_RATIOS: Final[tuple[str, ...]] = (
    "1:1",
    "3:4",
    "4:3",
    "9:16",
    "16:9",
    "2:3",
    "3:2",
    "9:19.5",
    "19.5:9",
    "9:20",
    "20:9",
    "1:2",
    "2:1",
    "auto",
)

# Gemini bills generated pixels as image-output tokens.
GOOGLE_OUTPUT_TOKENS: Final[Mapping[str, int]] = MappingProxyType(
    {"512": 747, "1K": 1120, "2K": 1680, "4K": 2520}
)

# Native Gemini dimensions. Explicit normalized sizes are deliberately limited
# to upstream-native outputs; the privacy boundary never silently rescales.
GOOGLE_NATIVE_SIZES: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        f"{width * multiplier}x{height * multiplier}": (resolution, ratio)
        for resolution, multiplier in (("512", 1), ("1K", 2), ("2K", 4), ("4K", 8))
        for ratio, (width, height) in {
            "1:1": (512, 512),
            "1:4": (256, 1024),
            "1:8": (192, 1536),
            "2:3": (424, 632),
            "3:2": (632, 424),
            "3:4": (448, 600),
            "4:1": (1024, 256),
            "4:3": (600, 448),
            "4:5": (464, 576),
            "5:4": (576, 464),
            "8:1": (1536, 192),
            "9:16": (384, 688),
            "16:9": (688, 384),
            "21:9": (792, 168),
        }.items()
    }
)


def _google_spec(model_id: str) -> ImageModelSpec:
    return ImageModelSpec(
        id=model_id,
        provider="google-ai-studio",
        upstream_id=model_id.split("/", 1)[1],
        supported_parameters=_parameters(
            n=_range(1, 1, 1),
            resolution=_enum(GOOGLE_RESOLUTIONS, "1K"),
            aspect_ratio=_enum(GOOGLE_ASPECT_RATIOS, "1:1"),
            size=_enum(tuple(GOOGLE_NATIVE_SIZES), "1024x1024"),
            input_references=_range(0, 14),
        ),
        supports_streaming=False,
        pricing_kind="gemini_tokens",
    )


def _openai_spec(
    model_id: str,
    aspect_ratios: tuple[str, ...],
    backgrounds: tuple[str, ...],
) -> ImageModelSpec:
    # The upstream models support edits, but the initial direct adapter is
    # intentionally text-to-image only. Discovery therefore omits
    # input_references instead of claiming a capability that would be ignored.
    return ImageModelSpec(
        id=model_id,
        provider="openai",
        upstream_id=model_id.split("/", 1)[1],
        supported_parameters=_parameters(
            aspect_ratio=_enum(aspect_ratios, "auto"),
            quality=_enum(("auto", "low", "medium", "high"), "auto"),
            background=_enum(backgrounds, "auto"),
            n=_range(1, 10, 1),
            output_compression=_range(0, 100),
        ),
        # Native partial-image relay is intentionally not advertised until the
        # enclave can preserve all-or-nothing settlement across disconnects.
        supports_streaming=False,
        allowed_passthrough_parameters=("moderation",),
        pricing_kind="openai_tokens",
    )


def _xai_spec(
    model_id: str,
    *,
    qualities: tuple[str, ...],
    output_prices: Mapping[str, int],
) -> ImageModelSpec:
    parameters: dict[str, Capability] = {
        "resolution": _enum(("1K", "2K"), "1K"),
        "aspect_ratio": _enum(XAI_ASPECT_RATIOS, "auto"),
        "n": _range(1, 1, 1),
    }
    if qualities:
        parameters["quality"] = _enum(qualities)
    return ImageModelSpec(
        id=model_id,
        provider="grok",
        upstream_id=model_id.split("/", 1)[1],
        supported_parameters=MappingProxyType(parameters),
        supports_streaming=False,
        pricing_kind="fixed",
        fixed_output_prices=MappingProxyType(dict(output_prices)),
    )


_OPENAI_CLASSIC_RATIOS = ("1:1", "3:2", "2:3", "auto")
_OPENAI_GPT_IMAGE_2_RATIOS = (
    "1:1",
    "3:2",
    "2:3",
    "4:3",
    "3:4",
    "16:9",
    "9:16",
    "21:9",
    "auto",
)

IMAGE_MODEL_SPECS: Final[Mapping[str, ImageModelSpec]] = MappingProxyType(
    {
        spec.id: spec
        for spec in (
            _google_spec("google/gemini-3.1-flash-image"),
            _google_spec("google/gemini-3.1-flash-image-preview"),
            _openai_spec(
                "openai/gpt-image-1-mini",
                _OPENAI_CLASSIC_RATIOS,
                ("auto", "transparent", "opaque"),
            ),
            _openai_spec(
                "openai/gpt-image-1",
                _OPENAI_CLASSIC_RATIOS,
                ("auto", "transparent", "opaque"),
            ),
            _openai_spec(
                "openai/gpt-image-2",
                _OPENAI_GPT_IMAGE_2_RATIOS,
                ("auto", "opaque"),
            ),
            _xai_spec(
                "x-ai/grok-imagine-image-quality",
                qualities=(),
                output_prices={"1k": 50_000, "2k": 70_000},
            ),
            _xai_spec(
                "x-ai/grok-imagine-image-2.0",
                qualities=("low", "medium"),
                output_prices={
                    "low_1k": 40_000,
                    "low_2k": 60_000,
                    "medium_1k": 60_000,
                    "medium_2k": 80_000,
                },
            ),
        )
    }
)
IMAGE_MODEL_IDS: Final[tuple[str, ...]] = tuple(IMAGE_MODEL_SPECS)
IMAGE_MODEL_ID_SET: Final[frozenset[str]] = frozenset(IMAGE_MODEL_SPECS)


def image_model_spec(model_id: str) -> ImageModelSpec:
    return IMAGE_MODEL_SPECS[model_id]


def image_supported_parameters(model_id: str) -> dict[str, dict[str, object]]:
    return image_model_spec(model_id).parameters()


def _token_price(rate_microdollars_per_million_tokens: int) -> float:
    return float(Decimal(rate_microdollars_per_million_tokens) / Decimal(1_000_000_000_000))


def fixed_image_customer_price_microdollars(provider_microdollars: int) -> int:
    """Return the exact customer quote for one fixed-price provider image."""
    if provider_microdollars < 0:
        raise ValueError("provider image price must be non-negative")
    return int(
        (Decimal(provider_microdollars) * _FIXED_PRICE_MARKUP_RATIO).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def fixed_image_provider_cost_microdollars(
    model_id: str, customer_microdollars: int
) -> int:
    """Resolve an enclave quote back to its registry-owned provider COGS.

    We deliberately match against the model's declared variants instead of
    algebraically reversing a markup. This rejects unknown/stale quotes and
    keeps authorization, discovery, and operator accounting on one price table.
    """
    spec = image_model_spec(model_id)
    if spec.pricing_kind != "fixed":
        raise ValueError(f"{model_id} is not a fixed-price image model")
    provider_prices = set(spec.fixed_output_prices.values())
    if spec.fixed_input_image_price:
        provider_prices.add(spec.fixed_input_image_price)
    matches = {
        provider_price
        for provider_price in provider_prices
        if fixed_image_customer_price_microdollars(provider_price)
        == customer_microdollars
    }
    if len(matches) != 1:
        raise ValueError(f"unknown fixed-price image quote for {model_id}")
    return matches.pop()


def _customer_fixed_price(provider_microdollars: int) -> float:
    return fixed_image_customer_price_microdollars(provider_microdollars) / 1_000_000


def image_pricing(
    model_id: str,
    prompt_price_microdollars_per_million_tokens: int,
    completion_price_microdollars_per_million_tokens: int,
) -> list[dict[str, object]]:
    spec = image_model_spec(model_id)
    if spec.pricing_kind == "gemini_tokens":
        input_price = _token_price(prompt_price_microdollars_per_million_tokens)
        output_price = Decimal(completion_price_microdollars_per_million_tokens) / Decimal(
            1_000_000_000_000
        )
        return [
            {"billable": "input_text", "unit": "token", "cost_usd": input_price},
            {"billable": "input_image", "unit": "token", "cost_usd": input_price},
            *(
                {
                    "billable": "output_image",
                    "unit": "image",
                    "variant": resolution.lower(),
                    "cost_usd": float(output_price * token_count),
                }
                for resolution, token_count in GOOGLE_OUTPUT_TOKENS.items()
            ),
        ]
    if spec.pricing_kind == "openai_tokens":
        return [
            {
                "billable": "input_text",
                "unit": "token",
                "cost_usd": _token_price(prompt_price_microdollars_per_million_tokens),
            },
            {
                "billable": "output_image",
                "unit": "token",
                "cost_usd": _token_price(completion_price_microdollars_per_million_tokens),
            },
        ]
    rows: list[dict[str, object]] = []
    if spec.fixed_input_image_price:
        rows.append(
            {
                "billable": "input_image",
                "unit": "image",
                "cost_usd": _customer_fixed_price(spec.fixed_input_image_price),
            }
        )
    rows.extend(
        {
            "billable": "output_image",
            "unit": "image",
            "variant": variant,
            "cost_usd": _customer_fixed_price(cost),
        }
        for variant, cost in spec.fixed_output_prices.items()
    )
    return rows
