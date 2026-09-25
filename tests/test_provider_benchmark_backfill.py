from __future__ import annotations

import pytest

from trusted_router.provider_benchmark_backfill_cli import run


class _PagedStore:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int]] = []

    def backfill_provider_benchmark_indexed_at_page(
        self, *, after: str | None = None, limit: int = 500
    ) -> list[str]:
        self.calls.append((after, limit))
        return self.pages.pop(0)


def test_backfill_runs_pages_until_an_empty_one_and_moves_the_cursor() -> None:
    store = _PagedStore([["a", "b"], ["c"], []])

    assert run(store, batch_size=2) == 3
    assert store.calls == [(None, 2), ("b", 2), ("c", 2)]


def test_backfill_resumes_after_a_given_id() -> None:
    store = _PagedStore([[]])

    assert run(store, after="m") == 0
    assert store.calls == [("m", 500)]


@pytest.mark.parametrize("batch_size", [0, 1_001])
def test_backfill_refuses_a_batch_size_outside_one_to_a_thousand(batch_size: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        run(_PagedStore([]), batch_size=batch_size)
