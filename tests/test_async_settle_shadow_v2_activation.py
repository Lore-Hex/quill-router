"""P6 release commands with recorded JSON and fake process/HTTP boundaries."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.async_settle import shadow_v2_activation as cli
from scripts.async_settle.shadow_v2_window import main as window_main
from trusted_router.async_settle_shadow_v2_activation_plan import Archive, Plan, Source, load
from trusted_router.async_settle_shadow_v2_activation_release import (
    MONEY_OFF,
    patch,
    pins,
    workflow_record,
)
from trusted_router.async_settle_shadow_v2_collect_ports import Files, Pass, Query
from trusted_router.async_settle_shadow_v2_native_artifacts import capture_archive
from trusted_router.async_settle_shadow_v2_native_reads import NativeClient, ReadBudget, Response

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'release/first-window-plan.json'


@pytest.fixture
def plan() -> Plan:
    return load(EXAMPLE)


class Recorded:
    def __init__(self, *values: Any):
        self.values = list(values)
        self.calls: list[Any] = []

    def read(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(json.dumps(value).encode(), 200, {}, 10)


def source(plan, kind='monitoring', **updates):
    value = plan.sources[0].model_dump()
    value.update(updates, kind=kind)
    if kind.startswith('github_'):
        value.update(parser='github-v3', url=f'https://api.github.com/repos/{plan.releases.router.repository}/actions/runs/5')
        value.update(updates)
    return Source.model_validate(value)


def native(plan, tmp_path, port, selected=None, budget=None):
    selected = selected or plan.sources[0]
    return NativeClient(plan, selected, port, Files(tmp_path), budget=budget)


def query(selected, size=100, seconds=10):
    return Query(selected.id, selected.resource, selected.day, size, seconds)


def run_record(plan, workflow=None):
    workflow = workflow or plan.workflows[0]
    release = getattr(plan.releases, workflow.role)
    return dict(id=workflow.run_id, head_sha=release.sha, status='completed', conclusion='success',
        event='push', head_branch='main', path='.github/workflows/' + workflow.workflow,
        repository=dict(full_name=release.repository), updated_at='2026-11-01T00:00:00Z')


def job_record(plan, workflow=None):
    workflow = workflow or plan.workflows[0]
    return dict(name=workflow.jobs[0], head_sha=getattr(plan.releases, workflow.role).sha,
        run_id=workflow.run_id, status='completed', conclusion='success', completed_at='2026-11-01T00:00:00Z')


def test_closed_schema_and_example(plan):
    schema = json.loads((ROOT / 'release/first-window-plan.schema.json').read_bytes())
    assert schema == Plan.model_json_schema()
    assert schema['additionalProperties'] is False
    assert all(v.get('additionalProperties') is False for v in schema['$defs'].values() if v.get('type') == 'object')
    assert len(plan.schedule.launches) == 532
    assert len(plan.passes) == 13
    assert plan.review == 'REVIEW'


@pytest.mark.parametrize('section', ['', 'releases.router', 'schedule', 'limits', 'probe', 'trial', 'artifacts', 'sources.0'])
def test_unknown_keys_refused(plan, section):
    value = plan.model_dump()
    target = value
    for part in filter(None, section.split('.')):
        target = target[int(part)] if part.isdigit() else target[part]
    target['unknown'] = True
    with pytest.raises(ValueError):
        Plan.model_validate(value)


@pytest.mark.parametrize(('field', 'bad'), [('records_per_page', 101), ('records_per_pass', 10001), ('calls_per_pass', 10001), ('call_seconds', 11), ('retries', 1), ('bytes_per_page', 262145), ('trial_seconds', 3601)])
def test_plan_bounds(plan, field, bad):
    value = plan.model_dump()
    value['limits'][field] = bad
    with pytest.raises(ValueError):
        Plan.model_validate(value)


@pytest.mark.parametrize(('section', 'field', 'bad'), [
    ('schedule', 'workspace_count', True), ('schedule', 'workspace_count', 1.0),
    ('limits', 'retries', False), ('limits', 'retries', 0.0),
    ('probe', 'warmup_minutes', 4.0), ('probe', 'window_minutes', True),
])
def test_plan_numeric_literals_are_strict(plan, section, field, bad):
    value = plan.model_dump()
    value[section][field] = bad
    with pytest.raises(ValueError, match='integer literal'):
        Plan.model_validate(value)


@pytest.mark.parametrize('change', ['workspace', 'coercion', 'secret', 'path', 'window', 'horizon', 'fleet', 'k', 'overlap'])
def test_plan_semantics(plan, change):
    value = plan.model_dump()
    if change == 'workspace':
        value['workspace'] = 'another'
    elif change == 'coercion':
        value['limits']['records_per_page'] = '100'
    elif change == 'secret':
        value['probe']['secret_reference'] = 'not-a-resource-reference'  # noqa: S105 - invalid reference test
    elif change == 'path':
        value['artifacts']['root'] = '../elsewhere'
    elif change == 'window':
        value['schedule']['window']['end'] = '2026-11-03T00:00:00Z'
    elif change == 'horizon':
        value['schedule']['horizon_end'] = '2026-11-09T03:00:00Z'
    elif change == 'fleet':
        value['schedule']['fleet_cap'] = 1
    elif change == 'k':
        value['schedule']['k'] = 1
    else:
        extra = dict(value['schedule']['launches'][0], id='extra')
        value['schedule']['launches'].append(extra)
    with pytest.raises(ValueError):
        Plan.model_validate(value)


def test_duplicate_keys_and_review(plan, tmp_path):
    path = tmp_path / 'plan.json'
    path.write_text(EXAMPLE.read_text().replace('"version":', '"review":"REVIEW","version":', 1))
    with pytest.raises(ValueError, match='duplicate'):
        load(path)
    with pytest.raises(ValueError, match='review'):
        load(EXAMPLE, approved_sha256=hashlib.sha256(EXAMPLE.read_bytes()).hexdigest())
    value = plan.model_dump()
    value['review'] = 'approved'
    value['reviewer'] = 'Joseph'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='release SHA'):
        load(path, approved_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('phase', ['shadow-opt-in', 'shadow-opt-out'])
def test_exact_phase_pins(plan, phase):
    expected = pins(plan, phase)
    assert {k: expected['router'][k] for k in MONEY_OFF} == {
        'TR_ASYNC_SETTLE_ENABLED': 'false', 'TR_ASYNC_SETTLE_PROTECTION': 'false',
        'TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED': 'false'}
    assert expected['enclave']['TR_ASYNC_SETTLE_NEGOTIATE'] == 'off'
    assert expected['router']['TR_ASYNC_SETTLE_SHADOW_ATTACHMENT_BUCKET'] == plan.attachment_bucket
    assert expected['enclave']['TR_ASYNC_SETTLE_SHADOW'] == ('on' if phase == 'shadow-opt-in' else 'off')
    assert expected['router']['TR_ASYNC_SETTLE_SHADOW_WORKSPACES'] == (plan.workspace if phase == 'shadow-opt-in' else '')
    original = (ROOT / 'scripts/deploy/rollout.sh').read_text()
    diff = patch(original, plan.releases.router.pin_file, expected['router'])
    assert '--- a/scripts/deploy/rollout.sh' in diff
    assert '+  "TR_ASYNC_SETTLE_ENABLED=true"' not in diff
    assert (ROOT / 'scripts/deploy/rollout.sh').read_text() == original


def test_bad_source_money_pin(plan):
    original = (ROOT / 'scripts/deploy/rollout.sh').read_text().replace('TR_ASYNC_SETTLE_ENABLED=false', 'TR_ASYNC_SETTLE_ENABLED=true')
    with pytest.raises(ValueError, match='money-off'):
        patch(original, 'rollout.sh', pins(plan, 'shadow-opt-in')['router'])


def test_release_cli_records_without_dispatch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'command', lambda args: calls.append(args))
    out = tmp_path / 'release'
    assert window_main(['release', '--plan', str(EXAMPLE), '--phase', 'shadow-opt-in', '--out', str(out)]) == 1
    assert (out / 'router.patch').is_file()
    assert 'gh pr create' in (out / 'operator-commands.txt').read_text()
    result = json.loads((out / 'result.json').read_text())
    assert 'P6EnclavePinSource' in result['missing']
    assert calls == []
    assert window_main(['release', '--plan', str(EXAMPLE), '--phase', 'shadow-opt-in', '--out', str(out)]) == 1


@pytest.mark.parametrize(('service', 'code', 'absent'), [
    ('scheduler', 'NOT_FOUND', True), ('scheduler', 'PERMISSION_DENIED', False),
    ('run', 'NOT_FOUND', False),
])
def test_job_describe_process_response(monkeypatch, service, code, absent):
    from types import SimpleNamespace
    response = SimpleNamespace(returncode=1, stdout=b'',
        stderr=f'ERROR: (gcloud.{service}.jobs.describe) {code}: selected job'.encode())
    monkeypatch.setattr(cli, 'subprocess', SimpleNamespace(run=lambda *args, **kwargs: response))
    argv = ['gcloud', service, 'jobs', 'describe', 'selected-job', '--format=json']
    if absent:
        assert cli.command(argv) == {'absent': True}
    else:
        with pytest.raises(ValueError, match='operator command failed'):
            cli.command(argv)


def test_probe_dry_run_never_executes(plan):
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    plan.schedule.warmup.start = (now - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    plan.schedule.warmup.end = (now + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    def forbidden(_):
        pytest.fail('dry run executed a command')
    result = cli.probe(plan, 'warmup', False, forbidden)
    assert not result['applied']
    for operation in result['commands']:
        assert all('--format=json' in argv for argv in operation.values())
        assert 'create' in operation['create'] and '--oauth-service-account-email' in operation['create']
        assert '--max-retry-attempts=0' in operation['create']
        assert 'run' not in operation['create']


@pytest.mark.parametrize('state', ['PAUSED', 'ENABLED', 'absent'])
def test_stop_is_idempotent(plan, state):
    plan.probe.max_requests = 1  # A start allowance never blocks pausing.
    calls = []
    def runner(args):
        calls.append(args)
        if state == 'absent':
            return dict(absent=True)
        if 'pause' in args:
            return {}
        return dict(state='PAUSED' if len(calls) > 1 or state == 'PAUSED' else 'ENABLED')
    result = cli.probe(plan, 'stop', True, runner)
    assert result['complete'] and result['applied']
    assert not any('create' in x or 'update' in x for x in calls)
    assert all('--format=json' in argv for argv in calls)


@pytest.mark.parametrize('field', ['head_sha', 'status', 'conclusion', 'event', 'head_branch', 'path'])
def test_workflow_gates(plan, field):
    value = run_record(plan)
    workflow_record(plan, 'router', plan.workflows[0].workflow, value, [job_record(plan)], plan.workflows[0].jobs)
    value[field] = 'wrong'
    with pytest.raises(ValueError):
        workflow_record(plan, 'router', plan.workflows[0].workflow, value, [job_record(plan)], plan.workflows[0].jobs)


def test_attestation_never_skipped(plan, tmp_path):
    values = []
    for workflow in plan.workflows:
        values.extend([run_record(plan, workflow), dict(total_count=1, jobs=[job_record(plan, workflow)])])
    port = Recorded(*values)
    result = cli.observe_release(plan, 'shadow-opt-in', tmp_path, port)
    assert result['complete'] is False
    assert 'P6AttestedRegionVerification' in result['missing']
    assert all(call[0] == 'GET' for call in port.calls)


def test_wait_pending_bounded(plan, tmp_path):
    values = [dict(run_record(plan, w), status='queued') for w in plan.workflows]
    port = Recorded(*values)
    result = cli.observe_release(plan, 'shadow-opt-in', tmp_path, port)
    assert result['missing'] == ['ExistingWorkflowPending']
    assert len(port.calls) == len(plan.workflows)


def test_monitoring_pages_keep_originals(plan, tmp_path):
    point = dict(interval=dict(endTime='2026-11-01T00:01:00Z'), value=dict(doubleValue=0.1))
    original = dict(timeSeries=[dict(metric={'type': 'cpu'}, resource={'type': 'spanner'}, points=[point])], nextPageToken='next')
    port = Recorded(original, {})
    client = native(plan, tmp_path, port)
    first = client.read(query(plan.sources[0]), None)
    assert not first.eof and first.next_token == 'next'  # noqa: S105 - page cursor
    last = client.read(query(plan.sources[0]), first.next_token)
    assert last.eof and last.complete_through_us is None
    assert json.loads((tmp_path / client.originals[0]['original']['path']).read_bytes()) == original
    assert port.calls[0][2]['params']['pageSize'] == 100


@pytest.mark.parametrize('issue', ['page', 'deadline', 'bytes', 'status', 'timeout', 'records', 'partial_page', 'calls', 'cycle'])
def test_native_bounds(plan, tmp_path, issue):
    port = Recorded({})
    client = native(plan, tmp_path, port)
    q = query(plan.sources[0])
    if issue == 'page':
        q = replace(q, page_size=101)
    elif issue == 'deadline':
        port.values = [Response(b'{}', 200, {}, 10000001)]
    elif issue == 'bytes':
        port.values = [Response(b' ' * 262145, 200, {}, 1)]
    elif issue == 'status':
        port.values = [Response(b'{}', 503, {}, 1)]
    elif issue == 'timeout':
        port.values = [TimeoutError()]
    elif issue == 'records':
        client.budget.records = 10000
    elif issue == 'partial_page':
        client.budget.records = 9951
    elif issue == 'calls':
        client.budget.calls = plan.limits.calls_per_pass
    elif issue == 'cycle':
        client._tokens.add(None)
    with pytest.raises((ValueError, TimeoutError)):
        client.read(q, None)
    assert len(port.calls) <= 1
    if issue in ('records', 'partial_page'):
        assert not port.calls


def test_failed_read_retained_no_retry(plan, tmp_path):
    port = Recorded(TimeoutError())
    client = native(plan, tmp_path, port)
    run = Pass(Files(tmp_path))
    assert list(run.scan(client, query(plan.sources[0]))) == []
    assert len(port.calls) == 1 and run.missing
    assert list(tmp_path.rglob('*failure.json'))


def test_shared_native_pass_budget(plan, tmp_path):
    budget = ReadBudget(calls=plan.limits.calls_per_pass - 1)
    port = Recorded({})
    native(plan, tmp_path / 'a', port, budget=budget).read(query(plan.sources[0]), None)
    with pytest.raises(ValueError, match='bounds'):
        native(plan, tmp_path / 'b', port, budget=budget).read(query(plan.sources[0]), None)
    assert len(port.calls) == 1


def test_ci_metadata_full_revision(plan, tmp_path):
    selected = source(plan, 'github_run')
    good = dict(run_record(plan), id=5)
    client = native(plan, tmp_path, Recorded(good), selected)
    assert client.read(query(selected), None).eof
    bad = dict(good, head_sha='a' * 40)
    with pytest.raises(ValueError, match='SHA'):
        native(plan, tmp_path / 'bad', Recorded(bad), selected).read(query(selected), None)


def test_lifecycle_read_only_post(plan, tmp_path):
    selected = source(plan, 'lifecycle', parser='logging-v2', url='https://logging.googleapis.com/v2/entries:list',
        filter='logName="projects/p/logs/platform" AND resource.labels.service_name="trusted-router"')
    port = Recorded(dict(entries=[]))
    result = native(plan, tmp_path, port, selected).read(query(selected), None)
    assert result.eof and result.complete_through_us is None
    assert port.calls[0][0] == 'POST'
    assert 'timestamp<' in port.calls[0][2]['body']['filter']


def test_original_archive_is_bound_to_run_and_sha(plan, tmp_path):
    raw = b'recorded original archive bytes'
    sha = hashlib.sha256(raw).hexdigest()
    spec = Archive(role='router', run_id=5, artifact_id=7, sha256=sha, max_bytes=100, out='original.zip')
    metadata = dict(id=7, workflow_run=dict(id=5, head_sha=plan.releases.router.sha), expired=False,
                    digest='sha256:' + sha, size_in_bytes=len(raw))
    class Download:
        def download(self, url, target, **kwargs):
            assert url.endswith('/7/zip')
            target.write_bytes(raw)
            return dict(sha256=sha, bytes=len(raw), calls=2, elapsed_us=50)
    budget = ReadBudget()
    capture_archive(plan, spec, Files(tmp_path), Recorded(metadata), Download(), budget)
    assert (tmp_path / 'original.zip').read_bytes() == raw
    assert budget.calls == 3 and budget.records == 1
    metadata['workflow_run']['head_sha'] = 'b' * 40
    with pytest.raises(ValueError, match='release'):
        capture_archive(plan, spec, Files(tmp_path / 'wrong'), Recorded(metadata), Download(), ReadBudget())


def job_description(plan, minutes=4):
    return {'spec': {'template': {'spec': {'taskCount': 1, 'parallelism': 1, 'template': {'spec': dict(
        serviceAccountName=plan.probe.service_account, maxRetries=0,
        containers=[dict(image=plan.releases.router.image, command=['/app/.venv/bin/python'],
            args=['-m', 'trusted_router.synthetic.cli', '--async-settle-shadow-probe'],
            env=[dict(name='TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE', value='/var/run/secrets/shadow-probe/api-key'),
                 dict(name='TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS', value='1'),
                 dict(name='TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES', value=str(minutes))],
            volumeMounts=[dict(name='key', mountPath='/var/run/secrets/shadow-probe')])],
        volumes=[dict(name='key', secret=dict(secretName=plan.probe.secret_reference.rsplit('/', 1)[-1]))])}}}}}


@pytest.mark.parametrize('existing', [False, True])
def test_probe_apply_existing_job_only(plan, existing):
    from datetime import UTC, datetime, timedelta
    current = datetime.now(UTC)
    plan.schedule.warmup.start = (current - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    plan.schedule.warmup.end = (current + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    calls = []
    applied = set()
    def runner(args):
        calls.append(args)
        if args[3:5] == ['run', 'jobs']:
            return job_description(plan)
        name = next(x for x in args if x.endswith('-scheduled'))
        if 'create' in args or 'update' in args or 'resume' in args:
            applied.add(name)
            return {}
        if name not in applied:
            return dict(state='PAUSED') if existing else dict(absent=True)
        job = next(j for j in plan.probe.jobs if j.scheduler == name)
        return dict(state='ENABLED', schedule='*/4 * * * *', timeZone='UTC',
            httpTarget=dict(uri=f'https://{job.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{plan.probe.project}/jobs/{job.name}:run', httpMethod='POST', body='e30=', oauthToken=dict(serviceAccountEmail=plan.probe.service_account)), retryConfig={})
    result = cli.probe(plan, 'warmup', True, runner)
    assert result['complete'] and result['applied']
    assert len(applied) == 2
    assert any(('update' if existing else 'create') in args for args in calls)
    assert all('execute' not in args and 'deploy' not in args for args in calls)
    assert result['stop_required_at'] == plan.schedule.warmup.end


@pytest.mark.parametrize('change', ['image', 'account', 'cadence', 'reference', 'taskCount', 'parallelism'])
def test_probe_wrong_job_stops_before_write(plan, change):
    value = job_description(plan)
    spec = value['spec']['template']['spec']['template']['spec']
    if change == 'image':
        spec['containers'][0]['image'] = plan.releases.router.image.split('@')[0] + ':' + plan.releases.router.sha[:7]
    elif change == 'account':
        spec['serviceAccountName'] = 'different'
    elif change == 'cadence':
        spec['containers'][0]['env'][2]['value'] = '1'
    elif change == 'reference':
        spec['volumes'][0]['secret']['secretName'] = 'different'
    else:
        value['spec']['template']['spec'][change] = 2
    with pytest.raises(cli.P6ProbeImageBinding if change == 'image' else ValueError):
        cli.check_job(plan, value, 4)


def test_enclave_patch_requires_exact_source(plan, tmp_path):
    from trusted_router.async_settle_shadow_v2_activation_release import prepare
    enclave = tmp_path / 'enclave'
    target = enclave / plan.releases.enclave.pin_file
    target.parent.mkdir(parents=True)
    target.write_text('ENV=(\n "TR_ASYNC_SETTLE_SHADOW=off"\n "TR_ASYNC_SETTLE_NEGOTIATE=off"\n)\n')
    original = target.read_bytes()
    out = tmp_path / 'out'
    out.mkdir()
    result = prepare(plan, 'shadow-opt-in', ROOT, enclave, out)
    assert not result['missing']
    assert '+ "TR_ASYNC_SETTLE_SHADOW=on"' in (out / 'enclave.patch').read_text()
    assert target.read_bytes() == original


def test_enclave_native_gcp_metadata_patch(plan):
    original = '  --metadata="^|^image=reviewed|tee-env-TR_ASYNC_SETTLE_SHADOW=off|tee-env-TR_ASYNC_SETTLE_NEGOTIATE=off|tail=retained"\n'
    desired = pins(plan, 'shadow-opt-in')['enclave']
    diff = patch(original, 'tools/deploy-gcp-mig.sh', desired)
    expected = original.replace('SHADOW=off|', 'SHADOW=on|')
    assert '+' + expected in diff
    assert '-' + original in diff
    with pytest.raises(ValueError, match='pin source shape'):
        patch(original + ' "TR_ASYNC_SETTLE_SHADOW=off"\n', 'pins.sh', desired)


@pytest.mark.parametrize('issue', ['missing_next', 'wrong_next', 'wrong_run', 'wrong_total'])
def test_ci_page_continuity(plan, tmp_path, issue):
    url = f'https://api.github.com/repos/{plan.releases.router.repository}/actions/runs/5/jobs'
    selected = source(plan, 'github_jobs', url=url)
    row = dict(job_record(plan), run_id=5)
    body = dict(total_count=2, jobs=[row])
    link = f'<{url}?per_page=100&page=2>; rel="next"'
    if issue == 'missing_next':
        link = ''
    elif issue == 'wrong_next':
        link = link.replace('page=2', 'page=3')
    elif issue == 'wrong_run':
        row['run_id'] = 6
    else:
        body['total_count'] = 10001
    port = Recorded(Response(json.dumps(body).encode(), 200, dict(link=link), 20))
    with pytest.raises(ValueError):
        native(plan, tmp_path, port, selected).read(query(selected), None)


def test_monitoring_point_count_is_page_count(plan, tmp_path):
    value = dict(timeSeries=[dict(metric={}, resource={}, points=[{}] * 101)])
    with pytest.raises(ValueError, match='record limit'):
        native(plan, tmp_path, Recorded(value)).read(query(plan.sources[0]), None)


def test_no_completion_boundary_does_not_claim_complete(plan, tmp_path):
    port = Recorded({})
    result = cli.capture(plan, EXAMPLE.parent, tmp_path, port)
    assert result['complete'] is False
    assert result['missing'][0]['reason'] == 'delivery_boundary_missing_or_delayed'


def test_release_and_trial_refuse_live_review_example(tmp_path):
    for action, options in [('release', ['--phase', 'shadow-opt-in', '--wait']), ('trial', [])]:
        out = tmp_path / action
        assert cli.main([action, '--plan', str(EXAMPLE), '--out', str(out), *options]) == 1
        assert not out.exists()


@pytest.mark.parametrize('region', ['us-central1', 'us-east4', 'europe-west4', 'us-west1'])
def test_regional_original_uses_existing_host(plan, tmp_path, region):
    host = 'api.trustedrouter.com' if region == 'us-central1' else f'api-{region}.quillrouter.com'
    selected = source(plan, 'attestation', resource=region, parser='released-json-v1',
                      url=f'https://{host}/attestation')
    client = native(plan, tmp_path, Recorded({'record': 'original-only'}), selected)
    assert client.read(query(selected), None).eof
    wrong = selected.model_copy(update={'url': 'https://api-us-central1.quillrouter.com/attestation'})
    with pytest.raises(ValueError, match='released original binding'):
        native(plan, tmp_path / 'wrong', Recorded({}), wrong).read(query(wrong), None)


def test_trial_missing_runner_does_not_read_window_sources(plan, tmp_path):
    raw = json.dumps(dict(resource=plan.trial.resource, decision='approved')).encode()
    (tmp_path / 'approval.json').write_bytes(raw)
    approval = type(plan.trial.approval)(path='approval.json', sha256=hashlib.sha256(raw).hexdigest())
    selected = plan.model_copy(update={'trial': plan.trial.model_copy(update={'approval': approval})})
    port = Recorded()
    result = cli.trial(selected, tmp_path, tmp_path / 'out', port)
    assert result['missing'] == ['P6NativeQualificationHarness']
    assert not result['complete'] and not port.calls


def test_native_only_permits_read_method():
    from trusted_router.async_settle_shadow_v2_native_reads import HTTPReadTransport
    transport = HTTPReadTransport(lambda _: pytest.fail('must refuse before credentials'))
    with pytest.raises(ValueError, match='read method'):
        transport.read('POST', 'https://api.github.com/repos/a/b/actions/workflows/x/dispatches',
                       params={}, body={}, seconds=10, max_bytes=100)


def test_total_deadline_restores_process_timer():
    import signal
    import time

    from trusted_router.async_settle_shadow_v2_native_reads import deadline
    prior = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TimeoutError):
        with deadline(0.01):
            time.sleep(0.1)
    assert signal.getsignal(signal.SIGALRM) == prior
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_failed_attestation_workflow_cannot_be_omitted(plan, tmp_path):
    for number, workflow in enumerate(plan.workflows, start=1):
        workflow.run_id = number
    class Workflows:
        def read(self, method, url, **kwargs):
            run_id = int(url.split('/runs/')[1].split('/')[0])
            workflow = next(w for w in plan.workflows if w.run_id == run_id)
            if url.endswith('/jobs'):
                value = dict(total_count=1, jobs=[job_record(plan, workflow)])
            else:
                value = run_record(plan, workflow)
                if workflow.gate == 'attestation':
                    value['conclusion'] = 'failure'
            return Response(json.dumps(value).encode(), 200, {}, 10)
    with pytest.raises(ValueError, match='released workflow'):
        cli.observe_release(plan, 'shadow-opt-in', tmp_path, Workflows())
