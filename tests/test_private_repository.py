from __future__ import annotations

import pytest

from tests.private_repository import RELEASE_RECORD_FIELD, mentions


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="https://github.com/Lore-Hex/quill">x</a>',
        '<a href="https://github.com/Lore-Hex/quill/">x</a>',
        "<a href='http://www.GitHub.com/lore-hex/QUILL#readme'>x</a>",
        '<a href="https://github.com/Lore-Hex/quill.git">x</a>',
        '<a href="https://github.com/Lore-Hex/quill/tree/main">x</a>',
        '<a href="https://github.com./Lore-Hex/quill">x</a>',
        '<svg><a xlink:href="https://github.com/Lore-Hex/quill"><text>x</text></a></svg>',
        '<a href="https://raw.githubusercontent.com/Lore-Hex/quill/main/x">x</a>',
        "<p>Source: Lore-Hex/quill</p>",
        "<p>The code is at Lore-Hex/quill.</p>",
        "<p>The code is at Lore-Hex/quill...</p>",
        "<p>Clone Lore-Hex/quill.git.</p>",
        "<p>See...Lore-Hex/quill</p>",
        "<p>Lore-Hex/quill已私有化。</p>",
        # Case folding is ASCII only: these are not name characters.
        "<p>Lore-Hex/quillİ</p>",
        "<p>Lore-Hex/quillK</p>",
        RELEASE_RECORD_FIELD + '<a href="https://github.com/Lore-Hex/quill">x</a>',
        # Only one copy of the record field is exempt.
        f"<p>{RELEASE_RECORD_FIELD}</p><pre>{RELEASE_RECORD_FIELD}</pre>",
        # A name right before the field is still a mention.
        "Lore-Hex/quill" + RELEASE_RECORD_FIELD + "-router",
    ],
)
def test_the_name_is_found_wherever_the_page_source_carries_it(markup: str) -> None:
    assert len(mentions(markup)) == 1


@pytest.mark.parametrize(
    "markup",
    [
        RELEASE_RECORD_FIELD,
        '<a href="https://github.com/Lore-Hex/quill-router">x</a>',
        '<a href="https://raw.githubusercontent.com/Lore-Hex/quill-router/main/README.md">x</a>',
        '<a href="https://github.com/Lore-Hex/quill-cloud-proxy/commit/abc">x</a>',
        # Text on either side of the field does not join into a name.
        "Lore-Hex/" + RELEASE_RECORD_FIELD + "quill",
        # Other owners and other repositories.
        '<a href="https://github.com/Not-Lore-Hex/quill">x</a>',
        '<a href="https://github.com/Lore-Hex/quill.docs">x</a>',
        '<a href="https://github.com/Lore-Hex/quill..docs">x</a>',
        '<a href="https://github.com/Lore-Hex/quill.github">x</a>',
        '<a href="https://github.com/Lore-Hex/quill_2">x</a>',
    ],
)
def test_public_repositories_and_the_release_record_are_not_mentions(markup: str) -> None:
    assert mentions(markup) == []
