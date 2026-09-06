from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import inspect_payment_owner as lookup


def test_point_read_has_complete_key_low_priority_deadline_and_no_retry() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def read(self, **kwargs: Any) -> Any:
            from google.cloud.spanner_v1.types import ResultSet

            calls.append(kwargs)
            return ResultSet(rows=[[json.dumps({"owner_user_id": "user-1"})]])

    assert lookup.read_entity(Client(), "session-1", "workspace", "ws-1") == {
        "owner_user_id": "user-1"
    }
    assert len(calls) == 1
    call = calls[0]
    assert call["request"]["session"] == "session-1"
    assert call["request"]["table"] == "tr_entities"
    assert call["request"]["key_set"] == {"keys": [["workspace", "ws-1"]]}
    assert call["request"]["limit"] == 1
    assert call["timeout"] == 5.0
    assert call["retry"] is None
    assert call["request"]["request_options"]["priority"] == "PRIORITY_LOW"
    assert call["request"]["request_options"]["request_tag"] == "tr_ops_payment_owner"


@pytest.mark.parametrize(
    "kind,entity_id",
    [("api_key", "key-1"), ("workspace", ""), ("workspace", "%"), ("user", "x' OR 1=1")],
)
def test_unsafe_entity_lookup_rejected_without_database_call(kind: str, entity_id: str) -> None:
    with pytest.raises(ValueError):
        lookup.read_entity(None, "session-1", kind, entity_id)


def test_payment_read_is_exact_and_redacts_unrelated_provider_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.stripe.com/v1/payment_intents/pi_example"
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "id": "pi_example",
                "status": "succeeded",
                "client_secret": "must-not-appear",
                "metadata": {"workspace_id": "ws-1", "prompt": "must-not-appear"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = lookup.stripe_payment_metadata(client, "pi_example")
    assert result["workspace_id"] == "ws-1"
    assert "must-not-appear" not in json.dumps(result)


@pytest.mark.parametrize(
    "value", ["", "pi_../../customers", "pi_x?expand[]=customer", "cus_example"]
)
def test_bad_payment_id_is_rejected_before_http(value: str) -> None:
    with pytest.raises(ValueError):
        lookup.stripe_payment_metadata(None, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [302, 401, 404, 429, 500])
def test_provider_errors_do_not_retry_or_expose_response_body(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private payment information")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match=f"HTTP {status}") as exc:
            lookup.stripe_payment_metadata(client, "pi_example")
    assert calls == 1
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(
    "initiator,expected_user,attribution",
    [(None, "owner", "current_workspace_owner"), ("buyer", "buyer", "initiating_user")],
)
def test_owner_resolution_is_two_point_reads_and_email_is_opt_in(
    initiator: str | None, expected_user: str, attribution: str
) -> None:
    calls: list[tuple[str, str]] = []

    def reader(kind: str, entity_id: str) -> dict[str, Any]:
        calls.append((kind, entity_id))
        return (
            {"owner_user_id": "owner"}
            if kind == "workspace"
            else {"email": "buyer@example.com", "password_hash": "private"}
        )

    payment = {
        "payment_intent": "pi_example",
        "workspace_id": "ws-1",
        "initiating_user_id": initiator,
    }
    result = lookup.resolve_owner(payment, reader)
    assert calls == [("workspace", "ws-1"), ("user", expected_user)]
    assert result["attribution"] == attribution
    assert "email" not in result
    assert "private" not in json.dumps(result)
    assert lookup.resolve_owner(payment, reader, include_email=True)["email"] == "buyer@example.com"


def test_missing_metadata_does_not_search_for_a_customer() -> None:
    def reader(kind: str, entity_id: str) -> dict[str, Any]:
        pytest.fail("missing metadata must not fall back to a database scan")

    assert (
        lookup.resolve_owner({"payment_intent": "pi_example"}, reader)["attribution"]
        == "missing_workspace_metadata"
    )


@pytest.mark.parametrize(
    "workspace,expected",
    [
        (None, "workspace_not_found"),
        ({}, "missing_user_metadata"),
        ({"federated_home": "aws", "owner_user_id": "owner"}, "workspace_has_remote_home"),
    ],
)
def test_missing_and_remote_workspace_is_not_guessed(
    workspace: dict[str, Any] | None, expected: str
) -> None:
    calls: list[str] = []

    def reader(kind: str, entity_id: str) -> dict[str, Any] | None:
        calls.append(kind)
        return workspace

    assert (
        lookup.resolve_owner({"payment_intent": "pi_example", "workspace_id": "ws-1"}, reader)[
            "attribution"
        ]
        == expected
    )
    assert calls == ["workspace"]


@pytest.mark.parametrize("with_workspace", [True, False])
def test_cli_lifecycle_and_redacted_operator_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], with_workspace: bool
) -> None:
    from google.cloud.spanner_v1.services import spanner
    from google.oauth2 import service_account

    monkeypatch.setenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "/not-read-in-test.json")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test-placeholder")
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_file",
        lambda *a, **kw: SimpleNamespace(service_account_email="ops@example.com"),
    )
    events: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            assert with_workspace
            assert kwargs["credentials"].service_account_email == "ops@example.com"
            events.append("client_opened")

        def __enter__(self) -> FakeClient:
            return self

        def create_session(self, **kwargs: Any) -> Any:
            assert (
                kwargs["request"]["database"]
                == "projects/test-project/instances/test-instance/databases/test-database"
            )
            assert kwargs["retry"] is None
            assert kwargs["timeout"] == 5.0
            return SimpleNamespace(name="session-1")

        def delete_session(self, **kwargs: Any) -> None:
            assert kwargs == {"request": {"name": "session-1"}, "retry": None, "timeout": 5.0}
            events.append("session_deleted")

        def __exit__(self, *args: Any) -> None:
            events.append("client_closed")

    monkeypatch.setattr(spanner, "SpannerClient", FakeClient)
    monkeypatch.setattr(
        lookup,
        "read_entity",
        lambda client, session, kind, key: (
            {"owner_user_id": "owner"} if kind == "workspace" else {"email": "owner@example.com"}
        ),
    )
    real_http_client = httpx.Client

    def make_http_client(**kwargs: Any) -> httpx.Client:
        assert kwargs["timeout"] == 5.0
        assert kwargs["follow_redirects"] is False
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": "pi_example",
                    "metadata": {"workspace_id": "ws-1"} if with_workspace else {},
                },
            )
        )
        return real_http_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", make_http_client)
    assert (
        lookup.main(
            [
                "--project",
                "test-project",
                "--instance",
                "test-instance",
                "--database",
                "test-database",
                "--payment-intent",
                "pi_example",
                "--include-email",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "test-placeholder" not in captured.out + captured.err
    assert [json.loads(line)["event"] for line in captured.err.splitlines()] == [
        "payment_owner_lookup_started",
        "payment_owner_lookup_completed",
    ]
    assert json.loads(captured.err.splitlines()[0])["operator_email"] == "ops@example.com"
    if with_workspace:
        assert output["email"] == "owner@example.com"
        assert events == ["client_opened", "session_deleted", "client_closed"]
    else:
        assert events == []
        assert output["attribution"] == "missing_workspace_metadata"


def test_cli_refuses_arbitrary_sql_before_credentials_are_read() -> None:
    with pytest.raises(SystemExit) as exc:
        lookup.main(["--sql", "SELECT body FROM tr_entities"])
    assert exc.value.code == 2
