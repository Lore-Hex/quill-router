"""Serializable recovery plan for dense sources. No cursor advances on a partial read."""
import copy
import datetime as dt
import hashlib

from scripts.axiom_growth import model as m

DAY = dt.timedelta(days=1)
SPACE = 65536


class Capacity(ValueError):
    def __init__(self, source, message):
        super().__init__(message)
        self.source = source
        self.lane = None
        self.usage_job = None
        self.base = None


def begin(previous):
    return {'stop': previous.isoformat(), 'event_next': (previous - DAY).isoformat(),
            'usage_next': (previous.date()-DAY).isoformat(), 'turn': 'live',
            'live_seconds': 1200, 'repair_seconds': 1200}


def usage_bucket(row):
    key = ':'.join(row.get(k, '') for k in ('workspace_fingerprint', 'model', 'provider'))
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:2], 'big')


def roll(saved, previous):
    p = copy.deepcopy(saved)
    stop = dt.datetime.fromisoformat(p['stop'])
    if (dt.datetime.fromisoformat(p['event_next']) >= stop and
            dt.date.fromisoformat(p['usage_next']) < stop.date() - 30*DAY and
            not p.get('usage_job') and previous >= stop + DAY):
        p.update(begin(stop+DAY), turn=p['turn'], live_seconds=p['live_seconds'],
                 repair_seconds=p['repair_seconds'])
    return p


def layout(saved, previous, target):
    p = copy.deepcopy(saved)
    stop = dt.datetime.fromisoformat(p['stop'])
    next_event = dt.datetime.fromisoformat(p['event_next'])
    next_usage = dt.date.fromisoformat(p['usage_next'])
    events_due = next_event < stop
    usage_due = next_usage >= stop.date() - 30*DAY
    replay = p['turn'] == 'repair' and target-previous <= dt.timedelta(seconds=max(900, 2*p['live_seconds']))
    lane = 'repair' if replay and events_due else 'live'
    start = next_event if lane == 'repair' else previous
    right = min(stop if lane == 'repair' else target,
                start + dt.timedelta(seconds=p[lane+'_seconds']))
    through = previous if lane == 'repair' else right
    if lane == 'live':
        start -= dt.timedelta(seconds=min(30, p['live_seconds']//4))
    if lane == 'repair':
        p['event_next'] = right.isoformat()
    historical = p.get('usage_turn', 'live') == 'repair' and usage_due
    width = p.get('repair_width' if historical else 'live_width', SPACE)
    job = p.get('usage_job') or {'day': (next_usage if historical else through.date()).isoformat(),
                                 'historical': historical, 'ranges': [[0, width]], 'after': width}
    p['turn'] = 'live' if replay else 'repair'
    return p, lane, start, right, through, job


def narrower(saved, previous, failure):
    if saved is None:
        return begin(previous)
    p = copy.deepcopy(failure.base or saved)
    if failure.source == 'events' and failure.lane in {'live', 'repair'}:
        name = failure.lane + '_seconds'
        if p[name] > 1:
            p[name] = max(1, p[name] // 2)
            return p
    if failure.source == 'usage' and failure.usage_job:
        job = copy.deepcopy(failure.usage_job)
        lo, hi = job['ranges'][0]
        if hi-lo > 1:
            mid = (lo+hi)//2
            job['ranges'][:1] = [[lo, mid], [mid, hi]]
            name = 'repair_width' if job['historical'] else 'live_width'
            p[name] = min(p.get(name, SPACE), mid-lo)
            p['usage_job'] = job
            return p
    raise ValueError('Source exceeds minimum complete slice; explicit recovery required') from failure


def read(state, io, previous, target, now):
    base = roll(state['source_windows'], previous)
    p, lane, start, right, through, job = layout(base, previous, target)
    if start < now - 29*DAY:
        raise ValueError('Source repair exceeds retention; explicit recovery required')
    try:
        incoming = io.events(start, right)
        if len(incoming) > m.MAX_ROWS:
            raise Capacity('events', 'Event budget exceeded')
    except Capacity as exc:
        exc.lane = lane
        exc.base = base
        raise
    span_key = lane + '_seconds'
    span = int((right-start).total_seconds())
    if span >= p[span_key] and len(incoming) < m.MAX_ROWS*0.75:
        # Recover throughput after bisecting a dense interval. Aim below the
        # unchanged row cap, and grow slowly when the next interval is unknown.
        suggested = int(span*m.MAX_ROWS*0.8/max(1, len(incoming)))
        p[span_key] = max(p[span_key], min(1200, p[span_key]*5//4+1, suggested))
    day = dt.date.fromisoformat(job['day'])
    left = dt.datetime.combine(day, dt.time(), dt.UTC)
    lo, hi = job['ranges'][0]
    try:
        daily = io.usage(left, left+DAY-dt.timedelta(microseconds=1), bucket=(lo, hi))
    except Capacity as exc:
        exc.usage_job = job
        exc.base = base
        raise
    for row in daily:
        if row['_time'][:10] != job['day'] or not lo <= usage_bucket(row) < hi:
            raise ValueError('Usage outside requested source slice')
    # Delete only the exact complete slice, never the unread remainder of a day.
    def replaced(row):
        return row['_time'][:10] == job['day'] and (lo == 0 and hi == SPACE or lo <= usage_bucket(row) < hi)

    job['ranges'] = job['ranges'][1:]
    if not job['ranges'] and job['after'] < SPACE:
        width = p.get('repair_width' if job['historical'] else 'live_width', SPACE)
        next_bound = min(SPACE, job['after'] + width)
        job['ranges'] = [[job['after'], next_bound]]
        job['after'] = next_bound
    if job['ranges']:
        p['usage_job'] = job
    else:
        p.pop('usage_job', None)
        p['usage_turn'] = 'live' if job['historical'] else 'repair'
        if job['historical']:
            p['usage_next'] = (day-DAY).isoformat()
        else:
            # A split day is a rolling read, not a single consistent snapshot.
            p['last_live_usage_day'] = job['day']
    state['source_windows'] = p
    if p['event_next'] == p['stop'] and dt.date.fromisoformat(p['usage_next']) < dt.date.fromisoformat(p['stop'][:10])-30*DAY:
        state['repair_day'] = p['stop'][:10]
    return start, right, through, incoming, daily, replaced
