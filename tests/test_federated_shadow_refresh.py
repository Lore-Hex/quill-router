"""A federated shadow's refresh replaces only what the home plane owns.

The home record's pause is imported as the "federation" cause and its abuse
suspension as "abuse". Pauses set on this plane ("migration" via
update_workspace, principal recovery, ...) belong to this plane; re-importing
a key for the same workspace must neither drop them nor be blocked by them.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.test_federated_workspace import HOME_RECORD
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import document_pause_causes, shard_pause_causes


def _record(n: int, **home: Any) -> dict[str, Any]:
    return {**HOME_RECORD, "lookup_hash": f"lh-{n}", "key_hash": f"kh-{n}", **home}


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryStore()
    return postgres_store_on(sqlite_postgres_conn())


def _causes(store: Any) -> list[str]:
    workspace = store.get_workspace(HOME_RECORD["workspace_id"])
    assert workspace is not None
    return list(workspace.billing_pause_causes)


def test_refresh_keeps_a_local_pause(store: Any) -> None:
    store.upsert_federated_api_key(_record(1))
    store.update_workspace(HOME_RECORD["workspace_id"], billing_paused=True)

    store.upsert_federated_api_key(_record(2))

    assert _causes(store) == ["migration"]


def test_home_pause_and_local_pause_are_lifted_independently(store: Any) -> None:
    store.upsert_federated_api_key(_record(1, workspace_billing_paused=True))
    assert _causes(store) == ["federation"]
    store.update_workspace(HOME_RECORD["workspace_id"], billing_paused=True)
    assert _causes(store) == ["federation", "migration"]

    store.upsert_federated_api_key(_record(2, workspace_billing_paused=False))
    assert _causes(store) == ["migration"]

    store.upsert_federated_api_key(_record(3, workspace_billing_paused=True))
    store.update_workspace(HOME_RECORD["workspace_id"], billing_paused=False)
    assert _causes(store) == ["federation"]


def test_home_suspension_replaces_the_home_pause(store: Any) -> None:
    store.upsert_federated_api_key(_record(1, workspace_billing_paused=True))
    store.upsert_federated_api_key(
        _record(2, workspace_billing_paused=True, workspace_suspended=True)
    )
    assert _causes(store) == ["abuse"]
    store.upsert_federated_api_key(_record(3))
    assert _causes(store) == []
    workspace = store.get_workspace(HOME_RECORD["workspace_id"])
    assert workspace is not None and workspace.billing_paused is False


def test_a_shadow_written_before_federation_had_its_own_cause_unpauses(store: Any) -> None:
    store.upsert_federated_api_key(_record(1))
    # The old import wrote the home pause as "migration" with the home reason.
    store.update_workspace(
        HOME_RECORD["workspace_id"],
        billing_paused=True,
        billing_pause_reason="billing is paused on the home plane",
    )
    assert _causes(store) == ["migration"]

    store.upsert_federated_api_key(_record(2))

    assert _causes(store) == []


def test_the_home_pause_survives_rebuilds_from_the_shard_set() -> None:
    # Shard writers rebuild the document from the shards; "federation" is
    # document-owned like "migration", so it survives and never reaches them.
    assert shard_pause_causes(["federation", "abuse"]) == ["abuse"]
    assert document_pause_causes([], ["federation", "abuse"]) == ["federation"]


def test_refresh_never_mutates_the_imported_shadow() -> None:
    """Postgres retries a transaction with the same imported object."""
    from trusted_router.storage_models import Workspace, refreshed_shadow

    imported = Workspace(id="w", name="federated", owner_user_id="", federated_home="r")
    existing = Workspace(id="w", name="federated", owner_user_id="", billing_paused=True)
    merged = refreshed_shadow(existing, imported)
    assert merged.billing_pause_causes == ["migration"]
    assert imported.billing_pause_causes == [] and imported.billing_paused is False


def test_a_local_pause_after_a_home_suspension_survives_the_unsuspend(store: Any) -> None:
    store.upsert_federated_api_key(_record(1, workspace_suspended=True))
    store.update_workspace(HOME_RECORD["workspace_id"], billing_paused=True)
    assert _causes(store) == ["abuse", "migration"]

    store.upsert_federated_api_key(_record(2))

    assert _causes(store) == ["migration"]
