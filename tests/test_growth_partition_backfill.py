import pytest

from scripts.axiom_growth import model as m
from scripts.axiom_growth import partition_backfill as backfill
from scripts.axiom_growth import partition_store as ps
from scripts.axiom_growth.incremental_projection import Overlay, index
from scripts.axiom_growth.partition_bench import LocalObjects


@pytest.fixture
def db(tmp_path):
    store = LocalObjects(tmp_path / 'restore.db')
    yield store
    store.db.close()


def source():
    hashes = {f'{n:02x}' + m.digest(str(i))[2:]: m.digest(f'{i}:{n}')
              for n in range(0, 256, 3) for i in range(3)}
    return '123456', {'watermark': '2026-11-28T12:00:00+00:00', 'hashes': hashes,
                      'events': [{'event_id': next(iter(hashes)), 'website_client_ip': 'sensitive'}],
                      'daily': [], 'billing_owners': [{'customer_domain': 'private.example'}]}, 30


def forbidden():
    pytest.fail('resume fetched live source a second time')


def test_full_projection_restore_survives_restart_and_source_changes(db):
    snapshot = source()
    expected = snapshot[1]['hashes'].copy()
    assert backfill.step(db, lambda: snapshot)['phase'] == 'write'
    snapshot[1]['hashes'].clear()
    receipts = [backfill.step(db, forbidden) for _ in range(8)]
    assert [r['phase'] for r in receipts] == ['write']*3 + ['verify']*4 + ['complete']
    assert all(r['generation'] == '123456' and r['object_ops'] <= ps.MAX_OPS for r in receipts)
    assert receipts[-1]['verified'] == receipts[-1]['written'] == len(expected)
    version = db.head()[0]
    assert backfill.step(db, forbidden)['phase'] == 'complete' and db.head()[0] == version
    tx = ps.Transaction(db)
    assert not tx.head['watermarks'] and 'outbox' not in tx.head
    view = Overlay(tx)
    for k, v in expected.items():
        assert view.get(index(k), 'projection', k) == v
    for prefix, ref in tx.head['restore']['refs'].items():
        assert tx.read(ref) == {k:v for k,v in expected.items() if k.startswith(prefix)}


def test_crash_before_root_commit_is_safe_to_retry(db, monkeypatch):
    backfill.step(db, source)
    before = db.head()
    def conflict(*args):
        raise ValueError('root conflict')
    with monkeypatch.context() as mp:
        mp.setattr(db, 'swap', conflict)
        with pytest.raises(ValueError, match='conflict'):
            backfill.step(db, forbidden)
    assert db.head() == before
    assert backfill.step(db, forbidden)['next'] == backfill.STRIDE
    for _ in range(7):
        receipt = backfill.step(db, forbidden)
    assert receipt['phase'] == 'complete'


@pytest.mark.parametrize('corruption', ['missing', 'wrong', 'extra', 'filter'])
def test_verification_rejects_incorrect_committed_index_without_advancing(db, corruption):
    backfill.step(db, source)
    for _ in range(4):
        receipt = backfill.step(db, forbidden)
    assert receipt['phase'] == 'verify'
    key = next(iter(source()[1]['hashes']))
    day = index(key)
    tx = ps.Transaction(db)
    if corruption == 'filter':
        pages = tx.day(day)
        pages['filter:' + ps.slot_for(day, 'projection', key)] = {}
        tx.dirty_days[day] = pages
    else:
        tx.apply(day, 'projection', {key if corruption != 'extra' else '0'*64:
                                    None if corruption == 'missing' else m.digest('wrong')})
    tx.commit({})
    before = db.head()
    with pytest.raises(ValueError, match='restored'):
        backfill.step(db, forbidden)
    assert db.head() == before


def test_freeze_refuses_missing_or_unbounded_source_and_nonempty_target(db, monkeypatch):
    snapshot = source()
    snapshot[1]['events'][0]['event_id'] = 'f'*64
    with pytest.raises(ValueError, match='no committed'):
        backfill.step(db, lambda: snapshot)
    with monkeypatch.context() as mp:
        mp.setattr(backfill, 'MAX_PREFIX_ROWS', 1)
        with pytest.raises(ValueError, match='budget'):
            backfill.step(db, source)
    assert db.head()[1] is None
    tx = ps.Transaction(db)
    tx.commit({'gateway': '2026-11-28T12:00:00Z'})
    with pytest.raises(ValueError, match='empty namespace'):
        backfill.step(db, forbidden)
