#!/usr/bin/env python3
"""Build static regional sites. No application imports or database access."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
from pathlib import Path
from string import Template
from urllib.parse import urlencode

from evidence import render_evidence, sector_art

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load_markets() -> list[dict]:
    markets = json.loads((HERE / "markets.json").read_text())
    domains: set[str] = set()
    slugs: set[str] = set()
    for market in markets:
        assert re.fullmatch(r"[a-z][a-z-]+", market["slug"])
        assert market["slug"] not in slugs
        assert market["scope"] in {"global", "region", "city"}
        slugs.add(market["slug"])
        for domain in [market["domain"], *market["aliases"]]:
            assert re.fullmatch(r"[a-z0-9-]+\.[a-z]+", domain)
            assert domain not in domains
            domains.add(domain)
        assert len(market["sectors"]) == 3
    return markets


def tracked_url(url: str, market: dict, intent: str) -> str:
    return (
        url
        + "?"
        + urlencode(
            {
                "utm_source": market["domain"],
                "utm_medium": "referral",
                "utm_campaign": "token-exchange-launch",
                "utm_content": intent,
                "exchange_market": market["slug"],
            }
        )
    )


def market_links(markets: list[dict], current: dict) -> str:
    return "".join(
        f'<a href="https://{html.escape(m["domain"], quote=True)}/"'
        + (' aria-current="page"' if m["slug"] == current["slug"] else "")
        + f">{html.escape(m['region'])}</a>"
        for m in markets
    )


PROVIDERS = [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("mistral", "Mistral"),
    ("google-vertex", "Google Vertex AI"),
    ("deepseek", "DeepSeek"),
    ("grok", "xAI"),
]


def provider_ticker(copies: int = 2) -> str:
    """One visible provider group plus a hidden copy so the CSS marquee loops without a gap.

    The loop slides by one group; the row is at most 1024px wide and narrower than a group, so two groups cover it throughout."""
    groups = []
    for index in range(copies):
        link_attrs = ' tabindex="-1"' if index else ""
        group_attrs = ' aria-hidden="true"' if index else ""
        links = "".join(
            f'<a href="https://trustedrouter.com/providers"{link_attrs}>'
            f'<img src="/assets/provider-{slug}.png" width="32" height="32" alt=""><span>{html.escape(name)}</span></a>'
            for slug, name in PROVIDERS
        )
        groups.append(f'<div class="hero-provider-group"{group_attrs}>{links}</div>')
    return "".join(groups)


def render(market: dict, markets: list[dict], version: str) -> str:
    profile = {"new-york": "new-york", "dubai": "dubai", "europe": "europe"}.get(market["slug"], "shared-gcp")
    catalogue, health = render_evidence(profile)
    headline = html.escape(market["headline"])
    accent = html.escape(market.get("headline_accent", ""))
    if accent:
        headline = headline.replace(accent, f'<span class="headline-accent">{accent}</span>', 1)
    values = {
        key: html.escape(value, quote=True)
        for key, value in market.items()
        if isinstance(value, str)
    }
    values.update(
        {
            "version": version,
            "hero_headline": headline,
            "provider_ticker": provider_ticker(),
            "buyer_heading": html.escape(market.get("buyer_heading", "Buy capacity. Set your requirements.")).replace("Choose how it is served.", '<br>Choose how it is <span class="headline-accent">served.</span>').replace("Spend it", "Spend<br>it").replace("jurisdiction.", '<span class="headline-accent">jurisdiction.</span>').replace("Set your requirements.", '<br>Set your <span class="headline-accent">requirements.</span>'),
            "buyer_copy": html.escape(market.get("buyer_copy", "Compare model rates and provider privacy policies. Prioritize end-to-end encrypted routes where available, or review zero-retention options. Confirm processing locations and commercial terms for your workload.")),
            "catalogue": "",
            "regional_health": health.replace('<figure class="uptime-panel', catalogue + '<figure class="uptime-panel', 1),
            "seller_url": html.escape(
                tracked_url("https://trustedrouter.com/providers/marketplace", market, "seller"),
                quote=True,
            ),
            "sectors": "".join(
                f"<article>{sector_art(index)}<h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></article>"
                for index, (title, body) in enumerate(market["sectors"])
            ),
            "markets": market_links(markets, market),
            "schema": json.dumps(
                {
                    "@context": "https://schema.org",
                    "@type": "WebPage",
                    "name": market["name"],
                    "url": f"https://{market['domain']}/",
                    "description": market["lead"],
                    "publisher": {
                        "@type": "Organization",
                        "name": "Lore Hex Corp",
                        "url": "https://trustedrouter.com",
                    },
                }
            ).replace("<", "\\u003c"),
        }
    )
    return Template((HERE / "template.html").read_text()).substitute(values)


def build(output: Path) -> None:
    markets = load_markets()
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    version = hashlib.sha256(
        (HERE / "exchange.css").read_bytes() + (HERE / "exchange.js").read_bytes() + (HERE / "live-evidence.js").read_bytes()
    ).hexdigest()[:12]
    for name in ("exchange.css", "exchange.js", "live-evidence.js"):
        shutil.copyfile(HERE / name, assets / name)
    # Public request-only page. Never copy the confidential provider PDF or slides.
    for name in ("providers", "providers.html"):
        shutil.copyfile(HERE / "providers.html", output / name)
    for art in sorted((HERE / "art").glob("*.webp")):
        shutil.copyfile(art, assets / art.name)
    static = ROOT / "src/trusted_router/static"
    for source, target in {
        "fonts/archivo-latin.woff2": "archivo.woff2",
        "trustedrouter-mark-dark.svg": "mark.svg",
        "provider-logos/openai.png": "provider-openai.png",
        "provider-logos/anthropic.png": "provider-anthropic.png",
        "provider-logos/mistral.png": "provider-mistral.png",
        "provider-logos/google-vertex.png": "provider-google-vertex.png",
        "provider-logos/deepseek.png": "provider-deepseek.png",
        "provider-logos/grok.png": "provider-grok.png",
    }.items():
        shutil.copyfile(static / source, assets / target)
    for market in markets:
        image_name = f"og-{market['slug']}.png"
        shutil.copyfile(HERE / "social-images" / image_name, assets / image_name)
        folder = output / market["slug"]
        folder.mkdir(exist_ok=True)
        (folder / "index.html").write_text(render(market, markets, version))
        (folder / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: https://{market['domain']}/sitemap.xml\n"
        )
        provider_page = (
            '<url><loc>https://nytokenexchange.com/providers.html</loc></url>'
            if market["slug"] == "new-york" else ""
        )
        (folder / "sitemap.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://{market["domain"]}/</loc></url>{provider_page}</urlset>'
        )
    print(
        f"Built {len(markets)} markets / {sum(1 + len(m['aliases']) for m in markets)} domains into {output}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
