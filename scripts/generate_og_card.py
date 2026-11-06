#!/usr/bin/env python3
"""Render a link-preview card in the homepage card style; by default the site's default card.

Every page without a card of its own unfurls with ``static/og.png`` through the
``og_image`` default in ``templates/public/_base.html``. On 2026-11-02 that was
3,639 of the 3,823 sitemap URLs: every model page, every model comparison, the
docs, legal and most core pages. This script renders that card from the
homepage card's own HTML (``docs/homepage/social-card.html``, the source of
``static/homepage/social-card-v2.jpg``) with the headline and subhead swapped
in, so the default card and the homepage card are one design by construction.

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
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
HOMEPAGE_CARD = REPO_ROOT / "docs" / "homepage" / "social-card.html"
STATIC_DIR = REPO_ROOT / "src" / "trusted_router" / "static"
DEFAULT_OUT = STATIC_DIR / "og.png"
PLAYWRIGHT_DIR = REPO_ROOT / "node_modules" / "@playwright" / "test"

WIDTH, HEIGHT = 1200, 630
MAX_BYTES = 300_000
MAX_HEADLINE_WORDS = 7
MIN_NODE_MAJOR = 18
# A headline wider than this shrinks from the homepage card's 76px until both
# lines fit, so the river stays visible on the right. The homepage words fit.
HEADLINE_MAX_WIDTH = 840
HEADLINE_MIN_SIZE = 56

# The homepage card is one <h1>, one <p> and a charset meta; those three are
# what the script replaces or anchors on.
_H1 = re.compile(r"<h1>.*?</h1>", re.S)
_P = re.compile(r"<p>.*?</p>", re.S)
_CHARSET = '<meta charset="utf-8">'


@dataclass(frozen=True)
class Card:
    headline: str
    accent: str  # italic pale-green second line; "" for a one-line headline
    subhead: str

    @property
    def words(self) -> int:
        return len(f"{self.headline} {self.accent}".split())

    @property
    def headline_html(self) -> str:
        headline = html.escape(self.headline)
        if self.accent:
            headline += f"<br><em>{html.escape(self.accent)}</em>"
        return headline


# The homepage card's words; tests/test_og_default_card.py keeps them equal to
# the homepage file. Changing them changes what most of the site shows when a
# link is shared.
DEFAULT_CARD = Card(
    headline="Encrypt all your",
    accent="AI chats",
    subhead="Access hundreds of models confidentially through a single OpenAI-compatible API.",
)

# Runs in Node against the repo's @playwright/test: load the filled card, fail
# loudly if the font or an image did not load (a silent fallback would ship a
# wrong card to thousands of pages), shrink a too-wide headline, screenshot.
_RENDER_JS = """
const fs = require("fs");
const { pathToFileURL } = require("url");
// The script runs from a temp folder; resolve Playwright from the repo's node_modules.
const { chromium } = require(require.resolve("@playwright/test", { paths: [process.cwd()] }));
const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({
      viewport: { width: job.width, height: job.height },
      deviceScaleFactor: 1,
    });
    await page.goto(pathToFileURL(job.html).href);
    await page.evaluate(async ({ maxWidth, minSize }) => {
      await document.fonts.ready;
      const archivo = [...document.fonts].find((f) => f.family.replace(/["']/g, "") === "Archivo");
      if (!archivo || archivo.status !== "loaded") {
        throw new Error("Archivo did not load: " + (archivo ? archivo.status : "no @font-face"));
      }
      for (const img of document.images) {
        await img.decode();
        if (!img.naturalWidth) throw new Error("image did not load: " + img.getAttribute("src"));
      }
      // Measure the text itself, not the block: a block's scrollWidth is never
      // below its own box, so it cannot tell a short headline from a long one.
      const h1 = document.querySelector("h1");
      h1.style.whiteSpace = "nowrap";
      h1.style.width = "max-content";
      h1.style.maxWidth = "none";
      let size = parseFloat(getComputedStyle(h1).fontSize);
      while (h1.getBoundingClientRect().width > maxWidth && size > minSize) {
        size -= 2;
        h1.style.fontSize = size + "px";
        h1.style.letterSpacing = "-0.045em";
      }
    }, job);
    await page.screenshot({ path: job.png, type: "png" });
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""


def render_html(card: Card) -> str:
    """The homepage card's HTML with this card's words and a <base> so its relative
    asset paths resolve from the homepage folder wherever the copy is written."""
    source = HOMEPAGE_CARD.read_text()
    if len(_H1.findall(source)) != 1 or len(_P.findall(source)) != 1 or _CHARSET not in source:
        raise ValueError(
            f"{HOMEPAGE_CARD} no longer has one <h1>, one <p> and a charset meta; "
            "update render_html to match it"
        )
    page = _H1.sub(lambda _: f"<h1>{card.headline_html}</h1>", source, count=1)
    page = _P.sub(lambda _: f"<p>{html.escape(card.subhead)}</p>", page, count=1)
    base = f'<base href="{HOMEPAGE_CARD.parent.as_uri()}/">'
    return page.replace(_CHARSET, _CHARSET + base, 1)


def compress(path: Path) -> int:
    """Reduce a screenshot to a palette PNG under MAX_BYTES; returns the final size.

    A full-colour 1200x630 render of the river photo is about 600 KB. Median-cut
    palettes keep the photo's colours (the octree and max-coverage quantisers
    shift them) and stay smooth without dithering, which showed as grain in the
    sky. The palette shrinks only as far as the size limit needs.
    """
    if path.stat().st_size <= MAX_BYTES:
        return path.stat().st_size
    with Image.open(path) as source:
        image = source.convert("RGB")
    for colors in (256, 192, 160, 128, 96, 64):
        image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).save(
            path, format="PNG", optimize=True
        )
        if path.stat().st_size <= MAX_BYTES:
            break
    return path.stat().st_size


def node_problem(node: str) -> str | None:
    """Why rendering cannot start, in one line, or None when Node and Playwright are ready."""
    if not PLAYWRIGHT_DIR.is_dir():
        return f"{PLAYWRIGHT_DIR.relative_to(REPO_ROOT)} is missing; run `npm ci` in the repo root"
    try:
        version = subprocess.run(  # noqa: S603 - fixed argv
            [node, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return f"cannot run {node!r}; install Node {MIN_NODE_MAJOR} or newer"
    major = int(version.lstrip("v").split(".")[0])
    if major < MIN_NODE_MAJOR:
        return f"{node} is {version}; Playwright needs Node {MIN_NODE_MAJOR} or newer"
    return None


def render(card: Card, out: Path, node: str = "node") -> int:
    """Render one card to ``out`` and return its size in bytes."""
    out = out.resolve()  # Node runs from the repo root; keep both sides on one path
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="og-card-") as tmp:
        work = Path(tmp)
        page = work / "card.html"
        page.write_text(render_html(card))
        script = work / "render.js"
        script.write_text(_RENDER_JS)
        job = work / "job.json"
        job.write_text(
            json.dumps(
                {
                    "html": str(page),
                    "png": str(out),
                    "width": WIDTH,
                    "height": HEIGHT,
                    "maxWidth": HEADLINE_MAX_WIDTH,
                    "minSize": HEADLINE_MIN_SIZE,
                }
            )
        )
        subprocess.run(  # noqa: S603 - fixed argv, our own script and job file
            [node, str(script), str(job)], cwd=REPO_ROOT, check=True
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
    problem = node_problem(args.node)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    size = render(card, args.out, args.node)
    flag = "" if size <= MAX_BYTES else "  OVER LIMIT"
    print(f"{args.out}  {size // 1024} KB{flag}")
    return 0 if size <= MAX_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main())
