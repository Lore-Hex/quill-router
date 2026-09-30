"""Render the decision-model post's measured scores into a 1200x630 bitmap."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs/blog-evidence/decision-models-2026-09-29.json"
OUTPUT = ROOT / "src/trusted_router/static/og/blog/trev-zev-lev-private-decisions.png"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument("--bold-font", required=True, type=Path)
    args = parser.parse_args()
    rows = json.loads(EVIDENCE.read_text())["models"]
    image = Image.new("RGB", (1200, 630), "#ffffff")
    draw = ImageDraw.Draw(image)

    def label(x: float, y: float, value: str, size: int, color: str, bold: bool = False) -> None:
        font = ImageFont.truetype(str(args.bold_font if bold else args.font), size)
        draw.text((x, y), value, font=font, fill=color)

    draw.rectangle((0, 0, 1200, 8), fill="#147d64")
    label(56, 35, "TrustedRouter", 26, "#147d64", True)
    label(56, 85, "Trev, Zev and Lev", 54, "#17201c", True)
    label(56, 152, "Decision models with ZDR routing", 30, "#46524c")
    label(56, 209, "PUBLIC JEVBENCH PASS RATE", 19, "#59645f", True)
    label(815, 200, "MODEL COST", 17, "#59645f", True)
    label(815, 223, "per 1,000 runs", 16, "#59645f")
    label(1005, 200, "MEAN EVAL", 17, "#59645f", True)
    label(1005, 223, "elapsed time", 16, "#59645f")
    colors = ["#87918c", "#147d64", "#316ba6", "#aa6724"]
    x0, width = 230, 410
    for i, (row, color) in enumerate(zip(rows, colors, strict=True)):
        y = 255 + i * 62
        score = 100 * row["passed"] / row["problems"]
        label(56, y + 2, row["label"], 27, "#17201c", True)
        draw.rectangle((x0, y, x0 + width, y + 34), fill="#f1f3f2")
        draw.rectangle((x0, y, x0 + width * score / 100, y + 34), fill=color)
        label(665, y, f"{score:.1f}%", 29, "#17201c", True)
        cost = Decimal(row["mean_model_cost_usd"]) * 1000
        label(815, y + 2, f"${cost:.3f}", 26, "#17201c")
        label(1005, y + 2, f'{row["mean_eval_elapsed_seconds"]:.1f}s', 26, "#17201c")
    label(x0, 505, "0%", 19, "#59645f")
    label(x0 + width - 45, 505, "100%", 19, "#59645f")
    label(56, 545, "231 public tasks | AnyEval | 29 September 2026", 20, "#46524c")
    label(56, 575, "Elapsed time includes the eval harness. Cost excludes safety monitors.", 19, "#46524c")
    label(56, 602, "ZDR requires a provider filter; these scores are not a separate ZDR eval.", 19, "#46524c")
    image.save(OUTPUT, optimize=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
