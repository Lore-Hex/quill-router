"""Strict async-v1 boundary and same-snapshot synchronous reconciliation."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_ticket import TicketClaims, verify_lookup_ticket, verify_ticket
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.async_settle import CACHE_SECONDS, TIER_CAPS, Runtime
from trusted_router.services.settle_outbox_drain import spanner_settle_outbox
from trusted_router.storage import STORE, typed_billing_store
from trusted_router.storage_errors import transient_store_error_types
from trusted_router.storage_gcp_async_settle import ReservationNotOpen, enqueue
from trusted_router.storage_gcp_io import spanner_rpc_deadline
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox, _is_already_exists
from trusted_router.storage_models import SettleOutboxRow

logger = logging.getLogger(__name__)


def metric(event: str, *, reason: str = "", value: float = 1) -> None:
    # Aggregate structured logs follow the existing billing latency plumbing.
    # No authorization/workspace/key IDs or user-supplied labels.
    logger.info("metric=async_settle_%s reason=%s value=%s", event, reason, value,
                extra={"metric": "async_settle_" + event, "reason": reason, "value": value})


def sync_required(reason: str) -> JSONResponse:
    metric("sync_required", reason=reason)
    return JSONResponse({"data": {"acceptance": {"status": "sync_required", "payload_hash": None,
                                               "settlement_status": None}, "reason": reason}})


def admission_reason(runtime: Runtime, workspace_id: str, pilot_cap: int) -> str:
    """Diagnostics only after eligible() denied; never confers admission."""
    cache = runtime.admission
    if cache is None or not cache.lock.acquire(blocking=False):
        return "admission_stale"
    try:
        health = cache.health
        if health is None or health.p95_age_seconds > 5:
            return "drain_unhealthy"
        now = cache.clock()
        entry = cache.entries.get(workspace_id)
        if (not 0 <= now - health.observed_at < CACHE_SECONDS or entry is None
                or not 0 <= now - entry[0] < CACHE_SECONDS or entry[1] is None):
            return "admission_stale"
        value = entry[1]
        if type(value.tier) is not int or value.tier not in TIER_CAPS:
            return "not_eligible"
        if type(value.pending_micro) is not int or value.pending_micro < 0:
            return "admission_stale"
        if value.pending_micro > (pilot_cap or TIER_CAPS[value.tier]):
            return "cap_exceeded"
        return "admission_stale"
    finally:
        cache.lock.release()


def unavailable() -> HTTPException:
    return api_error(503, "Persistent storage is temporarily unavailable; retry.",
                     "service_unavailable", headers={"Retry-After": "1"})


@dataclass(frozen=True)
class Input:
    snapshot: billing.BillingSnapshot
    terminal: billing.TerminalEnvelope
    raw: billing.RawUsage
    observed: billing.Eligibility
    ticket: str


def parse(raw: bytes) -> Input:
    try:
        obj = billing.strict_json_loads(raw)
        if (not isinstance(obj, dict) or set(obj) != {
                "billing_snapshot", "settlement_ticket", "raw_usage", "observed", "terminal"}
                or not isinstance(obj["settlement_ticket"], str)):
            raise ValueError("outer shape")
        return Input(billing.parse_snapshot(json.dumps(obj["billing_snapshot"])),
                     billing.parse_envelope(json.dumps(obj["terminal"])),
                     billing.RawUsage.model_validate(obj["raw_usage"]),
                     billing.parse_eligibility(json.dumps(obj["observed"])), obj["settlement_ticket"])
    except (ValueError, TypeError, OverflowError) as exc:
        raise api_error(400, "Invalid async settlement snapshot", "bad_request") from exc


def verify(value: Input, runtime: Runtime, kind: str, now: int) -> TicketClaims:
    try:
        if runtime.signer is None:
            raise ValueError("ticket key unavailable")
        claims = verify_lookup_ticket(value.ticket, [runtime.signer.trusted], now)
        terminal = value.terminal.model_dump(mode="json")
        bindings = claims.model_dump(mode="json")
        if (value.terminal.terminal_kind != kind or any(
                terminal[k] != bindings[k] for k in terminal.keys() & bindings.keys())
                or value.observed.route_type != claims.route_type
                or value.observed.streamed != claims.streamed):
            raise ValueError("terminal binding")
    except (ValueError, TypeError) as exc:
        raise api_error(401, "Invalid settlement ticket", "unauthorized") from exc
    if billing.canonical_hash(value.snapshot) != claims.snapshot_hash:
        metric("hash_mismatch")
        raise api_error(400, "Invalid async settlement snapshot", "bad_request")
    return claims


def price(value: Input) -> int:
    try:
        result = billing.evaluate(value.snapshot, value.terminal.selected_endpoint,
                                  value.raw, value.observed)
        amount = result.charge_micro if value.terminal.terminal_kind == "settle" else 0
        if amount != value.terminal.charge_micro:
            metric("amount_mismatch")
            raise api_error(409, "Async settlement amount mismatch", "conflict", extra={
                "expected_cost_microdollars": amount,
                "claimed_cost_microdollars": value.terminal.charge_micro})
        if result.usage != value.terminal.usage:
            raise ValueError("usage mismatch")
        billing.validate_envelope(value.snapshot, value.terminal)
        return amount
    except (ValueError, OverflowError) as exc:
        raise api_error(400, "Invalid async settlement snapshot", "bad_request") from exc


def intent(value: Input, claims: TicketClaims, amount: int) -> SettleOutboxRow:
    terminal = value.terminal
    candidate = next(c for c in value.snapshot.candidates if c.endpoint_id == terminal.selected_endpoint)
    if len(candidate.endpoint_id) > 128 or len(candidate.model_id) > 128:
        raise api_error(400, "Invalid async settlement snapshot", "bad_request")
    usage = terminal.usage
    # The existing repair primitive interprets Anthropic inputs as uncached,
    # other adapters as total prompt. Map conventions once at this boundary.
    repair = dict(authorization_id=claims.authorization_id, selected_endpoint=candidate.endpoint_id,
                  actual_input_tokens=(usage.uncached_input_tokens if candidate.provider == "anthropic"
                                       else usage.total_prompt_tokens),
                  actual_output_tokens=usage.output_tokens, cache_read_input_tokens=usage.cache_read_tokens,
                  cache_creation_input_tokens=usage.cache_creation_tokens,
                  reasoning_tokens=usage.reasoning_tokens, route_type=claims.route_type,
                  streamed=claims.streamed)
    return SettleOutboxRow(
        authorization_id=claims.authorization_id, intent_kind=terminal.terminal_kind,
        settle_origin="typed", actual_cost_micro=amount, reservation_id=claims.reservation_id,
        selected_endpoint_id=candidate.endpoint_id, model_id=candidate.model_id,
        selected_usage_type="Credits", settle_body=json.dumps(repair, separators=(",", ":")),
        async_version=1, workspace_id=claims.workspace_id, snapshot_hash=claims.snapshot_hash,
        payload_hash=billing.canonical_hash(terminal),
    )


def settlement(row: SettleOutboxRow, *, resolve: bool = True) -> dict[str, Any]:
    status, amount = ("failed" if row.status == "release_approved" else "pending"), row.actual_cost_micro
    if resolve:
        auth = STORE.get_gateway_authorization(row.authorization_id)
        if auth is not None and auth.settled:
            # Authorization is written atomically with the reservation winner;
            # done alone is insufficient (a sibling or reaper can win).
            amount = auth.finalized_cost_microdollars or 0
            status = "refunded" if auth.finalization_outcome == "refunded" else "settled"
    sid = row.authorization_id + "." + row.intent_kind
    view = dict(v=1, settlement_id=sid, settlement_status=status, cost_microdollars=amount,
                status_url="/v1/settlements/" + quote(sid, safe=""),
                poll_after_ms=1000 if status == "pending" else None)
    if row.intent_kind == "refund" and status == "settled":
        view["review_required"] = True
    return view


def response(row: SettleOutboxRow, *, duplicate: bool = False) -> JSONResponse:
    view = settlement(row, resolve=duplicate)
    return JSONResponse({"data": {"acceptance": {
        "status": "duplicate" if duplicate else "accepted", "payload_hash": row.payload_hash,
        "settlement_status": view["settlement_status"]}, "trusted_router_settlement": view}},
        status_code=202 if view["settlement_status"] == "pending" else 200)


def duplicate(outbox: SpannerSettleOutbox, row: SettleOutboxRow) -> JSONResponse | None:
    existing = outbox.get(row.authorization_id, row.intent_kind)
    if existing is None:
        return None
    if existing.async_version != 1 or existing.payload_hash != row.payload_hash:
        raise api_error(409, "Settlement intent already exists with a different payload", "conflict")
    metric("duplicate")
    return response(existing, duplicate=True)


def finish(row: SettleOutboxRow, outbox: SpannerSettleOutbox) -> JSONResponse:
    from trusted_router.services.settle_outbox_apply import apply_frozen_settle
    from trusted_router.services.settle_outbox_drain import _resolve_row

    existing = outbox.get(row.authorization_id, row.intent_kind)
    if existing is not None:
        if existing.async_version != 1 or existing.payload_hash != row.payload_hash:
            raise api_error(409, "Settlement intent already exists with a different payload", "conflict")
        row = replace(existing, lease_owner=None, leased_until=None)
        if row.status == "release_approved":
            raise api_error(409, "Settlement intent has been abandoned", "conflict")
        if row.status == "done":
            result = response(row, duplicate=True)
            if result.status_code == 200:
                return result
            raise unavailable()
    outcome = apply_frozen_settle(row)
    # A direct synchronous fallback can race a new INSERT after its first read.
    # Never let its resolution mark/park a different accepted payload.
    existing = outbox.get(row.authorization_id, row.intent_kind)
    if existing is not None and existing.payload_hash != row.payload_hash:
        raise api_error(409, "Settlement intent already exists with a different payload", "conflict")
    _resolve_row(outbox, row, outcome, error_note=None)
    result = response(row, duplicate=True)
    if result.status_code != 200:
        raise unavailable()
    return result


def _handle(raw: bytes, *, kind: str, runtime: Runtime | None, settings: Settings,
           started: float, synchronous: bool = False) -> JSONResponse:
    """The absolute deadlines start before body IO/queueing, not in this worker."""
    try:
        with spanner_rpc_deadline(started + 2):
            value = parse(raw)
            if runtime is None or runtime.signer is None:
                return sync_required("not_eligible")
            now = int(time.time())
            claims = verify(value, runtime, kind, now)
            if getattr(STORE, "_database", None) is None:
                return sync_required("unsupported_cohort")
            outbox = spanner_settle_outbox()
            excluded = billing.exclusion(value.observed)
            if synchronous or excluded:
                existing = outbox.get(claims.authorization_id, kind)
                if existing is not None:
                    if (existing.async_version != 1
                            or existing.payload_hash != billing.canonical_hash(value.terminal)):
                        raise api_error(409, "Settlement intent already exists with a different payload", "conflict")
                    metric("duplicate")
                    return finish(existing, outbox) if synchronous else response(existing, duplicate=True)
            if excluded:
                return sync_required("unsupported_cohort")
            try:
                amount = price(value)
            except HTTPException:
                # MF5: an evaluator disagreement can reject NEW acceptance, but
                # cannot reprice or invalidate a previously accepted payload.
                existing = outbox.get(claims.authorization_id, kind)
                if (existing is not None and existing.async_version == 1
                        and existing.payload_hash == billing.canonical_hash(value.terminal)):
                    metric("duplicate")
                    return finish(existing, outbox) if synchronous else response(existing, duplicate=True)
                raise
            row = intent(value, claims, amount)
            # Expired/disabled/admission-flipped retries still resolve accepted work.
            if synchronous or now >= claims.exp or not settings.async_settle_admission_enabled:
                existing = outbox.get(row.authorization_id, kind)
                if existing is not None:
                    result = duplicate(outbox, row)
                    if synchronous and result is not None and result.status_code == 202:
                        return finish(existing, outbox)
                    assert result is not None
                    return result
                if not synchronous:
                    return sync_required("ticket_expired" if now >= claims.exp else "disabled")
            if synchronous:
                return finish(row, outbox)
            if not synchronous:
                expected = claims.model_dump(mode="json")
                expected.update(journal_region=runtime.region, epoch=runtime.epoch,
                                iss=runtime.signer.trusted.iss, aud="router-settlement")
                verify_ticket(value.ticket, [runtime.signer.trusted], expected, now)
                if not claims.async_eligible:
                    return duplicate(outbox, row) or sync_required("not_eligible")
                if runtime.admission is None or not runtime.admission.eligible(
                        claims.workspace_id, settings.async_settle_pilot_cap_micro):
                    return duplicate(outbox, row) or sync_required(admission_reason(
                        runtime, claims.workspace_id, settings.async_settle_pilot_cap_micro))
            # Sync fallback also freezes before applying: identical money primitive,
            # no catalog lookup. Expired tickets may create only synchronous work.
            deadline = started + (2 if synchronous else 0.5)
            try:
                enqueue(outbox, row, deadline)
            except ReservationNotOpen:
                result = duplicate(outbox, row)
                if result is not None:
                    return result
                typed = typed_billing_store()
                if typed is None or row.reservation_id is None:
                    raise unavailable() from None
                winner = typed.read_typed_reservation(row.reservation_id)
                authorization = STORE.get_gateway_authorization(row.authorization_id)
                if winner is not None:
                    if winner.get("authorization_id") != row.authorization_id:
                        raise api_error(401, "Invalid settlement ticket", "unauthorized") from None
                    if not winner.get("settled"):
                        # Do not turn an inconsistent observation into admission.
                        raise unavailable() from None
                if authorization is not None and (authorization.workspace_id != claims.workspace_id
                                                  or authorization.key_hash != claims.key_id):
                    raise api_error(401, "Invalid settlement ticket", "unauthorized") from None
                # No async intent was accepted. The unchanged synchronous path
                # returns the confirmed sync/reaper winner on the same identity.
                return sync_required("reservation_not_open")
            except Exception as exc:
                if _is_already_exists(exc):
                    result = duplicate(outbox, row)
                    if result is not None:
                        if synchronous:
                            existing = outbox.get(row.authorization_id, kind)
                            assert existing is not None
                            return finish(existing, outbox)
                        return result
                    raise unavailable() from exc
                metric("unknown_outcome")
                # Retry exactly the same identity while handoff time remains.
                # A miss after this is still unknown; never authorize repricing.
                if time.monotonic() < deadline:
                    try:
                        enqueue(outbox, row, deadline)
                    except Exception:
                        metric("unknown_outcome")
                result = duplicate(outbox, row)
                if result is not None and time.monotonic() < deadline:
                    return result
                raise unavailable() from exc
            metric("accepted")
            metric("handoff_ms", value=(time.monotonic()-started)*1000)
            metric("remaining_budget_ms", value=max(0, deadline-time.monotonic())*1000)
            return finish(row, outbox) if synchronous else response(row)
    except HTTPException:
        metric("rejected")
        raise
    except ValueError as exc:
        metric("rejected")
        raise api_error(401, "Invalid settlement ticket", "unauthorized") from exc


def handle(raw: bytes, *, kind: str, runtime: Runtime | None, settings: Settings,
           started: float, synchronous: bool = False) -> JSONResponse:
    try:
        result = _handle(raw, kind=kind, runtime=runtime, settings=settings,
                         started=started, synchronous=synchronous)
        if time.monotonic() >= started + 2:
            raise unavailable()
        return result
    except transient_store_error_types() as exc:
        raise unavailable() from exc
