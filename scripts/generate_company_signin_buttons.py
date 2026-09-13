#!/usr/bin/env python3
"""Self-contained SVG button artwork from the same copy as the developer guides."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trusted_router.content.company_signin import (  # noqa: E402
    COMPANY_SIGNIN_PAGES,
    company_signin_context,
)

STATIC = ROOT / "src" / "trusted_router" / "static"
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def render_button(slug: str, theme: str) -> bytes:
    context = company_signin_context(slug)
    dark = theme == "dark"
    background, ink, muted, border = (
        ("#101713", "#f5f7f6", "#b2c3b9", "#456252") if dark
        else ("#ffffff", "#15241b", "#4f6357", "#a8bcb0")
    )
    root = ET.Element(f"{{{SVG_NS}}}svg", {
        "width": "360", "height": "88", "viewBox": "0 0 360 88",
        "role": "img", "aria-labelledby": "label backing",
    })
    ET.SubElement(root, "title").text = str(context["button_label"])
    ET.SubElement(root, "desc").text = (
        "Company affiliation sign-in through TrustedRouter with Google authentication. "
        "Not official authentication by the named directory."
    )
    ET.SubElement(root, "rect", {
        "x": "0.5", "y": "0.5", "width": "359", "height": "87", "rx": "6",
        "fill": background, "stroke": border,
    })
    # Reuse the actual TrustedRouter mark, inline so the downloaded image has no dependencies.
    mark = ET.fromstring((STATIC / "favicon.svg").read_bytes())  # noqa: S314 - trusted local asset
    mark.attrib.update({"x": "16", "y": "20", "width": "48", "height": "48"})
    mark.attrib.pop("role", None)
    mark.attrib.pop("aria-label", None)
    root.append(mark)
    font = {"font-family": "Arial, Helvetica, sans-serif", "letter-spacing": "0"}
    ET.SubElement(root, "text", {
        **font, "id": "label", "x": "76", "y": "36", "fill": ink,
        "font-size": "20", "font-weight": "600",
    }).text = str(context["button_label"])
    ET.SubElement(root, "text", {
        **font, "id": "backing", "x": "76", "y": "59", "fill": muted, "font-size": "12",
    }).text = str(context["button_attribution"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for slug in COMPANY_SIGNIN_PAGES:
        for theme in ("light", "dark"):
            path = STATIC / "sign-in" / f"{slug.removeprefix('sign-in-as-')}-{theme}.svg"
            body = render_button(slug, theme)
            if args.check:
                if not path.is_file() or path.read_bytes() != body:
                    print(f"Outdated button: {path}", file=sys.stderr)
                    return 1
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
