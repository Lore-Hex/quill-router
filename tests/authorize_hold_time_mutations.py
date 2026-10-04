"""Authorize hold-time mutation audit in a disposable COPY; no git writes.

Run: uv run python tests/authorize_hold_time_mutations.py
Each selected test must fail with an assertion, not collection/setup failure.
The credit/key inversion also triggers the autouse lock-order teardown guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORIZE = 'src/trusted_router/storage_gcp_authorize.py'
COUNTERS = 'src/trusted_router/storage_gcp_counter_dml.py'
PREFIX = 'tests/test_authorize_hold_time.py::'
MUTATIONS = [
    ('a: separate credit RPC', AUTHORIZE, [('    batch_credit = has_credit_candidate and not strict_budget and (speculative or skip_key_limit)', '    batch_credit = False')], 'test_only_commit_after_first_credit_write[speculative-True]'),
    ('b: accept zero credit count', AUTHORIZE, [('if batch_credit and counts and counts[0] == 0:', 'if False and batch_credit and counts and counts[0] == 0:'), ('transaction, statements, [(1,)] * len(statements), check_prefix=check_prefix,', 'transaction, statements, ([(0, 1)] + [(1,)] * (len(statements) - 1)), check_prefix=check_prefix,')], 'test_frozen_main_matrix[False-True-speculative-fresh-none-False-accepted]'),
    ('c: drop pause predicate', COUNTERS, [('" AND COALESCE(ARRAY_LENGTH(billing_pause_causes), 0) = 0" if check_pause else ""', '"" if check_pause else ""')], 'test_frozen_main_matrix[True-True-speculative-fresh-first-True-accepted]'),
    ('d: key before credit', AUTHORIZE, [('        statements.extend([reservation_statement, authorization_statement])', '        if batch_credit and speculative:\n            statements.reverse()\n        statements.extend([reservation_statement, authorization_statement])')], 'tests/test_authorize_speculative_batch.py::test_speculative_inserts_follow_credit_and_key'),
    ('e: RPC after batch', AUTHORIZE, [('            transaction, statements, [(1,)] * len(statements), check_prefix=check_prefix,\n        )', '            transaction, statements, [(1,)] * len(statements), check_prefix=check_prefix,\n        )\n        read_reservation_by_idempotency(transaction, pt, idempotency_scope)')], 'test_only_commit_after_first_credit_write[speculative-True]'),
    ('f: change fallback estimate', AUTHORIZE, [('                        transaction, pt, workspace_id, estimate, shard=candidate,', '                        transaction, pt, workspace_id, estimate + 1, shard=candidate,')], 'test_frozen_main_matrix[True-True-speculative-fresh-later-False-accepted]'),
    ('g: remove idempotency read', AUTHORIZE, [('    def txn(transaction: Any) -> dict:\n        if idempotency_scope is not None:', '    def txn(transaction: Any) -> dict:\n        if False and idempotency_scope is not None:')], 'test_only_commit_after_first_credit_write[speculative-True]'),
    ('h: empty array counts as paused', COUNTERS, [('COALESCE(ARRAY_LENGTH(billing_pause_causes), 0) = 0', 'COALESCE(ARRAY_LENGTH(billing_pause_causes), 0) = 1')], 'test_pause_predicate_matches_python_for_ddl_values[causes1]'),
]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='cut2-mutations-', dir='/private/tmp') as directory:
        target = Path(directory) / 'repo'
        target.mkdir()
        for name in ('src', 'tests', 'scripts', 'clickhouse', 'docs'):
            shutil.copytree(ROOT / name, target / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'), symlinks=True)
        shutil.copy(ROOT / 'pyproject.toml', target / 'pyproject.toml')
        for index, (label, relative, replacements, selection) in enumerate(MUTATIONS):
            path = target / relative
            original = path.read_text()
            changed = original
            for before, after in replacements:
                assert before in changed, (label, before)
                changed = changed.replace(before, after)
            path.write_text(changed)
            xml = Path(directory) / f'{index}.xml'
            try:
                result = subprocess.run(  # noqa: S603
                    [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                     selection if '::' in selection else PREFIX + selection,
                     '--tb=short', f'--junitxml={xml}'],
                    cwd=target, capture_output=True, text=True, check=False,
                    env={**os.environ, 'PYTHONPATH': str(target / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
                )
                cases = ET.parse(xml).findall('.//testcase')  # noqa: S314 - local pytest output
                failures = [case for case in cases if case.find('failure') is not None]
                errors = [case.find('error') for case in cases if case.find('error') is not None]
                unexpected_errors = [error for error in errors
                                     if 'LockOrderError' not in ET.tostring(error, encoding='unicode')]
                if result.returncode != 1 or not failures or unexpected_errors:
                    raise RuntimeError(f'Invalid/surviving mutation {label}:\n{result.stdout}\n{result.stderr}')
                print(f'RED: {label} ({selection})', flush=True)
            finally:
                path.write_text(original)
        print(f'{len(MUTATIONS)}/{len(MUTATIONS)} mutations RED; temporary copy deleted on exit', flush=True)


if __name__ == '__main__':
    main()
