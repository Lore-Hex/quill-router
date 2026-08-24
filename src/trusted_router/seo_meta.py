from __future__ import annotations

SEO_TITLE_MAX_LENGTH = 60
SEO_DESCRIPTION_MAX_LENGTH = 160


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
    if not had_brand_suffix and " | " in base:
        return truncate_seo_text(base, SEO_TITLE_MAX_LENGTH)
    branded = f"{base}{brand}"
    if len(branded) <= SEO_TITLE_MAX_LENGTH:
        return branded
    if len(base) <= SEO_TITLE_MAX_LENGTH:
        return base
    return truncate_seo_text(base, SEO_TITLE_MAX_LENGTH)


def seo_meta_description(value: str) -> str:
    return truncate_seo_text(value, SEO_DESCRIPTION_MAX_LENGTH)
