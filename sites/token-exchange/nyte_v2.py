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

from facilities import FACILITY_RATE, facility_cards, facility_cards_v3

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
    "badge": {
        "route": "/badge",
        "label": None,
        "title": "NYTE Verified Secure badge | New York Token Exchange",
        "description": "NYTE requires confidential AI routes. Enterprise buyers display the Verified Secure badge to show customers the required privacy standard behind their NYTE-powered AI.",
        "group": "buy",
        "menu_description": "",
    },
    # Where a supplier's Verified Secure badge links (build.VERIFIED_PROVIDERS).
    "maple": {
        "route": "/verified/maple",
        "label": None,
        "title": "Maple attestation evidence | NYTE Verified Secure",
        "description": "Check Maple’s confidential AI for yourself. This page asks Maple’s enclave and NYTE’s gateway what they are running, verifies the hardware signatures in your browser, and compares them with the public records.",
        "og_title": "Maple, checked. By you, right now.",
        "og_description": "Live attestation evidence for Maple’s confidential AI, verified in your browser.",
        "group": "buy",
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
def buyer_deck_html() -> str:
    """The buyer deck dialog, included once per page."""
    return (SOURCE / "buyer-deck.html").read_text()


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


# Header menus (designer's Concept 1, real pages only): Products opens the buy group and
# Market the exchange group. Sell pages are listed in the footer under "For providers".
HEADER_MENUS = {"buy": "Products", "exchange": "Market"}
FOOTER_GROUPS = {"buy": "Products", "exchange": "Market", "sell": "For providers"}
# Kit colour for each menu item's bar, by page key; the group's colour otherwise.
MENU_COLOURS = {"api": "sky", "cowork": "lime", "buy": "yellow", "index": "yellow", "models": "purple", "deck": "sky"}
GROUP_COLOURS = {"buy": "lime", "exchange": "purple", "sell": "sky", "trust": "yellow"}
TR = "https://trustedrouter.com"
# TrustedRouter pages shown beside NYTE's own; they are linked, never rebuilt here.
MENU_EXTRAS = {"exchange": [("index", {"key": "models", "route": f"{TR}/models", "label": "Models & providers",
                                        "menu_description": "Every model on the exchange and who runs it"})]}
HEADER_LINKS = (("Trust", f"{TR}/trust"), ("Docs", f"{TR}/docs"), ("Pricing", f"{TR}/pricing"))
SIGN_IN = f"{TR}/console"
FOOTER_EXTRAS = {"buy": [("Pricing", f"{TR}/pricing")], "exchange": [("Exchange overview", f"{TR}/token-exchange")]}
FOOTER_TRUST = (("Trust & attestation", f"{TR}/trust"), ("Security", f"{TR}/security"),
                ("Docs", f"{TR}/docs"), ("Service status", f"{TR}/status"), ("Let's Encrypt AI", "https://letsencrypt.ai/"))
COPY_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="8" y="8" width="12" height="12"/>'
             '<path d="M16 8V4H4v12h4"/></svg>')


def menu_items(group: str) -> list[dict]:
    """A group's labelled pages in registry order, with TrustedRouter extras placed after their anchor."""
    items = []
    for key, entry in PAGES.items():
        if entry.get("group") != group or not entry["label"]:
            continue
        items.append({"key": key, **entry})
        items.extend(extra for after, extra in MENU_EXTRAS.get(group, ()) if after == key)
    return items


def render_nav() -> str:
    groups = []
    for group, label in HEADER_MENUS.items():
        items = []
        for entry in menu_items(group):
            key = entry["key"]
            colour = MENU_COLOURS.get(key, GROUP_COLOURS[group])
            added = "" if entry["route"].startswith("http") else page_added(entry.get("body", f"nyte-v2/{key}.html"))
            date_attribute = f' data-added="{added}"' if added else ""
            items.append(
                f'<a href="{html.escape(entry["route"], quote=True)}"{date_attribute} style="--item:var(--{colour})" '
                f'data-cta="nav_{key}" aria-labelledby="nav-label-{key}"><span class="nav-label" id="nav-label-{key}">'
                f'{html.escape(entry["label"])}</span> <span class="tag cc" hidden>NEW</span>'
                f'<span class="nav-description">{html.escape(entry["menu_description"])}</span></a>'
            )
        foot = ""
        if group == "buy":
            foot = (f'<div class="nav-foot"><button type="button" class="nav-prompt" data-cta="nav_agent_prompt">{COPY_ICON}'
                    '<span>Using an agent? <span class="nav-prompt-action">Copy its setup prompt</span></span></button>'
                    '<span class="nav-prompt-status" role="status"></span></div>')
        groups.append(
            f'<div class="nav-group" data-group="{group}" style="--group:var(--{GROUP_COLOURS[group]})">'
            f'<button type="button" class="nav-toggle" aria-expanded="false" aria-controls="nav-{group}">{label}'
            '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m3 4.5 3 3 3-3"/></svg></button>'
            f'<h2 class="nav-heading">{label}</h2><div class="nav-panel" id="nav-{group}"><div class="wrap">'
            f'<div class="nav-items">{"".join(items)}</div>{foot}</div></div></div>'
        )
    links = "".join(f'<a href="{href}" data-cta="nav_{text.lower()}">{text}</a>' for text, href in HEADER_LINKS)
    actions = '<div class="nav-actions"><a class="btn btn-primary" href="/confidential#start">Start building</a></div>'
    return "".join(groups) + f'<div class="nav-links">{links}</div>' + actions


def render_footer_links() -> str:
    """Footer columns: every labelled page by group, then TrustedRouter's trust pages."""
    columns = []
    for group, label in FOOTER_GROUPS.items():
        links = [(e["label"], e["route"]) for e in menu_items(group)] + list(FOOTER_EXTRAS.get(group, ()))
        columns.append((group, label, links))
    columns.append(("trust", "Trust", list(FOOTER_TRUST)))
    return "".join(
        f'<div class="footer-col" style="--group:var(--{GROUP_COLOURS[group]})"><h2>{label}</h2>'
        + "".join(f'<a href="{html.escape(href, quote=True)}">{html.escape(text, quote=False)}</a>' for text, href in links)
        + '</div>'
        for group, label, links in columns
    )


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


def index_panel_v3() -> str:
    """index_panel() for the brand v3 sell pages: grade chip, a Ladder filled 4/3/2/1, same snapshot."""
    data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))
    quotes = data["QUOTES"]
    as_of = date.fromisoformat(data["AS_OF_ISO"]).strftime("%m/%d/%y")

    def movement(code: str) -> str:
        value = quotes[code]["change_7d"]
        direction = "up" if value > 0.05 else "down" if value < -0.05 else "flat"
        label = f"{value:+.1f}%" if abs(value) > 0.05 else "0.0%"
        return f'<span class="idx-change {direction}" aria-label="{label} over seven days">{label} 7d</span>'

    grades = (
        ("AAA", "aaa", 4, "Frontier", "Frontier research, the hardest reasoning"),
        ("A", "a", 3, "Advanced", "Coding agents, complex analysis"),
        ("B", "b", 2, "Professional", "Production assistants, structured work"),
        ("C", "c", 1, "Efficient", "Extraction, summarization, routing"),
    )
    result = (
        '<div class="idx sv3-idx" aria-label="NYTE Token Index"><div class="sv3-idx-hd"><div>'
        '<b class="v3-label">NYTE Token Index</b><small>USD per billion tokens · As of '
        f'<time datetime="{html.escape(data["AS_OF_ISO"])}">{as_of}</time>'
        f'</small></div><span class="sv3-idx-value"><span class="sv3-idx-figure">${quotes["ALL"]["value"]:,}</span>{movement("ALL")}</span></div>'
        '<div class="sv3-idx-rows">'
    )
    for code, key, steps, name, description in grades:
        ladder = "".join('<i class="on"></i>' if n < steps else "<i></i>" for n in range(4))
        result += (
            f'<div class="sv3-idx-row g-{key}"><span class="v3-chip">{code}</span>'
            f'<span class="sv3-ladder" aria-hidden="true">{ladder}</span>'
            f'<div class="d"><b>{name}</b><small>{description}</small></div>'
            f'<span class="p">${quotes[code]["value"]:,}{movement(code)}</span></div>'
        )
    return result + '</div></div>'


def load_markets() -> list[dict]:
    return json.loads((HERE / "markets.json").read_text())


# Cities whose home page uses this design, with their own workload cards. Kept apart
# from markets.json, which also drives the load balancer's Terraform.
CITIES = json.loads((HERE / "nyte-cities.json").read_text())

# Registered pages use the NYTE brand v3 (nyte-v2/brand-v3.css) by default, so a
# new page gets it without any extra step. nyte-brand-v3.json lists the few pages
# that keep the old look. NYTE_BRAND_V3_PREVIEW=1 repaints every page in a local build.
OLD_BRAND_PAGES = set(json.loads((HERE / "nyte-brand-v3.json").read_text())["old_brand_pages"])
BRAND_V3_PAGES = set(PAGES) - OLD_BRAND_PAGES


def uses_brand_v3(page: str) -> bool:
    return os.environ.get("NYTE_BRAND_V3_PREVIEW") == "1" or page in BRAND_V3_PAGES


def legacy_share_image_used(market: dict) -> bool:
    """Whether any built page still points at the pre-v3 og-{slug}.png."""
    if not uses_nyte(market) or not (HERE / f"social-images/og-v3-{market['slug']}.png").exists():
        return True
    if market["slug"] == "new-york":
        # The media kit and the deck anchor are not shell pages and carry no share image.
        return not all(uses_brand_v3(page) for page, entry in PAGES.items() if entry.get("builder") not in {"press", "anchor"})
    return not uses_brand_v3("home")


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
        "u.searchParams.set('utm_source',location.hostname);u.searchParams.set('exchange_market',c.slug);a.href=u.href}})})})()</script>"
    )


def render_nyte(
    page: str = "home", *, content: str | None = None, extra_head: str = "",
    image_url: str | None = None, market: dict | None = None, late_head: str = "",
) -> str:
    if page == "badge" and not uses_brand_v3("badge"):
        # Rollback: the pre-v3 badge page is a standalone document with its own sheet.
        return (HERE / "badge.html").read_text()
    if page == "maple" and not uses_brand_v3("maple"):
        # Rollback: the pre-v3 evidence page is a standalone document (badge.css, verified.css).
        return (HERE / "verified-maple.html").read_text()
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
    cowork_v3 = page == "cowork" and uses_brand_v3("cowork")
    # Pages rebuilt for brand v3 keep their pre-v3 body and sheet for rollback.
    v3_body = page in V3_BODIES and uses_brand_v3(page)
    if (page == "providers" and not v3_body) or (page == "cowork" and not cowork_v3):
        version = hashlib.sha256((version + (SOURCE / f"{page}.css").read_text()).encode()).hexdigest()[:12]
        extra_head += '<link rel="stylesheet" href="/assets/nyte-v2/' + page + '.css?v=' + version + '">'
    if page == "accelerator" and not v3_body:
        accelerator_version = hashlib.sha256((SOURCE / "accelerator.css").read_bytes()).hexdigest()[:12]
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/accelerator.css?v={accelerator_version}">'
    if page == "manage":
        manage_version = hashlib.sha256((SOURCE / "manage.js").read_bytes()).hexdigest()[:12]
        extra_head += f'<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><script src="/assets/nyte-v2/manage.js?v={manage_version}" defer></script>'
    if page == "badge":
        badge_version = hashlib.sha256((SOURCE / "badge-v3.js").read_bytes()).hexdigest()[:12]
        extra_head += f'<script src="/assets/nyte-v2/badge-v3.js?v={badge_version}" defer></script>'
    if page == "maple":
        # verified.js runs the live checks and is published as written at /assets/verified.js.
        verified_version = hashlib.sha256((HERE / "verified.js").read_bytes()).hexdigest()[:12]
        maple_version = hashlib.sha256((SOURCE / "maple-v3.js").read_bytes()).hexdigest()[:12]
        # Marks the page as scripted before first paint, so the checks show their running state at once.
        extra_head += ('<script>document.documentElement.classList.add("mv3-js")</script>'
                       '<meta name="referrer" content="strict-origin-when-cross-origin">'
                       f'<script src="/assets/verified.js?v={verified_version}" defer></script>'
                       f'<script src="/assets/nyte-v2/maple-v3.js?v={maple_version}" defer></script>')
    if page == "buy":
        from buy_page import buy_content

        buy_v3 = uses_brand_v3("buy")
        content = buy_content(v3=buy_v3)
        buy_version = hashlib.sha256((SOURCE / "buy.css").read_bytes() + (SOURCE / "buy.js").read_bytes()).hexdigest()[:12]
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/buy.css?v={buy_version}"><script src="/assets/nyte-v2/buy.js?v={buy_version}" defer></script>'
        if buy_v3:
            # Its layout and sheet follow the switch, so removing buy restores the old page.
            content = content.replace("{{band_ink}}", band_svg("band-ink.svg"))
            buy_v3_version = hashlib.sha256((SOURCE / "buy-v3.css").read_bytes()).hexdigest()[:12]
            late_head = f'<link rel="stylesheet" href="/assets/nyte-v2/buy-v3.css?v={buy_v3_version}">' + late_head
    # New York's pages list no other markets; city home pages keep their footer rail.
    market_links = MARKET_RAIL.replace("{{links}}", "".join(
        '<a href="https://' + html.escape(other["domain"], quote=True) + '/"'
        + (' aria-current="page"' if other["slug"] == market["slug"] else '')
        + '>' + html.escape(other["region"]) + '</a>' for other in load_markets()
        # Forwarded markets 301 to New York, so the rail skips them (a city keeps its own entry).
        if "forward_to" not in other or other["slug"] == market["slug"]
    )) if city else ""
    home_v3 = page == "home" and uses_brand_v3("home")
    home_head = ""
    if content is None:
        body = f"{V3_BODIES[page]}.html" if v3_body else f"{page}.html"
        content = (SOURCE / body).read_text().replace("{{index_panel}}", index_panel())
    if v3_body:
        content = (content.replace("{{band_purple}}", band_svg("band-purple.svg"))
                   .replace("{{band_ink}}", band_svg("band-ink.svg"))
                   .replace("{{fails_closed}}", illustration_svg("fails-closed.svg"))
                   .replace("{{arrow}}", ARROW_UP_RIGHT)
                   .replace("{{band_sky}}", band_svg("band-sky.svg"))
                   .replace("{{band_lime}}", band_svg("band-lime.svg"))
                   .replace("{{index_panel_v3}}", index_panel_v3()))
        content = re.sub(r"\{\{illustration:([a-z-]+)\}\}", lambda m: illustration_svg(m[1] + ".svg"), content)
        sheet = V3_SHEETS.get(page, V3_BODIES[page]) + ".css"
        sheet_version = hashlib.sha256((SOURCE / sheet).read_bytes()).hexdigest()[:12]
        home_head = f'<link rel="stylesheet" href="/assets/nyte-v2/{sheet}?v={sheet_version}">'
    if city:
        content = city_home(content, market, v3=home_v3)
    if page == "accelerator":
        content = content.replace("{{facilities}}", facility_cards()).replace("{{facilities_v3}}", facility_cards_v3()).replace("{{facility_rate}}", FACILITY_RATE)
    if uses_brand_v3(page):
        brand_version = hashlib.sha256((SOURCE / "brand-v3.css").read_bytes()).hexdigest()[:12]
        # Light-only design: tells browsers with automatic dark mode (Safari 27.3, dark-mode extensions)
        # not to repaint the page; otherwise cream sections turn dark while the black press logos stay black.
        extra_head += '<meta name="color-scheme" content="only light">'
        extra_head += f'<link rel="stylesheet" href="/assets/nyte-v2/brand-v3.css?v={brand_version}">'
        extra_head += '<link rel="icon" href="/assets/nyte-v2/logos/favicon.svg" type="image/svg+xml">'
    # A rebuilt page's own sheet loads after brand-v3.css.
    extra_head += home_head + late_head
    result = (SOURCE / "shell.html").read_text()
    evidence_version = hashlib.sha256((HERE / "live-evidence.js").read_bytes()).hexdigest()[:12]
    for key, value in dict(domain=market["domain"], market=market["slug"], region=html.escape(market.get("logo_place", market["region"])), brand_class=" brand-one" if market.get("logo_one_line") else "", exchange_name=html.escape(market["name"]), host_brand="" if city or page == "home" else host_brand_script(page), nav=render_nav(), footer_links=render_footer_links(), sign_in=SIGN_IN, buyer_deck=buyer_deck_html(), extra_head=extra_head, image_url=html.escape(image_url, quote=True), title=html.escape(title), description=html.escape(description, quote=True), path=path, page=page, version=version, content=content, markets=market_links, evidence_script='<script src="/assets/nyte-v2/live-evidence.js?v=' + evidence_version + '" defer></script>' if page == "api" else "").items():
        result = result.replace("{{" + key + "}}", value)
    # A page may share a shorter line than its search title and description.
    for field in ("title", "description"):
        if f"og_{field}" in entry:
            tag = f'<meta property="og:{field}" content="{html.escape(entry["og_" + field], quote=True)}">'
            result = re.sub(rf'<meta property="og:{field}" content="[^"]*">', lambda _, tag=tag: tag, result, count=1)
    if page in {"providers", "badge"}:
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

# Footer rail linking every market, shown on city home pages only.
MARKET_RAIL = '<div class="geo-scroll footer-markets"><svg class="market-pin" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 10c0 6-8 11-8 11S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg><nav class="market-links" aria-label="Exchange markets">{{links}}</nav><button class="geo-prev" type="button" aria-label="Scroll footer markets left" hidden><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m15 5-7 7 7 7"/></svg></button><button class="geo-next" type="button" aria-label="Scroll footer markets right" hidden><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 5 7 7-7 7"/></svg></button></div>'

# Pages with a body rebuilt for brand v3: page key -> body and sheet name in nyte-v2/.
V3_BODIES = {"home": "home-v3", "api": "api-v3", "cowork": "cowork-v3",
             "sup": "sup-v3", "providers": "providers-v3", "accelerator": "accelerator-v3",
             "badge": "badge-v3", "maple": "maple-v3"}
# Rebuilt pages that share one sheet instead of <body>.css.
V3_SHEETS = {"sup": "sell-v3", "providers": "sell-v3", "accelerator": "sell-v3"}
# Kit icon arrow-up-right, drawn in the text colour.
ARROW_UP_RIGHT = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 19 L15.5 8.5"/><path d="M8 5 L19 5 L19 16"/></svg>'


def illustration_svg(name: str) -> str:
    """Inline a kit illustration; its own aria-label stays as the alt text. Slice keeps its centre when cropped."""
    svg = (SOURCE / name).read_text().strip()
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)
    return svg.replace("<svg ", '<svg class="v3-illustration" preserveAspectRatio="xMidYMid slice" focusable="false" ', 1)


def band_svg(name: str) -> str:
    """Inline a kit Band file, stretched across the page with its 24 px pitch kept (GFX-01)."""
    svg = (SOURCE / name).read_text().strip()
    svg = re.sub(r'\s(width|height|role|aria-label)="[^"]*"', "", svg, count=4)
    return svg.replace("<svg ", '<svg class="v3-band" preserveAspectRatio="none" aria-hidden="true" focusable="false" ', 1)


def city_home(content: str, market: dict, v3: bool = False) -> str:
    """New York's home page with the city's own workload cards.

    The routing example drops New York's US rule: a city name never implies where a route runs."""
    start = content.index("<!-- workloads -->")
    end = content.index("<!-- /workloads -->", start) + len("<!-- /workloads -->")
    tags = {"CONFIDENTIAL": "cc", "ZDR": "zdr"}
    def chips(card: dict) -> str:
        return '<div class="tags">' + "".join(f'<span class="tag {tags[t]}">{t}</span>' for t in card["tags"]) + "</div>"

    workloads = CITIES[market["slug"]]["workloads"]
    if v3:
        cards = "".join(
            f'<li><span class="n" aria-hidden="true">{n:02d}</span><h3>{html.escape(card["title"])}</h3>'
            f'<p class="q">“{html.escape(card["prompt"])}”</p>{chips(card)}</li>'
            for n, card in enumerate(workloads, 1)
        )
    else:
        cards = "".join(
            f'<div class="card"><h3>{html.escape(card["title"])}</h3><p class="q">“{html.escape(card["prompt"])}”</p>{chips(card)}</div>'
            for card in workloads
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
    for name in ("styles.css", "app.js", "fonts.css", "providers.css", "cowork.css", "buy.css", "buy.js", "manage.js", "accelerator.css", "brand-v3.css", "home-v3.css", "api-v3.css", "cowork-v3.css", "buy-v3.css", "sell-v3.css", "badge-v3.css", "badge-v3.js", "maple-v3.css", "maple-v3.js"):
        shutil.copyfile(SOURCE / name, assets / name)
    for page, entry in PAGES.items():
        if page == "home" or entry.get("builder"):
            continue
        route = entry["route"].lstrip("/")
        content = render_nyte(page)
        (output / route).parent.mkdir(parents=True, exist_ok=True)
        for name in (route, route + ".html"):
            (output / name).write_text(content)
