from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, Request, Response
from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

from trusted_router.acquisition import log_browser_funnel_event
from trusted_router.catalog import MODELS, PROVIDERS


class CatalogEventDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=128)
    pick: Literal["glm 5.3", "kimi k3", "deepseek"] | None = None
    filter: Literal["all", "trusted", "e2e", "zdr", "open", "us", "eu"] | None = None
    sort: Literal["featured", "input", "cached", "output", "context", "providers", "name"] | None = None

    @model_validator(mode="after")
    def public_identifiers_only(self) -> Self:
        if self.model_id is not None:
            model = MODELS.get(self.model_id)
            if model is None or model.internal_only:
                raise ValueError("Unknown public model")
        if self.provider_id is not None and self.provider_id not in PROVIDERS:
            raise ValueError("Unknown public provider")
        return self


class MarketingEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal[
        "models.search_used",
        "models.search_zero_results",
        "models.popular_pick_clicked",
        "models.filter_changed",
        "models.sort_changed",
        "models.model_clicked",
        "models.provider_clicked",
        "models.comparison_expanded",
        # Homepage events carry names only. Interaction properties stay in the browser.
        "home.catalog_filter",
        "home.alias_copied",
        "home.migration_tab",
        "home.base_url_copied",
        "home.hero_agent_prompt_copied",
        "home.agent_prompt_copied",
        "home.code_copied",
        "home.faq_opened",
        "home.catalog_row_clicked",
        "home.token_exchange_clicked",
        "home.cta_clicked.hero",
        "home.cta_clicked.migration",
        "home.cta_clicked.closing",
        "landing_viewed",
        "landing_engaged",
        "sign_in_opened",
        # The shared sign-in pop-up's "Sign in / Create an account" link.
        "signin_mode_switched",
        "signup_mode_switched",
        "onboarding_call_started",
        "onboarding_call_succeeded",
        "onboarding_call_failed",
    ]
    catalog: CatalogEventDetails | None = None
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
        details = self.model_dump(exclude={"event", "catalog"}, exclude_none=True)
        if self.event.startswith("models."):
            required = {
                "models.search_used": set(), "models.search_zero_results": set(),
                "models.popular_pick_clicked": {"pick"}, "models.filter_changed": {"filter"},
                "models.sort_changed": {"sort"}, "models.model_clicked": {"model_id"},
                "models.provider_clicked": {"model_id", "provider_id"},
                "models.comparison_expanded": {"model_id"},
            }[self.event]
            fields = self.catalog.model_dump(exclude_none=True) if self.catalog else {}
            if set(fields) != required or details:
                raise ValueError("Invalid catalog event metadata")
            return self
        if self.catalog is not None:
            raise ValueError("Catalog metadata requires a catalog event")
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
    def marketing_event(body: MarketingEventRequest, request: Request) -> Response:
        log_browser_funnel_event(
            request, body.event,
            attempt_id=str(body.attempt_id) if body.attempt_id else None,
            http_status=body.http_status, elapsed_ms=body.elapsed_ms,
            failure_reason=body.failure_reason, finish_reason=body.finish_reason,
            catalog=body.catalog.model_dump(exclude_none=True) if body.catalog else None,
        )
        return Response(status_code=204)
