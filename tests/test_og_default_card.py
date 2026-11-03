"""The default social card: what most of the site shows when a link is shared.

Every page without a card of its own falls back to static/og.png through the
og_image default in templates/public/_base.html. These checks keep the file a
valid link-preview image: the OG size, a PNG, small enough to unfurl quickly,
and rendered from the shared template so it matches the homepage card.
"""

from pathlib import Path

from PIL import Image

from scripts.generate_og_card import DEFAULT_CARD, MAX_BYTES, Card, render_html

REPO_ROOT = Path(__file__).resolve().parents[1]
OG_PNG = REPO_ROOT / "src" / "trusted_router" / "static" / "og.png"
TEMPLATE = REPO_ROOT / "docs" / "marketing" / "og-card.html"


def test_default_card_is_a_small_1200x630_png() -> None:
    assert OG_PNG.stat().st_size <= MAX_BYTES
    with Image.open(OG_PNG) as image:
        assert image.format == "PNG"
        assert image.size == (1200, 630)


def test_default_card_words_match_the_homepage_card() -> None:
    homepage = (REPO_ROOT / "docs" / "homepage" / "social-card.html").read_text()
    assert f"{DEFAULT_CARD.headline}<br><em>{DEFAULT_CARD.accent}</em>" in homepage
    assert DEFAULT_CARD.subhead in homepage
    assert DEFAULT_CARD.words <= 7


def test_template_fills_words_and_escapes_them() -> None:
    page = render_html(Card("Routing & <proof>", "you can verify", "One sentence."))
    assert "Routing &amp; &lt;proof&gt;<br><em>you can verify</em>" in page
    assert "<p>One sentence.</p>" in page
    assert f'<base href="{TEMPLATE.parent.as_uri()}/">' in page
    assert "$" not in page.split("<style>")[1].split("</style>")[0]
