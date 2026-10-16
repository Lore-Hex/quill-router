"""Mixed sessions must release P5 data without collecting unrelated objects."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests import shadow_v2_gate_cache as cache


@pytest.mark.parametrize('unrelated_position', [None, 0, 1])
def test_module_checks_collect_only_in_package_sessions(monkeypatch, capsys, unrelated_position):
    path = Path('tests/test_async_settle_shadow_v2_gate_window.py')
    items = [SimpleNamespace(path=path, get_closest_marker=lambda name: None)]
    if unrelated_position is not None:
        items.insert(unrelated_position, SimpleNamespace(
            path=Path('tests/test_ci_workflow.py'), get_closest_marker=lambda name: None))
    request = SimpleNamespace(node=SimpleNamespace(path=path),
                              session=SimpleNamespace(items=items))
    calls = []

    def live_objects():
        assert unrelated_position is None, 'mixed session called live_objects'
        calls.append('objects')
        return 100

    def reachable_objects():
        calls.append('reachable')
        return 100

    def installed_lookups():
        calls.append('lookups')
        return ()

    def release_data_caches(*, shared):
        assert shared is False
        calls.append('release')

    def assert_released_data():
        assert calls.count('release') == 1
        calls.append('released')

    for function in (live_objects, reachable_objects, installed_lookups,
                     release_data_caches, assert_released_data):
        monkeypatch.setattr(cache, function.__name__, function)
    counts = conftest._p5_cache_counts.__wrapped__(request)
    next(counts)
    lifetime = conftest._p5_cache_lifetime.__wrapped__(request, None)
    next(lifetime)
    with pytest.raises(StopIteration):
        next(lifetime)
    with pytest.raises(StopIteration):
        next(counts)
    assert calls.count('reachable') == calls.count('lookups') == 2
    assert calls.count('released') == 1
    output = capsys.readouterr().out
    assert 'P5 reachable' in output
    if unrelated_position is None:
        assert calls.count('objects') == 2
        assert request.session._p5_objects_before == 100
        assert 'module_margin=35000 package_margin=35000 oracle=False' in output
        assert 'budget check skipped' not in output
    else:
        assert 'objects' not in calls
        assert not hasattr(request.session, '_p5_objects_before')
        assert 'budget check skipped (mixed session;' in output
