"""A strict-budget key with no spend limit can never gain one.

The rule (``storage_key_patch.StrictKeyFirstLimit``) makes a strict key's four
limit fields monotonic: never from all unset to any set. The Go fast path edge
relies on it to decide a limitless strict key without a key hold, so this file
pins the rule itself, the property, every route that can set a limit, the
reshard refusal and the replay tool's guard; tests/test_key_limit_writer_inventory.py
inventories every writer.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.storage import STORE
from trusted_router.storage_key_patch import (
    KEY_LIMIT_FIELDS,
    STRICT_FIRST_LIMIT_MESSAGE,
    StrictKeyFirstLimit,
    apply_key_patch,
    has_spend_limit,
)
from trusted_router.storage_models import ApiKey

ROOT = Path(__file__).resolve().parents[1]

WINDOW_FIELDS = ("limit_daily_microdollars", "limit_weekly_microdollars", "limit_monthly_microdollars")

# Every patch form that can set a limit on a key that has none.
FIRST_LIMIT_PATCHES = [
    {"limit_microdollars": 5_000_000},
    {"limit": "5"},
    {"limit_microdollars_at_most": 5_000_000},
    {"limit_daily_microdollars": 1_000_000},
    {"limit_weekly_microdollars": 2_000_000},
    {"limit_monthly_microdollars": 3_000_000},
    {"limit_microdollars": 0},
    {"limit_daily_microdollars": 0},
    {"name": "renamed", "limit_monthly_microdollars": 3_000_000},
]


def _key(**fields: Any) -> ApiKey:
    return ApiKey(
        hash="kh", salt="s", secret_hash="x",  # noqa: S106 - inert test fixture digest.
        lookup_hash="lh", name="key", label="lbl",
        workspace_id="ws", creator_user_id=None, **fields,
    )


def _limits(key: ApiKey) -> tuple[int | None, ...]:
    return tuple(getattr(key, field) for field in KEY_LIMIT_FIELDS)


# --- the rule ---------------------------------------------------------------


@pytest.mark.parametrize("patch", FIRST_LIMIT_PATCHES, ids=lambda p: ",".join(sorted(p)))
def test_a_limitless_strict_key_cannot_gain_a_first_limit(patch: dict[str, Any]) -> None:
    key = _key(budget_strict=True)
    before = dataclasses.asdict(key)
    with pytest.raises(StrictKeyFirstLimit) as raised:
        apply_key_patch(key, patch)
    assert str(raised.value) == STRICT_FIRST_LIMIT_MESSAGE
    # Refused whole: no field of the patch landed, the name included.
    assert dataclasses.asdict(key) == before


def test_the_refusal_is_a_value_error_so_every_existing_handler_still_catches_it() -> None:
    assert issubclass(StrictKeyFirstLimit, ValueError)


def test_the_message_says_what_to_do_and_what_removal_means() -> None:
    assert "Create a new strict key with the limits instead" in STRICT_FIRST_LIMIT_MESSAGE
    assert "can never have a limit again" in STRICT_FIRST_LIMIT_MESSAGE


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "renamed"},
        {"disabled": True},
        {"budget_alert_only": True},
        {"tags": {"team": "a"}},
        {"lenient": True},
        {"include_byok_in_limit": False},
        {"limit_reset": "monthly"},
        {"limit_microdollars": None},
        {"limit": None},
        {field: None for field in WINDOW_FIELDS},
        {"budget_alerted": {"daily": "2026-11-24T00:00:00Z"}},
    ],
    ids=lambda p: ",".join(sorted(p)),
)
def test_a_limitless_strict_key_takes_every_patch_that_sets_no_limit(patch: dict[str, Any]) -> None:
    key = _key(budget_strict=True)
    apply_key_patch(key, patch)
    assert not has_spend_limit(key)


@pytest.mark.parametrize("field", KEY_LIMIT_FIELDS)
def test_a_strict_key_with_a_limit_can_change_and_remove_it(field: str) -> None:
    key = _key(budget_strict=True, **{field: 10_000_000})
    apply_key_patch(key, {field: 20_000_000})
    assert getattr(key, field) == 20_000_000
    # Another limit beside it: the key already has one, so it is no first limit.
    other = next(f for f in KEY_LIMIT_FIELDS if f != field)
    apply_key_patch(key, {other: 1_000_000})
    assert getattr(key, other) == 1_000_000
    apply_key_patch(key, {other: None})
    apply_key_patch(key, {field: None})
    assert not has_spend_limit(key)
    # The last limit removed: the key is limitless for good.
    with pytest.raises(StrictKeyFirstLimit):
        apply_key_patch(key, {field: 20_000_000})
    assert not has_spend_limit(key)


def test_removing_the_last_limit_and_adding_another_in_one_patch_is_allowed() -> None:
    # The rule reads the stored key: it has a limit, so the patch gives it no
    # first limit, and it ends with one.
    key = _key(budget_strict=True, limit_daily_microdollars=5)
    apply_key_patch(key, {"limit_daily_microdollars": None, "limit_monthly_microdollars": 9})
    assert _limits(key) == (None, None, None, 9)


def test_limit_at_most_lowers_a_strict_keys_existing_limit() -> None:
    key = _key(budget_strict=True, limit_microdollars=10)
    apply_key_patch(key, {"limit_microdollars_at_most": 4})
    assert key.limit_microdollars == 4


@pytest.mark.parametrize("patch", FIRST_LIMIT_PATCHES, ids=lambda p: ",".join(sorted(p)))
def test_a_key_that_is_not_strict_keeps_todays_semantics(patch: dict[str, Any]) -> None:
    key = _key()
    apply_key_patch(key, patch)
    assert has_spend_limit(key)


# --- the property PR 2's edge relies on ----------------------------------

_LIMIT_VALUES = st.one_of(st.none(), st.integers(min_value=0, max_value=10**12))
_PATCH = st.fixed_dictionaries(
    {},
    optional={
        "limit_microdollars": _LIMIT_VALUES,
        "limit": st.one_of(st.none(), st.integers(min_value=0, max_value=10**6).map(str)),
        "limit_microdollars_at_most": st.one_of(st.none(), st.integers(min_value=0, max_value=10**12)),
        "limit_daily_microdollars": _LIMIT_VALUES,
        "limit_weekly_microdollars": _LIMIT_VALUES,
        "limit_monthly_microdollars": _LIMIT_VALUES,
        "name": st.sampled_from(["a", "b"]),
        "disabled": st.booleans(),
        "budget_alert_only": st.booleans(),
    },
)
_START = st.tuples(*(_LIMIT_VALUES for _ in KEY_LIMIT_FIELDS))


@settings(max_examples=400, deadline=None)
@given(start=_START, patches=st.lists(_PATCH, max_size=12))
def test_a_strict_keys_limits_never_go_from_none_to_some(
    start: tuple[int | None, ...], patches: list[dict[str, Any]],
) -> None:
    """Over any sequence of patches: once a strict key is limitless it stays
    limitless, and a refused patch changes nothing."""
    key = _key(budget_strict=True, **dict(zip(KEY_LIMIT_FIELDS, start, strict=True)))
    was_limitless = not has_spend_limit(key)
    for patch in patches:
        before = dataclasses.asdict(key)
        try:
            apply_key_patch(key, patch)
        except StrictKeyFirstLimit:
            assert not has_spend_limit(key)
            assert dataclasses.asdict(key) == before
            continue
        if was_limitless:
            assert not has_spend_limit(key), patch
        was_limitless = not has_spend_limit(key)


# --- the routes -------------------------------------------------------------


@pytest.fixture
def owner(client: TestClient) -> dict[str, Any]:
    """A signed-in console session for a workspace owner, who may also use
    the /v1/keys routes with the session cookie."""
    user = STORE.ensure_user("strict-first-limit@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="t", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    client.headers["Origin"] = "http://testserver"
    return {"client": client, "workspace": workspace, "user": user}


def _strict_key(owner: dict[str, Any], **limits: int) -> ApiKey:
    _raw, key = STORE.create_api_key(
        workspace_id=owner["workspace"].id, name="strict", creator_user_id=owner["user"].id,
        budget_strict=True, **limits,
    )
    return key


@pytest.mark.parametrize(
    "body", [{"limit": "5"}, {"limit_daily": "1"}, {"limit_weekly": "2"}, {"limit_monthly": "3"},
             {"name": "renamed", "limit": 0}],
)
def test_the_public_patch_answers_400_and_leaves_the_key(owner: dict[str, Any], body: dict[str, Any]) -> None:
    client, key = owner["client"], _strict_key(owner)
    resp = client.patch(f"/v1/keys/{key.hash}", json=body)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["type"] == "bad_request"
    assert error["message"] == STRICT_FIRST_LIMIT_MESSAGE
    stored = STORE.get_key_by_hash(key.hash)
    assert not has_spend_limit(stored) and stored.name == "strict"


def test_the_public_patch_answers_like_a_change_to_budget_strict(owner: dict[str, Any]) -> None:
    """The status and type the validation handler gives budget_strict itself."""
    client, key = owner["client"], _strict_key(owner)
    flag = client.patch(f"/v1/keys/{key.hash}", json={"budget_strict": False})
    limit = client.patch(f"/v1/keys/{key.hash}", json={"limit_daily": "1"})
    assert flag.status_code == limit.status_code == 400
    assert flag.json()["error"]["type"] == limit.json()["error"]["type"] == "bad_request"


def test_the_public_patch_still_changes_a_strict_keys_limits(owner: dict[str, Any]) -> None:
    client, key = owner["client"], _strict_key(owner, limit_daily_microdollars=1_000_000)
    resp = client.patch(f"/v1/keys/{key.hash}", json={"limit_daily": "2", "limit_monthly": "30"})
    assert resp.status_code == 200, resp.text
    stored = STORE.get_key_by_hash(key.hash)
    assert stored.limit_daily_microdollars == 2_000_000
    assert stored.limit_monthly_microdollars == 30_000_000
    assert client.patch(f"/v1/keys/{key.hash}", json={"name": "renamed"}).status_code == 200


def test_creating_a_strict_key_with_limits_is_unchanged(owner: dict[str, Any]) -> None:
    client = owner["client"]
    resp = client.post("/v1/keys", json={"name": "s", "budget_strict": True, "limit_daily": 5, "limit": 50})
    assert resp.status_code == 201, resp.text
    stored = STORE.get_key_by_hash(resp.json()["data"]["hash"])
    assert stored.budget_strict and stored.limit_daily_microdollars == 5_000_000
    assert stored.limit_microdollars == 50_000_000


def test_a_key_that_is_not_strict_still_gains_a_first_limit(owner: dict[str, Any]) -> None:
    client = owner["client"]
    _raw, key = STORE.create_api_key(
        workspace_id=owner["workspace"].id, name="plain", creator_user_id=owner["user"].id,
    )
    assert client.patch(f"/v1/keys/{key.hash}", json={"limit_daily": "1"}).status_code == 200
    assert STORE.get_key_by_hash(key.hash).limit_daily_microdollars == 1_000_000


def test_the_console_refuses_with_its_own_error_and_leaves_the_key(owner: dict[str, Any]) -> None:
    client, key = owner["client"], _strict_key(owner)
    resp = client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "", "limit_daily": "5", "limit_weekly": "", "limit_monthly": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/api-keys?error=strict-first-limit"
    assert not has_spend_limit(STORE.get_key_by_hash(key.hash))
    page = client.get("/console/api-keys?error=strict-first-limit").text
    assert "Create a new strict key with the limits instead" in page


def test_the_console_shows_a_limitless_strict_key_no_budget_form(owner: dict[str, Any]) -> None:
    client = owner["client"]
    limitless = _strict_key(owner)
    limited = _strict_key(owner, limit_monthly_microdollars=9_000_000)
    page = client.get("/console/api-keys").text
    assert f'action="/console/api-keys/{limitless.hash}/limit"' not in page
    assert "A strict key cannot gain a first limit" in page
    assert f'action="/console/api-keys/{limited.hash}/limit"' in page


def test_the_console_removes_a_strict_keys_last_limit_for_good(owner: dict[str, Any]) -> None:
    client, key = owner["client"], _strict_key(owner, limit_daily_microdollars=5_000_000)
    blank = {"limit": "", "limit_daily": "", "limit_weekly": "", "limit_monthly": ""}
    resp = client.post(f"/console/api-keys/{key.hash}/limit", data=blank, follow_redirects=False)
    assert resp.headers["location"] == "/console/api-keys?saved=limit"
    assert not has_spend_limit(STORE.get_key_by_hash(key.hash))
    resp = client.post(
        f"/console/api-keys/{key.hash}/limit", data={**blank, "limit_daily": "5"}, follow_redirects=False,
    )
    assert resp.headers["location"] == "/console/api-keys?error=strict-first-limit"


def test_an_oauth_grant_budget_on_a_strict_key_is_a_client_error(owner: dict[str, Any]) -> None:
    """Delegated keys are never created strict; should one be, a monthly
    budget on it while it has none is refused 400 and the grant's keys are
    left as they were."""
    from trusted_router.routes.oauth_authorized_apps import _update_all_or_rollback

    key = _strict_key(owner)
    with pytest.raises(HTTPException) as raised:
        _update_all_or_rollback([key], field="limit_monthly_microdollars", value=5_000_000)
    assert raised.value.status_code == 400
    assert not has_spend_limit(STORE.get_key_by_hash(key.hash))


def test_the_chat_browser_key_writers_never_meet_a_strict_key() -> None:
    """chat_browser_key's limit-at-most lowers run only on keys it created,
    always with a lifetime limit and never strict, so the rule cannot fire
    there; pinned so a change to either side is seen."""
    source = (ROOT / "src/trusted_router/routes/internal/chat_browser_key.py").read_text()
    tree = ast.parse(source)
    creates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_api_key"
    ]
    assert creates
    for call in creates:
        names = {k.arg for k in call.keywords}
        assert "limit_microdollars" in names and "budget_strict" not in names


# --- the replay tool ------------------------------------------------------


def _replay_module() -> Any:
    import importlib.util

    path = ROOT / "scripts" / "deploy" / "replay_orphan_writes.py"
    spec = importlib.util.spec_from_file_location("replay_orphan_writes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body(**fields: Any) -> str:
    base = {"hash": "kh", "name": "key", "budget_strict": False, "include_byok_in_limit": True,
            "usage_shard_count": 1, **dict.fromkeys(KEY_LIMIT_FIELDS)}
    return json.dumps({**base, **fields})


@pytest.mark.parametrize(
    ("target", "replayed", "reason"),
    [
        (None, _body(budget_strict=True, limit_daily_microdollars=5), None),
        (_body(budget_strict=True), _body(budget_strict=True, limit_daily_microdollars=5), "strict_first_limit"),
        (_body(budget_strict=True), _body(budget_strict=True, limit_microdollars=5), "strict_first_limit"),
        (_body(budget_strict=True), _body(budget_strict=True, name="renamed"), None),
        (_body(budget_strict=True, limit_monthly_microdollars=9), _body(budget_strict=True, limit_monthly_microdollars=8),
         "config_differs"),
        (_body(limit_daily_microdollars=5), _body(limit_daily_microdollars=6), "config_differs"),
        (_body(), _body(include_byok_in_limit=False), "config_differs"),
        (_body(), _body(usage_shard_count=4), "config_differs"),
        (_body(), _body(budget_strict=True), "config_differs"),
        (_body(), _body(name="renamed", disabled=True), None),
        ("not json", _body(), "unreadable"),
        (_body(), "[]", "unreadable"),
    ],
)
def test_the_replay_refuses_what_a_raw_write_would_break(target: str | None, replayed: str, reason: str | None) -> None:
    assert _replay_module().api_key_replay_refusal(target, replayed) == reason


def test_the_replay_cannot_give_a_limitless_strict_key_its_limit_back() -> None:
    """The reviewer's case on the real Spanner store fake: a strict key with a
    daily limit has its last limit removed; replaying its older body is
    refused in the transaction that would write it, the key stays limitless,
    and a later include_byok_in_limit patch mirrors no limit. A key the target
    lacks, and a metadata-only change, are written."""
    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE

    store, database = make_fake_store()
    _raw, key = store.api_keys.create(workspace_id="ws-replay", name="strict", creator_user_id=None,
                                      budget_strict=True, limit_daily_microdollars=5_000_000)
    rows_before = list(database.run_in_transaction(lambda txn: list(txn.execute_sql(
        "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id", params={"kind": "api_key", "id": key.hash}))))
    older = rows_before[0][0]
    assert store.update_key(key.hash, {"limit_daily_microdollars": None}) is not None
    assert not has_spend_limit(store.get_key_by_hash(key.hash))

    replay = _replay_module()
    _raw, other = store.api_keys.create(workspace_id="ws-replay", name="plain", creator_user_id=None)
    renamed = json.dumps({**dataclasses.asdict(store.get_key_by_hash(other.hash)), "name": "renamed"})
    missing = json.dumps({**dataclasses.asdict(store.get_key_by_hash(other.hash)), "hash": "kh-missing", "name": "new"})
    refused = replay.replay_api_keys(database, [
        ("api_key", key.hash, older, None),
        ("api_key", other.hash, renamed, None),
        ("api_key", "kh-missing", missing, None),
    ])
    assert refused == [(key.hash, "strict_first_limit")]
    assert not has_spend_limit(store.get_key_by_hash(key.hash))
    assert store.get_key_by_hash(other.hash).name == "renamed"
    assert store.get_key_by_hash("kh-missing") is not None
    with pytest.raises(ValueError, match="api_key records only"):
        replay.replay_api_keys(database, [("workspace", "ws-replay", "{}", None)])
    # The next ordinary patch that rewrites the mirror finds no limit to copy.
    assert store.update_key(key.hash, {"include_byok_in_limit": False}) is not None
    row = database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert all(row[column] is None for column in ("limit_micro", "day_limit_micro", "week_limit_micro",
                                                  "month_limit_micro"))


def test_the_replay_writes_api_keys_only_through_its_guard() -> None:
    import ast as _ast

    source = (ROOT / "scripts" / "deploy" / "replay_orphan_writes.py").read_text()
    tree = _ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, _ast.FunctionDef) and n.name == "main")
    text = _ast.unparse(main)
    assert "replay_api_keys(target" in text
    assert "other_rows = [r for r in replay_rows if r[0] != 'api_key']" in text


# --- the reshard tool -----------------------------------------------------


def test_a_strict_key_is_never_resharded() -> None:
    """storage_gcp_key_shard_admin refuses a strict key before writing: its
    shard count stays 1 and its counter rows are untouched, whatever the
    target (a strict key's authorize requires exactly one shard)."""
    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
    from trusted_router.storage_gcp_key_shard_admin import (
        inspect_key_usage_reshard,
        reshard_key_usage,
    )
    from trusted_router.storage_models import CreditAccount, Workspace

    store, database = make_fake_store()
    store._write_entity("workspace", "ws-strict-shards", Workspace(
        id="ws-strict-shards", name="w", owner_user_id="owner", billing_paused=True))
    store._write_entity("credit", "ws-strict-shards", CreditAccount(workspace_id="ws-strict-shards"))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("ws-strict-shards", 0)] = {
        "workspace_id": "ws-strict-shards", "shard": 0, "total_credits": 1_000_000, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(workspace_id="ws-strict-shards", name="strict", creator_user_id=None,
                                      budget_strict=True)
    before = {k: dict(v) for k, v in database.typed[KEY_LIMIT_TABLE].items() if k[0] == key.hash}
    for target in (2, 16):
        status = reshard_key_usage(store, key.hash, target, apply=True)
        assert not status.ready and not status.applied
        assert any("never reshard a strict key" in reason for reason in status.reasons)
        assert not inspect_key_usage_reshard(store, key.hash, target).ready
    assert store.get_key_by_hash(key.hash).usage_shard_count == 1
    after = {k: dict(v) for k, v in database.typed[KEY_LIMIT_TABLE].items() if k[0] == key.hash}
    assert after == before
