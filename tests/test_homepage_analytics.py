from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("event", [
    "home.catalog_filter", "home.alias_copied", "home.migration_tab",
    "home.agent_prompt_opened", "home.base_url_copied", "home.agent_prompt_copied",
    "home.code_copied", "home.faq_opened", "home.catalog_row_clicked", "home.cta_clicked",
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
