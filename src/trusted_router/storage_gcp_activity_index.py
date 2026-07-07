from __future__ import annotations

import json
from typing import Any

from trusted_router.storage_activity import generation_metrics
from trusted_router.storage_gcp_codec import json_body, reverse_time_key
from trusted_router.storage_models import Generation


def write_generation(table: Any, family: str, generation: Generation) -> None:
    body = json_body(generation).encode("utf-8")
    day = generation.created_at[:10]
    keys = [
        f"gen#{generation.id}",
        f"ws#{generation.workspace_id}#{day}#{generation.created_at}#{generation.id}",
        f"ws_recent#{generation.workspace_id}#{reverse_time_key(generation.created_at)}#{generation.id}",
    ]
    for key in keys:
        row = table.direct_row(key.encode("utf-8"))
        row.set_cell(family, b"body", body)
        row.commit()


def activity_generations(
    table: Any,
    family: str,
    workspace_id: str,
    *,
    api_key_hash: str | None,
    date: str | None,
    limit: int,
) -> list[Generation]:
    if date is None:
        prefix = f"ws_recent#{workspace_id}#".encode()
        rows = table.read_rows(start_key=prefix, end_key=prefix + b"~", limit=limit)
    else:
        prefix = f"ws#{workspace_id}#{date}#".encode()
        rows = table.read_rows(start_key=prefix, end_key=prefix + b"~", limit=limit)
    generations = _generations_from_rows(rows, family, api_key_hash=api_key_hash)
    if date is None and not generations:
        legacy_prefix = f"ws#{workspace_id}#".encode()
        legacy_rows = table.read_rows(
            start_key=legacy_prefix,
            end_key=legacy_prefix + b"~",
            limit=limit,
        )
        generations.extend(_generations_from_rows(legacy_rows, family, api_key_hash=api_key_hash))
    generations.sort(key=lambda item: item.created_at, reverse=True)
    return generations[:limit]


def usage_series(
    table: Any,
    family: str,
    workspace_id: str,
    *,
    start_day: str,
    end_day: str,
    granularity: str,
    api_key_hash: str | None = None,
    by_model: bool = False,
    max_rows: int = 200_000,
) -> dict[str, Any]:
    if granularity not in {"hour", "day"}:
        raise ValueError("granularity must be 'hour' or 'day'")

    buckets: dict[str, dict[str, Any]] = {}
    model_buckets: dict[str, dict[str, dict[str, Any]]] = {}
    rows = table.read_rows(
        start_key=f"ws#{workspace_id}#{start_day}".encode(),
        end_key=f"ws#{workspace_id}#{end_day}~".encode(),
        limit=max_rows,
    )
    scanned = 0
    truncated = False
    for row in rows:
        if scanned >= max_rows:
            truncated = True
            break
        scanned += 1
        generation = _generation_from_row(row, family)
        if generation is None:
            continue
        if api_key_hash is not None and generation.key_hash != api_key_hash:
            continue
        bucket = generation.created_at[:13] if granularity == "hour" else generation.created_at[:10]
        metrics = generation_metrics(generation)
        _add_metrics(_bucket(buckets, bucket), metrics)
        if by_model:
            _add_metrics(_bucket(model_buckets.setdefault(generation.model, {}), bucket), metrics)
    if scanned >= max_rows:
        truncated = True

    result: dict[str, Any] = {
        "granularity": granularity,
        "start_day": start_day,
        "end_day": end_day,
        "truncated": truncated,
        "buckets": _sorted_buckets(buckets),
    }
    if by_model:
        result["by_model"] = {
            model: _sorted_buckets(per_model) for model, per_model in sorted(model_buckets.items())
        }
    return result


def _generations_from_rows(
    rows: Any,
    family: str,
    *,
    api_key_hash: str | None,
) -> list[Generation]:
    generations: list[Generation] = []
    for row in rows:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        generation = Generation(**json.loads(cells[0].value.decode("utf-8")))
        if api_key_hash is None or generation.key_hash == api_key_hash:
            generations.append(generation)
    return generations


def _generation_from_row(row: Any, family: str) -> Generation | None:
    cells = row.cells.get(family, {}).get(b"body", [])
    if not cells:
        return None
    return Generation(**json.loads(cells[0].value.decode("utf-8")))


def _bucket(buckets: dict[str, dict[str, Any]], bucket: str) -> dict[str, Any]:
    return buckets.setdefault(
        bucket,
        {
            "bucket": bucket,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_micro": 0,
            "byok_micro": 0,
        },
    )


def _add_metrics(bucket: dict[str, Any], metrics: dict[str, int]) -> None:
    for key, value in metrics.items():
        bucket[key] += value


def _sorted_buckets(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [buckets[key] for key in sorted(buckets)]
