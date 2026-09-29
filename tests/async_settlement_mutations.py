"""Run destructive negatives only in a disposable copy under /private/tmp.

Usage: python -m tests.async_settlement_mutations
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'src/trusted_router/storage_gcp_async_settlement.py'
AUTHORIZE = 'src/trusted_router/storage_gcp_authorize.py'
RETENTION = 'src/trusted_router/storage_gcp_request_records.py'
CASES = [
    ('close_omits_ack_check', REGISTRY,
     [("                    if any(not slot_state(page, t)['ack'] for t in expected.values()):\n"
       "                        raise Conflict('unacknowledged slot')\n", '')],
     'test_registry_close_rejects_unacknowledged_slot_with_zero_outstanding'),
    ('close_omits_durable_projection', REGISTRY,
     [('                    self._project(tx, obligation, verified[_ticket(obligation, grant)])',
       '                    pass')],
     'test_close_projects_ack_after_crash_before_reconcile'),
    ('availability_always_present', REGISTRY,
     [('    name = getattr(database, "name", None)', '    return True\n    name = getattr(database, "name", None)')],
     'test_pre_migration_settlement'),
    ('availability_always_absent', REGISTRY,
     [('    name = getattr(database, "name", None)', '    return False\n    name = getattr(database, "name", None)')],
     'test_guard_scan_and_transaction_both_reapers'),
    ('availability_enable_flag', REGISTRY,
     [('    name = getattr(database, "name", None)',
       '    if not Settings().async_settlement_journal_enabled:\n        return False\n    name = getattr(database, "name", None)')],
     'test_guard_scan_and_transaction_both_reapers'),
    ('availability_conflated', REGISTRY,
     [('    name = getattr(database, "name", None)',
       '    from trusted_router.storage_gcp_authorize import _outbox_table_available\n    return _outbox_table_available(database, param_types)\n    name = getattr(database, "name", None)')],
     'test_table_availability_independent'),
    ('advisory_only', AUTHORIZE,
     [('if obligation_available and (guard_outbox or expires_before is not None) and res.get("authorization_id"):', 'if False and res.get("authorization_id"):'),
      ('if obligation_available and authorization_id:\n            async_rows', 'if False:\n            async_rows')],
     'test_guard_scan_and_transaction_both_reapers'),
    ('enable_flag_guard', AUTHORIZE,
     [('if obligation_available and (guard_outbox or expires_before is not None) and res.get("authorization_id"):',
       'if __import__("trusted_router.config", fromlist=["Settings"]).Settings().async_settlement_journal_enabled and guard_outbox and res.get("authorization_id"):'),
      ('if obligation_available and authorization_id:\n            async_rows',
       'if __import__("trusted_router.config", fromlist=["Settings"]).Settings().async_settlement_journal_enabled and authorization_id:\n            async_rows')],
     'test_guard_scan_and_transaction_both_reapers'),
    ('unresolved_terminal_at', RETENTION,
     [('    "AND NOT EXISTS (SELECT 1 FROM tr_async_settlement_obligation a "\n'
       '    "WHERE a.authorization_id = tr_gateway_authorization.authorization_id "\n'
       '    "AND a.state NOT IN (\'acknowledged\', \'fenced\'))"', '    ""')],
     'test_retention_is_guarded_independently_of_flags'),
    ('snapshot_booking', AUTHORIZE,
     [('if obligation_available and authorization_id:\n            async_rows',
       'if authorization_id and not snapshot_booking_enabled:\n            async_rows'),
      # Remove the independent retention rollback barriers too: this mutant
      # really commits a guarded snapshot booking, rather than merely changing
      # OUTBOX_GUARDED to GUARD_LOST with the money transaction rolled back.
      ('        if (\n            complete_reservation_retention(',
       '        if (False and\n            complete_reservation_retention('),
      ('        if (\n            complete_gateway_authorization_retention(',
       '        if (False and\n            complete_gateway_authorization_retention(')],
     'test_guard_scan_and_transaction_both_reapers and True-True'),
    ('omit_older_bound', REGISTRY,
     [("sum(int(r['recorded_bound']) for r in opened)", "int(prior['recorded_bound'] or 0)")],
     'test_all_open_epoch_bounds_and_close_preconditions'),
    ('nonidempotent_successor', REGISTRY,
     [("if prior['successor_epoch'] is not None:", "if False:")],
     'test_binding_sizing_and_epoch_cannot_be_recycled'),
    ('recycled_binding', REGISTRY,
     [('old = self._obligations(tx, aid=ticket.authorization)',
       "old = [r for r in self._obligations(tx, aid=ticket.authorization) if r['epoch'] == ticket.grant.epoch]")],
     'test_binding_sizing_and_epoch_cannot_be_recycled'),
    ('import_as_finalization', REGISTRY,
     [("'kind': 'ledger_finalization', 'ticket': asdict(ticket)", "'kind': 'import', 'ticket': asdict(ticket)")],
     'test_registry_lifecycle_receipts_and_restart'),
    ('mutable_sizing', REGISTRY,
     [('if any(_grant(r) == grant for r in rows):', "if any(r['epoch'] == grant.epoch for r in rows):")],
     'test_binding_sizing_and_epoch_cannot_be_recycled'),
]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='pr4-mutations-', dir='/private/tmp') as directory:
        target = Path(directory)
        # Copy source/tests plus runtime assets; no git writes or source edits.
        ignore = shutil.ignore_patterns('.git', '.venv', '__pycache__', '.pytest_cache',
                                        '.mypy_cache', '.ruff_cache', 'node_modules')
        shutil.copytree(ROOT, target, dirs_exist_ok=True, ignore=ignore)
        for name, filename, replacements, test in CASES:
            original = (ROOT / filename).read_text()
            changed = original
            for old, new in replacements:
                assert old in changed, (name, old)
                changed = changed.replace(old, new)
            (target / filename).write_text(changed)
            env = {**os.environ, 'PYTHONPATH': str(target / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run(  # noqa: S603 - fixed local pytest command and reviewed mutations
                [os.sys.executable, '-m', 'pytest', '-q', '-x', '-p', 'no:cacheprovider',
                 'tests/test_async_settlement_obligations.py', '-k', test,
                 '--basetemp', str(target / 'pytest-temp')],
                cwd=target, env=env, text=True, capture_output=True, check=False, timeout=180,
            )
            (target / filename).write_text(original)
            output = result.stdout + result.stderr
            if result.returncode != 1 or 'FAILED tests/test_async_settlement_obligations.py::' not in output:
                raise AssertionError(f'{name}: invalid negative or survivor\n{output}')
            print(f'{name}: RED — {test}', flush=True)
            if name.startswith('close_'):
                print('\n'.join(line for line in output.splitlines()
                                if line.startswith(('E ', 'FAILED '))), flush=True)


if __name__ == '__main__':
    main()
