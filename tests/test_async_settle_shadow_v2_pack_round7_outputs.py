"""Later file costs retain separate reservations and following-pass receipts."""
from __future__ import annotations

import copy

import pytest

from trusted_router.async_settle_shadow_v2_pack_output_costs import (
    compare_files,
    following_totals,
    reserve_files,
)
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles


def example(tmp_path):
    files = PackFiles(tmp_path)
    refs = [files.put(group + '/record.json', b'123') for group in ('coverage', 'roots', 'review', 'report')]
    ceiling = dict(export_operations=4, export_bytes=16, export_write_bytes=16, native_rpcs=0)
    return files, refs, reserve_files(refs, source_pass=1, ceiling=ceiling)


def test_later_files_enter_following_totals_only(tmp_path):
    files, refs, plan = example(tmp_path)
    before = copy.deepcopy(plan)
    measured = compare_files(files, plan, refs)
    assert plan == before
    assert measured['output_pass'] == 2 and measured['totals_pass'] == 3
    assert following_totals(files, [measured], current_pass=3) == dict(
        export_operations=4, export_bytes=12, export_write_bytes=12, native_rpcs=0)
    for current in (1, 2, 4):
        with pytest.raises(ValueError, match='LaterOutputOrder'):
            following_totals(files, [measured], current_pass=current)
    with pytest.raises(ValueError, match='LaterOutputPopulation'):
        following_totals(files, [measured, measured], current_pass=3)
    assert all(set(r['object']) == {'path', 'bytes', 'sha256'} for r in measured['receipts'])
    assert not any('receipt' in r['object']['path'] for r in measured['receipts'])


def test_later_file_over_reservation_is_not_measured(tmp_path):
    files, refs, plan = example(tmp_path)
    plan['reservations'][0]['bytes'] += 4
    measured = compare_files(files, plan, refs)
    assert measured['reserved']['export_bytes'] == 16
    assert measured['measured']['export_bytes'] == 12
    plan['reservations'][0]['bytes'] += 1
    with pytest.raises(ValueError, match='evidence_export_budget_gap'):
        compare_files(files, plan, refs)


@pytest.mark.parametrize('damage', ['skip', 'unreserved', 'exceeds', 'missing'])
def test_later_file_population_and_size(tmp_path, damage):
    files, refs, plan = example(tmp_path)
    if damage == 'skip':
        plan['reservations'].pop()
    elif damage == 'unreserved':
        refs.append(files.put('report/unreserved.json', b'x'))
    elif damage == 'exceeds':
        (files.root / refs[0]['path']).unlink()
        refs[0] = files.put(refs[0]['path'], b'1234')
    else:
        refs.pop()
    with pytest.raises(ValueError, match='evidence_export_budget_gap' if damage == 'exceeds' else 'LaterOutputPopulation'):
        compare_files(files, plan, refs)
