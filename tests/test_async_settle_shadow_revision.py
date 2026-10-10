from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_async_settle_shadow import NOW, context, signer, wire
from tests.test_async_settle_shadow_accounting import sample_row
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import SAMPLE, validate_sample
from trusted_router.services.async_settle_shadow import Capture, Runtime


def test_short_display_release_persists_full_source_revision(
    monkeypatch: pytest.MonkeyPatch, shadow_deadline_clock: Any,
) -> None:
    from tests.test_async_settle_shadow_accounting import Database
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

    revision = "c07628b" + "0" * 33
    cfg = settings(
        async_settle_enabled=False, release=revision[:7], source_revision=revision,
        async_settle_shadow_workspaces="ws-v1",
    )
    database = Database()
    store = EvidenceStore(database)
    monkeypatch.setattr(store, "booking", lambda *args: Booking(2, "settled", True))
    worker = Runtime(cfg, runtime(), store)
    worker.signer = signer()
    ctx = context()
    capture = Capture(worker, ctx.body, "settle", NOW, shadow_deadline_clock.monotonic(), ctx.authorization)
    try:
        worker.process(capture, wire(), {"data": {"settled": True}}, 1, ("openai", "chat.completions", False))
        assert worker.counters.revision == revision
        rows = [body for (kind, _), body in database.rows.items() if kind == SAMPLE]
        assert len(rows) == 1
        assert json.loads(rows[0])["deployment"]["router_revision"] == revision
        assert worker.counters.snapshot()[0][1]["comparison_dropped"] == 0
    finally:
        worker.executor.shutdown()


@pytest.mark.parametrize("value", ["c07628b", "not-a-commit", "G" * 40, "a" * 41])
def test_source_revision_does_not_accept_inexact_identity(value: str) -> None:
    with pytest.raises(ValidationError):
        settings(source_revision=value)


def test_sample_validator_still_rejects_short_commit() -> None:
    row = sample_row()
    row["deployment"]["router_revision"] = "c07628b"
    with pytest.raises(ValueError, match="deployment revision"):
        validate_sample(row, row["authorization_day"] + "/" + row["authorization_id"])


def test_full_release_remains_usable_for_local_fixtures() -> None:
    worker = Runtime(settings(release="a" * 40), runtime())
    try:
        assert worker.counters.revision == "a" * 40
    finally:
        worker.executor.shutdown()
