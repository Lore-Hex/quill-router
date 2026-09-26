from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path", ["/token-exchange/savings", "/token-exchange/savings/"])
def test_savings_page_is_public_indexable_and_accessible(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert client.head(path).status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select("h1")) == 1
    assert soup.select_one('link[rel="canonical"]')["href"] == "https://trustedrouter.com/token-exchange/savings"
    assert soup.select_one('meta[property="og:image"]')["content"].endswith("/og/token-exchange.png")
    assert soup.select_one("noscript") is not None
    for name in ("spend", "share", "discount"):
        assert soup.select_one(f'label[for="tx-{name}"]')
    assert soup.select_one('#tx-enabled[role="switch"]')
    assert soup.select_one('#tx-validation[role="status"]')
    assert "not a quote" in response.text
    assert "5.5%" in response.text
    assert "not live traffic" in response.text
    assert "per token price floor" in response.text
    for asset in ("token-exchange-savings.js", "token-exchange-savings.css", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_savings_page_is_discoverable(client: TestClient) -> None:
    assert 'href="/token-exchange/savings"' in client.get("/token-exchange").text
    assert "/token-exchange/savings" in client.get("/sitemap-core.xml").text


def test_savings_calculations_and_shared_inputs() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to validate the client-side savings calculator")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed repository test file, no external input
        [node, "--test", str(root / "tests/js/token_exchange_savings.test.cjs")],
        cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
