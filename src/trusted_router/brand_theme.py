"""Site-wide visual theme switch for server-rendered pages.

Templates call ``brand_theme()`` to decide whether to tag ``<html>`` with
``data-brand="nyte"`` and load ``static/nyte-theme.css``. The value comes from
``TR_BRAND_THEME`` and is read once per process: rollout sets it as
config-as-code, so a change always arrives with a new revision.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from trusted_router.config import get_settings


@lru_cache(maxsize=1)
def brand_theme() -> str:
    return get_settings().brand_theme


_STATIC_OG = Path(__file__).parent / "static" / "og"
# Share images that have a NYTE twin under static/og/nyte/, by URL path. The
# homepage card and the site default are one design, so both map to og.png.
_NYTE_OG_TWINS = {
    "/og.png": "og.png",
    "/static/homepage/social-card-v2.jpg": "og.png",
}


def brand_og_image(url: str) -> str:
    """The share-image URL for the active theme.

    With the NYTE theme on, a card that has a twin in ``static/og/nyte/`` (the
    default card, the homepage card, and any ``static/og/<name>`` rendered
    again in the NYTE look) is swapped for it; everything else is unchanged,
    so a page never points at a missing image.
    """
    if brand_theme() != "nyte":
        return url
    parts = urlsplit(url)
    twin = _NYTE_OG_TWINS.get(parts.path)
    if twin is None and parts.path.startswith("/static/og/"):
        twin = parts.path.removeprefix("/static/og/")
    if twin is None or not (_STATIC_OG / "nyte" / twin).is_file():
        return url
    return urlunsplit(parts._replace(path=f"/static/og/nyte/{twin}"))
