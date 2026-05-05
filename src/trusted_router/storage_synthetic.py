from __future__ import annotations

import threading

from trusted_router.storage_models import SyntheticProbeSample


class InMemorySyntheticChecks:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.samples: list[SyntheticProbeSample] = []

    def reset(self) -> None:
        with self._lock:
            self.samples.clear()

    def record(self, sample: SyntheticProbeSample) -> None:
        with self._lock:
            self.samples.append(sample)

    def query(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        with self._lock:
            rows = [
                sample
                for sample in self.samples
                if (date is None or sample.created_at.startswith(date))
                and (target is None or sample.target == target)
                and (probe_type is None or sample.probe_type == probe_type)
                and (monitor_region is None or sample.monitor_region == monitor_region)
            ]
        rows.sort(key=lambda sample: sample.created_at, reverse=True)
        return rows[:limit]
