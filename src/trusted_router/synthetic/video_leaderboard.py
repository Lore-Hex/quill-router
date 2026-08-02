"""Operational leaderboard aggregation for asynchronous video generation."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from trusted_router.money import microdollars_to_decimal
from trusted_router.storage_models import ProviderBenchmarkSample


def _percentile(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-percentile * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def _money(value: int | None) -> str | None:
    return microdollars_to_decimal(value) if value is not None else None


def _aggregate_group(
    *,
    provider: str,
    model: str | None,
    samples: Sequence[ProviderBenchmarkSample],
) -> dict[str, Any]:
    successes = [sample for sample in samples if sample.status == "success"]
    completion_ms = [
        sample.elapsed_milliseconds
        for sample in successes
        if sample.elapsed_milliseconds is not None
    ]
    costs = [sample.total_cost_microdollars for sample in successes]
    costs_per_second = [
        -(-sample.total_cost_microdollars // sample.video_duration_seconds)
        for sample in successes
        if sample.video_duration_seconds and sample.video_duration_seconds > 0
    ]
    generation_ms_per_second = [
        -(-sample.elapsed_milliseconds // sample.video_duration_seconds)
        for sample in successes
        if sample.elapsed_milliseconds is not None
        and sample.video_duration_seconds
        and sample.video_duration_seconds > 0
    ]
    p50_cost = _percentile(costs, 50)
    p50_cost_per_second = _percentile(costs_per_second, 50)
    p50_completion = _percentile(completion_ms, 50)
    p95_completion = _percentile(completion_ms, 95)
    p50_generation_per_second = _percentile(generation_ms_per_second, 50)
    errors = Counter(
        sample.error_type or "provider_error" for sample in samples if sample.status != "success"
    )
    modes = Counter(sample.video_input_mode for sample in samples if sample.video_input_mode)
    resolutions = Counter(sample.video_resolution for sample in samples if sample.video_resolution)
    audio_samples = [
        sample.video_generate_audio for sample in samples if sample.video_generate_audio is not None
    ]
    return {
        "provider": provider,
        "model": model,
        "sample_count": len(samples),
        "success_count": len(successes),
        "success_rate": round(len(successes) / len(samples), 4) if samples else None,
        "p50_completion_ms": p50_completion,
        "p95_completion_ms": p95_completion,
        "p50_completion_seconds": round(p50_completion / 1000, 1)
        if p50_completion is not None
        else None,
        "p95_completion_seconds": round(p95_completion / 1000, 1)
        if p95_completion is not None
        else None,
        "p50_generation_ms_per_output_second": p50_generation_per_second,
        "p50_generation_seconds_per_output_second": (
            round(p50_generation_per_second / 1000, 1)
            if p50_generation_per_second is not None
            else None
        ),
        "p50_cost_microdollars": p50_cost,
        "p50_cost_usd": _money(p50_cost),
        "p50_cost_per_second_microdollars": p50_cost_per_second,
        "p50_cost_per_second_usd": _money(p50_cost_per_second),
        "top_error": errors.most_common(1)[0][0] if errors else None,
        "input_modes": sorted(modes),
        "resolutions": sorted(resolutions),
        "audio_rate": round(sum(bool(value) for value in audio_samples) / len(audio_samples), 4)
        if audio_samples
        else None,
        "last_seen": max((sample.created_at for sample in samples), default=None),
        "measurement_status": "measured" if samples else "awaiting_samples",
    }


def aggregate_video_leaderboard(
    samples: Iterable[ProviderBenchmarkSample],
    *,
    min_samples: int = 1,
    configured_routes: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    rows = list(samples)
    by_model: dict[tuple[str, str], list[ProviderBenchmarkSample]] = defaultdict(list)
    by_provider: dict[str, list[ProviderBenchmarkSample]] = defaultdict(list)
    configured = set(configured_routes)
    for provider, model in configured:
        by_model[(provider, model)]
        by_provider[provider]
    for sample in rows:
        by_model[(sample.provider, sample.model)].append(sample)
        by_provider[sample.provider].append(sample)

    models = [
        _aggregate_group(provider=provider, model=model, samples=group)
        for (provider, model), group in by_model.items()
    ]
    providers = [
        {
            **_aggregate_group(provider=provider, model=None, samples=group),
            "model_count": len(
                {
                    model
                    for route_provider, model in by_model
                    if route_provider == provider
                }
            ),
        }
        for provider, group in by_provider.items()
    ]

    def rank_key(row: dict[str, Any]) -> tuple[int, float, int, int, str]:
        awaiting_samples = int(int(row["sample_count"]) == 0)
        success_rate = float(row["success_rate"] or 0)
        completion = int(row["p50_completion_ms"] or 2**31 - 1)
        cost = int(row["p50_cost_per_second_microdollars"] or 2**31 - 1)
        return (
            awaiting_samples,
            -success_rate,
            completion,
            cost,
            str(row.get("model") or row["provider"]),
        )

    models.sort(key=rank_key)
    providers.sort(key=rank_key)
    for ranked in (models, providers):
        rank = 0
        for row in ranked:
            eligible = int(row["sample_count"]) >= min_samples
            row["rank_eligible"] = eligible
            if eligible:
                rank += 1
                row["rank"] = rank
            else:
                row["rank"] = None

    return {
        "models": models,
        "providers": providers,
        "model_count": len(models),
        "provider_count": len(providers),
        "total_samples": len(rows),
        "total_successes": sum(sample.status == "success" for sample in rows),
    }
