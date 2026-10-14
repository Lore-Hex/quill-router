"""Finite window orchestration over original receipt and sample records."""
from __future__ import annotations

import copy
import json

import pytest

from tests.shadow_v2_pack_round3_captures import writer_capture
from tests.test_async_settle_shadow_v2_pack import DAY, START, review
from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan, input_plan
from trusted_router.async_settle_shadow_v2_pack_procedures import REQUIRED_STAGES
from trusted_router.async_settle_shadow_v2_pack_window import (
    collect_window,
    predecessors,
    reconciliation,
)
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def terminal_sources(tmp_path_factory):
    capture = writer_capture(tmp_path_factory.mktemp('window-writer'))
    pages = [json.loads(r['raw']) for r in capture['bundle']['rows'] if r['kind'] == p1.KINDS[2]]
    samples = [json.loads(r['raw']) for r in capture['bundle']['rows'] if r['kind'] == p1.KINDS[0]]
    page, issue = max(((p, e) for p in pages for e in p['events'] if e['phase'] == 'authorize'),
        key=lambda pair: pair[1]['event_at_us'])
    terminal = next(e for p in pages for e in p['events'] if e['phase'] == 'settle' and e['identity'] == issue['identity'])
    original = copy.deepcopy(page)
    original['events'] = [copy.deepcopy(issue)]
    original['first_seq'] = original['last_seq'] = issue['seq']
    original['events'][0]['receipt_locator']['first_seq'] = issue['seq']
    end = copy.deepcopy(terminal)
    end['event_at_us'] += 86400000000
    end['receipt_at_us'] += 86400000000
    later = dict(v=2, policy_version='shadow-v2', day='2026-10-13', boot=page['boot'],
        first_seq=end['seq'], last_seq=end['seq'], previous_page_sha256=None, events=[end])
    sample = copy.deepcopy(next(s for s in samples if s['original_terminal_receipt']['event_seq'] == end['seq']))
    sample['original_terminal_receipt']['day'] = later['day']
    return original, later, sample


def source_plan(root, terminal_sources):
    source = PackFiles(root)
    issue, end, sample = terminal_sources
    days = ['2026-10-' + str(n).zfill(2) for n in range(7, 15)]
    plans = {}
    for day in days:
        rows = []
        for value, kind in ((issue, p1.KINDS[2]), (end, p1.KINDS[2]), (sample, p1.KINDS[0])):
            observed = value['day'] if kind == p1.KINDS[2] else value['original_terminal_receipt']['day']
            if observed == day:
                rows.append(dict(kind=kind, id=day + '/' + str(len(rows)), raw=canonical(value).decode()))
        bundle = source.obj('native/' + day + '.json', dict(rows=rows, attachments=[]))
        spec = source.obj('captures/' + day, dict(bundle=bundle['path']))
        plans[day] = dict(day=day, days=[day], captures=dict(NativeExport=spec),
            complete_through_us=START + (int(day[-2:]) - 12 + 3) * 86400000000)
    return source, dict(days=days[:6], day_plans=plans, predecessors=[],
        checkpoint='shadow-serving', required_stages={s: list(n) for s, n in REQUIRED_STAGES.items()}, verifiers={})


def make_predecessor(files, name, previous):
    original = files.obj(name + '/original', dict(value=name))
    gate = files.obj(name + '/gate', dict(verdict='BLOCKED', completed_at_us=START))
    released = files.obj(name + '/release', dict(artifacts={original['path']: original['sha256']},
        criteria={}, required=[], verifiers={}))
    inputs = input_plan(files, [original, gate], days=[DAY], predecessors=[], prefix=name + '/inputs')
    return files.obj(name + '/descriptor', dict(format='shadow-v2-reviewed-predecessor-1',
        inputs=inputs, release_plan=released, release_sha256=released['sha256'], gate=gate, predecessors=previous))


def day_collector(calls, *, expect_terminal=True):
    def collect(source, out, plan, *, reconciliation):
        day = plan['day']
        # The daily assembler sees the same original support records, one day
        # per invocation, with its own independently supplied horizon.
        calls.append((day, plan['complete_through_us']))
        if day == DAY:
            page, event = next((p, e) for p, e in reconciliation.events.values() if e['phase'] == 'authorize')
            found = reconciliation.terminal(joins.authorization_obligation(page, event['seq']))
            assert (found[0]['day'] if found else None) == ('2026-10-13' if expect_terminal else None)
        files = PackFiles(out)
        subject = files.obj('subject.json', dict(day=day))
        inputs = input_plan(files, [subject], days=[day], predecessors=[])
        files.obj('stage/transport.json', dict(v=2, policy_version='shadow-v2', contract_revision='a1',
            days=[day], rows=[], attachments=[], ledgers=[], preimages=[], completed_at_us=plan['complete_through_us']))
        return dict(complete=False, missing=['base-day'], subject=subject, inputs=inputs, gate={'verdict': 'BLOCKED'})
    return collect


def test_eight_dates_support_horizons_and_predecessor_files(tmp_path, terminal_sources):
    source, plan = source_plan(tmp_path / 'source', terminal_sources)
    first = make_predecessor(source, 'prior/pre', [])
    second = make_predecessor(source, 'prior/fleet', [first])
    plan['predecessors'] = [second]
    calls = []
    result = collect_window(source.root, tmp_path / 'out', plan, day_collector(calls))
    expected = sorted(plan['day_plans'])
    assert [d for d, _ in calls] == expected and len(calls) == 8
    assert calls == [(day, plan['day_plans'][day]['complete_through_us']) for day in expected]
    assert result['inputs']['days'] == expected
    assert result['inputs']['predecessors'] == sorted([first, second], key=lambda r: r['path'])
    found = {ref['path'] for ref in check_plan(PackFiles(tmp_path / 'out'), result['inputs'])}
    assert {'prior/pre/original', 'prior/fleet/original', first['path'], second['path']} <= found
    assert not result['complete'] and 'MultiBundleLoading' in result['missing']
    subjects = result['review']['subjects']
    assert set(subjects) == set(expected)
    for day in expected:
        child = json.loads((tmp_path / 'out' / subjects[day]['path']).read_bytes())
        assert child['complete_through_us'] == plan['day_plans'][day]['complete_through_us']


@pytest.mark.parametrize('deleted', ['2026-10-13', '2026-10-14'])
def test_support_day_deletion_does_not_shorten_window(tmp_path, terminal_sources, deleted):
    source, plan = source_plan(tmp_path / 'source', terminal_sources)
    del plan['day_plans'][deleted]
    calls = []
    collect = day_collector(calls, expect_terminal=deleted != '2026-10-13')
    result = collect_window(source.root, tmp_path / 'out', plan, collect)
    assert 'MissingSupportDay:' + deleted in result['missing']
    assert deleted in result['inputs']['days'] and not result['complete']


def test_missing_predecessor_original_and_changed_review_block(tmp_path):
    for damage in ('original', 'review'):
        source = PackFiles(tmp_path / damage)
        ref = make_predecessor(source, 'prior', [])
        if damage == 'original':
            (source.root / 'prior/original').unlink()
        else:
            value = json.loads(source.read(ref))
            value['release_sha256'] = '0' * 64
            ref = source.obj('changed', value)
        with pytest.raises((ValueError, OSError)):
            predecessors(source, PackFiles(tmp_path / ('out-' + damage)), [ref])


def test_public_window_collect_review_and_final_plan(tmp_path, terminal_sources, monkeypatch):
    from scripts.async_settle import shadow_v2_window as command
    source, selected = source_plan(tmp_path / 'source', terminal_sources)
    plan = dict(default_stage='window', stages=dict(window=selected))
    monkeypatch.setattr(command, 'collect_day', day_collector([]))
    collected = tmp_path / 'collected'
    result = command.window('collect', plan, source=source.root, out=collected)
    assert result['kind'] == 'window' and not result['complete']
    decisions = PackFiles(collected)
    selected['daily_reviews'] = {d: review(decisions, ref, 'daily-reviews/' + d)
        for d, ref in result['review']['subjects'].items()}
    reviewed = command.window('review-inputs', plan, source=collected, out=tmp_path / 'review')
    selected['release_plan_sha256'] = reviewed['review']['release_plan']['sha256']
    packed = command.window('pack', plan, source=collected, out=tmp_path / 'final')
    assert packed['kind'] == 'window' and packed['missing'] == result['missing']
    assert check_plan(PackFiles(tmp_path / 'final'), packed['inputs'])
    assert (tmp_path / 'final/review/sealed.json').exists()


def test_support_selection_uses_originals(tmp_path, terminal_sources):
    source, plan = source_plan(tmp_path / 'source', terminal_sources)
    joined = reconciliation(source.root, plan['day_plans'])
    assert joined.support_days(plan['days']) == tuple(sorted(plan['day_plans']))
    changed = copy.deepcopy(plan['day_plans'])
    changed[DAY]['days'] = ['2026-10-11']
    with pytest.raises(ValueError, match='FiniteDayPlan'):
        reconciliation(source.root, changed)
