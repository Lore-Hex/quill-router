"""Private, replay-safe enterprise request tags. No access to mail or analytics."""

from __future__ import annotations

import os
from typing import Any

import httpx

from trusted_router.services.enterprise_requests import EnterpriseRequest

API = "https://api.twenty.com/graphql"
FIND = """query Match($filter: PersonFilterInput!) {
  people(first: 3, filter: $filter) {
    pageInfo { hasNextPage }
    edges { node { id updatedAt emails { primaryEmail additionalEmails } nyteEnterpriseRequestTags } }
  }
}"""
CREATE = """mutation Add($data: PersonCreateInput!) {
  createPerson(data: $data) { id nyteEnterpriseRequestTags }
}"""
UPDATE = """mutation Tag($filter: PersonFilterInput!, $data: PersonUpdateInput!) {
  updatePeople(filter: $filter, data: $data) { id nyteEnterpriseRequestTags }
}"""


class SyncError(RuntimeError):
    """Safe to report; never attach query variables, remote errors or credentials."""


def graphql(client: httpx.Client, query: str, **variables: Any) -> dict[str, Any]:
    response = client.post(API, json={"query": query, "variables": variables})
    if response.status_code != 200:
        raise SyncError(f"Twenty HTTP {response.status_code}")
    value = response.json()
    if value.get("errors") or not isinstance(value.get("data"), dict):
        raise SyncError("Twenty rejected operation")
    return value["data"]


def literal_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sync_contact(client: httpx.Client, record: EnterpriseRequest) -> None:
    email = record.email.casefold()
    literal = literal_like(email)
    matches = graphql(client, FIND, filter={"or": [
        {"emails": {"primaryEmail": {"ilike": literal}}},
        {"emails": {"additionalEmails": {"ilike": f'%"{literal}"%'}}},
    ]})["people"]
    edges = matches["edges"]
    if matches["pageInfo"]["hasNextPage"] or len(edges) > 1:
        raise SyncError("Ambiguous person; manual reconciliation required")
    tag = record.request_type
    if not edges:
        # A lost response reuses this UUID. If a person already exists, never
        # upsert by email with an incomplete field set: that can clear other tags.
        new = graphql(client, CREATE, data={"id": str(record.request_id),
                      "emails": {"primaryEmail": record.email}, "nyteEnterpriseRequestTags": [tag]})["createPerson"]
        if new["id"] != str(record.request_id) or tag not in new["nyteEnterpriseRequestTags"]:
            raise SyncError("Creation not confirmed")
        return
    person = edges[0]["node"]
    addresses = person["emails"]
    if email not in {x.casefold() for x in [addresses["primaryEmail"], *(addresses["additionalEmails"] or [])] if x}:
        raise SyncError("Contact email did not match exactly")
    tags = person["nyteEnterpriseRequestTags"] or []
    if tag in tags:
        return
    # Compare-and-set: never overwrite a concurrent edit using a stale array.
    updated = graphql(client, UPDATE, filter={"id": {"eq": person["id"]}, "updatedAt": {"eq": person["updatedAt"]}},
                      data={"nyteEnterpriseRequestTags": [*tags, tag]})["updatePeople"]
    if len(updated) != 1 or updated[0]["id"] != person["id"] or not set([*tags, tag]) <= set(updated[0]["nyteEnterpriseRequestTags"]):
        raise SyncError("Contact changed; retry")


def main() -> int:
    from trusted_router.storage_gcp_enterprise_requests import (
        CRM_SUBSCRIPTION,
        drain_once,
        queue_session,
    )

    done = 0
    try:
        with queue_session() as session, httpx.Client(
            headers={"Authorization": "Bearer " + os.environ["TR_TWENTY_ENTERPRISE_KEY"]},
            follow_redirects=False, trust_env=False, timeout=10,
        ) as client:
            def write(rows: list[EnterpriseRequest]) -> None:
                for row in rows:
                    sync_contact(client, row)

            for _ in range(20):
                count, invalid = drain_once(session, write, subscription=CRM_SUBSCRIPTION, batch=1)
                done += count
                if invalid:
                    raise SyncError("Invalid queue record")
                if not count:
                    break
    except Exception:
        print(f"Twenty sync failed after {done} acknowledgements; retry pending.")
        return 1
    print(f"Twenty enterprise requests acknowledged: {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
