"""Operator-only provision/bootstrap helpers. Credentials never reach logs or argv."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import secrets
import shlex
import subprocess
from pathlib import Path

from scripts.axiom_growth import model as m
from scripts.axiom_growth.admin_api import api, query
from scripts.axiom_growth.runtime import content_hash

PROJECT = 'quill-cloud-proxy'
BUCKET = 'quill-cloud-proxy-growth-sync'


def source_filter():
    events = ' OR '.join(f'jsonPayload.event="{event}"'
                         for event in sorted(m.BROWSER_EVENTS | m.CONVERSION_EVENTS))
    return ('resource.type="cloud_run_revision" '
            '(resource.labels.service_name="trusted-router-public" OR '
            'resource.labels.service_name="trusted-router") '
            f'({events})')


def update_source_filter():
    run(['gcloud', 'logging', 'sinks', 'update', 'tr-growth-source',
         '--project='+PROJECT, '--log-filter='+source_filter()])
    print(json.dumps({'updated': 'tr-growth-source', 'exact_events': len(m.BROWSER_EVENTS | m.CONVERSION_EVENTS)}))


def run(args, *, data=None):
    result = subprocess.run(args, input=data, capture_output=True, timeout=180)  # noqa: S603
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed with exit code {result.returncode}')
    return result.stdout


def new_secret(name, value):
    run(['gcloud', 'secrets', 'create', name, '--project='+PROJECT,
         '--replication-policy=automatic', '--data-file=-'], data=value.encode())


def provision_axiom():
    token = api('POST', '/v2/tokens', {
        'name': 'trustedrouter-growth-sync',
        'description': 'Dedicated worker: query sanitized operational source; ingest marketing projection only',
        'expiresAt': (dt.datetime.now(dt.UTC)+dt.timedelta(days=365)).isoformat(),
        'datasetCapabilities': {'trusted-router-logs': {'query': ['read']},
                                m.DATASET: {'ingest': ['create']}}, 'orgCapabilities': {}})
    try:
        new_secret('trustedrouter-growth-axiom-token', token['token'])
    except Exception:
        api('DELETE', '/v2/tokens/'+token['id'])
        raise
    print(json.dumps({'created': 'trustedrouter-growth-axiom-token', 'token_id': token['id']}))


def provision_clickhouse():
    password = secrets.token_urlsafe(36)
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    sql = f"""CREATE USER IF NOT EXISTS tr_growth_read ON CLUSTER trustedrouter
        IDENTIFIED WITH sha256_hash BY '{password_hash}'
        SETTINGS readonly=1, max_execution_time=45, max_threads=2, max_memory_usage=536870912;
        CREATE VIEW IF NOT EXISTS tr.growth_daily_usage ON CLUSTER trustedrouter
        DEFINER=tr SQL SECURITY DEFINER AS
        SELECT toStartOfDay(created_at) AS day,
        if(workspace_id='', '', lower(hex(SHA256(workspace_id)))) AS workspace_fingerprint,
        if(workspace_id='', '', lower(hex(SHA256(concat('tr-marketing-workspace:',workspace_id))))) AS marketing_workspace_fingerprint,
        model, provider, count() AS successful_calls, sum(input_tokens) AS input_tokens,
        sum(output_tokens) AS output_tokens, sum(total_cost_microdollars) AS usage_microdollars,
        min(created_at) AS first_call_at, max(created_at) AS last_call_at
        FROM tr.provider_benchmark_samples FINAL WHERE source='organic' AND status='success'
        GROUP BY day, workspace_fingerprint, marketing_workspace_fingerprint, model, provider;
        GRANT ON CLUSTER trustedrouter SELECT ON tr.growth_daily_usage TO tr_growth_read;"""  # noqa: S608 - generated hash only.
    command = '. /etc/tr-clickhouse-ingest.env; export CLICKHOUSE_PASSWORD="$CH_PASSWORD"; exec clickhouse-client --user tr --multiquery'
    run(['gcloud', 'compute', 'ssh', 'tr-clickhouse-1',
         '--account=tr-ops-local@quill-cloud-proxy.iam.gserviceaccount.com',
         '--project='+PROJECT, '--zone=us-central1-a', '--tunnel-through-iap', '--quiet',
         '--command', 'sudo sh -c '+shlex.quote(command)], data=sql.encode())
    new_secret('trustedrouter-growth-clickhouse-password', password)
    print(json.dumps({'created': 'tr_growth_read', 'scope': 'SELECT hashed aggregate view only'}))


def bootstrap(cache_path):
    cached = json.loads(Path(cache_path).read_text())
    events = {r['event_id']: r for r in sorted(cached['events']+cached['current'],
                                            key=lambda r: str(r.get('exported_at') or ''))}
    fields = ('_time, event, workspace_fingerprint, marketing_workspace_fingerprint, '
              'model, provider, successful_calls, input_tokens, output_tokens, usage_microdollars, '
              'first_call_at, last_call_at, source, schema_version, record_type')
    daily = m.rows(query(f"['{m.DATASET}'] | where event == 'growth.daily_usage' | take 100000 "
                        f'| summarize arg_max(exported_at, {fields}) by event_id | take 50000',
                        '2026-08-01T00:00:00Z'))
    if len(daily) >= 50000:
        raise ValueError('Bootstrap result cap reached')
    end = m.date(cached['end'])
    journeys = m.build_journeys(list(events.values()), daily, end)
    state = {'watermark': end.isoformat(), 'events': list(events.values()), 'daily': daily,
             'hashes': {r['event_id']: content_hash(r) for r in list(events.values())+daily+journeys},
             'repair_day': ''}
    files = {'state.json.gz': state, 'lock.json.gz': {'until': '2026-01-01T00:00:00Z'}}
    for name, value in files.items():
        run(['gcloud', 'storage', 'cp', '-', f'gs://{BUCKET}/{name}', '--if-generation-match=0'],
            data=gzip.compress(json.dumps(value).encode()))
    print(json.dumps({'bootstrapped_events': len(events), 'daily_rows': len(daily), 'watermark': state['watermark']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['axiom', 'clickhouse', 'bootstrap', 'source-filter'])
    parser.add_argument('--cache')
    args = parser.parse_args()
    if args.action == 'axiom':
        provision_axiom()
    elif args.action == 'clickhouse':
        provision_clickhouse()
    elif args.action == 'source-filter':
        update_source_filter()
    else:
        if not args.cache:
            parser.error('--cache is required for bootstrap')
        bootstrap(args.cache)
