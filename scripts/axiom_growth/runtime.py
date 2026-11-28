"""Scheduled, metadata-only growth export. Never imports the application STORE."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import io as streams
import json
import os
import time
from itertools import chain
from urllib.parse import quote

import google.auth
import httpx
from google.auth.transport.requests import Request

from scripts.axiom_growth import gateway_attempts
from scripts.axiom_growth import model as m
from scripts.axiom_growth import source_windows as windows

MAX_EVENTS = 300_000
MAX_DAILY = 100_000
MAX_GATEWAY_WINDOWS = 12
MAX_GATEWAY_PAGES = 20
GATEWAY_SECONDS = 60
INGEST_BATCH = 500
INGEST_BYTES = 1024 * 1024
MAX_STATE_BYTES = 180_000_000
MAX_COMPRESSED_BYTES = 40_000_000

# build_journeys recomputes these from the retained source facts on every load.
_DERIVED_DAILY = frozenset(('account_fingerprint', 'anonymous_fingerprint', 'identity_link_status',
    'utm_source', 'utm_medium', 'utm_campaign', 'landing_path', 'creative_id', 'experiment_id',
    'experiment_cell_id'))


def checkpoint_facts(state):
    return {**state, 'daily': [{k: v for k, v in row.items() if k not in _DERIVED_DAILY}
                               for row in state['daily']]}


def ingest_payloads(rows):
    batch = []
    size = 2  # JSON array brackets
    for row in rows:
        item = json.dumps(row, separators=(',', ':'), allow_nan=False).encode('ascii')
        if len(item) + 2 > INGEST_BYTES:
            raise ValueError('Single growth row exceeds ingest byte budget')
        if batch and (len(batch) == INGEST_BATCH or size + 1 + len(item) > INGEST_BYTES):
            yield len(batch), b'[' + b','.join(batch) + b']'
            batch, size = [], 2
        size += bool(batch) + len(item)
        batch.append(item)
    if batch:
        yield len(batch), b'[' + b','.join(batch) + b']'


def pack_checkpoint(value):
    """Write the existing gzip/JSON format without simultaneous full JSON text and bytes."""
    packed = streams.BytesIO()
    size = 0
    parts = []
    pending = 0
    with gzip.GzipFile(fileobj=packed, mode='wb', compresslevel=3, mtime=0) as dest:
        for part in json.JSONEncoder(separators=(',', ':'), ensure_ascii=True).iterencode(value):
            size += len(part)  # ASCII JSON: characters and bytes have identical lengths.
            if size > MAX_STATE_BYTES:
                raise ValueError('State size limit exceeded')
            parts.append(part)
            pending += len(part)
            if pending >= 64 * 1024:
                dest.write(''.join(parts).encode('ascii'))
                parts.clear()
                pending = 0
                if packed.tell() > MAX_COMPRESSED_BYTES:
                    raise ValueError('Compressed state limit exceeded')
        if parts:
            dest.write(''.join(parts).encode('ascii'))
    if packed.tell() > MAX_COMPRESSED_BYTES:
        raise ValueError('Compressed state limit exceeded')
    return packed.getvalue(), size


def complete(result):
    status = result.get('status', {})
    if status.get('isPartial') or status.get('isEstimate') or any(
        x.get('code') in {'default_limit_warning', 'max_limit_warning'}
        for x in status.get('messages', [])
    ):
        raise ValueError('Incomplete Axiom source result')
    return m.rows(result)


def project(row, source):
    return m.project_event(row, source=source)


def content_hash(row):
    # Progress timestamps must not cause every unchanged journey to be reingested.
    body = {k: v for k, v in row.items() if k not in {'exported_at', 'observed_through', 'billing_owner_observed_at'}}
    return m.digest(json.dumps(body, sort_keys=True, separators=(',', ':')))


def collect_gateway(state, io, now, end, journeys, daily, owners):
    """Replay complete windows only, within the original per-run source budget."""
    candidates = {}

    def project_audits(checkpoint, audits, observed_through):
        updated = copy.deepcopy(journeys)
        attempts = gateway_attempts.snapshots(audits, updated, observed_through)
        attempts = gateway_attempts.pre_activation_attempts(attempts, daily, updated)
        if owners is not None:
            m.link_billing_accounts(attempts, owners, updated)
        gateway_attempts.link_first_attempts(updated, attempts)
        gateway_attempts.preserve_milestones(checkpoint, updated)
        return updated, attempts

    status = 'unavailable'
    complete_windows = 0
    try:
        # Preserve evidence already in a stalled checkpoint before any pruning.
        updated, attempts = project_audits(state, state.get('gateway_audits', []),
            m.date(state.get('gateway_watermark', end.isoformat())))
        journeys[:] = updated
        candidates.update({r['event_id']: r for r in attempts})
        for _ in range(MAX_GATEWAY_WINDOWS):
            checkpoint = dict(state)
            audits = gateway_attempts.collect(checkpoint, io, now, end)
            observed = m.date(checkpoint['gateway_watermark'])
            updated, attempts = project_audits(checkpoint, audits, observed)
            state.update(gateway_audits=checkpoint['gateway_audits'],
                         gateway_watermark=checkpoint['gateway_watermark'],
                         gateway_settled_through=checkpoint['gateway_settled_through'],
                         gateway_milestones=checkpoint['gateway_milestones'])
            journeys[:] = updated
            candidates.update({r['event_id']: r for r in attempts})
            complete_windows += 1
            status = 'current' if observed >= end else 'catching_up'
            if status == 'current':
                state.pop('gateway_recovery_seconds', None)
                break
        if complete_windows == MAX_GATEWAY_WINDOWS and 'gateway_recovery_seconds' in state:
            state['gateway_recovery_seconds'] = min(int(gateway_attempts.RECOVERY_STEP.total_seconds()),
                                                    2 * state['gateway_recovery_seconds'])
    except Exception as exc:  # noqa: BLE001 - acquisition keeps running during audit-source failure.
        # A partial window never advances its cursor. Earlier complete windows
        # may commit, but only after the shared ingest and checkpoint succeed.
        if isinstance(exc, gateway_attempts.DenseHistoricalWindow) and not complete_windows:
            # A full, fresh budget failed. Persist only a narrower query plan, never partial rows.
            state['gateway_recovery_seconds'] = exc.retry_seconds
            print(json.dumps({'event': 'growth.gateway_replanned', 'retry_seconds': exc.retry_seconds}))
        if not isinstance(exc, gateway_attempts.AuditBudgetExceeded):
            status = 'unavailable'
        print(json.dumps({'event': 'growth.gateway_collection_failed',
                          'error_class': type(exc).__name__}), flush=True)
    return list(candidates.values()), status


def prepare_cycle(state, io, now):
    # Historical event and audit rows are immutable. Usage rows below are
    # copy-on-write because attribution annotates them during projection.
    state = dict(state)
    previous = m.date(state['watermark'])
    end = min(now - dt.timedelta(minutes=1), previous + dt.timedelta(days=1))
    if previous < now - dt.timedelta(days=29):
        raise ValueError('Watermark older than source retention; explicit recovery required')
    if end <= previous:
        return state, None
    repair = state.get('repair_day') != now.date().isoformat()
    if state.get('source_windows'):
        start, source_end, end, incoming, fresh_daily, replaced = windows.read(state, io, previous, end, now)
    else:
        start = previous - dt.timedelta(hours=24 if repair else 1)
        source_end = end
        incoming = io.events(start, end)
        if len(incoming) > m.MAX_ROWS:
            raise windows.Capacity('events', 'Event budget exceeded')
        usage_start = (end - dt.timedelta(days=30 if repair else 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        fresh_daily = io.usage(usage_start, end)
        def replaced(row):
            return m.date(row['_time']) >= usage_start
        state['repair_day'] = now.date().isoformat()
    cutoff = now - dt.timedelta(days=365)
    events = {r['event_id']: r for r in state['events'] if m.date(r['_time']) >= cutoff}
    for row in incoming:
        if not start <= m.date(row['_time']) < source_end:
            raise ValueError('Event outside requested interval')
        # Preserve previously recovered domain evidence on an overlapping refresh.
        prior = events.get(row['event_id'], {})
        if not row.get('customer_domain') and prior.get('customer_domain') and (
                not row.get('account_fingerprint') or row.get('account_fingerprint') == prior.get('account_fingerprint')):
            for key in ('customer_domain', 'domain_observed_at', 'customer_domain_basis', 'customer_domain_verified'):
                if key in prior:
                    row[key] = prior[key]
        for key in ('first_referrer_domain', *m.IDENTITY_FIELDS):
            if prior.get(key) and (not row.get(key) or row.get(key) == 'not_linked'):
                row[key] = prior[key]
        events[row['event_id']] = row
    if len(events) > MAX_EVENTS:
        raise ValueError('State bound reached; partition before continuing')
    daily = {r['event_id']: dict(r) for r in state['daily'] if cutoff <= m.date(r['_time']) and not replaced(r)}
    daily.update({r['event_id']: r for r in fresh_daily})
    if len(daily) > MAX_DAILY:
        raise ValueError('Usage state bound reached')
    daily_rows = list(daily.values())
    journeys = m.build_journeys(list(events.values()), daily_rows, end)
    owners = None
    billing_status = 'disabled'
    if os.environ.get('GROWTH_BILLING_OWNERS_ENABLED') == 'true':
        try:
            owners = io.billing_owners()
            m.link_billing_accounts(daily_rows, owners, journeys)
            state['billing_owners'] = owners
            billing_status = 'current'
        except Exception as exc:  # noqa: BLE001 - a directory outage must not stop conversion export.
            owners = state.get('billing_owners', [])
            m.link_billing_accounts(daily_rows, owners, journeys)
            billing_status = 'stale' if owners else 'unavailable'
            print(json.dumps({'event': 'growth.billing_directory_failed',
                              'error_class': type(exc).__name__}), flush=True)
    gateway_status = 'disabled'
    attempts = []
    if os.environ.get('GROWTH_GATEWAY_ATTEMPTS_ENABLED') == 'true':
        attempts, gateway_status = collect_gateway(state, io, now, end, journeys, daily_rows, owners)
    gateway_attempts.preserve_milestones(state, journeys)
    candidates = chain(events.values(), daily_rows, journeys, attempts)
    hashes = {}
    changed = []
    count = 0
    for row in m.with_verified_domains(events.values(), journeys, candidates):
        key = row['event_id']
        hashes[key] = content_hash(row)
        if hashes[key] != state.get('hashes', {}).get(key):
            changed.append({**row, 'exported_at': now.isoformat()})
            count += 1
            if len(changed) == INGEST_BATCH:
                io.ingest(changed)
                changed = []
    if changed:
        io.ingest(changed)
    heartbeat = {'_time': now.isoformat(), 'event': 'growth.sync_completed',
                 'event_id': m.digest('sync:' + end.isoformat()),
                 'exported_at': now.isoformat(), 'observed_through': end.isoformat(),
                 'event_rows': len(events), 'usage_rows': len(daily_rows),
                 'journey_rows': len(journeys), 'changed_rows': count,
                 'source': 'scheduled_incremental', 'cloud_scope': 'gcp',
                 'schema_version': 6, 'gateway_status': gateway_status,
                 'billing_owner_status': billing_status,
                 'gateway_observed_through': state.get('gateway_watermark'),
                 'billing_owners_observed_through': max((r.get('billing_owner_observed_at', '') for r in owners or []), default='')}
    state.update(events=list(events.values()), daily=daily_rows, hashes=hashes,
                 watermark=end.isoformat())
    if plan := state.get('source_windows'):
        heartbeat.update(source_window_mode='segmented', acquisition_repair_next=plan['event_next'],
                         usage_repair_next=plan['usage_next'], usage_live_day=plan.get('last_live_usage_day'))
        heartbeat['event_id'] = m.digest('sync:' + now.isoformat() + ':' + end.isoformat())
    return state, heartbeat


def cycle(state, io, now, *, generation):
    """Publish success only after accepted data AND a durable compare-and-swap."""
    try:
        updated, heartbeat = prepare_cycle(state, io, now)
    except windows.Capacity as exc:
        updated = {**state, 'source_windows': windows.narrower(state.get('source_windows'),
                                                               m.date(state['watermark']), exc)}
        io.save('state.json.gz', updated, generation)
        print(json.dumps({'event': 'growth.source_replanned', 'source': exc.source,
                          'observed_through': state['watermark']}), flush=True)
        return updated, 0
    if heartbeat is None:
        return updated, 0
    io.save('state.json.gz', updated, generation)
    io.ingest([heartbeat])
    return updated, heartbeat['changed_rows']


class Sources:
    def __init__(self):
        self.http = httpx.Client(timeout=60, follow_redirects=False)
        self.credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        self.project = os.environ.get('GOOGLE_CLOUD_PROJECT', 'quill-cloud-proxy')
        self.bucket = os.environ['GROWTH_STATE_BUCKET']
        self.headers = {'Authorization': 'Bearer ' + os.environ['GROWTH_AXIOM_TOKEN']}
        self.edge = 'https://' + m.EDGE_REGION

    def google(self, method, url, **kwargs):
        if not self.credentials.valid:
            self.credentials.refresh(Request())
        return self.checked(self.http.request(method, url, headers={
            'Authorization': 'Bearer ' + self.credentials.token}, **kwargs))

    @staticmethod
    def checked(response):
        if response.status_code >= 400:
            # Do not log response bodies, request headers, source rows or credentials.
            print(json.dumps({'event': 'growth.upstream_failed',
                              'upstream': response.request.url.host,
                              'http_status': response.status_code}), flush=True)
            raise RuntimeError(f'Analytics upstream HTTP {response.status_code}')
        return response

    def query(self, apl, start, end):
        result = self.checked(self.http.post(self.edge + '/v1/query/_apl?format=tabular',
            headers=self.headers, json={'apl': apl, 'startTime': start.isoformat(),
                                       'endTime': end.isoformat()})).json()
        return complete(result)

    def events(self, start, end):
        # Recovery deploys must not opt a dataset into raw website IP collection.
        fields = tuple(f for f in m.FIELDS if f != 'website_client_ip'
                       or os.environ.get('GROWTH_WEBSITE_IP_ENABLED') == 'true')
        def default(field):
            if field == 'customer_domain_verified':
                return 'false'
            return '0' if field in {'amount_microdollars', 'http_status', 'elapsed_ms'} else "''"
        projection = ', '.join(f"{f}=column_ifexists('{f}', {default(f)})" for f in fields)
        filters = ','.join(repr(e) for e in sorted(m.CONVERSION_EVENTS))
        base = f"['{m.SOURCE_DATASET}'] | where event in ({filters})"
        pending, rows, queries = [(start, end)], [], 0
        while pending:
            left, right = pending.pop()
            queries += 2
            if queries > 126:
                raise windows.Capacity('events', 'Query budget exceeded')
            count = self.query(base + ' | summarize n=count()', left, right)[0]['n']
            if count >= 1000:
                if right - left < dt.timedelta(seconds=1):
                    raise windows.Capacity('events', 'Source too dense for bounded query')
                mid = left + (right - left)/2
                pending.extend([(left, mid), (mid, right)])
                continue
            batch = self.query(base + f' | project _time, {projection} | take 1000', left, right)
            if len(batch) != count:
                raise ValueError('Source changed or truncated; retry window')
            rows.extend(project(row, 'axiom') for row in batch)
            if len(rows) > m.MAX_ROWS:
                raise windows.Capacity('events', 'Source row cap exceeded')
        browser = ' OR '.join(f'jsonPayload.event="{e}"' for e in sorted(m.BROWSER_EVENTS))
        log_filter = ('resource.type="cloud_run_revision" '
            f'{m.ACQUISITION_SERVICE_FILTER} '
            f'({browser}) timestamp>="{start.isoformat()}" timestamp<"{end.isoformat()}"')
        view = f'projects/{self.project}/locations/global/buckets/tr-growth-source/views/_AllLogs'
        body = {'resourceNames': [view], 'filter': log_filter,
                'pageSize': 1000, 'orderBy': 'timestamp asc'}
        for _ in range(25):
            result = self.google('POST', 'https://logging.googleapis.com/v2/entries:list', json=body).json()
            for entry in result.get('entries', []):
                raw = entry['jsonPayload']
                row = {f: raw.get(f) for f in fields}
                rows.append(project({**row, '_time': entry['timestamp']}, 'cloud_logging'))
            if len(rows) > m.MAX_ROWS:
                raise windows.Capacity('events', 'Source row cap exceeded')
            if not result.get('nextPageToken'):
                return rows
            body['pageToken'] = result['nextPageToken']
        raise windows.Capacity('events', 'Cloud Logging pagination budget exceeded')

    def usage(self, start, end, *, bucket=(0, windows.SPACE)):
        lo, hi = bucket
        if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo < hi <= windows.SPACE):
            raise ValueError('Invalid usage shard interval')
        extra = ''
        if bucket != (0, windows.SPACE):
            # Same first two big-endian SHA bytes as windows.usage_bucket.
            b = "lower(substring(hex(SHA256(concat(workspace_fingerprint, ':', model, ':', provider))), 1, 4))"
            extra = f" AND {b} >= '{lo:04x}'"
            if hi < windows.SPACE:
                extra += f" AND {b} < '{hi:04x}'"
        sql = f"""SELECT toString(day, 'UTC') AS usage_day, workspace_fingerprint,
        marketing_workspace_fingerprint, model, provider, successful_calls,
        input_tokens, output_tokens, usage_microdollars,
        toString(first_call_at, 'UTC') AS first_call_at, toString(last_call_at, 'UTC') AS last_call_at
        FROM tr.growth_daily_usage
        WHERE day >= toDateTime('{start:%Y-%m-%d %H:%M:%S}', 'UTC')
        AND day < toStartOfDay(toDateTime('{end:%Y-%m-%d %H:%M:%S}', 'UTC')) + INTERVAL 1 DAY
        {extra} LIMIT 50001 FORMAT JSONEachRow"""  # noqa: S608 - bounded integers and aware datetime values only.
        result = self.checked(self.http.post('http://10.128.0.96:8123', content=sql,
            auth=('tr_growth_read', os.environ['GROWTH_CH_PASSWORD']),
            params={'readonly': 1, 'max_execution_time': 45, 'max_threads': 2,
                    'max_memory_usage': 536870912})).text
        values = [json.loads(line) for line in result.splitlines() if line]
        if len(values) > 50000:
            raise windows.Capacity('usage', 'ClickHouse aggregate cap exceeded')
        for row in values:
            for f in ('successful_calls', 'input_tokens', 'output_tokens', 'usage_microdollars'):
                row[f] = int(row[f])
            row['_time'] = row.pop('usage_day').replace(' ', 'T') + 'Z'
            for f in ('first_call_at', 'last_call_at'):
                row[f] = row[f].replace(' ', 'T') + 'Z'
            row.update(event='growth.daily_usage', record_type='daily_usage_snapshot',
                       source='clickhouse_gcp', schema_version=3)
            row['event_id'] = m.digest('|'.join(str(row[k]) for k in
                ('event', '_time', 'workspace_fingerprint', 'model', 'provider')))
        return values

    def gateway_audits(self, start, end):
        if not hasattr(self, 'gateway_deadline'):
            self.gateway_deadline = time.monotonic() + GATEWAY_SECONDS
            self.gateway_pages = 0
        view = f'projects/{self.project}/locations/global/buckets/tr-growth-source/views/_AllLogs'
        body = {'resourceNames': [view], 'filter': gateway_attempts.source_filter() +
                f' timestamp>="{start.isoformat()}" timestamp<"{end.isoformat()}"',
                'pageSize': 1000, 'orderBy': 'timestamp asc'}
        rows = []
        while self.gateway_pages < MAX_GATEWAY_PAGES:
            remaining = self.gateway_deadline - time.monotonic()
            if remaining <= 0:
                raise gateway_attempts.AuditBudgetExceeded('Gateway audit time budget exceeded')
            self.gateway_pages += 1
            result = self.google('POST', 'https://logging.googleapis.com/v2/entries:list',
                                 json=body, timeout=min(10, remaining)).json()
            for entry in result.get('entries', []):
                row = gateway_attempts.project(entry)
                if row is not None:
                    rows.append(row)
            if not result.get('nextPageToken'):
                return rows
            body['pageToken'] = result['nextPageToken']
        raise gateway_attempts.AuditBudgetExceeded('Gateway audit pagination budget exceeded')

    def billing_owners(self):
        sql = """SELECT workspace_fingerprint, billing_account_fingerprint,
            toString(billing_owner_observed_at, 'UTC') AS billing_owner_observed_at
            FROM tr.growth_billing_owners LIMIT 50001 FORMAT JSONEachRow"""
        # readonly=1 locks settings. Keep the role's server-enforced resource
        # caps instead of trying to override them through HTTP query parameters.
        result = self.checked(self.http.post('http://10.128.0.96:8123', content=sql,
            auth=('tr_growth_read', os.environ['GROWTH_CH_PASSWORD']),
            params={'readonly': 1})).text
        rows = [json.loads(line) for line in result.splitlines() if line]
        if len(rows) > 50_000:
            raise ValueError('Billing owner row cap exceeded')
        for row in rows:
            row['billing_owner_observed_at'] = row['billing_owner_observed_at'].replace(' ', 'T') + 'Z'
        return rows

    def ingest(self, values):
        forbidden = {'email', 'workspace_id', 'user_id', 'api_key', 'prompt', 'output',
                     'body', 'request', 'headers', 'ip', 'textPayload', 'jsonPayload'}
        for row in values:
            if forbidden.intersection(row):
                raise ValueError('Private field in growth projection')
            for key, value in row.items():
                if key.endswith('_fingerprint') and value and not m.HASH.fullmatch(str(value)):
                    raise ValueError('Invalid growth fingerprint')
        for count, body in ingest_payloads(values):
            headers = {**self.headers, 'Content-Type': 'application/json'}
            if len(body) >= 4096:
                body = gzip.compress(body, compresslevel=1, mtime=0)
                headers['Content-Encoding'] = 'gzip'
            result = self.checked(self.http.post(self.edge + '/v1/ingest/' + m.DATASET,
                                  headers=headers, content=body)).json()
            if result.get('failed') != 0 or result.get('ingested') != count:
                raise ValueError('Axiom did not accept the complete batch')

    def load(self, name):
        path = f'https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe="")}'
        meta = self.google('GET', path).json()
        data = self.google('GET', path, params={'alt': 'media', 'generation': meta['generation']}).content
        if len(data) > MAX_COMPRESSED_BYTES:
            raise ValueError('Compressed state limit exceeded')
        with gzip.GzipFile(fileobj=streams.BytesIO(data)) as src:
            raw = src.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('State size limit exceeded')
        return json.loads(raw), meta['generation']

    def save(self, name, value, generation):
        data, raw_size = pack_checkpoint(checkpoint_facts(value) if name == 'state.json.gz' else value)
        result = self.google('POST', f'https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o',
            params={'name': name, 'uploadType': 'media', 'ifGenerationMatch': generation},
            content=data).json()
        if name == 'state.json.gz':
            events, daily = len(value['events']), len(value['daily'])
            utilization = max(raw_size / MAX_STATE_BYTES, len(data) / MAX_COMPRESSED_BYTES,
                              events / MAX_EVENTS, daily / MAX_DAILY)
            print(json.dumps({'event': 'growth.checkpoint_committed',
                              'severity': 'WARNING' if utilization >= 0.7 else 'INFO',
                              'observed_through': value['watermark'], 'state_bytes': raw_size,
                              'compressed_bytes': len(data), 'event_rows': events,
                              'usage_rows': daily, 'max_limit_pct': round(100 * utilization, 1)}),
                  flush=True)
        return result['generation']


def main():
    started = time.monotonic()
    io = Sources()
    now = dt.datetime.now(dt.UTC)
    lock, version = io.load('lock.json.gz')
    if m.date(lock['until']) > now:
        print(json.dumps({'event': 'growth.sync_skipped', 'reason': 'lease_active'}))
        return
    version = io.save('lock.json.gz', {'until': (now + dt.timedelta(minutes=10)).isoformat()}, version)
    try:
        state, generation = io.load('state.json.gz')
        updated, changed = cycle(state, io, now, generation=generation)
        event = 'growth.sync_success' if updated.get('watermark') != state.get('watermark') else 'growth.sync_no_forward_progress'
        print(json.dumps({'event': event, 'changed_rows': changed,
                          'observed_through': updated['watermark'],
                          'seconds': round(time.monotonic()-started, 2)}))
    finally:
        io.save('lock.json.gz', {'until': now.isoformat()}, version)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'severity': 'ERROR', 'event': 'growth.sync_failed',
                          'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
