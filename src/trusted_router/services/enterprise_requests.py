"""Private contact records. Never include this payload in acquisition telemetry."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Literal

from email_validator import validate_email
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

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


def publish_request(record: EnterpriseRequest) -> bool:
    # This port is called only on the explicitly enabled GCP form surface.
    from trusted_router.storage_gcp_enterprise_requests import publish

    return publish(record.model_dump_json())
