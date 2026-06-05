"""Public benchmark sample selection.

The benchmark table contains both organic provider observations and the
provider/model rotation probe. High-frequency health checks can make one cheap
route very hot, so public pages should not read one global "newest rows" window
and let that route crowd out every other provider.
"""

from __future__ import annotations

import datetime as dt

from trusted_router.catalog import providers_for_display
from trusted_router.storage import STORE
from trusted_router.storage_models import ProviderBenchmarkSample


def _cutoff_iso(*, recent_minutes: int | None, now: dt.datetime | None = None) -> str | None:
    if recent_minutes is None or recent_minutes <= 0:
        return None
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    cutoff = current.astimezone(dt.UTC) - dt.timedelta(minutes=recent_minutes)
    return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _after_cutoff(sample: ProviderBenchmarkSample, cutoff: str | None) -> bool:
    return cutoff is None or sample.created_at >= cutoff


def public_benchmark_samples(
    *,
    limit: int,
    per_provider_limit: int | None = None,
    recent_minutes: int | None = None,
    now: dt.datetime | None = None,
) -> list[ProviderBenchmarkSample]:
    """Return a bounded, provider-balanced public benchmark window."""
    if limit <= 0:
        return []
    cutoff = _cutoff_iso(recent_minutes=recent_minutes, now=now)
    provider_slugs = [provider.slug for provider in providers_for_display()]
    if not provider_slugs:
        rows = STORE.provider_benchmark_samples(date=None, limit=limit)
        return [sample for sample in rows if _after_cutoff(sample, cutoff)]

    per_provider = per_provider_limit or max(25, -(-limit // len(provider_slugs)))
    by_id: dict[str, ProviderBenchmarkSample] = {}
    for provider in provider_slugs:
        for sample in STORE.provider_benchmark_samples(
            date=None,
            provider=provider,
            limit=per_provider,
        ):
            if _after_cutoff(sample, cutoff):
                by_id[sample.id] = sample

    # Include a small global tail so an uncataloged provider name from organic
    # traffic is still visible, without allowing it to dominate the page.
    for sample in STORE.provider_benchmark_samples(date=None, limit=per_provider):
        if _after_cutoff(sample, cutoff):
            by_id[sample.id] = sample

    samples = sorted(by_id.values(), key=lambda sample: sample.created_at, reverse=True)
    return samples[:limit]
