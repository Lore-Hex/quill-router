from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app

PAGES = [
    ("sign-in-as-ycombinator", "Y Combinator"),
    ("sign-in-as-startx", "StartX"),
]


@pytest.mark.parametrize(("slug", "organization"), PAGES)
def test_company_signin_developer_page(client: TestClient, slug: str, organization: str) -> None:
    response = client.get(f"/{slug}")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert organization in page.h1.get_text()
    assert page.find("link", rel="canonical")["href"] == f"https://trustedrouter.com/{slug}"
    assert page.find("meta", property="og:url")["content"] == f"https://trustedrouter.com/{slug}"
    prompt = page.find(id="company-agent-prompt").get_text()
    for required in (
        organization, "PKCE S256", "state", "profile", "server", "email_verified",
        "company_affiliations", "funding_organization", "founding_year", "sub",
        "https://trustedrouter.com/v1/oauth/authorize",
        "https://trustedrouter.com/v1/oauth/token",
        "https://trustedrouter.com/v1/auth/userinfo",
        "https://github.com/Lore-Hex/quill-router/blob/main/docs/sign-in-with-trustedrouter.md",
        "not proof of employment", "ordinary sign-in", "localStorage",
    ):
        assert required in prompt
    assert "api.quillrouter.com" not in response.text
    button = page.find("button", attrs={"data-copy-prompt-target": "company-agent-prompt"})
    assert button is not None and button.has_attr("hidden")
    assert page.find(id=button["data-copy-prompt-status"])["role"] == "status"
    assert page.find("script", src=True, attrs={"src": "/static/copy-prompt.js"})
    example = json.loads(page.find(id="company-response").get_text())
    claim = example["data"]["company_affiliations"][0]
    assert example["data"]["email_verified"] is True
    assert claim["funding_organization"] == organization
    assert claim["domain"] == "example.com"
    assert claim["founding_year"] is None
    assert claim["match_method"] == "verified_email_domain"
    assert "server-side" in page.find(id="integration").get_text()
    assert "not operated or endorsed" in page.get_text()
    assert "Google" in page.get_text()
    for suffix in ("/", "?utm_source=test"):
        variant = client.get(f"/{slug}{suffix}")
        assert variant.status_code == 200
        assert f'href="https://trustedrouter.com/{slug}"' in variant.text
    assert client.head(f"/{slug}").status_code == 200


def test_company_signin_pages_are_discoverable(client: TestClient) -> None:
    for source in ("/sign-in-with-trustedrouter", "/docs", "/sitemap-core.xml"):
        response = client.get(source)
        assert response.status_code == 200
        for slug, _ in PAGES:
            assert f"/{slug}" in response.text


@pytest.mark.parametrize(("slug", "organization"), PAGES)
def test_company_signin_snippet_handles_optional_claims(
    client: TestClient, slug: str, organization: str,
) -> None:
    page = BeautifulSoup(client.get(f"/{slug}").text, "html.parser")
    code = page.find(id="company-match-code").get_text()
    assert 'cache: "no-store"' in code
    assert "response.ok" in code
    assert "data.email_verified === true" in code
    assert "Array.isArray(data.company_affiliations)" in code
    assert f'claim.funding_organization === "{organization}"' in code
    assert 'claim.match_method === "verified_email_domain"' in code
    assert "data.sub" in code


@pytest.mark.parametrize(("slug", "organization"), PAGES)
def test_company_signin_is_agent_readable_and_on_public_surface(
    client: TestClient, slug: str, organization: str,
) -> None:
    markdown = client.get(f"/{slug}", headers={"Accept": "text/markdown"})
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "PKCE S256" in markdown.text
    assert organization in markdown.text
    assert "company_affiliations" in markdown.text
    assert "<html" not in markdown.text
    settings = Settings(environment="test", storage_backend="memory", service_surface="public")
    with TestClient(create_app(settings, init_observability=False)) as public_client:
        response = public_client.get(f"/{slug}")
    assert response.status_code == 200
    assert organization in response.text
