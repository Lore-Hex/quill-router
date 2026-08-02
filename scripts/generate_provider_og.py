#!/usr/bin/env python3
"""Generate one 1200x630 social card for every catalog provider.

The renderer uses only vendored logos and fonts. A manifest records the exact
catalog facts drawn on each card, so routine catalog refreshes regenerate only
cards whose visible facts changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trusted_router.provider_og import ProviderOgFacts, all_provider_og_facts  # noqa: E402

WIDTH = 1200
HEIGHT = 630
CARD_VERSION = 2
STATIC_DIR = REPO_ROOT / "src" / "trusted_router" / "static"
LOGO_DIR = STATIC_DIR / "provider-logos"
OUT_DIR = STATIC_DIR / "og" / "providers"
MANIFEST_PATH = OUT_DIR / "manifest.json"
FONT_PATH = STATIC_DIR / "fonts" / "archivo-latin.woff2"

BG = "#07131f"
PANEL = "#0e2132"
LINE = "#20384b"
WHITE = "#f7fbff"
MUTED = "#a9bed0"
GREEN = "#72deb0"
BLUE = "#6ba5f2"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int) -> ImageFont.FreeTypeFont:
    for size in range(66, 39, -2):
        font = _font(size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(38)


def _contain_logo(path: Path, box: int = 154) -> Image.Image:
    with Image.open(path) as source:
        logo = source.convert("RGBA")
    logo.thumbnail((box, box), Image.Resampling.LANCZOS)
    return logo


def _manifest_row(facts: ProviderOgFacts) -> dict[str, object]:
    return {"card_version": CARD_VERSION, **facts.as_dict()}


def _read_manifest() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(MANIFEST_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(slug): row for slug, row in payload.items() if isinstance(row, dict)}


def _draw_stat(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    width: int,
    label: str,
    value: str,
) -> None:
    draw.rounded_rectangle((x, 421, x + width, 526), radius=8, fill=PANEL, outline=LINE)
    draw.text((x + 20, 441), label.upper(), font=_font(14), fill=MUTED)
    value_font = _fit_font(draw, value, width - 40)
    if value_font.size > 28:
        value_font = _font(28)
    draw.text((x + 20, 469), value, font=value_font, fill=WHITE)


def render_card(facts: ProviderOgFacts, destination: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    for x in range(0, WIDTH, 48):
        draw.line((x, 0, x, HEIGHT), fill="#0b1b29", width=1)
    for y in range(0, HEIGHT, 48):
        draw.line((0, y, WIDTH, y), fill="#0b1b29", width=1)
    draw.rectangle((0, 0, WIDTH, 6), fill=BLUE)
    draw.rectangle((600, 0, WIDTH, 6), fill=GREEN)

    trustedrouter_logo = _contain_logo(LOGO_DIR / "trustedrouter.png", box=50)
    image.paste(trustedrouter_logo, (62, 54), trustedrouter_logo)
    draw.text((128, 65), "TrustedRouter", font=_font(24), fill=WHITE)
    draw.text((128, 93), "PROVIDER ROUTE PROFILE", font=_font(13), fill=MUTED)

    draw.text((64, 160), "Provider routes on TrustedRouter", font=_font(18), fill=GREEN)
    name_font = _fit_font(draw, facts.name, 730)
    draw.text((62, 198), facts.name, font=name_font, fill=WHITE)
    draw.text(
        (64, 286),
        "Models, prices, privacy, and measured performance.",
        font=_font(25),
        fill=MUTED,
    )

    draw.rounded_rectangle((904, 94, 1128, 318), radius=8, fill="#ffffff", outline="#dce7ee")
    logo = _contain_logo(LOGO_DIR / f"{facts.slug}.png")
    image.paste(logo, (1016 - logo.width // 2, 206 - logo.height // 2), logo)

    _draw_stat(draw, x=64, width=194, label="Models", value=str(facts.model_count))
    _draw_stat(draw, x=276, width=228, label="Routes", value=str(facts.route_count))
    _draw_stat(draw, x=522, width=260, label="Access", value=facts.route_mode)
    _draw_stat(draw, x=800, width=328, label="Privacy", value=facts.privacy)

    draw.line((64, 565, 1128, 565), fill=LINE, width=1)
    draw.text(
        (64, 580),
        f"trustedrouter.com/providers/{facts.slug}",
        font=_font(18),
        fill=WHITE,
    )
    draw.text((1128, 580), "CURRENT CATALOG", font=_font(14), fill=GREEN, anchor="ra")

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)


def generate(*, force: bool = False) -> tuple[int, int]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    previous = _read_manifest()
    current: dict[str, dict[str, object]] = {}
    generated = 0
    skipped = 0
    for facts in all_provider_og_facts():
        row = _manifest_row(facts)
        current[facts.slug] = row
        destination = OUT_DIR / f"{facts.slug}.png"
        if not force and destination.is_file() and previous.get(facts.slug) == row:
            skipped += 1
            continue
        render_card(facts, destination)
        generated += 1

    manifest_text = json.dumps(current, indent=2, sort_keys=True) + "\n"
    if not MANIFEST_PATH.is_file() or MANIFEST_PATH.read_text() != manifest_text:
        MANIFEST_PATH.write_text(manifest_text)
    return generated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    generated, skipped = generate(force=args.force)
    print(f"provider OG cards: generated={generated} unchanged={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
