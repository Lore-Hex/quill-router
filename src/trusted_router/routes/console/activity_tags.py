"""/console/activity request tags: keys, a breakdown, and tagged requests.

Both endpoints read, and both take their input as a JSON body instead of a
query string. Tag keys and values are customer text that must stay out of
logs and Sentry (docs/design/request-tagging.md, section 10). A query string
is written to the load balancer's request log and copied into Sentry's
request context; a body is not, and the Sentry client never attaches one.
The workspace always comes from the session, never from the body.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from trusted_router import storage_activity_tags as tag_limits
from trusted_router.money import format_money_precise
from trusted_router.operational_analytics import (
    is_analytics_unavailable,
    log_analytics_unavailable,
)
from trusted_router.request_tags import InvalidTags, validate_tags
from trusted_router.routes.console._shared import ConsoleDep
from trusted_router.routes.console.activity import USAGE_RANGE_PRESETS
from trusted_router.storage import STORE

log = logging.getLogger(__name__)

_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}
# The columns the request list shows. Other generation metadata (user,
# session, referer) is not sent to a page that does not render it.
_LIST_FIELDS = (
    "created_at",
    "model",
    "provider_name",
    "input_tokens",
    "output_tokens",
    "finish_reason",
)


class TagPanelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    range: str = "30d"
    group_by: str | None = None
    tag_key: str | None = None
    tag_value: str | None = None


class TaggedRequestsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    range: str = "30d"
    tag_key: str
    tag_value: str | None = None


def window_start(range_: str, *, now: dt.datetime | None = None) -> str:
    """Start of the usage chart's window for a range preset, ISO-8601 UTC.

    Mirrors ``usage_series``: a range drawn in day buckets starts at 00:00 UTC
    of its first day and shorter ranges are rolling, so the tag totals cover
    the same requests as the chart above them.
    """
    if range_ not in USAGE_RANGE_PRESETS:
        raise HTTPException(status_code=400, detail="invalid range")
    window_minutes, granularity = USAGE_RANGE_PRESETS[range_]
    current = now if now is not None else dt.datetime.now(dt.UTC)
    if granularity == "day":
        days = max(1, window_minutes // 1440)
        first_day = current.date() - dt.timedelta(days=days - 1)
        start = dt.datetime.combine(first_day, dt.time(), tzinfo=dt.UTC)
    else:
        start = current - dt.timedelta(minutes=window_minutes)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def checked_tag(key: str | None, value: str | None = None) -> None:
    """Hold console input to the limits tags are accepted under.

    The same validator as the inference path and ``GET /v1/activity``, so a
    key or value a request could never have carried is refused before any
    query runs. Its messages name the rule, never the text.
    """
    if value is not None and key is None:
        raise HTTPException(status_code=400, detail="tag_value requires tag_key")
    if key is None:
        return
    try:
        validate_tags({key: value or ""})
    except InvalidTags as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@contextmanager
def _unavailable_as_503(surface: str) -> Iterator[None]:
    """An analytics outage is a retryable 503; any other failure stays one."""
    try:
        yield
    except Exception as exc:
        if not is_analytics_unavailable(exc):
            raise
        log_analytics_unavailable(log, surface, exc)
        raise HTTPException(
            status_code=503,
            detail="Tag data is temporarily unavailable; retry shortly.",
            headers={"Retry-After": "30"},
        ) from exc


def register(app: FastAPI) -> None:
    @app.post("/console/activity/tags.json")
    def console_activity_tags(body: TagPanelRequest, ctx: ConsoleDep) -> JSONResponse:
        start_at = window_start(body.range)
        checked_tag(body.group_by)
        checked_tag(body.tag_key, body.tag_value)
        with _unavailable_as_503("console_activity_tags"):
            summary = STORE.activity_tag_summary(
                ctx.workspace.id,
                start_at=start_at,
                group_by=body.group_by,
                tag_key=body.tag_key,
                tag_value=body.tag_value,
            )
        result: dict[str, Any] = {
            "range": body.range,
            "start_at": start_at,
            "keys": summary["keys"],
            "keys_truncated": summary["keys_truncated"],
            # True when the workspace has more distinct keys than the read
            # counts exactly: the list is then the frequent ones, with counts
            # that can be too high.
            "keys_approximate": summary["keys_approximate"],
            "group_by": summary["group_by"],
            "breakdown": summary["breakdown"],
            # True when the key has more distinct values than a breakdown
            # ranks; the rows are then empty and the page says why.
            "breakdown_too_many_values": summary["breakdown_too_many_values"],
            # Read when the request is served, so the page states the limits
            # the store applied to this answer.
            "distinct_limit": tag_limits.TAG_DISTINCT_LIMIT,
            "value_limit": tag_limits.TAG_VALUE_LIMIT,
            "filter": None,
        }
        if body.tag_key is not None:
            result["filter"] = {
                "key": body.tag_key,
                "value": body.tag_value,
                "values": summary["filter_values"],
                "values_truncated": summary["filter_values_truncated"],
                "values_approximate": summary["filter_values_approximate"],
            }
        return JSONResponse(result, headers=_HEADERS)

    @app.post("/console/activity/requests.json")
    def console_activity_tagged_requests(
        body: TaggedRequestsRequest, ctx: ConsoleDep
    ) -> JSONResponse:
        start_at = window_start(body.range)
        checked_tag(body.tag_key, body.tag_value)
        with _unavailable_as_503("console_activity_tagged_requests"):
            found = STORE.activity_tag_events(
                ctx.workspace.id,
                start_at=start_at,
                tag_key=body.tag_key,
                tag_value=body.tag_value,
                limit=tag_limits.TAG_EVENT_LIMIT,
            )
        rows = [
            {
                **{field: event.get(field) for field in _LIST_FIELDS},
                "cost_display": format_money_precise(int(event.get("cost_microdollars") or 0)),
                "tags": dict(event.get("tags") or {}),
            }
            for event in found.data
        ]
        return JSONResponse(
            {
                "range": body.range,
                "start_at": start_at,
                "data": rows,
                "truncated": bool(found.truncated),
                "limit": tag_limits.TAG_EVENT_LIMIT,
            },
            headers=_HEADERS,
        )
