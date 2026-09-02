from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.serialization import user_model_owner_shape
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import UserProvidedModel
from trusted_router.storage_postgres import PostgresStore


@pytest.mark.parametrize("count", [0, 1, 10])
def test_spanner_custom_model_list_is_one_read_statement(count: int) -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user(f"custom-list-{count}@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    for index in range(count):
        store.create_custom_model(
            owner_user_id=user.id,
            owner_workspace_id=workspace.id,
            name=f"Model {index}",
            base_model_id="anthropic/claude-sonnet-4.6",
            hidden_prompt=f"Prompt {index}",
            slug=f"custom-list-{count}-{index}",
        )

    database.snapshot_execute_sql_calls = 0
    database.snapshot_sql.clear()
    models = store.list_custom_models_for_user(user.id)

    assert [model.name for model in models] == [f"Model {index}" for index in range(count)]
    assert database.snapshot_execute_sql_calls == 1
    assert len(database.snapshot_sql) == 1
    assert "custom_model_list_for_user" in database.snapshot_sql[0]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_spanner_user_model_list_is_one_read_statement(count: int) -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user(f"user-model-list-{count}@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    for index in range(count):
        store.create_user_model(
            owner_user_id=user.id,
            owner_workspace_id=workspace.id,
            name=f"Operator {index}",
            kind="machine",
            endpoint_url=f"https://operator-{index}.example/v1",
            slug=f"user-model-list-{count}-{index}",
        )

    database.snapshot_execute_sql_calls = 0
    database.snapshot_sql.clear()
    models = store.list_user_models_for_user(user.id)

    assert [model.name for model in models] == [f"Operator {index}" for index in range(count)]
    assert database.snapshot_execute_sql_calls == 1
    assert len(database.snapshot_sql) == 1
    assert "user_model_list_for_user" in database.snapshot_sql[0]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_spanner_user_model_batch_lookup_is_at_most_one_statement(count: int) -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user(f"user-model-batch-{count}@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    model_ids: list[str] = []
    for index in range(count):
        model = store.create_user_model(
            owner_user_id=user.id,
            owner_workspace_id=workspace.id,
            name=f"Batch operator {index}",
            kind="machine",
            endpoint_url=f"https://batch-operator-{index}.example/v1",
            slug=f"user-model-batch-{count}-{index}",
        )
        model_ids.append(model.id)

    database.snapshot_execute_sql_calls = 0
    database.snapshot_sql.clear()
    lookup_ids = model_ids + (["tr-user-model/missing-model"] if model_ids else [])
    models = store.get_user_models_by_ids(lookup_ids)

    assert list(models) == model_ids
    assert database.snapshot_execute_sql_calls == (0 if count == 0 else 1)
    assert len(database.snapshot_sql) == (0 if count == 0 else 1)


def test_postgres_user_model_batch_lookup_is_one_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [
        UserProvidedModel(
            id=f"tr-user-model/pg-batch-{index}",
            owner_user_id="usr_owner",
            owner_workspace_id="ws_owner",
            name=f"Postgres model {index}",
            kind="machine",
            endpoint_url=f"https://pg-batch-{index}.example/v1",
        )
        for index in range(3)
    ]
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def fetchall(self):
            return [(model.id, json.dumps(model.__dict__)) for model in models]

    class Connection:
        def execute(self, sql: str, params: tuple[object, ...]):
            calls.append((sql, params))
            return Cursor()

    store = object.__new__(PostgresStore)
    monkeypatch.setattr(
        store,
        "_run_transaction",
        lambda operation: operation(Connection()),
    )

    result = store.get_user_models_by_ids(
        [
            f" {models[0].id.upper()} ",
            f" {models[1].id.upper()} ",
            models[0].id,
            models[2].id,
        ]
    )

    assert list(result) == [model.id for model in models]
    assert len(calls) == 1
    sql, params = calls[0]
    assert sql.count("%s") == 2
    assert params == ("user_provided_model", [model.id for model in models])


def test_spanner_owner_model_join_rejects_noncanonical_and_unsafe_pointers() -> None:
    store, database, _bigtable = make_fake_store()
    alice = store.ensure_user("owner-index-alice@example.com")
    bob = store.ensure_user("owner-index-bob@example.com")
    alice_workspace = store.list_workspaces_for_user(alice.id)[0]
    bob_workspace = store.list_workspaces_for_user(bob.id)[0]
    alice_model = store.create_custom_model(
        owner_user_id=alice.id,
        owner_workspace_id=alice_workspace.id,
        name="Alice model",
        base_model_id="anthropic/claude-sonnet-4.6",
        hidden_prompt="alice",
        slug="owner-index-alice",
    )
    bob_model = store.create_custom_model(
        owner_user_id=bob.id,
        owner_workspace_id=bob_workspace.id,
        name="Bob model",
        base_model_id="anthropic/claude-sonnet-4.6",
        hidden_prompt="bob",
        slug="owner-index-bob",
    )
    alice_user_model = store.create_user_model(
        owner_user_id=alice.id,
        owner_workspace_id=alice_workspace.id,
        name="Alice operator",
        kind="machine",
        endpoint_url="https://alice-operator.example/v1",
        slug="owner-index-alice-operator",
    )
    store._write_entity(
        "custom_model_by_user",
        f"{alice.id}#same-owner-alias",
        {"model_id": alice_model.id},
    )
    store._write_entity(
        "user_provided_model_by_user",
        f"{alice.id}#same-owner-alias",
        {"model_id": alice_user_model.id},
    )
    store._write_entity(
        "custom_model_by_user",
        f"{alice.id}#cross-owner",
        {"model_id": bob_model.id},
    )
    store._write_entity(
        "custom_model_by_user",
        f"{alice.id}#dangling",
        {"model_id": "tr-user-model/missing-model"},
    )

    database.snapshot_execute_sql_calls = 0
    listed_custom = store.list_custom_models_for_user(alice.id)
    listed_user = store.list_user_models_for_user(alice.id)

    assert [model.id for model in listed_custom] == [alice_model.id]
    assert [model.id for model in listed_user] == [alice_user_model.id]
    assert database.snapshot_execute_sql_calls == 2


def test_spanner_batched_model_decoders_ignore_future_fields() -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user("future-model-fields@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    model = store.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Forward compatible",
        kind="machine",
        endpoint_url="https://future-model-fields.example/v1",
        slug="future-model-fields",
    )
    key = ("user_provided_model", model.id)
    row = database.rows[key]
    body = json.loads(row.body)
    body["future_release_field"] = {"nested": True}
    database.rows[key] = type(row)(body=json.dumps(body), version=row.version)

    listed = store.list_user_models_for_user(user.id)
    batched = store.get_user_models_by_ids([model.id])

    assert listed[0].id == model.id
    assert batched[model.id].name == "Forward compatible"


def test_user_model_owner_shape_reuses_known_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    owner = store.ensure_user("owner-shape@example.com")
    workspace = store.list_workspaces_for_user(owner.id)[0]
    model = UserProvidedModel(
        id="tr-user-model/owner-shape-model",
        owner_user_id=owner.id,
        owner_workspace_id=workspace.id,
        name="Known owner",
        kind="machine",
        endpoint_url="https://owner-shape.example/v1",
        display_identity="verified_name",
    )
    owner.identity_status = "approved"
    owner.identity_verified_name = "Ada Owner"

    def reject_redundant_read(_self: InMemoryStore, _user_id: str):
        raise AssertionError("known owner must not be fetched again")

    monkeypatch.setattr(InMemoryStore, "get_user", reject_redundant_read)

    shape = user_model_owner_shape(model, owner=owner)

    assert shape["operator"]["display"] == "Ada Owner"


def test_console_earnings_bulk_hydrates_model_names(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = STORE.ensure_user("earnings-batch-route@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="earnings batch route",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    models = [
        STORE.create_user_model(
            owner_user_id=user.id,
            owner_workspace_id=workspace.id,
            name=f"Batch earnings model {index}",
            kind="machine",
            endpoint_url=f"https://earnings-batch-{index}.example/v1",
            slug=f"earnings-batch-route-{index}",
        )
        for index in range(2)
    ]
    for index, model in enumerate(models):
        assert STORE.credit_user_earnings(
            user.id,
            (index + 1) * 1_000_000,
            f"custom_model_payout:earnings-batch-route-{index}",
            custom_model_id=model.id,
            payer_workspace_id=workspace.id,
        )

    original_batch = getattr(InMemoryStore, "get_user_models_by_ids", None)
    batch_calls: list[list[str]] = []

    def count_batch(
        store: InMemoryStore,
        model_ids: list[str],
    ) -> dict[str, UserProvidedModel]:
        batch_calls.append(list(model_ids))
        assert original_batch is not None
        return original_batch(store, model_ids)

    def reject_point_read(_store: InMemoryStore, model_id: str):
        raise AssertionError(f"earnings rendered with point read for {model_id}")

    monkeypatch.setattr(
        InMemoryStore,
        "get_user_models_by_ids",
        count_batch,
        raising=False,
    )
    monkeypatch.setattr(InMemoryStore, "get_user_model", reject_point_read)

    response = client.get("/console/earnings")

    assert response.status_code == 200
    assert batch_calls == [[model.id for model in models]]
    assert all(model.name in response.text for model in models)


def test_console_user_models_passes_authenticated_owner_to_serializer(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = STORE.ensure_user("user-model-owner-route@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    user.identity_status = "approved"
    user.identity_verified_name = "Ada Console Owner"
    STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Owner reuse route model",
        kind="machine",
        endpoint_url="https://owner-reuse-route.example/v1",
        slug="owner-reuse-route",
        display_identity="verified_name",
    )
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="owner reuse route",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    original_get_user = InMemoryStore.get_user
    user_reads: list[str] = []

    def count_user(store: InMemoryStore, user_id: str):
        user_reads.append(user_id)
        return original_get_user(store, user_id)

    monkeypatch.setattr(InMemoryStore, "get_user", count_user)

    response = client.get("/console/user-models")

    assert response.status_code == 200
    assert "Owner reuse route model" in response.text
    assert user_reads == []


def test_session_context_keeps_management_role_for_console_fallback() -> None:
    store = InMemoryStore()
    user = store.ensure_user("fallback-role@example.com")
    bound_workspace = store.list_workspaces_for_user(user.id)[0]
    fallback_workspace = store.create_workspace(user.id, "Visible fallback")
    store.members[(bound_workspace.id, user.id)].role = ""
    raw_session, _session = store.create_auth_session(
        user_id=user.id,
        provider="test",
        label="fallback role",
        ttl_seconds=3600,
        workspace_id=bound_workspace.id,
    )

    context = store.session_auth_context(raw_session, requested_workspace_id=None)

    assert context is not None
    assert context.workspace == bound_workspace
    assert context.is_management is False
    assert context.workspaces == (fallback_workspace,)
    assert context.management_workspace_ids == frozenset({fallback_workspace.id})


@pytest.mark.parametrize(
    ("path", "expected_lifetime_reads"),
    [
        ("/console/settings", 0),
        ("/console/account/verification", 1),
    ],
)
def test_console_display_reuses_fresh_auth_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_lifetime_reads: int,
) -> None:
    user = STORE.ensure_user(f"display-context-{path.rsplit('/', 1)[-1]}@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="display context",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)

    reads = {"user": 0, "management": 0, "lifetime": 0}
    original_get_user = InMemoryStore.get_user
    original_user_can_manage = InMemoryStore.user_can_manage
    original_lifetime = InMemoryStore.get_lifetime_topup_microdollars

    def count_user(store: InMemoryStore, user_id: str):
        reads["user"] += 1
        return original_get_user(store, user_id)

    def count_management(store: InMemoryStore, user_id: str, workspace_id: str):
        reads["management"] += 1
        return original_user_can_manage(store, user_id, workspace_id)

    def count_lifetime(
        store: InMemoryStore,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> int:
        reads["lifetime"] += 1
        return original_lifetime(store, user_id, allow_stale=allow_stale)

    monkeypatch.setattr(InMemoryStore, "get_user", count_user)
    monkeypatch.setattr(InMemoryStore, "user_can_manage", count_management)
    monkeypatch.setattr(
        InMemoryStore,
        "get_lifetime_topup_microdollars",
        count_lifetime,
    )

    response = client.get(path)

    assert response.status_code == 200
    assert reads == {
        "user": 0,
        "management": 0,
        "lifetime": expected_lifetime_reads,
    }


def test_verification_display_reuses_stale_total_but_post_gate_stays_strong(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = STORE.ensure_user("verification-read-strength@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="verification strength",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    original = InMemoryStore.get_lifetime_topup_microdollars
    stale_flags: list[bool] = []

    def record_strength(
        store: InMemoryStore,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> int:
        stale_flags.append(allow_stale)
        return original(store, user_id, allow_stale=allow_stale)

    monkeypatch.setattr(
        InMemoryStore,
        "get_lifetime_topup_microdollars",
        record_strength,
    )

    display = client.get("/console/account/verification")
    assert display.status_code == 200
    assert stale_flags == [True]

    stale_flags.clear()
    start = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )
    assert start.status_code == 303
    assert stale_flags == [False]
