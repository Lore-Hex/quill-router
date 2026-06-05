"""Public benchmark sample selection.

The benchmark table contains both organic provider observations and the
provider/model rotation probe. High-frequency health checks can make one cheap
route very hot, so public pages should not read one global "newest rows" window
and let that route crowd out every other provider.
"""

from __future__ import annotations

from trusted_router.catalog import providers_for_display
from trusted_router.storage import STORE
from trusted_router.storage_models import ProviderBenchmarkSample


def public_benchmark_samples(
    *,
    limit: int,
    per_provider_limit: int | None = None,
) -> list[ProviderBenchmarkSample]:
    """Return a bounded, provider-balanced public benchmark window."""
    if limit <= 0:
        return []
    provider_slugs = [provider.slug for provider in providers_for_display()]
    if not provider_slugs:
        return STORE.provider_benchmark_samples(date=None, limit=limit)

    per_provider = per_provider_limit or max(25, -(-limit // len(provider_slugs)))
    by_id: dict[str, ProviderBenchmarkSample] = {}
    for provider in provider_slugs:
        for sample in STORE.provider_benchmark_samples(
            date=None,
            provider=provider,
            limit=per_provider,
        ):
            by_id[sample.id] = sample

    # Include a small global tail so an uncataloged provider name from organic
    # traffic is still visible, without allowing it to dominate the page.
    for sample in STORE.provider_benchmark_samples(date=None, limit=per_provider):
        by_id[sample.id] = sample

    samples = sorted(by_id.values(), key=lambda sample: sample.created_at, reverse=True)
    return samples[:limit]
