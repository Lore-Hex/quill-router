"""Protection gates strict recovery dispatch before the legacy body model.

Snapshot-sync recovery survives admission rollback; the handler separately
gates fresh async-v1 acceptance on admission.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import InferencePrincipal, Principal
from trusted_router.errors import api_error
from trusted_router.gateway_timing import _scope
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.services.async_settle_handler import handle, settlement, unavailable
from trusted_router.services.settle_outbox_drain import spanner_settle_outbox
from trusted_router.storage import STORE
from trusted_router.storage_gcp_io import spanner_rpc_deadline


class AsyncSettlementRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        legacy = super().get_route_handler()

        async def dispatch(request: Request) -> Response:
            settings = request.app.state.settings
            modes = request.headers.getlist("X-TR-Settlement-Mode")
            # Exact opt-in. Ordinary callers keep the old validation/auth order.
            if not settings.async_settle_protection or modes not in (["async-v1"], ["sync"]):
                return await legacy(request)
            started = time.monotonic()
            try:
                with _scope(), anyio.fail_after(2):
                    require_internal_gateway(request, settings)
                    raw = await request.body()
                    return await run_in_threadpool(
                        handle, raw, kind="refund" if request.url.path.endswith("/refund") else "settle",
                        runtime=getattr(request.app.state, "async_settle", None), settings=settings,
                        started=started, synchronous=modes == ["sync"],
                    )
            except TimeoutError as exc:
                raise unavailable() from exc

        return dispatch


def status(settlement_id: str, principal: Principal) -> dict[str, Any]:
    missing = api_error(404, "Settlement not found", "not_found")
    aid, sep, kind = settlement_id.rpartition(".")
    if not sep or not 1 <= len(aid) <= 64 or kind not in {"settle", "refund"}:
        raise missing
    if getattr(STORE, "_database", None) is None:
        raise missing
    with spanner_rpc_deadline(time.monotonic() + 2):
        row = spanner_settle_outbox().get(aid, kind)
        if row is None or row.async_version != 1 or row.workspace_id != principal.workspace.id:
            raise missing
        auth = STORE.get_gateway_authorization(aid)
        if (auth is None or auth.workspace_id != principal.workspace.id
                or principal.api_key is None or auth.key_hash != principal.api_key.hash):
            raise missing
        view = settlement(row)
        view.update(created_at=row.created_at, updated_at=row.updated_at, terminal_at=row.terminal_at)
        return {"data": {"trusted_router_settlement": view}}


def register_settlement_routes(router: APIRouter) -> None:
    @router.get("/settlements/{settlement_id}")
    async def get_settlement(settlement_id: str, principal: InferencePrincipal) -> dict[str, Any]:
        with _scope():
            return await run_in_threadpool(status, settlement_id, principal)
