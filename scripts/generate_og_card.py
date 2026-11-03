#!/usr/bin/env python3
"""Render a link-preview card in the homepage card style; by default the site's default card.

Every page without a card of its own unfurls with ``static/og.png`` through the
``og_image`` default in ``templates/public/_base.html``. On 2026-11-02 that was
3,639 of the 3,823 sitemap URLs: every model page, every model comparison, the
docs, legal and most core pages. This script renders that card from
``docs/marketing/og-card.html`` with the homepage card's own words, so the
default and the homepage card (``static/homepage/social-card-v2.jpg``, from
``docs/homepage/social-card.html``) are one design.

    uv run python scripts/generate_og_card.py                       # writes static/og.png
    uv run python scripts/generate_og_card.py --out card.png \\
        --headline "Private AI routing" --accent "you can verify" \\
        --subhead "Hundreds of models through one API."              # a one-off card, same style

Rendering uses the repo's Playwright Chromium (``npm ci`` installs it; Node 18+)
and Pillow. The PNG is reduced to a median-cut palette so it stays under 300 KB.
Blog cards (generate_blog_og.py) and provider cards (generate_provider_og.py)
are separate.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import Template

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "docs" / "marketing" / "og-card.html"
STATIC_DIR = REPO_ROOT / "src" / "trusted_router" / "static"
DEFAULT_OUT = STATIC_DIR / "og.png"

WIDTH, HEIGHT = 1200, 630
MAX_BYTES = 300_000
MAX_HEADLINE_WORDS = 7
# The headline shrinks from the homepage card's 76px until both lines fit
# inside this width, so the river stays visible on the right.
HEADLINE_MAX_WIDTH = 840
HEADLINE_MIN_SIZE = 56


@dataclass(frozen=True)
class Card:
    headline: str
    accent: str  # italic pale-green second line; "" for a one-line headline
    subhead: str

    @property
    def words(self) -> int:
        return len(f"{self.headline} {self.accent}".split())


# The homepage card's words. Changing them here changes what most of the site
# shows when a link is shared; keep them in step with docs/homepage/social-card.html.
DEFAULT_CARD = Card(
    headline="Encrypt all your",
    accent="AI chats",
    subhead="Access hundreds of models confidentially through a single OpenAI-compatible API.",
)

# Runs in Node against the repo's @playwright/test: load the filled template,
# wait for the font and the photo, shrink the headline until both lines fit,
# and screenshot the 1200x630 page.
_RENDER_JS = """
const fs = require("fs");
// The script runs from a temp folder; resolve Playwright from the repo's node_modules.
const { chromium } = require(require.resolve("@playwright/test", { paths: [process.cwd()] }));
const [manifestPath] = process.argv.slice(2);
const { html, png, maxWidth, minSize } = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.goto("file://" + html);
  await page.evaluate(async ({ maxWidth, minSize }) => {
    await document.fonts.ready;
    await Promise.all([...document.images].map((img) => img.decode().catch(() => {})));
    const h1 = document.querySelector("h1");
    let size = parseFloat(getComputedStyle(h1).fontSize);
    while (h1.scrollWidth > maxWidth && size > minSize) {
      size -= 2;
      h1.style.fontSize = size + "px";
    }
  }, { maxWidth, minSize });
  await page.screenshot({ path: png, type: "png" });
  await browser.close();
})().catch((error) => { console.error(error); process.exit(1); });
"""


def render_html(card: Card, template: str | None = None) -> str:
    """Fill the card template. Relative asset paths resolve from the template's folder."""
    headline = html.escape(card.headline)
    if card.accent:
        headline += f"<br><em>{html.escape(card.accent)}</em>"
    return Template(template or TEMPLATE_PATH.read_text()).substitute(
        base=TEMPLATE_PATH.parent.as_uri() + "/",
        headline=headline,
        subhead=html.escape(card.subhead),
    )


def compress(path: Path) -> int:
    """Re-encode a screenshot as a palette PNG under MAX_BYTES; returns the final size.

    A full-colour 1200x630 render of the river photo is about 600 KB. Median-cut
    palettes keep the photo's colours (the octree and max-coverage quantisers
    shift them) and stay smooth without dithering, which showed as grain in the
    sky. The palette shrinks only as far as the size limit needs.
    """
    with Image.open(path) as source:
        image = source.convert("RGB")
    for colors in (256, 192, 160, 128, 96, 64):
        image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).save(
            path, format="PNG", optimize=True
        )
        if path.stat().st_size <= MAX_BYTES:
            break
    return path.stat().st_size


def render(card: Card, out: Path, node: str = "node") -> int:
    """Render one card to ``out`` and return its size in bytes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="og-card-") as tmp:
        work = Path(tmp)
        page = work / "card.html"
        page.write_text(render_html(card))
        script = work / "render.js"
        script.write_text(_RENDER_JS)
        manifest = work / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "html": str(page),
                    "png": str(out),
                    "maxWidth": HEADLINE_MAX_WIDTH,
                    "minSize": HEADLINE_MIN_SIZE,
                }
            )
        )
        subprocess.run(  # noqa: S603 - fixed argv, our own script and manifest
            [node, str(script), str(manifest)], cwd=REPO_ROOT, check=True
        )
    return compress(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="PNG to write")
    parser.add_argument("--headline", default=DEFAULT_CARD.headline)
    parser.add_argument("--accent", default=DEFAULT_CARD.accent, help="italic second line; '' for none")
    parser.add_argument("--subhead", default=DEFAULT_CARD.subhead)
    parser.add_argument("--node", default=shutil.which("node") or "node", help="Node 18+ binary")
    args = parser.parse_args(argv)

    card = Card(args.headline, args.accent, args.subhead)
    if card.words > MAX_HEADLINE_WORDS:
        print(f"headline over {MAX_HEADLINE_WORDS} words; it will not read at thumbnail size", file=sys.stderr)
        return 2
    size = render(card, args.out, args.node)
    flag = "" if size <= MAX_BYTES else "  OVER LIMIT"
    print(f"{args.out}  {size // 1024} KB{flag}")
    return 0 if size <= MAX_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main())
