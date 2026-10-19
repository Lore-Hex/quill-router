"""Render the Hixtube pilot's neutral lettermark and standard provider card.

Uses the existing vendored font and social-card renderer; no network requests.
Only Hixtube's assets and manifest entry are changed.
"""

from __future__ import annotations

import json

from PIL import Image, ImageDraw

from scripts.generate_provider_og import (
    LOGO_DIR,
    MANIFEST_PATH,
    OUT_DIR,
    _font,
    _manifest_row,
    _read_manifest,
    render_card,
)
from trusted_router.provider_og import all_provider_og_facts


def main() -> None:
    logo = Image.new("RGB", (256, 256), "#ffffff")
    ImageDraw.Draw(logo).text((128, 128), "H", font=_font(184), fill="#167d8d", anchor="mm")
    logo.save(LOGO_DIR / "hixtube.png", format="PNG", optimize=True)
    facts = next(row for row in all_provider_og_facts() if row.slug == "hixtube")
    render_card(facts, OUT_DIR / "hixtube.png")
    manifest = _read_manifest()
    manifest["hixtube"] = _manifest_row(facts)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
