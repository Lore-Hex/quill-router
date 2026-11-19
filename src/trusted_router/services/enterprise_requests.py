"""Private contact records. Never include this payload in acquisition telemetry."""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Literal

import google.auth
from email_validator import validate_email
from google.auth.transport.requests import AuthorizedSession
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

BASE = "https://pubsub.googleapis.com/v1/projects/quill-cloud-proxy"
TOPIC = f"{BASE}/topics/tr-enterprise-requests:publish"
SUBSCRIPTION = f"{BASE}/subscriptions/tr-enterprise-requests-clickhouse"
SITES = frozenset(json.loads((Path(__file__).parents[1] / "data/token_exchange_origins.json").read_text()))
RequestKind = Literal["BUYER_DECK", "SECURITY_PACK", "MARKET_DATA", "SAMPLE_DATASET"]


class EnterpriseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    request_id: uuid.UUID
    requested_at: AwareDatetime
    request_type: RequestKind
    source_site: str
    email: str = Field(max_length=320, repr=False)

    @classmethod
    def new(cls, *, email: str, request_type: RequestKind, source_site: str) -> EnterpriseRequest:
        return cls(request_id=uuid.uuid4(), requested_at=dt.datetime.now(dt.UTC), email=email,
                   request_type=request_type, source_site=source_site)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        address = validate_email(value, check_deliverability=False, allow_smtputf8=False)
        return address.ascii_email or address.normalized

    @field_validator("source_site")
    @classmethod
    def known_site(cls, value: str) -> str:
        if value != "trustedrouter.com" and value not in SITES:
            raise ValueError("unknown source site")
        return value


def queue_session() -> AuthorizedSession:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/pubsub"])
    session = AuthorizedSession(credentials)
    session.trust_env = False
    return session


def publish_request(record: EnterpriseRequest) -> bool:
    data = base64.b64encode(record.model_dump_json().encode()).decode("ascii")
    with queue_session() as session:
        response = session.post(TOPIC, json={"messages": [{"data": data}]}, timeout=5, allow_redirects=False)
        return response.status_code == 200 and bool(response.json().get("messageIds"))
