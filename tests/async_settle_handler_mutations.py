"""PR C safety mutations in a disposable copy; never modify the working tree."""
# ruff: noqa: S108 - explicit disposable evidence paths
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLER = 'src/trusted_router/services/async_settle_handler.py'
STORAGE = 'src/trusted_router/storage_gcp_async_settle.py'
TEST = 'tests/test_async_settle_handler.py::'
MUTATIONS = [
    ('remove-atomic-check', [(STORAGE, [
        ('    statements.append(async_reservation_admission_statement(pt, row))', ''),
        ('[*intent_insert_counts(statements), (1,)]', 'intent_insert_counts(statements)'),
        ('if len(actual) == len(statements) and actual[-1] == 0:', 'if False:'),
    ])], 'test_admission_miss_rolls_back'),
    ('accept-zero-admission', [(STORAGE, [
        ('[*intent_insert_counts(statements), (1,)]', '[*intent_insert_counts(statements), (0, 1)]'),
        ('if len(actual) == len(statements) and actual[-1] == 0:', 'if False:'),
    ])], 'test_admission_miss_rolls_back'),
    ('refresh-conflicting-payload', [(HANDLER, [
        ('    if existing.async_version != 1 or existing.payload_hash != row.payload_hash:\n'
         '        raise api_error(409, "Settlement intent already exists with a different payload", "conflict")',
         '    if existing.payload_hash != row.payload_hash:\n'
         '        outbox.enqueue(row)\n'
         '        existing = outbox.get(row.authorization_id, row.intent_kind)'),
    ]), ('src/trusted_router/storage_gcp_settle_outbox.py', [
        ("AND status='pending' AND async_version IS NULL ", "AND status='pending' "),
    ])], 'test_duplicate_conflict_expired_and_immutable'),
    ('drop-hash-comparisons', [(HANDLER, [
        ('if billing.canonical_hash(value.snapshot) != claims.snapshot_hash:', 'if False:'),
    ]), ('src/trusted_router/billing_snapshot.py', [
        ('if envelope.snapshot_hash != canonical_hash(snapshot):', 'if False:'),
    ])], 'test_handler_matrix[hash-400-None]'),
    ('skip-admission-recheck', [(HANDLER, [
        ('if runtime.admission is None or not runtime.admission.eligible(',
         'if False and (runtime.admission is None or not runtime.admission.eligible('),
        ('claims.workspace_id, settings.async_settle_pilot_cap_micro):',
         'claims.workspace_id, settings.async_settle_pilot_cap_micro)):'),
    ])], 'test_handler_matrix[health-200-drain_unhealthy]'),
    ('credit-release-in-enqueue', [(STORAGE, [
        ('    opened: list[Any] = []',
         '    from trusted_router.storage_gcp_counter_dml import release_credit_no_debt_statement\n'
         '    statements.insert(1, release_credit_no_debt_statement(pt, row.workspace_id, 1, 0, shard=0))\n'
         '    counts.insert(1, (1,))\n    opened: list[Any] = []'),
    ])], 'test_enqueue_batch_no_money'),
    ('silently-extend-handoff', [(HANDLER, [
        ('started + (2 if synchronous else 0.5)', 'started + 2'),
    ])], 'test_budget_includes_admission_and_retries'),
    ('skip-status-owner-check', [('src/trusted_router/routes/settlements.py', [
        ('if row is None or row.async_version != 1 or row.workspace_id != principal.workspace.id:',
         'if row is None or row.async_version != 1:'),
        ('if (auth is None or auth.workspace_id != principal.workspace.id\n'
         '                or principal.api_key is None or auth.key_hash != principal.api_key.hash):',
         'if auth is None:'),
    ])], 'test_drain_and_status_ownership[settle]'),
]


def main() -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix='pr-c-mutations-') as directory:
        target = Path(directory)
        for folder in ('src', 'tests', 'scripts'):
            shutil.copytree(ROOT/folder, target/folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
        shutil.copy2(ROOT/'pyproject.toml', target/'pyproject.toml')
        for name, files, selection in MUTATIONS:
            originals = {}
            for relative, edits in files:
                path = target/relative
                text = originals[path] = path.read_text()
                for before, after in edits:
                    assert before in text, (name, before)
                    text = text.replace(before, after)
                path.write_text(text)
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable and test selection
                    [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                     '--disable-warnings', '--tb=short', '-x', TEST+selection],
                    cwd=target, capture_output=True, text=True, timeout=300,
                    env={**os.environ, 'PYTHONPATH': str(target/'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
                )
                log = Path('/tmp')/f'pr-c-mutation-{name}.log'
                log.write_text(result.stdout + result.stderr)
                killed = result.returncode == 1 and 'FAILED ' + TEST+selection.split('[')[0] in result.stdout
                record = dict(mutation=name, killed=killed, exit_code=result.returncode, log=str(log))
                results.append(record)
                print(json.dumps(record), flush=True)
            finally:
                for path, text in originals.items():
                    path.write_text(text)
    Path('/tmp/pr-c-mutations.json').write_text(json.dumps(results, indent=2)+'\n')
    assert all(row['killed'] for row in results), results


if __name__ == '__main__':
    main()
