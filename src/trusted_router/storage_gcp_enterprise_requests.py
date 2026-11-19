"""GCP transport adapter for the private enterprise request queues."""

import base64
from collections.abc import Callable

import google.auth
from google.auth.transport.requests import AuthorizedSession

from trusted_router.services.enterprise_requests import EnterpriseRequest

BASE = "https://pubsub.googleapis.com/v1/projects/quill-cloud-proxy"
TOPIC = f"{BASE}/topics/tr-enterprise-requests:publish"
SUBSCRIPTION = f"{BASE}/subscriptions/tr-enterprise-requests-clickhouse"
CRM_SUBSCRIPTION = f"{BASE}/subscriptions/tr-enterprise-requests-twenty"


def queue_session() -> AuthorizedSession:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/pubsub"])
    session = AuthorizedSession(credentials)
    session.trust_env = False
    return session


def publish(payload: str) -> bool:
    data = base64.b64encode(payload.encode()).decode("ascii")
    with queue_session() as session:
        response = session.post(TOPIC, json={"messages": [{"data": data}]}, timeout=5, allow_redirects=False)
        return response.status_code == 200 and bool(response.json().get("messageIds"))


def drain_once(session: AuthorizedSession, insert: Callable[[list[EnterpriseRequest]], None], *,
               subscription: str = SUBSCRIPTION, batch: int = 50) -> tuple[int, int]:
    if subscription not in (SUBSCRIPTION, CRM_SUBSCRIPTION):
        raise ValueError("unknown private subscription")
    response = session.post(f"{subscription}:pull", json={"maxMessages": batch}, timeout=65, allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError("enterprise request pull failed")
    records, ack_ids = [], []
    rejected = 0
    for item in response.json().get("receivedMessages", []):
        try:
            data = item["message"]["data"]
            if not isinstance(data, str) or len(data) > 4096:
                raise ValueError("invalid message size")
            record = EnterpriseRequest.model_validate_json(base64.b64decode(data, validate=True))
            ack_id = item["ackId"]
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        records.append(record)
        ack_ids.append(ack_id)
    if records:
        insert(records)
        ack = session.post(f"{subscription}:acknowledge", json={"ackIds": ack_ids},
                           timeout=10, allow_redirects=False)
        if ack.status_code != 200:
            raise RuntimeError("enterprise request acknowledgement failed")
    return len(records), rejected
