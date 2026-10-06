import json

import pytest
import yaml

from tests import test_async_settle_migration as harness


@pytest.mark.parametrize('fail_at', range(5))
def test_drain_index_migration_idempotent_and_fail_closed(tmp_path, monkeypatch, fail_at):
    monkeypatch.setattr(harness, 'SCRIPT', harness.ROOT/'scripts/deploy/migrate_async_settle_drain_health.sh')
    result = harness.migration(tmp_path, fail_at=fail_at)
    if fail_at:
        assert result.returncode == 23
        assert len(json.loads((tmp_path/'state.json').read_text())['calls']) == fail_at
        # A partially successful deployment can resume without duplicate DDL.
        assert harness.migration(tmp_path).returncode == 0
        assert json.loads((tmp_path/'state.json').read_text())['objects'] == [
            'unresolved_at', 'tr_settle_outbox_unresolved']
    else:
        assert result.returncode == 0, result.stderr
        assert harness.migration(tmp_path).returncode == 0
        state = json.loads((tmp_path/'state.json').read_text())
        assert state['objects'] == ['unresolved_at', 'tr_settle_outbox_unresolved']
        assert len(state['calls']) == 6
        assert sum('update' in call for call in state['calls']) == 2


def test_health_index_in_migrations_block():
    workflow = yaml.safe_load((harness.ROOT/'.github/workflows/deploy.yml').read_text())
    steps = workflow['jobs']['migrate-schema']['steps']
    sibling = next(i for i, step in enumerate(steps) if step.get('run') == 'scripts/deploy/migrate_async_settle_admission.sh')
    added = steps[sibling+1]
    assert added['run'] == 'scripts/deploy/migrate_async_settle_drain_health.sh'
    for field in ('env', 'if', 'shell', 'continue-on-error'):
        assert added.get(field) == steps[sibling].get(field)


def test_health_ddl_is_sparse_and_covering():
    from tests.conformance.spanner_ddl import DDL
    assert ("ALTER TABLE tr_settle_outbox ADD COLUMN unresolved_at TIMESTAMP "
            "AS (IF(status IN ('pending', 'dead'), created_at, NULL)) STORED") in DDL
    assert ("CREATE NULL_FILTERED INDEX tr_settle_outbox_unresolved ON tr_settle_outbox "
            "(unresolved_at) STORING (actual_cost_micro, status)") in DDL
    assert not any('tr_settle_outbox_health' in sql for sql in DDL)
