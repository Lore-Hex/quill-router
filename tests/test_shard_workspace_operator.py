from __future__ import annotations

import argparse

import pytest

from scripts import shard_workspace
from trusted_router.storage import InMemoryStore


def _args(*, workspace: str | None = None, owner_email: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(workspace=workspace, owner_email=owner_email)


def test_resolve_workspace_uses_explicit_id_without_lookup() -> None:
    assert shard_workspace._resolve_workspace(object(), _args(workspace="ws-exact")) == "ws-exact"


def test_resolve_workspace_accepts_owner_with_one_workspace() -> None:
    store = InMemoryStore()
    user = store.ensure_user("owner@example.com", email="owner@example.com")
    expected = store.list_workspaces_for_user(user.id)[0].id

    assert (
        shard_workspace._resolve_workspace(store, _args(owner_email="owner@example.com"))
        == expected
    )


def test_resolve_workspace_refuses_unknown_or_ambiguous_owner() -> None:
    store = InMemoryStore()
    with pytest.raises(ValueError, match="does not match"):
        shard_workspace._resolve_workspace(store, _args(owner_email="missing@example.com"))

    user = store.ensure_user("owner@example.com", email="owner@example.com")
    store.create_workspace(user.id, "Second workspace")
    with pytest.raises(ValueError, match="select one with --workspace"):
        shard_workspace._resolve_workspace(store, _args(owner_email="owner@example.com"))


def test_resolve_workspace_ignores_memberships_the_user_does_not_own() -> None:
    store = InMemoryStore()
    owner = store.ensure_user("owner@example.com", email="owner@example.com")
    member = store.ensure_user("member@example.com", email="member@example.com")
    owned = store.list_workspaces_for_user(member.id)[0]
    foreign = store.list_workspaces_for_user(owner.id)[0]
    store.add_members(foreign.id, ["member@example.com"])

    assert (
        shard_workspace._resolve_workspace(store, _args(owner_email="member@example.com"))
        == owned.id
    )


def test_parser_requires_exactly_one_target() -> None:
    parser = shard_workspace._parser()
    parsed = parser.parse_args(
        ["status", "--owner-email", "owner@example.com", "--shards", "16"]
    )
    assert parsed.owner_email == "owner@example.com"
    assert parsed.workspace is None

    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--shards", "16"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "status",
                "--workspace",
                "ws",
                "--owner-email",
                "owner@example.com",
                "--shards",
                "16",
            ]
        )
