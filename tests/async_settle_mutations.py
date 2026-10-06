"""Run PR B safety mutations in independent disposable copies; never mutate Git."""
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
MUTATIONS = [
    ('skip-jws-purpose', 'src/trusted_router/detached_jws.py',
     [('_require(key.purpose == purpose, "purpose")', 'pass  # mutant: ignore key purpose')],
     'tests/test_async_settle_ticket.py::test_shadow_grant_as_ticket_rejected'),
    ('drop-key-id-binding', 'src/trusted_router/async_settle_ticket.py',
     [('canonical(dict(expected)) != payload',
       'canonical({k: v for k, v in expected.items() if k != "key_id"}) != canonical({k: v for k, v in claims.items() if k != "key_id"})')],
     'tests/test_async_settle_ticket.py::test_every_claim_is_bound[key_id]'),
    ('accept-expired-ticket', 'src/trusted_router/async_settle_ticket.py',
     [(' or not parsed.iat <= now < parsed.exp', '')],
     'tests/test_async_settle_ticket.py::test_expiry'),
    ('ticket-for-excluded-cohort', 'src/trusted_router/services/async_settle.py',
     [('build_snapshot(endpoints, requested)', 'build_snapshot(endpoints, Eligibility())'),
      ('        require_eligible(requested)', '        pass')],
     'tests/test_async_settle_admission.py::test_each_contract_exclusion'),
    ('skip-cache-freshness', 'src/trusted_router/services/async_settle.py',
     [('cached is None or not 0 <= now - cached[0] < CACHE_SECONDS', 'cached is None'),
      ('0 <= now - cached[0] < CACHE_SECONDS\n                and', 'True\n                and')],
     'tests/test_async_settle_admission.py::test_cache_freshness_failed_reads_concurrency_and_slow_reads'),
    ('failed-read-eligible', 'src/trusted_router/services/async_settle.py',
     [('value = None  # Never log exception text or credentials.',
       'value = Admission(0, 2)  # mutant: missing evidence is healthy')],
     'tests/test_async_settle_admission.py::test_admission_read_failure_is_ineligible'),
    ('inherit-key-file', 'scripts/deploy/rollout.sh',
     [('"TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE="',
       '"TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE=${TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE:-}"')],
     'tests/test_async_settle_ticket.py::test_rollout_never_inherits_key_file'),
]


def run() -> None:
    results = []
    # Copies contain the runtime/test dependencies, not an editable install or
    # a Git directory. sys.executable supplies the already-resolved environment.
    with tempfile.TemporaryDirectory(prefix='pr-b-mutations-') as directory:
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
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--disable-warnings', '-x', test],
                cwd=target, env=env, capture_output=True, text=True, timeout=300,
            )
            log = Path('/tmp') / f'pr-b-mutation-{name}.log'
            log.write_text(completed.stdout + completed.stderr)
            # Collection/import crashes are not killed mutations. Require the
            # selected test's assertion failure and normal pytest failure code.
            killed = completed.returncode == 1 and 'FAILED ' + test.split('[')[0] in completed.stdout
            result = dict(mutation=name, killed=killed, exit_code=completed.returncode, log=str(log))
            results.append(result)
            print(json.dumps(result), flush=True)
            shutil.rmtree(target)
    Path('/tmp/pr-b-mutations.json').write_text(json.dumps(results, indent=2) + '\n')
    assert all(row['killed'] for row in results), results


if __name__ == '__main__':
    run()
