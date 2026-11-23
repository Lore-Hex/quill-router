import json
from unittest.mock import Mock

import httpx
import pytest

from trusted_router.services import twenty_enterprise as crm
from trusted_router.services.enterprise_requests import EnterpriseRequest


def record():
    return EnterpriseRequest.new(email="a_b@example.org", request_type="BUYER_DECK", source_site="trustedrouter.com")


def person(tags=None):
    return {"id": "11111111-1111-4111-8111-111111111111", "updatedAt": "2026-11-18T12:00:00.000Z",
            "emails": {"primaryEmail": "other@example.org", "additionalEmails": ["A_B@example.org"]},
            "nyteEnterpriseRequestTags": tags}


def find(*people, more=False):
    return {"data": {"people": {"pageInfo": {"hasNextPage": more}, "edges": [{"node": p} for p in people]}}}


def client_for(*replies):
    calls = []
    results = iter(replies)

    def send(req):
        assert str(req.url) == crm.API
        assert "a_b@" not in str(req.url)
        calls.append(json.loads(req.content))
        res = next(results)
        return httpx.Response(200, json=res) if isinstance(res, dict) else httpx.Response(res)

    return httpx.Client(transport=httpx.MockTransport(send)), calls


def test_create_then_replay_creates_only_one_contact():
    row = record()
    client, calls = client_for(find(), {"data": {"createPerson": {"id": str(row.request_id),
                              "nyteEnterpriseRequestTags": ["BUYER_DECK"]}}}, find(person(["BUYER_DECK"])))
    with client:
        crm.sync_contact(client, row)
        crm.sync_contact(client, row)
    assert [x["query"] for x in calls] == [crm.FIND, crm.CREATE, crm.FIND]
    assert calls[0]["variables"]["filter"]["or"][0]["emails"]["primaryEmail"]["ilike"] == r"a\_b@example.org"
    assert calls[1]["variables"]["data"] == {"id": str(row.request_id), "emails": {"primaryEmail": row.email},
                                             "nyteEnterpriseRequestTags": ["BUYER_DECK"]}


def test_merge_tags_on_additional_email_preserves_contact_and_other_tags():
    p = person(["SECURITY_PACK"])
    client, calls = client_for(find(p), {"data": {"updatePeople": [{"id": p["id"],
                                    "nyteEnterpriseRequestTags": ["SECURITY_PACK", "BUYER_DECK"]}]}})
    with client:
        crm.sync_contact(client, record())
    assert calls[1]["variables"] == {"filter": {"id": {"eq": p["id"]}, "updatedAt": {"eq": p["updatedAt"]}},
                                      "data": {"nyteEnterpriseRequestTags": ["SECURITY_PACK", "BUYER_DECK"]}}


@pytest.mark.parametrize("reply", [find(person(), person()), find(more=True), find(person() | {
    "emails": {"primaryEmail": "aXb@example.org", "additionalEmails": []}}),
    {"errors": [{"message": "a_b@example.org"}]}, 403, 429, 500])
def test_uncertain_or_failed_lookup_never_creates(reply):
    client, calls = client_for(reply)
    with client, pytest.raises(crm.SyncError) as caught:
        crm.sync_contact(client, record())
    assert len(calls) == 1 and "@" not in str(caught.value)


def test_concurrent_edit_returns_retry_without_overwriting_other_tags():
    client, _ = client_for(find(person()), {"data": {"updatePeople": []}})
    with client, pytest.raises(crm.SyncError, match="retry"):
        crm.sync_contact(client, record())


def test_crm_failure_never_acknowledges(monkeypatch):
    from trusted_router import storage_gcp_enterprise_requests as transport

    session = Mock()
    import base64
    envelope = {"ackId": "ack", "message": {"data": base64.b64encode(record().model_dump_json().encode()).decode()}}
    session.post.return_value = Mock(status_code=200, json=lambda: {"receivedMessages": [envelope]})
    with pytest.raises(crm.SyncError):
        transport.drain_once(session, Mock(side_effect=crm.SyncError("offline")),
                             subscription=transport.CRM_SUBSCRIPTION, batch=1)
    assert session.post.call_count == 1
    assert session.post.call_args.args[0] == transport.CRM_SUBSCRIPTION + ":pull"
