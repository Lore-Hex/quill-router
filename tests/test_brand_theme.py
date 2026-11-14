"""The site-wide NYTE theme switch: off by default, complete when on."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trusted_router.brand_theme import brand_theme

STATIC = Path(__file__).resolve().parents[1] / "src/trusted_router/static"
PAGES = ("/", "/pricing", "/models")


@pytest.fixture
def nyte_theme(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TR_BRAND_THEME", "nyte")
    brand_theme.cache_clear()
    yield
    brand_theme.cache_clear()


@pytest.mark.parametrize("path", PAGES)
def test_default_theme_renders_no_nyte_markup(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert 'data-brand="nyte"' not in html
    assert "nyte-theme.css" not in html
    assert "nyte-page.js" not in html


@pytest.mark.parametrize("path", PAGES)
def test_nyte_theme_tags_html_and_loads_its_sheet_last(
    client: TestClient, nyte_theme: None, path: str
) -> None:
    html = client.get(path).text
    assert re.search(r'<html[^>]*data-brand="nyte"', html)
    sheets = re.findall(r'<link rel="stylesheet" href="(/static/[^"?]+)', html)
    assert sheets[-1] == "/static/nyte-theme.css"
    assert '<script src="/static/nyte-page.js' in html.split("</head>")[0]


@pytest.mark.parametrize(
    ("family", "path"),
    [("price", "/pricing"), ("trust", "/trust"), ("knowledge", "/docs/quickstart")],
)
def test_page_family_script_covers_each_lead_colour(family: str, path: str) -> None:
    script = (STATIC / "nyte-page.js").read_text()
    assert f'["{family}", /' in script
    first_segment = path.strip("/").split("/")[0]
    assert re.search(rf"[(|]{re.escape(first_segment)}[|)]", script)


def _theme_defines() -> set[str]:
    return set(re.findall(r"(--[\w-]+)\s*:", (STATIC / "nyte-theme.css").read_text()))


def test_nyte_theme_overrides_every_shared_colour_token() -> None:
    tokens = set(re.findall(r"(--tr-color-[\w-]+)\s*:", (STATIC / "tokens.css").read_text()))
    assert tokens - _theme_defines() == set()


def test_nyte_theme_maps_every_legacy_colour_the_sheets_use() -> None:
    used: set[str] = set()
    for sheet in STATIC.glob("**/*.css"):
        used |= set(re.findall(r"var\((--tr-legacy-[0-9a-f-]+),", sheet.read_text()))
    templates = STATIC.parent / "templates"
    for page in templates.glob("**/*.html"):
        used |= set(re.findall(r"var\((--tr-legacy-[0-9a-f-]+),", page.read_text()))
    assert used, "expected the sheets to reference legacy colours"
    assert used - _theme_defines() == set()


def test_homepage_photos_only_render_in_the_default_theme(client: TestClient) -> None:
    assert client.get("/").text.count('class="landscape-picture"') == 2


def test_nyte_homepage_has_no_photo_heroes(client: TestClient, nyte_theme: None) -> None:
    html = client.get("/").text
    assert "landscape-picture" not in html
    assert "trustedrouter-landscape" not in html


def _og_images(html: str) -> set[str]:
    return set(re.findall(r'(?:og|twitter):image" content="([^"]+)"', html))


@pytest.mark.parametrize("path", ["/", "/pricing", "/models"])
def test_default_theme_keeps_its_share_images(client: TestClient, path: str) -> None:
    assert not any("/og/nyte/" in url for url in _og_images(client.get(path).text))


@pytest.mark.parametrize("path", ["/", "/models", "/pricing", "/trust", "/blog"])
def test_nyte_theme_uses_one_share_card_everywhere(client: TestClient, nyte_theme: None, path: str) -> None:
    html = client.get(path).text
    assert _og_images(html) == {"https://trustedrouter.com/static/og/nyte/og.png"}
    assert (STATIC / "og" / "nyte" / "og.png").is_file()
    head = html.split("</head>")[0]
    assert 'og:image:type" content="image/png"' in head
    assert "river" not in head
