"""A workspace's pause causes across the Store contract.

``update_workspace(billing_paused=...)`` owns the legacy "migration" cause and
records it on the workspace document only. The operator abuse latch and its
clear own "abuse". Neither may drop the other's cause: an abuse clear that
dropped "migration" would let a quiesced workspace take new work.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("migration_first", [True, False], ids=["migration-first", "abuse-first"])
def test_abuse_latch_and_clear_keep_a_migration_pause(
    store, unique, workspace_id, migration_first
):
    if migration_first:
        store.update_workspace(workspace_id, billing_paused=True)
    assert store.record_workspace_abuse_and_demote(
        workspace_id,
        abuse_ref=f"latch-{unique}",
        operator_identity="ops@example.com",
        reason="confirmed abuse",
    )
    if not migration_first:
        store.update_workspace(workspace_id, billing_paused=True)
    latched = store.get_workspace(workspace_id)
    assert latched is not None
    assert latched.billing_pause_causes == ["abuse", "migration"]

    assert store.clear_workspace_abuse_pause(
        workspace_id,
        abuse_ref=f"clear-{unique}",
        operator_identity="ops@example.com",
        reason="appeal accepted",
    )
    cleared = store.get_workspace(workspace_id)
    assert cleared is not None
    assert cleared.billing_pause_causes == ["migration"]
    assert cleared.billing_paused is True

    store.update_workspace(workspace_id, billing_paused=False)
    resumed = store.get_workspace(workspace_id)
    assert resumed is not None
    assert resumed.billing_pause_causes == []
    assert resumed.billing_paused is False
