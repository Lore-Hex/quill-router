"""Typed Spanner storage for bounded gateway authorization state.

The generic ``tr_entities`` table is appropriate for low-volume control-plane
objects, but not for one row per inference request. Gateway authorizations are
active billing state until settle/refund wins the reservation claim. Once the
durable settle outbox confirms the index write, ``terminal_at`` starts a bounded
idempotency/audit window.

``terminal_at`` is deliberately nullable. Spanner TTL ignores NULL timestamps,
so an unresolved authorization or a settled request with pending metadata
repair can never be deleted by the row-deletion policy.

The JSON payload is the minimal content-free replay record needed to honor an
idempotency key after settlement. It expires with the typed row after 30 days.
Prompt and output content are never present in either representation.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_spend_lease import (
    AUTHORIZATION_TYPED_COLUMNS,
    authorization_typed_columns,
    authorization_typed_param_types,
    merge_authorization_typed_columns,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

AUTHORIZATION_TABLE = "tr_gateway_authorization"

# Importing GUARD_STATUSES would cycle because storage_gcp_settle_outbox imports
# the retention helpers below. Keep this SQL list in sync with that tuple.
_OUTBOX_GUARD_STATUS_SQL = "'pending', 'dead'"

_COMPLETE_GATEWAY_AUTHORIZATION_RETENTION_SQL = (
    "UPDATE tr_gateway_authorization SET terminal_at=@terminal_at "
    "WHERE authorization_id=@authorization_id AND settled=true "
    "AND terminal_at IS NULL"
)
_COMPLETE_GATEWAY_AUTHORIZATION_RETENTION_GUARDED_SQL = (
    "UPDATE tr_gateway_authorization SET terminal_at=@terminal_at "  # noqa: S608
    "WHERE authorization_id=@authorization_id AND settled=true "
    "AND terminal_at IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_gateway_authorization.authorization_id "
    f"AND o.status IN ({_OUTBOX_GUARD_STATUS_SQL}))"
)


def insert_gateway_authorization(
    transaction: Any,
    param_types: Any,
    authorization: GatewayAuthorization,
    *,
    created_at: Any,
) -> None:
    """Insert active authorization state in the caller's billing transaction."""
    payload = dataclasses.asdict(authorization)
    typed = authorization_typed_columns(payload)
    transaction.execute_update(
        "INSERT INTO tr_gateway_authorization ("
        "authorization_id, workspace_id, key_hash, reservation_id, model_id, "
        "provider, usage_type, estimated_microdollars, settled, created_at, "
        "terminal_at, payload, spend_lease_id, spend_lease_gen, "
        "spend_lease_allocated_micro, spend_lease_token, spend_lease_status, "
        "spend_lease_exp, idempotency_fingerprint, finalization_outcome, "
        "finalized_cost_microdollars"
        ") VALUES ("
        "@authorization_id, @workspace_id, @key_hash, @reservation_id, @model_id, "
        "@provider, @usage_type, @estimated_microdollars, false, @created_at, "
        "NULL, @payload, @spend_lease_id, @spend_lease_gen, "
        "@spend_lease_allocated_micro, @spend_lease_token, @spend_lease_status, "
        "@spend_lease_exp, @idempotency_fingerprint, @finalization_outcome, "
        "@finalized_cost_microdollars"
        ")",
        params={
            "authorization_id": authorization.id,
            "workspace_id": authorization.workspace_id,
            "key_hash": authorization.key_hash,
            "reservation_id": authorization.credit_reservation_id,
            "model_id": authorization.model_id,
            "provider": authorization.provider,
            "usage_type": str(authorization.usage_type),
            "estimated_microdollars": int(authorization.estimated_microdollars),
            "created_at": created_at,
            "payload": json_body(authorization),
            **typed,
        },
        param_types={
            "authorization_id": param_types.STRING,
            "workspace_id": param_types.STRING,
            "key_hash": param_types.STRING,
            "reservation_id": param_types.STRING,
            "model_id": param_types.STRING,
            "provider": param_types.STRING,
            "usage_type": param_types.STRING,
            "estimated_microdollars": param_types.INT64,
            "created_at": param_types.TIMESTAMP,
            "payload": param_types.STRING,
            **authorization_typed_param_types(param_types),
        },
    )


def read_gateway_authorization(
    reader: Any,
    param_types: Any,
    authorization_id: str,
) -> GatewayAuthorization | None:
    rows = list(
        reader.execute_sql(
            "SELECT authorization_id, workspace_id, key_hash, reservation_id, "
            "model_id, provider, usage_type, estimated_microdollars, settled, "
            "created_at, payload, spend_lease_id, spend_lease_gen, "
            "spend_lease_allocated_micro, spend_lease_token, spend_lease_status, "
            "spend_lease_exp, idempotency_fingerprint, finalization_outcome, "
            "finalized_cost_microdollars "
            "FROM tr_gateway_authorization "
            "WHERE authorization_id=@authorization_id",
            params={"authorization_id": authorization_id},
            param_types={"authorization_id": param_types.STRING},
        )
    )
    if not rows:
        return None
    (
        row_id,
        workspace_id,
        key_hash,
        reservation_id,
        model_id,
        provider,
        usage_type,
        estimated_microdollars,
        settled,
        created_at,
        payload,
        *typed_values,
    ) = rows[0]
    typed = dict(zip(AUTHORIZATION_TYPED_COLUMNS, typed_values, strict=True))
    if payload:
        authorization = _authorization_from_payload(
            json.dumps(merge_authorization_typed_columns(json.loads(payload), typed))
        )
        authorization.settled = bool(settled)
        authorization.created_at = _timestamp_string(created_at)
        return authorization
    merged = merge_authorization_typed_columns(None, typed)
    return GatewayAuthorization(
        id=str(row_id),
        workspace_id=str(workspace_id),
        key_hash=str(key_hash),
        model_id=str(model_id),
        provider=str(provider),
        usage_type=UsageType.coerce(usage_type),
        estimated_microdollars=int(estimated_microdollars),
        credit_reservation_id=(
            str(reservation_id) if reservation_id is not None else None
        ),
        settled=bool(settled),
        created_at=_timestamp_string(created_at),
        settlement=str(merged.get("settlement") or "local"),
        spend_lease_id=merged.get("spend_lease_id"),
        spend_lease_gen=merged.get("spend_lease_gen"),
        spend_lease_allocated_micro=merged.get("spend_lease_allocated_micro"),
        spend_lease_token=merged.get("spend_lease_token"),
        spend_lease_status=merged.get("spend_lease_status"),
        spend_lease_exp=merged.get("spend_lease_exp"),
        idempotency_fingerprint=merged.get("idempotency_fingerprint"),
        finalization_outcome=merged.get("finalization_outcome"),
        finalized_cost_microdollars=merged.get("finalized_cost_microdollars"),
    )


def mark_gateway_authorization_settled(
    transaction: Any,
    param_types: Any,
    authorization: GatewayAuthorization,
) -> int:
    """Mark billing settled while keeping repair metadata and TTL disabled."""
    typed = authorization_typed_columns(dataclasses.asdict(authorization))
    return transaction.execute_update(
        "UPDATE tr_gateway_authorization SET settled=true, payload=@payload, "
        "finalization_outcome=@finalization_outcome, "
        "finalized_cost_microdollars=@finalized_cost_microdollars "
        "WHERE authorization_id=@authorization_id",
        params={
            "authorization_id": authorization.id,
            "payload": json_body(authorization),
            "finalization_outcome": typed["finalization_outcome"],
            "finalized_cost_microdollars": typed["finalized_cost_microdollars"],
        },
        param_types={
            "authorization_id": param_types.STRING,
            "payload": param_types.STRING,
            "finalization_outcome": param_types.STRING,
            "finalized_cost_microdollars": param_types.INT64,
        },
    )


def complete_gateway_authorization_retention(
    transaction: Any,
    param_types: Any,
    authorization_id: str,
    *,
    terminal_at: Any,
    outbox_available: bool = True,
) -> int:
    """Start the retention clock for a settled, replayable authorization.

    The settled predicate guards active authorizations. The NULL predicate makes
    retries idempotent without extending the 30-day replay/audit window.
    """
    return transaction.execute_update(
        _COMPLETE_GATEWAY_AUTHORIZATION_RETENTION_GUARDED_SQL
        if outbox_available
        else _COMPLETE_GATEWAY_AUTHORIZATION_RETENTION_SQL,
        params={
            "authorization_id": authorization_id,
            "terminal_at": terminal_at,
        },
        param_types={
            "authorization_id": param_types.STRING,
            "terminal_at": param_types.TIMESTAMP,
        },
    )


def clear_gateway_authorization_retention(
    transaction: Any,
    param_types: Any,
    authorization_id: str,
) -> int:
    """Make an authorization TTL-ineligible while durable repair is outstanding."""
    return transaction.execute_update(
        "UPDATE tr_gateway_authorization SET terminal_at=NULL "
        "WHERE authorization_id=@authorization_id AND terminal_at IS NOT NULL",
        params={"authorization_id": authorization_id},
        param_types={"authorization_id": param_types.STRING},
    )


def close_reaped_gateway_authorization(
    transaction: Any,
    param_types: Any,
    authorization_id: str,
    *,
    terminal_at: Any,
) -> int:
    """Close an expired authorization whose hold was released by the reaper."""
    return transaction.execute_update(
        "UPDATE tr_gateway_authorization SET settled=true, terminal_at=@terminal_at, "
        "payload=NULL WHERE authorization_id=@authorization_id AND settled=false",
        params={
            "authorization_id": authorization_id,
            "terminal_at": terminal_at,
        },
        param_types={
            "authorization_id": param_types.STRING,
            "terminal_at": param_types.TIMESTAMP,
        },
    )


def _authorization_from_payload(payload: str) -> GatewayAuthorization:
    data = json.loads(payload)
    known = {field.name for field in dataclasses.fields(GatewayAuthorization)}
    return GatewayAuthorization(**{key: value for key, value in data.items() if key in known})


def _timestamp_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise TypeError("gateway authorization created_at is not a timestamp")
