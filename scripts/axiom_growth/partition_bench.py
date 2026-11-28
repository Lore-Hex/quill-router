"""Offline retained-history and delivery benchmark. Synthetic IDs, no production connections."""
import argparse
import datetime as dt
import hashlib
import json
import math
import resource
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from scripts.axiom_growth import incremental_projection as inc
from scripts.axiom_growth import model as m
from scripts.axiom_growth.partition_store import Transaction
from scripts.axiom_growth.runtime import INGEST_BATCH, ingest_payloads


class LocalObjects:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript('PRAGMA journal_mode=WAL;'
            'CREATE TABLE IF NOT EXISTS objects(k TEXT PRIMARY KEY,b BLOB) WITHOUT ROWID;'
            'CREATE TABLE IF NOT EXISTS root(k INTEGER PRIMARY KEY,v INTEGER,b BLOB);'
            'INSERT OR IGNORE INTO root VALUES(0,0,NULL);')

    def head(self):
        return self.db.execute('SELECT v,b FROM root WHERE k=0').fetchone()

    def get(self, ref):
        return self.db.execute('SELECT b FROM objects WHERE k=?', (ref,)).fetchone()[0]

    def put(self, ref, blob):
        self.db.execute('INSERT OR IGNORE INTO objects VALUES(?,?)', (ref, blob))

    def swap(self, version, blob):
        if self.db.execute('UPDATE root SET v=v+1,b=? WHERE k=0 AND v=?', (blob,version)).rowcount != 1:
            raise ValueError('root generation conflict')
        return version + 1


def fixture(kind, i, day):
    key = hashlib.sha256(f'{kind}:{i}'.encode()).hexdigest()
    return key, {'event_id': key, 'event': kind, '_time': day + 'T00:00:00Z',
                 'anonymous_fingerprint': hashlib.sha256(f'visitor:{i//3}'.encode()).hexdigest(),
                 'workspace_fingerprint': hashlib.sha256(f'workspace:{i//8}'.encode()).hexdigest(),
                 'utm_source': 'synthetic', 'record_type': kind}


def run(store, scale, cycles, arrival):
    totals = {'fact': 125564*scale, 'usage': 18274*scale, 'projection': 44856*scale}
    start = dt.date(2025, 1, 1)
    for d in range(365):
        stamp = (start + dt.timedelta(days=d)).isoformat()
        tx = Transaction(store)
        for kind, total in totals.items():
            tx.apply(stamp, kind, dict(fixture(kind, i, stamp) for i in
                                      range(total*d//365, total*(d+1)//365)))
        tx.commit({'acquisition': stamp + 'T00:00:00+00:00'})
    print(json.dumps({'seeded': totals, 'scale': scale}), flush=True)
    elapsed, bytes_per, ops = [], [], []
    next_id = totals['fact']
    for iteration in range(cycles):
        before = time.perf_counter()
        end = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=5*iteration)
        day = end.date().isoformat()
        rows = dict(fixture('fact', i, day) for i in range(next_id, next_id+scale*arrival))
        next_id += len(rows)
        tx = Transaction(store)
        tx.apply(day, 'fact', rows)
        batch = list(rows.values())
        tx.stage_delivery([batch[i:i+INGEST_BATCH] for i in range(0, len(batch), INGEST_BATCH)])
        tx.commit({'acquisition': end.isoformat()})
        reader = Transaction(store)
        received = 0

        def receipt(sent):
            nonlocal received
            for count, body in ingest_payloads(sent):
                assert count == len(json.loads(body))
                received += count

        reader.drain(receipt)
        assert received == len(rows)
        elapsed.append(time.perf_counter() - before)
        bytes_per.append(tx.transferred + reader.transferred)
        ops.append(tx.ops + reader.ops)
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'scale': scale, 'historical_rows': sum(totals.values()), 'arrivals_per_cycle': scale*arrival,
            'cycles': cycles, 'p95_seconds': sorted(elapsed)[math.ceil(.95*cycles)-1],
            'max_logical_object_ops': max(ops), 'max_wire_bytes': max(bytes_per),
            'process_peak_mib': round(max_rss/(1024**2 if sys.platform == 'darwin' else 1024), 1),
            'scope': 'local object transport, committed outbox and mocked receipts; not source/projection or cloud latency'}


def independent_signups(store, scale, cycles, arrival):
    """Growing identity index workload: every five-minute cohort is new."""
    times, io_bytes, ops = [], [], []
    size = scale*arrival
    for step in range(cycles):
        end = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=step*5)

        def changes(cohort, at):
            for i in range(cohort*size, (cohort+1)*size):
                row = m.project_event({'event': 'acquisition.signup_completed', '_time': at.isoformat(),
                                       'anonymous_fingerprint': m.digest(f'v:{i}'),
                                       'account_fingerprint': m.digest(f'a:{i}'),
                                       'workspace_fingerprint': m.digest(f'w:{i}')}, source='axiom')
                yield 'fact', row['event_id'], row

        tx, before = Transaction(store), time.perf_counter()
        delta = inc.project(tx, changes(step, end), end)
        if len(delta) != size*2:
            raise ValueError('incomplete cohort projections')
        tx.commit({'acquisition': end.isoformat()})
        times.append(time.perf_counter()-before)
        io_bytes.append(tx.transferred)
        ops.append(tx.ops)
    return {'cycles': cycles, 'signups_per_cycle': size, 'independent_accounts': cycles*size,
            'p95_seconds': sorted(times)[math.ceil(.95*cycles)-1], 'max_object_ops': max(ops),
            'max_wire_bytes': max(io_bytes), 'scope': 'dirty projection and index commit, synthetic cohorts'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale', type=int, choices=[1, 10, 100], required=True)
    parser.add_argument('--cycles', type=int, default=12, choices=range(10, 101))
    parser.add_argument('--arrival', type=int, default=73, choices=range(1, 1001))
    parser.add_argument('--mode', choices=('transport', 'independent-signups'), default='transport')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalObjects(Path(tmp) / 'objects.db')
        try:
            bench = run if args.mode == 'transport' else independent_signups
            print(json.dumps(bench(backend, args.scale, args.cycles, args.arrival)))
        finally:
            backend.db.close()
