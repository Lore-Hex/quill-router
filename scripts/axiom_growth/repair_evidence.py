"""Recover omitted identity evidence from sanitized Axiom history, never Spanner.

Dry-run by default. Apply holds the exporter's lease, backs up the checkpoint,
and uses GCS generation preconditions. Observed event IDs/times stay unchanged.
"""
import argparse
import datetime as dt
import gzip
import json

from scripts.axiom_growth import model as m
from scripts.axiom_growth.admin_api import api, query
from scripts.axiom_growth.provision import BUCKET, run

FIELDS = ('account_fingerprint', 'marketing_workspace_fingerprint', 'workspace_fingerprint',
          'first_referrer_domain', 'customer_domain', 'customer_domain_verified',
          'customer_domain_basis', 'domain_observed_at', 'first_purchase_at')


def recover(events, history):
    by_anon = {}
    for row in history:
        anon = row.get('anonymous_fingerprint')
        if m.HASH.fullmatch(str(anon or '')):
            by_anon.setdefault(anon, []).append(row)
    changed = 0
    for row in events:
        candidates = by_anon.get(row.get('anonymous_fingerprint'), [])
        accounts = {r.get('account_fingerprint') for r in candidates if r.get('account_fingerprint')}
        if len(accounts) != 1 or (row.get('account_fingerprint') and row['account_fingerprint'] not in accounts):
            continue
        before = dict(row)
        for field in ('account_fingerprint', 'marketing_workspace_fingerprint', 'workspace_fingerprint'):
            values = {r[field] for r in candidates if m.HASH.fullmatch(str(r.get(field, '')))}
            if not row.get(field) and len(values) == 1:
                row[field] = next(iter(values))
        refs = {m.domain(r.get('first_referrer_domain')) for r in candidates} - {''}
        if not row.get('first_referrer_domain') and len(refs) == 1:
            row['first_referrer_domain'] = next(iter(refs))
        evidence = [r for r in candidates if m.domain(r.get('customer_domain')) and r.get('domain_observed_at')]
        if evidence and row.get('customer_domain_basis') in (None, '', 'not_linked'):
            latest = max(evidence, key=lambda r: m.date(r['domain_observed_at']))
            if latest.get('customer_domain_basis') in {'current_workspace_owner_email', 'current_account_email'}:
                row.update(customer_domain=m.domain(latest['customer_domain']),
                           customer_domain_verified=latest.get('customer_domain_verified') is True,
                           customer_domain_basis=latest['customer_domain_basis'],
                           domain_observed_at=m.stamp(latest['domain_observed_at']))
        # Historical lifetime purchase timestamps do not imply additional
        # observed purchase events or amounts.
        times = [m.stamp(r['first_purchase_at']) for r in candidates if r.get('first_purchase_at')]
        if not row.get('first_purchase_at') and times:
            row['first_purchase_at'] = min(times, key=m.date)
        if row != before:
            row.update(evidence_repaired_at=dt.datetime.now(dt.UTC).isoformat(), schema_version=5)
            changed += 1
    return changed


def load(name):
    uri = f'gs://{BUCKET}/{name}'
    meta = json.loads(run(['gcloud', 'storage', 'objects', 'describe', uri, '--format=json']))
    raw = run(['gcloud', 'storage', 'cat', uri + '#' + str(meta['generation'])])
    if len(raw) > 40_000_000:
        raise ValueError('Checkpoint exceeds bounded repair size')
    return json.loads(gzip.decompress(raw)), str(meta['generation'])


def save(name, body, generation):
    run(['gcloud', 'storage', 'cp', '-', f'gs://{BUCKET}/{name}',
         '--if-generation-match='+str(generation)], data=gzip.compress(json.dumps(body).encode()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    # Discover schema before querying. Only allowlisted fields leave Axiom.
    actual = {f['name'] for f in api('GET', f'/v2/datasets/{m.DATASET}/fields')}
    fields = [f for f in FIELDS if f in actual]
    history = m.rows(query(f"['{m.DATASET}'] | where record_type in ('historical_milestone','historical_snapshot') "
        '| summarize arg_max(exported_at, anonymous_fingerprint, ' + ', '.join(fields) + ') by event_id | take 10001',
        (dt.datetime.now(dt.UTC)-dt.timedelta(days=365)).isoformat()))
    if len(history) > 10000:
        raise ValueError('History repair exceeds reviewed row budget')
    now = dt.datetime.now(dt.UTC)
    lock_version = None
    if args.apply:
        lock, version = load('lock.json.gz')
        if m.date(lock['until']) > now:
            raise RuntimeError('Exporter active; retry between scheduled runs')
        save('lock.json.gz', {'until': (now+dt.timedelta(minutes=10)).isoformat()}, version)
        _, lock_version = load('lock.json.gz')
    try:
        state, generation = load('state.json.gz')
        if args.apply:
            save('backups/evidence-repair-' + generation + '.json.gz', state, '0')
        changed = recover(state['events'], history)
        journeys = m.build_journeys(state['events'], state['daily'], m.date(state['watermark']))
        print(json.dumps({'dry_run': not args.apply, 'historical_rows': len(history),
            'repaired_events': changed, 'journeys': len(journeys),
            'verified_journeys': sum(r['customer_domain_verified'] for r in journeys),
            'usage_rows': len(state['daily']),
            'account_linked_usage_rows': sum(bool(r['account_fingerprint']) for r in state['daily'])}))
        if args.apply:
            # Preserve hashes: next scheduled run exports only changed records.
            save('state.json.gz', state, generation)
    finally:
        if lock_version:
            save('lock.json.gz', {'until': now.isoformat()}, lock_version)


if __name__ == '__main__':
    main()
