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


@pytest.mark.parametrize("path", PAGES)
def test_nyte_theme_tags_html_and_loads_its_sheet_last(
    client: TestClient, nyte_theme: None, path: str
) -> None:
    html = client.get(path).text
    assert re.search(r'<html[^>]*data-brand="nyte"', html)
    sheets = re.findall(r'<link rel="stylesheet" href="(/static/[^"?]+)', html)
    assert sheets[-1] == "/static/nyte-theme.css"


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
