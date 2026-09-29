"""Run targeted money mutations ONLY in a disposable copy outside the worktree.

PYTHONPATH=src python tests/settlement_journal_mutations.py
The compensation mutation targets the rejected RMW example; the chosen journal
has no compensation write to skip. All other mutations target production code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = 'src/trusted_router/settlement_journal.py'
ADAPTER = 'src/trusted_router/settlement_journal_bigtable.py'
WIRE = 'tests/test_settlement_journal_bigtable.py'
ATOMIC_ACCEPT = """            if (yield Compare(key, b'version', row[b'version'], {
                ticket.column: encode(desired),
                b'outstanding': str(outstanding + envelope.charge).encode(),
                b'version': token(),
            })):
                return result(desired)"""
# Introduce a real second RPC: a crash/pause after the first now exposes an
# accepted intent without its counter. This is not merely a missing increment.
SPLIT_ACCEPT = ATOMIC_ACCEPT.replace(
    "                b'outstanding': str(outstanding + envelope.charge).encode(),\n", "",
).replace(
    "                return result(desired)",
    "                yield Compare(key, ticket.column, encode(desired), {\n"
    "                    b'outstanding': str(outstanding + envelope.charge).encode(),\n"
    "                })\n"
    "                return result(desired)",
)
MUTATIONS = [
    ('split_counter_intent', STATE, ATOMIC_ACCEPT, SPLIT_ACCEPT,
     'tests/test_settlement_journal.py::test_two_distinct_accepts_cannot_overrun_shard'),
    ('unconditional_increment', STATE,
     "            if state['status'] != 'pending':\n                return result(state)",
     "            if state['status'] not in ('pending', 'accepted'):\n                return result(state)",
     'tests/test_settlement_journal.py::test_duplicate_small_charge_does_not_increment'),
    ('skipped_compensation_rejected_rmw_candidate', WIRE,
     '    increment(client, key, -amount)\n', '',
     WIRE + '::test_rmw_atomic_return_and_rejected_candidate_compensation'),
    ('stale_version_match', ADAPTER,
     '            chain.append(RowFilter(value_regex_filter=b\'^\' + re.escape(request.expected) + b\'$\'))',
     '            chain.insert(-1, RowFilter(value_regex_filter=b\'^\' + re.escape(request.expected) + b\'$\'))',
     WIRE + '::test_wire_predicate_atomicity_bounded_reads_and_stale_version'),
    ('refund_overwrites_settle', STATE,
     "            if state.get('hash') and state['hash'] != desired['hash']:",
     "            if envelope.kind == 'refund':\n"
     "                state['status'] = 'pending'\n"
     "                state.pop('hash', None)\n"
     "            if state.get('hash') and state['hash'] != desired['hash']:",
     'tests/test_settlement_journal.py::test_accept_retry_conflict_ack_and_cap'),
    ('duplicate_ack_decrement', STATE,
     "            if state['ack']:\n                return result(state)",
     "            if False:\n                return result(state)",
     'tests/test_settlement_journal.py::test_accept_retry_conflict_ack_and_cap'),
    ('age_deletes_pending', STATE,
     "        if not state['ack'] or row[b'sealed'] not in (b'1', b'2'):", '        if False:',
     'tests/test_settlement_journal.py::test_retention_never_deletes_pending_or_unacknowledged'),
    ('multi_cluster_accepted', ADAPTER,
     "    pb = getattr(profile, '_pb', profile)",
     "    return\n    pb = getattr(profile, '_pb', profile)",
     WIRE + '::test_reject_unsafe_profile[multi]'),
]


JOURNAL = 'tests/test_settlement_journal.py::'
MUTATIONS += [
    ('intent_without_increment', STATE,
     "                b'outstanding': str(outstanding + envelope.charge).encode(),",
     "                b'outstanding': str(outstanding).encode(),",
     JOURNAL + 'test_accept_retry_conflict_ack_and_cap'),
    ('cap_check_removed', STATE,
     "or outstanding + envelope.charge > int(row[b'cap'])", '',
     JOURNAL + 'test_accept_retry_conflict_ack_and_cap'),
    ('sealed_ignored_on_accept', STATE,
     "row[b'sealed'] != b'0' or outstanding + envelope.charge", 'outstanding + envelope.charge',
     JOURNAL + 'test_partial_seal_missing_slot_and_resume'),
    ('ack_without_decrement', STATE,
     "str(int(row[b'outstanding']) - amount).encode()", "str(int(row[b'outstanding'])).encode()",
     JOURNAL + 'test_accept_retry_conflict_ack_and_cap'),
    ('ack_releases_nonaccepted', STATE,
     " if state['status'] == 'accepted' else 0", '',
     JOURNAL + 'test_ack_nonaccepted_never_releases_other_slot_debt'),
    ('payload_mismatch_accepted', STATE,
     "            if state.get('hash') and state['hash'] != desired['hash']:", '            if False:',
     JOURNAL + 'test_accept_retry_conflict_ack_and_cap'),
    ('close_with_debt', STATE,
     " or row[b'outstanding'] != b'0'", '',
     JOURNAL + 'test_close_inconsistent_debt_independent_of_slots'),
    ('close_unacknowledged', STATE,
     "                    if not slot_state(page, ticket)['ack']:", '                    if False:',
     JOURNAL + 'test_close_zero_debt_unresolved'),
    ('close_unregistered_slot', STATE,
     "                if {c for c in page if c.startswith(b's/')} != set(expected):\n"
     "                    raise JournalError('unregistered or missing slot in closure page')\n",
     '', JOURNAL + 'test_close_zero_debt_unregistered_slot'),
    ('create_after_seal', STATE,
     "            if row.get(b'sealed') != b'0' or self.registry.is_retired(ticket.grant):",
     '            if False:',
     JOURNAL + 'test_random_schedules_crashes_retries_ack_seal[0]'),
    ('fence_tolerates_pending', STATE,
     "        if winner['status'] == 'pending':", '        if False:',
     JOURNAL + 'test_failed_fence_predicate_pending_read_fails_closed'),
    ('ack_mismatched_hash', STATE,
     " or state.get('hash', '') != receipt.payload_hash", '',
     JOURNAL + 'test_fence_refund_and_receipt_binding'),
    ('receipt_unverified', STATE,
     '        if not self.receipts.verify(ticket, receipt):', '        if False:',
     JOURNAL + 'test_fence_refund_and_receipt_binding'),
    ('legacy_tier_fallback', STATE,
     "if type(tier) is not int or tier not in (1, 2, 3) or grant.cap > tier_cap(settings, tier):",
     "if grant.cap > tier_cap(settings, tier if tier in (1, 2, 3) else 3):",
     JOURNAL + 'test_registry_one_region_tier_caps_identity_and_slot_bound'),
    ('value_predicate_unescaped', ADAPTER,
     're.escape(request.expected)', 'request.expected',
     WIRE + '::test_wire_exact_byte_predicates'),
    ('age_gc_accepted', ADAPTER,
     "        if rule._pb.WhichOneof('rule') != 'max_num_versions' or rule.max_num_versions != 1:",
     '        if False:', WIRE + '::test_connect_validates_real_admin_metadata[True]'),
    ('wrong_region_accepted', ADAPTER,
     "        if len(parts) != 5 or parts[1] != b'settlement' or parts[2] != self.region.encode().hex().encode():",
     '        if False:', WIRE + '::test_wrong_regional_row_fails_closed'),
    ('pending_ack_allowed', STATE,
     "state['status'] == 'pending' or state.get('hash', '') != receipt.payload_hash",
     "state.get('hash', '') != receipt.payload_hash",
     JOURNAL + 'test_verified_receipt_before_terminal_selection_rejected'),
    ('slot_hash_not_recomputed', STATE,
     "            if state['hash'] != expected:", '            if False:',
     JOURNAL + 'test_corrupt_terminal_fails_closed[envelope_changed]'),
    ('grant_sizing_not_validated', STATE,
     "if (row[b'shards'] != str(grant.shards).encode() or\n                row[b'slots'] != str(grant.slots).encode()):",
     'if False:', JOURNAL + 'test_grant_sizing_validated_on_reads'),
    ('older_bound_ignored', STATE,
     'sum(self._bounds[g] for g in open_grants)', 'self._bounds.get(previous, 0)',
     JOURNAL + 'test_overlap_counts_every_older_epoch_and_replays_once'),
    ('grant_before_every_shard_sealed', STATE,
     "                if row[b'sealed'] not in (b'1', b'2'):", '                if False:',
     JOURNAL + 'test_grant_requires_every_shard_sealed'),
]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='journal-mutations-') as temp:
        copy = Path(temp)
        for directory in ('src', 'tests'):
            shutil.copytree(ROOT / directory, copy / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc'))
        shutil.copy2(ROOT / 'pyproject.toml', copy / 'pyproject.toml')
        env = {**os.environ, 'PYTHONPATH': str(copy / 'src'),
               'PYTHONDONTWRITEBYTECODE': '1', 'HYPOTHESIS_STORAGE_DIRECTORY': str(copy / 'hypothesis')}
        # Baseline must pass so dependency/setup failures cannot count as kills.
        tests = sorted({m[4] for m in MUTATIONS})
        baseline = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--tb=short',  # noqa: S603
                                   *tests], cwd=copy, env=env, capture_output=True, text=True)
        if baseline.returncode:
            raise RuntimeError('mutation baseline failed:\n' + baseline.stdout + baseline.stderr)
        for name, path, old, new, target in MUTATIONS:
            file = copy / path
            original = file.read_text()
            assert original.count(old) == 1, name
            file.write_text(original.replace(old, new))
            try:
                result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--tb=short',  # noqa: S603
                                         target], cwd=copy, env=env, capture_output=True, text=True)
                # Collection/import errors are not acceptable mutation evidence.
                assert result.returncode == 1 and 'FAILED ' in result.stdout, (
                    name + '\n' + result.stdout + result.stderr
                )
                failures = [line for line in result.stdout.splitlines() if line.startswith('FAILED ')]
                print(f'{name}: KILLED: {failures[0]}', flush=True)
            finally:
                file.write_text(original)


if __name__ == '__main__':
    main()
