"""Publish the same dated Token Index data and chart assets as TrustedRouter."""

from __future__ import annotations

import hashlib
import html
import runpy
import shutil
from pathlib import Path
from string import Template

from nyte_v2 import band_svg, render_nyte, uses_brand_v3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / "src/trusted_router"
CHART_ASSETS = ("series.json", "nyte-dark.png", "nyte-light.png", "grades-dark.png", "grades-light.png")
# Filled Ladder rungs per grade (kit Ladder: AAA 4, A 3, B 2, C 1).
RUNGS = {"AAA": 4, "A": 3, "B": 2, "C": 1}


def render_token_index() -> str:
    # This source module contains public literals only, with no application or database imports.
    data = runpy.run_path(str(SOURCE / "content/token_index.py"))["token_index_context"]()
    v3 = uses_brand_v3("index")
    sheet = "index-v3.css" if v3 else "index-page.css"
    version = hashlib.sha256(
        (HERE / sheet).read_bytes()
        + (SOURCE / "static/token-index.css").read_bytes()
        + (SOURCE / "static/token-index.js").read_bytes()
        + (SOURCE / "content/token_index.py").read_bytes()
        + (SOURCE / "static/token-index/series.json").read_bytes()
    ).hexdigest()[:12]
    escape = html.escape

    def change(value: float) -> str:
        return f"{value:+.1f}%" if abs(value) > 0.05 else "0.0%"

    tiles, cards, columns = [], [], []
    for grade in data["grades"]:
        code = escape(grade["code"])
        quote = data["quotes"][grade["code"]]
        tiles.append(
            f'<div class="ti-tile ti-{code.lower()}"><span class="ti-tile-name">'
            f'<span class="ti-code">{code}</span> {escape(grade["name"])}</span>'
            f'<span class="ti-tile-value">${quote["value"]:,}</span>'
            f'<span class="ti-tile-change">{change(quote["change_7d"])} (7D)</span>'
            f'<a class="btn btn-primary btn-sm" href="/buy?grade={code}&amp;unit=blocks">Reserve {code} tokens</a></div>'
        )
        examples = "".join(f"<li>{escape(model)}</li>" for model in grade["examples"])
        cards.append(
            f'<article class="ti-card ti-{code.lower()}"><h3><span class="ti-code">{code}</span>'
            f' {escape(grade["name"])}</h3><p>{escape(grade["use"])}</p>'
            f'<p class="ti-label">Example models</p><ul>{examples}</ul></article>'
        )
        rungs = "".join(
            '<i class="on"></i>' if rung <= RUNGS[grade["code"]] else "<i></i>" for rung in range(4, 0, -1)
        )
        columns.append(
            f'<article class="ti3-grade ti-{code.lower()}"><div class="ti3-ladder" aria-hidden="true">{rungs}</div>'
            f'<div class="ti3-grade-head"><span class="ti-code">{code}</span><h3>{escape(grade["name"])}</h3></div>'
            f'<p class="ti3-grade-price"><span class="ti-tile-value">${quote["value"]:,}</span>'
            f'<span class="ti-tile-change">{change(quote["change_7d"])} (7D)</span></p>'
            f'<p class="ti3-grade-use">{escape(grade["use"])}</p>'
            f'<div class="ti3-grade-models"><p class="ti-label">Example models</p><ul>{examples}</ul></div>'
            f'<a class="btn btn-secondary" href="/buy?grade={code}&amp;unit=blocks">Reserve {code} tokens</a></article>'
        )
    content = Template((HERE / ("index-v3.html" if v3 else "index-page.html")).read_text()).substitute(
        version=version, as_of=escape(data["as_of"]), as_of_iso=escape(data["as_of_iso"]),
        value=f'${data["quotes"]["ALL"]["value"]:,}',
        change=change(data["quotes"]["ALL"]["change_7d"]),
        nyte_alt=escape(data["chart_alt"]["nyte"], quote=True),
        grades_alt=escape(data["chart_alt"]["grades"], quote=True),
        tiles="".join(tiles), cards="".join(cards),
        grades="".join(columns), band=band_svg("band-yellow.svg") if v3 else "",
    )
    stylesheet = f'<link rel="stylesheet" href="/assets/{sheet}?v={version}">'
    return render_nyte(
        "index", content=content,
        image_url="https://nytokenexchange.com/assets/token-index/nyte-dark.png",
        extra_head=(
            f'<link rel="stylesheet" href="/assets/token-index.css?v={version}">'
            + ("" if v3 else stylesheet)
            + f'<script src="/assets/token-index.js?v={version}" defer></script>'
        ),
        # The v3 sheet loads after brand-v3.css, like the v3 home sheet.
        late_head=stylesheet if v3 else "",
    )



def build_token_index(output: Path) -> None:
    assets = output / "assets"
    charts = assets / "token-index"
    charts.mkdir(parents=True, exist_ok=True)
    for name in CHART_ASSETS:
        shutil.copyfile(SOURCE / "static/token-index" / name, charts / name)
    for name in ("token-index.css", "token-index.js"):
        shutil.copyfile(SOURCE / "static" / name, assets / name)
    sheet = "index-v3.css" if uses_brand_v3("index") else "index-page.css"
    shutil.copyfile(HERE / sheet, assets / sheet)
    # /index.html belongs to the existing homepage rewrite; only /index is the index page.
    (output / "index").write_text(render_token_index())
