"""Read-only pilot checklists; only header-probe creates and immediately refunds a hold.

Evidence bundle schema is documented in the PR G runbook. No discovery scans,
configuration writes, manifest publication, inference calls or async settles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from trusted_router.async_settle_shadow_evidence import CONTROL, COUNTER, SAMPLE
from trusted_router.detached_jws import b64decode, b64encode
from trusted_router.security import lookup_hash_api_key

if __package__:
    from . import shadow_report
    from .drain_capacity import timestamp
else:
    import shadow_report
    from drain_capacity import timestamp

PILOT = '45819281-0ce9-4811-a0cd-c660ab3a116d'
HEADER_BYTES = 12288


def checklist(checks: dict[str, bool], **details: Any) -> dict[str, Any]:
    return dict(status='PASS' if checks and all(checks.values()) else 'BLOCKED',
                checklist={key: 'PASS' if value else 'BLOCKED' for key, value in checks.items()}, **details)


def artifact(item: dict[str, Any]) -> bool:
    """Hash actual local CI/export bytes, never accept a free-form PASS assertion."""
    return bool(item.get('path') and item.get('sha256')
        and hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256'])


def live_database(project: str, instance: str, database: str) -> Any:
    """SDK database handle only; never construct a store or initialize a schema."""
    from google.cloud import spanner
    if not all((project, instance, database)):
        raise ValueError('explicit project, instance and database required for live reads')
    return spanner.Client(project=project).instance(instance).database(database)


def point_evidence(database: Any, identities: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Explicit sample IDs, day/instance IDs and manifest IDs; no discovery."""
    from google.cloud.spanner_v1 import param_types
    if not identities or len(identities) > 10000:
        raise ValueError('1..10000 explicit evidence keys required')
    rows = []
    for key in identities:
        if key['kind'] not in {CONTROL, COUNTER, SAMPLE} or not key['id'] or len(key['id']) > 512:
            raise ValueError('invalid evidence key')
        with database.snapshot() as reader:
            found = list(reader.execute_sql('SELECT body FROM tr_entities WHERE kind=@kind AND id=@id',
                params=key, param_types={'kind': param_types.STRING, 'id': param_types.STRING},
                timeout=.2, retry=None, request_options={'priority': 'PRIORITY_LOW'}))
        if len(found) != 1:
            raise ValueError('missing explicit evidence key')
        rows.append(dict(**key, body=json.loads(found[0][0])))
    return rows


def workspace_outbox(database: Any, workspace: str, limit: int) -> list[dict[str, Any]]:
    from google.cloud.spanner_v1 import param_types
    if workspace != PILOT or not 1 <= limit <= 1000:
        raise ValueError('pilot workspace and bounded limit required')
    columns = 'authorization_id,intent_kind,workspace_id,status,created_at,terminal_at,async_version'
    # Full leading workspace index key. Sentinel means incomplete, never PASS.
    with database.snapshot() as reader:
        found = list(reader.execute_sql(
            'SELECT authorization_id,intent_kind,workspace_id,status,created_at,terminal_at,async_version '
            'FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_workspace_status} '
            'WHERE workspace_id=@ws LIMIT @limit',
            params={'ws': workspace, 'limit': limit + 1},
            param_types={'ws': param_types.STRING, 'limit': param_types.INT64},
            timeout=.2, retry=None, request_options={'priority': 'PRIORITY_LOW'}))
    if len(found) > limit:
        raise ValueError('outbox export truncated; narrow pilot observation/export before retry')
    return [dict(zip(columns.split(','), [v.isoformat() if hasattr(v, 'isoformat') else v for v in row], strict=True)) for row in found]


def serving(bundle: dict[str, Any], *, admitted: bool) -> bool:
    roster = bundle['serving']
    expected = set(bundle['serving_instance_ids'])
    if not roster or len(expected) != len(roster) or {row['instance'] for row in roster} != expected:
        return False
    for row in roster:
        pins = row['pins']
        if not row['revision'] or not row['region']:
            return False
        if row['role'] == 'router':
            if pins.get('TR_ASYNC_SETTLE_ENABLED') != str(admitted).lower():
                return False
            if admitted:
                if (pins.get('TR_ASYNC_SETTLE_PILOT_WORKSPACES') != PILOT
                        or pins.get('TR_ASYNC_SETTLE_PILOT_CAP_MICRO') != '5000000'
                        or pins.get('TR_ASYNC_SETTLE_PROTECTION') != 'true'):
                    return False
            elif pins.get('TR_ASYNC_SETTLE_SHADOW_WORKSPACES') != PILOT:
                return False
        elif row['role'] == 'enclave':
            if pins.get('TR_ASYNC_SETTLE_NEGOTIATE') != ('on' if admitted else 'off'):
                return False
            if pins.get('TR_ASYNC_SETTLE_SHADOW') != ('off' if admitted else 'on'):
                return False
        else:
            return False
    return {row['role'] for row in roster} == {'router', 'enclave'}


def shadow_serving(bundle: dict[str, Any]) -> dict[str, Any]:
    gate_outputs = {name: json.loads(Path(bundle[name]['path']).read_text()) for name in ('transport', 'fleet')}
    gate_times = [value['completed_at_us'] for value in gate_outputs.values()]
    if any(type(value) is not int or value <= 0 for value in gate_times):
        raise ValueError('gate completion timestamps required')
    report = shadow_report.report(bundle['rows'], bundle['days'], bundle['proof'],
                                  not_before_us=max(gate_times))
    checks = dict(serving_pins=serving(bundle, admitted=False),
        durable_evaluable_sample=report['clean_window_start_us'] is not None,
        manifest_coverage=report['completeness'] == 'complete',
        serving_revisions=set(report['source_revisions']['router']) == {r['revision'] for r in bundle['serving'] if r['role'] == 'router'}
            and set(report['source_revisions']['go']) == {r['revision'] for r in bundle['serving'] if r['role'] == 'enclave'},
        transport_gate=artifact(bundle['transport']), fleet_gate=artifact(bundle['fleet']),
        transport_manifest=bundle['proof'].get('maximum_header_hops') == bundle['transport']['sha256'],
        fleet_manifest=bundle['proof'].get('fleet_load_budget') == bundle['fleet']['sha256']
            == bundle['proof'].get('publisher_poll_freshness'))
    # Artifacts must be outputs of their named gates, not arbitrary hash matches.
    for name in ('transport', 'fleet'):
        evidence = gate_outputs[name]
        checks[name + '_pass'] = evidence.get('status') == 'PASS'
    return checklist(checks, first_durable_evaluable_sample_us=report['clean_window_start_us'],
        clock_start_us=report['clean_window_start_us'] if all(checks.values()) else None,
        continuous_seconds=report['continuous_seconds'], shadow_report=report)


def fleet_budgets(bundle: dict[str, Any]) -> dict[str, Any]:
    counters = [row for row in bundle['rows'] if row['kind'] == COUNTER]
    checks = {'counters_present': bool(counters)}
    totals: dict[str, dict[str, float]] = {}
    for row in counters:
        shadow_report.validate_counter(row['id'], row['body'])
        body = row['body']
        duration = (body['flushed_at_us'] - body['started_at_us']) / 1e6
        obs = body['admission_observer']
        n = bundle['workspace_count']
        ok = (duration > 0 and 1 <= n <= 32 and body['closed']
              and not any(obs[k] for k in ('read_failures', 'missed_ticks', 'prediction_unknown'))
              and obs['prediction_yes'] + obs['prediction_no'] > 0
              and max(1, n * (math.floor(duration / 4) - 1)) <= obs['workspace_reads'] <= n * (math.ceil(duration / 4) + 1)
              and max(1, math.floor(duration) - 1) <= obs['health_reads'] <= math.ceil(duration) + 1)
        checks[row['id']] = ok
        if duration > 0:
            region = totals.setdefault(body['region'], {'reads_per_second': 0., 'pending_rows_per_second': 0.})
            region['reads_per_second'] += (obs['workspace_reads'] + obs['health_reads']) / duration
            region['pending_rows_per_second'] += obs['workspace_reads'] * 1001 / duration
    checks['complete_instance_roster'] = {row['body']['instance'] for row in counters} == set(bundle['router_instance_ids'])
    maxima = bundle['freshness_maxima_seconds']
    bounds = dict(publisher_period=2., publisher_jitter=.25, publication=.5,
                  poll_period=1., poll_jitter=.25, install=.2, skew=.25,
                  workspace_period=4., workspace_jitter=.25, workspace_install=.2)
    checks['freshness_components'] = set(maxima) == set(bounds) and all(
        type(maxima[k]) in (int, float) and math.isfinite(maxima[k]) and 0 <= maxima[k] <= bound
        for k, bound in bounds.items())
    checks['combined_health_age'] = sum(maxima[k] for k in list(bounds)[:7]) <= 4.45
    checks['combined_workspace_age'] = sum(maxima[k] for k in list(bounds)[7:]) <= 4.45
    total_reads = sum(v['reads_per_second'] for v in totals.values())
    maximum_instances = bundle['maximum_router_instances']
    checks['maximum_instance_headroom'] = (type(maximum_instances) is int
        and maximum_instances >= len(bundle['router_instance_ids'])
        and maximum_instances * (bundle['workspace_count'] / 4 + 1) <= bundle['approved_fleet_reads_per_second'])
    checks['fleet_read_headroom'] = total_reads <= bundle['approved_fleet_reads_per_second']
    for region, rates in totals.items():
        budget = bundle['regional_budgets'][region]
        maximum = bundle['maximum_router_instances_by_region'][region]
        observed = len({row['body']['instance'] for row in counters if row['body']['region'] == region})
        checks[region + '_headroom'] = (type(maximum) is int and maximum >= observed
            and all(rates[k] <= budget[k] for k in rates)
            and maximum * (bundle['workspace_count'] / 4 + 1) <= budget['reads_per_second']
            and maximum * bundle['workspace_count'] / 4 * 1001 <= budget['pending_rows_per_second'])
    checks['regional_roster_caps'] = sum(bundle['maximum_router_instances_by_region'].values()) == maximum_instances
    checks['measured_headroom_artifact'] = artifact(bundle['headroom'])
    return checklist(checks, regions=totals, fleet_reads_per_second=total_reads,
                     freshness_maxima_seconds=maxima)


def pre_flip(bundle: dict[str, Any]) -> dict[str, Any]:
    shadow = shadow_serving(bundle)
    report = shadow['shadow_report']
    drain = json.loads(Path(bundle['drain']['path']).read_text())
    checks = dict(shadow_serving=shadow['status'] == 'PASS',
        seven_day_shadow_pass=report['status'] == 'PASS' and report['continuous_seconds'] >= 604800,
        measured_drain=artifact(bundle['drain']) and drain.get('status') == 'PASS'
            and bool(drain.get('exports')) and bool(drain.get('pins')),
        signer_epoch=bundle['signer']['epoch'] > 0 and bundle['signer']['audience'] == 'router-settlement'
            and bool(bundle['signer']['kid']) and bool(bundle['signer']['issuer'])
            and bundle['signer']['mounted'] is True and artifact(bundle['signer']['verification']))
    signer = bundle['signer']
    public = b64decode(signer['public_key'])
    checks['signer_public_key'] = len(public) == 32 and b64encode(public) == signer['public_key']
    expected = {'TR_ASYNC_SETTLE_TICKET_KID': signer['kid'],
                'TR_ASYNC_SETTLE_TICKET_ISSUER': signer['issuer'],
                'TR_ASYNC_SETTLE_TICKET_AUDIENCE': signer['audience'],
                'TR_ASYNC_SETTLE_AUTHORITY_EPOCH': str(signer['epoch'])}
    for row in bundle['serving']:
        pins = row['pins']
        if row['role'] == 'router':
            checks['signer_pin:' + row['instance']] = (all(pins.get(k) == v for k, v in expected.items())
                and pins.get('TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE', '').startswith('/'))
        else:
            keyring = json.loads(pins.get('TR_ASYNC_SETTLE_TICKET_PUBLIC_KEYS', '{}'))
            checks['keyring:' + row['instance']] = keyring.get(signer['kid']) == signer['issuer'] + '~' + signer['public_key']
    for name in ('F1', 'F2b', 'F2c'):
        checks[name + '_CI_hash'] = artifact(bundle['ci'][name])
    for path, digest in drain.get('exports', {}).items():
        checks['drain_export:' + path] = artifact(dict(path=path, sha256=digest))
    return checklist(checks, chosen_drain_pins=drain.get('pins'), drain_exports=drain.get('exports'))


def post_flip(bundle: dict[str, Any]) -> dict[str, Any]:
    checks = dict(pilot_only_admission=serving(bundle, admitted=True),
                  rollback_exercised=artifact(bundle['rollback']))
    rollback = json.loads(Path(bundle['rollback']['path']).read_text())
    checks['rollback_sequence'] = rollback.get('states') == [
        'admission_on', 'pending', 'admission_off', 'drained', 'protection_off'] and rollback.get('status') == 'PASS'
    exported = bundle['outbox']
    rows = [row for row in exported if row.get('async_version') == 1]
    timings = bundle['handoff']
    regions: dict[str, Any] = {}
    checks['bounded_pilot_rows'] = bool(rows) and len(exported) <= 1000 and all(row['workspace_id'] == PILOT for row in exported)
    checks['unique_intents'] = len({(row['authorization_id'], row['intent_kind']) for row in rows}) == len(rows)
    for row in rows:
        key = row['authorization_id'] + '.' + row['intent_kind']
        timing = timings.get(key)
        checks[key] = bool(timing and row['terminal_at'] is not None and row['status'] in {'done', 'release_approved'})
        if not timing:
            continue
        regional = regions.setdefault(timing['region'], {'handoff_us': [], 'drain_us': []})
        handoff = timing['handoff_us']
        if type(handoff) is not int or handoff < 0:
            checks[key] = False
            handoff = None
        regional['handoff_us'].append(handoff)
        drain = int((timestamp(row['terminal_at']) - timestamp(row['created_at'])) * 1e6) if row['terminal_at'] is not None else None
        if drain is not None and drain < 0:
            checks[key] = False
            drain = None
        regional['drain_us'].append(drain)
    checks['all_regions_measured'] = set(regions) == {r['region'] for r in bundle['serving'] if r['role'] == 'router'}
    checks['peak_concurrency_recorded'] = type(bundle.get('observed_peak_concurrency')) is int and bundle['observed_peak_concurrency'] > 0
    return checklist(checks, regional_percentiles={region: {key: shadow_report.percentiles(values)
        for key, values in metrics.items()} for region, metrics in regions.items()},
        observed_peak_concurrency=bundle.get('observed_peak_concurrency'))


def header_probe(base_url: str, model: str, key: str, gateway_token: str,
                 client: Any, *, clock: Any = time.monotonic) -> dict[str, Any]:
    url = urlsplit(base_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('HTTPS base URL without credentials required')
    # Printable padding, not trailing whitespace that a hop could strip.
    header = 'v1' + 'x' * (HEADER_BYTES - 2)
    headers = {'X-TR-Settlement-Shadow': header, 'Authorization': 'Bearer ' + gateway_token}
    probe_id = 'async-shadow-header-' + uuid.uuid4().hex
    start = clock()
    result: dict[str, Any] = dict(status='BLOCKED', header_bytes=len(header.encode('ascii')),
        base_url=base_url, probe_id=probe_id, authorization_header_bytes=len(headers['Authorization'].encode('ascii')), checklist=dict(header_acceptance='BLOCKED',
        pilot_identity='BLOCKED', synchronous_zero_refund='BLOCKED'))
    try:
        response = client.post(base_url.rstrip('/') + '/internal/gateway/authorize', headers=headers,
            json=dict(api_key_lookup_hash=lookup_hash_api_key(key), model=model,
                      estimated_input_tokens=0, max_output_tokens=1, route_type='chat.completions',
                      idempotency_key=probe_id))
    except httpx.HTTPError:
        return {**result, 'cleanup': 'UNKNOWN: reconcile authorize using probe_id; do not create another hold'}
    result.update(authorize_status=response.status_code, authorize_latency_ms=(clock() - start) * 1000,
                  authorization_bytes=len(response.content))
    if response.status_code != 200:
        return result
    result['checklist']['header_acceptance'] = 'PASS'
    try:
        data = response.json()['data']
        aid = data['authorization_id']
        if not isinstance(aid, str) or not aid:
            raise ValueError('authorization identity')
    except (ValueError, KeyError, TypeError):
        return {**result, 'cleanup': 'UNKNOWN: reconcile authorize using probe_id; do not create another hold'}
    if (data.get('workspace_id') == PILOT
            and data.get('api_key_hash') == 'key_1ZXjS8vNqWdQ7qRUkZj8Meuj'):
        result['checklist']['pilot_identity'] = 'PASS'
    # Legacy refund is the documented synchronous zero-charge terminal path.
    result['authorization_id'] = aid
    try:
        refund = client.post(base_url.rstrip('/') + '/internal/gateway/refund',
            headers={'Authorization': 'Bearer ' + gateway_token},
            json=dict(authorization_id=aid, actual_input_tokens=0, actual_output_tokens=0))
    except httpx.HTTPError:
        return {**result, 'cleanup': 'UNKNOWN: retry synchronous refund for authorization_id'}
    result.update(authorization_id=aid, refund_status=refund.status_code)
    if refund.status_code == 200:
        try:
            terminal = refund.json()['data']
            if not isinstance(terminal, dict):
                raise ValueError('refund response')
        except (ValueError, KeyError, TypeError):
            return {**result, 'cleanup': 'UNKNOWN: retry synchronous refund for authorization_id'}
        if (terminal.get('authorization_id') == aid
                and terminal.get('finalization_outcome') == 'refunded'
                and terminal.get('cost_microdollars') == 0):
            result['checklist']['synchronous_zero_refund'] = 'PASS'
    if all(value == 'PASS' for value in result['checklist'].values()):
        result['status'] = 'PASS'
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('shadow-serving', 'fleet-budgets', 'pre-flip', 'post-flip'):
        command = commands.add_parser(name)
        command.add_argument('--bundle', type=Path, required=True)
        command.add_argument('--live', action='store_true', help='Read explicit bundle keys from the named billing DB')
        for field in ('project', 'instance', 'database'):
            command.add_argument('--' + field, default='')
    probe = commands.add_parser('header-probe')
    probe.add_argument('--base-url', required=True, help='Actual reviewed internal hop base including /v1')
    probe.add_argument('--model', required=True)
    probe.add_argument('--pilot-key-env', required=True)
    probe.add_argument('--gateway-token-env', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'header-probe':
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                result = header_probe(args.base_url, args.model, os.environ[args.pilot_key_env],
                                      os.environ[args.gateway_token_env], client)
        else:
            bundle = json.loads(args.bundle.read_text())
            if args.live:
                database = live_database(args.project, args.instance, args.database)
                if args.command == 'post-flip':
                    bundle['outbox'] = workspace_outbox(database, PILOT, 1000)
                else:
                    bundle['rows'] = point_evidence(database, bundle['evidence_keys'])
            result = {'shadow-serving': shadow_serving, 'fleet-budgets': fleet_budgets,
                      'pre-flip': pre_flip, 'post-flip': post_flip}[args.command](bundle)
    except Exception:
        # Exception text can contain credentials or request bodies.
        result = checklist({'complete_valid_evidence_or_probe_cleanup': False})
    result['completed_at_us'] = int(time.time() * 1e6)
    print(json.dumps(result, indent=2, allow_nan=False))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
