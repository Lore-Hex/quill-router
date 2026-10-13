import pytest

from tests.python_pin import pinned_python, require_pinned_python


def test_running_python_matches_repository_pin() -> None:
    require_pinned_python()


@pytest.mark.parametrize("other", [(3, 11, 9), (3, 13, 1)])
def test_other_interpreters_are_rejected(other: tuple[int, int, int]) -> None:
    assert other[:2] != pinned_python()
    with pytest.raises(pytest.UsageError, match=r"\.python-version pins"):
        require_pinned_python(other)
