"""RAM handoff negative controls. Mutate disposable copies only."""
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
MEMORY = 'src/trusted_router/services/async_settle_memory.py'
ADMISSION = 'src/trusted_router/services/async_settle.py'
REFRESH = 'src/trusted_router/services/async_settle_admission_refresh.py'
MUTATIONS = [
    ('accept-before-eligibility', HANDLER,
     '            row = intent(value, claims, amount)',
     '            row = intent(value, claims, amount)\n'
     '            if memory_mode and memory is not None:\n'
     '                return memory.accept(row)',
     'test_checks_before_acceptance[region]'),
    ('skip-queued-admission', 'src/trusted_router/services/async_settle.py',
     'value.pending_micro + queued_micro <=', 'value.pending_micro <=',
     'test_queued_workspace_amount_counts_in_admission'),
    ('drop-duplicate-index', MEMORY, 'existing = self.index.get(key)', 'existing = None',
     'test_duplicate_and_conflict'),
    ('shutdown-without-flush', MEMORY,
     '            self.thread.join(timeout=SHUTDOWN_SECONDS)', '            pass',
     'test_shutdown_flushes_in_order_with_fresh_context'),
    ('drop-transient-error', MEMORY, '            outcome = "retry"', '            outcome = "lost"',
     'test_transient_retry_or_budget_loss[True]'),
    ('spanner-duplicate-read', MEMORY,
     '            if duplicate is not None:\n                return duplicate',
     '            if duplicate is not None:\n'
     '                handler.STORE.get_gateway_authorization(row.authorization_id)\n'
     '                return duplicate',
     'test_no_spanner_on_memory_branches'),
    ('omit-miss-activity', ADMISSION,
     '                self.evaluated_at[workspace_id] = now\n', '',
     'test_stale_cached_only_miss_reactivates_read_ahead'),
    ('omit-miss-placeholder', ADMISSION,
     '                    self.entries[workspace_id] = (float("-inf"), None)',
     '                    pass',
     'test_cold_cached_only_miss_warms_via_read_ahead'),
    ('restore-second-lookup', HANDLER,
     '                    result = duplicate_response(existing, row, resolve=not memory_mode)',
     '                    result = find_duplicate(row)',
     'test_duplicate_survives_writer_eviction[expired]'),
    ('full-health-failure-keeps-evidence', ADMISSION,
     '                self.health = None\n'
     '                try:\n'
     '                    self.health = decode_health(self.health_read(), now=self.clock(), wall=self.wall_clock())\n'
     '                except Exception:\n'
     '                    self.health = None',
     '                try:\n'
     '                    self.health = decode_health(self.health_read(), now=self.clock(), wall=self.wall_clock())\n'
     '                except Exception:\n'
     '                    pass',
     'test_shared_health_failure_and_read_ahead_recovery[full]'),
    ('failed-read-ahead-refreshes-health-timestamp', REFRESH,
     '            except Exception:  # noqa: S110 - preserve old evidence on failed reads\n'
     '                pass\n'
     '        for workspace in workspaces:',
     '            except Exception:  # noqa: S110 - preserve old evidence on failed reads\n'
     '                with cache.lock:\n'
     '                    cache.health_read_at = started\n'
     '        for workspace in workspaces:',
     'test_shared_health_failure_and_read_ahead_recovery[full]'),
    ('read-ahead-cannot-recover-cleared-health', REFRESH,
     '                            cache.health, cache.health_read_at = health, started',
     '                            if cache.health is not None or cache.health_read_at == float("-inf"):\n'
     '                                cache.health, cache.health_read_at = health, started',
     'test_shared_health_failure_and_read_ahead_recovery[full]'),
    ('read-ahead-keeps-out-unhealthy-records', REFRESH,
     '                health = observe_health(cache.health_read(), now=clock(), wall=cache.wall_clock())\n',
     '                health = observe_health(cache.health_read(), now=clock(), wall=cache.wall_clock())\n'
     '                health = health if health is not None and health.p95_age_seconds <= 5 else None\n',
     'test_unhealthy_read_ahead_record_closes_cached_only_admission'),
    ('eligible-ignores-drain-p95', ADMISSION,
     '                and 0 <= health.p95_age_seconds <= 5\n',
     '                and 0 <= health.p95_age_seconds\n',
     'test_unhealthy_read_ahead_record_closes_cached_only_admission'),
]


def main() -> None:
    evidence = Path(os.environ['ASYNC_SETTLE_MUTATION_OUTPUT_DIR'])
    evidence.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='async-ram-mutants-') as directory:
        target = Path(directory)
        for folder in ('src', 'tests', 'scripts', '.github'):
            shutil.copytree(ROOT / folder, target / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
        for file in ('pyproject.toml', '.python-version'):
            shutil.copy2(ROOT / file, target / file)
        for name, relative, before, after, test in MUTATIONS:
            path = target / relative
            original = path.read_text()
            assert original.count(before) == 1, name
            path.write_text(original.replace(before, after))
            selection = 'tests/test_async_settle_memory.py::' + test
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable and test selection
                    [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                     '--disable-warnings', '--tb=short', '-x', selection], cwd=target,
                    capture_output=True, text=True, timeout=300,
                    env={**os.environ, 'PYTHONPATH': str(target / 'src'),
                         'PYTHONDONTWRITEBYTECODE': '1'},
                )
                (evidence / (name + '.log')).write_text(result.stdout + result.stderr)
                killed = result.returncode == 1 and 'FAILED ' + selection in result.stdout
                record = dict(mutation=name, test=selection, killed=killed, exit_code=result.returncode)
                results.append(record)
                print(json.dumps(record), flush=True)
            finally:
                path.write_text(original)
    (evidence / 'mutations.json').write_text(json.dumps(results, indent=2) + '\n')
    assert all(row['killed'] for row in results), results


if __name__ == '__main__':
    main()
