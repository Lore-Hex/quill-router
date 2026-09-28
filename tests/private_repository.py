"""Where a trust page names the private Lore-Hex/quill repository."""

from __future__ import annotations

import re

# The release records list the repository under source_repositories, and both
# trust pages print the GCP record, HTML-escaped. That field stays.
RELEASE_RECORD_FIELD = "&quot;quill&quot;: &quot;https://github.com/Lore-Hex/quill&quot;"
# Lore-Hex/quill or its clone name Lore-Hex/quill.git, in any ASCII letter
# case. A GitHub owner name is ASCII letters, digits and "-"; a repository name
# may also contain "." and "_". So the owner must not continue to the left
# (Not-Lore-Hex/quill is another owner), and the repository must not continue
# to the right: quill-router, quill.docs and quill..docs are other
# repositories. A run of "." with no name character after it is punctuation.
_NAME = re.compile(
    r"(?<![A-Za-z0-9-])lore-hex/quill(?:\.git)?(?!\.*[A-Za-z0-9_-])",
    re.IGNORECASE | re.ASCII,
)


def mentions(page: str) -> list[str]:
    """Each place the page source names Lore-Hex/quill, apart from one record field.

    The page prints the release record once, so the name inside the first copy
    of that field is exempt and every other occurrence is a mention. Matching
    runs over the unmodified source. This reads text, so it finds the name in
    a link, an attribute or prose, whatever the host or markup around it. It
    does not decode or resolve URLs.
    """
    field = page.find(RELEASE_RECORD_FIELD)
    exempt = field + RELEASE_RECORD_FIELD.index("Lore-Hex/quill") if field >= 0 else -1
    return [
        page[max(match.start() - 30, 0) : match.end() + 10]
        for match in _NAME.finditer(page)
        if match.start() != exempt
    ]
