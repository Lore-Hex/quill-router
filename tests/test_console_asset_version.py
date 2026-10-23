"""Console assets must not survive a release under a permanent cache URL."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from trusted_router.config import Settings
from trusted_router.routes.console._shared import ConsoleContext, render


@pytest.mark.parametrize("template", ["console/api_keys.html", "console/welcome.html"])
def test_console_asset_urls_change_between_releases(template):
    workspace = SimpleNamespace(id="asset-test", name="Asset test")
    ctx = ConsoleContext(
        user=SimpleNamespace(email="fixture@example.test"),
        session=SimpleNamespace(),
        workspace=workspace,
        workspaces=[workspace],
        can_manage=True,
        api_base_url="https://api.example.test/v1",
    )
    versions = []
    for release in ("release-before", "release-after"):
        html = render(
            template, settings=Settings(environment="test", release=release), ctx=ctx,
            active="api-keys", page_title="API Keys", keys=[],
            suggested={"daily": "20", "weekly": "100", "monthly": "200"},
            revealed_key="fixture-not-a-real-key",
        )
        soup = BeautifulSoup(html, "html.parser")
        urls = [str(tag.get("href") or tag.get("src")) for tag in
                soup.select('link[rel="stylesheet"], script[src]')]
        assert any("social-rewards.js" in url for url in urls) is (template == "console/welcome.html")
        if template == "console/api_keys.html":
            assert any("api-keys.css" in url for url in urls)
        assert any("charter.css" in url for url in urls)
        for url in urls:
            if url.startswith("/static/"):
                assert parse_qs(urlsplit(url).query)["v"] == [release], url
        versions.append(urls)
    assert versions[0] != versions[1]
