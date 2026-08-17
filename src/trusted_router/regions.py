from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.config import Settings


@dataclass(frozen=True)
class RegionGeo:
    """A region's display label and lat/long. Used by the marketing
    page's world map. `lat`/`long` get projected to SVG x/y inline; we
    keep the raw coordinates here so other surfaces (status page,
    docs) can reuse the same data.

    `cloud` defaults to `"gcp"` for back-compat and future portability."""

    id: str
    city: str
    lat: float
    lng: float
    cloud: str = "gcp"


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


def _lookup_region_geo(region: str) -> RegionGeo | None:
    """Return geo info for a configured region on any cloud."""
    return GCP_REGION_GEO.get(region) or MULTICLOUD_REGION_GEO.get(region)


# Standalone deployments on other clouds. These are SEPARATE TrustedRouter
# products — own database, own credits, own status page (see
# docs/storage-portability/multi-cloud-separation.md) — so their ids are
# namespaced by cloud rather than pretending to be GCP regions. A dot here
# must correspond to a deployment that actually serves; the live/staged
# split is settings.external_live_regions, not this table.
MULTICLOUD_REGION_GEO: dict[str, RegionGeo] = {
    "aws-eu-west-1": RegionGeo("aws-eu-west-1", "Dublin", 53.349, -6.260, cloud="aws"),
    # Stockholm is the DSQL replication peer of the AWS-EU deployment. It
    # holds live data but no compute yet, so it defaults to "staged" on the
    # map until compute lands there.
    "aws-eu-north-1": RegionGeo("aws-eu-north-1", "Stockholm", 59.329, 18.068, cloud="aws"),
    "azure-australiaeast": RegionGeo(
        "azure-australiaeast", "Sydney", -33.868, 151.209, cloud="azure"
    ),
}

#: Cloud a bare, un-namespaced region id belongs to. GCP was here first, so its
#: regions are spelled `us-central1` rather than `gcp-us-central1`.
DEFAULT_REGION_CLOUD = "gcp"


def cloud_region_namespaces() -> frozenset[str]:
    """Prefixes that are allowed to name a cloud inside a region id.

    Derived from `MULTICLOUD_REGION_GEO` at CALL time rather than written down
    a second time: that table is what makes `aws-`/`azure-` mean anything, so a
    cloud added there is recognised everywhere at once, and a prefix that is
    not there cannot become a cloud just because somebody typed it.
    """
    return frozenset(geo.cloud for geo in MULTICLOUD_REGION_GEO.values())


def cloud_for_region(region: str) -> str | None:
    """Which cloud a configured region id names, or `None` when nothing can say.

    Three answers, in order: the multi-cloud table; the GCP table (GCP was here
    first, so its ids are bare — `us-central1`, not `gcp-us-central1`); and a
    `<cloud>-<native id>` namespace whose prefix is already a KNOWN cloud, which
    is what lets a new region on an existing cloud (`aws-eu-west-2`) be
    attributed without a table edit.

    Everything else is `None`, and that is the point. An earlier revision
    returned the bare prefix for any hyphenated id, which minted a CLOUD out of
    an ordinary GCP geography: a real GCP region missing from `GCP_REGION_GEO`
    — that table is hand-maintained and Google keeps shipping regions — made
    `europe-west12` resolve to a cloud named "europe", and the fleet-coverage
    check in `trusted_router.operational_analytics_fleet` then failed CI
    demanding a drain-freshness endpoint for a cloud that does not exist.

    Guessing GCP instead would be the opposite failure and a worse one: a
    genuinely new cloud (`oracle-eu-frankfurt-1`) added to a settings list and
    nowhere else would silently read as GCP, i.e. as already covered, which is
    the "configured, healthy, and empty" shape the whole freshness registry
    exists to make impossible. So an id nobody can attribute is `None`, and the
    caller reports it as something a human has to resolve — by adding the row
    that says which cloud it is.
    """
    geo = MULTICLOUD_REGION_GEO.get(region)
    if geo is not None:
        return geo.cloud
    if region in GCP_REGION_GEO:
        return DEFAULT_REGION_CLOUD
    prefix, _, remainder = region.partition("-")
    if remainder and prefix in cloud_region_namespaces():
        return prefix
    return None


def configured_regions(settings: Settings) -> list[str]:
    regions = [item.strip() for item in settings.regions.split(",") if item.strip()]
    if settings.primary_region not in regions:
        regions.insert(0, settings.primary_region)
    return _unique_regions(regions)


def configured_marketing_regions(settings: Settings) -> list[str]:
    configured = [item.strip() for item in settings.marketing_regions.split(",") if item.strip()]
    regions = configured or configured_regions(settings)
    if settings.primary_region not in regions:
        regions.insert(0, settings.primary_region)
    return _unique_regions(regions)


def _unique_regions(regions: list[str]) -> list[str]:
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
            # (e.g. https://api.trustedrouter.com/v1) — that hostname's
            # ACME-issued cert covers the canonical name only, so the
            # api-{primary}.quillrouter.com alias would TLS-fail. Non-primary
            # regions advertise their own per-region hostname which has its
            # own enclave-issued cert (api-europe-west4.quillrouter.com etc).
            "api_base_url": (
                settings.api_base_url
                if region == primary
                else f"https://{settings.regional_api_hostname_template.format(region=region)}/v1"
            ),
            # Per-region Cloud Run direct URL. The synthetic monitor
            # uses this for /health probes that need to hit a
            # SPECIFIC region's Cloud Run (not whichever region the
            # global LB picks). The Cloud Run-managed cert covers
            # `*.{region}.run.app` so it works for every region we
            # have a Cloud Run service in — including the cold ones
            # where the regional `api-{region}.quillrouter.com`
            # hostname doesn't have a cert.
            #
            # Cold/warm doesn't matter: Cloud Run direct URLs work
            # whether the service is at min-scale=0 or 1; they just
            # incur a cold-start on first request after idle. The
            # synthetic monitor probes them anyway because the cold-
            # start tax IS the metric we want to measure.
            "control_plane_url": _cloud_run_direct_url(settings, region),
        }
        for region in configured_regions(settings)
    ]


def _cloud_run_direct_url(settings: Settings, region: str) -> str:
    """Build the Cloud Run direct URL for `region`. Format is
    `https://{service}-{project_number}.{region}.run.app`.

    project_number is read from settings if available so per-region
    URLs work in dev/staging projects without code edits. Falls back
    to the prod hash if unset (tests / unconfigured environments)."""
    project_number = getattr(settings, "gcp_project_number", "") or "44325983244"
    service = getattr(settings, "cloud_run_service_name", "") or "trusted-router"
    return f"https://{service}-{project_number}.{region}.run.app"


def region_map_payload(settings: Settings) -> list[dict[str, Any]]:
    """Project each marketing region's lat/long onto a 1000×500 SVG
    using equirectangular (Plate Carrée). Marketing page renders the
    result as <circle> elements over a world outline; the projection is
    intentionally trivial so unit tests can re-derive it."""
    primary = choose_region(settings)
    serving_regions = set(configured_regions(settings))
    # Standalone deployments on other clouds serve their own traffic, so the
    # GCP serving list can't know about them. They are declared live
    # explicitly — and must only be listed once their own smoke
    # (scripts/deploy/verify_deployment.sh) passes, because this flag is the
    # difference between a "live" and a "staged" dot on the marketing page.
    serving_regions |= {
        item.strip()
        for item in settings.external_live_regions.split(",")
        if item.strip()
    }
    out: list[dict[str, Any]] = []
    for region in configured_marketing_regions(settings):
        geo = _lookup_region_geo(region)
        if geo is None:
            continue
        serving = geo.id in serving_regions
        out.append(
            {
                "id": geo.id,
                "city": geo.city,
                "lat": geo.lat,
                "lng": geo.lng,
                "x": _project_x(geo.lng),
                "y": _project_y(geo.lat),
                "primary": geo.id == primary,
                "cloud": geo.cloud,
                "serving": serving,
                "status_label": "live" if serving else "edge",
            }
        )
    return out


def _project_x(lng: float) -> float:
    return (lng + 180.0) * (1000.0 / 360.0)


def _project_y(lat: float) -> float:
    return (90.0 - lat) * (500.0 / 180.0)
