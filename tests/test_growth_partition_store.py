import gzip
import hashlib
import json

import pytest

from scripts.axiom_growth import partition_shadow as shadow
from scripts.axiom_growth import partition_store as p
from scripts.axiom_growth.partition_bench import LocalObjects

DAY = '2026-11-27'
A, B = 'a'*64, 'b'*64
MARK = DAY + 'T12:00:00+00:00'


@pytest.fixture
def store(tmp_path):
    db = LocalObjects(tmp_path / 'objects.db')
    yield db
    db.db.close()


def test_loser_and_crashed_candidate_are_invisible_and_committed_replay_is_stable(store):
    winner, loser = p.Transaction(store), p.Transaction(store)
    for tx, label in [(winner, 'winner'), (loser, 'loser')]:
        tx.apply(DAY, 'fact', {A: label})
        tx.stage_delivery([[{'event_id': A, 'exported_at': MARK, 'label': label}]])
        with pytest.raises(ValueError, match='fresh committed'):
            tx.drain(lambda _: pytest.fail('candidate visible'))
    assert p.Transaction(store).head == p.empty()
    winner.commit({'acquisition': MARK})
    with pytest.raises(ValueError, match='conflict'):
        loser.commit({'acquisition': MARK})
    with pytest.raises(ValueError, match='drain'):
        p.Transaction(store).commit({})
    attempts = []

    def interrupted(rows):
        attempts.extend(rows)
        raise ConnectionError('no acknowledgement')

    with pytest.raises(ConnectionError):
        p.Transaction(store).drain(interrupted)
    assert p.Transaction(store).drain(attempts.extend) == 1
    assert attempts[0] == attempts[1] == {'event_id': A, 'exported_at': MARK, 'label': 'winner'}
    assert p.Transaction(store).drain(attempts.extend) == 0


def test_compaction_deletes_late_changes_other_days_untouched(store, monkeypatch):
    monkeypatch.setattr(p, 'MAX_DEPTH', 2)
    tx = p.Transaction(store)
    tx.apply(DAY, 'fact', {A: '1', 'aa'*31 + '00': 'other'})
    tx.apply('2026-11-26', 'fact', {B: 'historical'})
    tx.commit({'gateway': MARK})
    cold = p.Transaction(store).head['days']['2026-11-26']
    tx = p.Transaction(store)
    tx.apply(DAY, 'fact', {A: '2'})
    tx.commit({'acquisition': MARK})
    tx = p.Transaction(store)
    tx.apply(DAY, 'fact', {A: None})
    tx.commit({})
    check = p.Transaction(store)
    assert check.head['days']['2026-11-26'] == cold
    refs = check.day(DAY)['fact:a']
    assert len(refs) == 1 and check.fold(refs) == {'aa'*31 + '00': 'other'}
    assert check.head['watermarks'] == {'acquisition': MARK, 'gateway': MARK}
    with pytest.raises(ValueError, match='regressing'):
        check.commit({'gateway': '2026-11-01T00:00:00+00:00'})


def test_corruption_budget_and_oversized_payload_fail_without_advancing(store, monkeypatch):
    tx = p.Transaction(store)
    tx.apply(DAY, 'fact', {A: 'good'})
    tx.commit({})
    good = store.head()
    with monkeypatch.context() as mp:
        mp.setattr(store, 'get', lambda _: b'bad')
        with pytest.raises(ValueError, match='digest'):
            p.Transaction(store).day(DAY)
    with monkeypatch.context() as mp:
        mp.setattr(p, 'MAX_OPS', 1)
        tx = p.Transaction(store)
        with pytest.raises(ValueError, match='IO budget'):
            tx.apply(DAY, 'fact', {B: 'extra'})
        with pytest.raises(ValueError, match='closed'):
            tx.commit({})
    monkeypatch.setattr(p, 'PAGE_BYTES', 90)
    with pytest.raises(ValueError, match='raw budget'):
        p.decode(gzip.compress(b'x'*1000))
    assert store.head() == good


def test_hash_only_shadow_reconciles_source_changes_removals_and_expiry(store):
    snapshot = {'watermark': MARK, 'events': [{'event_id': A, '_time': MARK, 'customer_domain': 'private.example'}],
                'daily': [{'event_id': B, '_time': MARK}], 'hashes': {A: A, B: B}}
    first = shadow.reconcile(store, snapshot)
    assert (first['checked'], first['changed']) == (2, 2)
    assert shadow.reconcile(store, snapshot)['changed'] == 0
    snapshot['events'] = []
    snapshot['hashes'][B] = A
    assert shadow.reconcile(store, snapshot)['changed'] == 2
    for (blob,) in store.db.execute('SELECT b FROM objects'):
        assert b'private.example' not in gzip.decompress(blob)
    snapshot['watermark'] = '2026-11-30T12:00:00+00:00'
    assert shadow.reconcile(store, snapshot)['checked'] == 0
    assert not p.Transaction(store).head['days']


def test_shadow_gcs_never_writes_to_legacy_and_checks_preconditions():
    import httpx

    class Credentials:
        valid = True
        token = 'dummy'  # noqa: S105 - MockTransport fixture, no network.

    objs = {}
    version = 0

    def respond(req):
        nonlocal version
        params = req.url.params
        if req.method == 'POST':
            name = params['name']
            assert name.startswith(shadow.PREFIX)
            old = objs.get(name, ('0', b''))
            if params['ifGenerationMatch'] != old[0]:
                return httpx.Response(412)
            version += 1
            objs[name] = (str(version), req.content)
            return httpx.Response(200, json={'generation': str(version)})
        name = req.url.path.split('/o/', 1)[1]
        if name not in objs:
            return httpx.Response(404)
        gen, body = objs[name]
        if params.get('alt') == 'media':
            assert params.get('generation', gen) == gen
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={'generation': gen, 'size': str(len(body))})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        backend = shadow.ShadowObjects(http, Credentials(), 'test')
        assert backend.head() == ('0', None)
        blob = json.dumps({'a': 2}).encode()
        ref = hashlib.sha256(blob).hexdigest()
        backend.put(ref, blob)
        backend.put(ref, blob)
        ver = backend.swap('0', blob)
        assert backend.head() == (ver, blob)
        with pytest.raises(ValueError, match='conflict'):
            backend.swap('0', blob)


def test_dirty_projection_matches_reference_on_late_link_ambiguity_and_expiry(store):
    import datetime as dt
    from itertools import chain

    from scripts.axiom_growth import incremental_projection as inc
    from scripts.axiom_growth import model as m
    from scripts.axiom_growth.runtime import content_hash

    end = dt.datetime.fromisoformat(MARK)
    u = m.digest('daily')
    usage = {'event_id': u, 'event': 'growth.daily_usage', '_time': '2026-05-01T00:00:00Z',
             'workspace_fingerprint': A, 'successful_calls': 2, 'input_tokens': 22, 'output_tokens': 4}
    facts = [m.project_event({'event': 'acquisition.signup_completed', '_time': MARK,
             'anonymous_fingerprint': m.digest(f'visitor:{i}'), 'account_fingerprint': m.digest(f'account:{i}'),
             'workspace_fingerprint': A, 'utm_source': f'campaign{i}'}, source='axiom') for i in range(2)]
    history, daily, delivered = {}, {}, {}
    for changes in [[('usage', u, usage)], [('fact', facts[0]['event_id'], facts[0])],
                    [('fact', facts[1]['event_id'], facts[1])],
                    [('fact', facts[0]['event_id'], None)], [('fact', facts[1]['event_id'], None)],
                    [('usage', u, None)]]:
        for kind, key, row in changes:
            collection = history if kind == 'fact' else daily
            if row is None:
                del collection[key]
            else:
                collection[key] = row
        tx = p.Transaction(store)
        delta = inc.project(tx, changes, end)
        tx.commit({'acquisition': MARK})
        for key, value in delta.items():
            if value is None:
                del delivered[key]
            else:
                delivered[key] = content_hash(value)
        rows = [dict(r) for r in daily.values()]
        journeys = m.build_journeys(list(history.values()), rows, end)
        reference = m.with_verified_domains(history.values(), journeys, chain(history.values(), rows, journeys))
        assert delivered == {r['event_id']: content_hash(r) for r in reference}
        snapshot = {'watermark': MARK, 'events': list(history.values()), 'daily': rows, 'hashes': delivered}
        assert shadow.component_receipt(snapshot)['component_projections_checked'] == len(delivered)
        if delivered:
            with pytest.raises(ValueError, match='oracle'):
                shadow.component_receipt({**snapshot, 'hashes': {}})
        assert inc.project(p.Transaction(store), changes, end) == {}


@pytest.mark.parametrize('ambiguous', [False, True])
def test_commercial_projection_rebuilt_even_when_cached_billing_fields_lie(ambiguous):
    from scripts.axiom_growth import model as m
    from scripts.axiom_growth.runtime import checkpoint_facts, content_hash

    owner, workspace = m.digest('commercial-owner'), m.digest('caller-workspace')
    facts = [m.project_event({'event': 'acquisition.signup_completed', '_time': MARK,
             'anonymous_fingerprint': m.digest(str(i)), 'workspace_fingerprint': m.digest(f'other{i}'),
             'account_fingerprint': owner, 'utm_source': f'campaign{i}'}, source='axiom')
             for i in range(2 if ambiguous else 1)]
    pivot = int(m.digest(m.date(MARK).isoformat()), 16)
    for i in range(4):
        # The four ordinary seeds select unrelated visitors, not the owner's facts.
        other = m.project_event({'event': 'acquisition.landing_viewed', '_time': MARK,
                                 'anonymous_fingerprint': m.digest(f'decoy{i}')}, source='cloud_logging')
        facts.append({**other, 'event_id': f'{pivot+i:064x}'})
    daily = [{'event_id': m.digest('usage'), 'event': 'growth.daily_usage', '_time': MARK,
              'workspace_fingerprint': workspace, 'successful_calls': 3, 'input_tokens': 12, 'output_tokens': 6}]
    owners = [{'workspace_fingerprint': workspace, 'billing_account_fingerprint': owner,
               'billing_owner_observed_at': MARK}]
    journeys = m.build_journeys(facts, daily, m.date(MARK))
    m.link_billing_accounts(daily, owners, journeys)
    assert daily[0]['billing_source'] == ('(unattributed)' if ambiguous else 'campaign0')
    hashes = {r['event_id']: content_hash(r) for r in [*facts, *daily, *journeys]}
    bad = {**daily[0], 'billing_source': 'wrong'}
    state = checkpoint_facts({'events': facts, 'daily': [bad], 'watermark': MARK, 'hashes': hashes,
                              'billing_directory_read_at': MARK, 'billing_owner_status': 'current',
                              'billing_owners': owners})
    receipt = shadow.component_receipt(state)
    assert (receipt['billing_usage_checked'], receipt['billing_linked_checked']) == (1, 1)
    assert receipt['billing_directory_read_at'] == MARK
    with pytest.raises(ValueError, match='oracle'):
        shadow.component_receipt({**state, 'hashes': {**hashes, bad['event_id']: content_hash(bad)}})
    with pytest.raises(ValueError, match='read evidence'):
        shadow.component_receipt({**state, 'billing_directory_read_at': None})
    with pytest.raises(ValueError, match='oracle'):
        shadow.component_receipt({**state, 'billing_owners': []})


def test_broad_dirty_identity_fanout_is_packed_without_raising_io_guard(store):
    import datetime as dt

    from scripts.axiom_growth import incremental_projection as inc
    from scripts.axiom_growth import model as m

    end = dt.datetime.fromisoformat(MARK)
    rows = [m.project_event({'event': 'acquisition.signup_completed', '_time': MARK,
                             'anonymous_fingerprint': m.digest(f'v:{i}'),
                             'workspace_fingerprint': m.digest(f'w:{i}'),
                             'account_fingerprint': m.digest(f'a:{i}')}, source='axiom') for i in range(730)]
    for campaign in ('first', 'late'):
        tx = p.Transaction(store)
        delta = inc.project(tx, [('fact', r['event_id'], {**r, 'utm_campaign': campaign}) for r in rows], end)
        assert len(delta) == 2*len(rows)
        tx.commit({'acquisition': MARK})
        assert tx.ops <= 16


def test_verified_pack_eviction_never_refetches_or_leaks_a_temp_path(store, monkeypatch):
    monkeypatch.setattr(p, 'CACHE_BYTES', 20)
    bodies = [b'a'*15, b'b'*16]
    for body in bodies:
        store.put(p.digest(body), body)
    tx = p.Transaction(store)
    for _ in range(4):
        for body in bodies:
            assert tx.blob(p.digest(body)) == body
    assert tx.ops == 3
    assert tx.spill is not None
    tx.commit({})
    assert tx.spill is None


def test_index_summary_never_masks_updates_tombstones_or_legacy_pages(store):
    import base64

    from scripts.axiom_growth.incremental_projection import Overlay

    keys = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(1000)]
    bits = base64.b64decode(p.key_filter(keys))
    assert all(p.might_contain(bits, k) for k in keys)
    tx = p.Transaction(store)
    day = 'index-aa'
    tx.apply(day, 'dependency', {A: 'old'})
    tx.commit({})
    tx = p.Transaction(store)
    tx.apply(day, 'dependency', {A: None})
    tx.commit({})
    tx = p.Transaction(store)
    view = Overlay(tx)
    assert view.get(day, 'dependency', A) is None
    # An old writer makes a layer without a summary; it must always be read.
    pages = tx.day(day)
    slot = p.slot_for(day, 'dependency', A)
    pages[slot].append(tx.put({A: 'new'}))
    tx.dirty_days[day] = pages
    tx.commit({})
    assert Overlay(p.Transaction(store)).get(day, 'dependency', A) == 'new'
