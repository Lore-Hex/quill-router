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


def tracked_url(url: str, market: dict, intent: str, fragment: str = "") -> str:
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
        + fragment
    )


def market_links(markets: list[dict], current: dict) -> str:
    return "".join(
        f'<a href="https://{html.escape(m["domain"], quote=True)}/"'
        + (' aria-current="page"' if m["slug"] == current["slug"] else "")
        + f">{html.escape(m['region'])}</a>"
        for m in markets
    )


def render(market: dict, markets: list[dict], version: str) -> str:
    catalogue, health = render_evidence()
    values = {
        key: html.escape(value, quote=True)
        for key, value in market.items()
        if isinstance(value, str)
    }
    values.update(
        {
            "version": version,
            "catalogue": catalogue,
            "regional_health": health if market["slug"] == "new-york" else "",
            "buyer_url": html.escape(
                tracked_url("https://calendly.com/joseph-perla/15min", market, "buyer"), quote=True
            ),
            "brief_url": html.escape(
                tracked_url(
                    "https://trustedrouter.com/token-exchange", market, "brief", "#enterprise-brief"
                ),
                quote=True,
            ),
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
        (HERE / "exchange.css").read_bytes() + (HERE / "exchange.js").read_bytes()
    ).hexdigest()[:12]
    for name in ("exchange.css", "exchange.js"):
        shutil.copyfile(HERE / name, assets / name)
    static = ROOT / "src/trusted_router/static"
    for source, target in {
        "enterprise/token-exchange-hero.webp": "exchange.webp",
        "fonts/archivo-latin.woff2": "archivo.woff2",
        "fonts/spectral-300-latin.woff2": "spectral.woff2",
        "fonts/ibm-plex-mono-400-latin.woff2": "plex.woff2",
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
        folder = output / market["slug"]
        folder.mkdir(exist_ok=True)
        (folder / "index.html").write_text(render(market, markets, version))
        (folder / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: https://{market['domain']}/sitemap.xml\n"
        )
        (folder / "sitemap.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://{market["domain"]}/</loc></url></urlset>'
        )
    print(
        f"Built {len(markets)} markets / {sum(1 + len(m['aliases']) for m in markets)} domains into {output}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
