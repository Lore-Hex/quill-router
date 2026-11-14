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


_NYTE_CARD = Path(__file__).parent / "static" / "og" / "nyte" / "og.png"
NYTE_CARD_ALT = "TrustedRouter: Encrypt all your AI chats"


def brand_og_image(url: str) -> str:
    """The share-image URL for the active theme.

    With the NYTE theme on, every page unfurls with the one NYTE card for now
    (per-page cards can follow); the host of the page's own card is kept.
    Without the card file nothing changes, so a page never points at a
    missing image.
    """
    if brand_theme() != "nyte" or not _NYTE_CARD.is_file():
        return url
    return urlunsplit(urlsplit(url)._replace(path="/static/og/nyte/og.png", query="", fragment=""))


def brand_og_alt(alt: str) -> str:
    """The share image's alt text, matching the card brand_og_image chose."""
    if brand_theme() != "nyte" or not _NYTE_CARD.is_file():
        return alt
    return NYTE_CARD_ALT
