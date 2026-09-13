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


@pytest.mark.parametrize(("slug", "label"), [
    ("sign-in-as-ycombinator", "Sign in with Y Combinator"),
    ("sign-in-as-startx", "Sign in with StartX"),
    ("sign-in-as-vc", "Sign in as VC-backed"),
])
def test_company_signin_prompt_is_short_and_links_to_its_guide(
    client: TestClient, slug: str, label: str,
) -> None:
    page = BeautifulSoup(client.get(f"/{slug}").text, "html.parser")
    prompt = page.find(id="company-agent-prompt").get_text().strip()
    assert len(prompt.split()) <= 40
    assert "\n" not in prompt
    assert prompt == (
        f'Add "{label}" to this app using TrustedRouter. '
        f"Follow the guide and use its button image: https://trustedrouter.com/{slug}"
    )


@pytest.mark.parametrize(("slug", "organization"), PAGES)
def test_company_signin_developer_page(client: TestClient, slug: str, organization: str) -> None:
    response = client.get(f"/{slug}")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert organization in page.h1.get_text()
    assert page.find("link", rel="canonical")["href"] == f"https://trustedrouter.com/{slug}"
    assert page.find("meta", property="og:url")["content"] == f"https://trustedrouter.com/{slug}"
    for required in (
        organization, "PKCE S256", "state", "profile", "server", "email_verified",
        "company_affiliations", "funding_organization", "founding_year", "sub",
        "https://trustedrouter.com/v1/auth/userinfo",
        "https://github.com/Lore-Hex/quill-router/blob/main/docs/sign-in-with-trustedrouter.md",
        "does not prove employment", "ordinary sign-in",
    ):
        assert required in response.text
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


@pytest.mark.parametrize("slug", ["ycombinator", "startx"])
def test_company_signin_button_assets_and_embed(client: TestClient, slug: str) -> None:
    from defusedxml import ElementTree

    page = BeautifulSoup(client.get(f"/sign-in-as-{slug}").text, "html.parser")
    section = page.find(id="button-assets")
    assert section is not None
    for theme in ("light", "dark"):
        path = f"/static/sign-in/{slug}-{theme}.svg"
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        root = ElementTree.fromstring(response.content)
        assert root.attrib["viewBox"] == "0 0 360 88"
        text = " ".join(root.itertext())
        assert "Sign in with" in text
        assert "Backed by TrustedRouter / Google" in text
        assert "<script" not in response.text
        assert "href=" not in response.text
        assert section.find("a", href=path).has_attr("download")
        code = page.find(id=f"company-button-{theme}").get_text()
        assert f"https://trustedrouter.com{path}" in code
        assert 'href="/auth/trustedrouter"' in code
        assert "oauth/authorize?" not in code
    assert "Backed by TrustedRouter / Google" in section.get_text()


def test_vc_guide_lists_policy_firms_and_is_discoverable(client: TestClient) -> None:
    from trusted_router.company_affiliations import SOURCE_HOSTS

    response = client.get("/sign-in-as-vc")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.h1.get_text() == "Sign in as a VC-backed company"
    names = [node.get_text() for node in page.select(".company-firms a")]
    expected = [name for name in SOURCE_HOSTS if name not in {"Y Combinator", "StartX"}]
    assert len(names) == 10
    assert names == expected
    code = page.find(id="company-match-code").get_text()
    for name in expected:
        assert name in code
    assert "email_verified === true" in code
    assert "verified_email_domain" in code
    assert "funding_organization" in code
    assert "Sequoia Capital" in page.find(id="company-response").get_text()
    assert '"relationship": "portfolio"' in page.find(id="company-response").get_text()
    for source in ("/sign-in-as-ycombinator", "/sign-in-as-startx", "/docs", "/sign-in-with-trustedrouter", "/sitemap-core.xml"):
        assert "/sign-in-as-vc" in client.get(source).text
    for path in ("/sign-in-as-vc/", "/sign-in-as-vc?utm_source=test"):
        variant = client.get(path)
        assert variant.status_code == 200
        assert 'href="https://trustedrouter.com/sign-in-as-vc"' in variant.text
    assert client.head("/sign-in-as-vc").status_code == 200
    markdown = client.get("/sign-in-as-vc", headers={"Accept": "text/markdown"})
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "Sequoia Capital" in markdown.text


def test_signin_artwork_is_generated_from_current_guide_copy() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(root / "scripts/generate_company_signin_buttons.py"), "--check"],
        cwd=root, check=True, capture_output=True,
    )
