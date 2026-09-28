"""Decart's pricing page: queued video and image rates, never realtime ones.

The fixture is trimmed from the live page captured on 2026-09-28, when the
realtime table gained a "Fast mode" column. That gave it the same five columns
as the queued video table, and the parser, which told them apart by column
count, read realtime rows and failed on Lucy Restyle 2's '-' Fast mode cell.
Every hourly refresh then fell back to the committed manifest.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

from scripts.pricing.providers import decart

FIXTURE = Path(__file__).parent / "fixtures" / "pricing" / "decart.html"

# What the fixture publishes, and what decart.json billed on 2026-09-28.
FIXTURE_PRICES = {
    "decart/lucy-2.5": 40_000,
    "decart/lucy-vton-3.5": 40_000,
    "decart/lucy-restyle-2": 10_000,
    "decart/lucy-image-2": {"480p": 10_000, "720p": 20_000},
}


def _page() -> BeautifulSoup:
    return BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")


def _cell(page: BeautifulSoup, section: str, upstream_id: str, column: str) -> Any:
    """The first cell under `column` in the `section` row for `upstream_id`."""
    for table in page.find_all("table"):
        if table.find_previous("h2").get("id") != section:
            continue
        rows = table.find_all("tr")
        columns = [cell.get_text(strip=True) for cell in rows[0].find_all(["th", "td"])]
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if cells[columns.index("ID")].get_text(strip=True) == upstream_id:
                return cells[columns.index(column)]
    raise AssertionError(f"no {upstream_id} row under {section}")


def test_the_live_layout_parses_to_the_queued_and_image_prices() -> None:
    # The realtime table's Lucy Restyle 2 row still has '-' for Fast mode.
    assert _cell(_page(), "realtime-models", "lucy-restyle-2", "Fast mode").get_text() == "-"

    assert decart._parse_pricing(str(_page())) == FIXTURE_PRICES


def test_a_realtime_rate_never_prices_a_queued_job() -> None:
    page = _page()
    for upstream_id in ("lucy-2.5", "lucy-vton-3.5", "lucy-restyle-2"):
        _cell(page, "realtime-models", upstream_id, "720p").string = "$9.99/sec"
        _cell(page, "realtime-models", upstream_id, "Fast mode").string = "$9.99/sec"

    assert decart._parse_pricing(str(page)) == FIXTURE_PRICES

    # Without its queued row, a model is missing, not priced from realtime.
    _cell(page, "video-models", "lucy-restyle-2", "720p").find_parent("tr").decompose()
    with pytest.raises(RuntimeError, match="rows missing: decart/lucy-restyle-2$"):
        decart._parse_pricing(str(page))


@pytest.mark.parametrize(
    ("section", "upstream_id", "column", "value"),
    [
        ("video-models", "lucy-2.5", "720p", "-"),
        ("image-models", "lucy-image-2", "480p", "-"),
        ("video-models", "lucy-vton-3.5", "720p", "$0.04/min"),
        ("image-models", "lucy-image-2", "720p", "$0.02/sec"),
        ("video-models", "lucy-2.5", "720p", "$0.04/sec*"),
    ],
    ids=["video-dash", "image-dash", "video-per-minute", "image-per-second", "annotated"],
)
def test_a_needed_cell_that_is_not_a_price_in_its_unit_fails_closed(
    section: str, upstream_id: str, column: str, value: str
) -> None:
    page = _page()
    _cell(page, section, upstream_id, column).string = value

    with pytest.raises(RuntimeError, match=f"decart/{upstream_id} {column} is not a price"):
        decart._parse_pricing(str(page))


def _video_table(page: BeautifulSoup) -> Any:
    return _cell(page, "video-models", "lucy-2.5", "720p").find_parent("table")


def _rename_header(table: Any, old: str, new: str) -> None:
    header = table.find("tr").find_all(["th", "td"])
    next(cell for cell in header if cell.get_text(strip=True) == old).string = new


def _realtime_table_under_the_video_heading(page: BeautifulSoup) -> None:
    # The only Lucy Restyle 2 row sits in a realtime-shaped table (no 480p
    # column), at exactly the rate decart.json bills.
    _cell(page, "video-models", "lucy-restyle-2", "720p").find_parent("tr").decompose()
    realtime = BeautifulSoup(
        "<table><thead><tr><th>Model</th><th>ID</th><th>720p</th><th>Fast mode</th>"
        "<th>Best for</th></tr></thead><tbody><tr><td>Lucy Restyle 2</td>"
        "<td>lucy-restyle-2</td><td>$0.01/sec</td><td>-</td><td>Realtime</td></tr>"
        "</tbody></table>",
        "html.parser",
    ).table
    _video_table(page).insert_before(realtime)


def _heading_before_the_video_table(level: str) -> Callable[[BeautifulSoup], None]:
    def edit(page: BeautifulSoup) -> None:
        heading = page.new_tag(level)
        heading.string = "Realtime models"
        _video_table(page).insert_before(heading)

    return edit


def _set_cell(section: str, upstream_id: str, column: str, **attrs: str) -> Callable[..., None]:
    def edit(page: BeautifulSoup) -> None:
        cell = _cell(page, section, upstream_id, column)
        text = attrs.pop("text", None)
        if text is not None:
            cell.string = text
        for name, value in attrs.items():
            cell[name] = value

    return edit


def _longer_row(page: BeautifulSoup) -> None:
    _cell(page, "video-models", "lucy-2.5", "720p").find_parent("tr").append(page.new_tag("td"))


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (_realtime_table_under_the_video_heading, "lucy-restyle-2 needs exactly one 480p column"),
        (lambda page: _rename_header(_video_table(page), "Best for", "720p"),
         "lucy-2.5 needs exactly one 720p column"),
        (lambda page: _rename_header(_video_table(page), "720p", "HD"),
         "lucy-2.5 needs exactly one 720p column"),
        (lambda page: _rename_header(_video_table(page), "ID", "Model ID"),
         "a video models table needs exactly one ID column"),
        (_heading_before_the_video_table("h3"), "rows missing: decart/lucy-2.5, "),
        (_heading_before_the_video_table("h5"), "rows missing: decart/lucy-2.5, "),
        (_longer_row, "a video models row does not match its header"),
        (_set_cell("video-models", "lucy-2.5", "720p", colspan="2"), "lucy-2.5's table spans cells"),
        (_set_cell("video-models", "lucy-2.5", "720p", text="$0.0400009/sec"),
         "0.0400009 is not a whole number of microdollars"),
        # 34 significant digits: beyond Decimal's default 28-digit context.
        (_set_cell("video-models", "lucy-2.5", "720p",
                   text="$0.0400000000000000000000000000000001/sec"),
         "is not a whole number of microdollars"),
    ],
    ids=[
        "realtime-shaped-table",
        "duplicate-column",
        "renamed-column",
        "no-id-column",
        "nested-h3-heading",
        "nested-h5-heading",
        "row-longer-than-header",
        "spanned-cell",
        "fractional-microdollars",
        "fraction-beyond-decimal-precision",
    ],
)
def test_a_table_outside_the_queued_shape_fails_closed(
    edit: Callable[[BeautifulSoup], None], error: str
) -> None:
    page = _page()
    edit(page)

    with pytest.raises(RuntimeError, match=re.escape(error)):
        decart._parse_pricing(str(page))


def test_columns_are_read_by_header_in_any_order() -> None:
    page = _page()
    table = _video_table(page)
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        cells[2].insert_before(cells[3].extract())  # 720p moves ahead of 480p

    header = [cell.get_text(strip=True) for cell in table.find("tr").find_all("th")]
    assert header[2:4] == ["720p", "480p"]
    assert decart._parse_pricing(str(page)) == FIXTURE_PRICES


def test_one_model_in_two_tables_at_one_price_is_accepted() -> None:
    page = _page()
    row = _cell(page, "video-models", "lucy-2.5", "720p").find_parent("tr")
    previous_generation = _cell(page, "video-models", "lucy-clip", "720p").find_parent("table")
    previous_generation.find("tbody").append(BeautifulSoup(str(row), "html.parser").find("tr"))

    assert decart._parse_pricing(str(page)) == FIXTURE_PRICES


def test_rows_outside_the_video_and_image_sections_are_ignored() -> None:
    page = _page()
    page.main.append(
        BeautifulSoup(
            "<h2>Cost examples</h2><table><tr><th>Model</th><th>ID</th><th>480p</th>"
            "<th>720p</th></tr><tr><td>Lucy 2.5</td><td>lucy-2.5</td><td>-</td>"
            "<td>$9.99/sec</td></tr></table>",
            "html.parser",
        )
    )

    assert decart._parse_pricing(str(page)) == FIXTURE_PRICES


def test_a_heading_with_a_zero_width_space_inside_a_word_still_names_its_section() -> None:
    page = _page()
    heading = _video_table(page).find_previous("h2")
    heading.string = "Vid\u200beo models"

    assert decart._parse_pricing(str(page)) == FIXTURE_PRICES


def test_one_model_at_two_queued_prices_fails_closed() -> None:
    page = _page()
    row = _cell(page, "video-models", "lucy-2.5", "720p").find_parent("tr")
    duplicate = BeautifulSoup(str(row), "html.parser").find("tr")
    duplicate.find_all("td")[3].string = "$0.05/sec"
    row.insert_after(duplicate)

    with pytest.raises(RuntimeError, match="decart/lucy-2.5 is listed at two prices"):
        decart._parse_pricing(str(page))


def test_tables_without_their_section_headings_fail_closed() -> None:
    # The heading-less fixture this file replaced was parsed by column count.
    page = _page()
    for heading in page.find_all("h2"):
        heading.decompose()

    with pytest.raises(RuntimeError, match="official pricing rows missing"):
        decart._parse_pricing(str(page))
