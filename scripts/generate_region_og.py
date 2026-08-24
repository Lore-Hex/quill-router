#!/usr/bin/env python3
"""Generate the social cards for /us-ai-models, /eu-ai-models, /china-ai-models.

The three region directories answer one question each: which labs build models
here, and which companies operate the endpoints. The card carries that split --
labs on one side, operators on the other -- because the split IS the product,
and a card that showed a single number would flatten the thing the pages exist
to separate.

Counts are read from the live catalog at generation time, so a regenerated card
cannot drift from the page it fronts. Same palette and 1200x630 frame as
generate_provider_og.py; Pillow only, no rsvg dependency.

    uv run python scripts/generate_region_og.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trusted_router.catalog import MODELS, model_open_weights  # noqa: E402
from trusted_router.catalog_data import MODEL_ORIGINS, PROVIDERS  # noqa: E402

WIDTH, HEIGHT = 1200, 630
STATIC_DIR = REPO_ROOT / "src" / "trusted_router" / "static"
OUT_DIR = STATIC_DIR / "og"
FONT_PATH = STATIC_DIR / "fonts" / "archivo-latin.woff2"

BG = "#07131f"
PANEL = "#0e2132"
LINE = "#20384b"
WHITE = "#f7fbff"
MUTED = "#a9bed0"
GREEN = "#72deb0"
BLUE = "#6ba5f2"

REGIONS = (
    ("us-ai-models", "United States", ("US",), "US AI models & providers"),
    ("eu-ai-models", "European Union", ("FR", "NL", "SE", "DE", "IE", "ES", "IT", "PL", "FI"),
     "EU AI models & providers"),
    ("china-ai-models", "China", ("CN",), "Chinese AI models & providers"),
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _models() -> list:
    return MODELS if isinstance(MODELS, (list, tuple)) else list(MODELS.values())


def _providers() -> list:
    return PROVIDERS if isinstance(PROVIDERS, (list, tuple)) else list(PROVIDERS.values())


def _facts(countries: tuple[str, ...]) -> dict[str, int]:
    """Labs, models and operators for one region, from the live catalog."""
    labs = {
        prefix
        for prefix, origin in MODEL_ORIGINS.items()
        if getattr(origin, "country", None) in countries
    }
    models = [
        model
        for model in _models()
        if (model.id.split("/")[0] if "/" in model.id else "") in labs
    ]
    # Same definition the page section uses: exclude TrustedRouter's own meta
    # routes, which are open weight because their candidates are. A card that
    # counted them would print a bigger number than the page it fronts.
    open_weight = [
        model
        for model in models
        if model_open_weights(model) and model.id.split("/")[0] != "trustedrouter"
    ]
    operators = [
        provider
        for provider in _providers()
        if getattr(provider, "provider_headquarters_country", None) in countries
    ]
    return {
        "labs": len(labs),
        "models": len(models),
        "open_weight": len(open_weight),
        "operators": len(operators),
    }


def _stat(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, value: str, label: str,
          accent: str) -> None:
    draw.rounded_rectangle((x, y, x + w, y + 132), radius=14, fill=PANEL, outline=LINE, width=1)
    draw.text((x + 22, y + 26), value, font=_font(52), fill=accent)
    draw.text((x + 22, y + 92), label.upper(), font=_font(15), fill=MUTED)


def build(slug: str, region_label: str, countries: tuple[str, ...], title: str) -> Path:
    facts = _facts(countries)
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # Hairline frame, matching the provider cards.
    draw.rounded_rectangle((28, 28, WIDTH - 28, HEIGHT - 28), radius=22, outline=LINE, width=2)

    draw.text((72, 78), "TRUSTEDROUTER", font=_font(20), fill=GREEN)
    draw.text((72, 128), title, font=_font(58), fill=WHITE)

    # The one sentence the pages exist for.
    draw.text(
        (72, 210),
        "Who built the model, and who operates the endpoint.",
        font=_font(26),
        fill=MUTED,
    )
    draw.text((72, 248), "Two separate facts, both listed.", font=_font(26), fill=MUTED)

    gap, x = 22, 72
    width = (WIDTH - 144 - gap * 3) // 4
    for value, label, accent in (
        (str(facts["labs"]), f"labs in {region_label}", BLUE),
        (str(facts["models"]), "models built there", WHITE),
        (str(facts["open_weight"]), "open weight", GREEN),
        (str(facts["operators"]), "operators based there", BLUE),
    ):
        _stat(draw, x, 330, width, value, label, accent)
        x += width + gap

    draw.text((72, HEIGHT - 82), f"trustedrouter.com/{slug}", font=_font(24), fill=MUTED)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{slug}.png"
    image.save(out, "PNG", optimize=True)
    return out


def main() -> int:
    for slug, region_label, countries, title in REGIONS:
        out = build(slug, region_label, countries, title)
        facts = _facts(countries)
        print(f"{out.relative_to(REPO_ROOT)}  {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
