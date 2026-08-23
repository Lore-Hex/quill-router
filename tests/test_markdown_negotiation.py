"""acceptmarkdown.com content negotiation on the public pages.

THE LAW

  * A client that asks for markdown by name, and ranks it above HTML, gets
    `text/markdown; charset=utf-8`.
  * A client that does not gets byte-identical HTML to before.
  * Both variants carry `Accept` in `Vary`.

The `Vary` assertions are the ones worth having. Serving the markdown is the
visible half and it is hard to get wrong; omitting `Vary: Accept` is invisible
in every local test and breaks only in front of a CDN, where whichever variant
missed cache first is then served to everybody.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.markdown_negotiation import html_to_markdown, prefers_markdown


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("text/markdown", True),
        ("text/x-markdown", True),
        ("text/markdown;q=0.9,text/html;q=0.8", True),
        ("text/markdown, text/html", False),  # tie -> the existing representation
        ("text/markdown;q=0.4,text/html;q=0.9", False),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", False),
        ("*/*", False),
        ("", False),
        (None, False),
        ("text/markdown;q=notanumber", True),  # malformed q is not a 500
    ],
)
def test_preference_parsing(accept: str | None, expected: bool) -> None:
    assert prefers_markdown(accept) is expected


def test_wildcard_alone_does_not_flip_the_site_to_markdown() -> None:
    """`*/*` is what curl, monitoring and most SDKs send.

    Treating it as consent to markdown would change the default representation
    of every public page for all of them, which is a much larger change than
    the convention asks for.
    """
    assert prefers_markdown("*/*") is False


def test_markdown_variant_is_served_and_varies(client: TestClient) -> None:
    response = client.get("/", headers={"accept": "text/markdown"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert "accept" in vary
    assert "<html" not in response.text.lower()
    assert "<div" not in response.text.lower()


def test_html_variant_also_varies_on_accept(client: TestClient) -> None:
    """The half that is easy to skip: without it a cache can hand this HTML to
    the next client asking for markdown."""
    response = client.get("/", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert "accept" in vary


def test_vary_names_accept_once(client: TestClient) -> None:
    """Gzip appends its own token with a plain concat that does not dedupe, so
    a middleware contributing the full pair produced Accept-Encoding twice."""
    response = client.get("/", headers={"accept": "text/markdown", "accept-encoding": "gzip"})

    tokens = [part.strip().lower() for part in response.headers["vary"].split(",")]
    assert tokens.count("accept") == 1
    assert tokens.count("accept-encoding") == 1


def test_a_browser_request_is_untouched(client: TestClient) -> None:
    browser = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    response = client.get("/", headers={"accept": browser})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text.lower()


def test_api_routes_are_not_negotiated(client: TestClient) -> None:
    """/v1 already speaks JSON. Markdown there would be a breaking change to an
    API contract, not a convenience."""
    response = client.get("/v1/models", headers={"accept": "text/markdown"})

    assert response.headers["content-type"].startswith("application/json")


def test_markdown_keeps_the_content_and_drops_the_chrome() -> None:
    html = """
    <html><head><title>t</title><style>.a{color:red}</style></head>
    <body><nav>skip</nav><main>
      <h1>Heading</h1>
      <p>Body with <a href="/models">a link</a> and <strong>bold</strong>.</p>
      <ul><li>first</li><li>second</li></ul>
      <table><tr><th>Model</th><th>Price</th></tr><tr><td>a</td><td>$1</td></tr></table>
      <pre>code block</pre>
      <script>alert(1)</script>
    </main></body></html>
    """
    markdown = html_to_markdown(html)

    assert "# Heading" in markdown
    assert "[a link](/models)" in markdown
    assert "**bold**" in markdown
    assert "- first" in markdown
    assert "| Model | Price |" in markdown
    assert "```" in markdown
    assert "alert(1)" not in markdown
    assert "color:red" not in markdown


def test_conversion_of_a_real_page_is_not_empty(client: TestClient) -> None:
    """A converter that silently produces nothing would pass every structural
    assertion above while serving agents a blank page."""
    response = client.get("/", headers={"accept": "text/markdown"})

    assert len(response.text.strip()) > 500
    assert response.text.count("#") >= 2
