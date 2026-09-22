"""Project existing gateway audits into content-free, matched HTTP attempts.

This is an off-path GCP collector, not a new inference logger. HTTP 200 is
deliberately not called a successful generation: a stream can fail after headers.
"""
import datetime as dt
import json
import re
import shlex
from collections import defaultdict

from scripts.axiom_growth import model as m

ROUTES = ('/v1/chat/completions', '/v1/responses', '/v1/messages',
          '/v1/embeddings', '/v1/images/generations', '/v1/audio/speech')
MAX_AUDITS = 20_000
RETENTION_DAYS = 3


def source_filter():
    names = ' OR '.join('jsonPayload.MESSAGE:' + json.dumps('enclave.request_' + s + ' ')
                       for s in ('start', 'end'))
    routes = ' OR '.join('jsonPayload.MESSAGE:' + json.dumps(r) for r in ROUTES)
    return f'resource.type="gce_instance" ({names}) ({routes})'


def project(entry):
    """Discard the entire operational payload after selecting fixed metadata."""
    message = entry.get('jsonPayload', {}).get('MESSAGE', '')
    match = re.search(r'\benclave\.request_(start|end) ', message) if isinstance(message, str) else None
    if not match or len(message) > 8192:
        return None
    try:
        fields = dict(part.split('=', 1) for part in shlex.split(message[match.end():]))
    except ValueError:
        return None
    if fields.get('route') not in ROUTES or fields.get('method') != 'POST':
        return None
    request_id = fields.get('request_log_id', '')
    instance = entry.get('resource', {}).get('labels', {}).get('instance_id', '')
    if not request_id or len(request_id) > 128 or not instance:
        return None
    stage = match[1]
    attempt = m.digest('gateway-attempt:gcp:' + str(instance) + ':' + request_id)
    workspace = fields.get('workspace_id', '')
    credential = fields.get('credential_fingerprint', '')
    row = {'_time': m.timestamp(entry.get('timestamp')), 'stage': stage,
           'attempt_id': attempt, 'event_id': m.digest(attempt + ':' + stage),
           'route': fields['route'], 'cloud_scope': 'gcp',
           'workspace_fingerprint': m.digest(workspace) if workspace else '',
           'marketing_workspace_fingerprint': m.digest('tr-marketing-workspace:' + workspace) if workspace else '',
           'credential_fingerprint': m.digest('tr-marketing-credential:' + credential) if m.HASH.fullmatch(credential) else ''}
    if stage == 'end':
        try:
            status, elapsed = int(fields.get('status', '0')), int(fields.get('elapsed_ms', '0'))
        except ValueError:
            return None
        if status not in range(100, 600) and status != 0 or not 0 <= elapsed <= 86_400_000:
            return None
        row.update(http_status=status, elapsed_ms=elapsed)
    return row


def snapshots(audits, journeys, observed_through):
    grouped = defaultdict(list)
    owners = defaultdict(set)
    credentials = defaultdict(set)
    for j in journeys:
        if j.get('account_fingerprint') and j.get('workspace_fingerprint'):
            owners[j['workspace_fingerprint']].add(j['account_fingerprint'])
    for row in audits:
        grouped[row['attempt_id']].append(row)
        if row.get('credential_fingerprint') and row.get('workspace_fingerprint'):
            credentials[row['credential_fingerprint']].add(row['workspace_fingerprint'])
    result = []
    for attempt, group in grouped.items():
        starts = [r for r in group if r['stage'] == 'start']
        ends = [r for r in group if r['stage'] == 'end']
        start = min(starts, key=lambda r: m.date(r['_time'])) if starts else None
        end = max(ends, key=lambda r: m.date(r['_time'])) if ends else None
        row = end or start
        workspaces = {r['workspace_fingerprint'] for r in group if r['workspace_fingerprint']}
        if not workspaces:
            workspaces = set().union(*(credentials.get(r.get('credential_fingerprint'), set()) for r in group))
        accounts = set().union(*(owners[w] for w in workspaces))
        status = (end or {}).get('http_status', 0)
        conflict = len(workspaces) > 1 or len({r.get('http_status') for r in ends}) > 1
        if start and end and m.date(start['_time']) > m.date(end['_time']):
            conflict = True
        paired = bool(start and end and not conflict)
        outcome = ('conflicting' if conflict else 'missing_start' if not start else
                   'pending' if not end else 'http_error' if status >= 400 else
                   'http_response' if status >= 200 else 'connection_closed')
        result.append({
            '_time': (start or end)['_time'], 'event': 'growth.gateway_attempt',
            'event_id': m.digest('gateway-attempt-snapshot:' + attempt),
            'record_type': 'gateway_attempt_snapshot', 'schema_version': 6,
            'attempt_id': attempt, 'route': row['route'], 'cloud_scope': 'gcp',
            'started_at': start['_time'] if start else None,
            'finished_at': end['_time'] if end else None,
            'http_status': status, 'elapsed_ms': (end or {}).get('elapsed_ms', 0),
            'paired': paired, 'gateway_outcome': outcome,
            'workspace_fingerprint': next(iter(workspaces)) if len(workspaces) == 1 else '',
            'account_fingerprint': next(iter(accounts)) if len(accounts) == 1 and not conflict else '',
            'identity_link_status': 'ambiguous' if len(accounts) > 1 or conflict else 'linked' if accounts else 'orphaned',
            'observed_through': observed_through.isoformat(),
        })
    return result


def collect(state, io, now, end):
    """Short overlap, separate bounded checkpoint. Never fabricate a lost start."""
    start = m.date(state.get('gateway_watermark', end.isoformat())) - dt.timedelta(minutes=10)
    if end - start > dt.timedelta(hours=1):
        raise ValueError('Gateway audit backlog exceeds one hour; explicit recovery required')
    incoming = io.gateway_audits(start, end)
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    recent = state.get('gateway_audits', [])
    completed = {r['attempt_id'] for r in recent if r['stage'] == 'end'
                 and m.date(r['_time']) < now - dt.timedelta(minutes=30)}
    rows = {r['event_id']: r for r in recent if m.date(r['_time']) >= cutoff
            and r['attempt_id'] not in completed}
    for row in incoming:
        if not start <= m.date(row['_time']) < end:
            raise ValueError('Gateway audit outside requested interval')
        prior = rows.get(row['event_id'])
        # Duplicate delivery is normal. Conflicting end records must stay visible.
        if prior and prior != row:
            row = {**row, 'event_id': m.digest(row['event_id'] + json.dumps(row, sort_keys=True))}
        rows[row['event_id']] = row
    if len(rows) > MAX_AUDITS:
        raise ValueError('Gateway audit state bound reached; partition before continuing')
    state.update(gateway_audits=list(rows.values()), gateway_watermark=end.isoformat())
    return list(rows.values())


def pre_activation_attempts(attempts, daily):
    """Established usage is not acquisition telemetry. Keep the first-call window."""
    successful_at = {}
    for row in daily:
        workspace, first = row.get('workspace_fingerprint'), row.get('first_call_at')
        if workspace and first:
            when = m.date(first)
            successful_at[workspace] = min(when, successful_at.get(workspace, when))
    return [row for row in attempts if row['workspace_fingerprint'] and
            (row['workspace_fingerprint'] not in successful_at or
             m.date(row['started_at'] or row['finished_at']) <= successful_at[row['workspace_fingerprint']])]


def link_first_attempts(journeys, attempts):
    """Only observed starts between signup and the first recorded settlement."""
    by_workspace = defaultdict(list)
    for attempt in attempts:
        if attempt.get('workspace_fingerprint') and attempt.get('started_at'):
            by_workspace[attempt['workspace_fingerprint']].append(attempt)
    for journey in journeys:
        signup = journey.get('signup_completed_at')
        success = journey.get('first_successful_api_call_at')
        eligible = [r for r in by_workspace.get(journey.get('workspace_fingerprint'), [])
                    if signup and m.date(r['started_at']) >= m.date(signup)
                    and (not success or m.date(r['started_at']) <= m.date(success))]
        if not eligible:
            continue
        first = min(eligible, key=lambda r: m.date(r['started_at']))
        journey['first_gateway_attempt_at'] = first['started_at']
        failures = [r for r in eligible if r['paired'] and r['http_status'] >= 400]
        if failures:
            failure = min(failures, key=lambda r: m.date(r['started_at']))
            journey['first_gateway_http_error_at'] = failure['finished_at']
            journey['first_gateway_http_status'] = failure['http_status']


def preserve_milestones(state, journeys):
    """Compact milestones outlive the short audit-pair buffer, with identity checks."""
    retained = state.get('gateway_milestones', {})
    updated = {}
    for journey in journeys:
        key = journey['event_id']
        prior = retained.get(key, {})
        identity = {f: journey.get(f, '') for f in ('account_fingerprint', 'workspace_fingerprint')}
        if any(prior.get(f, '') != value for f, value in identity.items()):
            prior = {}
        for name in ('first_gateway_attempt_at', 'first_gateway_http_error_at'):
            old, new = prior.get(name), journey.get(name)
            if old and (not new or m.date(old) < m.date(new)):
                journey[name] = old
                if name == 'first_gateway_http_error_at':
                    journey['first_gateway_http_status'] = prior['first_gateway_http_status']
        values = {f: journey[f] for f in ('first_gateway_attempt_at', 'first_gateway_http_error_at',
                                          'first_gateway_http_status') if f in journey}
        if values:
            updated[key] = {**identity, **values}
    if len(updated) > 50_000:
        raise ValueError('Gateway milestone bound exceeded')
    state['gateway_milestones'] = updated
