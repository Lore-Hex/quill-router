"""Quota lifecycle: reservation, settle, refund, and post-settle auto-refill.

The reservation flow (key-limit reserve, credit reserve, settle, refund on
failure) used to be duplicated four times across run_chat, run_chat_stream,
gateway_authorize, and gateway_settle. It now lives in `reserved_quota`
(context manager for the in-process inference paths) and
`apply_authorization_outcome` (for the cross-request enclave path), so the
financial bookkeeping has one home and the rest of the runners can focus
on transport.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import BackgroundTasks

from trusted_router.auth import Principal
from trusted_router.catalog import PROVIDERS, Model
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.providers import ProviderError
from trusted_router.services.inference_errors import (
    provider_error_type,
    provider_http_error,
)
from trusted_router.storage import STORE, GatewayAuthorization, ProviderBenchmarkSample
from trusted_router.types import ErrorType, UsageType


@dataclass
class QuotaTicket:
    """Active reservation against a workspace's credits and a key's spend limit.

    Caller is responsible for invoking `settle(actual_cost)` on success;
    `reserved_quota` auto-refunds on exception or if the body exits without
    settling (treating an unsettled exit as cancellation).
    """

    reserve_amount: int
    usage_type: UsageType
    reservation_id: str | None
    key_hash: str
    _finalized: bool = False

    def settle(self, actual_cost_microdollars: int) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self.reservation_id is not None:
            STORE.settle(self.reservation_id, actual_cost_microdollars)
        STORE.settle_key_limit(
            self.key_hash,
            self.reserve_amount,
            actual_cost_microdollars,
            usage_type=self.usage_type,
        )

    def refund(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self.reservation_id is not None:
            STORE.refund(self.reservation_id)
        STORE.refund_key_limit(
            self.key_hash,
            self.reserve_amount,
            usage_type=self.usage_type,
        )


@asynccontextmanager
async def reserved_quota(
    principal: Principal,
    model: Model,
    *,
    reserve_amount: int,
    input_tokens: int,
    streamed: bool,
    region: str | None,
    usage_type_override: UsageType | None = None,
) -> AsyncIterator[QuotaTicket]:
    """Acquire a reservation against the principal's key limit and (for
    prepaid models) the workspace credit account, yield a `QuotaTicket`,
    and refund automatically if the body raises or doesn't settle.
    """
    assert principal.api_key is not None
    usage_type = usage_type_override or UsageType.for_model(model)
    try:
        STORE.reserve_key_limit(
            principal.api_key.hash,
            reserve_amount,
            usage_type=usage_type,
        )
    except ValueError as exc:
        raise api_error(
            402,
            "API key spend limit exceeded",
            ErrorType.KEY_LIMIT_EXCEEDED,
        ) from exc

    reservation_id: str | None = None
    if usage_type == UsageType.CREDITS:
        try:
            reservation = STORE.reserve(
                principal.workspace.id,
                principal.api_key.hash,
                reserve_amount,
            )
            reservation_id = reservation.id
        except ValueError as exc:
            STORE.refund_key_limit(
                principal.api_key.hash,
                reserve_amount,
                usage_type=usage_type,
            )
            raise api_error(
                402,
                "Insufficient credits",
                ErrorType.INSUFFICIENT_CREDITS,
            ) from exc

    ticket = QuotaTicket(
        reserve_amount=reserve_amount,
        usage_type=usage_type,
        reservation_id=reservation_id,
        key_hash=principal.api_key.hash,
    )
    started_at = time.monotonic()
    try:
        yield ticket
    except ProviderError as exc:
        ticket.refund()
        STORE.record_provider_benchmark(
            ProviderBenchmarkSample.from_provider_error(
                model=model,
                provider_name=PROVIDERS[model.provider].name,
                input_tokens=input_tokens,
                elapsed_seconds=max(time.monotonic() - started_at, 0.001),
                streamed=streamed,
                usage_type=usage_type,
                error_status=exc.status_code,
                error_type=provider_error_type(exc.status_code),
                region=region,
            )
        )
        raise provider_http_error(exc) from exc
    except BaseException:
        ticket.refund()
        raise
    if not ticket._finalized:
        ticket.refund()


def apply_authorization_outcome(
    *,
    authorization: GatewayAuthorization,
    success: bool,
    actual_cost_microdollars: int,
    selected_usage_type: UsageType | str | None = None,
    settings: Settings | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """Settle or refund a previously-stored gateway reservation. Used by the
    enclave's settle/refund callbacks where the reservation outlives a single
    request and must be looked up by `authorization_id`.

    Auto-refill: if `settings` is passed and the workspace has auto-refill
    enabled, schedules a Stripe off-session top-up after a successful
    credits-settle. When `background_tasks` is provided (the route-handler
    case), the Stripe call runs after the response is sent — ~200-500ms off
    the critical settle path. When omitted (tests, in-process callers),
    the trigger runs inline and any Stripe error is swallowed by the
    auto-refill service.
    """
    reservation_usage_type = authorization.usage_type
    actual_usage_type = UsageType.coerce(selected_usage_type or authorization.usage_type)
    if success:
        if authorization.credit_reservation_id is not None:
            if actual_usage_type == UsageType.CREDITS:
                STORE.settle(authorization.credit_reservation_id, actual_cost_microdollars)
                if settings is not None:
                    _schedule_auto_refill(
                        authorization.workspace_id, settings, background_tasks
                    )
            else:
                STORE.refund(authorization.credit_reservation_id)
        STORE.settle_key_limit(
            authorization.key_hash,
            authorization.estimated_microdollars,
            actual_cost_microdollars,
            usage_type=reservation_usage_type,
        )
    else:
        if authorization.credit_reservation_id is not None:
            STORE.refund(authorization.credit_reservation_id)
        STORE.refund_key_limit(
            authorization.key_hash,
            authorization.estimated_microdollars,
            usage_type=reservation_usage_type,
        )


def _schedule_auto_refill(
    workspace_id: str,
    settings: Settings,
    background_tasks: BackgroundTasks | None,
) -> None:
    """Run the refill check off the request thread when a BackgroundTasks
    pool is available; otherwise fall back to inline so unit tests + the
    in-process inference path keep working without one."""
    from trusted_router.services.auto_refill import maybe_charge_after_settle

    if background_tasks is not None:
        background_tasks.add_task(
            maybe_charge_after_settle,
            workspace_id,
            settings=settings,
        )
        return
    maybe_charge_after_settle(workspace_id, settings=settings)
