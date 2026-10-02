"""System1 EU tier: distinct key, tariff and non-fallback model namespace."""

from scripts.pricing.providers._system1models import BASE_URL, URL, System1Catalog  # noqa: F401

CATALOG = System1Catalog("eu")
SLUG = CATALOG.slug
MANIFEST_PATH = CATALOG.manifest_path
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = False
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
canonical_model_id = CATALOG.canonical_model_id
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
