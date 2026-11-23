from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.seo_meta import provider_link_rel


@pytest.mark.parametrize(("url", "external"), [
    ("https://vendor.example/terms", True),
    ("//vendor.example/terms", True),
    ("https://trustedrouter.com.vendor.example/privacy", True),
    ("https://github.com/fortanix/armet-ctf", True),
    ("https://github.com/Lore-Hex/quill-router", False),
    ("https://trust.trustedrouter.com/policy", False),
    ("/providers/maple", False),
    ("mailto:provider.ops@nytex.com", False),
])
def test_provider_resource_rel(url: str, external: bool) -> None:
    tokens = provider_link_rel(url, blank=True).split()
    assert ("nofollow" in tokens) == external
    assert {"noopener", "noreferrer"} <= set(tokens)


@pytest.mark.parametrize(("page", "external_selector", "internal_selector"), [
    ("/providers/marketplace", 'a[href="https://www.neurometric.ai/"]',
     'a[href="/providers/neurometric"]'),
    ("/providers", 'a[href*="venice.ai/"]', 'a[href="/providers/venice"]'),
    ("/providers/venice", 'a[href*="venice.ai/"]', 'a[href="/providers"]'),
    ("/models/minimax/minimax-m3", 'a[target="_blank"][href*="minimax.io/"]',
     'a[href="/providers/minimax"]'),
    ("/legal/subprocessors", 'a[href*="venice.ai/"]', 'a[href="/privacy"]'),
])
def test_rendered_provider_link_rel(
    client: TestClient, page: str, external_selector: str, internal_selector: str,
) -> None:
    response = client.get(page)
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    external = soup.select(external_selector)
    assert external and all("nofollow" in link.get_attribute_list("rel") for link in external)
    internal = soup.select(internal_selector)
    assert internal and not any("nofollow" in link.get_attribute_list("rel") for link in internal)
