from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("path", "landscape"),
    [("/", False), ("/", True), ("/docs", True), ("/blog", True)],
)
def test_public_pages_credit_company_x_account_and_link_next_to_discord(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    landscape: bool,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "homepage_landscape_enabled", landscape)
    response = client.get(path)
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    tags = page.select('head meta[name="twitter:site"]')
    assert len(tags) == 1
    assert tags[0]["content"] == "@TrustedRouter"
    assert page.select_one('head meta[name="twitter:card"]')["content"] == (
        "summary_large_image"
    )
    x_link = page.select_one('footer .base a[href="https://x.com/TrustedRouter"]')
    assert x_link is not None
    assert x_link.get_text(strip=True) == "X"
    assert x_link["aria-label"] == "TrustedRouter on X"
    assert x_link.find_previous_sibling("a")["href"] == "https://discord.gg/FREVts9KAG"
