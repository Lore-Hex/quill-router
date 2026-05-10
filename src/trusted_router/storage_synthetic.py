from __future__ import annotations

import datetime as dt
import threading
from dataclasses import replace

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    raw_sample_is_within_retention,
    rollup_is_within_retention,
    sample_rollup_ids,
)


class InMemorySyntheticChecks:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.samples: list[SyntheticProbeSample] = []
        self.rollups: dict[str, SyntheticRollup] = {}
        self.seen_rollup_samples: set[tuple[str, str]] = set()

    def reset(self) -> None:
        with self._lock:
            self.samples.clear()
            self.rollups.clear()
            self.seen_rollup_samples.clear()

    def record(self, sample: SyntheticProbeSample) -> None:
        with self._lock:
            self.samples.append(sample)
            for period, component in sample_rollup_ids(sample):
                rollup = new_rollup_for_sample(sample, period=period, component=component)
                seen_key = (rollup.id, sample.id)
                if seen_key in self.seen_rollup_samples:
                    continue
                self.seen_rollup_samples.add(seen_key)
                existing = self.rollups.get(rollup.id)
                if existing is None:
                    self.rollups[rollup.id] = rollup
                else:
                    apply_sample_to_rollup(existing, sample)

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
                and raw_sample_is_within_retention(sample, now=dt.datetime.now(dt.UTC))
            ]
        rows.sort(key=lambda sample: sample.created_at, reverse=True)
        return rows[:limit]

    def query_rollups(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_histograms: bool = True,
        limit: int = 1000,
    ) -> list[SyntheticRollup]:
        with self._lock:
            rows = [
                rollup
                for rollup in self.rollups.values()
                if (period is None or rollup.period == period)
                and (since is None or rollup.period_start >= since)
                and (until is None or rollup.period_start <= until)
            ]
        rows = [
            rollup
            for rollup in rows
            if rollup_is_within_retention(rollup, now=dt.datetime.now(dt.UTC))
        ]
        rows.sort(key=lambda rollup: rollup.period_start, reverse=True)
        if not include_histograms:
            rows = [
                replace(rollup, latency_histogram={}, ttfb_histogram={})
                for rollup in rows
            ]
        return rows[:limit]
