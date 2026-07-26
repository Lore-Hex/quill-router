"""Authenticated Google Ads Data Manager conversion feed.

The CSV is intentionally narrower than the private attribution record. It
contains only Google click IDs and conversion metadata, never TrustedRouter
account identifiers or inference content.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt

from fastapi import APIRouter, HTTPException, Query, Request, Response

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.google_ads_conversions import google_ads_conversions_csv
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.security import constant_time_equal
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.get("/internal/marketing/google-ads-conversions.csv")
    async def google_ads_conversion_feed(
        request: Request,
        settings: SettingsDep,
    ) -> Response:
        _require_feed_auth(request, settings)
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        since = now - dt.timedelta(days=settings.google_ads_conversion_feed_retention_days)
        max_rows = settings.google_ads_conversion_feed_max_rows
        conversions = STORE.list_google_ads_conversions(
            since=since.isoformat().replace("+00:00", "Z"),
            limit=max_rows + 1,
        )
        if len(conversions) > max_rows:
            raise api_error(
                503,
                "Google Ads conversion feed exceeded its configured row limit",
                ErrorType.SERVICE_UNAVAILABLE,
            )
        return Response(
            google_ads_conversions_csv(conversions),
            media_type="text/csv",
            headers={
                "Cache-Control": "private, no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @router.post("/internal/marketing/google-ads-conversions/backfill")
    async def backfill_google_ads_conversions(
        request: Request,
        settings: SettingsDep,
        limit: int = Query(default=10_000, ge=1, le=100_000),
    ) -> dict[str, object]:
        require_internal_gateway(request, settings)
        created = STORE.backfill_google_ads_conversions(limit=limit)
        return {"data": {"created": created, "scanned_limit": limit}}


def _require_feed_auth(request: Request, settings: Settings) -> None:
    expected_password = settings.google_ads_conversion_feed_password
    if not expected_password:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    supplied_username, supplied_password = _basic_credentials(
        request.headers.get("authorization", "")
    )
    valid_username = constant_time_equal(
        supplied_username,
        settings.google_ads_conversion_feed_username,
    )
    valid_password = constant_time_equal(supplied_password, expected_password)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid Google Ads Data Manager credentials",
            headers={"WWW-Authenticate": 'Basic realm="TrustedRouter conversions"'},
        )


def _basic_credentials(header: str) -> tuple[str, str]:
    if not header.lower().startswith("basic "):
        return "", ""
    encoded = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return "", ""
    if ":" not in decoded:
        return "", ""
    username, password = decoded.split(":", 1)
    return username, password
