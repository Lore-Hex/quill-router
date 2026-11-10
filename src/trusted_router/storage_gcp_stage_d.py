"""Atomic typed-Spanner persistence for Stage D usage heartbeats."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from trusted_router.app_markup_billing import app_markup_microdollars
from trusted_router.catalog import endpoint_provider_and_model
from trusted_router.pricing import signed_receipt_price_microdollars
from trusted_router.provider_contracts import provider_input_excludes_cache
from trusted_router.stage_d import (
    billing_pricing_snapshot,
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
        _reservation_id, _credit_reserved_micro, reservation_settled, expires_at = rows[0]
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
            stored_usage = delivered_usage_counts(authorization.delivered_usage)
            document = parse_pricing_snapshot(authorization.pricing_snapshot)
            cap_micro = _cap_micro(authorization)
            stored_endpoint_id = str(authorization.selected_endpoint_id)
            return HeartbeatResult(
                accepted=True,
                seq=seq,
                expires_at_ms=_epoch_millis(expires_at),
                cap_micro=cap_micro,
                running_micro=delivered_usage_charge_microdollars(
                    authorization,
                    document,
                    _selected_provider(authorization, stored_endpoint_id),
                    stored_endpoint_id,
                    stored_usage,
                ),
                replay=True,
            )

        if seq > 1 and selected_endpoint_id != authorization.selected_endpoint_id:
            return reject("endpoint_mismatch")
        stored_usage = delivered_usage_counts(authorization.delivered_usage)
        if any(int(usage[name]) < int(stored_usage[name]) for name in usage):
            return reject("usage_regression")
        if _usage_exceeds_authorized_tokens(authorization, usage, selected_endpoint_id):
            return reject("usage_exceeds_cap")

        document = parse_pricing_snapshot(authorization.pricing_snapshot)
        try:
            running_micro = delivered_usage_charge_microdollars(
                authorization,
                document,
                _selected_provider(authorization, selected_endpoint_id),
                selected_endpoint_id,
                usage,
            )
        except ValueError:
            return reject("endpoint_mismatch")
        cap_micro = _cap_micro(authorization)
        if running_micro > cap_micro:
            return reject("usage_exceeds_cap")
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


def delivered_usage_counts(value: str | None) -> dict[str, int]:
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


def normalized_delivered_prompt(
    provider: str, usage: dict[str, int], *, model_id: str | None = None
) -> tuple[int, int]:
    """(uncached input, total prompt) of a stream's delivered usage, by the
    settle path's own convention rule over the provider slug and the model id
    (provider_contracts.provider_input_excludes_cache; a caller passing the
    slug alone gets the slug's answer)."""
    reported = int(usage["input_tokens"])
    cached = int(usage["cache_read_input_tokens"])
    created = int(usage["cache_creation_input_tokens"])
    if cached or created:
        if provider_input_excludes_cache(provider, model_id):
            return reported, reported + cached + created
        return max(reported - cached - created, 0), reported
    return reported, reported


def delivered_usage_cost_microdollars(
    document: dict[str, Any],
    provider: str,
    endpoint_id: str,
    usage: dict[str, int],
    *,
    model_id: str | None = None,
) -> int:
    uncached, _total_prompt = normalized_delivered_prompt(provider, usage, model_id=model_id)
    return endpoint_cost_microdollars_from_document(
        document,
        endpoint_id,
        uncached,
        int(usage["output_tokens"]),
        cache_read_tokens=int(usage["cache_read_input_tokens"]),
        cache_creation_tokens=int(usage["cache_creation_input_tokens"]),
        price_tier_input_tokens=(int(usage["price_tier_input_tokens"]) or None),
    )


def delivered_usage_charge_microdollars(
    authorization: Any,
    document: dict[str, Any],
    provider: str,
    endpoint_id: str,
    usage: dict[str, int],
) -> int:
    """Price a heartbeat through the same frozen downstream fee layers as settle."""

    model_id = _selected_model_id(authorization, endpoint_id)
    billing_snapshot = billing_pricing_snapshot(authorization)
    if billing_snapshot is not None:
        # Only Fugu's pinned contract admits a private prompt-tier basis, just
        # as inline settlement and the gateway do. Preserve legacy cohorts.
        if provider != "sakana" or model_id != "sakana-ai/fugu-ultra-v1.1":
            usage = {**usage, "price_tier_input_tokens": 0}
        document = billing_snapshot
    charge = delivered_usage_cost_microdollars(
        document,
        provider,
        endpoint_id,
        usage,
        model_id=model_id,
    )
    receipt_fee_basis_points = int(authorization.receipt_fee_basis_points or 0)
    if receipt_fee_basis_points > 0:
        charge = signed_receipt_price_microdollars(
            charge,
            receipt_fee_basis_points,
        )
    app_markup_basis_points = int(authorization.app_markup_basis_points or 0)
    if app_markup_basis_points > 0:
        charge += app_markup_microdollars(charge, app_markup_basis_points)
    return charge


def _usage_exceeds_authorized_tokens(
    authorization: Any, usage: dict[str, int], selected_endpoint_id: str
) -> bool:
    prompt_limit = authorization.stage_d_prompt_tokens
    output_limit = authorization.stage_d_max_output_tokens
    if prompt_limit is None or output_limit is None:
        return True
    # The delivered prompt is read in the selected endpoint's convention, as
    # the running charge below reads it.
    _uncached, total_prompt = normalized_delivered_prompt(
        _selected_provider(authorization, selected_endpoint_id),
        usage,
        model_id=_selected_model_id(authorization, selected_endpoint_id),
    )
    return total_prompt + int(usage["output_tokens"]) > int(prompt_limit) + int(output_limit)


def _cap_micro(authorization: Any) -> int:
    """The spend ceiling is the authorization's estimate, not the reservation.

    The reservation row's credit_reserved_micro is the credit hold: for a
    catalog route, a stream included, authorize prices it at no more than
    HOLD_OUTPUT_TOKENS of the output budget (authorize_candidates.
    hold_output_tokens). The estimate prices the whole budget, and it is what the gateway exports as
    cap_micro and cuts a stream at. Capping the running charge at the
    reservation would end a stream whose charge passed its credit hold,
    however many tokens that is.
    """
    return int(authorization.estimated_microdollars)


def selected_endpoint_identity(authorization: Any, endpoint_id: str | None) -> tuple[str, str]:
    """The (provider slug, router model id) a stream's delivered usage is
    billed in: the selected endpoint's, read from the catalog or, once the
    endpoint has left it, from the id's own shape
    (catalog.endpoint_provider_and_model);
    the authorization's own fields only for an id of neither kind. The
    selected endpoint may be a fallback candidate on another provider than
    the authorization names, so the authorization's fields are the last
    resort, not the first."""
    identity = endpoint_provider_and_model(endpoint_id)
    if identity is not None:
        return identity
    return str(authorization.provider), str(authorization.model_id)


def _selected_provider(authorization: Any, endpoint_id: str) -> str:
    return selected_endpoint_identity(authorization, endpoint_id)[0]


def _selected_model_id(authorization: Any, endpoint_id: str) -> str:
    return selected_endpoint_identity(authorization, endpoint_id)[1]


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
