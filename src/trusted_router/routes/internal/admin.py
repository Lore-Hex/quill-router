"""Dedicated operator-only trust controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep, get_authorization_bearer
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.security import constant_time_equal
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

_OPERATOR_PATH_PREFIX = "/internal/admin/workspaces/"
_MAX_OPERATOR_TEXT = 500
_MAX_ABUSE_REF = 255


def _operator_identity(request: Request, settings: SettingsDep) -> str:
    supplied = (
        get_authorization_bearer(request)
        or request.headers.get("x-trustedrouter-internal-token")
        or ""
    )
    if not settings.operator_token or not constant_time_equal(
        supplied, settings.operator_token
    ):
        raise api_error(401, "Invalid operator token", ErrorType.UNAUTHORIZED)
    identity = request.headers.get("x-trustedrouter-operator-identity", "").strip()
    if identity not in settings.operator_identity_set:
        raise api_error(403, "Invalid operator identity", ErrorType.FORBIDDEN)
    return identity


def _required_text(
    body: dict[str, Any], field: str, *, limit: int = _MAX_OPERATOR_TEXT
) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise api_error(400, f"{field} is required", ErrorType.BAD_REQUEST)
    normalized = value.strip()
    if len(normalized) > limit:
        raise api_error(
            400, f"{field} must be at most {limit} characters", ErrorType.BAD_REQUEST
        )
    return normalized


def register(router: APIRouter) -> None:
    @router.post("/internal/admin/workspaces/{workspace_id}/trust-override")
    async def trust_override(
        workspace_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        identity = _operator_identity(request, settings)
        body = await json_body(request)
        tier = body.get("tier")
        if isinstance(tier, bool) or not isinstance(tier, int) or not 0 <= tier <= 3:
            raise api_error(400, "tier must be an integer from 0 through 3", ErrorType.BAD_REQUEST)
        try:
            record = STORE.set_workspace_trust_override(
                workspace_id,
                tier=tier,
                identity_bypass=bool(body.get("identity_bypass", False)),
                operator_identity=identity,
                reason=_required_text(body, "reason"),
            )
        except ValueError as exc:
            if str(exc) == "workspace_not_found":
                raise api_error(404, "Resource not found", ErrorType.NOT_FOUND) from exc
            raise
        return {
            "data": {
                "workspace_id": record.workspace_id,
                "tier": record.tier,
                "identity_bypass": record.identity_bypass,
                "operator_identity": record.operator_identity,
                "reason": record.reason,
                "set_at": record.set_at.isoformat(),
            }
        }

    @router.post("/internal/admin/workspaces/{workspace_id}/abuse")
    async def workspace_abuse(
        workspace_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        identity = _operator_identity(request, settings)
        body = await json_body(request)
        abuse_ref = _required_text(body, "abuse_ref", limit=_MAX_ABUSE_REF)
        reason = _required_text(body, "reason")
        try:
            if body.get("action") == "clear":
                applied = STORE.clear_workspace_abuse_pause(
                    workspace_id,
                    abuse_ref=abuse_ref,
                    operator_identity=identity,
                    reason=reason,
                )
                action = "clear"
            else:
                applied = STORE.record_workspace_abuse_and_demote(
                    workspace_id,
                    abuse_ref=abuse_ref,
                    operator_identity=identity,
                    reason=reason,
                )
                action = "latch"
        except ValueError as exc:
            if str(exc) == "workspace_not_found":
                raise api_error(404, "Resource not found", ErrorType.NOT_FOUND) from exc
            if str(exc) == "abuse_ref_conflict":
                raise api_error(409, "abuse_ref is already used", ErrorType.CONFLICT) from exc
            raise
        return {"data": {"applied": applied, "replayed": not applied, "action": action}}


def is_operator_route(path: str) -> bool:
    normalized = path[3:] if path.startswith("/v1/internal/") else path
    if not normalized.startswith(_OPERATOR_PATH_PREFIX):
        return False
    workspace_id, separator, action = normalized[len(_OPERATOR_PATH_PREFIX) :].partition(
        "/"
    )
    return bool(
        workspace_id
        and separator
        and "/" not in action
        and action in {"trust-override", "abuse"}
    )
