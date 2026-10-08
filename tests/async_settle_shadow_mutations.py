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
    ('skip-rollback-invalidation', REPORT, [
        ('if rollback is not None:', 'if False:')],
        'tests/test_async_settle_shadow_r13_rollback.py::test_rollback_invalidates_resolution'),
    ('rollback-detected-only-on-resolution-day', REPORT, [
        ('if right > since and revision not in safe',
         'if right > since and left < since + 86400_000000 and revision not in safe')],
        'tests/test_async_settle_shadow_r13_rollback.py::test_rollback_invalidates_resolution'),
    ('rollback-ignored-in-other-region', REPORT, [
        ('serving.append((body["started_at_us"], body["flushed_at_us"], body["router_revision"]))',
         'if body["region"] == "us-central1":\n                serving.append((body["started_at_us"], body["flushed_at_us"], body["router_revision"]))')],
        'tests/test_async_settle_shadow_r13_rollback.py::test_rollback_invalidates_resolution[False-other-counter-1]'),
    ('retry-exclusive-expiry-inclusive', 'src/trusted_router/async_settle_shadow_binding.py', [
        ('iat <= now < exp', 'iat <= now <= exp')],
        'tests/test_async_settle_shadow_r11_validity.py::test_signed_expiry_report_boundary[0]'),
    ('retry-issuance-truncation-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('iat = observed_at_us // 1000000', 'iat = observed_at_us / 1000000'),
        ('earliest_retry // 1000000', 'earliest_retry / 1000000')],
        'tests/test_async_settle_shadow_r11_validity.py::test_signed_expiry_report_boundary[500000]'),
    ('retry-receipt-order-required', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('earlier, later = sorted((original["observed_at_us"], retry["observed_at_us"]))',
         'earlier, later = original["observed_at_us"], retry["observed_at_us"]'),
        ('retry_identity(retry), later, later):', 'retry_identity(retry), later, later) or earlier > later:')],
        'tests/test_async_settle_shadow_r11_validity.py::test_same_signed_payload_delayed_observation'),
    ('lookback-sample-reset-filtered', REPORT, [
        ('if body["classification"] in {"hash", "identity", "normalization", "evaluator_disagreement"}:',
         'if body["authorization_day"] in requested and body["classification"] in {"hash", "identity", "normalization", "evaluator_disagreement"}:')],
        'tests/test_async_settle_shadow_r11_validity.py::test_lookback_mismatch_requires_resolution'),
    ('lookback-counter-reset-filtered', REPORT, [
        ('if body["last_mismatch_at_us"] is not None or body["conflicting_samples"]:',
         'if identity.split("/")[0] in requested and (body["last_mismatch_at_us"] is not None or body["conflicting_samples"]):')],
        'tests/test_async_settle_shadow_r11_validity.py::test_lookback_mismatch_requires_resolution'),
    ('retry-streamed-compatibility-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('original == retry', 'original._replace(streamed=retry.streamed) == retry')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_damage_original_class[other-stream-1-duplicate_samples-settle]'),
    ('retry-adapter-compatibility-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('original == retry', 'original._replace(adapter=retry.adapter) == retry')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_damage_original_class[other-adapter-1-duplicate_samples-settle]'),
    ('retry-route_type-compatibility-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('original == retry', 'original._replace(route_type=retry.route_type) == retry')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_damage_original_class[other-route-1-duplicate_samples-settle]'),
    ('retry-binding-validity-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('and binding_valid_at(iat, iat + LIFETIME, earliest_retry // 1000000)', '')],
        'tests/test_async_settle_shadow_r9_retry_plausibility.py::test_duplicate_cannot_outlive_binding'),
    ('retry-adapter-validity-removed', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('if not original_can_back_retry(', 'if False and not original_can_back_retry(')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_adapter_retry_original_class[expired-settle]'),
    ('retry-report-shared-predicate-bypassed', REPORT, [
        ('if position and original_can_back_retry(original, times[position - 1], retry,',
         'if position or original_can_back_retry(original, times[position - 1], retry,')],
        'tests/test_async_settle_shadow_r9_retry_plausibility.py::test_duplicate_cannot_change_signed_stream_dimension'),

    ('retry-null-hash-original-accepted', REPORT, [
        ('if retry_classification(row, row) != "duplicate":', 'if False:')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_damage_original_class[null-hash-1-duplicate_samples-settle]'),
    ('retry-null-hashes-equal', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('original["payload_hash"] is None or retry["payload_hash"] is None', 'False')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_requires_verified_hash_not_diagnostic_equality[both-null-settle]'),
    ('retry-differing-hash-accepted', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('or original["payload_hash"] != retry["payload_hash"]', '')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_adapter_retry_original_class[verified-different-settle]'),
    ('retry-diagnostic-change-conflicts', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('or original["payload_hash"] != retry["payload_hash"]',
         'or original["payload_hash"] != retry["payload_hash"] or original["classification"] != retry["classification"]')],
        'tests/test_async_settle_shadow_r8_retry_class.py::test_retry_requires_verified_hash_not_diagnostic_equality[diagnostic-change-settle]'),
    ('retry-original-reconciliation-removed', REPORT, [
        ('if not has_original:', 'if False:')],
        'tests/test_async_settle_shadow_r7_retry_original.py::test_last_day_duplicate_refund_requires_original'),
    ('sample-stream-from-legacy-body', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('ctx.body.route_type, comparison.verified_streamed)', 'ctx.body.route_type, ctx.body.streamed)')],
        'tests/test_async_settle_shadow_r7_refund_dimensions.py::test_actual_nonstream_refund_retains_signed_stream_dimensions[True-sample]'),
    ('counter-stream-from-legacy-body', RUNTIME, [
        ('verified_dims = (dims[0], dims[1], compared.verified_streamed)',
         'verified_dims = (dims[0], dims[1], capture.body.streamed)')],
        'tests/test_async_settle_shadow_r7_refund_dimensions.py::test_actual_nonstream_refund_retains_signed_stream_dimensions[True-counter]'),

    ('refund-placeholder-compared', COMPARE, [
        ('expected["streamed"] = claims.streamed', 'expected["streamed"] = body.streamed')],
        'tests/test_async_settle_shadow_r6_legacy_refund.py::test_legacy_refund_placeholder_is_not_authorize_stream_identity[False]'),
    ('retention-budget-guard-removed', STORAGE, [
        ('if retired_day(day, observed_at + dt.timedelta(seconds=WRITE_BUDGET_SECONDS)):', 'if False:')],
        'tests/test_async_settle_shadow_r6_retention_all.py::test_cleanup_can_finish_before_old_sample_mutation'),
    ('retention-fence-read-removed', STORAGE, [
        ('rows = self.query(tx, point_statement(CONTROL, RETENTION_FENCE), deadline)', 'rows = []')],
        'tests/test_async_settle_shadow_r6_retention_all.py::test_cleanup_fence_aborts_delayed_insert_even_after_empty_scan'),
    ('terminal-phase-accounting-removed', REPORT, [
        ('gap(identity+":"+phase+":phase_coverage_gap", day)', 'pass'),
        ('gap(identity+":"+phase+":sample_phase_gap", day)', 'pass')],
        'tests/test_async_settle_shadow_r6_counter_phase.py::test_refund_exclusions_require_refund_attempts'),
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
    ('eligible-coverage-removed',REPORT,[('if bucket["observed_eligible"] != accounted:', 'if False:')],
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
        'tests/test_async_settle_shadow_http.py::test_flag_off_terminal_http_identity[False--False-invalid-settle]'),
    ('flag-off-admission-on-bad-mode-http','src/trusted_router/routes/internal/gateway.py',[
        ('    subject = body.api_key_lookup_hash or body.api_key_hash',
         '    if settings.async_settle_enabled and not settings.async_settle_shadow_workspace_ids and request.headers.get("X-TR-Settlement-Mode") == "!":\n        return {"data": {"review_mutant": True}}\n    subject = body.api_key_lookup_hash or body.api_key_hash')],
        'tests/test_async_settle_shadow_http.py::test_flag_off_authorize_http_identity[False--True-True-bad]'),
    ('inline-snapshot-cap-removed','src/trusted_router/async_settle_shadow_wire.py',[
        ('if len(snapshot_bytes) > INLINE_BYTES:', 'if False:')], BASE+'test_inline_snapshot_boundary[6145]'),
    ('cold-cpu-gate-removed','scripts/async_settle/shadow_benchmark.py',[
        ("        assert records[-1]['cold_max_us'] <= 5000, f'cold shadow comparator exceeds 5 ms CPU budget: {records[-1]}'", '')],
        'tests/test_async_settle_shadow_lifecycle.py::test_cpu_budget_rejects_only_cold_tail'),
    ('persistence-reconciliation-removed',REPORT,[
        ('if persistence != counter["comparison_attempts"]:', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_report_reviewer_missing_persistence_outcomes[0]'),
    ('writer-coverage-union-removed',REPORT,[
        ('intervals.extend(merged)', 'intervals.append((start, end))'),
        ('gap(day+":writer_coverage_gap", day)', 'pass')],
        'tests/test_async_settle_shadow_accounting.py::test_report_reviewer_same_boot_uncovered_day'),
    ('hash-replay-capture-removed','src/trusted_router/routes/internal/gateway.py',[
        ('capture_prices(authorization, _select_authorized_endpoint(authorization, body),',
         '(lambda *args, **kwargs: None)(authorization, _select_authorized_endpoint(authorization, body),')],
        'tests/test_async_settle_shadow_integration.py::test_reviewer_hash_only_replay'),

    ('counter-capture-blocks-worker', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('acquired = self.lock.acquire(blocking=False)', 'acquired = self.lock.acquire(blocking=True)')],
        'tests/test_async_settle_shadow_counter_isolation.py::test_evidence_worker_counter_lock_does_not_hold_money[counter-True]'),
    ('rejected-endpoint-persisted', 'src/trusted_router/async_settle_shadow_evidence.py', [
        ('endpoint_id=ctx.selected_endpoint if (ctx.selected_endpoint and len(ctx.selected_endpoint) <= 128\n                    and ctx.selected_endpoint in (*auth.candidate_endpoint_ids, auth.endpoint_id)) else None,',
         'endpoint_id=ctx.body.selected_endpoint_id,')],
        'tests/test_async_settle_shadow_rejected_endpoint.py::test_rejected_replay_endpoint_is_not_persisted'),
    ('bidirectional-reconciliation-removed', REPORT, [
        ('if exclusions != bucket["observed_ineligible"]:', 'if False:'),
        ('if counter["comparison_attempts"] > verified:', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_report_reviewer_bidirectional_accounting'),
    ('comparison-ceiling-excludes-ineligible', REPORT, [
        ('bucket["observed_eligible"] + bucket["observed_ineligible"]', 'bucket["observed_eligible"]')],
        'tests/test_async_settle_shadow_real_exclusion.py::test_actual_verified_cohort_exclusion_does_not_break_clean_window'),
    ('real-exclusion-counter-unreconciled', REPORT, [
        ('if exclusions != bucket["observed_ineligible"]:', 'if False:')],
        'tests/test_async_settle_shadow_real_exclusion.py::test_real_exclusion_counter_mutation_blocks[missing_exclusion]'),
    ('expired-observation-persisted', RUNTIME, [
        ('if "proof_expired" in compared.reasons or retired:', 'if False:')],
        'tests/test_async_settle_shadow_expired_retention.py::test_expired_replay_cannot_repopulate_deleted_authorization_day'),
    ('loaded-signer-bad-header-http', 'src/trusted_router/routes/internal/gateway.py', [
        ('    subject = body.api_key_lookup_hash or body.api_key_hash',
         '    if request.app.state.async_settle.signer is not None and not settings.async_settle_shadow_workspace_ids and request.headers.get("X-TR-Settlement-Mode") == "!":\n        return {"data": {"review_mutant": True}}\n    subject = body.api_key_lookup_hash or body.api_key_hash')],
        'tests/test_async_settle_shadow_http.py::test_flag_off_authorize_http_identity[True--False-True-bad]'),
    ('comparison-outcomes-unreconciled', REPORT, [
        ('if outcomes_total != counter["comparison_attempts"]:', 'if False:')],
        'tests/test_async_settle_shadow_counter_false_pass.py::test_phantom_comparisons_have_an_explicit_gap'),
    ('exclusion-outcomes-unreconciled', REPORT, [
        ('if exclusions > bucket["unevaluable"]:', 'if False:')],
        'tests/test_async_settle_shadow_accounting.py::test_report_rejects_unverified_ineligible_zero_sample_writer'),
    ('sample-classifications-unreconciled', REPORT, [
        ('if durable > bucket[field]:', 'if False:')],
        'tests/test_async_settle_shadow_counter_false_pass.py::test_durable_samples_cannot_borrow_another_bucket_or_class[eligibility]'),
    ('write-boundary-retention-removed', STORAGE, [
        ('if retired_day(day, observed_at) or cutoff is not None and dt.date.fromisoformat(day) < cutoff:', 'if False:')],
        'tests/test_async_settle_shadow_retention_midnight.py::test_insert_rechecks_clock_after_point_read'),
    ('retention-counted-as-second-primary', RUNTIME, [
        ('if primary is not None:', 'if "proof_expired" in compared.reasons and primary != "proof_expired":\n                self.counters.reason(dims, capture.kind, "proof_expired", "rejections")\n            if primary is not None:')],
        'tests/test_async_settle_shadow_primary_rejection.py::test_one_primary_rejection_per_old_malformed_attempt'),
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
