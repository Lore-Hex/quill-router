"""Independent, hash-only verifier for committed legacy checkpoints. Never ingests into Axiom."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
from collections import defaultdict, deque
from heapq import nsmallest
from urllib.parse import quote

import google.auth
import httpx
from google.auth.transport.requests import Request

from scripts.axiom_growth import incremental_projection as incremental
from scripts.axiom_growth import model as m
from scripts.axiom_growth import partition_store as ps
from scripts.axiom_growth.runtime import MAX_COMPRESSED_BYTES, MAX_STATE_BYTES, content_hash

PREFIX = 'partition-shadow-v1/'


class ShadowObjects:
    def __init__(self, http, creds, bucket, prefix=PREFIX):
        self.http, self.creds = http, creds
        self.prefix = prefix
        self.base = 'https://storage.googleapis.com/storage/v1/b/' + quote(bucket, safe='') + '/o'
        self.upload = self.base.replace('/storage/v1/', '/upload/storage/v1/')

    def request(self, method, url, *, limit=65536, **kwargs):
        if not self.creds.valid:
            self.creds.refresh(Request())
        with self.http.stream(method, url, headers={'Authorization': 'Bearer ' + self.creds.token},
                              **kwargs) as response:
            if response.status_code in (404, 412):
                return response.status_code, b''
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'shadow GCS HTTP {response.status_code}')
            out = bytearray()
            for piece in response.iter_bytes(65536):
                if len(out) + len(piece) > limit:
                    raise ValueError('shadow object exceeds transfer limit')
                out.extend(piece)
            return response.status_code, bytes(out)

    def url(self, name):
        return self.base + '/' + quote(name, safe='')

    def read_version(self, name, limit):
        url = self.url(name)
        status, meta = self.request('GET', url, params={'fields': 'generation,size'})
        if status == 404:
            return '0', None
        if status != 200:
            raise ValueError('checkpoint metadata unavailable')
        meta = json.loads(meta)
        if int(meta['size']) > limit:
            raise ValueError('checkpoint exceeds transfer limit')
        status, data = self.request('GET', url, params={'alt': 'media', 'generation': meta['generation']},
                                    limit=limit)
        if status != 200 or len(data) != int(meta['size']):
            raise ValueError('checkpoint generation changed or truncated')
        return meta['generation'], data

    def head(self):
        return self.read_version(self.prefix + 'root', ps.PAGE_BYTES + 65536)

    def get(self, ref):
        if not ps.ID.fullmatch(ref):
            raise ValueError('invalid shadow object reference')
        status, body = self.request('GET', self.url(self.prefix + ref), params={'alt': 'media'},
                                    limit=ps.PAGE_BYTES + 65536)
        if status != 200:
            raise ValueError('missing immutable shadow object')
        return body

    def write(self, name, generation, data):
        return self.request('POST', self.upload, params={'uploadType': 'media', 'name': self.prefix + name,
                            'ifGenerationMatch': generation, 'fields': 'generation'}, content=data)

    def put(self, ref, blob):
        if ref != ps.digest(blob):
            raise ValueError('invalid immutable write')
        status, _ = self.write(ref, '0', blob)
        if status == 412 and self.get(ref) == blob:
            return
        if status != 200:
            raise ValueError('immutable upload failed')

    def swap(self, generation, blob):
        status, meta = self.write('root', generation, blob)
        if status != 200:
            raise ValueError('shadow root generation conflict')
        return json.loads(meta)['generation']


def sample(snapshot, since):
    """Verify every fact/daily row in the chosen window, not a random sample."""
    groups = defaultdict(dict)
    for name, kind in [('events', 'fact'), ('daily', 'usage')]:
        for row in snapshot[name]:
            day = row['_time'][:10]
            if day >= since:
                key = row['event_id']
                value = snapshot['hashes'][key]
                if not ps.ID.fullmatch(value):
                    raise ValueError('invalid oracle projection digest')
                groups[day, kind][key] = value
    return groups


_BILLING_FIELDS = frozenset(('billing_account_fingerprint', 'billing_identity_basis',
                            'billing_owner_observed_at', 'billing_source'))


def component_receipt(snapshot):
    # This oracle deliberately reads the legacy history. Only the incremental
    # candidate reads a dirty component. No source rows leave the verifier.
    end = m.date(snapshot['watermark'])
    rows, reverse = {}, defaultdict(list)
    status = snapshot.get('billing_owner_status', 'not_recorded')
    owners = snapshot.get('billing_owners', []) if status in {'current', 'stale', 'unavailable'} else None
    if status == 'current' and ('billing_owners' not in snapshot or not snapshot.get('billing_directory_read_at')):
        raise ValueError('missing successful owner-directory read evidence')
    directory = {r['workspace_fingerprint']: r['billing_account_fingerprint'] for r in owners or ()}

    def raw_edges(kind, row):
        fields = [('w', 'workspace_fingerprint'), ('w', 'marketing_workspace_fingerprint')]
        if kind == 'fact':
            fields += [('a', 'account_fingerprint'), ('v', 'anonymous_fingerprint')]
        return {(space, row[key]) for space, key in fields if row.get(key)}

    for name, kind in [('events', 'fact'), ('daily', 'usage')]:
        for row in snapshot[name]:
            key = row['event_id']
            if key in rows:
                raise ValueError('duplicate oracle source key')
            rows[key] = (kind, row)
            for edge in raw_edges(kind, row):
                reverse[edge].append(key)
    pivot = m.digest(end.isoformat())
    seeds = nsmallest(4, rows, key=lambda k: (k < pivot, k))
    if owners is not None:
        # Include a commercial projection on every populated check. Rotate
        # matched and unmatched usage independently of the general source seeds.
        for linked in (True, False):
            seeds.extend(nsmallest(1, (r['event_id'] for r in snapshot['daily']
                if (r.get('workspace_fingerprint') in directory) == linked), key=lambda k: (k < pivot, k)))
    todo, selected, expanded = deque(seeds), set(), set()
    while todo:
        key = todo.popleft()
        if key in selected:
            continue
        selected.add(key)
        if len(selected) > incremental.MAX_COMPONENT:
            raise ValueError('oracle component exceeds qualification budget')
        kind, row = rows[key]
        relevant = raw_edges(kind, row)
        if kind == 'usage' and (owner := directory.get(row.get('workspace_fingerprint'))):
            # The commercial owner can be someone other than the caller and
            # their source may be ambiguous across unrelated visitor journeys.
            relevant.add(('a', owner))
        for edge in relevant - expanded:
            expanded.add(edge)
            todo.extend(reverse[edge])
    del reverse

    class Scratch:
        def __init__(self):
            self.objects = {}

        def head(self):
            return 0, None

        def put(self, ref, blob):
            self.objects[ref] = blob

        def get(self, ref):
            return self.objects[ref]

    changes = [(kind, key, {k: v for k, v in row.items() if k not in _BILLING_FIELDS}
                if kind == 'usage' and owners is not None else row)
               for key, (kind, row) in rows.items() if key in selected]
    delta = incremental.project(ps.Transaction(Scratch()), changes, end,
                                milestones=snapshot.get('gateway_milestones', {}))
    usage = [delta[key] for kind, key, _ in changes if kind == 'usage']
    if owners is not None:
        m.link_billing_accounts(usage, owners, [r for r in delta.values() if r['event'] == 'growth.journey'])
    expected = set(selected)
    expected.update(m.digest('journey:' + row['anonymous_fingerprint']) for kind, _, row in changes
                    if kind == 'fact' and row['event'].startswith('acquisition.')
                    and m.HASH.fullmatch(row.get('anonymous_fingerprint') or ''))
    if expected != delta.keys() or any(content_hash(delta[k]) != snapshot['hashes'].get(k) for k in expected):
        raise ValueError('incremental projection differs from committed legacy oracle')
    return {'component_projections_checked': len(expected), 'billing_directory_status': status,
            'billing_usage_checked': len(usage) if owners is not None else 0,
            'billing_linked_checked': sum(bool(r.get('billing_account_fingerprint')) for r in usage)
            if owners is not None else 0, 'billing_directory_read_at': snapshot.get('billing_directory_read_at')}


def reconcile(store, snapshot, *, days=2):
    tx = ps.Transaction(store)
    watermark = snapshot['watermark']
    cutoff = (dt.datetime.fromisoformat(watermark).date() - dt.timedelta(days=days - 1)).isoformat()
    groups = sample(snapshot, cutoff)
    checked = changed = 0
    summaries = tx.head.setdefault('verified_slots', {})
    sequence = int(tx.head.get('checks', 0))
    for day in set(tx.head['days']) | {d for d, _ in groups}:
        if day < cutoff:
            tx.remove_day(day)
            summaries.pop(day, None)
            continue
        pages = tx.day(day)
        saved = summaries.setdefault(day, {})
        for kind in ('fact', 'usage'):
            current = groups.get((day, kind), {})
            slots = defaultdict(dict)
            for key, value in current.items():
                slots[kind + ':' + key[0]][key] = value
            for slot in slots.keys() | {s for s in pages if s.startswith(kind + ':')}:
                wanted = slots[slot]
                signature = ps.digest(json.dumps(wanted, separators=(',', ':'), sort_keys=True).encode())
                scrub = int(slot[-1], 16) == sequence % 16
                if saved.get(slot) == signature and not scrub:
                    continue
                previous = tx.fold(pages.get(slot, []))
                if saved.get(slot) == signature and previous != wanted:
                    raise ValueError('previously verified immutable slot diverged')
                diff = {k: None for k in previous.keys() - wanted.keys()}
                diff.update({k: v for k, v in wanted.items() if previous.get(k) != v})
                if diff:
                    tx.apply(day, kind, diff)
                    tx.flush_pack()
                    pages = tx.day(day)
                    if tx.fold(pages[slot]) != wanted:
                        raise ValueError('shadow readback parity mismatch')
                saved[slot] = signature
                changed += len(diff)
            checked += len(current)
    tx.head['checks'] = sequence + 1
    markers = {'acquisition': watermark}
    if snapshot.get('gateway_watermark'):
        markers['gateway'] = snapshot['gateway_watermark']
    # No fabricated freshness for cached billing owners.
    tx.commit(markers)
    return {'checked': checked, 'changed': changed, 'ops': tx.ops, 'bytes': tx.transferred,
            'observed_through': watermark}


def load_checkpoint(source):
    generation, data = source.read_version('state.json.gz', MAX_COMPRESSED_BYTES)
    if data is None:
        raise ValueError('no legacy checkpoint')
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as src:
        raw = src.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError('legacy checkpoint too large')
    state = json.loads(raw)
    lag = (dt.datetime.now(dt.UTC) - m.date(state['watermark'])).total_seconds()
    if not 0 <= lag <= 15*60:
        raise ValueError('legacy checkpoint is stale or from the future')
    return generation, state, round(lag)


def main():
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    with httpx.Client(timeout=20, follow_redirects=False) as http:
        source_bucket, dest = os.environ['GROWTH_STATE_BUCKET'], os.environ['GROWTH_SHADOW_BUCKET']
        if source_bucket == dest:
            raise ValueError('shadow requires an isolated bucket')
        source = ShadowObjects(http, credentials, source_bucket)
        generation, state, lag = load_checkpoint(source)
        now = dt.datetime.now(dt.UTC)
        verified = component_receipt(state)
        if not verified['component_projections_checked']:
            raise ValueError('no component parity evidence')
        epoch = now.strftime('%G-W%V')
        store = ShadowObjects(http, credentials, dest, PREFIX + epoch + '/')
        receipt = reconcile(store, state)
        print(json.dumps({'event': 'growth.partition_shadow_verified', 'source_generation': generation,
                          **verified, 'source_lag_seconds': round(lag),
                          **receipt}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'severity': 'ERROR', 'event': 'growth.partition_shadow_failed',
                          'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
