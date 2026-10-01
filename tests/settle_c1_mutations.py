"""Behavioral C1 mutation audit in a disposable COPY; no git writes.

Run: uv run python tests/settle_c1_mutations.py
Each selected test must fail with an assertion, not collection/setup failure.
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
PREFIX = 'tests/test_settle_c1.py::'
MUTATIONS = [
    ('drop reserved guard; negative reserved reachable', COUNTERS,
     [('AND shard=@shard AND reserved >= @hold "', 'AND shard=@shard "')],
     'test_credit_guards_execute_without_fake_predicate_assertions[99-0-0]'),
    ('drop transactional no-debt guard', COUNTERS,
     [('        "AND (@hold <= @actual OR NOT EXISTS ("\n'
       '        "SELECT 1 FROM tr_trust_event "\n'
       '        "WHERE workspace_id=@ws AND kind=\'payment\' AND unrecovered_micro>0))",',
       '        "",')],
     'test_credit_guards_execute_without_fake_predicate_assertions[100-50-0]'),
    ('book release again on terminal replay', AUTHORIZE,
     [('        fold_tail = (',
       '        if res["settled"]:\n'
       '            release_credit(transaction, pt, res["workspace_id"], 0, book_actual,\n'
       '                           shard=res["credit_shard"])\n'
       '        fold_tail = (')],
     'test_main_money_differential[True-True-replay_settled]'),
    ('guard mismatch commits instead of falling back', AUTHORIZE,
     [('if count == 0 and reason is not None:',
       'if count == 0 and reason is not None and reason not in {"credit_release_zero", "key_release_zero"}:'),
      ('counts.extend([(1,), (1,)])', 'counts.extend([(0, 1), (0, 1)])')],
     'test_main_money_differential[True-True-debt]'),
    ('outbox done moved after finalize commit', AUTHORIZE,
     [('    mark_done = (', '    mark_done = False and ('),
      ('        _log_missing_key_releases(result)\n        return result',
       '        _log_missing_key_releases(result)\n'
       '        if settle_outbox_done is not None:\n'
       '            database.run_in_transaction(lambda tx: mark_done_unleased_tx(\n'
       '                tx, pt, authorization_id=settle_outbox_done[0],\n'
       '                intent_kind=settle_outbox_done[1]))\n'
       '        return result')],
     'test_outbox_resolution_is_in_charge_commit'),
    ('tail counts not validated', AUTHORIZE,
     [('counts.extend([(1,), (1,)])', 'counts.extend([(0, 1, 2, -1), (0, 1, 2, -1)])')],
     'test_tail_counts_require_rollback[2-7]'),
    ('refund accidentally folds tail', AUTHORIZE,
     [('speculate and success and not res["settled"]', 'speculate and not res["settled"]')],
     'test_refund_never_folds_tail'),
]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='astra-c1-mutations-', dir='/private/tmp') as directory:
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
                     PREFIX + selection, '--tb=short', f'--junitxml={xml}'],
                    cwd=target, capture_output=True, text=True, check=False,
                    env={**os.environ, 'PYTHONPATH': str(target / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
                )
                cases = ET.parse(xml).findall('.//testcase')  # noqa: S314 - local pytest output
                if result.returncode != 1 or not cases or any(case.find('failure') is None for case in cases):
                    raise RuntimeError(f'Invalid/surviving mutation {label}:\n{result.stdout}\n{result.stderr}')
                print(f'RED: {label} ({selection})', flush=True)
            finally:
                path.write_text(original)
        print(f'{len(MUTATIONS)}/{len(MUTATIONS)} mutations RED; temporary copy deleted on exit', flush=True)


if __name__ == '__main__':
    main()
