"""Independent launch inventory; application records only join, never enumerate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import (
    TAGS,
    LifecyclePort,
    MonitoringPort,
    Pass,
    PlatformPort,
    Query,
    approval,
    day_bounds,
)
from trusted_router.async_settle_shadow_v2_schema import decode, require


@dataclass(frozen=True)
class InventoryResult:
    envelope: dict[str, Any] | None
    serving: dict[str, Any]
    deployment: dict[str, Any]
    boots: tuple[dict[str, Any], ...]
    expected: tuple[dict[str, Any], ...]
    complete: bool


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None)
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def lifecycle(entry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any] | None:
    """Exact binding v1 parsers. Unknown platform termination kinds block upstream.

    Compute instance-name membership/revision mapping comes from captured MIG
    history, not name-prefix guessing. Supervisors supply process launch identity.
    """
    mode = binding['mode']
    if mode == 'supervisor':
        record = entry.get('jsonPayload', {})
        if record.get('kind') not in ('a3_process_launch', 'a3_process_exit', 'a3_process_boundary'):
            return None
        require(record['supervisor'] == binding['supervisor'])
        return dict(instance=record['instance'], launch=record['launch'],
                    event=('start' if record['kind'] == 'a3_process_launch' else
                           'boundary' if record['kind'] == 'a3_process_boundary' else record['event']),
                    at_us=record['at_us'], role=record['role'], revision=record['revision'],
                    region=record['region'], event_id=entry['insertId'])
    resource = entry['resource']
    labels = resource['labels']
    if mode == 'cloud_run_instance':
        require(resource['type'] == 'cloud_run_revision')
        require(entry['logName'] == binding['log_name'])
        require(labels['service_name'] == 'trusted-router')
        message = entry.get('textPayload', '')
        if message.startswith('Starting new instance. Reason: '):
            event = 'start'
        elif message == 'Shutting down user disabled instance':
            event = 'stop'
        elif message.startswith('Default STARTUP TCP probe succeeded '):
            return None
        else:
            raise ValueError('unclassified_platform_lifecycle')
        instance = entry['labels']['instanceId']
        revision = binding['revisions'][labels['revision_name']]
        return dict(instance=instance, launch=instance, event=event, at_us=timestamp(entry['timestamp']),
                    role='router', revision=revision, region=labels['location'], event_id=entry['insertId'])
    require(mode == 'compute_instance')
    require(entry['logName'] == binding['log_name'])
    payload = entry['protoPayload']
    require(payload['serviceName'] == 'compute.googleapis.com')
    method = payload['methodName']
    require(method in ('v1.compute.instances.insert', 'v1.compute.instances.delete'))
    if entry.get('operation', {}).get('last') is not True:
        return None
    require(not payload.get('status', {}).get('code', 0))
    instance = labels['instance_id']
    member = binding['instances'][instance]
    require(member['resource_name'] == payload['resourceName'])
    require(member['mig'] in binding['migs'])
    return dict(instance=instance, launch=instance, event='start' if method.endswith('.insert') else 'stop',
                at_us=timestamp(entry['timestamp']), role='enclave', revision=member['revision'],
                region=member['region'], event_id=entry['insertId'])


def collect(run: Pass, *, platform: PlatformPort, logs: LifecyclePort, monitoring: MonitoringPort,
            queries: dict[str, Query], binding_ref: dict[str, Any],
            review: dict[str, Any] | None = None) -> InventoryResult:
    """One explicitly named resource/day pass; missing binds never get boot IDs."""
    try:
        binding = decode(run.files.read(binding_ref), 262144, retained_strings=True)
    except (OSError, ValueError):
        run.gap('binding', 'binding_missing', 'writer_roster_gap')
        binding = {}
    day = queries['lifecycle'].day
    start_day, end_day = day_bounds(day)
    require(all(q.day == day for q in queries.values()))
    config = list(run.scan(platform, queries['platform']))
    valid_config = []
    for row in config:
        try:
            run.files.read(row['original'])
            valid_config.append(row)
        except (KeyError, OSError, ValueError):
            run.gap('platform', 'original_platform_page_missing', 'writer_roster_gap')
    config = valid_config
    events = list(run.scan(logs, queries['lifecycle']))
    correlations = list(run.scan(logs, queries['startup']))
    boundaries = list(run.scan(monitoring, queries['monitoring']))
    for row in boundaries:
        try:
            run.files.read(row['original'])
        except (KeyError, OSError, ValueError):
            run.gap('monitoring', 'original_monitoring_page_missing', 'writer_roster_gap')
    if not config:
        run.gap('platform', 'deployment_missing', 'writer_roster_gap')
    if not boundaries:
        run.gap('monitoring', 'boundary_corroboration_missing', 'writer_roster_gap')
    # Review must establish both boundaries. An EOF/writer startup cannot do so.
    if not binding.get('process_complete') or not binding.get('delivery_complete'):
        run.gap('binding', 'ProcessCompletenessQualification', 'writer_roster_gap')
    for ref in binding.get('witnesses', []):
        try:
            run.files.read(ref)
        except (OSError, ValueError):
            run.gap('binding', 'witness_missing', 'writer_roster_gap')
    if len(binding.get('witnesses', [])) < 2:
        run.gap('binding', 'kill_and_restart_witnesses_required', 'writer_roster_gap')
    parsed = []
    event_ids: dict[str, dict[str, Any]] = {}
    for entry in events:
        try:
            event_id = entry['insertId']
            if event_id in event_ids:
                require(event_ids[event_id] == entry)
                continue
            event_ids[event_id] = entry
            value = lifecycle(entry, binding)
            if value is not None:
                parsed.append(value)
        except (ValueError, KeyError, TypeError):
            run.gap('lifecycle', 'unclassified_or_invalid_event', 'writer_roster_gap')
    # Carry-in launches are independent prior-day enumerations with original refs.
    for carry in binding.get('carry_in', []):
        try:
            run.files.read(carry['source'])
            parsed.append(carry['launch'])
        except (OSError, ValueError):
            run.gap('lifecycle', 'carry_in_missing', 'writer_roster_gap')
    launches: dict[str, list[dict[str, Any]]] = {}
    for event in parsed:
        launches.setdefault(event['launch'], []).append(event)
    expected, boots, serving = [], [], []
    seen_boots: set[str] = set()
    for launch_id, history in sorted(launches.items()):
        starts = [r for r in history if r['event'] == 'start']
        exits = [r for r in history if r['event'] in ('stop', 'kill', 'boundary')]
        state: dict[str, Any] = dict(launch=launch_id, events=history, boot=None, missing=[])
        expected.append(state)
        if len(starts) != 1 or len(exits) != 1:
            state['missing'].append('lifecycle_boundary')
            run.gap(launch_id, 'lifecycle_boundary', 'writer_roster_gap')
            continue
        first, last = starts[0], exits[0]
        joins = [r for r in correlations if r.get('kind') == 'a3_startup_correlation'
                 and r.get('launch') == launch_id and r.get('instance') == first['instance']]
        deployments = [r for r in config if all(r.get(k) == first[k] for k in ('role', 'revision', 'region'))]
        if len(joins) != 1 or len(deployments) != 1:
            state['missing'].append('startup_or_deployment_join')
            run.gap(launch_id, 'startup_or_deployment_join', 'writer_roster_gap')
            continue
        join = joins[0]
        try:
            a2.identity(join['boot'])
            a2.revision(first['revision'])
            a2.name(first['region'])
            require(join['boot'] not in seen_boots)
            require(all(join[k] == first[k] == last[k] for k in ('role', 'revision', 'region', 'instance')))
            require(first['at_us'] <= join['process_start_us'] <= join['recorded_at_us'] < last['at_us'])
            left, right = max(start_day, first['at_us']), min(end_day, last['at_us'])
            require(left < right)
            require(last['event'] != 'boundary' or last['at_us'] == end_day)
        except (KeyError, ValueError, TypeError):
            state['missing'].append('invalid_join')
            run.gap(launch_id, 'invalid_join', 'writer_roster_gap')
            continue
        seen_boots.add(join['boot'])
        state['boot'] = join['boot']
        boot = dict(boot=join['boot'], role=first['role'], revision=first['revision'], region=first['region'],
                    boot_at_us=join['process_start_us'], start_us=left, end_us=right,
                    launch=launch_id, instance=first['instance'], reason=('hard_kill' if last['event'] == 'kill' else
                        'utc_boundary' if last['event'] == 'boundary' or last['at_us'] > end_day else 'shutdown'),
                    grace_us=deployments[0].get('grace_us'), pins=deployments[0]['pins'])
        boots.append(boot)
        serving.append(dict(instance=boot['boot'], role=boot['role'], revision=boot['revision'],
            region=boot['region'], pins=boot['pins'], intervals=[dict(day=day, started_at_us=left, flushed_at_us=right)]))
    if any(r.get('launch') not in launches for r in correlations):
        run.gap('startup', 'correlation_without_independent_launch', 'writer_roster_gap')
    if not expected:
        run.gap('lifecycle', 'no_independent_launches', 'writer_roster_gap')
    # Serving's historical field flushed_at_us means interval end here, not a writer ack.
    serving_ref = run.files.obj(f'inventory/{day}/serving.json', serving)
    deployment_ref = run.files.obj(f'inventory/{day}/deployment.json', dict(serving=serving,
        expected=expected, binding=binding_ref, platform=config, monitoring=boundaries,
        originals=run.sources, missing=run.missing))
    approved = approval(run.files, review, deployment_ref)
    envelope = None
    if approved is not None:
        value = dict(**TAGS, day=day, serving=serving_ref, deployment=deployment_ref,
                     approval=approved, complete=not run.missing)
        envelope = run.files.obj(f'inventory/{day}/inventory.json', value, a2.INDEPENDENT_INVENTORY)
    return InventoryResult(envelope, serving_ref, deployment_ref, tuple(boots), tuple(expected), not run.missing)
