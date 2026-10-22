"""New York's approved v2 design; other markets retain their shared template."""
from __future__ import annotations

import hashlib
import html
import json
import re
import runpy
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "nyte-v2"

def index_panel() -> str:
    data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))["token_index_context"]()
    def change(value):
        return f"{value:+.1f}%" if abs(value) > .05 else "0.0%"
    quote = data["quotes"]["ALL"]
    result = f'<div class="idx" aria-label="Dated NYTE Token Index"><div class="idx-hd"><div><b>NYTE Token Index</b><small>Per billion tokens · {html.escape(data["as_of"])}</small></div><div class="idx-big">${quote["value"]:,}<span class="chg">{change(quote["change_7d"])} 7d</span></div></div>'
    for grade in data["grades"]:
        quote = data["quotes"][grade["code"]]
        result += f'<div class="idx-row"><span class="g">{html.escape(grade["code"])}</span><div class="d"><b>{html.escape(grade["name"])}</b><small>{html.escape(grade["use"])}</small></div><span class="p">${quote["value"]:,}</span><span class="chg">{change(quote["change_7d"])}</span></div>'
    return result + '</div>'

def render_nyte(
    page: str = "home", *, content: str | None = None, extra_head: str = "",
    image_url: str | None = None,
) -> str:
    if image_url is None:
        image_version = hashlib.sha256((HERE / "social-images/og-new-york.png").read_bytes()).hexdigest()[:12]
        image_url = "https://nytokenexchange.com/assets/og-new-york.png?v=" + image_version
    path, title, description = {
        "home": ("/", "New York Token Exchange: enterprise AI from competing providers", "Every major model from competing providers, through one API. Set your privacy and price rules once, and check the proof yourself."),
        "api": ("/confidential", "Confidential API · New York Token Exchange", "Frontier AI for sensitive work. Confidential compute, end-to-end encryption and live evidence."),
        "sup": ("/suppliers", "Suppliers · New York Token Exchange", "Sell your inference to enterprise buyers. List your models and prices once."),
        "index": ("/index", "NYTE Token Index | New York Token Exchange", "Compare AI inference prices across four capability grades. List spare GPU capacity under your own brand on the New York Token Exchange."),
    }[page]
    version = hashlib.sha256((SOURCE / "styles.css").read_bytes() + (SOURCE / "app.js").read_bytes() + (HERE / "analytics.js").read_bytes()).hexdigest()[:12]
    markets = json.loads((HERE / "markets.json").read_text())
    market_links = "".join(
        '<a href="https://' + html.escape(market["domain"], quote=True) + '/"'
        + (' aria-current="page"' if market["slug"] == "new-york" else '')
        + '>' + html.escape(market["region"]) + '</a>' for market in markets
    )
    if content is None:
        content = (SOURCE / f"{page}.html").read_text().replace("{{index_panel}}", index_panel())
    result = (SOURCE / "shell.html").read_text()
    for key, value in dict(extra_head=extra_head, image_url=html.escape(image_url, quote=True), title=html.escape(title), description=html.escape(description, quote=True), path=path, page=page, version=version, content=content, markets=market_links, evidence_script='<script src="/assets/live-evidence.js?v=' + version + '" defer></script>' if page == "api" else "").items():
        result = result.replace("{{" + key + "}}", value)
    def tracked(match):
        url = urlsplit(html.unescape(match[1]))
        if url.hostname != "trustedrouter.com":
            return match[0]
        query = dict(parse_qsl(url.query))
        query.setdefault("utm_source", "nytokenexchange.com")
        query.setdefault("utm_medium", "referral")
        query.setdefault("utm_campaign", "token-exchange-launch")
        query.setdefault("utm_content", page)
        query.setdefault("exchange_market", "new-york")
        return 'href="' + html.escape(urlunsplit(url._replace(query=urlencode(query))), quote=True) + '"'
    return re.sub(r'href="([^"]+)"', tracked, result)

def build_nyte(output: Path) -> None:
    assets = output / "assets/nyte-v2"
    # HTML asset paths omit the CSS-relative assets/ segment.
    shutil.copytree(SOURCE / "assets", assets, dirs_exist_ok=True)
    for name in ("styles.css", "app.js", "fonts.css"):
        shutil.copyfile(SOURCE / name, assets / name)
    for page, route in (("api", "confidential"), ("sup", "suppliers")):
        content = render_nyte(page)
        for name in (route, route + ".html"):
            (output / name).write_text(content)
