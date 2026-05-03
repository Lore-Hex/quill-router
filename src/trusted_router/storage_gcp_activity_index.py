from __future__ import annotations

import json
from typing import Any

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
