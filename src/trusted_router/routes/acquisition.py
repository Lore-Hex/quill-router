from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from trusted_router.acquisition import log_browser_funnel_event


class MarketingEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["sign_in_opened"]


def register_acquisition_routes(router: APIRouter) -> None:
    @router.post("/analytics/events", status_code=204)
    async def marketing_event(body: MarketingEventRequest, request: Request) -> Response:
        log_browser_funnel_event(request, body.event)
        return Response(status_code=204)
