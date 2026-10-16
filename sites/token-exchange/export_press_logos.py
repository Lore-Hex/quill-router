"""Regenerate outlined press logos: uv run --with fonttools --with brotli this-file.

Requires rsvg-convert. Normal site builds use the checked-in exports, not fonts
or rendering dependencies. The mark is copied from the approved shared asset.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

HERE = Path(__file__).resolve().parent
STATIC = HERE.parents[1] / "src/trusted_router/static"
OUTPUT = HERE / "press-assets"


def outline(text: str, font: TTFont, x: float, baseline: float, size: float) -> str:
    glyphs = font.getGlyphSet()
    mapping = font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    paths = []
    for character in text:
        glyph = glyphs[mapping[ord(character)]]
        pen = SVGPathPen(glyphs)
        glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, x, baseline)))
        paths.append(pen.getCommands())
        x += glyph.width * scale
    return " ".join(paths)


def main() -> None:
    renderer = shutil.which("rsvg-convert")
    if not renderer:
        raise SystemExit("Install librsvg (rsvg-convert) first")
    OUTPUT.mkdir(exist_ok=True)
    font = instantiateVariableFont(TTFont(STATIC / "fonts/archivo-latin.woff2"), {"wght": 500})
    for variant, source, color in (
        ("light", "trustedrouter-mark.svg", "#171c16"),
        ("dark", "trustedrouter-mark-dark.svg", "#f0f1e8"),
        ("mono", "trustedrouter-mark-mono.svg", "#171c16"),
    ):
        # These are fixed, version-controlled brand assets, never uploaded XML.
        root = ET.fromstring((STATIC / source).read_text())  # noqa: S314
        # A fixed currentColor makes the standalone monochrome download portable.
        mark = ET.tostring(root, encoding="unicode").replace("currentColor", color)
        mark_path = OUTPUT / f"nyte-mark-{variant}.svg"
        mark_path.write_text(mark)
        group = ET.tostring(next(iter(root)), encoding="unicode").replace("currentColor", color)
        title = outline("New York Token Exchange", font, 124, 48, 28)
        subtitle = outline("Powered by TrustedRouter", font, 125, 77, 14)
        wordmark = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 120" '
            'width="600" height="120" role="img" aria-label="New York Token Exchange, powered by TrustedRouter">'
            '<g transform="translate(12 12) scale(4)">' + group + '</g>'
            f'<path fill="{color}" d="{title}"/><path fill="{color}" d="{subtitle}"/></svg>'
        )
        wordmark_path = OUTPUT / f"nyte-wordmark-{variant}.svg"
        wordmark_path.write_text(wordmark)
        for path, width, height in ((mark_path, 1024, 1024), (wordmark_path, 2400, 480)):
            subprocess.run(  # noqa: S603 - fixed renderer arguments and local asset paths
                [renderer, "--width", str(width), "--height", str(height), "--output", str(path.with_suffix(".png")), str(path)],
                check=True,
            )


if __name__ == "__main__":
    main()
