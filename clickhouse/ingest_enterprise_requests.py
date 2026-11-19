"""Drain private enterprise inquiries. No body, address or credential in output."""

from __future__ import annotations

import datetime as dt
import json
import os

import httpx

from trusted_router.services.enterprise_requests import EnterpriseRequest
from trusted_router.storage_gcp_enterprise_requests import drain_once, queue_session


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
