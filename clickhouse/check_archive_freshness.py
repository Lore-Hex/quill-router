"""Alert when the verified ClickHouse archive stops advancing.

``archive_daily`` exports every closed day of each dataset and advances
``raw/<dataset>/day=<day>/_latest.json`` only after the export has been read
back and fingerprint-matched; ``verify_archive_restore`` then restores that
day again as a drill. Both run on a ClickHouse node under systemd timers, so
when they fail nothing outside the node notices — on 2026-08-16 a
service-account swap left both units failing for ten hours before a person
happened to look at the bucket (NC-005).

This check runs from *outside* the node, after the timers should have fired,
and asks the only question that matters: does yesterday's pointer exist, for
every dataset, and does it name a manifest that exists? It therefore catches
"the archiver never ran" (node down, timer disabled, unit masked) as well as
"the archiver ran and failed", which an ``OnFailure=`` hook on the unit cannot.

It reads the bucket through ``gcloud storage`` so it needs no Python
dependencies beyond the standard library and whatever identity gcloud holds.
A permission or transport error is reported as its own failure, never folded
into "missing": a check that reads 403 as "not archived yet" would be wrong in
the same way the incident was.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from clickhouse.archive_daily import ARCHIVE_BUCKET, ARCHIVE_SCHEMA_VERSION, DATASETS

# Observed on 2026-08-16 against the real bucket:
#   gcloud storage cat  -> "The following URLs matched no objects or files: ..."
#   gcloud storage ls   -> "One or more URLs matched no objects."
# The HTTP forms are kept for the API-error path. Nothing here matches a 403.
_NOT_FOUND_MARKERS = (
    "matched no objects",
    "No URLs matched",
    "HTTPError 404",
    "NotFound",
)


class StoreError(RuntimeError):
    """The archive could not be read for a reason other than absence."""


class FreshnessStore(Protocol):
    def read_json(self, key: str) -> dict[str, Any] | None: ...

    def exists(self, key: str) -> bool: ...


@dataclasses.dataclass(frozen=True)
class CompletedCommand:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], CompletedCommand]


def _run_gcloud(argv: list[str]) -> CompletedCommand:
    result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return CompletedCommand(result.returncode, result.stdout, result.stderr)


class GcloudStore:
    """Read the archive bucket through the gcloud CLI.

    ``gcloud storage cat`` and ``gcloud storage ls`` exit non-zero both when
    an object is absent and when it cannot be read; the two are told apart by
    the error text, and only absence is reported as ``None``/``False``.
    """

    def __init__(self, *, bucket: str, runner: Runner = _run_gcloud) -> None:
        self._bucket = bucket
        self._run = runner

    def _url(self, key: str) -> str:
        return f"gs://{self._bucket}/{key}"

    @staticmethod
    def _is_not_found(stderr: str) -> bool:
        return any(marker in stderr for marker in _NOT_FOUND_MARKERS)

    def read_json(self, key: str) -> dict[str, Any] | None:
        result = self._run(["gcloud", "storage", "cat", self._url(key)])
        if result.returncode != 0:
            if self._is_not_found(result.stderr):
                return None
            raise StoreError(f"cannot read {self._url(key)}: {result.stderr.strip()}")
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise StoreError(f"{self._url(key)} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise StoreError(f"{self._url(key)} is not a JSON object")
        return value

    def exists(self, key: str) -> bool:
        result = self._run(["gcloud", "storage", "ls", self._url(key)])
        if result.returncode == 0:
            return True
        if self._is_not_found(result.stderr):
            return False
        raise StoreError(f"cannot list {self._url(key)}: {result.stderr.strip()}")


@dataclasses.dataclass(frozen=True)
class Problem:
    dataset: str
    day: dt.date
    message: str

    def __str__(self) -> str:
        return f"{self.dataset} day={self.day.isoformat()}: {self.message}"


@dataclasses.dataclass(frozen=True)
class DatasetStatus:
    dataset: str
    day: dt.date
    revision: str | None
    updated_at: str | None
    problems: tuple[Problem, ...]

    @property
    def fresh(self) -> bool:
        return not self.problems


def pointer_key(dataset: str, day: dt.date) -> str:
    return f"raw/{dataset}/day={day.isoformat()}/_latest.json"


def _parse_updated_at(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def check_dataset(
    store: FreshnessStore,
    dataset: str,
    day: dt.date,
) -> DatasetStatus:
    """Judge one dataset-day. Every defect is a separate, specific problem."""
    key = pointer_key(dataset, day)
    problems: list[Problem] = []

    def problem(message: str) -> None:
        problems.append(Problem(dataset, day, message))

    pointer = store.read_json(key)
    if pointer is None:
        problem(f"no pointer at {key}; the day was never archived")
        return DatasetStatus(dataset, day, None, None, tuple(problems))

    revision = pointer.get("revision")
    if not isinstance(revision, str) or not revision:
        problem("pointer has no revision")
        revision = None

    if pointer.get("day") != day.isoformat():
        problem(f"pointer says day={pointer.get('day')!r}, expected {day.isoformat()}")

    if pointer.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        problem(
            f"pointer schema_version={pointer.get('schema_version')!r}, "
            f"expected {ARCHIVE_SCHEMA_VERSION}"
        )

    manifest_key = pointer.get("manifest")
    if not isinstance(manifest_key, str) or not manifest_key:
        problem("pointer names no manifest")
    elif not store.exists(manifest_key):
        problem(f"pointer names manifest {manifest_key}, which does not exist")

    updated_raw = pointer.get("updated_at")
    updated_at = _parse_updated_at(updated_raw)
    if updated_at is None:
        problem(f"pointer updated_at={updated_raw!r} is not a timezone-aware timestamp")
    else:
        day_closed = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)
        if updated_at < day_closed:
            problem(
                f"pointer was written at {updated_at.isoformat()}, before the day closed "
                f"at {day_closed.isoformat()}; the archive would be of a partial day"
            )

    return DatasetStatus(
        dataset,
        day,
        revision,
        updated_raw if isinstance(updated_raw, str) else None,
        tuple(problems),
    )


def check_days(
    store: FreshnessStore,
    days: Iterable[dt.date],
    datasets: Iterable[str] = tuple(DATASETS),
) -> list[DatasetStatus]:
    statuses: list[DatasetStatus] = []
    for day in days:
        for dataset in datasets:
            statuses.append(check_dataset(store, dataset, day))
    return statuses


def days_to_check(*, now: dt.datetime, lookback: int) -> list[dt.date]:
    """The closed days that should be archived by ``now``: yesterday backwards.

    ``lookback`` is how many closed days to check; 1 means yesterday only.
    Today is never checked — the archiver only exports closed days.
    """
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    yesterday = now.astimezone(dt.UTC).date() - dt.timedelta(days=1)
    return [yesterday - dt.timedelta(days=offset) for offset in range(lookback)]


def format_report(statuses: Iterable[DatasetStatus]) -> str:
    lines: list[str] = []
    for status in statuses:
        if status.fresh:
            lines.append(
                f"OK    {status.dataset} day={status.day.isoformat()} "
                f"revision={status.revision} updated_at={status.updated_at}"
            )
        else:
            for problem in status.problems:
                lines.append(f"STALE {problem}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=ARCHIVE_BUCKET)
    parser.add_argument(
        "--day",
        type=dt.date.fromisoformat,
        help="check this closed day instead of yesterday (UTC)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=1,
        help="how many closed days to check, ending yesterday (default 1)",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=sorted(DATASETS),
        help="restrict to a dataset (repeatable; default all)",
    )
    parser.add_argument(
        "--problems-file",
        help="write one problem per line here on failure, for the alerting step",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    store: FreshnessStore | None = None,
    now: dt.datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parse_args(argv)
    if args.day is not None:
        days = [args.day]
    else:
        days = days_to_check(now=now or dt.datetime.now(dt.UTC), lookback=args.lookback)
    datasets: tuple[str, ...] = tuple(args.dataset) if args.dataset else tuple(DATASETS)
    active_store: FreshnessStore = store or GcloudStore(bucket=args.bucket)

    try:
        statuses = check_days(active_store, days, datasets)
    except StoreError as exc:
        out(f"::error::archive freshness could not be determined: {exc}")
        if args.problems_file:
            with open(args.problems_file, "w", encoding="utf-8") as handle:
                handle.write(f"could not read the archive: {exc}\n")
        return 2

    out(format_report(statuses))
    problems = [problem for status in statuses for problem in status.problems]
    if problems:
        out(f"::error::{len(problems)} archive freshness problem(s) in gs://{args.bucket}")
        if args.problems_file:
            with open(args.problems_file, "w", encoding="utf-8") as handle:
                handle.write("\n".join(str(problem) for problem in problems) + "\n")
        return 1
    checked = ", ".join(day.isoformat() for day in days)
    out(f"archive is fresh for {len(datasets)} dataset(s) on {checked}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
