"""New York's approved v2 design; other markets retain their shared template."""
from __future__ import annotations

import hashlib
import html
import json
import re
import runpy
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from facilities import FACILITY_RATE, facility_cards

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "nyte-v2"

def index_panel() -> str:
    """Use the same dated public snapshot as the full index page."""
    data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))
    quotes = data["QUOTES"]
    as_of = date.fromisoformat(data["AS_OF_ISO"]).strftime("%m/%d/%y")

    def movement(code: str) -> str:
        value = quotes[code]["change_7d"]
        direction = "up" if value > 0.05 else "down" if value < -0.05 else "flat"
        label = f"{value:+.1f}%" if abs(value) > 0.05 else "0.0%"
        return f'<span class="idx-change {direction}" aria-label="{label} over seven days">{label} 7d</span>'

    grades = (
        ("AAA", "Frontier", "Frontier research, the hardest reasoning"),
        ("A", "Advanced", "Coding agents, complex analysis"),
        ("B", "Professional", "Production assistants, structured work"),
        ("C", "Efficient", "Extraction, summarization, routing"),
    )
    result = (
        '<div class="idx" aria-label="NYTE Token Index"><div class="idx-hd"><div>'
        '<b>NYTE Token Index</b><small>USD per billion tokens · As of '
        f'<time datetime="{html.escape(data["AS_OF_ISO"])}">{as_of}</time>'
        f'</small></div><span class="idx-value">${quotes["ALL"]["value"]:,}{movement("ALL")}</span></div>'
    )
    for code, name, description in grades:
        value = quotes[code]["value"]
        result += f'<div class="idx-row"><span class="g">{code}</span><div class="d"><b>{name}</b><small>{description}</small></div><span class="p">${value:,}{movement(code)}</span></div>'
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
        "accelerator": ("/neocloud-accelerator", "Neocloud accelerator | New York Token Exchange", "Pre-sell reserved tokens from a planned facility to help finance the build. Explore the open-source neocloud stack and demand through NYTE."),
        "manage": ("/manage-reservation", "Manage my reservation | New York Token Exchange", "Manage your NYTE reservations with a private email link."),
        "reserved-terms": ("/reserved-tokens-terms", "Reserved tokens terms | New York Token Exchange", "Terms for prepaid NYTE Index-grade tokens usable during 2027."),
        "buy": ("/buy", "Reserved tokens | New York Token Exchange", "Reserve NYTE Index-grade tokens by card for January–December 2027, or discuss models, throughput and facilities on a 15-minute call."),
        "providers": ("/providers", "For inference providers | New York Token Exchange", "Bring your available inference capacity to the New York Token Exchange. Request the updated private seven-page provider deck and explore joining the exchange."),
        "cowork": ("/trusted-cowork", "Trusted Cowork · New York Token Exchange", "Trusted Cowork by NYTE. A desktop agent for your Mac using only trusted tokens: confidential inference, explicit tool approvals, and no standard-provider fallback."),
        "index": ("/index", "NYTE Token Index | New York Token Exchange", "Compare AI inference prices across four capability grades. List spare GPU capacity under your own brand on the New York Token Exchange."),
    }[page]
    version = hashlib.sha256((SOURCE / "styles.css").read_bytes() + (SOURCE / "app.js").read_bytes() + (HERE / "analytics.js").read_bytes()).hexdigest()[:12]
    if page in {"providers", "cowork"}:
        version = hashlib.sha256((version + (SOURCE / f"{page}.css").read_text()).encode()).hexdigest()[:12]
        extra_head += '<link rel="stylesheet" href="/assets/nyte-v2/' + page + '.css?v=' + version + '">'
    if page == "accelerator":
        accelerator_version = hashlib.sha256((SOURCE / "accelerator.css").read_bytes()).hexdigest()[:12]
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/accelerator.css?v={accelerator_version}">'
    if page == "manage":
        manage_version = hashlib.sha256((SOURCE / "manage.js").read_bytes()).hexdigest()[:12]
        extra_head += f'<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><script src="/assets/nyte-v2/manage.js?v={manage_version}" defer></script>'
    if page == "buy":
        from buy_page import buy_content

        content = buy_content()
        buy_version = hashlib.sha256((SOURCE / "buy.css").read_bytes() + (SOURCE / "buy.js").read_bytes()).hexdigest()[:12]
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/buy.css?v={buy_version}"><script src="/assets/nyte-v2/buy.js?v={buy_version}" defer></script>'
    markets = json.loads((HERE / "markets.json").read_text())
    market_links = "".join(
        '<a href="https://' + html.escape(market["domain"], quote=True) + '/"'
        + (' aria-current="page"' if market["slug"] == "new-york" else '')
        + '>' + html.escape(market["region"]) + '</a>' for market in markets
    )
    if content is None:
        content = (SOURCE / f"{page}.html").read_text().replace("{{index_panel}}", index_panel())
    if page == "accelerator":
        content = content.replace("{{facilities}}", facility_cards()).replace("{{facility_rate}}", FACILITY_RATE)
    result = (SOURCE / "shell.html").read_text()
    evidence_version = hashlib.sha256((HERE / "live-evidence.js").read_bytes()).hexdigest()[:12]
    for key, value in dict(buyer_deck=(SOURCE / "buyer-deck.html").read_text() if page in {"home", "api"} else "", extra_head=extra_head, image_url=html.escape(image_url, quote=True), title=html.escape(title), description=html.escape(description, quote=True), path=path, page=page, version=version, content=content, markets=market_links, evidence_script='<script src="/assets/nyte-v2/live-evidence.js?v=' + evidence_version + '" defer></script>' if page == "api" else "").items():
        result = result.replace("{{" + key + "}}", value)
    if page == "providers":
        result = result.replace('<div class="legal">', '<div class="legal"><a href="https://trustedrouter.com/trust">Trust center</a>')
    def tracked(match: re.Match[str]) -> str:
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
    # NYTE can publish its approved evidence renderer without changing other markets.
    shutil.copyfile(HERE / "live-evidence.js", assets / "live-evidence.js")
    for name in ("styles.css", "app.js", "fonts.css", "providers.css", "cowork.css", "buy.css", "buy.js", "manage.js", "accelerator.css"):
        shutil.copyfile(SOURCE / name, assets / name)
    for page, route in (("manage", "manage-reservation"), ("reserved-terms", "reserved-tokens-terms"), ("api", "confidential"), ("sup", "suppliers"), ("providers", "providers"), ("cowork", "trusted-cowork"), ("buy", "buy"), ("accelerator", "neocloud-accelerator")):
        content = render_nyte(page)
        for name in (route, route + ".html"):
            (output / name).write_text(content)
