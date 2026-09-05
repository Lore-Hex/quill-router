from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from clickhouse import ingest_operational_outbox as worker


@pytest.mark.parametrize(
    ("batches", "expected_sleeps"),
    [
        ([1, 1, 1], [2, 2, 2]),
        ([0, 0, 1, 0, 0], [2, 4, 2, 2, 4]),
        ([5, 5, 1], [2]),
        ([0, 0, 0, 0, 0, 0], [2, 4, 8, 16, 30, 30]),
    ],
)
@pytest.mark.parametrize("quarantined", [False, True])
def test_worker_paces_partial_batches_without_delaying_full_backlogs(
    monkeypatch: pytest.MonkeyPatch,
    batches: list[int],
    expected_sleeps: list[int],
    quarantined: bool,
) -> None:
    sleeps: list[float] = []
    pending = iter(batches)

    def drain(*args: Any, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["batch_size"] == 5
        fetched = next(pending)
        return SimpleNamespace(
            fetched=fetched,
            inserted=0 if quarantined else fetched,
            quarantined=fetched if quarantined else 0,
            rows_per_second=1.0,
            lag_seconds=0.0,
        )

    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--batch-size", "5", "--poll-seconds", "2"])
    monkeypatch.setattr(worker, "SpannerOperationalOutboxSource", lambda **_: object())
    monkeypatch.setattr(worker, "ClickHouseOperationalWriter", lambda **_: object())
    monkeypatch.setattr(worker, "sd_notify", lambda _: None)
    monkeypatch.setattr(worker, "drain_once", drain)
    monkeypatch.setattr(worker, "time", SimpleNamespace(sleep=sleeps.append))
    with pytest.raises(StopIteration):
        worker.main()
    assert sleeps == expected_sleeps


def test_once_does_not_wait_after_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    monkeypatch.setattr(worker, "SpannerOperationalOutboxSource", lambda **_: object())
    monkeypatch.setattr(worker, "ClickHouseOperationalWriter", lambda **_: object())
    monkeypatch.setattr(worker, "sd_notify", lambda _: None)
    monkeypatch.setattr(
        worker, "drain_once",
        lambda *_, **__: SimpleNamespace(
            fetched=1, inserted=1, quarantined=0, rows_per_second=1.0, lag_seconds=0.0,
        ),
    )

    def unexpected_sleep(_: float) -> None:
        pytest.fail("one-shot drain must not sleep")

    monkeypatch.setattr(worker, "time", SimpleNamespace(sleep=unexpected_sleep))
    assert worker.main() == 0
