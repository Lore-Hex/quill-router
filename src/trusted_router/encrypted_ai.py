"""The encrypted AI app directory: ranked by encrypted tokens anyone can verify.

Mechanic (OpenRouter's /rankings, kept): an app names itself with headers
on its own requests -- HTTP-Referer is its unique id, X-NYTE-Title (or
X-OpenRouter-Title / X-Title) its display name. No application, no review.
Unlike OpenRouter, the ranking counts only tokens served on Confidential
routes: attested gateway to attested provider hardware, the same evidence a
visitor can check at /trust and /docs/receipts.

Three kinds of listing:
- verified: observed on the exchange with encrypted tokens in the window.
  Its name and referer domain are self-reported headers, so observed traffic
  is never matched to a curated company: traffic that names one (by title or
  domain) is held out of the ranking unless an operator confirmed that
  company's claim (``confirmed`` in the listing data).
- self_attested / on_device: curated, the vendor's own claim, linked to the
  page where it can be checked.
- watchlist: curated, "Not verified" -- a statement about our evidence, never
  "not encrypted", which would be a claim about their stack.

Privacy: the snapshot carries app names, referer domains and token counts
only, never a workspace, key or prompt. ``hidden`` in the listing data drops
an app from the verified ranking (OpenRouter's opt-out); exchange-wide
totals still count its tokens.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL
from trusted_router.catalog_privacy import endpoint_privacy_tier

LISTINGS_PATH = Path(__file__).parent / "data" / "encrypted_ai_directory.json"

# (window name, label, snapshot field suffix). The builder sums each window.
WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("day", "Last 24 hours", "1"),
    ("week", "This week", "7"),
    ("month", "This month", "30"),
)
DEFAULT_WINDOW = "week"

# Gateway and first-party labels, not apps: the attested gateway stamps
# "attested-gateway" on every request that names no app.
_NOT_APPS = frozenset({
    "",
    "attested-gateway",
    "trustedrouter",
    "trustedrouter gateway",
    "trustedrouter synthetic",
    "trustedrouter batch",
    "trustedrouter mcp",
})

TIERS: tuple[tuple[str, str], ...] = (
    ("verified", "Verified encrypted"),
    ("self_attested", "Encrypted, self-attested"),
    ("on_device", "On-device or local-first"),
    ("watchlist", "Watchlist"),
)


def _domain(value: str) -> str:
    """Casefolded, without "www.": how titles, domains and the listing data
    are compared."""
    return value.strip().casefold().removeprefix("www.")


@dataclass(frozen=True)
class Listing:
    slug: str
    name: str
    url: str
    tier: str
    category: str
    description: str
    verify_url: str | None = None
    yc_batch: str | None = None
    # Titles and referer domains naming this company. Observed traffic that
    # carries one is held out of the ranking unless ``confirmed``.
    matches: tuple[str, ...] = ()
    # An operator confirmed the company owns its listed domains; only then
    # may traffic from those domains rank, under its listing.
    confirmed: bool = False


@dataclass(frozen=True)
class Directory:
    categories: tuple[tuple[str, str], ...]
    consumer_categories: frozenset[str]
    listings: tuple[Listing, ...]
    hidden: frozenset[str]


@cache
def directory() -> Directory:
    raw = json.loads(LISTINGS_PATH.read_text(encoding="utf-8"))
    tiers = {slug for slug, _label in TIERS}
    categories = tuple((str(c["slug"]), str(c["label"])) for c in raw["categories"])
    known = {slug for slug, _label in categories}
    listings = []
    for item in raw["listings"]:
        listing = Listing(
            slug=str(item["slug"]),
            name=str(item["name"]),
            url=str(item["url"]),
            tier=str(item["tier"]),
            category=str(item["category"]),
            description=str(item["description"]),
            verify_url=item.get("verify_url"),
            yc_batch=item.get("yc_batch"),
            # The company's own name and URL domain always count.
            matches=tuple(dict.fromkeys(_domain(str(m)) for m in (
                item["name"], urlparse(str(item["url"])).hostname or "", *item.get("matches", ()),
            ) if str(m).strip())),
            confirmed=bool(item.get("confirmed", False)),
        )
        if listing.tier not in tiers - {"verified"} or listing.category not in known:
            raise ValueError(f"encrypted AI listing {listing.slug}: bad tier or category")
        listings.append(listing)
    return Directory(
        categories=categories,
        consumer_categories=frozenset(str(c) for c in raw["consumer_categories"]),
        listings=tuple(listings),
        hidden=frozenset(_domain(str(h)) for h in raw.get("hidden", ())),
    )


@cache
def _confidential_providers() -> frozenset[str]:
    """Providers whose every catalog endpoint is Confidential or better.

    Used only for a model id the catalog no longer lists: a retired model on
    such a provider was still served on attested hardware.
    """
    tiers: dict[str, list[int]] = defaultdict(list)
    for model_id in MODELS:
        for endpoint in endpoints_for_model(model_id):
            tiers[endpoint.provider].append(endpoint_privacy_tier(endpoint))
    return frozenset(
        provider for provider, values in tiers.items()
        if values and min(values) >= PRIVACY_TIER_CONFIDENTIAL
    )


def is_confidential_route(provider: str, model: str) -> bool:
    if model in MODELS:
        return any(
            endpoint.provider == provider
            and endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_CONFIDENTIAL
            for endpoint in endpoints_for_model(model)
        )
    return provider in _confidential_providers()


def _domain_matches(domain: str, match: str) -> bool:
    """A referer domain belongs to a listed domain, or to a subdomain of it."""
    return "." in match and (domain == match or domain.endswith("." + match))


def _placement(directory_: Directory, title: str, domain: str) -> tuple[bool, Listing | None]:
    """Whether observed traffic may rank, and the listing it ranks under.

    Names and domains are self-reported, so traffic that names a curated
    company -- its domain (or a subdomain), its name or a listed spelling --
    is held out. The one exception is a confirmed company's own domain: that
    traffic ranks under its listing, and a title alone never attaches it.
    """
    title_key = _domain(title)
    by_domain = next(
        (listing for listing in directory_.listings
         if domain and any(_domain_matches(domain, match) for match in listing.matches)),
        None,
    )
    by_title = next(
        (listing for listing in directory_.listings if title_key and title_key in listing.matches),
        None,
    )
    if by_domain is not None:
        if by_domain.confirmed and by_title in (None, by_domain):
            return True, by_domain
        return False, None
    return by_title is None, None


def aggregate_encrypted_ai(rows: Iterable[Mapping[str, Any]], *, generated_at: str) -> dict[str, Any]:
    """Rank apps by encrypted tokens per window from builder rows.

    A row is one (app, referer domain, provider, model) with tokens and
    requests summed per window: ``t1/r1``, ``t7/r7``, ``t30/r30``. An app's
    identity is its referer domain when it sends one (OpenRouter's rule),
    else its title; its display name is its most-used title.
    """
    hidden = directory().hidden
    per_app: dict[str, dict[str, Any]] = {}
    exchange = {name: {"tokens": 0, "requests": 0} for name, _label, _suffix in WINDOWS}
    for row in rows:
        title = str(row.get("app") or "").strip()
        domain = _domain(str(row.get("ref") or ""))
        confidential = is_confidential_route(str(row.get("provider") or ""), str(row.get("model") or ""))
        if confidential:
            for name, _label, suffix in WINDOWS:
                exchange[name]["tokens"] += int(row.get(f"t{suffix}") or 0)
                exchange[name]["requests"] += int(row.get(f"r{suffix}") or 0)
        if title.casefold() in _NOT_APPS and not domain:
            continue
        if _domain(title) in hidden or (domain and domain in hidden):
            continue
        key = domain or title.casefold()
        app = per_app.setdefault(key, {
            "titles": defaultdict(int), "domain": domain, "categories": set(),
            **{f"{kind}{suffix}": 0 for _n, _l, suffix in WINDOWS for kind in ("enc_t", "enc_r", "all_t")},
        })
        if title and title.casefold() not in _NOT_APPS:
            app["titles"][title] += int(row.get("r30") or 0)
        app["categories"].update(str(c) for c in row.get("categories") or ())
        for _name, _label, suffix in WINDOWS:
            tokens = int(row.get(f"t{suffix}") or 0)
            app[f"all_t{suffix}"] += tokens
            if confidential:
                app[f"enc_t{suffix}"] += tokens
                app[f"enc_r{suffix}"] += int(row.get(f"r{suffix}") or 0)

    windows: dict[str, Any] = {}
    for name, label, suffix in WINDOWS:
        ranked = []
        for key, app in per_app.items():
            encrypted = app[f"enc_t{suffix}"]
            if encrypted <= 0:
                continue
            titles = app["titles"]
            display = max(titles, key=lambda t: (titles[t], t)) if titles else app["domain"] or key
            ranked.append({
                "key": key,
                "name": display,
                "domain": app["domain"],
                "categories": sorted(app["categories"]),
                "encrypted_tokens": encrypted,
                "encrypted_requests": app[f"enc_r{suffix}"],
                "encrypted_share": round(encrypted / app[f"all_t{suffix}"], 4) if app[f"all_t{suffix}"] else 1.0,
            })
        ranked.sort(key=lambda a: (-a["encrypted_tokens"], a["name"].casefold()))
        windows[name] = {
            "label": label,
            "apps": ranked,
            "exchange_encrypted_tokens": exchange[name]["tokens"],
            "exchange_encrypted_requests": exchange[name]["requests"],
        }
    return {"generated_at": generated_at, "windows": windows}


def directory_view(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Everything the page renders: the observed ranking per window, the
    curated tiers, and the headline counts.

    Observed traffic that names a curated company ranks only from that
    company's own domain and only once an operator confirmed its claim (see
    _placement); otherwise anyone could put a named company in the Verified
    table with two headers. A confirmed company leaves its curated tier.
    """
    directory_ = directory()
    labels = dict(directory_.categories)
    windows: list[dict[str, Any]] = []
    for name, label, _suffix in WINDOWS:
        window = ((snapshot or {}).get("windows") or {}).get(name) or {}
        apps: list[dict[str, Any]] = []
        for app in window.get("apps") or []:
            ranks, listing = _placement(directory_, str(app.get("name") or ""), str(app.get("domain") or ""))
            if not ranks:
                continue
            category = listing.category if listing else next(
                (c for c in app.get("categories") or () if c in labels), "other"
            )
            apps.append({
                **app,
                "rank": len(apps) + 1,
                "listing": listing,
                "category": category,
                "url": f"https://{app['domain']}" if app.get("domain") else None,
                "consumer": category in directory_.consumer_categories,
            })
        windows.append({
            "name": name,
            "label": label,
            "apps": apps,
            "consumer_count": sum(1 for a in apps if a["consumer"]),
            "exchange_encrypted_tokens": int(window.get("exchange_encrypted_tokens") or 0),
        })
    # A confirmed company is verified: its curated card would say otherwise.
    curated = {
        tier: [listing for listing in directory_.listings if listing.tier == tier and not listing.confirmed]
        for tier, _label in TIERS if tier != "verified"
    }
    return {
        "available": snapshot is not None,
        "generated_at": (snapshot or {}).get("generated_at"),
        "windows": windows,
        "default_window": DEFAULT_WINDOW,
        "tiers": dict(TIERS),
        "curated": curated,
        "categories": directory_.categories,
        "category_labels": labels,
    }
