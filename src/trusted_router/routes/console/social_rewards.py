"""Authenticated, same-origin social reward status and self-attested claims."""
from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from trusted_router.acquisition import log_browser_funnel_event
from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.domains import request_control_origin
from trusted_router.routes.console._shared import ConsoleDep
from trusted_router.social_rewards import claim, require_offer, status
from trusted_router.storage import STORE

log = logging.getLogger(__name__)

_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["github", "x_trustedrouter", "x_founder", "linkedin", "youtube"]


class ClaimRequest(ActionRequest):
    attested: StrictBool

    @field_validator("attested")
    @classmethod
    def require_attestation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Self-attestation is required")
        return value


def _track(request: Request, event: str) -> None:
    try:
        log_browser_funnel_event(request, event)
    except Exception:  # noqa: BLE001 - telemetry must not affect the offer.
        log.warning("social_rewards.telemetry_failed", exc_info=True)


def _same_origin(request: Request, settings: Settings) -> None:
    # Browser POST fetches send Origin. Reject missing/null origins too; no
    # local-mode bypass, and compare scheme and port as well as host.
    try:
        source = urlsplit(request.headers.get("origin", ""))
    except ValueError as exc:
        raise HTTPException(403, "Same-origin request required.") from exc
    expected = urlsplit(
        str(request.base_url) if settings.environment.lower() in {"local", "test"}
        else request_control_origin(request, settings)
    )
    if (
        source.scheme not in {"https", "http"}
        or source.scheme != expected.scheme
        or source.netloc.lower() != expected.netloc.lower()
        or source.path not in {"", "/"}
        or source.query or source.fragment
    ):
        raise HTTPException(403, "Same-origin request required.")


def register(app: FastAPI) -> None:
    @app.get("/console/social-rewards")
    def reward_status(ctx: ConsoleDep, settings: SettingsDep) -> JSONResponse:
        return JSONResponse(status(ctx.user.id, settings), headers=_HEADERS)

    @app.post("/console/social-rewards/claim")
    def reward_claim(
        request: Request, body: ClaimRequest, ctx: ConsoleDep, settings: SettingsDep,
    ) -> JSONResponse:
        _same_origin(request, settings)
        return JSONResponse(claim(ctx.user.id, body.action, settings), headers=_HEADERS)

    @app.post("/console/social-rewards/shown")
    def reward_shown(request: Request, ctx: ConsoleDep, settings: SettingsDep) -> JSONResponse:
        _same_origin(request, settings)
        require_offer(ctx.user.id, settings)
        show_now = STORE.dismiss_social_rewards(ctx.user.id)
        if show_now:
            _track(request, "social_rewards_exposed")
        return JSONResponse({**status(ctx.user.id, settings), "show_now": show_now}, headers=_HEADERS)

    @app.post("/console/social-rewards/dismiss")
    def reward_dismiss(request: Request, ctx: ConsoleDep, settings: SettingsDep) -> JSONResponse:
        _same_origin(request, settings)
        # Dismissal still works while issuance is switched off.
        STORE.dismiss_social_rewards(ctx.user.id)
        return JSONResponse(status(ctx.user.id, settings), headers=_HEADERS)

    @app.post("/console/social-rewards/clicked")
    def reward_clicked(
        request: Request, body: ActionRequest, ctx: ConsoleDep, settings: SettingsDep,
    ) -> JSONResponse:
        _same_origin(request, settings)
        require_offer(ctx.user.id, settings)
        _track(request, "social_reward_clicked")
        return JSONResponse({"recorded": True}, headers=_HEADERS)
