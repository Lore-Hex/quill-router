"""Dashboard rendering. The page itself lives in templates/dashboard.html
with HTML/CSS/JS in their own files; this module only resolves
settings-driven values and renders the Jinja2 template."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from trusted_router.config import Settings
from trusted_router.og import OG_DESCRIPTION, OG_IMAGE_HEIGHT, OG_IMAGE_WIDTH, OG_TITLE
from trusted_router.regions import region_map_payload

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DEV_USER_FALLBACK = "alpha@trustedrouter.local"


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )


def dashboard_html(settings: Settings) -> str:
    domain = settings.trusted_domain
    environment = settings.environment.lower()
    tr_config = {
        "environment": environment,
        "defaultDevUser": "" if environment == "production" else DEV_USER_FALLBACK,
        "apiBaseUrl": settings.api_base_url,
        "stablecoinCheckoutEnabled": settings.stablecoin_checkout_enabled,
        "googleEnabled": settings.google_oauth_enabled,
        "githubEnabled": settings.github_oauth_enabled,
    }
    map_regions = region_map_payload(settings)
    return _env().get_template("dashboard.html").render(
        api_base_url=settings.api_base_url,
        site_url=f"https://{domain}/",
        og_image=f"https://{domain}/og.png",
        og_title=OG_TITLE,
        og_description=OG_DESCRIPTION,
        og_image_width=OG_IMAGE_WIDTH,
        og_image_height=OG_IMAGE_HEIGHT,
        tr_config=json.dumps(tr_config),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        map_regions=map_regions,
        primary_region=settings.primary_region,
    )
