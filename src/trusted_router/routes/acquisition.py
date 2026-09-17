from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, Request, Response
from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

from trusted_router.acquisition import log_browser_funnel_event


class MarketingEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal[
        "landing_engaged",
        "sign_in_opened",
        "first_call_started",
        "first_call_failed",
        "onboarding_call_started",
        "onboarding_call_succeeded",
        "onboarding_call_failed",
    ]
    attempt_id: UUID4 | None = None
    http_status: int | None = Field(default=None, ge=0, le=599, strict=True)
    elapsed_ms: int | None = Field(default=None, ge=0, le=120_000, strict=True)
    failure_reason: Literal[
        "http_error", "empty_output", "output_budget_exhausted", "invalid_response",
        "network_error", "timeout", "client_error", "missing_key",
    ] | None = None
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "unknown"] | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        details = self.model_dump(exclude={"event"}, exclude_none=True)
        if not self.event.startswith("onboarding_call_"):
            if details:
                raise ValueError("Attempt metadata requires an onboarding event")
            return self
        if self.attempt_id is None:
            raise ValueError("Onboarding events require an attempt ID")
        if self.event == "onboarding_call_started":
            if set(details) != {"attempt_id"}:
                raise ValueError("A start event cannot include an outcome")
        else:
            if self.http_status is None or self.elapsed_ms is None:
                raise ValueError("An outcome requires status and elapsed time")
            if 0 < self.http_status < 100:
                raise ValueError("Invalid HTTP status")
            if self.event == "onboarding_call_succeeded":
                if not 200 <= self.http_status < 300 or self.failure_reason is not None:
                    raise ValueError("Success requires a successful HTTP status and no failure")
            elif self.failure_reason is None:
                raise ValueError("Failure requires a classified reason")
        return self


def register_acquisition_routes(router: APIRouter) -> None:
    @router.post("/analytics/events", status_code=204)
    async def marketing_event(body: MarketingEventRequest, request: Request) -> Response:
        log_browser_funnel_event(
            request, body.event,
            attempt_id=str(body.attempt_id) if body.attempt_id else None,
            http_status=body.http_status, elapsed_ms=body.elapsed_ms,
            failure_reason=body.failure_reason, finish_reason=body.finish_reason,
        )
        return Response(status_code=204)
