"""Bounded Spanner records for generation lookup and repair.

The record contains metadata only. Prompt text, model output, tool arguments,
authorization headers, and provider credentials are never projected here.
Longer-lived activity analytics live in ClickHouse; this table only preserves
the OpenRouter-compatible generation lookup and a short repair window.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

from trusted_router.storage_models import Generation

GENERATION_TABLE = "tr_generation"


def generation_record_body(generation: Generation) -> str:
    """Serialize lookup metadata without model-produced tool arguments."""
    payload = dataclasses.asdict(generation)
    payload.pop("tool_calls", None)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def insert_generation_record(
    transaction: Any,
    param_types: Any,
    generation: Generation,
    *,
    terminal_at: Any,
) -> None:
    transaction.execute_update(
        "INSERT INTO tr_generation ("
        "generation_id, workspace_id, key_hash, created_at, terminal_at, payload"
        ") VALUES ("
        "@generation_id, @workspace_id, @key_hash, @created_at, @terminal_at, @payload"
        ")",
        params={
            "generation_id": generation.id,
            "workspace_id": generation.workspace_id,
            "key_hash": generation.key_hash,
            "created_at": _timestamp(generation.created_at),
            "terminal_at": terminal_at,
            "payload": generation_record_body(generation),
        },
        param_types={
            "generation_id": param_types.STRING,
            "workspace_id": param_types.STRING,
            "key_hash": param_types.STRING,
            "created_at": param_types.TIMESTAMP,
            "terminal_at": param_types.TIMESTAMP,
            "payload": param_types.STRING,
        },
    )


def upsert_generation_record(
    transaction: Any,
    param_types: Any,
    generation: Generation,
    *,
    terminal_at: Any,
) -> None:
    """Idempotently restore a record while replaying an old settlement."""
    transaction.execute_update(
        "INSERT OR UPDATE INTO tr_generation ("
        "generation_id, workspace_id, key_hash, created_at, terminal_at, payload"
        ") VALUES ("
        "@generation_id, @workspace_id, @key_hash, @created_at, @terminal_at, @payload"
        ")",
        params={
            "generation_id": generation.id,
            "workspace_id": generation.workspace_id,
            "key_hash": generation.key_hash,
            "created_at": _timestamp(generation.created_at),
            "terminal_at": terminal_at,
            "payload": generation_record_body(generation),
        },
        param_types={
            "generation_id": param_types.STRING,
            "workspace_id": param_types.STRING,
            "key_hash": param_types.STRING,
            "created_at": param_types.TIMESTAMP,
            "terminal_at": param_types.TIMESTAMP,
            "payload": param_types.STRING,
        },
    )


def read_generation_record(
    reader: Any,
    param_types: Any,
    generation_id: str,
) -> Generation | None:
    rows = list(
        reader.execute_sql(
            "SELECT payload FROM tr_generation WHERE generation_id=@generation_id",
            params={"generation_id": generation_id},
            param_types={"generation_id": param_types.STRING},
        )
    )
    if not rows:
        return None
    payload = json.loads(str(rows[0][0]))
    if not isinstance(payload, dict):
        return None
    known = {field.name for field in dataclasses.fields(Generation)}
    return Generation(**{key: value for key, value in payload.items() if key in known})


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
