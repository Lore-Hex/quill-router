from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.services.broadcast import drain_broadcast_queue
from trusted_router.storage import STORE


def register(router: APIRouter) -> None:
    @router.post("/internal/broadcast/drain")
    async def drain_broadcast(request: Request, settings: SettingsDep, limit: int = 100) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        before = len(STORE.due_broadcast_deliveries(limit=limit))
        attempted = drain_broadcast_queue(settings=settings, limit=limit)
        after = len(STORE.due_broadcast_deliveries(limit=limit))
        return {"data": {"attempted": attempted or before, "remaining_due": after}}
