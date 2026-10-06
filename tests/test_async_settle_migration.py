"""Execute the additive migration with a fake gcloud; never contact a cloud."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy/migrate_async_settle_admission.sh'


def migration(tmp_path, *, fail_at=0):
    stub = tmp_path / 'gcloud'
    stub.write_text('#!' + sys.executable + '\n' + '''
import json, os, re, sys
from pathlib import Path
state_file = Path(os.environ['FAKE_STATE'])
state = json.loads(state_file.read_text()) if state_file.exists() else {'calls': [], 'objects': []}
args = sys.argv[1:]
state['calls'].append(args)
state_file.write_text(json.dumps(state))
if len(state['calls']) == int(os.environ['FAIL_AT']):
    sys.exit(23)
if 'execute-sql' in args:
    sql = next(arg for arg in args if arg.startswith('--sql='))
    name = re.search(r"(?:column_name|index_name)='([^']+)'", sql)[1]
    print(int(name in state['objects']))
else:
    ddl = next(arg for arg in args if arg.startswith('--ddl='))
    name = re.search(r'(?:ADD COLUMN|INDEX) (\\w+)', ddl)[1]
    assert name not in state['objects'], 'duplicate schema mutation'
    state['objects'].append(name)
    state_file.write_text(json.dumps(state))
''')
    stub.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path), 'FAKE_STATE': str(tmp_path / 'state.json'),
           'FAIL_AT': str(fail_at), 'GCP_PROJECT_ID': 'test-project',
           'SPANNER_INSTANCE_ID': 'test-instance', 'SPANNER_DATABASE_ID': 'test-database'}
    return subprocess.run(['/bin/bash', str(SCRIPT)], env=env, capture_output=True, text=True, check=False)  # noqa: S603 - fixed script, isolated fake CLI


def test_migration_repeated_deploy_is_idempotent(tmp_path):
    first = migration(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count('added ') == 4
    assert first.stdout.count('created ') == 1
    second = migration(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout.count('exists; skipped') == 5
    state = json.loads((tmp_path / 'state.json').read_text())
    assert len(state['objects']) == 5
    assert len(state['calls']) == 15
    assert sum('update' in call for call in state['calls']) == 5
    for call in state['calls']:
        assert 'test-database' in call and '--instance=test-instance' in call
        assert call[call.index('--project') + 1] == 'test-project'


@pytest.mark.parametrize('fail_at', range(1, 11))
def test_migration_stops_on_every_gcloud_failure(tmp_path, fail_at):
    result = migration(tmp_path, fail_at=fail_at)
    assert result.returncode == 23
    state = json.loads((tmp_path / 'state.json').read_text())
    assert len(state['calls']) == fail_at


def test_migration_inherits_sibling_workflow_execution():
    workflow = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())
    steps = workflow['jobs']['migrate-schema']['steps']
    sibling = next(i for i, step in enumerate(steps)
                   if step.get('run') == 'scripts/deploy/migrate_operational_analytics_outbox.sh')
    added = steps[sibling + 1]
    assert added['run'] == 'scripts/deploy/migrate_async_settle_admission.sh'
    for field in ('env', 'if', 'shell', 'continue-on-error'):
        assert added.get(field) == steps[sibling].get(field)
