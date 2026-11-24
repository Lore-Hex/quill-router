"""A strict-budget key with no spend limit can never gain one.

The rule (``storage_key_patch.StrictKeyFirstLimit``) makes a strict key's four
limit fields monotonic: never from all unset to any set. The Go fast path edge
relies on it to decide a limitless strict key without a key hold, so this file
pins the rule itself, the property, every route that can set a limit, the
runtime chokepoint that holds it on tr_key_limit's mirror whatever writes the
entity (storage_gcp_key_limit_mirror on Spanner; the Postgres one is in
tests/conformance/test_key_management.py), the reshard refusal and the replay
tool's no-trace rule; tests/test_key_limit_writer_inventory.py inventories every
writer of tr_key_limit.
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


def _body(**fields: Any) -> dict[str, Any]:
    base = {"hash": "kh", "name": "key", "lookup_hash": "lh", "workspace_id": "ws", "budget_strict": False,
            "budget_alert_only": False, "include_byok_in_limit": True, "usage_shard_count": 1,
            **dict.fromkeys(KEY_LIMIT_FIELDS)}
    return {**base, **fields}


@pytest.mark.parametrize(
    ("held", "target", "replayed", "reason"),
    [
        ([], None, _body(budget_strict=True, limit_daily_microdollars=5), None),
        (["tr_key_limit rows"], None, _body(budget_strict=True, limit_daily_microdollars=5),
         "target holds tr_key_limit rows"),
        (["api_key_lookup"], None, _body(), "target holds api_key_lookup"),
        (["api_key", "tr_key_limit rows"], _body(budget_strict=True), _body(budget_strict=True, name="renamed"),
         "target holds api_key, tr_key_limit rows"),
        (["api_key"], _body(budget_strict=True), _body(budget_strict=True, limit_microdollars=5),
         "target holds api_key; would change limit_microdollars"),
        (["api_key"], _body(limit_daily_microdollars=5), _body(budget_alert_only=True),
         "target holds api_key; would change budget_alert_only, limit_daily_microdollars"),
        (["api_key"], _body(), _body(include_byok_in_limit=False, usage_shard_count=4),
         "target holds api_key; would change include_byok_in_limit, usage_shard_count"),
        (["api_key"], "not json", _body(), "target holds api_key; would change " + ", ".join(
            ("budget_alert_only", "budget_strict", "include_byok_in_limit", "limit_daily_microdollars",
             "limit_microdollars", "limit_monthly_microdollars", "limit_weekly_microdollars", "usage_shard_count"))),
        ([], None, None, "unreadable"),
        (["api_key"], _body(), None, "unreadable"),
    ],
)
def test_the_replay_writes_only_a_key_the_target_holds_no_trace_of(
    held: list[str], target: Any, replayed: dict[str, Any] | None, reason: str | None,
) -> None:
    target_body = json.dumps(target) if isinstance(target, dict) else target
    assert _replay_module().api_key_replay_refusal(held, target_body, replayed) == reason


@pytest.mark.parametrize(
    ("body", "index_rows", "readable"),
    [
        (_body(), [("api_key_lookup", "lh"), ("api_key_by_workspace", "ws#kh")], True),
        (_body(), [], True),
        (_body(hash="other"), [], False),
        (_body(lookup_hash=""), [], False),
        (_body(workspace_id=None), [], False),
        (_body(), [("api_key_lookup", "another-lookup")], False),
        (_body(), [("api_key_by_workspace", "other-ws#kh")], False),
        ("[]", [], False),
        ("not json", [], False),
    ],
)
def test_the_replay_checks_only_a_body_it_can_read(body: Any, index_rows: list[tuple[str, str]],
                                                   readable: bool) -> None:
    replay = _replay_module()
    raw = json.dumps(body) if isinstance(body, dict) else body
    records = replay.KeyRecords("kh", ("api_key", "kh", raw, None),
                                tuple((kind, entity_id, json.dumps({"key_id": "kh"}), None)
                                      for kind, entity_id in index_rows))
    assert (replay._replayed_key(records) is not None) == readable


def test_index_records_travel_with_their_key_or_not_at_all() -> None:
    replay = _replay_module()
    key = ("api_key", "kh", json.dumps(_body()), None)
    lookup = ("api_key_lookup", "lh", json.dumps({"key_id": "kh"}), None)
    stray = ("api_key_lookup", "lh-2", json.dumps({"key_id": "kh-gone"}), None)
    broken = ("api_key_by_workspace", "ws#x", "not json", None)
    workspace = ("workspace", "ws", "{}", None)
    keys, others, refused = replay.group_key_records([key, lookup, stray, broken, workspace])
    assert keys == [replay.KeyRecords("kh", key, (lookup,))]
    assert others == [workspace]
    assert refused == [("api_key_lookup", "lh-2", "index_without_key"),
                       ("api_key_by_workspace", "ws#x", "unreadable")]


def _entity(database: Any, kind: str, entity_id: str) -> str | None:
    rows = database.run_in_transaction(lambda txn: list(txn.execute_sql(
        "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id", params={"kind": kind, "id": entity_id})))
    return rows[0][0] if rows else None


def _key_rows(database: Any, key_hash: str) -> dict[int, dict[str, Any]]:
    from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE

    return {pk[1]: dict(row) for pk, row in database.typed.get(KEY_LIMIT_TABLE, {}).items() if pk[0] == key_hash}


_MIRROR = ("limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro")


def test_the_replay_cannot_resurrect_a_deleted_strict_key() -> None:
    """The reviewer's case on the Spanner store fake: a strict key with a
    daily limit has the limit removed, is disabled and deleted; deletion
    keeps its tr_key_limit rows. Replaying its older body with its lookup and
    workspace records is refused (the target still holds its rows), so its
    credential does not come back. Even with the body forced back by a raw
    write, the next ordinary patch that rewrites the mirror is refused by the
    chokepoint and the mirror stays limitless."""
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    raw, key = store.api_keys.create(workspace_id="ws-replay", name="strict", creator_user_id=None,
                                     budget_strict=True, limit_daily_microdollars=5_000_000)
    older = json.dumps(dataclasses.asdict(key))
    lookup = _entity(database, "api_key_lookup", key.lookup_hash)
    by_workspace = _entity(database, "api_key_by_workspace", f"ws-replay#{key.hash}")
    assert lookup is not None and by_workspace is not None
    assert store.update_key(key.hash, {"limit_daily_microdollars": None}) is not None
    assert store.update_key(key.hash, {"disabled": True}) is not None
    assert store.delete_key(key.hash)
    assert all(row[c] is None for row in _key_rows(database, key.hash).values() for c in _MIRROR)

    replay = _replay_module()
    refused = replay.replay_api_keys(database, [
        ("api_key", key.hash, older, None),
        ("api_key_lookup", key.lookup_hash, lookup, None),
        ("api_key_by_workspace", f"ws-replay#{key.hash}", by_workspace, None),
    ])
    assert refused == [(key.hash, "target holds tr_key_limit rows")]
    assert store.get_key_by_raw(raw) is None
    assert _entity(database, "api_key", key.hash) is None
    assert _entity(database, "api_key_lookup", key.lookup_hash) is None

    # An unguarded writer forces the entity, limit and all, back.
    with database.batch() as batch:
        batch.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                               values=[("api_key", key.hash, older, None)])
    with pytest.raises(StrictKeyFirstLimit):
        store.update_key(key.hash, {"include_byok_in_limit": False})
    assert all(row[c] is None for row in _key_rows(database, key.hash).values() for c in _MIRROR)


def test_the_replay_never_changes_a_held_keys_alert_policy() -> None:
    """The reviewer's second case: a replayed body that turns a held key's
    window limits into alerts is refused and reported, and the key keeps
    enforcing."""
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    _raw, key = store.api_keys.create(workspace_id="ws-replay", name="strict", creator_user_id=None,
                                      budget_strict=True, limit_daily_microdollars=5_000_000)
    changed = json.dumps({**dataclasses.asdict(key), "budget_alert_only": True})
    refused = _replay_module().replay_api_keys(database, [("api_key", key.hash, changed, None)])
    assert refused == [(key.hash, "target holds api_key, api_key_lookup, api_key_by_workspace, tr_key_limit rows; "
                                  "would change budget_alert_only")]
    assert not store.get_key_by_hash(key.hash).budget_alert_only


def test_the_replay_writes_a_key_new_to_the_target_with_its_index_records() -> None:
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    body = _body(hash="kh-new", lookup_hash="lh-new", workspace_id="ws-new", limit_monthly_microdollars=9)
    rows = [
        ("api_key", "kh-new", json.dumps(body), None),
        ("api_key_lookup", "lh-new", json.dumps({"key_id": "kh-new"}), None),
        ("api_key_by_workspace", "ws-new#kh-new", json.dumps({"key_id": "kh-new"}), None),
        ("api_key_lookup", "lh-stray", json.dumps({"key_id": "kh-absent"}), None),
    ]
    replay = _replay_module()
    assert replay.replay_api_keys(database, rows) == [("lh-stray", "api_key_lookup: index_without_key")]
    assert json.loads(_entity(database, "api_key", "kh-new") or "{}")["limit_monthly_microdollars"] == 9
    assert _entity(database, "api_key_lookup", "lh-new") is not None
    assert _entity(database, "api_key_by_workspace", "ws-new#kh-new") is not None
    assert _entity(database, "api_key_lookup", "lh-stray") is None
    with pytest.raises(ValueError, match="api_key and key index records only"):
        replay.replay_api_keys(database, [("workspace", "ws-replay", "{}", None)])


def test_the_replay_names_what_decides_which_limits_are_enforced() -> None:
    """KEY_CONFIG_FIELDS is derived from the code that enforces limits, not
    listed by hand: it holds budget_alert_only, and every key field the
    gateway's typed authorize reads to choose how a key's limits apply."""
    from trusted_router.storage_key_patch import key_enforcement_fields

    fields = _replay_module().KEY_CONFIG_FIELDS
    assert fields == key_enforcement_fields()
    assert {"budget_alert_only", "budget_strict", "include_byok_in_limit", "usage_shard_count",
            *KEY_LIMIT_FIELDS} <= set(fields)
    source = (ROOT / "src/trusted_router/routes/internal/gateway.py").read_text()
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "authorize_gateway_typed"]
    assert calls
    limit_keywords = {"strict_budget", "strict_budget_alert_only", "skip_key_limit", "speculate_key_limit"}
    read = {
        sub.attr for call in calls for keyword in call.keywords if keyword.arg in limit_keywords
        for sub in ast.walk(keyword.value)
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "api_key"
    }
    assert {"budget_strict", "budget_alert_only"} <= read
    assert read <= set(fields), read - set(fields)


def test_the_replay_writes_api_keys_only_through_its_guard() -> None:
    source = (ROOT / "scripts" / "deploy" / "replay_orphan_writes.py").read_text()
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    text = ast.unparse(main)
    assert "keys, other_rows, refused_indexes = group_key_records(replay_rows)" in text
    assert "replay_api_keys(target" in text
    batch_writes = [n for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "insert_or_update"]
    assert len(batch_writes) == 1 and "values=batch" in ast.unparse(batch_writes[0])
    assert "batch = other_rows[i:i + BATCH]" in text


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


def test_the_reshard_transaction_refuses_a_strict_key_past_its_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the preflight forced to pass, the reshard transaction's own guard
    still refuses a strict key: nothing is written and its count stays 1."""
    from tests.fakes.spanner import make_fake_store
    from trusted_router import storage_gcp_key_shard_admin as admin
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
    from trusted_router.storage_models import CreditAccount, Workspace

    store, database = make_fake_store()
    store._write_entity("workspace", "ws-forced", Workspace(
        id="ws-forced", name="w", owner_user_id="owner", billing_paused=True))
    store._write_entity("credit", "ws-forced", CreditAccount(workspace_id="ws-forced"))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("ws-forced", 0)] = {
        "workspace_id": "ws-forced", "shard": 0, "total_credits": 1_000_000, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(workspace_id="ws-forced", name="strict", creator_user_id=None,
                                      budget_strict=True, limit_daily_microdollars=1_000_000)
    before = _key_rows(database, key.hash)
    real_inspect = admin.inspect_key_usage_reshard
    calls: list[int] = []

    def forced(store_: Any, key_hash: str, target: int, **kwargs: Any) -> Any:
        calls.append(target)
        if len(calls) == 1:
            return admin.KeyUsageReshardResult(key_hash=key_hash, workspace_id="ws-forced",
                                               target_shard_count=target, current_shard_count=1)
        return real_inspect(store_, key_hash, target, **kwargs)

    monkeypatch.setattr(admin, "inspect_key_usage_reshard", forced)
    status = admin.reshard_key_usage(store, key.hash, 4, apply=True)
    assert not status.applied
    assert any("preconditions changed" in reason for reason in status.reasons)
    assert json.loads(_entity(database, "api_key", key.hash) or "{}")["usage_shard_count"] == 1
    assert _key_rows(database, key.hash) == before


# --- the runtime chokepoint ------------------------------------------------


def _create(store: Any, database: Any, **fields: Any) -> Any:
    _raw, key = store.api_keys.create(workspace_id="ws-mirror", name="k", creator_user_id=None, **fields)
    return key


def _write(database: Any, key_hash: str, limit: int | None, *, mutation: str = "insert_or_update") -> None:
    from trusted_router.storage_gcp_key_limit_mirror import write_key_limit_rows

    def txn(transaction: Any) -> None:
        write_key_limit_rows(transaction, database_param_types(), key_hash=key_hash,
                             columns=("key_hash", "shard", "day_limit_micro"), values=[(key_hash, 0, limit)],
                             mutation=mutation)

    database.run_in_transaction(txn)


def database_param_types() -> Any:
    from tests.fakes.spanner import _ParamTypes

    return _ParamTypes()


@pytest.mark.parametrize("mutation", ["insert_or_update", "update"])
def test_the_spanner_chokepoint_refuses_a_strict_keys_first_mirrored_limit(mutation: str) -> None:
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    key = _create(store, database, budget_strict=True)
    with pytest.raises(StrictKeyFirstLimit):
        _write(database, key.hash, 5, mutation=mutation)
    assert all(row[c] is None for row in _key_rows(database, key.hash).values() for c in _MIRROR)
    _write(database, key.hash, None, mutation=mutation)  # writing no limit is fine


def test_the_spanner_chokepoint_refuses_a_limit_on_a_deleted_keys_rows() -> None:
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    key = _create(store, database)
    assert store.delete_key(key.hash)
    assert _key_rows(database, key.hash)
    with pytest.raises(StrictKeyFirstLimit):
        _write(database, key.hash, 5)


@pytest.mark.parametrize("body", ["not json", json.dumps(["a list"])])
def test_the_spanner_chokepoint_refuses_when_the_key_does_not_read(body: str) -> None:
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    key = _create(store, database)
    with database.batch() as batch:
        batch.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                               values=[("api_key", key.hash, body, None)])
    with pytest.raises(StrictKeyFirstLimit):
        _write(database, key.hash, 5)


def test_the_spanner_chokepoint_lets_every_other_key_change_its_limits() -> None:
    """A key that is not strict (or whose body predates budget_strict) gains
    a first limit; a strict key with a limit changes or removes it."""
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    plain = _create(store, database)
    _write(database, plain.hash, 5)
    assert _key_rows(database, plain.hash)[0]["day_limit_micro"] == 5
    legacy = _create(store, database)
    body = {k: v for k, v in dataclasses.asdict(legacy).items() if k != "budget_strict"}
    with database.batch() as batch:
        batch.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                               values=[("api_key", legacy.hash, json.dumps(body), None)])
    _write(database, legacy.hash, 7)
    limited = _create(store, database, budget_strict=True, limit_daily_microdollars=9)
    _write(database, limited.hash, 3)
    _write(database, limited.hash, None)
    assert _key_rows(database, limited.hash)[0]["day_limit_micro"] is None
    with pytest.raises(StrictKeyFirstLimit):
        _write(database, limited.hash, 3)


def test_a_batch_only_inserts_new_rows_through_the_chokepoint() -> None:
    from google.api_core.exceptions import AlreadyExists

    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_gcp_key_limit_mirror import write_key_limit_rows

    store, database = make_fake_store()
    key = _create(store, database, budget_strict=True)
    pt = database_param_types()
    with pytest.raises(ValueError, match="only inserts"), database.batch() as batch:
        write_key_limit_rows(batch, pt, key_hash=key.hash, columns=("key_hash", "shard", "limit_micro"),
                             values=[(key.hash, 0, 5)], mutation="insert_or_update")
    with pytest.raises(ValueError, match="shard 0 among them"), database.batch() as batch:
        write_key_limit_rows(batch, pt, key_hash=key.hash, columns=("key_hash", "shard", "limit_micro"),
                             values=[(key.hash, 1, 5)], mutation="insert")
    with pytest.raises(AlreadyExists), database.batch() as batch:
        write_key_limit_rows(batch, pt, key_hash=key.hash, columns=("key_hash", "shard", "limit_micro"),
                             values=[(key.hash, 0, 5)], mutation="insert")
    assert all(row[c] is None for row in _key_rows(database, key.hash).values() for c in _MIRROR)
    with pytest.raises(StrictKeyFirstLimit):
        _write(database, key.hash, 5, mutation="insert")


def test_a_transaction_insert_of_a_new_shard_is_checked_too() -> None:
    """A new shard row with a limit would give a limitless strict key's
    mirror its first limit as surely as an update of shard 0."""
    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_gcp_key_limit_mirror import write_key_limit_rows

    store, database = make_fake_store()
    key = _create(store, database, budget_strict=True)

    def insert_shard(transaction: Any) -> None:
        write_key_limit_rows(transaction, database_param_types(), key_hash=key.hash,
                             columns=("key_hash", "shard", "month_limit_micro"), values=[(key.hash, 1, 5)],
                             mutation="insert")

    with pytest.raises(StrictKeyFirstLimit):
        database.run_in_transaction(insert_shard)
    assert set(_key_rows(database, key.hash)) == {0}


@pytest.mark.parametrize(
    ("columns", "values", "mutation", "message"),
    [
        (("shard", "key_hash", "limit_micro"), [(0, "kh", 1)], "update", "lead with key_hash"),
        (("key_hash", "shard", "limit_micro"), [("kh", 0)], "update", "lead with key_hash"),
        (("key_hash", "shard", "limit_micro"), [("other", 0, 1)], "update", "one key's rows"),
        (("key_hash", "shard", "limit_micro"), [("kh", 0, 1)], "replace", "unknown tr_key_limit mutation"),
    ],
)
def test_the_chokepoint_validates_its_rows(columns: tuple[str, ...], values: list[tuple[Any, ...]],
                                           mutation: str, message: str) -> None:
    from trusted_router.storage_gcp_key_limit_mirror import write_key_limit_rows

    with pytest.raises(ValueError, match=message):
        write_key_limit_rows(object(), database_param_types(), key_hash="kh", columns=columns, values=values,
                             mutation=mutation)


# --- the documented limit-column exceptions --------------------------------


def test_shard_expansion_refuses_a_strict_key() -> None:
    """scripts/expand_billing_shards.py writes window caps into new shard
    rows; it never reaches a strict key."""
    from scripts.expand_billing_shards import make_plan

    workspace = {"id": "ws"}
    key = {"workspace_id": "ws", "budget_strict": True, "limit_daily_microdollars": 1}
    with pytest.raises(ValueError, match="strict keys require"):
        make_plan(workspace, {"workspace_id": "ws"}, key, [], [], {}, {}, 2, allow_window_caps=True)


def test_the_leased_usage_backfill_assigns_no_limit_column() -> None:
    import datetime as dt

    from scripts.backfill_leased_key_usage import update_sql
    from tests.key_limit_writers import LIMIT_COLUMNS, assigns_limit_column

    sql = update_sql({"key_hash": "kh_1", "usage": 3, "day": 1, "week": 2, "month": 3},
                     now=dt.datetime(2026, 11, 23, tzinfo=dt.UTC))
    assert sql.startswith("UPDATE tr_key_limit SET ")
    assert not assigns_limit_column(sql)
    assert not any(f"{column} =" in sql for column in LIMIT_COLUMNS)
    assert assigns_limit_column(sql.replace("usage = usage + 3", "limit_micro = 1"))


def test_the_key_release_assigns_no_limit_column() -> None:
    """storage_gcp_counter_dml.release_key_statement builds its window SQL
    with str.replace in a loop, which the inventory's scanner cannot
    resolve, so it fails closed there. Every statement it returns, for both
    usage columns and each window mode, is one the strict grammar clears,
    assigning reserved, usage or byok_usage and window usage only."""
    import datetime as dt

    from tests.fakes.spanner import _ParamTypes
    from tests.key_limit_writers import LIMIT_COLUMNS, written_columns
    from trusted_router.spend_windows import window_floors
    from trusted_router.storage_gcp_counter_dml import release_key_statement

    floors = window_floors(dt.datetime(2026, 11, 24, tzinfo=dt.UTC))
    for book_to_byok in (False, True):
        for windows in ("current", "stale", "any"):
            sql, _params, _types = release_key_statement(
                _ParamTypes(), "kh", 1, 1, book_to_byok=book_to_byok, window_floors=floors, windows=windows)
            columns = written_columns(sql)
            assert columns is not None, (book_to_byok, windows)
            assert {"reserved", "byok_usage" if book_to_byok else "usage", "day_usage"} <= columns
            assert not columns & LIMIT_COLUMNS, (book_to_byok, windows, columns)


def test_the_key_release_check_sees_past_a_carriage_return() -> None:
    """Review round 7: a limit assignment after a line comment ended by a
    bare carriage return (PostgreSQL ends it there) is one the release
    check above reads, so it would fail."""
    import datetime as dt
    import inspect

    from tests.fakes.spanner import _ParamTypes
    from tests.key_limit_writers import written_columns
    from trusted_router import storage_gcp_counter_dml as counter
    from trusted_router.spend_windows import window_floors

    source = inspect.getsource(counter.release_key_statement)
    needle = '+ " WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"'
    assert source.count(needle) == 2
    scope = dict(vars(counter))
    probe = source.replace(needle, '+ " -- note\\r, day_limit_micro=1 WHERE key_hash=@kh AND shard=@shard AND '
                                   'reserved >= @hold"')
    exec(compile(probe, counter.__file__, "exec"), scope)  # noqa: S102 - the real function, rebuilt with the probe
    floors = window_floors(dt.datetime(2026, 11, 24, tzinfo=dt.UTC))
    sql, _params, _types = scope["release_key_statement"](_ParamTypes(), "kh", 1, 1, book_to_byok=False,
                                                         window_floors=floors, windows="current")
    assert "\r" in sql
    columns = written_columns(sql)
    assert columns is None or "day_limit_micro" in columns

