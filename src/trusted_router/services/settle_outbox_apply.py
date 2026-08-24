from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import replace
from typing import Any, cast

from pydantic import ValidationError

from trusted_router.catalog import PROVIDERS, endpoint_for_id
from trusted_router.catalog_data import PARASAIL_LIBERTY_2_0_MODEL_ID
from trusted_router.custom_model_billing import (
    USER_MODEL_ID_SETTLE_FIELD,
    USER_MODEL_OWNER_SETTLE_FIELD,
    USER_MODEL_PAYOUT_SETTLE_FIELD,
    user_model_payout_event_id,
)
from trusted_router.partner_billing import PARTNER_OPERATOR_COST_SETTLE_FIELD
from trusted_router.regional_quota_ledger import RegionalLeaseLedgerError
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.services.regional_quota_leases import LeaseSettlementError
from trusted_router.storage import STORE, Generation, typed_billing_store
from trusted_router.storage_errors import transient_store_error_types
from trusted_router.storage_gcp_authorize import SettleOutcome
from trusted_router.storage_gcp_codec import (
    generation_workspace_id as _generation_workspace_id,
)
from trusted_router.storage_gcp_codec import json_body as _json_body
from trusted_router.storage_models import (
    GatewayAuthorization,
    SettleOutboxRow,
    UserModelPayout,
)
from trusted_router.types import UsageType

logger = logging.getLogger(__name__)

# Retryable infra failures. Typed-origin rows PARK on these (MF4/§6: a
# whole-backend outage must not burn attempts toward dead); legacy-origin rows
# map to ERROR (drain backoff). Anything else propagates — an unrecognized
# exception is a bug, and the drain's generic handler is the right place for it.
# The concrete exception classes are backend-specific, so `storage_errors`
# owns that mapping and this module stays free of cloud SDK imports. The set is
# unchanged: Spanner's Aborted/DeadlineExceeded/InternalServerError/
# ResourceExhausted (session-pool and admission-control overload)/RetryError/
# ServiceUnavailable, plus the backend-neutral StoreConflict/StoreUnavailable.
_TRANSIENT_STORE_EXCS = transient_store_error_types()
_REGIONAL_SETTLE_RETRY_EXCS: tuple[type[Exception], ...] = (
    *_TRANSIENT_STORE_EXCS,
    RegionalLeaseLedgerError,
    LeaseSettlementError,
)

# Rolling legacy rows can still carry this historical marker. New typed rows
# atomically enqueue ClickHouse delivery in the settlement transaction and do
# not park on the optional Bigtable migration mirror.
_ACTIVITY_PARK_NOTE = "bigtable activity index pending"


class ApplyOutcome:
    SETTLED_NOW = "settled_now"
    ACTIVITY_PENDING = "activity_pending"
    ALREADY_SETTLED_WITH_CHARGE = "already_settled_with_charge"
    RESOLVED_ZERO_COST_ELSEWHERE = "resolved_zero_cost_elsewhere"
    # Legacy origin cannot disambiguate a charged replay from a refund/
    # failure-settle free release (legacy Reservation records no actual
    # amount). Increment 4: mark done; flag for low-priority review when a
    # sibling refund-intent outbox row exists for the same authorization_id.
    ALREADY_SETTLED_LEGACY = "already_settled_legacy"
    # §3 outcome table: settle intent => invariant violation (reaper won);
    # refund intent => benign replay of a free release.
    ALREADY_RELEASED_FREE = "already_released_free"
    RESERVATION_MISSING = "reservation_missing"
    # MF4: typed-origin rows park when typed storage is capability-missing or
    # transiently unavailable. Never reroute them to legacy after enqueue,
    # because settle_origin is frozen.
    PARK_TYPED_UNAVAILABLE = "park_typed_unavailable"
    INVALID_ROW = "invalid_row"
    ERROR = "error"


def normalized_prompt_accounting(
    provider_slug: str, body: GatewaySettleRequest
) -> tuple[int, int, int, int]:
    input_tokens = body.input_count
    cache_read = body.cache_read_count
    cache_creation = body.cache_creation_count
    # Provider-dependent prompt accounting: Anthropic reports input_tokens
    # EXCLUSIVE of cached tokens (input 14 + cache_read 6081 = 6095-token
    # prompt), while OpenAI-compatible and Gemini prompt counts INCLUDE the
    # cached subset. Normalize to (uncached, read, creation) for pricing
    # and store the TOTAL prompt on the generation for honest dashboards.
    if cache_read or cache_creation:
        if provider_slug == "anthropic":
            uncached_input = input_tokens
            total_input = input_tokens + cache_read + cache_creation
        else:
            uncached_input = max(input_tokens - cache_read - cache_creation, 0)
            total_input = input_tokens
    else:
        uncached_input = total_input = input_tokens
    return uncached_input, total_input, cache_read, cache_creation


def apply_frozen_settle(row: SettleOutboxRow) -> str:
    """Apply one durable outbox row using only its frozen settle inputs.

    SF7: this dormant primitive is intentionally narrower than the HTTP settle
    handler. It must not import or call pricing, auto-refill, budget alert, or
    metadata broadcast code. A new typed settlement atomically writes the
    bounded generation record and ClickHouse delivery intent. Post-commit work
    is limited to loss-tolerant benchmark delivery and an optional migration
    mirror; rolling legacy rows still use index_after_commit repair. Increment
    4's drain interprets the rich §3 outcome and decides row status/alerting.
    """
    parsed_body = _parse_settle_body(row.settle_body)
    if parsed_body is None:
        return ApplyOutcome.INVALID_ROW
    try:
        body = GatewaySettleRequest(**parsed_body)
    except ValidationError:
        return ApplyOutcome.INVALID_ROW
    body_dict = body.model_dump(exclude_none=True)
    if row.intent_kind not in {"settle", "refund"}:
        return ApplyOutcome.INVALID_ROW
    if row.selected_usage_type is None:
        return ApplyOutcome.INVALID_ROW
    if row.settle_origin not in {"typed", "legacy"}:
        return ApplyOutcome.INVALID_ROW

    success = row.intent_kind == "settle"
    try:
        auth = STORE.get_gateway_authorization(row.authorization_id)
    except _TRANSIENT_STORE_EXCS:
        if row.settle_origin == "typed":
            return ApplyOutcome.PARK_TYPED_UNAVAILABLE
        return ApplyOutcome.ERROR
    if auth is None:
        return ApplyOutcome.RESERVATION_MISSING

    # Do not short-circuit auth.settled here. The claim/finalize layer is the
    # authority; this pre-read is only for body construction and is TOCTOU-prone.
    usage_type = UsageType.coerce(row.selected_usage_type)
    operator_cost_raw = body_dict.pop(PARTNER_OPERATOR_COST_SETTLE_FIELD, None)
    payout_raw = body_dict.pop(USER_MODEL_PAYOUT_SETTLE_FIELD, None)
    owner_raw = body_dict.pop(USER_MODEL_OWNER_SETTLE_FIELD, None)
    model_raw = body_dict.pop(USER_MODEL_ID_SETTLE_FIELD, None)
    try:
        operator_cost = (
            _operator_cost_microdollars(operator_cost_raw)
            if operator_cost_raw is not None
            else None
        )
        user_model_payout = _frozen_user_model_payout(
            auth,
            amount=payout_raw,
            owner_user_id=owner_raw,
            model_id=model_raw,
        )
        generation = (
            _frozen_generation(
                auth,
                row,
                body,
                body_dict,
                usage_type,
                operator_cost_microdollars=operator_cost,
            )
            if success
            else None
        )
    except (ValueError, TypeError):
        # MF3: deterministic-bad frozen rows dead-letter cleanly. Inline would
        # 500 at request time where the enclave retries; the drain must classify.
        return ApplyOutcome.INVALID_ROW

    if row.settle_origin == "typed":
        outcome = _apply_typed(
            row,
            auth,
            success,
            usage_type,
            generation,
            user_model_payout,
        )
    elif row.settle_origin == "legacy":
        outcome = _apply_legacy(
            row,
            success,
            usage_type,
            generation,
            user_model_payout,
        )
    else:
        return ApplyOutcome.INVALID_ROW
    # The inline settle releases the user-model concurrency slot after it
    # finalizes; when the inline attempt died before that (this row exists
    # because it did), the repair is the only thing left that can. Releasing
    # is idempotent and independent of the money outcome, so do it for every
    # terminal-or-already-terminal result rather than leaving the model at
    # capacity until the slot's ttl.
    _release_user_model_slot_safely(auth)
    return outcome


def _release_user_model_slot_safely(auth: GatewayAuthorization) -> None:
    model_id = auth.user_provided_model_id
    if not model_id:
        return
    try:
        STORE.release_user_model_slot(model_id, auth.id)
    except Exception:
        logger.warning(
            "user_model_slot_release_failed authorization_id=%s model_id=%s",
            auth.id,
            model_id,
            exc_info=True,
        )


def _parse_settle_body(raw: str | None) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw) if raw is not None else None
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _frozen_generation(
    auth: GatewayAuthorization,
    row: SettleOutboxRow,
    body: GatewaySettleRequest,
    body_dict: dict[str, Any],
    usage_type: UsageType,
    *,
    operator_cost_microdollars: int | None = None,
) -> Generation:
    # MF5: rebuild generation metadata from the row's frozen decision and
    # settle_body only. Retired endpoints fall back to parsing the stored id;
    # pricing/catalog drift must not change the amount or provider attribution.
    provider_slug = (
        "parasail"
        if row.model_id == PARASAIL_LIBERTY_2_0_MODEL_ID
        else _provider_slug(row.selected_endpoint_id)
    )
    provider_name = PROVIDERS[provider_slug].name if provider_slug in PROVIDERS else provider_slug
    _uncached_input, total_input, _cache_read, _cache_creation = normalized_prompt_accounting(
        provider_slug, body
    )
    return Generation.from_settle_body(
        authorization=auth,
        provider_name=provider_name,
        model_id=row.model_id,
        usage_type=usage_type,
        provider=provider_slug,
        body=body_dict,
        input_tokens=total_input,
        output_tokens=body.output_count,
        actual_cost_microdollars=row.actual_cost_micro,
        operator_cost_microdollars=operator_cost_microdollars,
    )


def _operator_cost_microdollars(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid frozen operator cost")
    return value


def _frozen_user_model_payout(
    auth: GatewayAuthorization,
    *,
    amount: Any,
    owner_user_id: Any,
    model_id: Any,
) -> UserModelPayout | None:
    fields = (amount, owner_user_id, model_id)
    if fields == (None, None, None):
        return None
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("invalid frozen user-model payout")
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        raise ValueError("invalid frozen user-model owner")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("invalid frozen user-model id")
    if not auth.workspace_id:
        raise ValueError("invalid payout workspace")
    return UserModelPayout(
        owner_user_id=owner_user_id,
        model_id=model_id,
        amount_microdollars=amount,
        payer_workspace_id=auth.workspace_id,
    )


def _provider_slug(endpoint_id: str | None) -> str:
    endpoint = endpoint_for_id(endpoint_id)
    if endpoint is not None:
        return endpoint.provider
    return _provider_slug_from_endpoint_id(endpoint_id)


def _provider_slug_from_endpoint_id(endpoint_id: str | None) -> str:
    if not endpoint_id or "@" not in endpoint_id:
        return "unknown"
    suffix = endpoint_id.rsplit("@", 1)[1]
    slug = suffix.split("/", 1)[0].strip()
    return slug or "unknown"


def _apply_typed(
    row: SettleOutboxRow,
    auth: GatewayAuthorization,
    success: bool,
    usage_type: UsageType,
    generation: Generation | None,
    user_model_payout: UserModelPayout | None,
) -> str:
    typed_store = typed_billing_store()
    if typed_store is None:
        # MF4: park typed-origin work until typed storage is available. Returning
        # here is read-only: no legacy reroute, no auth mark, no hold release.
        return ApplyOutcome.PARK_TYPED_UNAVAILABLE
    if auth.credit_reservation_id is None:
        return ApplyOutcome.RESERVATION_MISSING

    # A regional request must settle/refund its durable local hold before the
    # typed Spanner request record becomes terminal. Calling the lower-level
    # typed primitive directly would skip that step; the reconciler could then
    # release the grant as unused and turn an outbox-recovered request into a
    # free request. The wrapper is idempotent at both boundaries.
    if auth.settlement == "regional_lease":
        regional_finalize = getattr(
            typed_store,
            "typed_finalize_gateway_authorization_result",
            None,
        )
        if not callable(regional_finalize):
            return ApplyOutcome.PARK_TYPED_UNAVAILABLE
        try:
            existing_reservation = typed_store.read_typed_reservation(auth.credit_reservation_id)
            if existing_reservation is not None and existing_reservation.get("settled"):
                finalize_result = None
            else:
                finalize_result = regional_finalize(
                    auth.id,
                    success=success,
                    actual_microdollars=row.actual_cost_micro,
                    selected_usage_type=usage_type,
                    generation=generation,
                    user_model_payout=user_model_payout,
                )
        except _REGIONAL_SETTLE_RETRY_EXCS:
            # An opposing settle/refund can win the local row just before its
            # Spanner transaction. Park until that transaction commits, then
            # the replay classifier below can report the exact terminal result.
            return ApplyOutcome.PARK_TYPED_UNAVAILABLE
        if finalize_result is not None and finalize_result.finalized:
            return (
                ApplyOutcome.SETTLED_NOW
                if finalize_result.activity_indexed
                else ApplyOutcome.ACTIVITY_PENDING
            )
        # The wrapper returns false for an already-terminal request. Continue
        # through the existing exact replay classifier below.
        result: dict[str, Any] = {"outcome": SettleOutcome.ALREADY_SETTLED}
    else:
        result = {}

    generation_writes: list[tuple[str, str, str]] = []
    if success and generation is not None:
        generation_writes = [
            ("generation", generation.id, _json_body(generation)),
            (
                "generation_by_workspace",
                _generation_workspace_id(generation),
                _json_body({"generation_id": generation.id}),
            ),
        ]
    auth_settled = replace(auth)
    auth_settled.record_finalization(
        success=success,
        actual_microdollars=row.actual_cost_micro,
        selected_usage_type=usage_type,
        generation=generation,
    )
    if auth.settlement != "regional_lease":
        try:
            result = typed_store.typed_finalize_gateway(
                reservation_id=auth.credit_reservation_id,
                authorization_id=auth.id,
                success=success,
                actual_micro=row.actual_cost_micro,
                settled_usage_type=str(usage_type),
                now=dt.datetime.now(dt.UTC),
                authorization=auth_settled,
                auth_body_settled=_json_body(auth_settled),
                generation_writes=generation_writes,
                generation=generation,
                user_model_payout=user_model_payout,
            )
        except _TRANSIENT_STORE_EXCS:
            return ApplyOutcome.PARK_TYPED_UNAVAILABLE
    outcome = result.get("outcome")
    if outcome == SettleOutcome.SETTLED:
        # The typed transaction atomically persisted the bounded generation
        # record and operational analytics outbox row. Bigtable is only an
        # optional migration mirror and cannot keep settlement work pending.
        if success and generation is not None:
            generation_store = cast(Any, typed_store).generation_store
            if result.get("activity_durable"):
                generation_store.mirror_after_commit(generation)
            elif not _index_generation_after_commit(typed_store, generation):
                return ApplyOutcome.ACTIVITY_PENDING
        return ApplyOutcome.SETTLED_NOW
    if outcome == SettleOutcome.NOT_FOUND:
        return ApplyOutcome.RESERVATION_MISSING
    if outcome == SettleOutcome.ERROR:
        return ApplyOutcome.ERROR
    if outcome == SettleOutcome.ALREADY_SETTLED:
        try:
            reservation = typed_store.read_typed_reservation(auth.credit_reservation_id)
        except _TRANSIENT_STORE_EXCS:
            return ApplyOutcome.PARK_TYPED_UNAVAILABLE
        if reservation is None:
            return ApplyOutcome.RESERVATION_MISSING
        actual_micro = int(reservation.get("actual_micro") or 0)
        if actual_micro > 0:
            # Refunds never carry a generation, so requiring one here made the
            # benign charged-settle-beats-refund replay unreachable and
            # dead-lettered it, pinning retention on a non-problem (issue #356).
            if not success:
                return ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
            if generation is None:
                return ApplyOutcome.INVALID_ROW
            if not _index_generation_after_commit(typed_store, generation):
                return ApplyOutcome.ACTIVITY_PENDING
            return ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
        if row.actual_cost_micro == 0:
            # Deliberately index every available generation, including a genuine
            # zero-cost reaper/refund race. This writes no billing state: the
            # reservation already returned ALREADY_SETTLED, so no credit or key
            # counter moves. It is not activity-ONLY though — index_after_commit
            # also records a provider benchmark and, when enabled, enqueues an
            # analytics-outbox event. Both are non-billing and at-least-once by
            # design with stable ids, so a replay overwrites rather than
            # duplicates. A park-note discriminator is unsound: inline settle,
            # a lost lease before park(), and operator re-arm can all leave a
            # repairable row without the note, causing silent destruction of its
            # only typed payload. An accurate $0 activity row is better evidence
            # than none; failure stays ACTIVITY_PENDING and preserves the body.
            # The activity write is idempotent, so replay cannot duplicate it.
            if generation is not None:
                if not _index_generation_after_commit(typed_store, generation):
                    return ApplyOutcome.ACTIVITY_PENDING
            return ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE
        # Booked 0 while this row intended a real charge: the hold was resolved
        # WITHOUT our charge (reaper free-release, or a refund won the race).
        # For settle intent this is the §3 lost-charge signal; for refund intent
        # it is a benign replay — Increment 4 interprets outcome × intent_kind.
        return ApplyOutcome.ALREADY_RELEASED_FREE
    return ApplyOutcome.ERROR


def _apply_legacy(
    row: SettleOutboxRow,
    success: bool,
    usage_type: UsageType,
    generation: Generation | None,
    user_model_payout: UserModelPayout | None,
) -> str:
    try:
        finalized = STORE.finalize_gateway_authorization(
            row.authorization_id,
            success=success,
            actual_microdollars=row.actual_cost_micro,
            selected_usage_type=usage_type,
            generation=generation,
        )
    except ValueError:
        return ApplyOutcome.RESERVATION_MISSING
    except _TRANSIENT_STORE_EXCS:
        return ApplyOutcome.ERROR
    if finalized:
        if success and user_model_payout is not None and user_model_payout.amount_microdollars > 0:
            try:
                STORE.credit_user_earnings(
                    user_model_payout.owner_user_id,
                    user_model_payout.amount_microdollars,
                    user_model_payout_event_id(row.authorization_id),
                    custom_model_id=user_model_payout.model_id,
                    payer_workspace_id=user_model_payout.payer_workspace_id,
                )
            except Exception:
                logger.error(
                    "user_model_payout_failed authorization_id=%s owner=%s",
                    row.authorization_id,
                    user_model_payout.owner_user_id,
                    exc_info=True,
                )
        return ApplyOutcome.SETTLED_NOW
    # Legacy free releases do exist (inline refund/failure-settle). Only the
    # typed origin can disambiguate via the reservation's actual_micro.
    return ApplyOutcome.ALREADY_SETTLED_LEGACY


def _index_generation_after_commit(
    typed_store: Any,
    generation: Generation,
) -> bool:
    generation_store = cast(Any, typed_store).generation_store
    return bool(generation_store.index_after_commit(generation))
