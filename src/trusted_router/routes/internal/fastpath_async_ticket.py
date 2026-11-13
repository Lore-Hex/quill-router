"""Sign-only edge tickets, imported only while the workspace switch is on."""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trusted_router.auth import SettingsDep
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.services.async_settle import edge_ticket
from trusted_router.storage import STORE, typed_billing_store

PATH = "/internal/fastpath/async-ticket"


class EdgeTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    v: Literal[1]
    authorization_id: str
    workspace_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+\-]{1,64}$")]
    key_hash: str
    reservation_id: str
    invocation_nonce: str
    route_type: Literal["chat.completions", "responses"]
    streamed: bool
    region: str
    candidate_endpoint_ids: Annotated[list[str], Field(min_length=1, max_length=64)]
    priced_at: int
    billing_snapshot_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("v", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("v must be an integer")
        return value

    @model_validator(mode="after")
    def unique_candidates(self) -> Self:
        if len(set(self.candidate_endpoint_ids)) != len(self.candidate_endpoint_ids):
            raise ValueError("candidate_endpoint_ids must be unique")
        return self


class _TicketRoute(APIRoute):
    """Keep this route's 422 contract local to its optional registration."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid request body"}, status_code=422,
                                    headers={"Cache-Control": "no-store"})

        return handle


def _authenticate(request: Request, settings: SettingsDep) -> None:
    require_internal_gateway(request, settings)


def register(router: APIRouter) -> None:
    ticket_router = APIRouter(route_class=_TicketRoute)

    @ticket_router.post(PATH, include_in_schema=False, dependencies=[Depends(_authenticate)])
    def async_ticket(body: EdgeTicketRequest, request: Request, settings: SettingsDep) -> JSONResponse:
        backend = typed_billing_store(STORE)
        result = edge_ticket(
            body=body, settings=settings, runtime=getattr(request.app.state, "async_settle", None),
            typed=backend is not None and getattr(backend, "request_record_write_mode", "legacy") == "typed",
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    router.include_router(ticket_router)
