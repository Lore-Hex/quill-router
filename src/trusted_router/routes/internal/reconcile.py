from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.schemas import ReconcileGenerationActivityRequest
from trusted_router.storage import STORE


def register(router: APIRouter) -> None:
    @router.post("/internal/reconcile/generation-activity")
    async def reconcile_generation_activity(
        request: Request,
        body: ReconcileGenerationActivityRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        rewritten = STORE.reconcile_generation_activity(
            body.workspace_id,
            date=body.date,
            limit=body.limit,
        )
        return {
            "data": {
                "workspace_id": body.workspace_id,
                "date": body.date,
                "limit": body.limit,
                "rewritten": rewritten,
            }
        }
