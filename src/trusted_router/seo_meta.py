from __future__ import annotations

from urllib.parse import urlsplit

SEO_TITLE_MAX_LENGTH = 60
SEO_DESCRIPTION_MAX_LENGTH = 160


def provider_link_rel(url: str | None, blank: bool = False) -> str:
    """Only external web destinations in provider resource slots get nofollow."""
    rel = "noopener noreferrer" if blank else "noopener"
    if not url:
        return rel
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return rel
    if not host or parts.scheme not in {"", "http", "https"}:
        return rel
    domains = (
        "trustedrouter.com", "quillrouter.com", "allyrouter.com", "uptimerouter.com",
        "nytex.com", "nytokenexchange.com", "thetokenexchange.com",
    )
    if any(host == domain or host.endswith("." + domain) for domain in domains):
        return rel
    if host in {"github.com", "raw.githubusercontent.com"} and (
        parts.path.strip("/").split("/", 1)[0].lower() == "lore-hex"
    ):
        return rel
    return f"nofollow {rel}"


def truncate_seo_text(value: str, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    cutoff = max_length - 3
    shortened = normalized[:cutoff].rsplit(" ", 1)[0].rstrip(" ,:;|-")
    if not shortened:
        shortened = normalized[:cutoff].rstrip()
    return f"{shortened}..."


def seo_title(value: str) -> str:
    """Return one search title with at most one optional brand suffix."""
    brand = " | TrustedRouter"
    base = " ".join(value.split())
    had_brand_suffix = base.endswith(brand)
    while base.endswith(brand):
        base = base[: -len(brand)].rstrip()
    # No suffix when the title has its own " | " part or already names the
    # product ("TrustedRouter API Docs | TrustedRouter" would repeat it).
    if (not had_brand_suffix and " | " in base) or brand.removeprefix(" | ") in base:
        return truncate_seo_text(base, SEO_TITLE_MAX_LENGTH)
    branded = f"{base}{brand}"
    if len(branded) <= SEO_TITLE_MAX_LENGTH:
        return branded
    if len(base) <= SEO_TITLE_MAX_LENGTH:
        return base
    return truncate_seo_text(base, SEO_TITLE_MAX_LENGTH)


def seo_meta_description(value: str) -> str:
    return truncate_seo_text(value, SEO_DESCRIPTION_MAX_LENGTH)
