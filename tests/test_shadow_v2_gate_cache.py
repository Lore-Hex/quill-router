"""Mixed sessions must release P5 data without walking or collecting the heap."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests import shadow_v2_gate_cache as cache
from tests.fakes import frozen_package


@pytest.mark.parametrize('oracle_selection', [False, True])
@pytest.mark.parametrize('unrelated_position', [None, 0, 1])
def test_module_checks_measure_only_in_package_sessions(
        monkeypatch, capsys, unrelated_position, oracle_selection):
    path = Path('tests/test_async_settle_shadow_v2_gate_window.py')
    items = [SimpleNamespace(path=path, get_closest_marker=lambda name: oracle_selection)]
    if unrelated_position is not None:
        items.insert(unrelated_position, SimpleNamespace(
            path=Path('tests/test_ci_workflow.py'), get_closest_marker=lambda name: None))
    request = SimpleNamespace(node=SimpleNamespace(path=path),
                              session=SimpleNamespace(items=items))
    calls = []
    release = cache.release_data_caches
    check_released = cache.assert_released_data
    get_lookups = cache.installed_lookups

    def forbidden_heap_measurement(*args, **kwargs):
        raise AssertionError('mixed session walked or collected the heap')

    if unrelated_position is not None:
        import gc

        monkeypatch.setattr(frozen_package, '_references', forbidden_heap_measurement)
        monkeypatch.setattr(gc, 'collect', forbidden_heap_measurement)
        monkeypatch.setattr(gc, 'get_objects', forbidden_heap_measurement)
        monkeypatch.setattr(gc, 'get_referents', forbidden_heap_measurement)

    def live_objects():
        assert unrelated_position is None, 'mixed session called live_objects'
        calls.append('objects')
        return 100

    def reachable_objects():
        calls.append('reachable')
        return 100

    def installed_lookups():
        calls.append('lookups')
        return get_lookups()

    def release_data_caches(*, shared):
        assert shared is oracle_selection
        release(shared=shared)
        calls.append('release')

    def assert_released_data():
        assert calls.count('release') == 1
        check_released()
        calls.append('released')

    for function in (live_objects, reachable_objects, installed_lookups,
                     release_data_caches, assert_released_data):
        monkeypatch.setattr(cache, function.__name__, function)
    if unrelated_position is not None:
        monkeypatch.setattr(cache, 'reachable_objects', forbidden_heap_measurement)
        monkeypatch.setattr(cache, 'live_objects', forbidden_heap_measurement)
    counts = conftest._p5_cache_counts.__wrapped__(request)
    next(counts)
    lifetime = conftest._p5_cache_lifetime.__wrapped__(request, None)
    next(lifetime)
    with pytest.raises(StopIteration):
        next(lifetime)
    with pytest.raises(StopIteration):
        next(counts)
    assert calls.count('lookups') == 2
    assert calls.count('released') == 1
    output = capsys.readouterr().out
    if unrelated_position is None:
        assert calls.count('reachable') == calls.count('objects') == 2
        assert 'margin=5000' in output
        assert request.session._p5_objects_before == 100
        assert f'module_margin=35000 package_margin=35000 oracle={oracle_selection}' in output
        assert 'budget checks skipped' not in output
    else:
        assert 'objects' not in calls
        assert 'reachable' not in calls
        assert not hasattr(request.session, '_p5_objects_before')
        assert output.splitlines() == [
            f'P5 {path.name}: reachable_objects and live_objects budget checks skipped '
            '(mixed session; heap measurements are package-only)']
