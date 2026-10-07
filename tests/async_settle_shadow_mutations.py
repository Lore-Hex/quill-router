"""F2b assertion-killed mutations, executed only in a disposable source copy."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPARE = 'src/trusted_router/async_settle_shadow_compare.py'
RUNTIME = 'src/trusted_router/services/async_settle_shadow.py'
STORAGE = 'src/trusted_router/storage_gcp_async_settle_shadow.py'
REPORT = 'scripts/async_settle/shadow_report.py'
BASE = 'tests/test_async_settle_shadow.py::'
MUTATIONS = [
    ('evidence-before-money-outcome',RUNTIME,[('capture.document = document',
        'capture.document = document\n        capture.runtime.store.reserve(day_at(time.time()), time.monotonic()+1)')],
        'tests/test_async_settle_shadow_integration.py::test_shadow_failure_after_real_money_commit[none-2]'),
    ('shadow-error-escapes',RUNTIME,[('# The caller\'s original result/error/cancellation always wins.\n            pass',
        '# The caller\'s original result/error/cancellation always wins.\n            raise')],
        'tests/test_async_settle_shadow_integration.py::test_shadow_failure_after_real_money_commit[queue-2]'),
    ('shadow-error-replaces-legacy-error',RUNTIME,[('# The caller\'s original result/error/cancellation always wins.\n            pass',
        '# The caller\'s original result/error/cancellation always wins.\n            raise')],
        'tests/test_async_settle_shadow_integration.py::test_shadow_submission_failure_preserves_real_legacy_error'),
    ('booked-from-envelope',COMPARE,[('result = Comparison(booked_micro=context.booking.amount)',
        'result = Comparison(booked_micro=parse_header(headers).terminal.charge_micro)')],'tests/test_async_settle_shadow_integration.py::test_shadow_failure_after_real_money_commit[none-999]'),
    ('signature-bypass','src/trusted_router/async_settle_shadow_binding.py',[
        ('claims, key, payload = verify(token, keys, TYP, PURPOSE)',
         'import json\n        from trusted_router.detached_jws import b64decode\n        payload = b64decode(token.split(".")[1])\n        claims = json.loads(payload)\n        key = keys[0]')],BASE+'test_invalid_signature'),
    ('signed-snapshot-hash',COMPARE,[('if out.snapshot_hash != claims.snapshot_hash:', 'if False:'), ('terminal.snapshot_hash != claims.snapshot_hash or ', '')],BASE+'test_signed_hash_not_self_hash'),
    ('terminal-payload-hash',COMPARE,[('or b.canonical_hash(terminal) != envelope.payload_hash','')],BASE+'test_signed_hash_not_self_hash'),
    ('hash-only-equality',COMPARE,[('or snapshot_hash != claims.snapshot_hash',''),
        ('if out.snapshot_hash != claims.snapshot_hash:', 'if False:'),
        ('if terminal.snapshot_hash != claims.snapshot_hash or b.canonical_hash(terminal) != envelope.payload_hash:', 'if False:')],BASE+'test_hash_only_success_and_corrected_failure'),
    ('opt-in-removed',RUNTIME,[('return workspace in self.settings.async_settle_shadow_workspace_ids and not self.settings.async_settle_enabled',
        'return not self.settings.async_settle_enabled')],BASE+'test_authorize_shadow_signing_no_ticket_api'),
    ('rate-limit-removed',RUNTIME,[('if self.tokens < 1:', 'if False:')],'tests/test_async_settle_shadow_lifecycle.py::test_rate_admission_precedes_real_comparator_and_refills'),
    ('daily-cap-removed',STORAGE,[('granted = min(100, 100000 - current["reserved"])','granted = 100')],
        'tests/test_async_settle_shadow_accounting.py::test_daily_cap_concurrent_instances'),
    ('catalog-hash-only',COMPARE,[('ctx.booking.amount == out.rebuilt_micro','True')],BASE+'test_catalog_explanation[4]'),
    ('shared-defect-oracle-removed',COMPARE,[('or out.legacy_frozen_micro not in (None, out.python_micro)','')],BASE+'test_two_evaluators_corrupt_rounding'),
    ('winner-polarity-removed',COMPARE,[('if ctx.booking.kind is not None and ctx.booking.kind != ctx.attempted_kind:', 'if False:')],BASE+'test_refund_and_winner_polarity[settled-refund]'),
    ('restart-clears-mismatch',REPORT,[('if resolved is None:\n            unresolved = True',
        'if resolved is None:\n            unresolved = False')],
        'tests/test_async_settle_shadow_accounting.py::test_positive_clock_requires_604800_seconds_and_complete_roster'),
    ('missing-coverage-clean',REPORT,[('start = candidates[0]["observed_at_us"] if candidates and not unresolved else None',
        'start = samples[0]["observed_at_us"] if samples else 0')],
        'tests/test_async_settle_shadow_accounting.py::test_report_cannot_start_from_empty_counter_or_null_samples'),
    ('eligible-coverage-removed',REPORT,[('if bucket["observed_eligible"] > accounted + counter["booking_pending"] + counter["booking_unknown"]:', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_report_reviewer_unaccounted_eligible_probe'),
    ('writer-interval-removed',REPORT,[('if (writer is None or not writer["started_at_us"] <= row["observed_at_us"] <= writer["flushed_at_us"]):', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_report_reviewer_counter_time_probe[after_close]'),
    ('rollover-close-removed','src/trusted_router/async_settle_shadow_evidence.py',[
        ('closed or day < day_at(self.clock())', 'closed')],
        'tests/test_async_settle_shadow_lifecycle.py::test_long_lived_writer_closes_seven_clean_days_before_new_writer'),
    ('transaction-attempt-fence-removed',STORAGE,[('if attempted:', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_sdk_abort_cannot_repeat_evidence_attempt[commit]'),
    ('flag-off-invalid-header-http','src/trusted_router/routes/internal/gateway.py',[
        ('    """Run one settlement off-loop behind the process-local per-key gate."""\n    require_internal_gateway(request, settings)',
         '    """Run one settlement off-loop behind the process-local per-key gate."""\n    require_internal_gateway(request, settings)\n    if not settings.async_settle_shadow_workspace_ids and request.headers.get("X-TR-Settlement-Shadow") == "!":\n        return {"data": {"review_mutant": True}}')],
        'tests/test_async_settle_shadow_http.py::test_flag_off_terminal_http_identity[invalid-settle]'),
]


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='f2b-mutations-') as directory:
        target = Path(directory)
        for name in ('src','tests','scripts'):
            shutil.copytree(ROOT/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache'))
        shutil.copy2(ROOT/'pyproject.toml',target/'pyproject.toml')
        for name,relative,edits,test in MUTATIONS:
            path = target/relative
            original = path.read_text()
            changed = original
            for before,after in edits:
                assert before in changed,(name,before)
                changed = changed.replace(before,after)
            path.write_text(changed)
            try:
                compile(changed,str(path),'exec')
                outcome = subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider','--tb=short',test],  # noqa: S603
                    cwd=target,capture_output=True,text=True,timeout=300,
                    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(target/'src')})
                log = Path(tempfile.gettempdir())/('f2b-mutation-'+name+'.log')
                log.write_text(outcome.stdout+outcome.stderr)
                killed = outcome.returncode == 1 and 'FAILED '+test.split('[')[0] in outcome.stdout and bool(re.search(r'\nE\s+(?:assert |AssertionError)', outcome.stdout))
                record = dict(mutation=name,killed=killed,exit_code=outcome.returncode,log=str(log))
                results.append(record)
                print(json.dumps(record),flush=True)
            finally:
                path.write_text(original)
    Path(tempfile.gettempdir(),'f2b-mutations.json').write_text(json.dumps(results,indent=2)+'\n')
    assert all(row['killed'] for row in results),results


if __name__ == '__main__':
    main()
