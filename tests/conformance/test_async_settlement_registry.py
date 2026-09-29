"""Native GoogleSQL execution of the registry's durable transitions."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import pytest

from trusted_router.config import Settings
from trusted_router.settlement_journal import Envelope, Grant, Journal, Receipt, Sizing
from trusted_router.settlement_journal_memory import InMemoryJournalStorage
from trusted_router.storage_gcp_async_settlement import SpannerEpochRegistry, SpannerReceiptVerifier

pytestmark = pytest.mark.xdist_group('conformance-spanner-emulator')


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_durable_registry_transitions(native_emulator_resources: Any, backend: str) -> None:
    from google.cloud.spanner_v1 import param_types
    db = native_emulator_resources[0]
    storage = InMemoryJournalStorage()
    registry = SpannerEpochRegistry(db, param_types, journal_storage=storage)
    verifier = SpannerReceiptVerifier(registry)
    j = Journal(storage, registry, verifier)
    settings = Settings(async_settlement_journal_minimum_shard_cap_micros=1)
    grant = Grant(uuid4().hex, 'us-central1', 'first', 5_000_000, 1, 64)
    registry.register(grant, settings, tier=1)
    j.run(j.initialize(grant, 0))
    t = registry.allocate(grant, authorization=uuid4().hex, generation='g', key_id='k',
                          nonce='n', snapshot_hash='a'*64, idempotency_until=100)
    registry.bind(t)
    assert registry.registered(t) and registry.tickets(grant) == (t,)
    assert grant in registry.grants()
    j.run(j.create(t))
    j.run(j.accept(t, Envelope('settle', 'endpoint', 99)))
    registry.reconcile(t)
    state = json.loads(storage.rows[grant.row_key(0)][t.column])
    proof = Receipt(t.authorization, grant.epoch, state['hash'], 'ledger-finalization')
    assert not verifier.verify(t, proof)
    def finalize(tx: Any) -> None:
        row = registry._obligations(tx, aid=t.authorization)[0]
        row['ledger_receipt'] = json.dumps(dict(kind='ledger_finalization', ticket=asdict(t),
            receipt=asdict(proof), amount=99), sort_keys=True, separators=(',', ':'))
        registry._write(tx, 'tr_async_settlement_obligation', row)
    db.run_in_transaction(finalize)
    assert verifier.verify(t, proof)
    j.run(j.acknowledge(t, proof))
    registry.reconcile(t)
    registry.retire(grant)
    assert registry.is_retired(grant)
    j.run(j.seal_shard(grant, 0))
    j.run(j.bound_epoch(grant))
    next_grant = registry.successor(grant, 'second', settings, tier=1, sizing=Sizing(1, 64))
    assert registry.successor(grant, 'second', settings, tier=1, sizing=Sizing(1, 64)) == next_grant
    registry.close(grant, retain_until=100)
    assert SpannerEpochRegistry(db, param_types).retention_deadline(grant) == 100
