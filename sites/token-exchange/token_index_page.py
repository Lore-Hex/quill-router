"""Publish the same dated Token Index data and chart assets as TrustedRouter."""

from __future__ import annotations

import hashlib
import html
import runpy
import shutil
from pathlib import Path
from string import Template

from nyte_v2 import render_nyte

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / "src/trusted_router"
CHART_ASSETS = ("series.json", "nyte-dark.png", "nyte-light.png", "grades-dark.png", "grades-light.png")


def render_token_index() -> str:
    # This source module contains public literals only, with no application or database imports.
    data = runpy.run_path(str(SOURCE / "content/token_index.py"))["token_index_context"]()
    version = hashlib.sha256(
        (HERE / "index-page.css").read_bytes()
        + (SOURCE / "static/token-index.css").read_bytes()
        + (SOURCE / "static/token-index.js").read_bytes()
        + (SOURCE / "content/token_index.py").read_bytes()
        + (SOURCE / "static/token-index/series.json").read_bytes()
    ).hexdigest()[:12]
    escape = html.escape

    def change(value: float) -> str:
        return f"{value:+.1f}%" if abs(value) > 0.05 else "0.0%"

    tiles, cards = [], []
    for grade in data["grades"]:
        code = escape(grade["code"])
        quote = data["quotes"][grade["code"]]
        tiles.append(
            f'<div class="ti-tile ti-{code.lower()}"><span class="ti-tile-name">'
            f'<span class="ti-code">{code}</span> {escape(grade["name"])}</span>'
            f'<span class="ti-tile-value">${quote["value"]:,}</span>'
            f'<span class="ti-tile-change">{change(quote["change_7d"])} (7D)</span></div>'
        )
        examples = "".join(f"<li>{escape(model)}</li>" for model in grade["examples"])
        cards.append(
            f'<article class="ti-card ti-{code.lower()}"><h3><span class="ti-code">{code}</span>'
            f' {escape(grade["name"])}</h3><p>{escape(grade["use"])}</p>'
            f'<p class="ti-label">Example models</p><ul>{examples}</ul></article>'
        )
    content = Template((HERE / "index-page.html").read_text()).substitute(
        version=version, as_of=escape(data["as_of"]), as_of_iso=escape(data["as_of_iso"]),
        value=f'${data["quotes"]["ALL"]["value"]:,}',
        change=change(data["quotes"]["ALL"]["change_7d"]),
        nyte_alt=escape(data["chart_alt"]["nyte"], quote=True),
        grades_alt=escape(data["chart_alt"]["grades"], quote=True),
        tiles="".join(tiles), cards="".join(cards),
    )
    return render_nyte(
        "index", content=content,
        image_url="https://nytokenexchange.com/assets/token-index/nyte-dark.png",
        extra_head=(
            f'<link rel="stylesheet" href="/assets/token-index.css?v={version}">'
            f'<link rel="stylesheet" href="/assets/index-page.css?v={version}">'
            f'<script src="/assets/token-index.js?v={version}" defer></script>'
        ),
    )



def build_token_index(output: Path) -> None:
    assets = output / "assets"
    charts = assets / "token-index"
    charts.mkdir(parents=True, exist_ok=True)
    for name in CHART_ASSETS:
        shutil.copyfile(SOURCE / "static/token-index" / name, charts / name)
    for name in ("token-index.css", "token-index.js"):
        shutil.copyfile(SOURCE / "static" / name, assets / name)
    shutil.copyfile(HERE / "index-page.css", assets / "index-page.css")
    # /index.html belongs to the existing homepage rewrite; only /index is the index page.
    (output / "index").write_text(render_token_index())
