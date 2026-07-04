from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import replace
from typing import Any, cast

from google.api_core.exceptions import (
    Aborted,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)
from pydantic import ValidationError

from trusted_router.catalog import PROVIDERS, endpoint_for_id
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.storage import STORE, Generation, typed_billing_store
from trusted_router.storage_gcp_authorize import SettleOutcome
from trusted_router.storage_gcp_codec import (
    generation_workspace_id as _generation_workspace_id,
)
from trusted_router.storage_gcp_codec import json_body as _json_body
from trusted_router.storage_models import GatewayAuthorization, SettleOutboxRow
from trusted_router.types import UsageType

logger = logging.getLogger(__name__)

# Retryable infra failures. Typed-origin rows PARK on these (MF4/§6: a
# whole-backend outage must not burn attempts toward dead); legacy-origin rows
# map to ERROR (drain backoff). Anything else propagates — an unrecognized
# exception is a bug, and the drain's generic handler is the right place for it.
# ResourceExhausted covers Spanner session-pool/admission-control overload.
# RetryError subclasses GoogleAPIError, not GoogleAPICallError, so list it here.
_TRANSIENT_STORE_EXCS = (
    Aborted,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)


class ApplyOutcome:
    SETTLED_NOW = "settled_now"
    ALREADY_SETTLED_WITH_CHARGE = "already_settled_with_charge"
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
    metadata broadcast code. The one side effect it does fire is the
    claim-gated, at-most-once Bigtable activity index and provider-benchmark
    sample via index_after_commit on SETTLED_NOW, matching the inline typed path;
    replay outcomes do not fire it. Increment 4's drain will interpret the rich
    §3 outcome and decide row status/alerting.
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
    try:
        generation = _frozen_generation(auth, row, body, body_dict, usage_type) if success else None
    except (ValueError, TypeError):
        # MF3: deterministic-bad frozen rows dead-letter cleanly. Inline would
        # 500 at request time where the enclave retries; the drain must classify.
        return ApplyOutcome.INVALID_ROW

    if row.settle_origin == "typed":
        return _apply_typed(row, auth, success, usage_type, generation)
    if row.settle_origin == "legacy":
        return _apply_legacy(row, success, usage_type, generation)
    return ApplyOutcome.INVALID_ROW


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
) -> Generation:
    # MF5: rebuild generation metadata from the row's frozen decision and
    # settle_body only. Retired endpoints fall back to parsing the stored id;
    # pricing/catalog drift must not change the amount or provider attribution.
    provider_slug = _provider_slug(row.selected_endpoint_id)
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
) -> str:
    typed_store = typed_billing_store()
    if typed_store is None:
        # MF4: park typed-origin work until typed storage is available. Returning
        # here is read-only: no legacy reroute, no auth mark, no hold release.
        return ApplyOutcome.PARK_TYPED_UNAVAILABLE
    if auth.credit_reservation_id is None:
        return ApplyOutcome.RESERVATION_MISSING

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
    auth_settled = replace(auth, settled=True)
    try:
        result = typed_store.typed_finalize_gateway(
            reservation_id=auth.credit_reservation_id,
            authorization_id=auth.id,
            success=success,
            actual_micro=row.actual_cost_micro,
            settled_usage_type=str(usage_type),
            now=dt.datetime.now(dt.UTC),
            auth_body_settled=_json_body(auth_settled),
            generation_writes=generation_writes,
        )
    except _TRANSIENT_STORE_EXCS:
        return ApplyOutcome.PARK_TYPED_UNAVAILABLE
    outcome = result.get("outcome")
    if outcome == SettleOutcome.SETTLED:
        if success and generation is not None:
            _index_generation_after_commit(typed_store, generation)
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
        if actual_micro > 0 or row.actual_cost_micro == 0:
            # Charged replay — or our own frozen cost is 0, in which case
            # whatever resolved the hold produced a state identical to applying
            # this row (booking 0 == booking nothing): nothing was lost, benign.
            # Parity with the inline path's known post-commit index hole; the
            # drain's retry-after-ambiguous-failure purpose makes it likelier.
            logger.info(
                "drain replay of a charged settle; if the charge was committed by a finalize whose response was lost (park->retry), its Bigtable activity-index entry may be absent; repairable via reconcile_activity"
            )
            return ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
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
        return ApplyOutcome.SETTLED_NOW
    # Legacy free releases do exist (inline refund/failure-settle). Only the
    # typed origin can disambiguate via the reservation's actual_micro.
    return ApplyOutcome.ALREADY_SETTLED_LEGACY


def _index_generation_after_commit(typed_store: Any, generation: Generation) -> None:
    generation_store = cast(Any, typed_store).generation_store
    generation_store.index_after_commit(generation)
