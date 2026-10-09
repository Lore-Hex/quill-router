from __future__ import annotations

import logging
import re
import typing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.axiom_growth import model
from trusted_router.routes.acquisition import MarketingEventRequest

STATIC = Path(__file__).resolve().parents[1] / "src/trusted_router/static"
CTA_MODULES = ("hero", "migration", "closing")  # trackHomepageCta in homepage.js


@pytest.mark.parametrize("event", [
    "home.catalog_filter", "home.alias_copied", "home.migration_tab",
    "home.agent_prompt_opened", "home.base_url_copied", "home.agent_prompt_copied",
    "home.code_copied", "home.faq_opened", "home.catalog_row_clicked", "home.cta_clicked",
    "home.cta_clicked.hero", "home.cta_clicked.migration", "home.cta_clicked.closing",
])
def test_homepage_events_use_existing_attributed_pipeline(
    client: TestClient, caplog: pytest.LogCaptureFixture, event: str,
) -> None:
    client.get("/?utm_source=homepage-test")
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    assert client.post("/analytics/events", json={"event": event}).status_code == 204
    record = next(item for item in caplog.records if item.getMessage() == f"acquisition.{event}")
    assert record.anonymous_fingerprint
    assert not hasattr(record, "properties")


@pytest.mark.parametrize("extra", [
    {"properties": {"query": "private search"}},
    {"model_id": "private-model"},
    {"elapsed_ms": 10},
])
def test_homepage_events_reject_content_and_onboarding_metadata(
    client: TestClient, extra: dict[str, object],
) -> None:
    response = client.post("/analytics/events", json={"event": "home.cta_clicked", **extra})
    assert response.status_code == 400


@pytest.mark.parametrize("headers", [{"sec-gpc": "1"}, {"dnt": "1"}])
def test_homepage_events_respect_privacy_even_with_existing_attribution(
    client: TestClient, caplog: pytest.LogCaptureFixture, headers: dict[str, str],
) -> None:
    client.get("/?utm_source=homepage-test")
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": "home.cta_clicked"}, headers=headers)
    assert response.status_code == 204
    assert "acquisition.home.cta_clicked" not in caplog.text


def test_homepage_events_without_attribution_do_not_create_identifiers(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    client.cookies.clear()
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": "home.cta_clicked"})
    assert response.status_code == 204
    assert "acquisition.home.cta_clicked" not in caplog.text
    assert not response.cookies


def _browser_event_names() -> set[str]:
    names: set[str] = set()
    for script in ("homepage/homepage.js", "dashboard.js", "console.js"):
        source = (STATIC / script).read_text()
        names |= set(
            re.findall(
                r"""(?:track|trackFunnelEvent|postActivationEvent)\(\s*['"]([a-z_.]+)['"]""",
                source,
            )
        )
        names |= set(re.findall(r"""['"](home\.[a-z_.]+)['"]""", source))
    names.discard("home.cta_clicked.")
    return names | {f"home.cta_clicked.{module}" for module in CTA_MODULES}


# Homepage names were missing from the Axiom export list for a week in Oct 2026 and nothing caught it.
def test_browser_event_names_are_accepted_and_exported() -> None:
    accepted = set(typing.get_args(MarketingEventRequest.model_fields["event"].annotation))
    sent = _browser_event_names()
    assert sent, "no event names found in the browser scripts"
    assert sent <= accepted, sent - accepted
    exported = {name.removeprefix("acquisition.") for name in model.BROWSER_EVENTS}
    assert accepted <= exported, accepted - exported
