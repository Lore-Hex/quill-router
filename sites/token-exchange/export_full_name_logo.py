"""Compose the supplied guide outlines; requires rsvg-convert, never fonts.

Normal site builds use the checked-in SVG/PNG/PDF exports.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent / "press-assets"
SVG = "http://www.w3.org/2000/svg"


def main() -> None:
    renderer = shutil.which("rsvg-convert")
    if not renderer:
        raise SystemExit("Install librsvg (rsvg-convert) to regenerate these exports")
    ET.register_namespace("", SVG)
    root = ET.Element(f"{{{SVG}}}svg", {
        "viewBox": "0 0 1200 500", "role": "img",
        "aria-label": "NY Token Exchange logo with globe and full name",
    })
    for name, transform in (
        ("globe", "translate(385.1344645 20) scale(0.7)"),
        ("one-line-name", "translate(81.396955 330)"),
    ):
        # Preserve the supplied path geometry and ink color without retracing.
        source = ET.parse(HERE / "guide" / f"{name}.svg").getroot()  # noqa: S314
        group = ET.SubElement(root, f"{{{SVG}}}g", {"id": name, "transform": transform})
        group.extend(source)
    path = HERE / "nyte-full-name-logo.svg"
    ET.ElementTree(root).write(path, encoding="unicode")
    for extension, arguments in (("png", ["--width", "2400"]), ("pdf", ["--format", "pdf"])):
        subprocess.run(  # noqa: S603 - fixed local renderer and checked-in assets
            [renderer, *arguments, "--output", str(path.with_suffix(f".{extension}")), str(path)],
            check=True,
        )


if __name__ == "__main__":
    main()
