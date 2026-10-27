"""Atomic access closure, not personal-data erasure.

Retain identity tombstones and accounting records. Archive solo workspaces using
the existing lifecycle operation so keys, owner inventory and trust latches agree.
Shared accounts require support; never disable another member's workspace.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

from trusted_router.storage_models import ApiKey, CreditAccount, Member, User, Workspace, iso_now

MAX_WORKSPACES = 16
MAX_KEYS = 128


class AccountClosureBlocked(ValueError):
    """The account needs assisted closure without partial mutations."""


def _validate(
    user: User, owned: list[str], memberships: list[Member],
    read: Callable[[str, str, Any], Any],
    members: Callable[[str], list[Member]],
    keys: Callable[[str], list[ApiKey]],
) -> list[tuple[Workspace, CreditAccount, list[ApiKey]]]:
    if len(owned) > MAX_WORKSPACES or len(memberships) > MAX_WORKSPACES:
        raise AccountClosureBlocked("assisted")
    for membership in memberships:
        workspace = read("workspace", membership.workspace_id, Workspace)
        if workspace and not workspace.deleted and workspace.owner_user_id != user.id:
            raise AccountClosureBlocked("shared")
    if len(owned) != user.owner_workspace_count:
        raise AccountClosureBlocked("assisted")
    plan = []
    key_count = 0
    for workspace_id in owned:
        workspace = read("workspace", workspace_id, Workspace)
        credit = read("credit", workspace_id, CreditAccount)
        if not workspace or workspace.deleted or workspace.owner_user_id != user.id or not credit:
            raise AccountClosureBlocked("assisted")
        if any(member.user_id != user.id for member in members(workspace_id)):
            raise AccountClosureBlocked("shared")
        workspace_keys = keys(workspace_id)
        key_count += len(workspace_keys)
        if key_count > MAX_KEYS or any(key.workspace_id != workspace_id for key in workspace_keys):
            raise AccountClosureBlocked("assisted")
        plan.append((workspace, credit, workspace_keys))
    return plan


def _finish(
    user: User, plan: list[tuple[Workspace, CreditAccount, list[ApiKey]]],
    write: Callable[[str, str, Any], None], close_workspace: Callable[[str], Any],
) -> User:
    for workspace, credit, keys in plan:
        close_workspace(workspace.id)
        credit.auto_refill_enabled = False
        write("credit", workspace.id, credit)
        for key in keys:
            key.disabled = True
            key.updated_at = iso_now()
            write("api_key", key.hash, key)
    user.disabled = True
    user.account_closed_at = iso_now()
    user.owner_workspace_count = 0
    write("user", user.id, user)
    return user


def close_memory(store: Any, user_id: str) -> User | None:
    with store._lock:
        user = store.users.get(user_id)
        if user is None or user.account_closed_at:
            return user
        owned = sorted(wid for owner, wid in store.owner_workspaces if owner == user_id)
        tables = {"workspace": store.workspaces, "credit": store.credits}
        plan = _validate(
            user, owned, [m for m in store.members.values() if m.user_id == user_id],
            lambda kind, key, cls: copy.deepcopy(tables[kind].get(key)),
            lambda wid: store.list_members(wid), lambda wid: copy.deepcopy(store.list_keys(wid)),
        )
        def write(kind: str, key: str, value: Any) -> None:
            {"user": store.users, "credit": store.credits, "api_key": store.api_keys.keys}[kind][key] = value
        return _finish(copy.deepcopy(user), plan, write, lambda wid: store.update_workspace(wid, deleted=True))


def close_spanner(store: Any, user_id: str) -> User | None:
    def txn(transaction: Any) -> User | None:
        def read(kind: str, key: str, cls: Any) -> Any:
            return store._read_entity_tx(transaction, kind, key, cls)
        user = read("user", user_id, User)
        if user is None or user.account_closed_at:
            return user
        def rows(kind: str, match: str, *, suffix: bool = False, limit: int) -> list[dict[str, Any]]:
            parameter = "suffix" if suffix else "prefix"
            predicate = "ENDS_WITH(id, @suffix)" if suffix else "STARTS_WITH(id, @prefix)"
            result = transaction.execute_sql(
                f"SELECT body FROM tr_entities WHERE kind=@kind AND {predicate} ORDER BY id LIMIT @limit",  # noqa: S608
                params={"kind": kind, parameter: match, "limit": limit},
                param_types={"kind": store._param_types.STRING, parameter: store._param_types.STRING,
                             "limit": store._param_types.INT64},
            )
            return [json.loads(row[0]) for row in result]
        def keys(wid: str) -> list[ApiKey]:
            refs = rows("api_key_by_workspace", f"{wid}#", limit=MAX_KEYS + 1)
            return [key for ref in refs if (key := read("api_key", str(ref["key_id"]), ApiKey))]
        plan = _validate(
            user, store._owner_workspace_ids_tx(transaction, user_id),
            [Member(**row) for row in rows("member", f"#{user_id}", suffix=True, limit=MAX_WORKSPACES + 1)],
            read, lambda wid: [Member(**row) for row in rows("member", f"{wid}#", limit=2)], keys,
        )
        return _finish(
            user, plan, lambda kind, key, value: store._write_entity_tx(transaction, kind, key, value),
            lambda wid: store._update_workspace_tx(transaction, wid, deleted=True),
        )
    return store._run_in_transaction(txn)


def close_postgres(store: Any, user_id: str) -> User | None:
    _like_prefix = store._like_prefix

    def txn(conn: Any) -> User | None:
        def read(kind: str, key: str, cls: Any) -> Any:
            return store._read_entity_tx(conn, kind, key, cls, for_update=True)
        user = read("user", user_id, User)
        if user is None or user.account_closed_at:
            return user
        def rows(kind: str, pattern: str, limit: int) -> list[dict[str, Any]]:
            result = conn.execute(
                "SELECT body FROM tr_entities WHERE kind = %s AND id LIKE %s ESCAPE '\\' "
                "ORDER BY id LIMIT %s FOR UPDATE", (kind, pattern, limit),
            ).fetchall()
            return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in result]
        def keys(wid: str) -> list[ApiKey]:
            refs = rows("api_key_by_workspace", _like_prefix(f"{wid}#"), MAX_KEYS + 1)
            return [key for ref in refs if (key := read("api_key", str(ref["key_id"]), ApiKey))]
        owned = [str(row[0]) for row in conn.execute(
            "SELECT workspace_id FROM tr_owner_workspace WHERE owner_user_id = %s ORDER BY workspace_id",
            (user_id,),
        ).fetchall()]
        plan = _validate(
            user, owned,
            [Member(**row) for row in rows("member", "%" + _like_prefix(f"#{user_id}")[:-1], MAX_WORKSPACES + 1)],
            read, lambda wid: [Member(**row) for row in rows("member", _like_prefix(f"{wid}#"), 2)], keys,
        )
        return _finish(
            user, plan, lambda kind, key, value: store._write_entity_tx(conn, kind, key, value),
            lambda wid: store._update_workspace_tx(conn, wid, deleted=True),
        )
    return store._run_transaction(txn)
