from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore, configure_store
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_spend_lease import authorization_typed_columns
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

FIXTURE = Path(__file__).parent / "fixtures" / "stage_d" / "evidence-lookup.json"
GATEWAY_REQUEST_ID = "rlog_00112233445566778899aabbccddeeff"
INTERNAL_TOKEN = "stage-d-evidence-token"  # noqa: S105 - test placeholder
AUTHORIZATION_ID_RE = re.compile(r"^gwa-[0-9a-f]{32}$")


@pytest.fixture
def evidence_client() -> Iterator[TestClient]:
    expected = json.loads(FIXTURE.read_text())
    data = expected["data"]
    authorization = GatewayAuthorization(
        id=data["authorization_id"],
        workspace_id=data["workspace_id"],
        key_hash="fixture-key-hash",
        model_id="fixture/model",
        provider="fixture",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=300,
        credit_reservation_id="fixture-reservation",
        settled=data["settled"],
        settlement="local",
        finalization_outcome="settled",
        gateway_request_id=data["gateway_request_id"],
        stage_d_boot_kid=data["stage_d_boot_kid"],
        heartbeat_seq=data["heartbeat_seq"],
    )
    store, database, _table = make_fake_store(request_record_write_mode="typed")
    payload = dataclasses.asdict(authorization)
    database.gateway_authorizations[authorization.id] = {
        "authorization_id": authorization.id,
        "workspace_id": authorization.workspace_id,
        "key_hash": authorization.key_hash,
        "reservation_id": authorization.credit_reservation_id,
        "model_id": authorization.model_id,
        "provider": authorization.provider,
        "usage_type": str(authorization.usage_type),
        "estimated_microdollars": authorization.estimated_microdollars,
        "settled": authorization.settled,
        "created_at": authorization.created_at,
        "payload": json_body(authorization),
        **authorization_typed_columns(payload),
    }
    configure_store(store)
    settings = Settings(
        environment="test",
        internal_gateway_token=INTERNAL_TOKEN,
    )
    try:
        with TestClient(
            create_app(settings, configure_store_arg=False, init_observability=False)
        ) as client:
            yield client
    finally:
        configure_store(InMemoryStore())


def _path(gateway_request_id: str = GATEWAY_REQUEST_ID) -> str:
    return (
        "/v1/internal/gateway/authorizations/by-gateway-request-id/"
        f"{gateway_request_id}"
    )


def _headers(token: str = INTERNAL_TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_evidence_lookup_response_matches_literal_fixture_exactly(
    evidence_client: TestClient,
) -> None:
    expected = json.loads(FIXTURE.read_text())

    response = evidence_client.get(_path(), headers=_headers())

    assert response.status_code == 200
    actual = response.json()
    assert actual == expected
    assert set(actual) == set(expected)
    assert set(actual["data"]) == set(expected["data"])
    assert {key: type(value) for key, value in actual["data"].items()} == {
        key: type(value) for key, value in expected["data"].items()
    }
    assert AUTHORIZATION_ID_RE.fullmatch(actual["data"]["authorization_id"])
    assert AUTHORIZATION_ID_RE.fullmatch(expected["data"]["authorization_id"])


def test_evidence_lookup_unknown_id_has_exact_404_shape(
    evidence_client: TestClient,
) -> None:
    response = evidence_client.get(
        _path("rlog_ffffffffffffffffffffffffffffffff"),
        headers=_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "type": "not_found",
            "message": "unknown gateway request id",
        }
    }


def test_evidence_lookup_requires_internal_gateway_token(
    evidence_client: TestClient,
) -> None:
    response = evidence_client.get(_path(), headers=_headers("wrong-token"))

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_evidence_lookup_rejects_shared_parent_trace_instead_of_picking_a_call(
    evidence_client: TestClient,
) -> None:
    database = STORE._database
    first = next(iter(database.gateway_authorizations.values()))
    second = dict(first, authorization_id="gwa-ffffffffffffffffffffffffffffffff")
    database.gateway_authorizations[second["authorization_id"]] = second

    response = evidence_client.get(_path(), headers=_headers())

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "ambiguous_gateway_request_id"
    assert "workspace_id" not in response.text


@pytest.mark.parametrize(
    "gateway_request_id",
    (
        "rlog_00112233445566778899aabbccddeef",
        "rlog_00112233445566778899aabbccddeeff0",
        "rlog_00112233445566778899AABBCCDDEEFF",
        "req_00112233445566778899aabbccddeeff",
    ),
)
def test_evidence_lookup_rejects_invalid_gateway_request_id(
    evidence_client: TestClient,
    gateway_request_id: str,
) -> None:
    response = evidence_client.get(_path(gateway_request_id), headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"
