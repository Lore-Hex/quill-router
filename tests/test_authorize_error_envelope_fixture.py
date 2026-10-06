"""Literal gateway error envelopes, pinned for the enclave (quill-cloud-proxy).

The enclave decodes ``data.timing`` from every authorize response, errors
included, and must not depend on a field the router does not send. This
fixture is the router's side of that contract: exact response bytes for the
denials the enclave observes. Regenerate deliberately with
``REGENERATE_AUTHORIZE_ERROR_FIXTURE=1``; both repositories take new bytes
together, never one side adjusted to pass.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router import gateway_timing
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

FIXTURE = Path(__file__).parent / "fixtures/speculation_v1/authorize-error-envelopes.json"
AUTHORIZE = "/internal/gateway/authorize"  # the path the enclave calls
SETTLE = "/internal/gateway/settle"
TIMING_FIELDS = {"total_ms", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms", "spanner_rpcs"}


def _capture(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    return {
        "path": path,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "retry_after": response.headers.get("retry-after"),
        "body_exact": response.text,
    }


def _envelopes(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_errors import StoreConflict, StoreUnavailable

    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)
    cases: dict[str, dict[str, Any]] = {}

    def typed_client() -> TestClient:
        return TestClient(create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False))

    _store, _database, key = _seed_typed_gateway_store()
    body = _lookup_body(key).model_dump(exclude_none=True)
    cases["invalid_api_key"] = _capture(typed_client(), AUTHORIZE, {**body, "api_key_lookup_hash": "f" * 64})

    _store, database, key = _seed_typed_gateway_store()
    database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)]["total_credits"] = 0
    cases["insufficient_credits"] = _capture(
        typed_client(), AUTHORIZE, _lookup_body(key).model_dump(exclude_none=True),
    )

    store, database, key = _seed_typed_gateway_store()
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)].update(billing_pause_causes=["abuse"], pause_epoch=19)
    cases["billing_paused"] = _capture(
        typed_client(), AUTHORIZE, _lookup_body(key).model_dump(exclude_none=True),
    )

    for name, error in (("storage_unavailable", StoreUnavailable), ("storage_conflict", StoreConflict)):
        def fail(self: Any, authorization_id: str, error: type[Exception] = error) -> None:
            raise error("storage unavailable")

        with monkeypatch.context() as scoped:
            scoped.setattr(InMemoryStore, "get_gateway_authorization", fail)
            memory = TestClient(create_app(Settings(environment="test"), init_observability=False))
            cases[name] = _capture(memory, SETTLE, {"authorization_id": "missing"})
    return cases


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_gateway_error_envelopes_match_the_frozen_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    current = _envelopes(monkeypatch)
    if os.environ.get("REGENERATE_AUTHORIZE_ERROR_FIXTURE") == "1":
        FIXTURE.write_text(json.dumps({
            "fixture_version": 1,
            "purpose": (
                "Exact gateway error envelopes as served by the router. The enclave pins the same "
                "bytes. data carries timing only: no workspace, key, lookup or rate scope."
            ),
            "timing_fields": sorted(TIMING_FIELDS),
            "cases": current,
        }, indent=2) + "\n")
    frozen = json.loads(FIXTURE.read_text())
    assert current == frozen["cases"]
    assert {case["status"] for case in frozen["cases"].values()} >= {401, 402, 403, 503}
    for name, case in frozen["cases"].items():
        envelope = json.loads(case["body_exact"])
        assert set(envelope) == {"error", "data"}, name
        # The whole contract for denial metadata: timing, and nothing the enclave
        # could mistake for a resolved workspace/key scope.
        assert set(envelope["data"]) == {"timing"}, name
        assert set(envelope["data"]["timing"]) == TIMING_FIELDS, name
        assert all(isinstance(v, int) and v >= 0 for v in envelope["data"]["timing"].values()), name
