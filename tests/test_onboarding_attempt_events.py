import json
import logging

import pytest
from fastapi.testclient import TestClient

from trusted_router.main import _ApplicationConsoleFormatter

ATTEMPT = "cd0fb379-6d48-4660-bde1-7c80b7be174d"


@pytest.mark.parametrize("name,details", [
    ("onboarding_call_started", {}),
    ("onboarding_call_succeeded", {"http_status": 200, "elapsed_ms": 1500, "finish_reason": "stop"}),
    ("onboarding_call_failed", {"http_status": 200, "elapsed_ms": 1500, "failure_reason": "output_budget_exhausted", "finish_reason": "length"}),
])
def test_attempt_events_reach_structured_logs(client: TestClient, caplog, name, details):
    client.get("/?utm_source=test")
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": name, "attempt_id": ATTEMPT, **details})
    assert response.status_code == 204
    record = next(r for r in caplog.records if r.getMessage() == "acquisition." + name)
    payload = json.loads(_ApplicationConsoleFormatter().format(record))
    assert payload["attempt_id"] == ATTEMPT
    assert payload["flow"] == "welcome_test"
    for key, value in details.items():
        assert payload[key] == value


@pytest.mark.parametrize("body", [
    {"event": "onboarding_call_started"},
    {"event": "onboarding_call_started", "attempt_id": "sk-secret"},
    {"event": "onboarding_call_succeeded", "attempt_id": ATTEMPT, "http_status": 500},
    {"event": "onboarding_call_failed", "attempt_id": ATTEMPT, "failure_reason": "raw private message"},
    {"event": "onboarding_call_failed", "attempt_id": ATTEMPT, "failure_reason": "http_error", "http_status": 900, "elapsed_ms": 10},
    {"event": "onboarding_call_failed", "attempt_id": ATTEMPT, "failure_reason": "http_error", "elapsed_ms": -1},
    {"event": "onboarding_call_started", "attempt_id": ATTEMPT, "prompt": "private"},
    {"event": "landing_engaged", "attempt_id": ATTEMPT},
])
def test_attempt_metadata_is_strictly_validated(client: TestClient, body):
    response = client.post("/analytics/events", json=body)
    assert response.status_code == 400


def test_client_cannot_claim_server_activation(client: TestClient):
    assert client.post("/analytics/events", json={"event": "first_successful_api_call", "attempt_id": ATTEMPT}).status_code == 400
