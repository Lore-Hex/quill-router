"""Site-wide visual theme switch for server-rendered pages.

Templates call ``brand_theme()`` to decide whether to tag ``<html>`` with
``data-brand="nyte"`` and load ``static/nyte-theme.css``. The value comes from
``TR_BRAND_THEME`` and is read once per process: rollout sets it as
config-as-code, so a change always arrives with a new revision.
"""

from __future__ import annotations

from functools import lru_cache

from trusted_router.config import get_settings


@lru_cache(maxsize=1)
def brand_theme() -> str:
    return get_settings().brand_theme
