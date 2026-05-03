from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trusted_router.money import microdollars_to_float
from trusted_router.storage_models import Generation, _is_byok


def filter_generations(
    generations: Iterable[Generation],
    *,
    workspace_id: str,
    api_key_hash: str | None = None,
    date: str | None = None,
) -> list[Generation]:
    return [
        gen
        for gen in generations
        if gen.workspace_id == workspace_id
        and (api_key_hash is None or gen.key_hash == api_key_hash)
        and (date is None or gen.created_at.startswith(date))
    ]


def summarize_activity(generations: Iterable[Generation]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for gen in generations:
        day = gen.created_at[:10]
        key = (day, gen.model, gen.provider_name)
        item = grouped.setdefault(
            key,
            {
                "date": day,
                "model": gen.model,
                "provider_name": gen.provider_name,
                "endpoint_id": gen.provider_name.lower().replace(" ", "-"),
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "usage": 0.0,
                "usage_microdollars": 0,
                "byok_usage_inference": 0.0,
                "byok_usage_inference_microdollars": 0,
            },
        )
        item["requests"] += 1
        item["prompt_tokens"] += gen.tokens_prompt
        item["completion_tokens"] += gen.tokens_completion
        cost = microdollars_to_float(gen.total_cost_microdollars)
        if _is_byok(gen.usage_type):
            item["byok_usage_inference"] += cost
        else:
            item["usage"] += cost
        item["usage_microdollars"] += 0 if _is_byok(gen.usage_type) else gen.total_cost_microdollars
        item["byok_usage_inference_microdollars"] += (
            gen.total_cost_microdollars if _is_byok(gen.usage_type) else 0
        )
    return sorted(grouped.values(), key=lambda item: item["date"], reverse=True)


def generation_events(
    generations: Iterable[Generation],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = sorted(generations, key=lambda gen: gen.created_at, reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return [
        {
            "id": gen.id,
            "request_id": gen.request_id,
            "created_at": gen.created_at,
            "date": gen.created_at[:10],
            "model": gen.model,
            "provider_name": gen.provider_name,
            "app": gen.app,
            "input_tokens": gen.tokens_prompt,
            "output_tokens": gen.tokens_completion,
            "cost": microdollars_to_float(gen.total_cost_microdollars),
            "cost_microdollars": gen.total_cost_microdollars,
            "usage_type": gen.usage_type,
            "speed_tokens_per_second": gen.speed_tokens_per_second,
            "finish_reason": gen.finish_reason,
            "status": gen.status,
            "streamed": gen.streamed,
            "content_stored": False,
        }
        for gen in rows
    ]
