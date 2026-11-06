"""New York's approved v2 design; other markets retain their shared template."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import runpy
import shutil
import subprocess
from datetime import date
from functools import cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from facilities import FACILITY_RATE, facility_cards

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "nyte-v2"


GROUPS = {"buy": "Buy", "sell": "Sell", "exchange": "Exchange"}
GROUP_GUIDANCE = "\n".join((
    "# Who is this page for?",
    "#   buying tokens     -> buy",
    "#   selling capacity  -> sell",
    "#   anything else     -> exchange",
))

# Who is this page for?
#   buying tokens     -> buy
#   selling capacity  -> sell
#   anything else     -> exchange
PAGES = {
    "home": {
        "route": "/",
        "label": "Home",
        "title": "New York Token Exchange: enterprise AI from competing providers",
        "description": "Every major model from competing providers, through one API. Set your privacy and price rules once, and check the proof yourself.",
        "group": None,
        "menu_description": "",
    },
    "api": {
        "route": "/confidential",
        "label": "Confidential API",
        "title": "Confidential API · New York Token Exchange",
        "description": "Frontier AI for sensitive work. Confidential compute, end-to-end encryption and live evidence.",
        "group": "buy",
        "menu_description": "Frontier AI for sensitive work",
    },
    "cowork": {
        "route": "/trusted-cowork",
        "label": "Trusted Cowork",
        "title": "Trusted Cowork · New York Token Exchange",
        "description": "Trusted Cowork by NYTE. A desktop agent for your Mac using only trusted tokens: confidential inference, explicit tool approvals, and no standard-provider fallback.",
        "group": "buy",
        "menu_description": "A desktop agent for sensitive work",
    },
    "buy": {
        "route": "/buy",
        "label": "Reserved tokens",
        "title": "Reserved tokens | New York Token Exchange",
        "description": "Reserve NYTE Index-grade tokens by card for January–December 2027, or discuss models, throughput and facilities on a 15-minute call.",
        "group": "buy",
        "menu_description": "Reserve your next billion tokens",
    },
    "sup": {
        "route": "/suppliers",
        "label": "Suppliers",
        "title": "Suppliers · New York Token Exchange",
        "description": "Sell your inference to enterprise buyers. List your models and prices once.",
        "group": "sell",
        "menu_description": "Sell your inference to enterprise buyers",
    },
    "providers": {
        "route": "/providers",
        "label": "Providers",
        "title": "For inference providers | New York Token Exchange",
        "description": "Bring your available inference capacity to the New York Token Exchange. Request the updated private seven-page provider deck and explore joining the exchange.",
        "group": "sell",
        "menu_description": "More demand, same GPUs",
    },
    "accelerator": {
        "route": "/neocloud-accelerator",
        "label": "Neocloud accelerator",
        "title": "Neocloud accelerator | New York Token Exchange",
        "description": "Pre-sell reserved tokens from a planned facility to help finance the build. Explore the open-source neocloud stack and demand through NYTE.",
        "group": "sell",
        "menu_description": "Pre-sell tokens, build your neocloud",
    },
    "index": {
        "route": "/index",
        "label": "Token Index",
        "title": "NYTE Token Index | New York Token Exchange",
        "description": "Compare AI inference prices across four capability grades. List spare GPU capacity under your own brand on the New York Token Exchange.",
        "group": "exchange",
        "menu_description": "Compare inference prices by grade",
        "builder": "index",
        "body": "index-page.html",
    },
    "reserved-terms": {
        "route": "/reserved-tokens-terms",
        "label": None,
        "title": "Reserved tokens terms | New York Token Exchange",
        "description": "Terms for prepaid NYTE Index-grade tokens usable during 2027.",
        "group": "exchange",
        "menu_description": "",
    },
    "manage": {
        "route": "/manage-reservation",
        "label": None,
        "title": "Manage my reservation | New York Token Exchange",
        "description": "Manage your NYTE reservations with a private email link.",
        "group": "exchange",
        "menu_description": "",
    },
    "media-kit": {
        "route": "/media-kit",
        "label": None,
        "group": "exchange",
        "builder": "press",
    },
    "deck": {
        "route": "/#deck",
        "label": "Buyer deck",
        "group": "exchange",
        "menu_description": "The October buyer deck",
        "builder": "anchor",
        "body": "nyte-v2/buyer-deck.html",
    },
}


@cache
def page_added(source: str) -> str:
    """Use a recorded creation date only when full Git history is available."""
    git = shutil.which("git")
    if git is None:
        return ""
    # The executable is resolved locally; arguments are fixed and paths follow --.
    shallow = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--is-shallow-repository"], cwd=HERE,
        capture_output=True, text=True,
    )
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        return ""
    history = subprocess.run(  # noqa: S603
        [git, "log", "--diff-filter=A", "--format=%cs", "--", source], cwd=HERE,
        capture_output=True, text=True,
    )
    if history.returncode != 0:
        return ""
    dates = history.stdout.splitlines()
    return dates[-1] if dates else ""


def render_nav() -> str:
    groups = []
    for group, label in GROUPS.items():
        items = []
        for key, entry in PAGES.items():
            if entry.get("group") != group or not entry["label"]:
                continue
            added = page_added(entry.get("body", f"nyte-v2/{key}.html"))
            date_attribute = f' data-added="{added}"' if added else ""
            items.append(
                f'<a href="{html.escape(entry["route"], quote=True)}"{date_attribute} '
                f'aria-labelledby="nav-label-{key}"><span class="nav-label" id="nav-label-{key}">'
                f'{html.escape(entry["label"])}</span> <span class="tag cc" hidden>NEW</span>'
                f'<span class="nav-description">{html.escape(entry["menu_description"])}</span></a>'
            )
        groups.append(
            f'<div class="nav-group" data-group="{group}">'
            f'<button type="button" class="nav-toggle" aria-expanded="false" aria-controls="nav-{group}">{label}'
            '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m3 4.5 3 3 3-3"/></svg></button>'
            f'<h2 class="nav-heading">{label}</h2><div class="nav-panel" id="nav-{group}">' + ''.join(items) + '</div></div>'
        )
    return ''.join(groups)

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

def load_markets() -> list[dict]:
    return json.loads((HERE / "markets.json").read_text())


# Cities whose home page uses this design, with their own workload cards. Kept apart
# from markets.json, which also drives the load balancer's Terraform.
CITIES = json.loads((HERE / "nyte-cities.json").read_text())

# Pages repainted with the NYTE brand v3 (nyte-v2/brand-v3.css). Empty means
# production is unchanged. NYTE_BRAND_V3_PREVIEW=1 repaints every page in a local build.
BRAND_V3_PAGES = set(json.loads((HERE / "nyte-brand-v3.json").read_text())["pages"])


def uses_brand_v3(page: str) -> bool:
    return os.environ.get("NYTE_BRAND_V3_PREVIEW") == "1" or page in BRAND_V3_PAGES


def brand_v3_share_image_used(slug: str) -> bool:
    """New York's image is shared by all its pages; a city's only by its home page."""
    if slug == "new-york":
        return any(uses_brand_v3(page) for page in PAGES)
    return uses_brand_v3("home")


def uses_nyte(market: dict) -> bool:
    """Whether a market's home page is rendered from this design."""
    return market["slug"] == "new-york" or market["slug"] in CITIES


def nyte_markets() -> list[dict]:
    """Markets other than New York whose home page uses this design."""
    return [market | CITIES[market["slug"]] for market in load_markets() if market["slug"] in CITIES]


def host_brand_script(page: str) -> str:
    """Rename a shared page for visitors who reached it on a city's own domain.

    Every path other than / on a city domain serves New York's pages. Those visitors keep
    their city's name and attribution. Checkout and its terms add "by NYTE", since the receipt
    and contract name NYTE as the seller. Reservations are managed on New York's domain,
    the only origin the reservations API accepts."""
    hosts = {
        m["domain"]: {
            "slug": m["slug"], "name": m["name"], "logo": m.get("logo_place", m["region"]),
            "one": bool(m.get("logo_one_line")), "faq": m.get("faq_place", m["region"]), "us": bool(m.get("us")),
        }
        for m in nyte_markets()
    }
    if not hosts:
        return ""
    data = json.dumps(hosts, separators=(",", ":")).replace("<", "\\u003c")
    if page == "manage":
        return ("<script>(()=>{if(" + data + "[location.hostname])location.replace("
                "'https://nytokenexchange.com'+location.pathname+location.search+location.hash)})()</script>")
    names_seller = page in {"buy", "reserved-terms"}
    return (
        "<script>(()=>{const c=" + data + "[location.hostname];if(!c)return;document.body.dataset.market=c.slug;"
        "const b=document.querySelector('.hdr .brand');b.setAttribute('aria-label',c.name+' home');"
        "b.querySelector('.brand-place').textContent=c.logo;if(c.one)b.classList.add('brand-one');"
        + ("const by=document.createElement('small');by.className='brand-by';by.textContent='by NYTE';"
           "b.querySelector('.brand-line').after(by);"
           "document.documentElement.dataset.billedBy='New York Token Exchange (NYTE)';" if names_seller else "")
        + "document.addEventListener('DOMContentLoaded',()=>{const n=c.name" + ("+' · by NYTE'" if names_seller else "") + ";"
        "document.title=document.title.replace('New York Token Exchange',n);"
        "document.querySelectorAll('[data-exchange-name]').forEach(e=>{e.textContent=n});"
        "const f=document.querySelector('.site-footer .brand>span');if(f&&f.firstChild)f.firstChild.textContent=n;"
        # The data-location FAQ names the visitor's city; an empty place drops it.
        "const q=document.getElementById('place-faq');if(q){if(!c.faq)q.remove();else{q.querySelectorAll('[data-place]').forEach(e=>{e.textContent=c.faq});"
        "if(!c.us)q.querySelector('[data-us-only]').remove()}}"
        "document.querySelectorAll('a[href]').forEach(a=>{const u=new URL(a.href);"
        "if(u.pathname==='/manage-reservation'&&u.hostname===location.hostname){a.href='https://nytokenexchange.com/manage-reservation';return}"
        "if(u.hostname==='trustedrouter.com'&&u.searchParams.get('exchange_market')==='new-york'){"
        "u.searchParams.set('utm_source',location.hostname);u.searchParams.set('exchange_market',c.slug);a.href=u.href}});"
        "document.querySelectorAll('.market-links a').forEach(a=>{if(new URL(a.href).hostname===location.hostname)"
        "a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')})})})()</script>"
    )


def render_nyte(
    page: str = "home", *, content: str | None = None, extra_head: str = "",
    image_url: str | None = None, market: dict | None = None,
) -> str:
    market = market or next(m for m in load_markets() if m["slug"] == "new-york")
    market = market | CITIES.get(market["slug"], {})
    city = market["slug"] != "new-york"
    if city and page != "home":
        raise ValueError("City domains serve New York's shared pages; only their home page is rendered per city.")
    if image_url is None:
        image_name = f"og-{market['slug']}.png"
        if uses_brand_v3(page) and (HERE / f"social-images/og-v3-{market['slug']}.png").exists():
            image_name = f"og-v3-{market['slug']}.png"
        image_version = hashlib.sha256((HERE / "social-images" / image_name).read_bytes()).hexdigest()[:12]
        image_url = f"https://{market['domain']}/assets/{image_name}?v=" + image_version
    entry = PAGES[page]
    path, title, description = entry["route"], entry["title"], entry["description"]
    if city:
        title = f"{market['name']}: enterprise AI from competing providers"
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
    market_links = "".join(
        '<a href="https://' + html.escape(other["domain"], quote=True) + '/"'
        + (' aria-current="page"' if other["slug"] == market["slug"] else '')
        + '>' + html.escape(other["region"]) + '</a>' for other in load_markets()
    )
    if content is None:
        content = (SOURCE / f"{page}.html").read_text().replace("{{index_panel}}", index_panel())
    if city:
        content = city_home(content, market)
    if page == "accelerator":
        content = content.replace("{{facilities}}", facility_cards()).replace("{{facility_rate}}", FACILITY_RATE)
    if uses_brand_v3(page):
        brand_version = hashlib.sha256((SOURCE / "brand-v3.css").read_bytes()).hexdigest()[:12]
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/brand-v3.css?v={brand_version}">'
        extra_head += '<link rel="icon" href="/assets/nyte-v2/logos/favicon.svg" type="image/svg+xml">'
    result = (SOURCE / "shell.html").read_text()
    evidence_version = hashlib.sha256((HERE / "live-evidence.js").read_bytes()).hexdigest()[:12]
    for key, value in dict(domain=market["domain"], market=market["slug"], region=html.escape(market.get("logo_place", market["region"])), brand_class=" brand-one" if market.get("logo_one_line") else "", exchange_name=html.escape(market["name"]), host_brand="" if city or page == "home" else host_brand_script(page), nav=render_nav(), buyer_deck=(SOURCE / "buyer-deck.html").read_text() if page in {"home", "api"} else "", extra_head=extra_head, image_url=html.escape(image_url, quote=True), title=html.escape(title), description=html.escape(description, quote=True), path=path, page=page, version=version, content=content, markets=market_links, evidence_script='<script src="/assets/nyte-v2/live-evidence.js?v=' + evidence_version + '" defer></script>' if page == "api" else "").items():
        result = result.replace("{{" + key + "}}", value)
    if page == "providers":
        result = result.replace('<div class="legal">', '<div class="legal"><a href="https://trustedrouter.com/trust">Trust center</a>')
    def tracked(match: re.Match[str]) -> str:
        url = urlsplit(html.unescape(match[1]))
        if url.hostname != "trustedrouter.com":
            return match[0]
        query = dict(parse_qsl(url.query))
        query.setdefault("utm_source", market["domain"])
        query.setdefault("utm_medium", "referral")
        query.setdefault("utm_campaign", "token-exchange-launch")
        query.setdefault("utm_content", page)
        query.setdefault("exchange_market", market["slug"])
        return 'href="' + html.escape(urlunsplit(url._replace(query=urlencode(query))), quote=True) + '"'
    return re.sub(r'href="([^"]+)"', tracked, result)

def city_home(content: str, market: dict) -> str:
    """New York's home page with the city's own workload cards.

    The routing example drops New York's US rule: a city name never implies where a route runs."""
    start = content.index("<!-- workloads -->")
    end = content.index("<!-- /workloads -->", start) + len("<!-- /workloads -->")
    tags = {"CONFIDENTIAL": "cc", "ZDR": "zdr"}
    cards = "".join(
        f'<div class="card"><h3>{html.escape(card["title"])}</h3><p class="q">“{html.escape(card["prompt"])}”</p>'
        '<div class="tags">' + "".join(f'<span class="tag {tags[t]}">{t}</span>' for t in card["tags"]) + "</div></div>"
        for card in CITIES[market["slug"]]["workloads"]
    )
    content = content[:start] + cards + content[end:]
    return content.replace('<span class="tag us">US</span>', "")


def build_nyte(output: Path) -> None:
    for key, entry in PAGES.items():
        if key != "home" and entry.get("group") not in GROUPS:
            raise ValueError(GROUP_GUIDANCE + "\n" + key)
    assets = output / "assets/nyte-v2"
    # HTML asset paths omit the CSS-relative assets/ segment.
    shutil.copytree(SOURCE / "assets", assets, dirs_exist_ok=True)
    # NYTE can publish its approved evidence renderer without changing other markets.
    shutil.copyfile(HERE / "live-evidence.js", assets / "live-evidence.js")
    for name in ("styles.css", "app.js", "fonts.css", "providers.css", "cowork.css", "buy.css", "buy.js", "manage.js", "accelerator.css", "brand-v3.css"):
        shutil.copyfile(SOURCE / name, assets / name)
    for page, entry in PAGES.items():
        if page == "home" or entry.get("builder"):
            continue
        route = entry["route"].lstrip("/")
        content = render_nyte(page)
        for name in (route, route + ".html"):
            (output / name).write_text(content)
