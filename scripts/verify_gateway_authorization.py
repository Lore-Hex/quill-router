#!/usr/bin/env python3
"""Verify one gateway authorization without scanning production request tables."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from typing import Any

from google.cloud import spanner
from google.cloud.spanner_v1 import param_types

RESERVATION_BY_SCOPE_SQL = (
    "SELECT reservation_id, authorization_id "
    "FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_idemp} "
    "WHERE idempotency_scope=@scope"
)
AUTHORIZATION_BY_ID_SQL = (
    "SELECT authorization_id, workspace_id, key_hash, reservation_id, model_id, provider, "
    "usage_type, estimated_microdollars, settled, created_at, terminal_at, payload, "
    "stage_d_boot_kid, heartbeat_seq, invocation_nonce "
    "FROM tr_gateway_authorization WHERE authorization_id=@authorization_id"
)
RESERVATION_BY_ID_SQL = (
    "SELECT reservation_id, workspace_id, key_hash, credit_reserved_micro, "
    "key_reserved_micro, actual_micro, hold_usage_type, settled_usage_type, settled, "
    "created_at, expires_at, terminal_at FROM tr_reservation "
    "WHERE reservation_id=@reservation_id"
)
OUTBOX_BY_AUTHORIZATION_SQL = (
    "SELECT intent_kind, settle_origin, actual_cost_micro, model_id, selected_usage_type, "
    "status, attempts, created_at, updated_at, terminal_at FROM tr_settle_outbox "
    "WHERE authorization_id=@authorization_id ORDER BY intent_kind"
)
GENERATION_BY_ID_SQL = (
    "SELECT generation_id, workspace_id, key_hash, created_at, terminal_at, payload "
    "FROM tr_generation WHERE generation_id=@generation_id"
)


def idempotency_scope(workspace_id: str, key_hash: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"{workspace_id}#{key_hash}#{digest}"


def _one(rows: Any, *, label: str) -> tuple[Any, ...]:
    materialized = list(rows)
    if len(materialized) != 1:
        raise RuntimeError(f"expected one {label} row, found {len(materialized)}")
    return tuple(materialized[0])


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _authorization_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    payload = _json_object(row[11])
    return {
        "authorization_id": row[0],
        "workspace_id": row[1],
        "key_hash": row[2],
        "reservation_id": row[3],
        "model_id": row[4],
        "provider": row[5],
        "usage_type": row[6],
        "estimated_microdollars": row[7],
        "settled": row[8],
        "created_at": _json_safe(row[9]),
        "terminal_at": _json_safe(row[10]),
        "finalization_outcome": payload.get("finalization_outcome"),
        "finalized_cost_microdollars": payload.get("finalized_cost_microdollars"),
        "finalized_generation_id": payload.get("finalized_generation_id"),
        "finalized_model_id": payload.get("finalized_model_id"),
        "finalized_provider": payload.get("finalized_provider"),
        "finalized_region": payload.get("finalized_region"),
        "stage_d_boot_kid": row[12],
        "heartbeat_seq": row[13],
        "invocation_nonce": row[14],
    }


def verify_authorization(
    snapshot: Any,
    *,
    workspace_id: str | None = None,
    key_hash: str | None = None,
    idempotency_key: str | None = None,
    authorization_id: str | None = None,
) -> dict[str, Any]:
    if authorization_id is None:
        if not (workspace_id and key_hash and idempotency_key):
            raise ValueError("workspace_id, key_hash, and idempotency_key are required together")
        scope = idempotency_scope(workspace_id, key_hash, idempotency_key)
        reservation_ref = _one(
            snapshot.execute_sql(
                RESERVATION_BY_SCOPE_SQL,
                params={"scope": scope},
                param_types={"scope": param_types.STRING},
            ),
            label="idempotency reservation",
        )
        reservation_id, authorization_id = reservation_ref
    else:
        reservation_id = None

    authorization_row = _one(
        snapshot.execute_sql(
            AUTHORIZATION_BY_ID_SQL,
            params={"authorization_id": authorization_id},
            param_types={"authorization_id": param_types.STRING},
        ),
        label="gateway authorization",
    )
    authorization = _authorization_summary(authorization_row)
    reservation_id = reservation_id or authorization["reservation_id"]
    reservation_row = _one(
        snapshot.execute_sql(
            RESERVATION_BY_ID_SQL,
            params={"reservation_id": reservation_id},
            param_types={"reservation_id": param_types.STRING},
        ),
        label="reservation",
    )
    reservation = {
        "reservation_id": reservation_row[0],
        "workspace_id": reservation_row[1],
        "key_hash": reservation_row[2],
        "credit_reserved_microdollars": reservation_row[3],
        "key_reserved_microdollars": reservation_row[4],
        "actual_microdollars": reservation_row[5],
        "hold_usage_type": reservation_row[6],
        "settled_usage_type": reservation_row[7],
        "settled": reservation_row[8],
        "created_at": _json_safe(reservation_row[9]),
        "expires_at": _json_safe(reservation_row[10]),
        "terminal_at": _json_safe(reservation_row[11]),
    }
    outbox_rows = snapshot.execute_sql(
        OUTBOX_BY_AUTHORIZATION_SQL,
        params={"authorization_id": authorization_id},
        param_types={"authorization_id": param_types.STRING},
    )
    outbox = [
        {
            "intent_kind": row[0],
            "settle_origin": row[1],
            "actual_cost_microdollars": row[2],
            "model_id": row[3],
            "selected_usage_type": row[4],
            "status": row[5],
            "attempts": row[6],
            "created_at": _json_safe(row[7]),
            "updated_at": _json_safe(row[8]),
            "terminal_at": _json_safe(row[9]),
        }
        for row in outbox_rows
    ]

    generation = None
    generation_id = authorization.get("finalized_generation_id")
    if generation_id:
        generation_row = _one(
            snapshot.execute_sql(
                GENERATION_BY_ID_SQL,
                params={"generation_id": generation_id},
                param_types={"generation_id": param_types.STRING},
            ),
            label="generation",
        )
        payload = _json_object(generation_row[5])
        generation = {
            "generation_id": generation_row[0],
            "workspace_id": generation_row[1],
            "key_hash": generation_row[2],
            "created_at": _json_safe(generation_row[3]),
            "terminal_at": _json_safe(generation_row[4]),
            "model": payload.get("model"),
            "provider": payload.get("provider"),
            "usage_type": payload.get("usage_type"),
            "status": payload.get("status"),
            "finish_reason": payload.get("finish_reason"),
            "cost_microdollars": payload.get("total_cost_microdollars"),
        }

    return {
        "authorization": authorization,
        "reservation": reservation,
        "outbox": outbox,
        "generation": generation,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID", "quill-cloud-proxy"))
    parser.add_argument(
        "--instance", default=os.getenv("SPANNER_INSTANCE_ID", "trusted-router-nam6")
    )
    parser.add_argument("--database", default=os.getenv("SPANNER_DATABASE_ID", "trusted-router"))
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--authorization-id")
    selector.add_argument("--idempotency-key")
    parser.add_argument("--workspace-id")
    parser.add_argument("--key-hash")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.idempotency_key and not (args.workspace_id and args.key_hash):
        raise SystemExit("--workspace-id and --key-hash are required with --idempotency-key")
    client = spanner.Client(project=args.project, disable_builtin_metrics=True)
    database = client.instance(args.instance).database(args.database)
    with database.snapshot(multi_use=True) as snapshot:
        result = verify_authorization(
            snapshot,
            workspace_id=args.workspace_id,
            key_hash=args.key_hash,
            idempotency_key=args.idempotency_key,
            authorization_id=args.authorization_id,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
