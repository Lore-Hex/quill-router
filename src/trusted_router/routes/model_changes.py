"""Anonymous, read-only catalog change polling and Atom subscriptions."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel

from trusted_router.model_changes import (
    PRICE_TYPES,
    active_history,
    canonical,
    load_history,
    timestamp,
)

ChangeType = Literal[
    "model_added", "model_removed", "endpoint_added", "endpoint_removed", "endpoint_changed",
    "confidential_route_gained", "confidential_route_lost", "privacy_tier_changed",
    "retirement_scheduled", "pricing_schedule_changed", "schedule_cancelled",
]


class CatalogChange(BaseModel):
    id: str
    model: str
    type: ChangeType
    effective_at: AwareDatetime
    recorded_at: AwareDatetime
    source: str
    before: Any
    after: Any
    endpoint: str | None = None
    commit: str | None = None


class CatalogChanges(BaseModel):
    data: list[CatalogChange]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def selected_changes(
    since: AwareDatetime | None = Query(None, description="Inclusive recorded_at timestamp, with timezone. Deduplicate by id."),
    model: str | None = Query(None, max_length=256, description="Exact public model id."),
    type: list[ChangeType] | None = Query(None, description="Repeat to include several change types."),
    include_prices: bool = Query(False, description="Include pricing schedule changes; also enabled by explicitly selecting their type."),
    upcoming: bool | None = Query(None, description="True: future effective_at only; false: effective already; omitted: both."),
) -> list[dict[str, Any]]:
    now = timestamp(_utc_now())
    rows = []
    for event in active_history(load_history()):
        if since is not None and datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00")) < since:
            continue
        if model is not None and event["model"] != model:
            continue
        if type is not None and event["type"] not in type:
            continue
        price_type = event["before"]["type"] if event["type"] == "schedule_cancelled" else event["type"]
        if not include_prices and type is None and price_type in PRICE_TYPES:
            continue
        if upcoming is not None and (event["effective_at"] > now) != upcoming:
            continue
        rows.append(event)
    return sorted(rows, key=lambda e: (e["recorded_at"], e["id"]))


class AtomResponse(Response):
    media_type = "application/atom+xml"


def register_model_change_routes(router: APIRouter) -> None:
    @router.get(
        "/models/changes", response_model=CatalogChanges, response_model_exclude_unset=True,
        summary="Poll public model route changes",
        description=(
            "Committed, derived catalog history, including announced future retirements. "
            "No authentication. Ordered by (recorded_at, id), ascending, without truncation. "
            "since filters the recording time inclusively; effective_at is the change date. "
            "Use id to deduplicate polls. Cancelled forecasts are replaced by schedule_cancelled "
            "entries. Prices are excluded by default. Dates describe catalog observations and "
            "announced cutovers, not deployment completion or live route health. "
            "Subscribe at /v1/models/changes.atom. See /docs#model-changes."
        ),
    )
    async def model_changes(rows: list[dict[str, Any]] = Depends(selected_changes)) -> dict[str, Any]:
        return {"data": rows}

    @router.get(
        "/models/changes.atom", response_class=AtomResponse,
        summary="Subscribe to public model route changes",
        description="Atom feed with the same filters, ordering and entries as /v1/models/changes.",
    )
    async def model_changes_atom(
        request: Request, rows: list[dict[str, Any]] = Depends(selected_changes),
    ) -> Response:
        feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")
        ET.SubElement(feed, "id").text = "https://trustedrouter.com/v1/models/changes.atom"
        ET.SubElement(feed, "title").text = "TrustedRouter model route changes"
        ET.SubElement(feed, "updated").text = max(
            (e["recorded_at"] for e in load_history()), default="2026-09-01T00:00:00Z",
        )
        author = ET.SubElement(feed, "author")
        ET.SubElement(author, "name").text = "TrustedRouter"
        query = f"?{request.url.query}" if request.url.query else ""
        ET.SubElement(feed, "link", rel="self", type="application/atom+xml",
                      href=f"https://trustedrouter.com/v1/models/changes.atom{query}")
        ET.SubElement(feed, "link", rel="alternate", href="https://trustedrouter.com/docs#model-changes")
        for row in rows:
            entry = ET.SubElement(feed, "entry")
            ET.SubElement(entry, "id").text = f"urn:trustedrouter:model-change:{row['id']}"
            ET.SubElement(entry, "title").text = (
                f"{row['model']}: {row['type'].replace('_', ' ')} ({row['effective_at']})"
            )
            ET.SubElement(entry, "updated").text = row["recorded_at"]
            ET.SubElement(entry, "published").text = row["recorded_at"]
            ET.SubElement(entry, "category", term=row["type"])
            ET.SubElement(entry, "content", type="text").text = canonical(row)
        return AtomResponse(ET.tostring(feed, encoding="utf-8", xml_declaration=True))
