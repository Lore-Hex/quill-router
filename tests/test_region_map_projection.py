"""Pin the equirectangular projection used by the marketing world map.

The map renders region dots at SVG coordinates derived from each
region's lat/long. A bug in the projection would push markers off the
continents. The projection is trivial so this test is mostly a
regression guard against accidental sign flips and against the SVG
viewBox dimensions changing without the markers being repositioned.
"""

from __future__ import annotations

import pytest

from trusted_router.config import Settings
from trusted_router.regions import (
    GCP_REGION_GEO,
    _project_x,
    _project_y,
    region_map_payload,
)

# SVG viewBox dimensions — change these and the projection has to update.
SVG_WIDTH = 1000.0
SVG_HEIGHT = 500.0


@pytest.mark.parametrize(
    "lng,expected_x",
    [
        (-180.0, 0.0),
        (-90.0, 250.0),
        (0.0, 500.0),
        (90.0, 750.0),
        (180.0, 1000.0),
    ],
)
def test_project_x_maps_longitude_to_svg_pixels(lng: float, expected_x: float) -> None:
    assert _project_x(lng) == pytest.approx(expected_x)


@pytest.mark.parametrize(
    "lat,expected_y",
    [
        (90.0, 0.0),  # north pole at top
        (45.0, 125.0),
        (0.0, 250.0),
        (-45.0, 375.0),
        (-90.0, 500.0),  # south pole at bottom
    ],
)
def test_project_y_maps_latitude_to_svg_pixels(lat: float, expected_y: float) -> None:
    assert _project_y(lat) == pytest.approx(expected_y)


def test_every_known_region_projects_inside_svg_bounds() -> None:
    """If we add a new region with a typo'd lat/long, this catches the
    marker landing off the canvas before it ships."""
    for region in GCP_REGION_GEO.values():
        x = _project_x(region.lng)
        y = _project_y(region.lat)
        assert 0 <= x <= SVG_WIDTH, f"{region.id} x={x} outside [0, {SVG_WIDTH}]"
        assert 0 <= y <= SVG_HEIGHT, f"{region.id} y={y} outside [0, {SVG_HEIGHT}]"


def test_us_central1_lands_in_iowa_quadrant() -> None:
    """Sanity check: us-central1 (Iowa, ~95°W, ~41°N) projects to the
    upper-middle-left of the SVG. Off-by-one sign flips would put it
    on the wrong continent."""
    x = _project_x(-95.860)
    y = _project_y(41.262)
    # Iowa is ~25% across (longitude -95° on a -180→180 map) and ~28%
    # down (latitude 41° on a 90→-90 map).
    assert 230 < x < 240
    assert 130 < y < 140


def test_australia_southeast1_lands_in_southeast_quadrant() -> None:
    """Sydney (~151°E, ~34°S) → bottom-right of the SVG."""
    x = _project_x(151.209)
    y = _project_y(-33.868)
    assert 900 < x < 940
    assert 340 < y < 360


def test_region_map_payload_marks_primary() -> None:
    settings = Settings(environment="local")
    rendered = region_map_payload(settings)
    primaries = [r for r in rendered if r["primary"]]
    assert len(primaries) == 1, "exactly one region must be marked primary"
    assert primaries[0]["id"] == settings.primary_region


def test_region_map_payload_skips_unknown_region_ids() -> None:
    """A misconfigured TR_REGIONS shouldn't crash the marketing page —
    just drop the unknowns silently. Map drawing tolerates a shorter
    list."""
    settings = Settings(environment="local", regions="us-central1,not-a-real-region")
    rendered = region_map_payload(settings)
    ids = {r["id"] for r in rendered}
    assert "us-central1" in ids
    assert "not-a-real-region" not in ids


def test_region_map_payload_includes_city_label() -> None:
    """Screen-reader fallback text + the right-hand list both rely on
    the city field being present and human-readable."""
    settings = Settings(environment="local")
    for region in region_map_payload(settings):
        assert region["city"], f"{region['id']} missing city label"
        assert region["city"][0].isalpha(), f"{region['id']} city looks malformed"


def test_region_map_payload_orders_primary_first() -> None:
    """The legend list shows primary at top — both the underlying
    configured_regions() helper and the map renderer rely on this."""
    settings = Settings(environment="local")
    rendered = region_map_payload(settings)
    if rendered:
        assert rendered[0]["id"] == settings.primary_region
