"""Regenerate outlined press logos: uv run --with fonttools --with brotli this-file.

Requires rsvg-convert and Arial Bold (pass --font on non-macOS machines).
Normal site builds use checked-in outlined exports, not fonts or renderers.
The globe and short wordmark follow Joseph's October 28 supplied artwork.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"))
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error("Pass --font with a locally licensed Arial Bold TTF; fonts are not distributed in the kit")
    renderer = shutil.which("rsvg-convert")
    if not renderer:
        raise SystemExit("Install librsvg (rsvg-convert) first")
    OUTPUT.mkdir(exist_ok=True)
    font = TTFont(args.font)
    for variant, color in (
        ("light", "#000000"),
        ("dark", "#ffffff"),
        ("mono", "#000000"),
    ):
        # These are fixed, version-controlled brand assets, never uploaded XML.
        root = ET.fromstring((OUTPUT / "nyte-globe-source.svg").read_text())  # noqa: S314
        # A fixed currentColor makes the standalone monochrome download portable.
        mark = ET.tostring(root, encoding="unicode").replace("currentColor", color)
        mark_path = OUTPUT / f"nyte-mark-{variant}.svg"
        mark_path.write_text(mark)
        title = outline("NY Token Exchange", font, 16, 78, 58)
        wordmark = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 120" '
            'width="600" height="120" role="img" aria-label="NY Token Exchange">'
            f'<path fill="{color}" d="{title}"/></svg>'
        )
        wordmark_path = OUTPUT / f"nyte-wordmark-{variant}.svg"
        wordmark_path.write_text(wordmark)
        group = ET.tostring(next(iter(root)), encoding="unicode").replace("currentColor", color)
        exports = [(mark_path, 2048, 1024), (wordmark_path, 2400, 480)]
        for kind, height, globe_transform, text_transform in (
            ("lockup", 450, "translate(240 15) scale(.52)", "translate(20 270) scale(1.6)"),
            ("horizontal", 200, "translate(0 20) scale(.32)", "translate(330 42) scale(1.1)"),
        ):
            combined = (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 {height}" '
                f'width="1000" height="{height}" role="img" aria-label="NY Token Exchange">'
                f'<g transform="{globe_transform}">{group}</g>'
                f'<g transform="{text_transform}"><path fill="{color}" d="{title}"/></g></svg>'
            )
            combined_path = OUTPUT / f"nyte-{kind}-{variant}.svg"
            combined_path.write_text(combined)
            exports.append((combined_path, 2400, int(height * 2.4)))
        for path, width, height in exports:
            subprocess.run(  # noqa: S603 - fixed renderer arguments and local asset paths
                [renderer, "--width", str(width), "--height", str(height), "--output", str(path.with_suffix(".png")), str(path)],
                check=True,
            )
    group = ET.tostring(next(iter(root)), encoding="unicode").replace("currentColor", "#000000")
    social = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" '
        'role="img" aria-label="NY Token Exchange">'
        '<rect width="1200" height="630" fill="#ffffff"/>'
        '<g transform="translate(340 70) scale(.52)">' + group + '</g>'
        '<g transform="translate(60 335) scale(1.8)">'
        f'<path fill="#000000" d="{title}"/></g></svg>'
    )
    social_path = OUTPUT / "nyte-social.svg"
    social_path.write_text(social)
    subprocess.run(  # noqa: S603 - fixed renderer arguments and local asset paths
        [renderer, "--width", "1200", "--height", "630", "--output", str(social_path.with_suffix(".png")), str(social_path)],
        check=True,
    )


if __name__ == "__main__":
    main()
