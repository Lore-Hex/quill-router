"""Scheduled, metadata-only growth export. Never imports the application STORE."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import json
import os
import time
from urllib.parse import quote

import google.auth
import httpx
from google.auth.transport.requests import Request

from scripts.axiom_growth import model as m

MAX_EVENTS = 300_000
EXTRAS = ('workspace_fingerprint', 'first_utm_source', 'first_utm_medium',
          'first_utm_campaign', 'first_landing_path', 'first_creative_id')


def complete(result):
    status = result.get('status', {})
    if status.get('isPartial') or status.get('isEstimate') or any(
        x.get('code') in {'default_limit_warning', 'max_limit_warning'}
        for x in status.get('messages', [])
    ):
        raise ValueError('Incomplete Axiom source result')
    return m.rows(result)


def project(row, source):
    item = m.project_event(row, source=source)
    item['landing_path'] = m.safe_path(row.get('landing_path'))
    if m.HASH.fullmatch(str(row.get('workspace_fingerprint', ''))):
        item['workspace_fingerprint'] = row['workspace_fingerprint']
    for field in EXTRAS[1:]:
        item[field] = (m.safe_path(row.get(field)) if field.endswith('path')
                       else m.safe_label(row.get(field)))
    return item


def content_hash(row):
    # Progress timestamps must not cause every unchanged journey to be reingested.
    body = {k: v for k, v in row.items() if k not in {'exported_at', 'observed_through'}}
    return m.digest(json.dumps(body, sort_keys=True, separators=(',', ':')))


def cycle(state, io, now):
    """Commit only after all batches succeed; failures leave the old watermark intact."""
    state = copy.deepcopy(state)
    previous = m.date(state['watermark'])
    end = min(now - dt.timedelta(minutes=1), previous + dt.timedelta(days=1))
    if previous < now - dt.timedelta(days=29):
        raise ValueError('Watermark older than source retention; explicit recovery required')
    if end <= previous:
        return state, 0
    repair = state.get('repair_day') != now.date().isoformat()
    start = previous - dt.timedelta(hours=24 if repair else 1)
    incoming = io.events(start, end)
    if len(incoming) > m.MAX_ROWS:
        raise ValueError('Event budget exceeded')
    cutoff = now - dt.timedelta(days=365)
    events = {r['event_id']: r for r in state['events'] if m.date(r['_time']) >= cutoff}
    for row in incoming:
        if not start <= m.date(row['_time']) < end:
            raise ValueError('Event outside requested interval')
        # Preserve previously recovered domain evidence on an overlapping refresh.
        prior = events.get(row['event_id'], {})
        for key in ('customer_domain', 'first_referrer_domain', 'domain_observed_at',
                    'customer_domain_basis', 'customer_domain_verified'):
            if prior.get(key) and (not row.get(key) or row.get(key) == 'not_linked'):
                row[key] = prior[key]
        events[row['event_id']] = row
    if len(events) > MAX_EVENTS:
        raise ValueError('State bound reached; partition before continuing')
    usage_start = (end - dt.timedelta(days=30 if repair else 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    fresh_daily = io.usage(usage_start, end)
    daily = {r['event_id']: r for r in state['daily'] if cutoff <= m.date(r['_time']) < usage_start}
    daily.update({r['event_id']: r for r in fresh_daily})
    if len(daily) > 100_000:
        raise ValueError('Usage state bound reached')
    daily_rows = list(daily.values())
    journeys = m.build_journeys(list(events.values()), daily_rows, end)
    candidates = list(events.values()) + daily_rows + journeys
    hashes = {r['event_id']: content_hash(r) for r in candidates}
    changed = [{**r, 'exported_at': now.isoformat()} for r in candidates
               if hashes[r['event_id']] != state.get('hashes', {}).get(r['event_id'])]
    io.ingest(changed)
    heartbeat = {'_time': now.isoformat(), 'event': 'growth.sync_completed',
                 'event_id': m.digest('sync:' + end.isoformat()),
                 'exported_at': now.isoformat(), 'observed_through': end.isoformat(),
                 'event_rows': len(events), 'usage_rows': len(daily_rows),
                 'journey_rows': len(journeys), 'changed_rows': len(changed),
                 'source': 'scheduled_incremental', 'cloud_scope': 'gcp',
                 'schema_version': 4}
    io.ingest([heartbeat])
    state.update(events=list(events.values()), daily=daily_rows, hashes=hashes,
                 watermark=end.isoformat(), repair_day=now.date().isoformat())
    return state, len(changed)


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
        fields = (*m.FIELDS, *EXTRAS)
        projection = ', '.join(f"{f}=column_ifexists('{f}', {0 if f == 'amount_microdollars' else chr(39)*2})" for f in fields)
        filters = ','.join(repr(e) for e in sorted(m.CONVERSION_EVENTS))
        base = f"['{m.SOURCE_DATASET}'] | where event in ({filters})"
        pending, rows, queries = [(start, end)], [], 0
        while pending:
            left, right = pending.pop()
            queries += 2
            if queries > 126:
                raise ValueError('Query budget exceeded')
            count = self.query(base + ' | summarize n=count()', left, right)[0]['n']
            if count >= 1000:
                if right - left < dt.timedelta(seconds=1):
                    raise ValueError('Source too dense for bounded query')
                mid = left + (right - left)/2
                pending.extend([(left, mid), (mid, right)])
                continue
            batch = self.query(base + f' | project _time, {projection} | take 1000', left, right)
            if len(batch) != count:
                raise ValueError('Source changed or truncated; retry window')
            rows.extend(project(row, 'axiom') for row in batch)
        browser = ' OR '.join(f'jsonPayload.event="{e}"' for e in sorted(m.BROWSER_EVENTS))
        log_filter = ('resource.type="cloud_run_revision" '
            '(resource.labels.service_name="trusted-router-public" OR resource.labels.service_name="trusted-router") '
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
                raise ValueError('Source row cap exceeded')
            if not result.get('nextPageToken'):
                return rows
            body['pageToken'] = result['nextPageToken']
        raise ValueError('Cloud Logging pagination budget exceeded')

    def usage(self, start, end):
        sql = f"""SELECT toString(day, 'UTC') AS day, workspace_fingerprint,
        marketing_workspace_fingerprint, model, provider, successful_calls,
        input_tokens, output_tokens, usage_microdollars,
        toString(first_call_at, 'UTC') AS first_call_at, toString(last_call_at, 'UTC') AS last_call_at
        FROM tr.growth_daily_usage
        WHERE day >= toDateTime('{start:%Y-%m-%d %H:%M:%S}', 'UTC')
        AND day < toStartOfDay(toDateTime('{end:%Y-%m-%d %H:%M:%S}', 'UTC')) + INTERVAL 1 DAY
        LIMIT 50001 FORMAT JSONEachRow"""  # noqa: S608 - aware datetime values only, never user SQL.
        result = self.checked(self.http.post('http://10.128.0.96:8123', content=sql,
            auth=('tr_growth_read', os.environ['GROWTH_CH_PASSWORD']),
            params={'readonly': 1, 'max_execution_time': 45, 'max_threads': 2,
                    'max_memory_usage': 536870912})).text
        values = [json.loads(line) for line in result.splitlines() if line]
        if len(values) > 50000:
            raise ValueError('ClickHouse aggregate cap exceeded')
        for row in values:
            for f in ('successful_calls', 'input_tokens', 'output_tokens', 'usage_microdollars'):
                row[f] = int(row[f])
            row['_time'] = row.pop('day').replace(' ', 'T') + 'Z'
            for f in ('first_call_at', 'last_call_at'):
                row[f] = row[f].replace(' ', 'T') + 'Z'
            row.update(event='growth.daily_usage', record_type='daily_usage_snapshot',
                       source='clickhouse_gcp', schema_version=3)
            row['event_id'] = m.digest('|'.join(str(row[k]) for k in
                ('event', '_time', 'workspace_fingerprint', 'model', 'provider')))
        return values

    def ingest(self, values):
        forbidden = {'email', 'workspace_id', 'user_id', 'api_key', 'prompt', 'output',
                     'body', 'request', 'headers', 'ip', 'textPayload', 'jsonPayload'}
        for row in values:
            if forbidden.intersection(row):
                raise ValueError('Private field in growth projection')
            for key, value in row.items():
                if key.endswith('_fingerprint') and value and not m.HASH.fullmatch(str(value)):
                    raise ValueError('Invalid growth fingerprint')
        for offset in range(0, len(values), 500):
            batch = values[offset:offset+500]
            result = self.checked(self.http.post(self.edge + '/v1/ingest/' + m.DATASET,
                                  headers=self.headers, json=batch)).json()
            if result.get('failed') != 0 or result.get('ingested') != len(batch):
                raise ValueError('Axiom did not accept the complete batch')

    def load(self, name):
        path = f'https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe="")}'
        meta = self.google('GET', path).json()
        data = self.google('GET', path, params={'alt': 'media', 'generation': meta['generation']}).content
        if len(data) > 40_000_000:
            raise ValueError('Compressed state limit exceeded')
        return json.loads(gzip.decompress(data)), meta['generation']

    def save(self, name, value, generation):
        data = json.dumps(value, separators=(',', ':')).encode()
        if len(data) > 180_000_000:
            raise ValueError('State size limit exceeded')
        result = self.google('POST', f'https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o',
            params={'name': name, 'uploadType': 'media', 'ifGenerationMatch': generation},
            content=gzip.compress(data)).json()
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
        updated, changed = cycle(state, io, now)
        io.save('state.json.gz', updated, generation)
        print(json.dumps({'event': 'growth.sync_success', 'changed_rows': changed,
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
