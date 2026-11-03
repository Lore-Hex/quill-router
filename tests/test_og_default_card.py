"""The default social card: what most of the site shows when a link is shared.

Every page without a card of its own falls back to static/og.png through the
og_image default in templates/public/_base.html. These checks keep the file a
valid link-preview image (the OG size, a PNG, small enough to unfurl quickly)
and keep the script rendering the homepage card's own HTML, so the default
card and the homepage card cannot drift apart.
"""

from pathlib import Path

from PIL import Image

from scripts.generate_og_card import (
    DEFAULT_CARD,
    HEIGHT,
    HOMEPAGE_CARD,
    MAX_BYTES,
    MAX_HEADLINE_WORDS,
    WIDTH,
    Card,
    render_html,
)

OG_PNG = Path(__file__).resolve().parents[1] / "src" / "trusted_router" / "static" / "og.png"


def test_default_card_is_a_small_png_at_the_og_size() -> None:
    assert OG_PNG.stat().st_size <= MAX_BYTES
    with Image.open(OG_PNG) as image:
        assert image.format == "PNG"
        assert image.size == (WIDTH, HEIGHT)


def test_default_card_is_the_homepage_card() -> None:
    """With the homepage words, the script reproduces the homepage card's HTML
    exactly, plus the <base> that lets a copy find the homepage folder's assets."""
    homepage = HOMEPAGE_CARD.read_text()
    base = f'<base href="{HOMEPAGE_CARD.parent.as_uri()}/">'
    expected = homepage.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + base, 1)
    assert render_html(DEFAULT_CARD) == expected
    assert DEFAULT_CARD.words <= MAX_HEADLINE_WORDS


def test_other_words_are_escaped_and_nothing_else_changes() -> None:
    page = render_html(Card("Routing & <proof>", "you can verify", "One sentence."))
    assert "<h1>Routing &amp; &lt;proof&gt;<br><em>you can verify</em></h1>" in page
    assert "<p>One sentence.</p>" in page
    assert "$" not in page
    # Everything but the words is the homepage card: same length once the words are put back.
    restored = page.replace("Routing &amp; &lt;proof&gt;<br><em>you can verify</em>", DEFAULT_CARD.headline_html)
    restored = restored.replace("One sentence.", DEFAULT_CARD.subhead)
    assert restored == render_html(DEFAULT_CARD)


def test_one_line_headline_has_no_italic_line() -> None:
    assert "<h1>Just this</h1>" in render_html(Card("Just this", "", "Sub."))
