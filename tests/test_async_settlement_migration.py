"""Exercise operator apply/preflight with a local gcloud recording double."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

GCLOUD = '''#!/usr/bin/env python3
import json, os, pathlib, re, sys
path = pathlib.Path(os.environ['SCHEMA_STATE'])
state = json.loads(path.read_text()) if path.exists() else {'tables': [], 'indexes': [], 'writes': 0}
args = sys.argv[1:]
sql = next((a[6:] for a in args if a.startswith('--sql=')), '')
if 'ddl' in args:
    ddl = next(a[6:] for a in args if a.startswith('--ddl='))
    match = re.search(r'CREATE (?:UNIQUE )?(TABLE|INDEX) (\\w+)', ddl)
    state['tables' if match[1] == 'TABLE' else 'indexes'].append(match[2])
    state['writes'] += 1
    path.write_text(json.dumps(state))
elif 'INFORMATION_SCHEMA.TABLES' in sql:
    name = re.search("table_name='([^']+)'", sql)[1]
    print(int(name in state['tables']))
elif 'INFORMATION_SCHEMA.INDEXES' in sql:
    name = re.search("(?:index_name|INDEX_NAME)='([^']+)'", sql)[1]
    print(('READ_WRITE' if name in state['indexes'] else '') if 'INDEX_STATE' in sql else int(name in state['indexes']))
else:
    table = re.search(r'FROM (\\w+)', sql)[1]
    if table not in state['tables']:
        sys.exit(1)
'''


def test_operator_preflight_and_idempotent_apply(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / 'gcloud'
    executable.write_text(GCLOUD)
    executable.chmod(0o755)
    state = tmp_path / 'state.json'
    env = {**os.environ, 'PATH': str(tmp_path)+os.pathsep+os.environ['PATH'],
           'SCHEMA_STATE': str(state), 'SPANNER_INSTANCE_ID': 'local-test',
           'SPANNER_DATABASE_ID': 'local-test', 'GCP_PROJECT_ID': 'local-test'}
    def run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed local shell scripts with fake gcloud
            ['/bin/bash', str(root / 'scripts/deploy' / script), *args],
            env=env, text=True, capture_output=True, check=False)
    assert run('check_async_settlement_schema.sh').returncode != 0
    assert not state.exists()  # Preflight cannot create anything.
    sleeper = tmp_path / 'sleep'
    waited = tmp_path / 'waited'
    sleeper.write_text('#!/bin/sh\nprintf "%s" "$1" > "$SCHEMA_WAIT"\n')
    sleeper.chmod(0o755)
    env['SCHEMA_WAIT'] = str(waited)
    assert run('check_async_settlement_schema.sh', '--enablement').returncode != 0
    assert not waited.exists()
    applied = run('migrate_async_settlement.sh')
    assert applied.returncode == 0, applied.stderr
    assert json.loads(state.read_text())['writes'] == 3
    assert run('check_async_settlement_schema.sh').returncode == 0
    assert not waited.exists()
    assert run('check_async_settlement_schema.sh', '--enablement').returncode == 0
    from trusted_router.storage_gcp_async_settlement import OBLIGATION_ABSENT_CACHE_SECONDS
    assert float(waited.read_text()) == OBLIGATION_ABSENT_CACHE_SECONDS
    waited.unlink()
    assert run('migrate_async_settlement.sh').returncode == 0
    assert json.loads(state.read_text())['writes'] == 3
    broken = json.loads(state.read_text())
    broken['indexes'] = []
    state.write_text(json.dumps(broken))
    assert run('check_async_settlement_schema.sh', '--enablement').returncode != 0
    assert not waited.exists()
    assert json.loads(state.read_text())['writes'] == 3
