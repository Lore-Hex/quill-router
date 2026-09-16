"""Small static documents share one shell; no account data is rendered here."""

from html import escape
from pathlib import Path

from fastapi.responses import HTMLResponse, Response

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = {"usage": "Usage", "pricing": "Pricing", "docs": "Docs", "terms": "Terms of Service", "privacy": "Privacy Policy"}
ORIGIN = "https://lightningrouter.ai"
DESCRIPTIONS = {
    "index": "Fund an AI API key with Bitcoin over Lightning. No email, password or card. Use USD credits with models and coding agents through TrustedRouter.",
    "docs": "Connect your LightningRouter key to TrustedRouter, OpenCode, Crush or OMP. Learn how Lightning funding, USD credits and gateway attestation work.",
    "pricing": "Compare AI model prices in USD and understand LightningRouter's Bitcoin conversion rate, 10% FX buffer, token costs and provider pricing.",
    "usage": "Check your LightningRouter USD credit balance and usage with your API key. Your account details stay private and are never indexed.",
    "terms": "Read the terms for funding AI API credits with Bitcoin over Lightning, including conversion, payment handling and responsible use.",
    "privacy": "Learn what LightningRouter processes for Lightning payments, API keys and support, and where TrustedRouter's inference privacy boundary applies.",
    "not-found": "This page could not be found. Find LightningRouter documentation, pricing, agent setup and support.",
}


def prefers_markdown(accept: str) -> bool:
    def quality(media_type: str) -> tuple[int, float]:
        matches = []
        for item in accept.lower().split(","):
            media, *parameters = item.strip().split(";")
            specificity = {media_type: 2, "text/*": 1, "*/*": 0}.get(media.strip())
            if specificity is None:
                continue
            weight = 1.0
            for parameter in parameters:
                key, _, value = parameter.strip().partition("=")
                if key == "q":
                    try:
                        weight = float(value.strip())
                    except ValueError:
                        weight = 0
            if not 0 <= weight <= 1:
                weight = 0
            matches.append((specificity, weight))
        return max(matches, default=(-1, 0.0))

    markdown = quality("text/markdown")
    html = quality("text/html")
    # Wildcard clients and browsers keep HTML; explicit Markdown can win a tie.
    return markdown[0] == 2 and markdown[1] > 0 and markdown[1] >= html[1]


def metadata(name: str) -> str:
    title = "LightningRouter | Pay with Bitcoin. Start building." if name == "index" else f"{PAGES.get(name, 'Page not found')} | LightningRouter"
    url = ORIGIN + ("/" if name == "index" else f"/{name}")
    description = DESCRIPTIONS[name]
    image = ORIGIN + "/assets/lightningrouter-og.jpg"
    robots = "noindex, nofollow" if name in {"usage", "not-found"} else "index, follow"
    fields = {
        "description": description, "robots": robots, "is-agentic-site-type": "app",
        "og:type": "website", "og:site_name": "LightningRouter", "og:title": title,
        "og:description": description, "og:url": url, "og:image": image,
        "og:image:type": "image/jpeg", "og:image:width": "1732", "og:image:height": "908",
        "og:image:alt": "LightningRouter's orange lightning mascot. Pay with Bitcoin. Start building. No email. No password. No card.",
        "twitter:card": "summary_large_image", "twitter:site": "@lightningrouter",
        "twitter:title": title, "twitter:description": description, "twitter:image": image,
        "twitter:image:alt": "LightningRouter: Pay with Bitcoin. Start building.",
    }
    tags = [f"<title>{escape(title)}</title>", f'<link rel="canonical" href="{url}">',
            '<link rel="service-desc" type="application/vnd.oai.openapi+json" href="/openapi.json">']
    for key, value in fields.items():
        attribute = "property" if key.startswith("og:") else "name"
        tags.append(f'<meta {attribute}="{key}" content="{escape(value, quote=True)}">')
    if name in {"index", "docs"}:
        tags.append(f'<link rel="alternate" type="text/markdown" href="/{name}.md">')
    return "\n  ".join(tags)


def public_page(name: str, *, accept: str = "", status_code: int = 200) -> Response:
    headers = {"Vary": "Accept", "Link": f'<{ORIGIN}/openapi.json>; rel="service-desc"; type="application/vnd.oai.openapi+json"'}
    if name in {"usage", "not-found"}:
        headers["X-Robots-Tag"] = "noindex"
    if name in {"index", "docs", "not-found"} and prefers_markdown(accept):
        return Response((WEB / f"{name}.md").read_text(), media_type="text/markdown", status_code=status_code, headers=headers)
    if name == "index":
        html = (WEB / "index.html").read_text()
    else:
        body = (WEB / f"{name}.html").read_text()
        html = (WEB / "page.html").read_text().replace("{{body}}", body)
    return HTMLResponse(html.replace("{{metadata}}", metadata(name)), status_code=status_code, headers=headers)
