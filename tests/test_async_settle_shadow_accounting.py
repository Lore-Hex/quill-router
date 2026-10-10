from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.async_settle.shadow_report import delta_bin, percentiles, report, validate_counter
from tests.test_async_settle_shadow import NOW, context, signer, wire
from trusted_router.async_settle_shadow_compare import compare
from trusted_router.async_settle_shadow_evidence import (
    CONTROL,
    COUNTER,
    SAMPLE,
    Counters,
    dimensions,
    sample,
)
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore, day_statement


class Database:
    """Serialize actual adapter callbacks; native tests own real abort evidence."""
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.trace = []
        self.transaction_options = []
        self.query_options = []
        self.write_shapes = []

    def run_in_transaction(self, callback, **kwargs):
        self.transaction_options.append(kwargs)
        with self.lock:
            before = copy.deepcopy(self.rows)
            try:
                return callback(self)
            except Exception:
                self.rows = before
                raise

    def execute_sql(self, sql, *, params, param_types, timeout, retry, request_options):
        self.query_options.append((timeout, retry, request_options))
        self.trace.append((sql, params, param_types))
        value = self.rows.get((params['kind'], params['id']))
        return [] if value is None else [(value,)]

    def insert_or_update(self, *, table, columns, values):
        self.write_shapes.append((table, columns))
        for kind, identity, body, _ in values:
            self.rows[kind,identity] = body


def test_daily_cap_concurrent_instances(shadow_deadline_clock):
    db = Database()
    db.rows[CONTROL,'2026-10-06/cap-v1'] = json.dumps(dict(v=1,limit=100000,reserved=99900,updated_at_us=0))
    def attempt(_):
        return EvidenceStore(db).reserve('2026-10-06', shadow_deadline_clock.monotonic()+1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt,range(2))) == [0,100]
    assert json.loads(db.rows[CONTROL,'2026-10-06/cap-v1'])['reserved'] == 100000
    assert attempt(0) == 0


def sample_row():
    ctx = context()
    return sample(ctx, compare(wire(),ctx,[signer().trusted]), observed_us=NOW*1000000,
        router_us=1, comparator_us=1, booking_us=1, instance='00000000-0000-0000-0000-000000000001', revision='a'*40)


def test_first_sample_wins_and_conflicts(shadow_deadline_clock):
    db, row = Database(), sample_row()
    store = EvidenceStore(db)
    identity = '2026-10-06/auth-v1'
    deadline = shadow_deadline_clock.monotonic()+1
    assert store.insert_sample(identity,row,deadline) == 'inserted'
    assert store.insert_sample(identity,row,deadline) == 'duplicate'
    row['payload_hash'] = '0'*64
    assert store.insert_sample(identity,row,deadline) == 'conflict'
    row['booking']['attempted_kind'] = 'refund'
    row['classification'] = 'requires_review'
    row['reason_codes'] = ['winner_polarity']
    row['booked_minus_frozen'] = row['booked_minus_rebuilt'] = None
    assert store.insert_sample(identity,row,deadline) == 'winner_polarity'
    assert json.loads(db.rows[SAMPLE,identity]) == sample_row()


def test_cumulative_flush_monotonic_and_partition(shadow_deadline_clock):
    db = Database()
    counters = Counters('us-central1','a'*40,clock=lambda:NOW)
    dims = dimensions('openai','responses',True)
    for field in ('settle_attempts','observed_attempts','observed_unknown'):
        counters.increment(dims,field)
    identity, first = counters.snapshot()[0]
    validate_counter(identity,first)
    second = copy.deepcopy(first)
    second['sequence'] += 1
    store = EvidenceStore(db)
    for row in (second,first,second):
        store.flush(identity,row,shadow_deadline_clock.monotonic()+1)
    assert json.loads(db.rows[COUNTER,identity]) == second
    broken = copy.deepcopy(first)
    broken['counts'][0]['observed_attempts'] += 1
    with pytest.raises(ValueError,match='partition'):
        validate_counter(identity,broken)


@pytest.mark.parametrize('rpc_latency', [.141, .163])
@pytest.mark.parametrize('commit_latency', [.163, .3, .6])
@pytest.mark.parametrize('budget', [1., .1, .3, .4])
def test_counter_flush_allows_multiple_bounded_regional_rpcs(shadow_deadline_clock, rpc_latency, commit_latency, budget):
    from types import SimpleNamespace

    from google.api_core.exceptions import DeadlineExceeded

    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines

    clock = shadow_deadline_clock
    calls = []

    def rpc(*, latency, **kwargs):
        calls.append(kwargs)
        timeout = kwargs['timeout']
        clock.now += min(timeout, latency)
        if timeout < latency:
            raise DeadlineExceeded('regional round trip exceeds remaining budget')
        return []

    class RegionalDatabase(Database):
        def __init__(self):
            super().__init__()
            self.spanner_api = SimpleNamespace(
                execute_sql=lambda **kw: rpc(latency=rpc_latency, **kw),
                commit=lambda **kw: rpc(latency=commit_latency, **kw))

        def execute_sql(self, *args, **kwargs):
            self.spanner_api.execute_sql(timeout=kwargs['timeout'], retry=kwargs['retry'])
            return super().execute_sql(*args, **kwargs)

        def run_in_transaction(self, callback, **kwargs):
            result = super().run_in_transaction(callback, **kwargs)
            self.spanner_api.commit(request_options=kwargs['commit_request_options'])
            return result

    db = RegionalDatabase()
    configure_spanner_rpc_deadlines(db)
    counters = Counters('europe-west4', 'a'*40, clock=lambda: NOW)
    counters.increment(dimensions('openai', 'responses', True), 'authorize_attempts')
    identity, row = counters.snapshot(closed=True)[0]
    started = clock.monotonic()
    if budget < 2*rpc_latency+commit_latency or commit_latency > .5:
        with pytest.raises(DeadlineExceeded):
            EvidenceStore(db).flush(identity, row, started+budget)
        assert clock.monotonic()-started == pytest.approx(min(budget, 2*rpc_latency+.5))
    else:
        EvidenceStore(db).flush(identity, row, started+budget)
        assert json.loads(db.rows[COUNTER, identity]) == row
        assert clock.monotonic()-started == pytest.approx(2*rpc_latency+commit_latency)
        assert len(calls) == 3
    assert all(0 < call['timeout'] <= .2 for call in calls[:2])
    if len(calls) == 3:
        assert 0 < calls[-1]['timeout'] <= .5
        assert calls[-1]['request_options'] == {'priority': 'PRIORITY_LOW'}


@pytest.mark.parametrize('failure_stage', ['begin', 'callback', 'commit'])
@pytest.mark.parametrize('error_name, reason', [
    ('DeadlineExceeded', 'deadline_exceeded'), ('Cancelled', 'cancelled'),
    ('Aborted', 'aborted'), ('ServiceUnavailable', 'unavailable'),
    ('TimeoutError', 'local_budget'), ('ValueError', 'other'),
])
def test_transaction_failure_diagnostics_are_bounded_and_redacted(
        shadow_deadline_clock, caplog, failure_stage, error_name, reason):
    from types import SimpleNamespace

    from google.api_core import exceptions

    clock = shadow_deadline_clock
    cls = {'TimeoutError': TimeoutError, 'ValueError': ValueError}.get(error_name) or getattr(exceptions, error_name)
    error = cls('private prompt, token, SQL body and customer identifiers')

    def fail():
        clock.now += .05
        raise error

    def callback(tx):
        if failure_stage == 'callback':
            fail()

    def run(cb, **kw):
        if failure_stage == 'begin':
            fail()
        cb(None)
        fail()

    store = EvidenceStore(SimpleNamespace(run_in_transaction=run))
    for _ in range(3):
        with pytest.raises(cls) as caught:
            store.transaction(callback, clock.monotonic()+1)
        assert caught.value is error
        clock.now += 5
    assert len(caplog.records) == 1
    clock.now += 60
    with pytest.raises(cls):
        store.transaction(callback, clock.monotonic()+1)
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert record.getMessage() == f'shadow evidence transaction failed stage={failure_stage} reason={reason} elapsed_ms=50'
        assert record.exc_info is None
        assert record.stack_info is None
    assert 'private' not in caplog.text


def test_day_reads_are_bounded_and_exact():
    sql, params, types = day_statement(SAMPLE,'2026-10-06','2026-10-06/auth',200)
    assert params == dict(kind=SAMPLE,day_start='2026-10-06/',next_day_start='2026-10-07/',after_id='2026-10-06/auth',page_size=200)
    assert 'LIKE' not in sql and 'JSON' not in sql and 'id>@after_id' in sql
    assert set(types) == set(params)
    for kwargs in ({'kind':'other'}, {'page_size':201}, {'after_id':'2026-10-05/x'}):
        with pytest.raises(ValueError):
            day_statement(**{'kind':SAMPLE,'day':'2026-10-06',**kwargs})


def test_report_cannot_start_from_empty_counter_or_null_samples():
    for rows in ([],[dict(kind=SAMPLE,id='2026-10-06/auth-v1',body=sample_row())]):
        result = report(rows,['2026-10-06'],{})
        assert result['status'] == 'BLOCKED' and result['clean_window_start_us'] is None
        assert {r['criterion']:r['status'] for r in result['exit_criteria']} == {
            'seven_days_admission_off':'BLOCKED','denominators_and_diagnostics':'BLOCKED',
            'zero_unexplained_all_evaluable':'BLOCKED','shared_fixtures':'BLOCKED',
            'frozen_pricing_crash_mutations':'BLOCKED','d2_d3_capacity_slo':'BLOCKED',
            'policy_rollback_status':'BLOCKED','rare_cases':'BLOCKED'}


def test_percentiles_retain_nulls_and_signed_deltas():
    assert percentiles([None,1,2,3,None]) == dict(count=3,null_count=2,p50=2,p95=3,p99=3)
    assert [delta_bin(v) for v in (-1001,-11,-1,0,1,10,1000)] == ['->1000','-11-100','-1','0','+1','+2-10','+101-1000']


def test_report_rejects_damaged_evidence():
    row = sample_row()
    row['python_micro'] = '2'
    with pytest.raises(ValueError):
        report([dict(kind=SAMPLE,id='2026-10-06/auth-v1',body=row)],['2026-10-06'],{})


def synthetic_window():
    import datetime as dt
    import hashlib

    from scripts.async_settle.shadow_report import EXTERNAL_GATES
    from trusted_router.async_settle_shadow_binding import FIXTURE_SHA256
    from trusted_router.detached_jws import canonical

    days = [(dt.date(2026,10,6)+dt.timedelta(days=n)).isoformat() for n in range(8)]
    proof = {gate:'a'*64 for gate in EXTERNAL_GATES}
    proof.update(fixture_sha256=FIXTURE_SHA256,instance_boot_ids_by_day={})
    rows = []
    for day in days:
        start = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp())
        counter = Counters('us-central1','a'*40,clock=lambda start=start:start)
        dims = dimensions('openai','chat.completions',False)
        counter._day()['admission_observer']['prediction_yes'] = 1
        if day == days[0]:
            for name in ('settle_attempts','envelope_present','observed_attempts','observed_eligible','evaluable','exact'):
                counter.increment(dims,name)
            counter.increment(dims,'samples_inserted')
            counter.increment(dims,'comparison_attempts')
            counter._day()['admission_observer']['prediction_yes'] = 1
        else:
            counter.increment(dims,'authorize_attempts',0)
        identity,body = counter.snapshot(closed=True)[0]
        body['flushed_at_us'] = (start+86400)*1000000
        rows.append(dict(kind=COUNTER,id=identity,body=body))
        proof['instance_boot_ids_by_day'][day] = [counter.instance]
        rows.append(dict(kind=CONTROL,id=day+'/manifest-v1',body=dict(v=1,day=day,
            instance_boot_ids=[counter.instance],router_revisions=['a'*40],go_revisions=[sample_row()['deployment']['go_revision']],
            configuration_sha256='c'*64,admission_disabled_from_us=start*1000000,
            admission_disabled_until_us=(start+86400)*1000000,first_evidence_at_us=None,
            completeness='complete',gap_intervals=[],proof_manifest_sha256=None)))
    digest = hashlib.sha256(canonical(proof)).hexdigest()
    for row in rows:
        if row['kind'] == CONTROL:
            row['body']['proof_manifest_sha256'] = digest
    value = sample_row()
    value['deployment']['instance'] = proof['instance_boot_ids_by_day'][days[0]][0]
    value['admission'].update(prediction='yes',reason='eligible',tier=2,pending_micro=0,cap_micro=25000000,
                              workspace_age_us=1000,health_age_us=1000,health_p95_us=0)
    rows.append(dict(kind=SAMPLE,id='2026-10-06/auth-v1',body=value))
    return rows,days,proof


def test_positive_clock_requires_604800_seconds_and_complete_roster():
    rows,days,proof = synthetic_window()
    result = report(rows,days,proof)
    assert result['status'] == 'PASS' and result['continuous_seconds'] == 691199
    assert result['clean_window_start_us'] == NOW*1000000
    # Seven UTC filenames are only 604799 seconds from the first positive sample.
    short = report(rows,days[:7],proof)
    assert short['status'] == 'BLOCKED' and short['continuous_seconds'] == 604799
    for damage in ('unclosed','mismatch','roster','counter_missing'):
        broken = copy.deepcopy(rows)
        if damage == 'unclosed':
            broken[0]['body']['closed'] = False
        elif damage == 'mismatch':
            broken[0]['body']['last_mismatch_at_us'] = NOW*1000000
        elif damage == 'roster':
            broken[1]['body']['instance_boot_ids'] = []
        else:
            broken.pop(0)
        damaged = report(broken,days,proof)
        assert damaged['status'] == 'BLOCKED' and damaged['clean_window_start_us'] is None


def test_report_restarts_only_after_restored_coverage_and_reviewed_fix():
    import hashlib

    from trusted_router.detached_jws import canonical
    rows,days,proof = synthetic_window()
    rows[0]['body']['last_mismatch_at_us'] = NOW*1000000
    later = copy.deepcopy(rows[-1])
    later['body']['observed_at_us'] = (NOW+86400)*1000000
    later['body']['authorization_id'] = 'auth-after-fix'
    later['body']['deployment']['instance'] = proof['instance_boot_ids_by_day'][days[1]][0]
    later['body']['deployment']['router_revision'] = 'b'*40
    later['id'] = later['body']['authorization_day']+'/auth-after-fix'
    rows.append(later)
    rows[2]['body']['samples_inserted'] = rows[2]['body']['comparison_attempts'] = 1
    rows[2]['body']['terminal_counts'] = copy.deepcopy(rows[0]['body']['terminal_counts'])
    rows[2]['body']['admission_observer']['prediction_yes'] = 1
    bucket = next(b for b in rows[2]['body']['counts'] if (b['adapter'], b['route_type'], b['streamed']) == ('openai', 'chat.completions', False))
    for field in ('settle_attempts', 'envelope_present', 'observed_attempts', 'observed_eligible', 'evaluable', 'exact'):
        bucket[field] = 1
    for row in rows:
        if row['kind'] == COUNTER and row['id'].split('/')[0] != days[0]:
            row['body']['router_revision'] = 'b'*40
        if row['kind'] == CONTROL and row['body']['day'] != days[0]:
            row['body']['router_revisions'] = ['b'*40]
    assert report(rows,days,proof)['clean_window_start_us'] is None
    proof['resolved_mismatches'] = [dict(at_us=NOW*1000000,revision='a'*40,fixed_revision='b'*40,
        serving_since_us=(NOW+86400)*1000000,artifact_sha256='d'*64)]
    digest = hashlib.sha256(canonical(proof)).hexdigest()
    for row in rows:
        if row['kind'] == CONTROL:
            row['body']['proof_manifest_sha256'] = digest
    result = report(rows,days,proof)
    assert result['clean_window_start_us'] == (NOW+86400)*1000000
    assert result['resets'] == [dict(at_us=NOW*1000000,revision='a'*40,reason='counter_mismatch',resolved_at_us=(NOW+86400)*1000000,fixed_revision='b'*40)]
    assert result['continuous_seconds'] == 604799 and result['status'] == 'BLOCKED'
    # Coverage gaps need a fully covered later day, never an in-memory clear.
    rows[0]['body']['first_gap_at_us'] = NOW*1000000
    assert report(rows,days,proof)['clean_window_start_us'] == (NOW+86400)*1000000


def test_report_prints_source_builds_and_usage_vectors():
    from tests.test_async_settle_shadow import FIXTURE
    rows, days, proof = synthetic_window()
    output = report(rows, days, proof)
    expected = dict(count=1,null_count=0,p50=1,p95=1,p99=1)
    assert (output['source_revisions'],output['usage']['raw_usage']['input_tokens'],output['usage']['legacy_usage']['total_prompt_tokens']) == (
        dict(router=['a'*40],go=[FIXTURE['go_revision']]),expected,expected)


def test_report_reviewer_unaccounted_eligible_probe():
    rows, days, proof = synthetic_window()
    bucket = next(b for b in rows[0]['body']['counts'] if b['exact'])
    for key in ('settle_attempts', 'observed_attempts', 'observed_eligible', 'envelope_present'):
        bucket[key] = 100
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert any(g.endswith(':eligible_coverage_gap') for g in result['gaps'])


@pytest.mark.parametrize('boundary', ['before_start', 'after_close'])
def test_report_reviewer_counter_time_probe(boundary):
    rows, days, proof = synthetic_window()
    for row in rows:
        if row['kind'] == COUNTER:
            if boundary == 'after_close':
                row['body']['flushed_at_us'] = row['body']['started_at_us']
            else:
                row['body']['started_at_us'] += 2_000_000
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert any(g.endswith(':writer_interval_gap') for g in result['gaps'])


def test_report_reconciles_samples_per_writer():
    rows, days, proof = synthetic_window()
    other = copy.deepcopy(rows[0])
    boot = '00000000-0000-0000-0000-000000000002'
    other['id'] = days[0] + '/' + boot
    other['body']['instance'] = boot
    other['body']['samples_inserted'] = 0
    rows.append(other)
    # Aggregate insert count remains correct, but the sample names the wrong writer.
    rows[-2]['body']['deployment']['instance'] = boot
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert sum(g.endswith(':sample_count_gap') for g in result['gaps']) == 2


@pytest.mark.parametrize('abort_at', ['callback', 'commit'])
def test_sdk_abort_cannot_repeat_evidence_attempt(monkeypatch, abort_at, shadow_deadline_clock):
    import contextlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import google.cloud.spanner_v1.session as sdk
    from google.api_core.exceptions import Aborted
    from google.rpc.error_details_pb2 import RetryInfo

    from trusted_router.storage_gcp_io import configure_spanner_rpc_deadlines

    session = MagicMock()
    session._database.log_commit_stats = False
    session.is_multiplexed = False
    cause = SimpleNamespace(trailing_metadata=lambda: (
        ('google.rpc.retryinfo-bin', RetryInfo().SerializeToString()),))
    error = Aborted('synthetic abort', errors=[cause])
    session.transaction.return_value.commit.side_effect = [error, None] if abort_at == 'commit' else None
    calls = []
    horizons = []
    class DB:
        spanner_api = SimpleNamespace()
        def run_in_transaction(self, callback, **kwargs):
            horizons.append(kwargs['timeout_secs'])
            return sdk.Session.run_in_transaction(session, callback, **kwargs)
    def callback(tx):
        calls.append(tx)
        if abort_at == 'callback':
            raise error
        return 'first'
    monkeypatch.setattr(sdk, 'trace_call', lambda *a, **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(sdk, 'MetricsCapture', lambda *a, **kw: contextlib.nullcontext())
    monkeypatch.setattr(sdk, 'add_span_event', lambda *a, **kw: None)
    db = DB()
    configure_spanner_rpc_deadlines(db)
    failure = None
    try:
        EvidenceStore(db).transaction(callback, shadow_deadline_clock.monotonic()+1)
    except Exception as exc:
        failure = exc
    assert isinstance(failure, RuntimeError) and str(failure) == 'shadow_transaction_retry'
    assert len(calls) == 1 and 0 < horizons[0] <= 1
    assert session.transaction.return_value.commit.call_count == int(abort_at == 'commit')


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_report_reviewer_missing_persistence_outcomes(delta):
    rows, days, proof = synthetic_window()
    counter = rows[0]['body']
    bucket = next(b for b in counter['counts'] if b['exact'])
    for name in ('settle_attempts', 'observed_attempts', 'observed_eligible',
                 'envelope_present', 'exact', 'evaluable'):
        bucket[name] = 100
    counter['comparison_attempts'] = 100
    terminal = counter['terminal_counts'][0]
    for field in ('observed_attempts', 'observed_eligible', 'envelope_present', 'exact', 'evaluable', 'comparison_attempts'):
        terminal[field] = 100
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert any(g.endswith(':persistence_count_gap') for g in result['gaps'])
    counter['duplicate_samples'] = 99 + delta
    terminal['duplicate_samples'] = 99 + delta
    result = report(rows, days, proof)
    assert (result['status'] == 'PASS') is (delta == 0)
    assert any(g.endswith(':persistence_count_gap') for g in result['gaps']) is (delta != 0)


def test_report_reviewer_same_boot_uncovered_day():
    import hashlib

    from trusted_router.detached_jws import canonical
    rows, days, proof = synthetic_window()
    rows[0]['body']['flushed_at_us'] = rows[-1]['body']['observed_at_us'] + 1
    boot = rows[0]['body']['instance']
    for day in days:
        proof['instance_boot_ids_by_day'][day] = [boot]
    for row in rows:
        if row['kind'] == COUNTER:
            row['body']['instance'] = boot
            row['id'] = row['id'].split('/')[0] + '/' + boot
        elif row['kind'] == CONTROL:
            row['body']['instance_boot_ids'] = [boot]
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED' and result['continuous_seconds'] == 0
    assert days[0]+':writer_coverage_gap' in result['gaps']


def test_same_verified_payload_retains_original_observation(shadow_deadline_clock):
    db, row = Database(), sample_row()
    store = EvidenceStore(db)
    identity, deadline = '2026-10-06/auth-v1', shadow_deadline_clock.monotonic()+1
    assert store.insert_sample(identity, row, deadline) == 'inserted'
    original = copy.deepcopy(row)
    from dataclasses import replace

    from tests.test_async_settle_shadow import FIXTURE
    value = copy.deepcopy(FIXTURE)
    del value['billing_snapshot']
    ctx = replace(context(), rebuild=None)
    failed = compare(wire(value), ctx, [signer().trusted])
    row = sample(ctx, failed, observed_us=NOW*1000000, router_us=1,
        comparator_us=1, booking_us=1, instance=original['deployment']['instance'], revision='a'*40)
    assert row['classification'] == 'unevaluable'
    assert row['reason_codes'] == ['snapshot_reconstruction_failed']
    assert store.insert_sample(identity, row, deadline) == 'duplicate'
    assert json.loads(db.rows[SAMPLE, identity]) == original


@pytest.mark.parametrize('overlap_us', [0, 1_000_000])
def test_writer_union_counts_overlapping_and_adjacent_intervals_once(overlap_us):
    import hashlib

    from trusted_router.detached_jws import canonical
    rows, days, proof = synthetic_window()
    first = rows[0]['body']
    second = copy.deepcopy(rows[0])
    boot = '00000000-0000-0000-0000-000000000002'
    split = first['started_at_us'] + 12*3600_000000
    first['flushed_at_us'] = split
    second['id'] = days[0]+'/'+boot
    second['body'].update(instance=boot, started_at_us=split-overlap_us,
                          samples_inserted=0, comparison_attempts=0, terminal_counts=[])
    for bucket in second['body']['counts']:
        for field in ('settle_attempts', 'envelope_present', 'observed_attempts', 'observed_eligible', 'evaluable', 'exact'):
            bucket[field] = 0
    rows.append(second)
    proof['instance_boot_ids_by_day'][days[0]].append(boot)
    proof['instance_boot_ids_by_day'][days[0]].sort()
    rows[1]['body']['instance_boot_ids'] = proof['instance_boot_ids_by_day'][days[0]]
    for row in rows:
        if row['kind'] == CONTROL:
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()
    result = report(rows, days, proof)
    assert result['status'] == 'PASS'
    assert result['continuous_seconds'] == 691199
    assert result['gaps'] == []



def test_writer_union_preserves_actual_midnight_flush_overlap():
    import hashlib

    from trusted_router.detached_jws import canonical
    rows, days, proof = synthetic_window()
    boot = rows[0]['body']['instance']
    rows[0]['body']['flushed_at_us'] += 5_000_000
    rows[2]['body']['started_at_us'] += 5_000_000
    rows[2]['body']['instance'] = boot
    rows[2]['id'] = days[1]+'/'+boot
    proof['instance_boot_ids_by_day'][days[1]] = [boot]
    rows[3]['body']['instance_boot_ids'] = [boot]
    for row in rows:
        if row['kind'] == CONTROL:
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()
    result = report(rows, days, proof)
    assert result['status'] == 'PASS' and result['continuous_seconds'] == 691199
    assert result['gaps'] == []


@pytest.mark.parametrize('probe', ['ineligible', 'comparisons', 'partition', 'exclusion_surplus', 'wrong_bucket', 'outcome_surplus'])
def test_report_reviewer_bidirectional_accounting(probe):
    rows, days, proof = synthetic_window()
    counter = rows[2]['body']
    bucket = counter['counts'][0]
    if probe == 'comparisons':
        counter = rows[0]['body']
        counter.update(comparison_attempts=100, duplicate_samples=99)
        suffix = ':comparison_observation_gap'
    elif probe == 'partition':
        bucket.update(settle_attempts=99, observed_attempts=99)
        suffix = ':observed_partition_gap'
    elif probe == 'outcome_surplus':
        bucket['unevaluable'] = 99
        suffix = ':outcome_observation_gap'
    else:
        bucket.update(settle_attempts=99, observed_attempts=99, observed_ineligible=99)
        if probe != 'ineligible':
            counter['exclusions'] = [dict(phase='settle', adapter=bucket['adapter'],
                route_type='responses' if probe == 'wrong_bucket' else bucket['route_type'],
                streamed=bucket['streamed'], reason='service_tier', count=100 if probe == 'exclusion_surplus' else 99)]
        suffix = ':ineligible_coverage_gap'
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert any(g.endswith(suffix) for g in result['gaps'])


def test_report_rejects_unverified_ineligible_zero_sample_writer():
    rows, days, proof = synthetic_window()
    counter = rows[2]['body']
    bucket = counter['counts'][0]
    bucket.update(settle_attempts=99, observed_attempts=99, observed_ineligible=99)
    counter['exclusions'] = [dict(phase='settle', reason='service_tier', count=99,
        **{key: bucket[key] for key in ('adapter', 'route_type', 'streamed')})]
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED' and any(g.endswith(':exclusion_outcome_gap') for g in result['gaps'])


def test_evidence_storage_call_contract(shadow_deadline_clock):
    db = Database()
    granted = EvidenceStore(db).reserve('2026-10-06', shadow_deadline_clock.monotonic()+1)
    assert granted == 100
    assert db.transaction_options == [dict(timeout_secs=1, commit_request_options={'priority':'PRIORITY_LOW'})]
    assert len(db.query_options) == 2
    assert [params['id'] for _, params, _ in db.trace] == ['2026-10-06/cap-v1', 'retention-v1']
    for timeout, retry, options in db.query_options:
        assert 0 < timeout <= .2 and retry is None and options == {'priority':'PRIORITY_LOW'}
    assert db.write_shapes == [('tr_entities', ('kind', 'id', 'body', 'updated_at'))]


@pytest.mark.parametrize('field', ['late_installs', 'max_consecutive_failures', 'degraded_seconds'])
def test_observer_counter_requires_new_fields(field):
    counters = Counters('us-central1', 'a'*40, clock=lambda: NOW)
    counters._day()
    identity, body = counters.snapshot()[0]
    assert body['admission_observer'][field] == 0
    validate_counter(identity, body)
    del body['admission_observer'][field]
    with pytest.raises(ValueError, match='observer schema'):
        validate_counter(identity, body)


@pytest.mark.parametrize('value', [-1, .5, float('nan'), float('inf'), True, '1'])
def test_observer_degraded_seconds_rejects_invalid_numbers(value):
    counters = Counters('us-central1', 'a'*40, clock=lambda: NOW)
    counters._day()
    identity, body = counters.snapshot()[0]
    body['admission_observer']['degraded_seconds'] = value
    with pytest.raises(ValueError):
        validate_counter(identity, body)


@pytest.mark.parametrize('damage', [None, 'failures', 'streak', 'unknown', 'degraded', 'no_known'])
def test_report_bounded_observer_failures(damage):
    rows, days, proof = synthetic_window()
    obs = rows[0]['body']['admission_observer']
    # At 100 reads, the three-event floor must win over the 1% allowance.
    obs.update(health_reads=40, workspace_reads=60, read_failures=1, missed_ticks=1,
               late_installs=1, max_consecutive_failures=2, prediction_yes=98,
               prediction_unknown=2, degraded_seconds=864)
    if damage == 'failures':
        obs['late_installs'] += 1
    elif damage == 'streak':
        obs['max_consecutive_failures'] = 3
    elif damage == 'unknown':
        obs['prediction_unknown'] += 1
    elif damage == 'degraded':
        obs['degraded_seconds'] += 1
    elif damage == 'no_known':
        obs.update(prediction_yes=0, prediction_unknown=0)
    result = report(rows, days, proof)
    assert result['status'] == ('PASS' if damage is None else 'BLOCKED')
    budget = result['metrics']['observer_budgets'][rows[0]['id']]
    assert budget['failure_limit'] == 3 and budget['failure_ratio'] >= .01


def test_unknown_admission_sample_is_not_clock_seed_or_mismatch_but_can_be_tolerated():
    from scripts.async_settle.shadow_report import positive_sample
    from trusted_router.services.async_settle_shadow_admission import unknown
    rows, days, proof = synthetic_window()
    original = rows[-1]
    tail = copy.deepcopy(original)
    tail['id'] += '-tail'
    tail['body']['authorization_id'] += '-tail'
    tail['body']['admission'] = unknown('cache_stale')
    rows.append(tail)
    body = rows[0]['body']
    body['samples_inserted'] = body['comparison_attempts'] = 2
    for bucket in (*body['counts'], *body['terminal_counts']):
        for key, value in bucket.items():
            if type(value) is int:
                bucket[key] *= 2
    body['admission_observer'].update(prediction_yes=49, prediction_unknown=1)
    assert not positive_sample(tail['body'])
    result = report(rows, days, proof)
    assert result['status'] == 'PASS' and result['resets'] == []
    assert result['clean_window_start_us'] == NOW*1000000
    body['admission_observer']['prediction_unknown'] = 0
    assert any('prediction_count_gap' in gap for gap in report(rows, days, proof)['gaps'])
    tail['body']['python_usage']['output_tokens'] += 1
    body['admission_observer']['prediction_unknown'] = 1
    with pytest.raises(ValueError, match='contradictory clean sample'):
        report(rows, days, proof)


# Adopted from the independent Round 1 review contract tests.
def test_unknown_admission_is_not_reported_as_exact_or_evaluable():
    from trusted_router.services.async_settle_shadow_admission import unknown

    rows, days, proof = synthetic_window()
    tail = copy.deepcopy(rows[-1])
    tail['id'] += '-tail'
    tail['body']['authorization_id'] += '-tail'
    tail['body']['admission'] = unknown('cache_stale')
    rows.append(tail)
    body = rows[0]['body']
    body['samples_inserted'] = body['comparison_attempts'] = 2
    for bucket in (*body['counts'], *body['terminal_counts']):
        for key, value in bucket.items():
            if type(value) is int:
                bucket[key] *= 2
    body['admission_observer'].update(prediction_yes=49, prediction_unknown=1)
    result = report(rows, days, proof)
    assert result['status'] == 'PASS'
    assert result['metrics']['classification']['exact'] == 1
    assert sum(row['evaluable'] for row in result['denominators']) == 1
    assert sum(row['exact'] for row in result['denominators']) == 1
    assert sum(row['unevaluable'] for row in result['denominators']) == 1
    assert result['metrics']['classification']['unevaluable'] == 1
    assert result['metrics']['comparator_classification']['exact'] == 2


def test_unknown_admission_keeps_independent_comparator_disagreement_gate():
    from trusted_router.services.async_settle_shadow_admission import unknown

    rows, days, proof = synthetic_window()
    row = rows[-1]['body']
    row['admission'] = unknown('cache_stale')
    row['classification'] = 'evaluator_disagreement'
    row['go_micro'] = row['python_micro'] + 1
    row['python_minus_go'] = -1
    row['reason_codes'] = ['go_failure']
    body = rows[0]['body']
    body['last_mismatch_at_us'] = row['observed_at_us']
    body['admission_observer'].update(prediction_yes=49, prediction_unknown=1)
    for bucket in (*body['counts'], *body['terminal_counts']):
        if bucket['exact']:
            bucket.update(exact=0, evaluable=0, mismatch=1)
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert result['clean_window_start_us'] is None
    assert any(reset['reason'] == 'evaluator_disagreement' for reset in result['resets'])
    assert result['metrics']['classification'] == {'unevaluable': 1}
    assert result['metrics']['comparator_classification'] == {'evaluator_disagreement': 1}
    assert sum(row['mismatch'] for row in result['denominators']) == 0
    assert sum(row['unevaluable'] for row in result['denominators']) == 1
