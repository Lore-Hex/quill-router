"""Shadow acquisition/usage projector. The live scheduler must not publish from this module."""
import base64
import json
from collections import defaultdict, deque
from itertools import chain

from scripts.axiom_growth import gateway_attempts
from scripts.axiom_growth import model as m
from scripts.axiom_growth import partition_store as ps
from scripts.axiom_growth.runtime import _DERIVED_DAILY, content_hash

MAX_COMPONENT = 20_000
MAX_CACHE_BYTES = 64 * 1024 * 1024


def index(key):
    return 'index-' + key[:2]


def edges(row):
    if not row:
        return set()
    fields = {'anonymous_fingerprint': 'visitor', 'account_fingerprint': 'account',
              'workspace_fingerprint': 'workspace', 'marketing_workspace_fingerprint': 'workspace'}
    return {m.digest(label + ':' + row[k]) for k, label in fields.items()
            if row.get(k) and ps.ID.fullmatch(row[k])}


class Overlay:
    """A bounded read-your-writes view. Flush only modified entries into touched partitions."""
    def __init__(self, tx):
        self.tx = tx
        self.cache, self.manifests, self.filters = {}, {}, {}
        self.changed = defaultdict(dict)
        self.size = 0

    def charge(self, row):
        self.size += len(json.dumps(row, separators=(',', ':')))
        if self.size > MAX_CACHE_BYTES:
            raise ValueError('incremental projection working set exceeded')

    def get(self, day, kind, key):
        delta = self.changed.get((day, kind), {})
        if key in delta:
            return delta[key]
        slot = ps.slot_for(day, kind, key)
        if day not in self.manifests:
            self.manifests[day] = self.tx.day(day)
            self.charge(self.manifests[day])
        pages = self.manifests[day]
        for ref in reversed(pages.get(slot, [])):
            checksum = ref if isinstance(ref, str) else ref[3]
            if checksum not in self.cache:
                encoded = pages.get('filter:' + slot, {}).get(checksum)
                if encoded:
                    if checksum not in self.filters:
                        self.filters[checksum] = base64.b64decode(encoded, validate=True)
                    if not ps.might_contain(self.filters[checksum], key):
                        continue
                page = self.tx.read(ref)
                self.charge(page)
                self.cache[checksum] = page
            if key in self.cache[checksum]:
                return self.cache[checksum][key]
        return None

    def set(self, day, kind, key, value):
        self.charge(value)
        self.changed[day, kind][key] = value

    def location(self, key):
        return self.get(index(key), 'dependency', m.digest('location:' + key))

    def members(self, edge):
        return self.get(index(edge), 'dependency', edge) or []

    def flush(self):
        for (day, kind), delta in self.changed.items():
            self.tx.apply(day, kind, delta)


def project(tx, changes, end, *, milestones=None):
    """Return replacements/tombstones by stable ID, never send from a candidate.

    Inputs are complete normalized fact/usage replacements, or None for expiry.
    Both sides of removed edges must be revisited, even months after an event.
    Commercial ownership and gateway milestones are not in this projector yet.
    """
    try:
        return _project(tx, changes, end, milestones)
    except Exception:
        tx.failed = True
        tx.close()
        raise


def _project(tx, changes, end, milestones):
    view = Overlay(tx)
    sequence = tx.head.get('source_sequence', 0)
    dirty, dirty_edges, visitors = set(), set(), set()
    memberships = {}
    for kind, key, row in changes:
        if kind not in {'fact', 'usage'} or not ps.ID.fullmatch(key):
            raise ValueError('invalid incremental source key')
        loc = view.location(key)
        if loc and loc[0] != kind:
            raise ValueError('source key reused across streams')
        old = view.get(loc[1], kind, key) if loc else None
        if loc and old is None:
            raise ValueError('dangling source location')
        if row is not None:
            if row['event_id'] != key:
                raise ValueError('source row ID mismatch')
            if kind == 'usage':
                row = {k: v for k, v in row.items() if k not in _DERIVED_DAILY}
        if old == row:
            continue
        before, after = edges(old), edges(row)
        for edge in before | after:
            if edge not in (before & after):
                if edge not in memberships:
                    memberships[edge] = set(view.members(edge))
                members = memberships[edge]
                if edge in before:
                    members.discard(key)
                else:
                    members.add(key)
                if len(members) > MAX_COMPONENT:
                    raise ValueError('identity fanout exceeds projection budget')
        dirty_edges.update(before | after)
        dirty.add(key)
        if len(dirty) > MAX_COMPONENT:
            raise ValueError('incremental change budget exceeded')
        for value in (old, row):
            if value and value.get('anonymous_fingerprint'):
                visitors.add(value['anonymous_fingerprint'])
        day = m.date(row['_time']).date().isoformat() if row else None
        if loc and loc[1] != day:
            view.set(loc[1], kind, key, None)
        if row:
            view.set(day, kind, key, row)
        if row and not loc:
            sequence += 1
        view.set(index(key), 'dependency', m.digest('location:' + key),
                 [kind, day, loc[2] if loc else sequence] if row else None)

    for edge, members in memberships.items():
        view.set(index(edge), 'dependency', edge, sorted(members) or None)
    todo = deque(dirty)
    for edge in dirty_edges:
        todo.extend(view.members(edge))
    visited, expanded = set(), set(dirty_edges)
    facts, usage, order = [], [], {}
    while todo:
        key = todo.popleft()
        if key in visited:
            continue
        visited.add(key)
        if len(visited) > MAX_COMPONENT:
            raise ValueError('identity component exceeds projection budget')
        loc = view.location(key)
        if not loc:
            continue
        row = view.get(loc[1], loc[0], key)
        if row is None:
            raise ValueError('dangling identity index')
        (usage if loc[0] == 'usage' else facts).append(dict(row))
        order[key] = loc[2]
        if row.get('anonymous_fingerprint'):
            visitors.add(row['anonymous_fingerprint'])
        for edge in edges(row) - expanded:
            expanded.add(edge)
            todo.extend(view.members(edge))
    # The legacy projector breaks exact timestamp ties by source insertion order.
    facts.sort(key=lambda r: order[r['event_id']])
    journeys = m.build_journeys(facts, usage, end)
    if milestones is not None:
        gateway_attempts.preserve_milestones({'gateway_milestones': milestones}, journeys)
    outputs = {r['event_id']: r for r in m.with_verified_domains(facts, journeys, chain(facts, usage, journeys))}
    candidates = visited | {m.digest('journey:' + a) for a in visitors}
    delta = {}
    for key in candidates:
        value = outputs.get(key)
        new = content_hash(value) if value else None
        if view.get(index(key), 'projection', key) != new:
            view.set(index(key), 'projection', key, new)
            delta[key] = value
    view.flush()
    tx.head['source_sequence'] = sequence
    return delta
