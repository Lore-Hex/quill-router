from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted, ResourceExhausted

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore, configure_store


def _keys(client: TestClient, headers: dict[str, str], count: int) -> list[str]:
    hashes = []
    for index in range(count):
        response = client.post("/v1/keys", headers=headers, json={"name": str(index)})
        assert response.status_code == 201
        key_hash = response.json()["data"]["hash"]
        key = STORE.get_key_by_hash(key_hash)
        assert key is not None
        # Include timestamp ties, so a stable secondary order is necessary.
        key.created_at = f"2026-01-0{index // 2 + 1}T00:00:00Z"
        hashes.append(key_hash)
    return hashes


def test_key_pages_and_legacy_unbounded_response(client, user_headers):
    hashes = _keys(client, user_headers, 5)
    expected = [hashes[4], *sorted(hashes[2:4]), *sorted(hashes[:2])]
    STORE.update_key(hashes[4], {"disabled": True})
    STORE.api_keys.add_usage(hashes[3], 123, is_byok=False)
    response = client.get("/v1/keys", headers=user_headers)
    assert response.status_code == 200
    assert set(response.json()) == {"data"}
    assert [key["hash"] for key in response.json()["data"]] == expected
    assert (
        next(k for k in response.json()["data"] if k["hash"] == hashes[3])["usage_microdollars"]
        == 123
    )
    seen = []
    for offset, next_offset in [(0, 2), (2, 4), (4, None), (5, None)]:
        response = client.get(f"/v1/keys?limit=2&offset={offset}", headers=user_headers)
        assert response.status_code == 200
        assert response.json()["next_offset"] == next_offset
        page = [key["hash"] for key in response.json()["data"]]
        assert page == expected[offset : offset + 2]
        seen.extend(page)
    assert seen == expected
    response = client.get("/v1/keys?offset=2", headers=user_headers)
    assert response.status_code == 200
    assert [key["hash"] for key in response.json()["data"]] == expected[2:]
    response = client.get("/v1/keys?limit=1000", headers=user_headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 5
    assert response.json()["next_offset"] is None


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=1001",
        "limit=abc",
        "limit=1.5",
        "offset=-1",
        "offset=abc",
        "offset=0.5",
        "include_disabled=invalid",
    ],
)
def test_key_page_invalid_parameters_are_400(client, user_headers, query):
    response = client.get(f"/v1/keys?{query}", headers=user_headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 400
    assert response.json()["error"]["type"] == "bad_request"
    assert response.json()["error"]["message"].startswith("query.")


@pytest.mark.parametrize("include_disabled", [True, False])
def test_disabled_filter_applies_before_paging(client, user_headers, include_disabled):
    hashes = _keys(client, user_headers, 4)
    newest = sorted(hashes[2:])
    STORE.update_key(newest[0], {"disabled": True})
    expected = [*newest, *sorted(hashes[:2])]
    if not include_disabled:
        expected.remove(newest[0])
    response = client.get(
        "/v1/keys",
        headers=user_headers,
        params={
            "limit": 1,
            "offset": 1,
            "include_disabled": str(include_disabled).lower(),
        },
    )
    assert response.status_code == 200
    assert [k["hash"] for k in response.json()["data"]] == expected[1:2]
    assert response.json()["next_offset"] == 2


@pytest.mark.parametrize("key_count", [0, 1, 1000])
def test_list_uses_one_storage_projection_and_no_per_key_usage(
    client,
    user_headers,
    monkeypatch,
    key_count,
):
    user = STORE.ensure_user("alice@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    for index in range(key_count):
        STORE.create_api_key(workspace_id=workspace.id, name=str(index), creator_user_id=user.id)
    calls = []
    original = InMemoryStore.list_api_keys_with_usage

    def read(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    def point_read(*args, **kwargs):
        pytest.fail("list must not issue a point read per key")

    monkeypatch.setattr(InMemoryStore, "list_api_keys_with_usage", read)
    monkeypatch.setattr(InMemoryStore, "typed_key_usage", point_read)
    monkeypatch.setattr(InMemoryStore, "list_keys", point_read)
    monkeypatch.setattr(InMemoryStore, "get_key_by_hash", point_read)
    response = client.get("/v1/keys", headers=user_headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == key_count
    assert calls == [
        (
            (workspace.id,),
            {
                "limit": None,
                "offset": 0,
                "include_disabled": True,
            },
        )
    ]


def test_bulk_delete_mixed_results_and_tenant_isolation(client, user_headers):
    own = _keys(client, user_headers, 2)
    foreign_response = client.post(
        "/v1/keys",
        headers={
            "x-trustedrouter-user": "other@example.com",
        },
        json={"name": "foreign"},
    )
    assert foreign_response.status_code == 201
    foreign = foreign_response.json()["data"]["hash"]
    hashes = [own[0], "missing", foreign, own[1], own[0]]
    response = client.post("/v1/keys/bulk-delete", headers=user_headers, json={"hashes": hashes})
    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {"hash": h, "status": status}
            for h, status in zip(
                hashes,
                [
                    "deleted",
                    "not_found",
                    "not_found",
                    "deleted",
                    "deleted",
                ],
                strict=True,
            )
        ]
    }
    assert STORE.get_key_by_hash(foreign) is not None
    assert STORE.get_key_by_raw(foreign_response.json()["key"]) is not None
    assert all(STORE.get_key_by_hash(h) is None for h in own)
    retry = client.post("/v1/keys/bulk-delete", headers=user_headers, json={"hashes": own})
    assert retry.status_code == 200
    assert retry.json() == {"data": [{"hash": h, "status": "not_found"} for h in own]}


@pytest.mark.parametrize("count,code", [(0, 400), (1000, 200), (1001, 400)])
def test_bulk_delete_size_bound(client, user_headers, count, code):
    response = client.post(
        "/v1/keys/bulk-delete", headers=user_headers, json={"hashes": ["missing"] * count}
    )
    assert response.status_code == code
    if code == 400:
        assert response.json()["error"]["type"] == "bad_request"
    else:
        assert response.json() == {"data": [{"hash": "missing", "status": "not_found"}] * count}


def test_bulk_delete_requires_management_auth(client, user_headers, inference_headers):
    own = _keys(client, user_headers, 1)[0]
    for headers, status, error in [
        ({}, 401, "unauthorized"),
        (inference_headers, 403, "forbidden"),
    ]:
        response = client.post("/v1/keys/bulk-delete", headers=headers, json={"hashes": [own]})
        assert response.status_code == status
        assert response.json()["error"]["type"] == error
        assert STORE.get_key_by_hash(own) is not None
    raw, _key = STORE.create_api_key(
        workspace_id=STORE.get_key_by_hash(own).workspace_id,
        name="management",
        creator_user_id=None,
        management=True,
    )
    response = client.post(
        "/v1/keys/bulk-delete", headers={"authorization": f"Bearer {raw}"}, json={"hashes": [own]}
    )
    assert response.status_code == 200
    assert response.json() == {"data": [{"hash": own, "status": "deleted"}]}


@pytest.mark.parametrize("error_class", [Aborted, ResourceExhausted])
def test_delete_storage_contention_is_503_not_429(client, user_headers, monkeypatch, error_class):
    own = _keys(client, user_headers, 1)[0]

    def unavailable(*args, **kwargs):
        raise error_class("transient")

    monkeypatch.setattr(InMemoryStore, "delete_key", unavailable)
    response = client.delete(f"/v1/keys/{own}", headers=user_headers)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "service_unavailable"
    assert STORE.get_key_by_hash(own) is not None


@pytest.mark.parametrize("count", [0, 1, 1000])
def test_spanner_list_route_read_count_is_constant(count):
    store, database = make_fake_store()
    user = store.ensure_user(f"key-list-{count}@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, management = store.create_api_key(
        workspace_id=workspace.id,
        name="management",
        creator_user_id=user.id,
        management=True,
    )
    for index in range(count):
        store.create_api_key(workspace_id=workspace.id, name=str(index), creator_user_id=user.id)
    configure_store(store)
    try:
        with TestClient(
            create_app(
                Settings(environment="test", rate_limit_enabled=False),
                configure_store_arg=False,
                init_observability=False,
            )
        ) as client:
            before = database.snapshot_execute_sql_calls
            response = client.get("/v1/keys", headers={"authorization": f"Bearer {raw}"})
            assert response.status_code == 200
            assert len(response.json()["data"]) == count + 1
            # One joined auth context plus one joined key/usage projection.
            assert database.snapshot_execute_sql_calls - before == 2
            before = database.snapshot_execute_sql_calls
            page = client.get("/v1/keys?limit=1", headers={"authorization": f"Bearer {raw}"})
            assert page.status_code == 200
            assert len(page.json()["data"]) == 1
            assert page.json()["next_offset"] == (1 if count else None)
            assert database.snapshot_execute_sql_calls - before == 2
    finally:
        configure_store(InMemoryStore())


@pytest.mark.parametrize("operation", ["list", "delete", "bulk-delete"])
def test_key_management_storage_runs_off_the_event_loop(
    client,
    user_headers,
    monkeypatch,
    operation,
):
    own = _keys(client, user_headers, 1)[0]
    method = {
        "list": "list_api_keys_with_usage",
        "delete": "delete_key",
        "bulk-delete": "delete_keys",
    }[operation]
    original = getattr(InMemoryStore, method)
    calls = []

    def in_worker(self, *args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InMemoryStore, method, in_worker)
    if operation == "list":
        response = client.get("/v1/keys", headers=user_headers)
        assert [key["hash"] for key in response.json()["data"]] == [own]
    elif operation == "delete":
        response = client.delete(f"/v1/keys/{own}", headers=user_headers)
        assert response.json() == {"data": {"deleted": True, "hash": own}}
    else:
        response = client.post("/v1/keys/bulk-delete", headers=user_headers, json={"hashes": [own]})
        assert response.json() == {"data": [{"hash": own, "status": "deleted"}]}
    assert response.status_code == 200
    assert calls == [True]


@pytest.mark.parametrize("typed", [False, True])
def test_list_preserves_legacy_json_usage_fallback(typed):
    store, database = make_fake_store()
    user = store.ensure_user("legacy-list@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="legacy",
        creator_user_id=user.id,
        management=True,
    )
    key.usage_microdollars = 123
    store._write_entity("api_key", key.hash, key)
    if typed:
        database.typed["tr_key_limit"][(key.hash, 0)]["usage"] = 456
    else:
        for row_id in list(database.typed["tr_key_limit"]):
            if row_id[0] == key.hash:
                database.typed["tr_key_limit"].pop(row_id)
    configure_store(store)
    try:
        with TestClient(
            create_app(
                Settings(environment="test", rate_limit_enabled=False),
                configure_store_arg=False,
                init_observability=False,
            )
        ) as client:
            response = client.get("/v1/keys", headers={"authorization": f"Bearer {raw}"})
            assert response.status_code == 200
            assert len(response.json()["data"]) == 1
            shape = response.json()["data"][0]
            assert shape["usage_microdollars"] == (456 if typed else 123)
            assert shape["usage_daily_microdollars"] == (0 if typed else 123)
    finally:
        configure_store(InMemoryStore())


def _edge_default(name: str) -> int:
    script = (Path(__file__).resolve().parents[1] / "scripts/deploy/_edge_security.sh").read_text()
    match = re.search(rf'local {name}="\$\{{TR_CLOUD_ARMOR_[A-Z_]+:-([0-9]+)\}}"', script)
    assert match is not None, name
    return int(match.group(1))


@pytest.mark.parametrize("path", ["/docs", "/docs/spend-controls"])
def test_docs_quote_the_enforced_edge_limits(client, path):
    interval = _edge_default("interval")
    writes = _edge_default("write_count")
    total = _edge_default("global_count")
    assert (interval, writes, total) == (60, 300, 2400)
    response = client.get(path)
    assert response.status_code == 200
    text = " ".join(BeautifulSoup(response.text, "html.parser").get_text(" ").split())
    if path == "/docs":
        assert f"{writes} state-changing requests per {interval} seconds" in text
        assert f"{total:,} requests per {interval} seconds" in text
    else:
        assert f"{writes} state-changing requests and {total:,} requests in total per {interval} seconds" in text
