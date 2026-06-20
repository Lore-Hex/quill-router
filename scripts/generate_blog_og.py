#!/usr/bin/env python3
"""Generate social-card PNGs for blog posts from their first inline <svg>.

Each blog post that opens with an inline chart/diagram <svg> gets a 1200x630
``static/og/blog/<slug>.png`` rendered from that SVG (fitted on a white card),
which `dashboard._blog_og_image` then serves as the og:image. Posts that lead
with an <img>, set an explicit `og_image`, or have no imagery are skipped (they
resolve to the <img> src or the default brand card at request time).

Requires `rsvg-convert` (brew install librsvg). Run from the repo root and
commit the resulting PNGs:

    python scripts/generate_blog_og.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from trusted_router.blog import BLOG_POSTS  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "trusted_router" / "static" / "og" / "blog"
W, H, PAD = 1200, 630, 28

_SVG_BLOCK = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
_IMG = re.compile(r"<img\b", re.IGNORECASE)
_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_STYLE = re.compile(r'\sstyle="[^"]*"', re.IGNORECASE)
_WH = re.compile(r'\s(?:width|height|x|y|preserveAspectRatio)="[^"]*"', re.IGNORECASE)


def card_svg(inner_svg: str) -> str:
    """Wrap a post's <svg> in a 1200x630 white card, fitted and centered."""
    open_tag = _OPEN.match(inner_svg).group(0)
    body = inner_svg[len(open_tag):]
    # drop the inline brand mark so the card's own (cleaner, edge-placed) one is the only one
    body = re.sub(r"<text[^>]*>TrustedRouter\.com</text>", "", body)
    # strip sizing/style so our nested placement controls layout; keep viewBox + the rest
    open_tag = _WH.sub("", _STYLE.sub("", open_tag))
    # reserve a strip at the bottom for the TrustedRouter.com brand mark
    nested = (
        f'<svg x="{PAD}" y="{PAD}" width="{W - 2 * PAD}" height="{H - 2 * PAD - 40}" '
        f'preserveAspectRatio="xMidYMid meet"{open_tag[4:-1]}>{body}'
    )
    brand = (
        f'<text x="{W - PAD}" y="{H - 16}" text-anchor="end" '
        f'font-family="Inter,Arial,sans-serif" font-size="24" font-weight="700" '
        f'fill="#0f6e56">TrustedRouter.com</text>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>{nested}{brand}</svg>'
    )


def main() -> int:
    rsvg = shutil.which("rsvg-convert")
    if not rsvg:
        print("ERROR: rsvg-convert not found (brew install librsvg)", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made, skipped = [], []
    for post in BLOG_POSTS:
        body = post.body_html
        svg = _SVG_BLOCK.search(body)
        img = _IMG.search(body)
        if post.og_image or not svg or (img and img.start() < svg.start()):
            skipped.append(post.slug)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as tf:
            tf.write(card_svg(svg.group(0)))
            tmp = tf.name
        out = OUT_DIR / f"{post.slug}.png"
        subprocess.run(  # noqa: S603 - fixed argv, resolved binary, our own SVG input
            [rsvg, "-w", str(W), "-h", str(H), "-o", str(out), tmp],
            check=True,
        )
        Path(tmp).unlink(missing_ok=True)
        made.append(f"{post.slug}.png ({out.stat().st_size // 1024} KB)")
    print("generated:", *made, sep="\n  " if made else " (none)")
    print(f"skipped {len(skipped)} post(s) with no leading inline <svg>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
