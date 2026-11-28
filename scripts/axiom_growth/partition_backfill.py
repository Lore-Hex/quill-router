"""Resumable projection-index restore drill. No ingestion or source cursor writes."""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict

import google.auth
import httpx

from scripts.axiom_growth import partition_store as ps
from scripts.axiom_growth.partition_shadow import ShadowObjects, load_checkpoint

PREFIX = 'projection-restore-v1/'
STRIDE = 64  # At most one quarter of the index per invocation.
MAX_ROWS = 750_000
MAX_PREFIX_ROWS = 8_192


def freeze(tx, load):
    if tx.head != ps.empty():
        raise ValueError('restore requires its own empty namespace')
    generation, state, _ = load()
    hashes = state['hashes']
    if not (str(generation).isascii() and str(generation).isdecimal() and 0 < len(hashes) <= MAX_ROWS):
        raise ValueError('invalid source generation or projection count')
    if any(row['event_id'] not in hashes for stream in ('events', 'daily') for row in state[stream]):
        raise ValueError('source row has no committed projection digest')
    groups = defaultdict(dict)
    for key, value in hashes.items():
        if not (ps.ID.fullmatch(key) and ps.ID.fullmatch(value)):
            raise ValueError('invalid projection ID or digest')
        group = groups[key[:2]]
        if len(group) >= MAX_PREFIX_ROWS:
            raise ValueError('source prefix exceeds restore budget')
        group[key] = value
    # A restore slice should fetch contiguous packs, not every pack in the baseline.
    refs = {prefix: tx.put(groups[prefix]) for prefix in sorted(groups)}
    tx.flush_pack()
    return {'version': 1, 'generation': str(generation), 'through': state['watermark'],
            'total': len(hashes), 'refs': refs, 'phase': 'write', 'next': 0,
            'written': 0, 'verified': 0}


def visit(tx, progress, prefix):
    day = 'index-' + prefix
    ref = progress['refs'].get(prefix)
    wanted = tx.read(ref) if ref is not None else {}
    if progress['phase'] == 'write':
        if day in tx.head['days']:
            raise ValueError('target prefix already exists outside committed cursor')
        if wanted:
            tx.apply(day, 'projection', wanted)
    else:
        # A fresh transaction reads the committed target, not the candidate cache.
        pages = tx.day(day)
        slots = defaultdict(dict)
        for key, digest in wanted.items():
            slots[ps.slot_for(day, 'projection', key)][key] = digest
        if {s for s in pages if not s.startswith('filter:')} != slots.keys():
            raise ValueError('restored partition slots differ from frozen source')
        for slot, expected in slots.items():
            if tx.fold(pages[slot]) != expected:
                raise ValueError('restored projection differs from frozen source')
            # An incorrect negative filter could hide rows even with exact leaf bytes.
            refs = pages[slot]
            if (len(refs) != 1 or pages.get('filter:' + slot, {}).get(refs[0][3])
                    != ps.key_filter(expected)):
                raise ValueError('restored lookup filter differs from frozen source')
    return len(wanted)


def step(store, load):
    tx = ps.Transaction(store)
    try:
        progress = tx.head.get('restore')
        if progress is None:
            progress = tx.head['restore'] = freeze(tx, load)
        elif progress.get('version') != 1 or progress['phase'] not in {'write', 'verify', 'complete'}:
            raise ValueError('unknown restore format')
        elif progress['phase'] != 'complete':
            first = progress['next']
            last = min(first + STRIDE, 256)
            if not 0 <= first < last:
                raise ValueError('invalid restore cursor')
            count = sum(visit(tx, progress, f'{n:02x}') for n in range(first, last))
            counter = 'written' if progress['phase'] == 'write' else 'verified'
            progress[counter] += count
            progress['next'] = last
            if last == 256:
                if progress[counter] != progress['total']:
                    raise ValueError('restore count differs from source')
                if counter == 'verified' and set(tx.head['days']) != {'index-'+k for k in progress['refs']}:
                    raise ValueError('restore has unexpected partitions')
                progress.update(phase='verify' if counter == 'written' else 'complete', next=0)
        if tx.head != tx.original:
            tx.commit({})  # The progress cursor and the new pages become visible together.
        return {k: progress[k] for k in ('generation', 'through', 'phase', 'total', 'written', 'verified', 'next')} | {
            'object_ops': tx.ops, 'bytes': tx.transferred}
    finally:
        tx.close()


def main():
    source, dest = os.environ['GROWTH_STATE_BUCKET'], os.environ['GROWTH_SHADOW_BUCKET']
    if source == dest:
        raise ValueError('restore requires isolated storage')
    epoch = dt.datetime.now(dt.UTC).strftime('%G-W%V')
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    with httpx.Client(timeout=20, follow_redirects=False) as http:
        target = ShadowObjects(http, credentials, dest, PREFIX + epoch + '/')
        receipt = step(target, lambda: load_checkpoint(ShadowObjects(http, credentials, source)))
    print(json.dumps({'event': 'growth.partition_restore_progress', **receipt}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'severity': 'ERROR', 'event': 'growth.partition_restore_failed',
                          'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
