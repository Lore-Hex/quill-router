"""The signed routing snapshot the fast path's edge polls (E2, publish dark).

Registered only on the surfaces that carry the internal gateway routes, and
only when ``routing_snapshot_publish_enabled`` is on; otherwise the path is a
404. Authenticated by the internal gateway token, the credential the edge
already holds. The publisher module is imported only when the route runs, so
the off state loads neither snapshot module.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.internal._shared import require_internal_gateway

PATH = "/internal/fastpath/routing-snapshot"
_NO_STORE = {"Cache-Control": "no-store"}


def register(router: APIRouter) -> None:
    @router.get(PATH, include_in_schema=False)
    def routing_snapshot(request: Request, settings: SettingsDep) -> Response:
        """``{"v", "kind", "manifest", "body", "refresh_at"}``: the manifest JWS,
        the body's exact bytes in standard base64, and when to poll again.

        ``ETag`` is the body's sha256; a matching ``If-None-Match`` is a 304.
        503 while no window is current; the edge keeps its last good copy
        until that copy's not_after.
        """
        require_internal_gateway(request, settings)
        publisher = getattr(request.app.state, "routing_snapshot_publisher", None)
        if publisher is None:
            status = getattr(request.app.state, "routing_snapshot_status", "not-installed")
            return JSONResponse({"status": status}, status_code=503, headers=_NO_STORE)
        from trusted_router.services.routing_snapshot_publisher import SnapshotUnavailable

        try:
            publication = publisher.current()
        except SnapshotUnavailable as exc:
            return JSONResponse(
                {"status": exc.status, "retry_at": exc.retry_at}, status_code=503,
                headers={**_NO_STORE, "Retry-After": str(exc.retry_after)},
            )
        headers = {**_NO_STORE, "ETag": f'"{publication.sha256}"'}
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(publication.response, media_type="application/json", headers=headers)
