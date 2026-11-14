from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import ManagementPrincipal, Principal, require_scope
from trusted_router.client_context import parse_gateway_request_id
from trusted_router.errors import api_error, error_response
from trusted_router.operational_analytics import (
    is_analytics_unavailable,
    log_analytics_unavailable,
)
from trusted_router.request_tags import InvalidTags, validate_tags
from trusted_router.schemas import RequestBillingResponse
from trusted_router.scopes import SCOPE_INFERENCE
from trusted_router.storage import STORE
from trusted_router.storage_models import AmbiguousGatewayRequestId
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_activity_routes(router: APIRouter) -> None:
    @router.get("/requests/{gateway_request_id}/billing", response_model=RequestBillingResponse)
    def request_billing(
        gateway_request_id: str,
        principal: Annotated[Principal, Depends(require_scope(SCOPE_INFERENCE))],
    ) -> JSONResponse:
        """Read the durable billing outcome for an authorized request ID.

        A missing or ambiguous ID returns 404, not proof of a zero charge.
        This lookup does not depend on the activity/Observability pipeline.
        """
        headers = {"Cache-Control": "private, no-store"}
        if parse_gateway_request_id(gateway_request_id) is None:
            raise api_error(400, "Invalid gateway request ID", ErrorType.BAD_REQUEST, headers=headers)
        try:
            # Exact trace-index seek, not the eventually consistent analytics
            # store. Missing evidence never establishes a zero charge.
            authorization = STORE.get_gateway_authorization_by_gateway_request_id(gateway_request_id)
        except AmbiguousGatewayRequestId:
            authorization = None
        if (
            authorization is None
            or authorization.workspace_id != principal.workspace.id
            or (
                principal.api_key is not None
                and principal.scopes
                and authorization.key_hash != principal.api_key.hash
            )
        ):
            raise api_error(404, "Billing outcome unavailable for this request", ErrorType.NOT_FOUND, headers=headers)
        outcome = authorization.finalization_outcome
        status = "pending" if not authorization.settled else "unknown"
        cost = None
        if authorization.settled and outcome == "refunded":
            status, cost = "refunded", 0
        elif (
            authorization.settled
            and outcome in {"settled", "reaped_snapshot"}
            and authorization.finalized_cost_microdollars is not None
        ):
            status, cost = "settled", authorization.finalized_cost_microdollars
        return JSONResponse({"data": {
            "gateway_request_id": gateway_request_id,
            "billing_status": status,
            "cost_microdollars": cost,
            "currency": "USD",
            "finalization_outcome": outcome,
            "generation_id": authorization.finalized_generation_id,
        }}, headers=headers)

    @router.get("/activity")
    async def activity(
        principal: ManagementPrincipal,
        date: str | None = None,
        api_key_hash: str | None = None,
        group_by: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> dict[str, Any]:
        if date is not None:
            try:
                if dt.date.fromisoformat(date).isoformat() != date:
                    raise ValueError("non-canonical date")
            except ValueError as exc:
                raise api_error(
                    400, "date must be a valid calendar date in YYYY-MM-DD format", ErrorType.BAD_REQUEST,
                ) from exc
        if tag_value is not None and tag_key is None:
            raise api_error(400, "tag_value requires tag_key", ErrorType.INVALID_TAGS)
        try:
            if tag_key is not None:
                validate_tags({tag_key: tag_value or ""})
            group_by_tag = (
                group_by.removeprefix("tag:")
                if group_by and group_by.startswith("tag:")
                else None
            )
            if group_by_tag is not None:
                validate_tags({group_by_tag: ""})
        except InvalidTags as exc:
            raise api_error(400, str(exc), ErrorType.INVALID_TAGS) from exc
        if group_by in {"none", "request", "generation"}:
            normalized_limit = max(1, min(limit, 1000))
            with _analytics_unavailable_as_503():
                result = await run_in_threadpool(
                    STORE.activity_events_result,
                    principal.workspace.id,
                    api_key_hash=api_key_hash,
                    date=date,
                    limit=normalized_limit,
                    tag_key=tag_key,
                    tag_value=tag_value,
                )
            return {
                "data": result.data,
                "meta": _activity_meta(result, tag_filter=tag_key is not None),
            }
        with _analytics_unavailable_as_503():
            result = await run_in_threadpool(
                STORE.activity_result,
                principal.workspace.id,
                api_key_hash=api_key_hash,
                date=date,
                tag_key=tag_key,
                tag_value=tag_value,
                group_by_tag=group_by_tag,
            )
        return {
            "data": result.data,
            "meta": _activity_meta(result, tag_filter=tag_key is not None),
        }

    @router.get("/generation")
    def generation(  # noqa: A002
        id: str,
        principal: Annotated[Principal, Depends(require_scope(SCOPE_INFERENCE))],
    ) -> dict[str, Any]:
        # Either an inference key or a management session works here.
        # OpenRouter accepts the same bearer token the caller used for
        # the chat completion that produced this generation_id, so
        # forty.news (and any OpenRouter-shaped client) can issue the
        # follow-up GET without juggling key types. Workspace scoping
        # below stops cross-workspace lookups.
        gen = STORE.get_generation(id)
        if gen is None or gen.workspace_id != principal.workspace.id:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        # Delegated inference authority is key-isolated. Use the same 404 as a
        # missing generation so one app cannot probe another app's activity.
        if (
            principal.api_key is not None
            and principal.scopes
            and gen.key_hash != principal.api_key.hash
        ):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": gen.to_openrouter_generation()}

    @router.get("/generation/content")
    def generation_content(id: str) -> JSONResponse:  # noqa: A002
        _ = id
        return error_response(
            404,
            "TrustedRouter does not store prompt or output content",
            ErrorType.CONTENT_NOT_STORED,
        )


@contextmanager
def _analytics_unavailable_as_503() -> Iterator[None]:
    """An analytics (ClickHouse) outage is a retryable 503, not a 500."""
    try:
        yield
    except Exception as exc:
        if not is_analytics_unavailable(exc):
            raise
        log_analytics_unavailable(log, "api_activity", exc)
        raise api_error(
            503,
            "Activity analytics is temporarily unavailable; retry shortly.",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "30"},
        ) from exc


def _activity_meta(result: Any, *, tag_filter: bool) -> dict[str, Any]:
    return {
        "truncated": bool(result.truncated),
        "groups_truncated": bool(result.groups_truncated),
        "scanned": int(result.scanned),
        "scan_limit": result.scan_limit,
        "tag_filter_scope": "recent_window" if tag_filter else None,
    }
