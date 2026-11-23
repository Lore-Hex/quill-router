from __future__ import annotations

import base64
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from clickhouse import ingest_enterprise_requests as ingest
from trusted_router import storage_gcp_enterprise_requests as transport
from trusted_router.routes import public
from trusted_router.services import enterprise_requests as private


@pytest.fixture(autouse=True)
def clean_rate_limit():
    public._INQUIRY_HITS.clear()
    public._INQUIRY_GLOBAL_HITS.clear()


@pytest.mark.parametrize(("resource", "tag"), [("brochure", "BUYER_DECK"), ("security", "SECURITY_PACK")])
def test_valid_request_captured_without_tracking(client, test_settings, monkeypatch, caplog, resource, tag):
    test_settings.enterprise_request_capture_enabled = True
    publish = Mock(return_value=True)
    notification = Mock()
    analytics = Mock()
    monkeypatch.setattr(public, "publish_request", publish)
    monkeypatch.setattr(public, "get_email_service", notification)
    monkeypatch.setattr(public, "log_exchange_site_funnel_event", analytics)
    result = client.post("/token-exchange/brief", headers={"Origin": "https://nytokenexchange.com"},
                         json={"email": " user@Example.org ", "resource": resource, "tracking_disabled": True,
                               "page": "/?private=yes", "campaign": {"utm_content": "not-for-crm"}})
    assert result.status_code == 200
    row, = publish.call_args.args
    assert row.email == "user@example.org"
    assert row.request_type == tag and row.source_site == "nytokenexchange.com"
    assert set(row.model_dump()) == {"email", "request_id", "request_type", "requested_at", "source_site"}
    assert row.email not in caplog.text and row.email not in repr(row)
    analytics.assert_not_called()
    notification.return_value.send.assert_called_once()


def test_queue_failure_or_rejected_input_never_delivers(client, test_settings, monkeypatch, caplog):
    test_settings.enterprise_request_capture_enabled = True
    publish = Mock(side_effect=ValueError("private@example.org"))
    notification = Mock()
    monkeypatch.setattr(public, "publish_request", publish)
    monkeypatch.setattr(public, "get_email_service", notification)
    for body, status in [({"email": "invalid"}, 422), ({"website": "spam"}, 200),
                         ({"email": "private@example.org", "resource": "market_data"}, 422)]:
        assert client.post("/token-exchange/brief", json=body).status_code == status
    publish.assert_not_called()
    assert client.post("/token-exchange/brief", json={"email": "private@example.org"}).status_code == 503
    assert "private@example.org" not in caplog.text
    notification.assert_not_called()


def record():
    return private.EnterpriseRequest.new(email="example@example.org", source_site="trustedrouter.com",
                                         request_type="BUYER_DECK")


def message(row):
    return {"ackId": "good", "message": {"data": base64.b64encode(row.model_dump_json().encode()).decode()}}


def response(payload):
    return Mock(status_code=200, json=Mock(return_value=payload))


def test_publish_only_private_envelope(monkeypatch):
    from unittest.mock import MagicMock

    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value = response({"messageIds": ["42"]})
    monkeypatch.setattr(transport, "queue_session", lambda: session)
    row = record()
    assert private.publish_request(row)
    url, = session.post.call_args.args
    assert url == transport.TOPIC
    assert session.post.call_args.kwargs["allow_redirects"] is False
    [msg] = session.post.call_args.kwargs["json"]["messages"]
    assert set(msg) == {"data"}
    assert private.EnterpriseRequest.model_validate_json(base64.b64decode(msg["data"])) == row


def test_drain_redelivery_keeps_id_and_acks_only_after_insert():
    row = record()
    session = Mock()
    payload = {"receivedMessages": [message(row), {"ackId": "bad", "message": {"data": "invalid!"}}]}
    session.post.side_effect = [response(payload), response(payload), response({})]
    insert = Mock(side_effect=[RuntimeError(), None])
    with pytest.raises(RuntimeError):
        ingest.drain_once(session, insert)
    assert session.post.call_count == 1
    assert ingest.drain_once(session, insert) == (1, 1)
    assert insert.call_args_list[0] == insert.call_args_list[1]
    assert insert.call_args.args == ([row],)
    assert session.post.call_args.kwargs["json"] == {"ackIds": ["good"]}


@pytest.mark.parametrize("change", [{"email": "no"}, {"source_site": "untrusted.invalid"}, {"prompt": "no"},
                                   {"request_type": "NEWSLETTER"}, {"request_id": None}])
def test_private_schema_rejects_other_fields_or_invalid_records(change):
    with pytest.raises(ValidationError):
        private.EnterpriseRequest.model_validate(record().model_dump() | change)


def test_insert_uses_only_local_writer_and_data_separate_from_query(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.status_code = 200
    factory = Mock(return_value=client)
    monkeypatch.setattr(ingest.httpx, "Client", factory)
    monkeypatch.setenv("TR_ENTERPRISE_INSERT_PASSWORD", "example")
    row = record()
    ingest.insert_requests([row])
    assert factory.call_args.kwargs["trust_env"] is False and factory.call_args.kwargs["follow_redirects"] is False
    assert client.post.call_args.args == ("http://127.0.0.1:8123/",)
    args = client.post.call_args.kwargs
    assert args["headers"]["X-ClickHouse-User"] == "tr_enterprise_insert"
    assert args["params"]["insert_quorum"] == "2" and row.email not in str(args["params"])
    assert json.loads(args["content"])["email"] == row.email
