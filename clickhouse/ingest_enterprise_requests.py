"""Drain private enterprise inquiries. No body, address or credential in output."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
from collections.abc import Callable

import httpx
from google.auth.transport.requests import AuthorizedSession
from pydantic import ValidationError

from trusted_router.services.enterprise_requests import (
    SUBSCRIPTION,
    EnterpriseRequest,
    queue_session,
)


def insert_requests(records: list[EnterpriseRequest]) -> None:
    rows = []
    for record in records:
        row = record.model_dump(mode="json")
        row["requested_at"] = record.requested_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        rows.append(json.dumps(row, separators=(",", ":")))
    # On-node only. Never fall back to a generic analytics username or URL.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
        response = client.post(
            "http://127.0.0.1:8123/",
            params={"query": "INSERT INTO tr_enterprise.requests FORMAT JSONEachRow",
                    "insert_quorum": "2", "insert_quorum_timeout": "10000"},
            headers={"X-ClickHouse-User": "tr_enterprise_insert",
                     "X-ClickHouse-Key": os.environ["TR_ENTERPRISE_INSERT_PASSWORD"]},
            content=("\n".join(rows) + "\n").encode(),
        )
        if response.status_code != 200:
            raise RuntimeError("enterprise request insert failed")


def drain_once(session: AuthorizedSession, insert: Callable[[list[EnterpriseRequest]], None]) -> tuple[int, int]:
    response = session.post(f"{SUBSCRIPTION}:pull", json={"maxMessages": 50}, timeout=65, allow_redirects=False)
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
        except (KeyError, TypeError, ValueError, binascii.Error, ValidationError):
            rejected += 1  # Leave unacknowledged; alert on oldest outstanding message.
            continue
        records.append(record)
        ack_ids.append(ack_id)
    if records:
        insert(records)
        ack = session.post(f"{SUBSCRIPTION}:acknowledge", json={"ackIds": ack_ids},
                           timeout=10, allow_redirects=False)
        if ack.status_code != 200:
            raise RuntimeError("enterprise request acknowledgement failed")
    return len(records), rejected


def main() -> int:
    try:
        with queue_session() as session:
            count, rejected = drain_once(session, insert_requests)
    except Exception:
        print("Enterprise request drain failed; retry pending.")
        return 1
    print(f"Stored={count} invalid={rejected}")
    return int(bool(rejected))


if __name__ == "__main__":
    raise SystemExit(main())
