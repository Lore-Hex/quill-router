"""Wire fake: evaluates protobuf filters/mutations, not a pre-canned CAS result."""
from __future__ import annotations

import copy
import os
import re
import struct
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from google.cloud.bigtable_admin_v2.types import AppProfile
from google.cloud.bigtable_v2.types import (
    ReadModifyWriteRowRequest,
    ReadModifyWriteRowResponse,
    ReadModifyWriteRule,
    ReadRowsResponse,
    Row,
)

from trusted_router.settlement_journal import Compare, Grant, JournalError, Read
from trusted_router.settlement_journal_bigtable import BigtableJournalStorage, validate_profile

TABLE = 'projects/test/instances/test/tables/journal'
PROFILE = 'projects/test/instances/test/appProfiles/regional'


def profile() -> AppProfile:
    return AppProfile(name=PROFILE, single_cluster_routing={
        'cluster_id': 'cluster', 'allow_transactional_writes': True,
    })


class FakeDataClient:
    """Atomic per-row calls, arbitrary history (GC need not have happened)."""
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[bytes, list[bytes]]] = {}
        self.lock = threading.Lock()
        self.calls: list[tuple[str, Any]] = []

    @staticmethod
    def filtered(row: dict[bytes, list[bytes]], filter_: Any) -> dict[bytes, list[bytes]]:
        cells = copy.deepcopy(row)
        kind = filter_._pb.WhichOneof('filter')
        if kind == 'chain':
            for child in filter_.chain.filters:
                cells = FakeDataClient.filtered(cells, child)
        elif kind == 'interleave':
            cells = {}
            for child in filter_.interleave.filters:
                cells.update(FakeDataClient.filtered(row, child))
        elif kind == 'column_range_filter':
            bounds = filter_.column_range_filter
            cells = {k: v for k, v in cells.items()
                     if bounds.start_qualifier_closed <= k < bounds.end_qualifier_open}
        elif kind == 'family_name_regex_filter':
            if not re.fullmatch(filter_.family_name_regex_filter, 'journal'):
                cells = {}
        elif kind == 'column_qualifier_regex_filter':
            cells = {k: v for k, v in cells.items()
                     if re.fullmatch(filter_.column_qualifier_regex_filter, k)}
        elif kind == 'cells_per_column_limit_filter':
            cells = {k: v[:filter_.cells_per_column_limit_filter] for k, v in cells.items()}
        elif kind == 'value_regex_filter':
            cells = {k: [v for v in values if re.fullmatch(filter_.value_regex_filter, v)]
                     for k, values in cells.items()}
        else:
            raise AssertionError(f'unsupported predicate {kind}')
        return {k: v for k, v in cells.items() if v}

    def check_and_mutate_row(self, req: Any, **kwargs: Any) -> Any:
        assert kwargs['retry'] is None
        assert 0 < kwargs['timeout'] <= 10
        assert req.app_profile_id == 'regional'
        with self.lock:
            self.calls.append(('cas', req))
            row = self.rows.get(req.row_key, {})
            matched = bool(self.filtered(row, req.predicate_filter))
            updated = copy.deepcopy(row)
            for mutation in req.true_mutations if matched else req.false_mutations:
                kind = mutation._pb.WhichOneof('mutation')
                if kind == 'delete_from_column':
                    assert mutation.delete_from_column.family_name == 'journal'
                    updated.pop(mutation.delete_from_column.column_qualifier, None)
                elif kind == 'set_cell':
                    cell = mutation.set_cell
                    assert cell.family_name == 'journal'
                    updated.setdefault(cell.column_qualifier, []).insert(0, cell.value)
                else:
                    raise AssertionError(kind)
            self.rows[req.row_key] = updated
            return type('Result', (), {'predicate_matched': matched})()

    def read_rows(self, req: Any, **kwargs: Any) -> Any:
        assert kwargs['retry'] is None
        assert kwargs['timeout'] == 0.25  # No legacy +1 second timeout padding.
        assert req.rows_limit == 1 and len(req.rows.row_keys) == 1
        with self.lock:
            self.calls.append(('read', req))
            key = req.rows.row_keys[0]
            cells = self.filtered(self.rows.get(key, {}), req.filter)
            chunks = []
            for column, values in sorted(cells.items()):
                for i, value in enumerate(values):
                    chunks.append(ReadRowsResponse.CellChunk(
                        row_key=key, family_name={'value': 'journal'},
                        qualifier={'value': column}, timestamp_micros=1000 - i, value=value,
                    ))
            if not chunks:
                return iter([])
            chunks[-1].commit_row = True
            return iter([ReadRowsResponse(chunks=chunks)])

    def read_modify_write_row(self, req: Any, **kwargs: Any) -> ReadModifyWriteRowResponse:
        assert kwargs.get('retry') is None  # RMW is not replay safe.
        with self.lock:
            self.calls.append(('rmw', req))
            row = self.rows.setdefault(req.row_key, {})
            modified = {}
            for rule in req.rules:
                assert rule._pb.WhichOneof('rule') == 'increment_amount'
                assert rule.family_name == 'journal'
                old = row.get(rule.column_qualifier, [b'\x00' * 8])[0]
                value = struct.unpack('>q', old)[0] + rule.increment_amount
                encoded = struct.pack('>q', value)
                row.setdefault(rule.column_qualifier, []).insert(0, encoded)
                modified[rule.column_qualifier] = encoded
            return ReadModifyWriteRowResponse(row=Row(key=req.row_key, families=[{
                'name': 'journal', 'columns': [
                    {'qualifier': c, 'cells': [{'value': v, 'timestamp_micros': 1000}]}
                    for c, v in modified.items()
                ],
            }]))


def adapter(client: Any | None = None, **changes: Any) -> BigtableJournalStorage:
    kwargs = dict(table_name=TABLE, app_profile_id='regional', region='us-central1',
                  cluster='cluster', profile=profile(), location='projects/test/locations/us-central1-a')
    kwargs.update(changes)
    return BigtableJournalStorage(client or FakeDataClient(), **kwargs)


def test_wire_predicate_atomicity_bounded_reads_and_stale_version() -> None:
    client = FakeDataClient()
    bt = adapter(client)
    key = Grant('w', 'us-central1', 'e', 100).row_key(0)
    assert bt.call(Compare(key, b'version', None, {b'version': b'v1', b'outstanding': b'0'}))
    assert not bt.call(Compare(key, b'version', None, {b'outstanding': b'wrong'}))
    client.rows[key][b'version'] = [b'v2', b'v1']
    assert not bt.call(Compare(key, b'version', b'v1', {b'outstanding': b'wrong'}))
    assert bt.call(Compare(key, b'version', b'v2', {b'version': b'v3', b'outstanding': b'10'}))
    assert bt.call(Read(key, (b'version', b'outstanding'))) == {b'version': b'v3', b'outstanding': b'10'}
    assert client.rows[key][b'version'] == [b'v3']
    assert bt.call(Compare(key, b'version', b'v3', {b'outstanding': None}))
    assert bt.call(Read(key, (b'outstanding',))) == {}


def test_fake_adapter_runs_real_journal_transitions() -> None:
    from tests.test_settlement_journal import acknowledge, setup
    from trusted_router.settlement_journal import Envelope

    j, memory, reg, grant, tickets = setup()
    client = FakeDataClient()
    client.rows = {key: {c: [v] for c, v in row.items()} for key, row in memory.rows.items()}
    j.storage = adapter(client)
    original = j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 50)))
    assert j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 50))) == original
    assert j.run(j.fence(tickets[0])) == original
    acknowledge(j, reg, tickets[0])
    acknowledge(j, reg, tickets[0])
    assert j.storage.call(Read(grant.row_key(0), (b'outstanding',))) == {b'outstanding': b'0'}


@pytest.mark.parametrize('change', ['multi', 'no_transactions', 'wrong_cluster', 'wrong_region', 'wrong_name'])
def test_reject_unsafe_profile(change: str) -> None:
    p = profile()
    location = 'projects/test/locations/us-central1-a'
    if change == 'multi':
        p = AppProfile(name=PROFILE, multi_cluster_routing_use_any={})
    elif change == 'no_transactions':
        p.single_cluster_routing.allow_transactional_writes = False
    elif change == 'wrong_cluster':
        p.single_cluster_routing.cluster_id = 'other'
    elif change == 'wrong_name':
        p.name = 'projects/elsewhere/instances/test/appProfiles/regional'
    else:
        location = 'projects/test/locations/europe-west4-a'
    with pytest.raises(JournalError):
        adapter(profile=p, location=location)


def test_wrong_regional_row_fails_closed() -> None:
    bt = adapter()
    key = Grant('w', 'europe-west4', 'e', 100).row_key(0)
    with pytest.raises(JournalError):
        bt.call(Read(key, (b'version',)))


def increment(client: Any, key: bytes, amount: int, *, table: str = TABLE,
              app_profile: str = 'regional') -> int:
    response = client.read_modify_write_row(ReadModifyWriteRowRequest(
        table_name=table, app_profile_id=app_profile, row_key=key,
        rules=[ReadModifyWriteRule(family_name='journal', column_qualifier=b'counter',
                                  increment_amount=amount)],
    ), retry=None, timeout=10)
    # API returns only modified cells, containing the post-increment value.
    return struct.unpack('>q', response.row.families[0].columns[0].cells[0].value)[0]


def compensated_candidate(client: Any, key: bytes, amount: int, cap: int) -> bool:
    """Rejected RMW candidate, only to test its required compensation semantics.

    Not a safe journal: a crash before compensation leaks; lost RMW responses
    cannot be blindly retried. Production uses no RMW or compensation protocol.
    """
    if increment(client, key, amount) <= cap:
        return True
    increment(client, key, -amount)
    return False


def test_rmw_atomic_return_and_rejected_candidate_compensation() -> None:
    client = FakeDataClient()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: increment(client, b'rmw', 1), range(150)))
    assert sorted(results) == list(range(1, 151))
    assert not compensated_candidate(client, b'rmw', 10, 150)
    assert increment(client, b'rmw', 0) == 150


@pytest.fixture
def real_bigtable() -> Any:
    """Explicit scratch-only opt in; no provisioning, no default credentials use."""
    raw = os.environ.get('TR_JOURNAL_BIGTABLE_INTEGRATION', '')
    if not raw:
        pytest.skip('set TR_JOURNAL_BIGTABLE_INTEGRATION=project,instance,table,profile,region,cluster')
    project, instance, table, app_profile, region, cluster = raw.split(',')
    bt = BigtableJournalStorage.connect(project=project, instance=instance, table=table,
                                       app_profile_id=app_profile, region=region, cluster=cluster,
                                       timeout=10)
    yield bt


def test_real_bigtable_cas_races_and_profile(real_bigtable: BigtableJournalStorage) -> None:
    bt = real_bigtable
    key = Grant('scratch', bt.region, uuid.uuid4().hex, 100).row_key(0)
    try:
        assert bt.call(Compare(key, b'version', None, {b'version': b'initial'}))
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda i: bt.call(Compare(
                key, b'version', b'initial', {b'version': f'winner-{i}'.encode()},
            )), range(150)))
        assert sum(results) == 1
        current = bt.call(Read(key, (b'version',)))[b'version']
        assert current.startswith(b'winner-')
        assert not bt.call(Compare(key, b'version', b'initial', {b'version': b'stale'}))
        # Unsafe alternative routing is rejected locally even on a real client.
        with pytest.raises(JournalError):
            validate_profile(AppProfile(multi_cluster_routing_use_any={}), cluster='x',
                             location='us-central1-a', region='us-central1')
    finally:
        # Only delete this test's isolated row, never table-level operations.
        from google.cloud.bigtable_v2.types import MutateRowRequest, Mutation
        bt.client.mutate_row(MutateRowRequest(
            table_name=bt.table_name, app_profile_id=bt.app_profile_id, row_key=key,
            mutations=[Mutation(delete_from_row={})],
        ), retry=None, timeout=10)


def test_real_bigtable_rmw(real_bigtable: BigtableJournalStorage) -> None:
    bt = real_bigtable
    key = Grant('scratch', bt.region, uuid.uuid4().hex, 100).row_key(0)
    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            values = list(pool.map(lambda _: increment(bt.client, key, 1, table=bt.table_name,
                                                       app_profile=bt.app_profile_id), range(150)))
        assert sorted(values) == list(range(1, 151))
    finally:
        from google.cloud.bigtable_v2.types import MutateRowRequest, Mutation
        bt.client.mutate_row(MutateRowRequest(
            table_name=bt.table_name, app_profile_id=bt.app_profile_id, row_key=key,
            mutations=[Mutation(delete_from_row={})],
        ), retry=None, timeout=10)


def test_real_bigtable_handoff_load(real_bigtable: BigtableJournalStorage) -> None:
    """600 handoffs / 4 in flight; provisioning and slot creation excluded."""
    import time

    from trusted_router.config import Settings
    from trusted_router.settlement_journal import Envelope, InMemoryEpochRegistry, Journal

    bt = real_bigtable
    registry = InMemoryEpochRegistry()
    grant = Grant('scratch-load', bt.region, uuid.uuid4().hex, 5_000_000,
                  int(os.environ.get('TR_JOURNAL_TEST_K', '128')),
                  int(os.environ.get('TR_JOURNAL_TEST_S', '4096')))
    registry.register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    journal = Journal(bt, registry, registry)
    tickets = [registry.allocate(grant, authorization=f'{grant.epoch}-{i}', generation=f'g-{i}',
                                 key_id='key', nonce='nonce', snapshot_hash='a' * 64, idempotency_until=0)
               for i in range(600)]
    keys = {grant.row_key(t.shard) for t in tickets}
    try:
        for shard in {t.shard for t in tickets}:
            journal.run(journal.initialize(grant, shard))
        for ticket in tickets:
            journal.run(journal.create(ticket))

        barrier = threading.Barrier(4)

        def handoff(ticket: Any) -> tuple[Any, float]:
            barrier.wait(timeout=30)
            start = time.perf_counter()
            outcome = journal.run(journal.accept(ticket, Envelope('settle', 'endpoint', 1)))
            return outcome, time.perf_counter() - start

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(handoff, tickets))
        elapsed = time.perf_counter() - start
        latencies = sorted(latency for _, latency in results)
        rate = len(results) / elapsed
        print(f'journal load: {rate:.1f}/s p50={latencies[300]:.4f}s '
              f'p95={latencies[570]:.4f}s conflicts={journal.metrics.cas_conflicts}')
        assert all(outcome.status == 'accepted' for outcome, _ in results)
        assert rate >= 200, 'scratch instance did not meet the required throughput gate'
    finally:
        from google.cloud.bigtable_v2.types import MutateRowRequest, Mutation
        # Synthetic identities have no ledger obligations; clean only our keys.
        for key in keys:
            bt.client.mutate_row(MutateRowRequest(
                table_name=bt.table_name, app_profile_id=bt.app_profile_id, row_key=key,
                mutations=[Mutation(delete_from_row={})],
            ), retry=None, timeout=10)


@pytest.mark.parametrize('unsafe_gc', [False, True])
def test_connect_validates_real_admin_metadata(monkeypatch: pytest.MonkeyPatch, unsafe_gc: bool) -> None:
    from types import SimpleNamespace

    from google.cloud.bigtable_admin_v2.types import Table

    table = Table(name=TABLE, column_families={'journal': {
        'gc_rule': {'max_age': {'seconds': 3600}} if unsafe_gc else {'max_num_versions': 1},
    }})
    calls = []

    def respond(value: Any) -> Any:
        def rpc(**kwargs: Any) -> Any:
            calls.append(kwargs)
            assert kwargs['retry'] is None and kwargs['timeout'] == 0.25
            return value
        return rpc

    client = SimpleNamespace(
        instance=lambda _: SimpleNamespace(name='projects/test/instances/test'),
        instance_admin_client=SimpleNamespace(
            get_app_profile=respond(profile()),
            get_cluster=respond(SimpleNamespace(location='projects/test/locations/us-central1-a')),
        ),
        table_admin_client=SimpleNamespace(get_table=respond(table)),
        table_data_client=FakeDataClient(),
    )
    monkeypatch.setattr('google.cloud.bigtable.Client', lambda **_: client)
    if unsafe_gc:
        with pytest.raises(JournalError, match='GC'):
            BigtableJournalStorage.connect(project='test', instance='test', table='journal',
                                           app_profile_id='regional', region='us-central1', cluster='cluster')
    else:
        bt = BigtableJournalStorage.connect(project='test', instance='test', table='journal',
                                            app_profile_id='regional', region='us-central1', cluster='cluster')
        assert bt.table_name == TABLE
    assert len(calls) == 3


@pytest.mark.parametrize('expected,other', [
    (b'a.b', b'aXb'), (b'a[bc]*|d+$^()?{}', b'abbb'),
    (b'a\\b', b'ab'), (b'\x00\x01\t\r\xff', b'\x00\x02\t\r\xff'),
    (b'end\n', b'end'), (b'end', b'end\n'), (b'end\n\n', b'end\n'),
])
def test_wire_exact_byte_predicates(expected: bytes, other: bytes) -> None:
    client = FakeDataClient()
    bt = adapter(client)
    key = Grant('w', 'us-central1', 'e', 100).row_key(0)
    client.rows[key] = {b's/0000': [other]}
    assert not bt.call(Compare(key, b's/0000', expected, {b'result': b'bad'}))
    assert b'result' not in client.rows[key]
    client.rows[key][b's/0000'] = [expected]
    assert bt.call(Compare(key, b's/0000', expected, {b'result': b'yes'}))


def test_wire_pending_metacharacters_and_pages() -> None:
    from tests.test_settlement_journal import setup
    from trusted_router.settlement_journal import META, Journal, ReadPage, Ticket

    _, memory, reg, grant, _ = setup(count=0)
    t = Ticket(grant, 0, 63, 'a.*[x]\\\n', 'g', 'k', 'n', 'a'*64, 0)
    reg.bind(t)
    client = FakeDataClient()
    client.rows = {k: {c: [v] for c, v in row.items()} for k, row in memory.rows.items()}
    bt = adapter(client)
    j = Journal(bt, reg, reg)
    j.run(j.create(t))
    assert j.run(j.fence(t)).status == 'fenced'
    key = grant.row_key(0)
    assert set(bt.call(ReadPage(key, 0, 60))) == set(META)
    assert set(bt.call(ReadPage(key, 60, 64))) == {*META, t.column}
    with pytest.raises(ValueError, match='bounded'):
        bt.call(Read(key, tuple(str(i).encode() for i in range(69))))
