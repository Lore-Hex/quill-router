"""Private operational dashboard for upstream model providers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from trusted_router.auth import SESSION_COOKIE_NAME
from trusted_router.config import Settings
from trusted_router.provider_analytics import (
    MAX_PROVIDER_EXPORT_DAYS,
    ProviderAnalyticsClient,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import (
    AuthSession,
    ProviderAccessGrant,
    User,
)
from trusted_router.views import render_template

log = logging.getLogger("trusted_router.provider_portal")


@dataclass(frozen=True)
class ProviderPortalContext:
    user: User
    session: AuthSession
    grants: list[ProviderAccessGrant]

    def selected_grant(self, provider: str | None) -> ProviderAccessGrant:
        selected = provider or self.grants[0].provider
        for grant in self.grants:
            if grant.provider == selected:
                return grant
        raise HTTPException(status_code=403, detail="No access to this provider")


def require_provider_portal_context(request: Request) -> ProviderPortalContext:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = STORE.get_auth_session_by_raw(token) if token else None
    if session is None or session.state != "active":
        raise HTTPException(
            status_code=302,
            headers={"Location": "/?reason=signin&next=/provider"},
        )
    user = STORE.get_user(session.user_id)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    grants = STORE.list_provider_access_for_user(user.id)
    if not grants:
        raise HTTPException(status_code=403, detail="No provider portal access")
    return ProviderPortalContext(user=user, session=session, grants=grants)


ProviderPortalDep = Annotated[
    ProviderPortalContext,
    Depends(require_provider_portal_context),
]


def _client(settings: Settings) -> ProviderAnalyticsClient:
    if (
        not settings.provider_analytics_clickhouse_url
        or not settings.provider_analytics_clickhouse_password
    ):
        raise RuntimeError("provider analytics is not configured")
    return ProviderAnalyticsClient(
        base_url=settings.provider_analytics_clickhouse_url,
        user=settings.provider_analytics_clickhouse_user,
        password=settings.provider_analytics_clickhouse_password,
        database=settings.provider_analytics_clickhouse_database,
        table=settings.provider_analytics_clickhouse_table,
    )


def register_provider_portal_routes(app: FastAPI) -> None:
    settings: Settings = app.state.settings

    async def _render_provider_portal(
        context: ProviderPortalContext,
        grant: ProviderAccessGrant,
        days: int,
    ) -> HTMLResponse:
        try:
            summary = await _client(settings).summary(grant.provider, days=days)
        except (httpx.HTTPError, RuntimeError, ValueError):
            log.exception(
                "provider_portal.analytics_unavailable provider=%s",
                grant.provider,
            )
            raise HTTPException(
                status_code=503,
                detail="Provider analytics are temporarily unavailable",
                headers={"Retry-After": "30"},
            ) from None
        html = render_template(
            "provider/overview.html",
            page_title=f"{grant.provider} operations",
            page_subtitle=(
                "Private request reliability and latency metadata. "
                "Prompts, outputs, customer identities, workspaces, and API keys are excluded."
            ),
            user=context.user,
            user_email=context.user.email,
            workspaces=[],
            current_workspace_id="",
            console_next_path=f"/provider/{grant.provider}",
            provider_mode=True,
            grants=context.grants,
            selected_provider=grant.provider,
            selected_role=grant.role,
            summary=summary,
            days=days,
            static_version=settings.release,
        )
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "private, no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.get("/provider", response_class=RedirectResponse, include_in_schema=False)
    async def provider_portal_redirect(
        context: ProviderPortalDep,
        provider: str | None = Query(default=None),
        days: int = Query(default=7, ge=1, le=MAX_PROVIDER_EXPORT_DAYS),
    ) -> RedirectResponse:
        grant = context.selected_grant(provider)
        suffix = f"?days={days}" if days != 7 else ""
        return RedirectResponse(
            url=f"/provider/{grant.provider}{suffix}",
            status_code=302,
            headers={
                "Cache-Control": "private, no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.get(
        "/provider/requests.csv",
        response_class=StreamingResponse,
        include_in_schema=False,
    )
    async def provider_requests_csv(
        context: ProviderPortalDep,
        provider: str | None = Query(default=None),
        days: int = Query(default=MAX_PROVIDER_EXPORT_DAYS, ge=1, le=MAX_PROVIDER_EXPORT_DAYS),
    ) -> StreamingResponse:
        grant = context.selected_grant(provider)
        try:
            export = await _client(settings).open_csv_export(
                grant.provider,
                days=days,
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            log.exception(
                "provider_portal.export_unavailable provider=%s",
                grant.provider,
            )
            raise HTTPException(
                status_code=503,
                detail="Provider export is temporarily unavailable",
                headers={"Retry-After": "30"},
            ) from None
        filename = f"trustedrouter-{grant.provider}-requests-{days}d.csv"
        return StreamingResponse(
            export.chunks(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.get("/provider/{provider}", response_class=HTMLResponse, include_in_schema=False)
    async def provider_portal(
        provider: str,
        context: ProviderPortalDep,
        days: int = Query(default=7, ge=1, le=MAX_PROVIDER_EXPORT_DAYS),
    ) -> HTMLResponse:
        grant = context.selected_grant(provider)
        return await _render_provider_portal(context, grant, days)
