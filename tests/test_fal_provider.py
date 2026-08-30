from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image

from scripts.pricing.providers import fal
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
from trusted_router.image_generation import FIXED_IMAGE_PRICES_MICRODOLLARS, IMAGE_MODEL_ID_SET


def _png() -> str:
    output = BytesIO()
    Image.new("RGB", (1024, 1024), color="white").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


def test_fal_exact_megapixel_price_rounds_up() -> None:
    assert fal._price_for_1024_square(
        {
            "prices": [
                {
                    "endpoint_id": fal.UPSTREAM_ID,
                    "unit_price": "0.003",
                    "unit": "megapixels",
                    "currency": "USD",
                }
            ]
        }
    ) == 3_146


def test_fal_canary_requires_private_exact_png() -> None:
    assert fal._valid_canary_image(
        {
            "images": [
                {
                    "url": f"data:image/png;base64,{_png()}",
                    "width": 1024,
                    "height": 1024,
                    "content_type": "image/png",
                }
            ],
            "has_nsfw_concepts": [False],
        }
    )
    assert not fal._valid_canary_image(
        {
            "images": [
                {
                    "url": "https://provider.example/result.png",
                    "width": 1024,
                    "height": 1024,
                    "content_type": "image/png",
                }
            ],
            "has_nsfw_concepts": [False],
        }
    )


def test_fal_catalog_and_gateway_contract_match() -> None:
    assert PROVIDERS[fal.SLUG].supports_prepaid is True
    assert PROVIDERS[fal.SLUG].supports_byok is False
    assert fal.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert fal.MODEL_ID in IMAGE_MODEL_ID_SET
    assert FIXED_IMAGE_PRICES_MICRODOLLARS[fal.MODEL_ID] == {"1k": 3_146}
