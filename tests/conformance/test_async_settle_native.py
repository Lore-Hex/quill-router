"""GoogleSQL semantics for the additive row and bounded exposure query."""
from datetime import UTC, datetime, timedelta

import pytest

from trusted_router.services.async_settle import Admission
from trusted_router.storage_gcp_async_admission import read_admission

pytestmark = pytest.mark.xdist_group('conformance-spanner-emulator')


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_admission_counts_leased_and_dead_excludes_terminal_and_null(native_emulator_resources, backend):
    database, _ = native_emulator_resources
    with database.batch() as batch:
        batch.insert('tr_credit_balance', columns=('workspace_id', 'shard', 'total_credits',
                                                  'total_usage', 'reserved', 'trust_tier'),
                     values=[('async-admit', 0, 100000000, 0, 0, 2)])
        batch.insert('tr_settle_outbox', columns=('authorization_id', 'intent_kind', 'settle_origin',
                                                'actual_cost_micro', 'workspace_id', 'status', 'leased_until'),
                     values=[('async-pending', 'settle', 'typed', 100, 'async-admit', 'pending', None),
                             ('async-leased', 'settle', 'typed', 200, 'async-admit', 'pending', datetime.now(UTC) + timedelta(seconds=300)),
                             ('async-dead', 'settle', 'typed', 300, 'async-admit', 'dead', None),
                             ('async-done', 'settle', 'typed', 10000, 'async-admit', 'done', None),
                             ('async-other', 'settle', 'typed', 10000, 'other', 'pending', None),
                             ('async-legacy', 'settle', 'typed', 10000, None, 'pending', None)])
    assert read_admission(database, 'async-admit') == Admission(600, 2)
    with database.batch() as batch:
        batch.insert('tr_settle_outbox', columns=('authorization_id', 'intent_kind', 'settle_origin',
                                                'actual_cost_micro', 'workspace_id'),
                     values=[(f'async-overflow-{i}', 'settle', 'typed', 0, 'async-admit') for i in range(1001)])
    with pytest.raises(ValueError, match='admission unavailable'):
        read_admission(database, 'async-admit')
