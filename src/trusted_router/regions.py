from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.config import Settings


@dataclass(frozen=True)
class RegionGeo:
    """A GCP region's display label and lat/long. Used by the marketing
    page's world map. `lat`/`long` get projected to SVG x/y inline; we
    keep the raw coordinates here so other surfaces (status page,
    docs) can reuse the same data."""

    id: str
    city: str
    lat: float
    lng: float


# GCP region locations — the cities Cloud Run actually runs in. Keep in
# sync with https://cloud.google.com/about/locations when adding rows.
GCP_REGION_GEO: dict[str, RegionGeo] = {
    "us-central1": RegionGeo("us-central1", "Iowa", 41.262, -95.860),
    "us-east1": RegionGeo("us-east1", "S. Carolina", 33.836, -81.163),
    "us-east4": RegionGeo("us-east4", "N. Virginia", 39.045, -77.487),
    "us-west1": RegionGeo("us-west1", "Oregon", 45.523, -122.676),
    "us-west2": RegionGeo("us-west2", "Los Angeles", 34.052, -118.244),
    "northamerica-northeast1": RegionGeo("northamerica-northeast1", "Montréal", 45.501, -73.567),
    "southamerica-east1": RegionGeo("southamerica-east1", "São Paulo", -23.550, -46.633),
    "europe-west1": RegionGeo("europe-west1", "Belgium", 50.503, 4.469),
    "europe-west2": RegionGeo("europe-west2", "London", 51.507, -0.128),
    "europe-west3": RegionGeo("europe-west3", "Frankfurt", 50.111, 8.682),
    "europe-west4": RegionGeo("europe-west4", "Netherlands", 52.379, 4.900),
    "europe-west6": RegionGeo("europe-west6", "Zürich", 47.376, 8.541),
    "me-west1": RegionGeo("me-west1", "Tel Aviv", 32.085, 34.781),
    "africa-south1": RegionGeo("africa-south1", "Johannesburg", -26.204, 28.047),
    "asia-east1": RegionGeo("asia-east1", "Taiwan", 23.553, 121.000),
    "asia-east2": RegionGeo("asia-east2", "Hong Kong", 22.396, 114.109),
    "asia-northeast1": RegionGeo("asia-northeast1", "Tokyo", 35.689, 139.692),
    "asia-northeast2": RegionGeo("asia-northeast2", "Osaka", 34.694, 135.502),
    "asia-northeast3": RegionGeo("asia-northeast3", "Seoul", 37.566, 126.978),
    "asia-south1": RegionGeo("asia-south1", "Mumbai", 19.076, 72.877),
    "asia-southeast1": RegionGeo("asia-southeast1", "Singapore", 1.352, 103.819),
    "asia-southeast2": RegionGeo("asia-southeast2", "Jakarta", -6.208, 106.846),
    "australia-southeast1": RegionGeo("australia-southeast1", "Sydney", -33.868, 151.209),
}


def configured_regions(settings: Settings) -> list[str]:
    regions = [item.strip() for item in settings.regions.split(",") if item.strip()]
    if settings.primary_region not in regions:
        regions.insert(0, settings.primary_region)
    seen: set[str] = set()
    unique: list[str] = []
    for region in regions:
        if region not in seen:
            unique.append(region)
            seen.add(region)
    return unique


def choose_region(settings: Settings, requested: str | None = None) -> str:
    regions = configured_regions(settings)
    if requested and requested in regions:
        return requested
    if settings.primary_region in regions:
        return settings.primary_region
    return regions[0]


def region_payload(settings: Settings) -> list[dict[str, Any]]:
    primary = choose_region(settings)
    return [
        {
            "id": region,
            "name": region,
            "primary": region == primary,
            "enabled": settings.multi_region_enabled or region == primary,
            # The primary region uses the canonical settings.api_base_url
            # (e.g. https://api.quillrouter.com/v1) — that hostname's
            # ACME-issued cert covers the canonical name only, so the
            # api-{primary}.quillrouter.com alias would TLS-fail. Non-primary
            # regions advertise their own per-region hostname which has its
            # own enclave-issued cert (api-europe-west4.quillrouter.com etc).
            "api_base_url": (
                settings.api_base_url
                if region == primary
                else f"https://{settings.regional_api_hostname_template.format(region=region)}/v1"
            ),
        }
        for region in configured_regions(settings)
    ]


def region_map_payload(settings: Settings) -> list[dict[str, Any]]:
    """Project each configured region's lat/long onto a 1000×500 SVG
    using equirectangular (Plate Carrée). Marketing page renders the
    result as <circle> elements over a world outline; the projection is
    intentionally trivial so unit tests can re-derive it."""
    primary = choose_region(settings)
    out: list[dict[str, Any]] = []
    for region in configured_regions(settings):
        geo = GCP_REGION_GEO.get(region)
        if geo is None:
            continue
        out.append(
            {
                "id": geo.id,
                "city": geo.city,
                "lat": geo.lat,
                "lng": geo.lng,
                "x": _project_x(geo.lng),
                "y": _project_y(geo.lat),
                "primary": geo.id == primary,
            }
        )
    return out


def _project_x(lng: float) -> float:
    return (lng + 180.0) * (1000.0 / 360.0)


def _project_y(lat: float) -> float:
    return (90.0 - lat) * (500.0 / 180.0)
