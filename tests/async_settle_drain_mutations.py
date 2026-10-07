"""Run PR D drain mutations in independent disposable copies; never mutate Git."""
# ruff: noqa: S108 - user-requested disposable cache and evidence paths
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRAIN = 'src/trusted_router/services/settle_outbox_worker.py'
HEALTH = 'src/trusted_router/services/async_settle.py'
TEST = 'tests/test_async_settle_drain.py::'
MUTATIONS = [
    ('done-insert-nonnull-unresolved', 'scripts/deploy/migrate_async_settle_drain_health.sh',
     [("TIMESTAMP '1970-01-01T00:00:00Z'), NULL)) STORED",
       "TIMESTAMP '1970-01-01T00:00:00Z'), created_at)) STORED")],
     TEST + 'test_inserted_done_has_no_unresolved_index_entry'),
    ('publish-health-every-pass', DRAIN,
     [('if claim_health_publish(outbox._database, settings.settle_outbox_health_publish_interval_seconds):',
       'if True:')], TEST + 'test_health_publish_cadence_across_workers'),
    ('remove-claim-lease-fence', 'src/trusted_router/storage_gcp_settle_outbox.py',
     [("AND status='pending' AND (leased_until IS NULL OR leased_until < @now)",
       "AND status='pending'")], TEST + 'test_concurrent_claims_and_owner_crash_fences'),
    ('housekeeping-every-poll', DRAIN,
     [('if claim_housekeeping(outbox._database):', 'if True:')],
     TEST + 'test_housekeeping_cadence_over_fast_polls'),
    ('publish-without-heartbeat', 'src/trusted_router/storage_gcp_async_admission.py',
     [('worker_heartbeat=time.time(), complete=complete,', 'complete=complete,')],
     TEST + 'test_publish_empty_has_heartbeat'),
    ('stale-health-is-fresh', HEALTH,
     [('observed, heartbeat = value["observed_at"], value["worker_heartbeat"]',
       'observed, heartbeat = wall, value["worker_heartbeat"]')],
     TEST + 'test_health_freshness_matrix'),
    ('claim-over-concurrency-bound', DRAIN,
     [('concurrency = min(settings.settle_outbox_worker_concurrency, settings.settle_outbox_claim_batch)',
       'concurrency = 4')],
     TEST + 'test_claims_never_exceed_running_slots'),
    ('consumer-read-with-admission-off', HEALTH,
     [('if settings.async_settle_admission_enabled:', 'if True:'),
      ('return read_health(backend._database) if settings.async_settle_admission_enabled else None',
       'return read_health(backend._database)')],
     TEST + 'test_consumer_off_no_rpc_and_on_bounded_refresh'),
    ('drop-batch-tail-rule', DRAIN,
     [("if (time.monotonic() >= deadline or row.leased_until is None\n"
       "            or dt.datetime.fromisoformat(row.leased_until.replace('Z', '+00:00')) <= now):", 'if False:')],
     TEST + 'test_batch_tail_expired_claim_never_applied'),
    ('retention-body-clear', 'src/trusted_router/storage_gcp_settle_outbox.py',
     [('terminal_at=@now, settle_body=NULL', 'terminal_at=@now, settle_body=settle_body')],
     'tests/test_async_settle_proof.py::test_four_path_billing_state[component_half_up]'),
    ('generation-future-terminal-at', 'src/trusted_router/storage_gcp_generation_records.py',
     [(') -> DmlStatement:\n    return (\n        "INSERT INTO tr_generation ("',
       ') -> DmlStatement:\n    terminal_at = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)\n'
       '    return (\n        "INSERT INTO tr_generation ("')],
     'tests/test_async_settle_proof_oracle.py::test_f83bbaac_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('sparse-predicate', 'src/trusted_router/storage_gcp_async_admission.py',
     [('WHERE unresolved_at IS NOT NULL ORDER BY', 'WHERE TRUE ORDER BY')],
     'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[sparse]'),
    ('control-kind', 'src/trusted_router/storage_gcp_async_admission.py',
     [('WHERE kind=@kind AND id=@id', 'WHERE id=@id')],
     'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[control_kind]'),
    ('control-id', 'src/trusted_router/storage_gcp_async_admission.py',
     [('WHERE kind=@kind AND id=@id', 'WHERE kind=@kind')],
     'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[control_id]'),

]


def run() -> None:
    results = []
    # Copies contain the runtime/test dependencies, not an editable install or
    # a Git directory. sys.executable supplies the already-resolved environment.
    with tempfile.TemporaryDirectory(prefix='pr-d-mutations-') as directory:
        root = Path(directory)
        for name, relative, edits, test in MUTATIONS:
            target = root / name
            target.mkdir()
            for folder in ('src', 'tests', 'scripts'):
                shutil.copytree(ROOT / folder, target / folder,
                                ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
            shutil.copy2(ROOT / 'pyproject.toml', target / 'pyproject.toml')
            file = target / relative
            text = file.read_text()
            for old, new in edits:
                assert old in text, (name, old)
                text = text.replace(old, new)
            file.write_text(text)
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(target / 'src'),
                   'UV_CACHE_DIR': '/tmp/uv', 'RUFF_CACHE_DIR': '/tmp/ruff', 'MYPY_CACHE_DIR': '/tmp/mypy'}
            completed = subprocess.run(  # noqa: S603 - fixed disposable repository/test args
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--disable-warnings', test],
                cwd=target, env=env, capture_output=True, text=True, timeout=300,
            )
            log = Path('/tmp') / f'pr-d-mutation-{name}.log'
            log.write_text(completed.stdout + completed.stderr)
            # Collection/import crashes are not killed mutations. Require the
            # selected test's assertion failure and normal pytest failure code.
            killed = completed.returncode == 1 and 'FAILED ' + test.split('[')[0] in completed.stdout
            result = dict(mutation=name, killed=killed, exit_code=completed.returncode, log=str(log))
            results.append(result)
            print(json.dumps(result), flush=True)
            shutil.rmtree(target)
    Path('/tmp/pr-d-mutations.json').write_text(json.dumps(results, indent=2) + '\n')
    assert all(row['killed'] for row in results), results


if __name__ == '__main__':
    run()
