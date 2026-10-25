"""P6 explicit release preparation, observation and dedicated-job scheduling."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from trusted_router.async_settle_shadow_v2_activation_plan import (
    Plan,
    contained_path,
    load,
    read_file,
    stamp,
)
from trusted_router.async_settle_shadow_v2_activation_release import pins, prepare, workflow_record
from trusted_router.async_settle_shadow_v2_collect_ports import Pass, Query
from trusted_router.async_settle_shadow_v2_native_reads import (
    BoundedFiles,
    GitHubPopulation,
    HTTPReadTransport,
    NativeClient,
    ReadBudget,
    ReadTransport,
)


def command(argv: list[str]) -> dict[str, Any]:
    result = subprocess.run(argv, capture_output=True, timeout=10, check=False)  # noqa: S603
    absent = any(code in result.stderr.decode(errors='replace') for code in ('NOT_FOUND:', '(NOT_FOUND)'))
    if result.returncode and absent and 'describe' in argv and 'scheduler' in argv:
        return {'absent': True}
    if result.returncode:
        # Do not put CLI responses in reports; they can contain unrelated data.
        raise ValueError('operator command failed')
    return json.loads(result.stdout or b'{}')


def headers(url: str) -> dict[str, str]:
    host = urlsplit(url).hostname or ''
    if host == 'api.github.com':
        argv = ['gh', 'auth', 'token']
    elif host.endswith('.googleapis.com'):
        argv = ['gcloud', 'auth', 'print-access-token']
    else:
        return {}
    result = subprocess.run(argv, capture_output=True, timeout=10, check=False)  # noqa: S603
    if result.returncode:
        raise ValueError('read identity unavailable')
    return {'Authorization': 'Bearer ' + result.stdout.decode().strip()}


def save(out: Path, name: str, value: Any) -> None:
    with (out / name).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def probe_commands(plan: Plan, phase: str) -> list[dict[str, Any]]:
    if phase not in ('warmup', 'window', 'stop'):
        raise ValueError('probe phase required')
    probe = plan.probe
    operations = []
    for job in probe.jobs:
        if job.name != 'trusted-router-async-settle-shadow-probe-' + job.region or job.scheduler != job.name + '-scheduled':
            raise ValueError('dedicated job required')
        base = ['gcloud', '--project', probe.project, 'scheduler', 'jobs']
        describe = base + ['describe', job.scheduler, '--location', job.region, '--format=json']
        if phase == 'stop':
            operations.append(dict(describe=describe, pause=base + ['pause', job.scheduler, '--location', job.region, '--quiet', '--format=json']))
            continue
        minutes = probe.warmup_minutes if phase == 'warmup' else probe.window_minutes
        uri = f'https://{job.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{probe.project}/jobs/{job.name}:run'
        args = ['http', job.scheduler, '--location', job.region, '--schedule', f'*/{minutes} * * * *',
                '--time-zone=UTC', '--uri', uri, '--http-method=POST', '--message-body={}',
                '--oauth-service-account-email', probe.service_account,
                '--max-retry-attempts=0', '--max-retry-duration=0s', '--quiet', '--format=json']
        operations.append(dict(describe=describe, create=base + ['create', *args], update=base + ['update', *args],
            resume=base + ['resume', job.scheduler, '--location', job.region, '--quiet', '--format=json'],
            job=['gcloud', '--project', probe.project, 'run', 'jobs', 'describe', job.name,
                 '--region', job.region, '--format=json']))
    return operations


class P6ProbeImageBinding(ValueError):
    """The existing job does not name the reviewed image digest."""


def check_job(plan: Plan, value: dict[str, Any], minutes: int) -> None:
    execution = value['spec']['template']['spec']
    spec = execution['template']['spec']
    container = spec['containers'][0]
    if container['image'] != plan.releases.router.image:
        raise P6ProbeImageBinding('reviewed dedicated-job digest required')
    env = {e['name']: e for e in container['env']}
    source = env['TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE']['value']
    if (execution.get('taskCount') not in (1, '1') or execution.get('parallelism') not in (1, '1')
            or len(spec['containers']) != 1 or spec['serviceAccountName'] != plan.probe.service_account
            or spec.get('maxRetries') not in (0, '0')
            or container['command'] != ['/app/.venv/bin/python']
            or container['args'] != ['-m', 'trusted_router.synthetic.cli', '--async-settle-shadow-probe']
            or env['TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS']['value'] != str(plan.probe.repetitions)
            or env['TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES']['value'] != str(minutes)
            or source != '/var/run/secrets/shadow-probe/api-key'):
        raise ValueError('dedicated released job differs')
    mounts = {v['name']: v['mountPath'] for v in container['volumeMounts']}
    expected = plan.probe.secret_reference.rsplit('/', 1)[-1]
    if plan.probe.secret_reference != f'projects/{plan.probe.project}/secrets/trustedrouter-async-settle-shadow-probe-api-key':
        raise ValueError('dedicated cohort reference differs')
    if not any(v.get('secret', {}).get('secretName') == expected and
               mounts.get(v['name']) == '/var/run/secrets/shadow-probe' for v in spec['volumes']):
        raise ValueError('dedicated job reference differs')


def probe(plan: Plan, phase: str, apply: bool, runner: Any = command) -> dict[str, Any]:
    operations = probe_commands(plan, phase)
    if phase != 'stop':
        from trusted_router.synthetic.shadow_probe_contract import SHAPES
        if plan.probe.max_invocations * len(SHAPES) * len(plan.enclave_regions) * plan.probe.repetitions > plan.probe.max_requests:
            raise ValueError('probe request budget')
    result: dict[str, Any] = dict(complete=False, applied=False, commands=operations, missing=[])
    if not apply:
        result['missing'] = ['OperatorApplyRequired']
        return result
    if phase != 'stop':
        interval = plan.schedule.warmup if phase == 'warmup' else plan.schedule.window
        now = datetime.now(UTC).timestamp()
        if not stamp(interval.start) <= now < stamp(interval.end):
            raise ValueError('outside approved probe interval')
        minutes = plan.probe.warmup_minutes if phase == 'warmup' else plan.probe.window_minutes
        for operation in operations:
            check_job(plan, runner(operation['job']), minutes)
        for operation in operations:
            state = runner(operation['describe'])
            runner(operation['create'] if state.get('absent') else operation['update'])
            if state.get('state') == 'PAUSED':
                runner(operation['resume'])
            observed = runner(operation['describe'])
            target = observed.get('httpTarget', {})
            expected_uri = operation['create'][operation['create'].index('--uri') + 1]
            retry = observed.get('retryConfig', {})
            if (observed.get('state') != 'ENABLED' or observed.get('schedule') != f'*/{minutes} * * * *'
                    or observed.get('timeZone') != 'UTC' or target.get('uri') != expected_uri
                    or target.get('httpMethod') != 'POST' or target.get('body') != 'e30='
                    or target.get('oauthToken', {}).get('serviceAccountEmail') != plan.probe.service_account
                    or retry.get('retryCount', 0) != 0 or retry.get('maxRetryDuration', '0s') != '0s'):
                raise ValueError('exact enabled schedule not observed')
        result.update(complete=True, applied=True, stop_required_at=interval.end,
                      stop_command='probe --plan <same-approved-plan> --phase stop --apply')
        return result
    for operation in operations:
        state = runner(operation['describe'])
        if state.get('absent'):
            continue
        if state.get('state') != 'PAUSED':
            runner(operation['pause'])
        if runner(operation['describe']).get('state') != 'PAUSED':
            raise ValueError('pause not observed')
    result.update(complete=True, applied=True)
    return result


def capture(plan: Plan, root: Path, out: Path, transport: ReadTransport) -> dict[str, Any]:
    files = BoundedFiles(out, plan.limits.output_bytes)
    budget = ReadBudget()
    current = Pass(files, limit=plan.limits.records_per_pass)
    for source in plan.sources:
        if budget.stopped or budget.records >= plan.limits.records_per_pass or budget.calls >= plan.limits.calls_per_pass:
            current.gap(source.id, 'native_pass_stopped' if budget.stopped else 'native_pass_allowance_exhausted')
            break
        boundary = None
        if source.completion_record is not None:
            record = json.loads(read_file(root, source.completion_record))
            if record['source'] != source.id or record['resource'] != source.resource:
                raise ValueError('source completion binding differs')
            boundary = record['complete_through_us']
        client = NativeClient(plan, source, transport, files, complete_through_us=boundary, budget=budget)
        query = Query(source.id, source.resource, source.day, plan.limits.records_per_page, plan.limits.call_seconds)
        for _ in current.scan(client, query):
            pass
        files.obj('native/' + source.id + '/index.json', client.originals)
    from trusted_router.async_settle_shadow_v2_native_artifacts import (
        HTTPArchivePort,
        capture_archive,
    )
    for archive in plan.archives:
        if budget.stopped or budget.records >= plan.limits.records_per_pass or budget.calls + 3 > plan.limits.calls_per_pass:
            current.gap('archives', 'native_pass_stopped_or_allowance_exhausted')
            break
        if archive.max_bytes > files.remaining:
            raise ValueError('archive output budget')
        files.remaining -= archive.max_bytes
        capture_archive(plan, archive, files, transport, HTTPArchivePort(headers), budget)
    files.obj('native-budget.json', dict(calls=budget.calls, reserved_records=budget.records,
        recorded_records=budget.recorded, remaining_calls=plan.limits.calls_per_pass - budget.calls,
        remaining_records=plan.limits.records_per_pass - budget.records, stopped=budget.stopped))
    current.calls, current.records = budget.calls, budget.recorded
    current.status('native-pass.json')
    return dict(complete=not current.missing, missing=current.missing,
                records=current.records, calls=current.calls)


def observe_release(plan: Plan, phase: str, out: Path, transport: ReadTransport,
                    *, polls: int = 1, sleep: Any = time.sleep, expires_at: str = '') -> dict[str, Any]:
    if not 1 <= polls <= plan.limits.calls_per_pass // max(1, len(plan.workflows) * 2):
        raise ValueError('release poll budget')
    files = BoundedFiles(out, plan.limits.output_bytes)
    calls = 0
    records = 0
    def get(url: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls, records
        if calls >= plan.limits.calls_per_pass or records + params.get('per_page', 1) > plan.limits.records_per_pass:
            raise ValueError('release call budget')
        calls += 1
        records += params.get('per_page', 1)
        response = transport.read('GET', url, params=params, body=None,
            seconds=plan.limits.call_seconds, max_bytes=plan.limits.bytes_per_page)
        files.put(f'observed/{calls:05d}.json', response.raw)
        if response.status != 200 or response.elapsed_us > plan.limits.call_seconds * 1000000:
            raise ValueError('release read failed')
        value = json.loads(response.raw)
        if len(response.raw) > plan.limits.bytes_per_page or ('jobs' in value and len(value['jobs']) > params.get('per_page', 1)):
            raise ValueError('release record budget')
        return value
    for poll in range(polls):
        pending = False
        for workflow in plan.workflows:
            release = getattr(plan.releases, workflow.role)
            base = f'https://api.github.com/repos/{release.repository}/actions/runs/{workflow.run_id}'
            run = get(base, {})
            if run.get('head_sha') != release.sha or run.get('id') != workflow.run_id:
                raise ValueError('unreviewed release SHA')
            if run.get('status') != 'completed':
                pending = True
                continue
            jobs = []
            population = GitHubPopulation()
            page = 1
            while True:
                response = get(base + '/jobs', dict(per_page=plan.limits.records_per_page, page=page, filter='latest'))
                batch = response['jobs']
                if len(batch) > plan.limits.records_per_page:
                    raise ValueError('release job page limit')
                complete = population.add(response['total_count'], batch, plan.limits.records_per_pass)
                if any(job.get('run_id') != run['id'] or job.get('head_sha') != release.sha for job in batch):
                    raise ValueError('GitHub selected record differs')
                jobs.extend(batch)
                if len(jobs) > plan.limits.records_per_pass:
                    raise ValueError('release job pass limit')
                if complete:
                    break
                if not batch:
                    raise ValueError('incomplete job pages')
                page += 1
            workflow_record(plan, workflow.role, workflow.workflow, run, jobs, workflow.jobs)
        if not pending:
            break
        if poll + 1 < polls:
            sleep(min(10, plan.limits.call_seconds))
    if pending:
        return dict(complete=False, missing=['ExistingWorkflowPending'], calls=calls)
    required = {'trust', 'registry', 'health', 'attestation'}
    sources = [s for s in plan.sources if s.kind in required]
    for source in sources:
        budget = ReadBudget(calls=calls, records=records)
        client = NativeClient(plan, source, transport, files, budget=budget)
        client.read(Query(source.id, source.resource, source.day, plan.limits.records_per_page, plan.limits.call_seconds), None)
        calls, records = budget.calls, budget.records
    # Raw attestation JSON is not a verified regional pin projection. The
    # router /health surface reports status only, not deployed environment.
    missing = ['P6LivePinProjection', 'P6AttestedRegionVerification']
    missing.extend('P6ReleasedOriginal:' + name for name in sorted(required - {s.kind for s in sources}))
    return dict(complete=False, missing=missing, calls=calls,
                expected_pins=pins(plan, phase, expires_at=expires_at))


def trial(plan: Plan, root: Path, out: Path, transport: ReadTransport) -> dict[str, Any]:
    # pack_trial and CapturedBudget consume existing originals only. Without
    # the isolated runner, window source reads cannot stand in for trial work.
    record = json.loads(read_file(root, plan.trial.approval))
    if record.get('resource') != plan.trial.resource or record.get('decision') != 'approved':
        raise ValueError('isolated trial approval differs')
    return dict(complete=False, missing=['P6NativeQualificationHarness'],
        producer='trusted_router.async_settle_shadow_v2_pack_captured_budget.CapturedBudget.build',
        blocker='No native writer/transport/shutdown harness entry point exists in this checkout; existing trial functions validate retained originals.')


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command in ('release', 'probe') and args.phase not in ({'shadow-opt-in', 'shadow-opt-out'} if args.command == 'release' else {'warmup', 'window', 'stop'}):
        raise ValueError('phase required')
    if (args.apply and args.command != 'probe') or (args.wait and args.command != 'release'):
        raise ValueError('action option differs')
    live = args.apply or args.wait or args.command in ('native', 'trial')
    if live and not args.plan_sha256:
        raise ValueError('independent --plan-sha256 required')
    plan = load(args.plan, approved_sha256=args.plan_sha256 if live else None)
    root = args.plan.parent
    planned_out = Path(getattr(plan.artifacts, args.command))
    if args.phase:
        planned_out /= args.phase
    out = args.out or contained_path(root, str(planned_out))
    out.mkdir(parents=True, exist_ok=False)
    save(out, 'plan-record.json', dict(plan_sha256=hashlib.sha256(args.plan.read_bytes()).hexdigest(),
         command=args.command, phase=args.phase, apply=args.apply, reviewed=live))
    try:
        if args.command == 'probe':
            result = probe(plan, args.phase, args.apply)
        elif args.command == 'release':
            result = prepare(plan, args.phase, Path.cwd(), args.enclave_root, out,
                             expires_at=args.expires_at or '')
            commands = []
            for role in ('router', 'enclave'):
                release = getattr(plan.releases, role)
                commands.append(shlex.join(['gh', 'pr', 'create', '--repo', release.repository,
                    '--base', 'main', '--title', 'P6 ' + args.phase, '--body-file', str(out / 'release-review.md')]))
            (out / 'operator-commands.txt').write_text('\n'.join(commands) + '\n')
            (out / 'release-review.md').write_text('Apply the emitted patch in the named repository; review exact pins and measured pre-enable PASS. Merge through existing CI and release gates. Retain close/export originals for opt-out.\n')
            result.update(complete=False, prepared=True, operator_commands=commands)
            if args.wait:
                observed = observe_release(plan, args.phase, out, HTTPReadTransport(headers), polls=args.polls,
                                           expires_at=args.expires_at or '')
                result['observation'] = observed
                result['missing'].extend(observed['missing'])
            else:
                result['missing'].append('OperatorMergeAndReleaseObservation')
        elif args.command == 'native':
            result = capture(plan, root, out, HTTPReadTransport(headers))
        else:
            result = trial(plan, root, out, HTTPReadTransport(headers))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = dict(complete=False, missing=['P6ActionIncomplete:' + type(exc).__name__])
    save(out, 'result.json', result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('release', 'probe', 'trial', 'native', 'validate-plan'))
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--phase')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--polls', type=int, default=1)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--enclave-root', type=Path)
    # Required for release shadow-opt-in: the canary's runtime stop, e.g.
    # 2026-10-24T15:30:00Z (UTC, future, at most 48 h ahead). Without it the
    # deployed routers treat the pins as expired and do nothing.
    parser.add_argument('--expires-at')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate-plan':
            plan = load(args.plan, approved_sha256=args.plan_sha256)
            result = dict(complete=True, review=plan.review, passes=len(plan.passes), launches=len(plan.schedule.launches))
        else:
            result = run(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        owner = {'trial': 'P6NativeQualification', 'release': 'P6ReviewedRelease', 'probe': 'P6DedicatedJob'}.get(args.command, 'P6Plan')
        result = dict(complete=False, missing=[owner, 'P6PlanOrOutput:' + type(exc).__name__])
    print(json.dumps(result, sort_keys=True))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
