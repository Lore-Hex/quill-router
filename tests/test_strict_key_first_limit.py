"""A strict-budget key with no spend limit can never gain one.

The rule (``storage_key_patch.StrictKeyFirstLimit``) makes a strict key's four
limit fields monotonic: never from all unset to any set. The Go fast path edge
relies on it to decide a limitless strict key without a key hold, so this file
pins the rule itself, the property, every route that can set a limit, and an
inventory of every writer of a key's limits, which fails when a new one
appears outside the one guarded patch function.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
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


# --- the writer inventory ---------------------------------------------------
#
# Every place in src/ and scripts/ that can write a key's limits, its
# tr_key_limit mirrors, or its api_key entity, with why it cannot give a
# limitless strict key a first limit. A new writer fails here until it goes
# through storage_key_patch.apply_key_patch or is reviewed and listed.

_MIRROR_COLUMNS = frozenset({"limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro"})
_PATCH = "the one guarded patch function"
_CREATE = "creates a new key, whose limits are its creation's"
_VIA_PATCH = "writes the key apply_key_patch returned, in the same transaction"
_COUNTERS = "writes usage, holds or disabled, never a limit field"
_FEDERATED = (
    "a peer plane copies the home plane's record, where the rule holds; federated keys "
    "never reach the GCP edge (key.federated), and GCP imports none"
)
_REPARTITION = (
    "repartitions an existing limit across shards; a strict key has exactly one shard and "
    "is refused by key_usage_shard_count"
)

ALLOWED_WRITERS: dict[tuple[str, str, str], str] = {
    # Entity limit fields assigned.
    ("assign", "src/trusted_router/storage_key_patch.py", "_apply"): _PATCH,
    ("setattr", "src/trusted_router/storage_key_patch.py", "_apply"): _PATCH,
    ("assign", "src/trusted_router/routes/oauth_keys.py", "register_oauth_key_routes.oauth_authorize_approve.approve"): (
        "sets the consent's limit, from which a new key is created"
    ),
    ("replace", "scripts/fastpath/key_window_vectors.py", "_key"): "builds golden-vector keys in memory",
    # Mirror columns written.
    ("mirror", "src/trusted_router/storage_gcp_counters.py", "<module>"): "defines the mirror's columns",
    ("mirror", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.create"): _CREATE,
    ("mirror", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.txn"): _VIA_PATCH,
    ("mirror", "src/trusted_router/storage_gcp_key_escrow.py", "rebalance_key_limit_headroom.txn"): _REPARTITION,
    ("mirror", "src/trusted_router/storage_gcp_key_shard_admin.py", "<module>"): _REPARTITION,
    ("mirror", "src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"): (
        "mirrors the entity it is given: create, the federated upsert, and update after apply_key_patch"
    ),
    ("mirror", "scripts/expand_billing_shards.py", "<module>"): "refuses strict keys (budget_strict)",
    ("mirror", "scripts/expand_billing_shards.py", "make_plan"): "refuses strict keys (budget_strict)",
    ("mirror", "scripts/stress_credit_shards.py", "_seed"): "seeds new keys for a load test",
    # update_key callers: the store applies apply_key_patch.
    ("update_key", "src/trusted_router/routes/keys.py", "register_key_routes.patch_key"): "answers 400",
    ("update_key", "src/trusted_router/routes/keys.py", "register_key_routes.create_key"): "lenient only",
    ("update_key", "src/trusted_router/routes/console/api_keys.py", "register.console_update_api_key_limit"): (
        "redirects with error=strict-first-limit"
    ),
    ("update_key", "src/trusted_router/routes/console/api_keys.py", "register.console_disable_api_key"): (
        "disabled only"
    ),
    ("update_key", "src/trusted_router/routes/console/api_keys.py", "register.console_enable_api_key"): (
        "disabled only"
    ),
    ("update_key", "src/trusted_router/routes/oauth_authorized_apps.py", "_update_all_or_rollback"): (
        "answers 400 after repairing the grant's keys"
    ),
    ("update_key", "src/trusted_router/routes/oauth_authorized_apps.py", "_repair_all"): (
        "restores values each key had, never a first limit"
    ),
    ("update_key", "src/trusted_router/routes/internal/chat_browser_key.py", "_issue_member_key"): (
        "lowers a lifetime limit its own non-strict, capped keys already have"
    ),
    ("update_key", "src/trusted_router/routes/internal/chat_browser_key.py", "_freeze"): (
        "disables and lowers its own non-strict, capped keys"
    ),
    ("update_key", "src/trusted_router/services/budget_alerts.py", "maybe_send_budget_alerts"): "budget_alerted only",
    ("update_key", "scripts/provision_async_settle_shadow_probe_workspace.py", "_apply_with_credential"): (
        "disabled only"
    ),
    # api_key entity writes.
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.create"): _CREATE,
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.metadata_txn"): _VIA_PATCH,
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.txn"): _VIA_PATCH,
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.reserve_limit.txn"): _COUNTERS,
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.add_usage.txn"): _COUNTERS,
    ("entity", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys._release_limit.txn"): _COUNTERS,
    ("entity", "src/trusted_router/storage_postgres.py", "PostgresStore.create_api_key.create"): _CREATE,
    ("entity", "src/trusted_router/storage_postgres.py", "PostgresStore.upsert_federated_api_key.upsert"): _FEDERATED,
    ("entity", "src/trusted_router/storage_postgres.py", "PostgresStore.update_key.update"): _VIA_PATCH,
    ("entity", "src/trusted_router/storage_account_closure.py", "_finish"): _COUNTERS,
    ("entity", "src/trusted_router/storage_legacy_trust.py", "create_spanner_legacy_authorization.create"): (
        _COUNTERS
    ),
    ("entity", "scripts/stress_credit_shards.py", "_seed"): "seeds new keys for a load test",
    ("entity", "scripts/fastpath/authorize_vectors.py", "build_world"): "builds golden-vector keys in memory",
    ("entity", "scripts/fastpath/authorize_vectors.py", "_run_case"): "builds golden-vector keys in memory",
}

_ENTITY_WRITERS = frozenset(
    {"write_entity", "write_entity_tx", "write_entity_batch", "_write_entity_tx", "_write_entity", "write"}
)


def _qualnames(tree: ast.AST) -> tuple[dict[int, str], dict[int, ast.AST]]:
    """Each node's enclosing qualified name, and its innermost function."""
    names: dict[int, str] = {}
    scopes: dict[int, ast.AST] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                for sub in ast.walk(child):
                    names[id(sub)] = inner
                    if not isinstance(child, ast.ClassDef):
                        scopes[id(sub)] = child
                visit(child, inner)
            else:
                visit(child, prefix)

    visit(tree, "")
    return names, scopes


def _is_limit_attr_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in KEY_LIMIT_FIELDS
    if isinstance(node, ast.JoinedStr):
        text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return text.startswith("limit_") and text.endswith("_microdollars")
    return False


def _names_a_limit_field(scope: ast.AST | None) -> bool:
    """Whether a function spells a limit field anywhere: a setattr there
    whose attribute is a variable may be setting one."""
    return scope is not None and any(_is_limit_attr_name(sub) for sub in ast.walk(scope))


def _writers(path: Path) -> Iterator[tuple[str, str]]:
    tree = ast.parse(path.read_text())
    names, scopes = _qualnames(tree)
    for node in ast.walk(tree):
        where = names.get(id(node), "<module>")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(sub, ast.Attribute) and sub.attr in KEY_LIMIT_FIELDS
                for target in targets for sub in ast.walk(target)
            ):
                yield "assign", where
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "setattr" and len(node.args) >= 2 and (
                _is_limit_attr_name(node.args[1])
                or (not isinstance(node.args[1], ast.Constant) and _names_a_limit_field(scopes.get(id(node))))
            ):
                yield "setattr", where
            if name == "replace" and any(k.arg in KEY_LIMIT_FIELDS for k in node.keywords):
                yield "replace", where
            if name == "update_key":
                yield "update_key", where
            if name in _ENTITY_WRITERS and any(
                isinstance(arg, ast.Constant) and arg.value == "api_key" for arg in node.args
            ):
                yield "entity", where
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text in _MIRROR_COLUMNS or (
                ("UPDATE" in text or "INSERT" in text)
                and any(f"{column} =" in text or f"{column}=" in text for column in _MIRROR_COLUMNS)
            ):
                yield "mirror", where
        elif isinstance(node, ast.Name) and node.id == "KEY_LIMIT_COLUMNS":
            yield "mirror", where


def _scan() -> set[tuple[str, str, str]]:
    found = set()
    for top in ("src/trusted_router", "scripts"):
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            for kind, where in _writers(path):
                found.add((kind, relative, where))
    return found


def test_every_writer_of_a_keys_limits_is_known() -> None:
    found = _scan()
    unknown = sorted(found - ALLOWED_WRITERS.keys())
    assert not unknown, (
        "New writers of an API key's limits, their tr_key_limit mirrors or the api_key entity. "
        "Route limit changes through storage_key_patch.apply_key_patch (StrictKeyFirstLimit), "
        f"then list each here with why it cannot give a limitless strict key a first limit: {unknown}"
    )


def test_the_writer_inventory_has_no_dead_entries() -> None:
    dead = sorted(ALLOWED_WRITERS.keys() - _scan())
    assert not dead, f"Listed writers no longer found; remove them: {dead}"


def test_the_inventory_scanner_finds_what_it_must(tmp_path: Path) -> None:
    """A positive control: each shape the scanner looks for, in a file it has
    never seen, is reported."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def a(key):\n    key.limit_daily_microdollars = 5\n"
        "def b(key, w):\n    setattr(key, f'limit_{w}_microdollars', 5)\n"
        "def b2(key, patch):\n    for f in ('limit_weekly_microdollars',):\n        setattr(key, f, patch[f])\n"
        "def c(key):\n    return replace(key, limit_microdollars=5)\n"
        "def d(store):\n    store.update_key('h', {'limit': 5})\n"
        "def e(io, tx, key):\n    io.write_entity_tx(tx, 'api_key', key.hash, key)\n"
        "def f(conn):\n    conn.execute('UPDATE tr_key_limit SET day_limit_micro = 5')\n"
        "def g(row, name):\n    setattr(row, name, 1)\n"
        "COLUMNS = ('key_hash', 'month_limit_micro')\n"
    )
    assert set(_writers(sample)) == {
        ("assign", "a"), ("setattr", "b"), ("setattr", "b2"), ("replace", "c"), ("update_key", "d"),
        ("entity", "e"), ("mirror", "f"), ("mirror", "<module>"),
    }
