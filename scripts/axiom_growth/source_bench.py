"""Offline dense-source scheduling qualification; no GCS, ClickHouse or Axiom traffic."""
import bisect
import datetime as dt
import json

from scripts.axiom_growth import source_windows as w


class SyntheticSources:
    def __init__(self):
        self.max_events = self.max_usage = 0
        fixtures = [{'model': f'model:{i}', 'provider': 'synthetic', 'workspace_fingerprint': ''}
                    for i in range(111_000)]
        self.rows = sorted(fixtures, key=w.usage_bucket)
        self.keys = [w.usage_bucket(row) for row in self.rows]

    def events(self, left, right):
        n = int((right-left).total_seconds())*21  # 6,300 arrivals every five minutes.
        if n > 20_000:
            raise w.Capacity('events', 'source row cap')
        self.max_events = max(self.max_events, n)
        return range(n)

    def usage(self, left, right, *, bucket):
        i, j = (bisect.bisect_left(self.keys, n) for n in bucket)
        if j-i > 50_000:
            raise w.Capacity('usage', 'aggregate cap')
        self.max_usage = max(self.max_usage, j-i)
        return [{**row, '_time': left.isoformat()} for row in self.rows[i:j]]


def main():
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    previous, io = start, SyntheticSources()
    state = {'source_windows': w.begin(start)}
    replans = 0
    for step in range(1, 289):
        now = start + dt.timedelta(minutes=5*step)
        try:
            _, _, previous, _, _, _ = w.read(state, io, previous, now, now)
        except w.Capacity as exc:
            state['source_windows'] = w.narrower(state['source_windows'], previous, exc)
            replans += 1
        if state.get('repair_day') == start.date().isoformat():
            print(json.dumps({'cycles': step, 'replans_without_cursor_advance': replans,
                              'simulated_hours': round(step/12, 2), 'acquisition_repair_hours': 24,
                              'usage_repair_days': 30, 'usage_groups_per_day': len(io.rows),
                              'max_accepted_events': io.max_events, 'max_accepted_usage_rows': io.max_usage,
                              'forward_lag_minutes': (now-previous).total_seconds()/60,
                              'scope': 'source scheduling and actual usage range membership; no cloud latency'}))
            return
    raise ValueError('dense source sweeps did not finish in one simulated day')


if __name__ == '__main__':
    main()
