from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scripts.generate_provider_og import CARD_VERSION, generate
from tests.lifecycle_clock import LIFECYCLE_CLOCK_OVERRIDDEN
from trusted_router.catalog import PROVIDERS
from trusted_router.provider_branding import (
    PROVIDER_BRANDS,
    provider_homepage_url,
    provider_logo_url,
    provider_og_image_url,
)
from trusted_router.provider_og import all_provider_og_facts
from trusted_router.storage import STORE, ProviderBenchmarkSample

STATIC_DIR = Path(__file__).parents[1] / "src" / "trusted_router" / "static"

# Provider social cards embed live model and route COUNTS, so the committed
# cards match exactly one catalog: the one the real clock produces. Under a
# pinned future lifecycle clock they are stale by construction and `generate()`
# would rewrite the committed PNGs, so these two are the one thing the
# post-cutover job cannot assert. Cards are regenerated after a real cutover by
# the hourly refresh workflow -- see
# test_hourly_catalog_refresh_keeps_provider_cards_current below, which runs on
# both clocks because it reads the workflow rather than the catalog.
_needs_real_clock = pytest.mark.skipif(
    LIFECYCLE_CLOCK_OVERRIDDEN,
    reason="social cards embed live route counts; only the real clock's catalog matches",
)


def test_every_catalog_provider_has_local_branding() -> None:
    assert set(PROVIDER_BRANDS) == set(PROVIDERS)
    for slug in PROVIDERS:
        logo = STATIC_DIR / provider_logo_url(slug).removeprefix("/static/")
        assert logo.is_file(), slug
        assert logo.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), slug
        with Image.open(logo) as provider_logo:
            assert provider_logo.format == "PNG", slug
            assert min(provider_logo.size) >= 16, slug
        assert provider_homepage_url(slug)


def test_unknown_provider_logo_falls_back_locally() -> None:
    assert provider_logo_url("future-provider") == "/static/favicon.svg"
    assert provider_homepage_url("future-provider") is None


@_needs_real_clock
def test_every_provider_has_current_social_card() -> None:
    manifest_path = STATIC_DIR / "og" / "providers" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected = {
        facts.slug: {"card_version": CARD_VERSION, **facts.as_dict()}
        for facts in all_provider_og_facts()
    }
    assert manifest == expected
    for slug in PROVIDERS:
        card_path = STATIC_DIR / provider_og_image_url(slug).removeprefix("/static/")
        assert card_path.is_file(), slug
        with Image.open(card_path) as card:
            assert card.format == "PNG", slug
            assert card.size == (1200, 630), slug


def test_provider_social_cards_use_current_trustedrouter_mark() -> None:
    card_path = STATIC_DIR / "og" / "providers" / "trustedrouter.png"
    with Image.open(card_path) as card:
        header_mark = card.convert("RGB").crop((62, 54, 112, 104))
        color_counts = header_mark.getcolors(maxcolors=50 * 50)

    assert color_counts is not None
    colors = {color for _, color in color_counts}

    assert (237, 232, 219) in colors  # cream routing branches
    assert (169, 205, 185) in colors  # mint attested route


@_needs_real_clock
def test_provider_social_card_generation_is_idempotent() -> None:
    generated, unchanged = generate()

    assert generated == 0
    assert unchanged == len(PROVIDERS)


def test_hourly_catalog_refresh_keeps_provider_cards_current() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/refresh-prices.yml").read_text()

    assert "uv run python scripts/generate_provider_og.py" in workflow
    assert "src/trusted_router/static/og/providers/" in workflow


def test_provider_catalog_and_detail_render_local_logos(client: TestClient) -> None:
    catalog = client.get("/providers")
    detail = client.get("/providers/minimax")

    assert catalog.status_code == 200
    assert detail.status_code == 200
    assert "/static/provider-logos/minimax.png" in catalog.text
    assert "/static/provider-logos/minimax.png" in detail.text
    assert "https://www.minimax.io/" in detail.text
    image_sources = re.findall(r'<img[^>]+src="([^"]+)"', catalog.text)
    provider_sources = [source for source in image_sources if "provider-logos" in source]
    assert provider_sources
    assert all(source.startswith("/static/provider-logos/") for source in provider_sources)


def test_provider_detail_structured_data_describes_visible_provider(
    client: TestClient,
) -> None:
    response = client.get("/providers/minimax")
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    graph = payload["@graph"]
    page = next(node for node in graph if node.get("@type") == "WebPage")
    assert page["about"]["name"] == "MiniMax"
    assert page["about"]["logo"].endswith("/static/provider-logos/minimax.png")


def test_provider_pages_use_provider_specific_social_card(client: TestClient) -> None:
    for path in ("/providers/minimax", "/providers/minimax/performance"):
        response = client.get(path)
        assert response.status_code == 200
        assert (
            '<meta property="og:image" '
            'content="https://trustedrouter.com/static/og/providers/minimax.png">'
        ) in response.text
        assert (
            '<meta name="twitter:image" '
            'content="https://trustedrouter.com/static/og/providers/minimax.png">'
        ) in response.text


def test_model_pages_render_publisher_and_route_logos(client: TestClient) -> None:
    response = client.get("/models/minimax/minimax-m3")

    assert response.status_code == 200
    assert "/static/provider-logos/minimax.png" in response.text
    assert "provider-chip" in response.text


def test_leaderboard_rows_render_provider_logo(client: TestClient) -> None:
    STORE.record_provider_benchmark(
        ProviderBenchmarkSample(
            id="provider-logo-leaderboard",
            model="openai/gpt-5.5",
            provider="openai",
            provider_name="OpenAI",
            status="success",
            usage_type="Credits",
            streamed=True,
            first_token_milliseconds=120,
            source="synthetic",
        )
    )
    response = client.get("/leaderboard")

    assert response.status_code == 200
    assert "/static/provider-logos/openai.png" in response.text
