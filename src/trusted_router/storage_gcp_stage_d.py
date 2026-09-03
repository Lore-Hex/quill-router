"""Atomic typed-Spanner persistence for Stage D usage heartbeats."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_document,
    parse_pricing_snapshot,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import read_gateway_authorization

HeartbeatReason = Literal[
    "unknown_authorization",
    "already_terminal",
    "out_of_cohort",
    "stale_seq",
    "endpoint_mismatch",
    "usage_regression",
    "usage_exceeds_cap",
]


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    accepted: bool
    reason: HeartbeatReason | None = None
    seq: int | None = None
    expires_at_ms: int | None = None
    cap_micro: int | None = None
    running_micro: int | None = None
    replay: bool = False


class _RollbackHeartbeat(Exception):
    def __init__(self, result: HeartbeatResult) -> None:
        self.result = result


def heartbeat_gateway_atomic(
    database: Any,
    param_types: Any,
    *,
    authorization_id: str,
    seq: int,
    started_at: datetime,
    selected_endpoint_id: str,
    usage: dict[str, int],
    heartbeat_hash: str,
    stream: bool,
    grace_seconds: int,
    now: datetime | None = None,
) -> HeartbeatResult:
    """Read, validate, snapshot usage, and renew the hold in one transaction."""

    heartbeat_at = _utc(now or datetime.now(UTC))
    requested_renewal = heartbeat_at + timedelta(seconds=int(grace_seconds))
    delivered_usage = json.dumps(usage, sort_keys=True, separators=(",", ":"))

    def reject(reason: HeartbeatReason) -> HeartbeatResult:
        return HeartbeatResult(accepted=False, reason=reason)

    def txn(transaction: Any) -> HeartbeatResult:
        authorization = read_gateway_authorization(
            transaction,
            param_types,
            authorization_id,
        )
        if authorization is None or authorization.credit_reservation_id is None:
            return reject("unknown_authorization")
        rows = list(
            transaction.execute_sql(
                "SELECT reservation_id, credit_reserved_micro, settled, expires_at "
                "FROM tr_reservation WHERE reservation_id=@rid",
                params={"rid": authorization.credit_reservation_id},
                param_types={"rid": param_types.STRING},
            )
        )
        if not rows:
            return reject("unknown_authorization")
        _reservation_id, credit_reserved_micro, reservation_settled, expires_at = rows[0]
        if authorization.settled or bool(reservation_settled):
            return reject("already_terminal")
        if authorization.pricing_snapshot is None or not stream:
            return reject("out_of_cohort")

        stored_seq = int(authorization.heartbeat_seq or 0)
        if seq < stored_seq:
            return reject("stale_seq")
        if seq == stored_seq:
            if authorization.heartbeat_hash != heartbeat_hash:
                return reject("stale_seq")
            if stored_seq == 0:
                return reject("stale_seq")
            stored_usage = _stored_usage(authorization.delivered_usage)
            document = parse_pricing_snapshot(authorization.pricing_snapshot)
            cap_micro = _cap_micro(authorization, credit_reserved_micro)
            return HeartbeatResult(
                accepted=True,
                seq=seq,
                expires_at_ms=_epoch_millis(expires_at),
                cap_micro=cap_micro,
                running_micro=_running_micro(
                    document,
                    authorization.provider,
                    str(authorization.selected_endpoint_id),
                    stored_usage,
                ),
                replay=True,
            )

        if seq > 1 and selected_endpoint_id != authorization.selected_endpoint_id:
            return reject("endpoint_mismatch")
        stored_usage = _stored_usage(authorization.delivered_usage)
        if any(int(usage[name]) < int(stored_usage[name]) for name in usage):
            return reject("usage_regression")
        if _usage_exceeds_authorized_tokens(authorization, usage):
            return reject("usage_exceeds_cap")

        document = parse_pricing_snapshot(authorization.pricing_snapshot)
        try:
            running_micro = _running_micro(
                document,
                authorization.provider,
                selected_endpoint_id,
                usage,
            )
        except ValueError:
            return reject("endpoint_mismatch")
        cap_micro = _cap_micro(authorization, credit_reserved_micro)
        updated = transaction.execute_update(
            "UPDATE tr_gateway_authorization SET heartbeat_seq=@seq, "
            "heartbeat_at=@heartbeat_at, heartbeat_hash=@heartbeat_hash, "
            "delivered_usage=@delivered_usage, "
            "started_at=IF(@seq=1 AND started_at IS NULL,@started_at,started_at), "
            "selected_endpoint_id=IF(@seq=1 AND selected_endpoint_id IS NULL,"
            "@selected_endpoint_id,selected_endpoint_id) "
            "WHERE authorization_id=@authorization_id AND settled=false "
            "AND COALESCE(heartbeat_seq,0)<@seq",
            params={
                "authorization_id": authorization_id,
                "seq": int(seq),
                "heartbeat_at": heartbeat_at,
                "heartbeat_hash": heartbeat_hash,
                "delivered_usage": delivered_usage,
                "started_at": _utc(started_at),
                "selected_endpoint_id": selected_endpoint_id,
            },
            param_types={
                "authorization_id": param_types.STRING,
                "seq": param_types.INT64,
                "heartbeat_at": param_types.TIMESTAMP,
                "heartbeat_hash": param_types.STRING,
                "delivered_usage": param_types.STRING,
                "started_at": param_types.TIMESTAMP,
                "selected_endpoint_id": param_types.STRING,
            },
        )
        if int(updated) != 1:
            raise _RollbackHeartbeat(reject("stale_seq"))
        renewed = transaction.execute_update(
            "UPDATE tr_reservation SET expires_at=GREATEST(expires_at,@renewed_expires_at) "
            "WHERE reservation_id=@rid AND settled=false",
            params={
                "rid": authorization.credit_reservation_id,
                "renewed_expires_at": requested_renewal,
            },
            param_types={
                "rid": param_types.STRING,
                "renewed_expires_at": param_types.TIMESTAMP,
            },
        )
        if int(renewed) != 1:
            raise _RollbackHeartbeat(reject("already_terminal"))
        effective_expiry = max(_utc_datetime(expires_at), requested_renewal)
        return HeartbeatResult(
            accepted=True,
            seq=seq,
            expires_at_ms=_epoch_millis(effective_expiry),
            cap_micro=cap_micro,
            running_micro=running_micro,
        )

    try:
        return run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_stage_d_heartbeat",
        )
    except _RollbackHeartbeat as rejected:
        return rejected.result


def _stored_usage(value: str | None) -> dict[str, int]:
    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "price_tier_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    if value is None:
        return empty
    parsed = json.loads(value)
    return {name: int(parsed.get(name, 0)) for name in empty}


def _priced_prompt(provider: str, usage: dict[str, int]) -> tuple[int, int]:
    reported = int(usage["input_tokens"])
    cached = int(usage["cache_read_input_tokens"])
    created = int(usage["cache_creation_input_tokens"])
    if cached or created:
        if provider == "anthropic":
            return reported, reported + cached + created
        return max(reported - cached - created, 0), reported
    return reported, reported


def _running_micro(
    document: dict[str, Any],
    provider: str,
    endpoint_id: str,
    usage: dict[str, int],
) -> int:
    uncached, _total_prompt = _priced_prompt(provider, usage)
    return endpoint_cost_microdollars_from_document(
        document,
        endpoint_id,
        uncached,
        int(usage["output_tokens"]),
        cache_read_tokens=int(usage["cache_read_input_tokens"]),
        cache_creation_tokens=int(usage["cache_creation_input_tokens"]),
        price_tier_input_tokens=(int(usage["price_tier_input_tokens"]) or None),
    )


def _usage_exceeds_authorized_tokens(authorization: Any, usage: dict[str, int]) -> bool:
    prompt_limit = authorization.stage_d_prompt_tokens
    output_limit = authorization.stage_d_max_output_tokens
    if prompt_limit is None or output_limit is None:
        return True
    _uncached, total_prompt = _priced_prompt(authorization.provider, usage)
    return total_prompt + int(usage["output_tokens"]) > int(prompt_limit) + int(output_limit)


def _cap_micro(authorization: Any, credit_reserved_micro: Any) -> int:
    cap = int(credit_reserved_micro)
    if authorization.spend_lease_allocated_micro is not None:
        cap = min(cap, int(authorization.spend_lease_allocated_micro))
    return cap


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise TypeError("reservation expiry is not a timestamp")


def _epoch_millis(value: Any) -> int:
    return int(_utc_datetime(value).timestamp() * 1000)
