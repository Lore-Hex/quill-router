"""Home-plane endpoint serving federated key records to peer planes.

This is the one place a standalone plane learns about a user it has never
seen. What it returns — and refuses to return — is the whole security
boundary:

  * NEVER `salt` or `secret_hash`. A peer verifies bearer tokens through
    the attested gateway using the lookup hash; it does not need the raw
    key material, and copying it would place home-issued secrets at rest
    in a second jurisdiction. Absent by construction, not by filtering.

  * NEVER credits or usage counters. Identity federates; money does not.
    A peer seeds a federated key at ZERO balance and requires an explicit
    transfer, which is what keeps the conservation law intact.

  * NEVER a management key. Those can mint keys and move money; a peer
    that could federate one could escalate itself.

  * Its OWN auth token, not the internal gateway token. Anything holding
    the gateway token can already call /internal/gateway/authorize and
    /settle. Reusing it here would mean one leaked secret grants both
    "spend money" and "enumerate the user directory".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.security import constant_time_equal
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def require_federation_peer(request: Request, settings: Any) -> None:
    """Authenticate a peer plane.

    Distinct from require_internal_gateway on purpose — see the module
    docstring. A plane with no peer token configured serves nobody.
    """
    expected = getattr(settings, "federation_peer_token", "") or ""
    if not expected:
        raise api_error(
            403,
            "Federation is not enabled on this plane",
            ErrorType.FORBIDDEN,
        )
    supplied = request.headers.get("x-trustedrouter-federation-token") or ""
    if not constant_time_equal(supplied, expected):
        raise api_error(401, "Invalid federation peer token", ErrorType.UNAUTHORIZED)


def register(router: APIRouter) -> None:
    @router.post("/internal/federation/resolve-key")
    async def federation_resolve_key(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_federation_peer(request, settings)
        body = await json_body(request)
        lookup_hash = str(body.get("api_key_lookup_hash") or "")
        if not lookup_hash:
            raise api_error(400, "api_key_lookup_hash is required", ErrorType.BAD_REQUEST)

        api_key = STORE.get_key_by_lookup_hash(lookup_hash)
        if api_key is None:
            # A genuine "no such key" — the peer caches this briefly so a
            # leaked key in a retry loop cannot become sustained
            # cross-border traffic. Distinct from a 5xx, which the peer
            # must treat as an outage and NOT cache.
            raise api_error(404, "Unknown API key", ErrorType.NOT_FOUND)

        if getattr(api_key, "management", False):
            raise api_error(
                403,
                "Management keys cannot be federated",
                ErrorType.FORBIDDEN,
            )

        workspace = STORE.get_workspace(api_key.workspace_id)
        if workspace is None:
            raise api_error(404, "Workspace is unavailable", ErrorType.NOT_FOUND)

        # An explicit allow-list. A field added to ApiKey later must be
        # opted in here deliberately — the alternative (serialize the
        # object and strip secrets) leaks the next secret-shaped field
        # somebody adds.
        return {
            "data": {
                "lookup_hash": api_key.lookup_hash,
                "key_hash": api_key.hash,
                "workspace_id": api_key.workspace_id,
                "name": getattr(api_key, "name", "") or "",
                "disabled": bool(getattr(api_key, "disabled", False)),
                "expires_at": getattr(api_key, "expires_at", None),
                "limit_microdollars": getattr(api_key, "limit_microdollars", None),
                "limit_daily_microdollars": getattr(api_key, "limit_daily_microdollars", None),
                "limit_weekly_microdollars": getattr(api_key, "limit_weekly_microdollars", None),
                "limit_monthly_microdollars": getattr(api_key, "limit_monthly_microdollars", None),
                "include_byok_in_limit": bool(
                    getattr(api_key, "include_byok_in_limit", True)
                ),
                "budget_alert_only": bool(getattr(api_key, "budget_alert_only", False)),
                "workspace_billing_paused": bool(
                    getattr(workspace, "billing_paused", False)
                ),
                # Lets the peer detect a changed record without diffing
                # every field, and lets a future revocation feed say
                # "anything older than X is stale".
                "revision": getattr(api_key, "updated_at", None)
                or getattr(api_key, "created_at", None),
            }
        }
