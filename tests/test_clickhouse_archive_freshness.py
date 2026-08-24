from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from clickhouse.archive_daily import ARCHIVE_SCHEMA_VERSION, DATASETS
from clickhouse.check_archive_freshness import (
    CompletedCommand,
    GcloudStore,
    StoreError,
    check_dataset,
    check_days,
    days_to_check,
    main,
    pointer_key,
)

DAY = dt.date(2026, 8, 15)
UTC = dt.UTC


def _pointer(day: dt.date = DAY, **overrides: Any) -> dict[str, Any]:
    pointer: dict[str, Any] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "day": day.isoformat(),
        "revision": "v1-19060-11af19dec320bb40-5e74e8045922709a",
        "manifest": (
            f"raw/activity_generations/day={day.isoformat()}/revisions/"
            "v1-19060-11af19dec320bb40-5e74e8045922709a/manifest.json"
        ),
        "source_fingerprint": {"rows": 19060, "hash_sum": 1, "hash_xor": 2},
        "updated_at": "2026-08-16T14:40:48Z",
    }
    pointer.update(overrides)
    return pointer


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any] | None] = {}
        self.present: set[str] = set()
        self.reads: list[str] = []
        self.fail_reads_with: str | None = None
        # Fail only the keys containing one of these fragments, so a test can
        # make ONE dataset unreadable while the others stay legible.
        self.fail_only_keys_containing: tuple[str, ...] = ()

    def put(self, key: str, pointer: dict[str, Any], *, with_manifest: bool = True) -> None:
        self.objects[key] = pointer
        if with_manifest and isinstance(pointer.get("manifest"), str):
            self.present.add(pointer["manifest"])

    def read_json(self, key: str) -> dict[str, Any] | None:
        self.reads.append(key)
        if self.fail_reads_with is not None and (
            not self.fail_only_keys_containing
            or any(fragment in key for fragment in self.fail_only_keys_containing)
        ):
            raise StoreError(self.fail_reads_with)
        return self.objects.get(key)

    def exists(self, key: str) -> bool:
        return key in self.present


def _fresh_store(day: dt.date = DAY) -> FakeStore:
    store = FakeStore()
    for dataset in DATASETS:
        pointer = _pointer(day)
        pointer["manifest"] = pointer["manifest"].replace("activity_generations", dataset)
        store.put(pointer_key(dataset, day), pointer)
    return store


def test_fresh_day_has_no_problems() -> None:
    statuses = check_days(_fresh_store(), [DAY])
    assert len(statuses) == len(DATASETS)
    assert all(status.fresh for status in statuses)
    assert {status.revision for status in statuses} == {
        "v1-19060-11af19dec320bb40-5e74e8045922709a"
    }


def test_missing_pointer_is_a_problem_naming_the_key() -> None:
    store = FakeStore()
    status = check_dataset(store, "activity_generations", DAY)
    assert not status.fresh
    assert len(status.problems) == 1
    assert "never archived" in status.problems[0].message
    assert pointer_key("activity_generations", DAY) in status.problems[0].message


def test_pointer_whose_manifest_is_absent_is_a_problem() -> None:
    store = FakeStore()
    store.put(pointer_key("activity_generations", DAY), _pointer(), with_manifest=False)
    status = check_dataset(store, "activity_generations", DAY)
    assert [p.message for p in status.problems] == [
        f"pointer names manifest {_pointer()['manifest']}, which does not exist"
    ]


def test_pointer_for_the_wrong_day_is_a_problem() -> None:
    store = FakeStore()
    store.put(pointer_key("activity_generations", DAY), _pointer(day=DAY - dt.timedelta(days=1)))
    status = check_dataset(store, "activity_generations", DAY)
    assert any("expected 2026-08-15" in p.message for p in status.problems)


def test_pointer_written_before_the_day_closed_is_a_problem() -> None:
    store = FakeStore()
    store.put(
        pointer_key("activity_generations", DAY),
        _pointer(updated_at="2026-08-15T23:59:59Z"),
    )
    status = check_dataset(store, "activity_generations", DAY)
    assert any("before the day closed" in p.message for p in status.problems)


def test_real_pointer_shape_with_microseconds_is_fresh() -> None:
    # Copied from the live bucket on 2026-08-16: fromisoformat must accept the
    # microsecond precision the archiver actually writes.
    store = FakeStore()
    store.put(
        pointer_key("activity_generations", DAY),
        _pointer(updated_at="2026-08-16T14:40:47.567030Z"),
    )
    status = check_dataset(store, "activity_generations", DAY)
    assert status.fresh, [str(p) for p in status.problems]


def test_pointer_written_exactly_at_day_close_is_fresh() -> None:
    store = FakeStore()
    store.put(
        pointer_key("activity_generations", DAY),
        _pointer(updated_at="2026-08-16T00:00:00Z"),
    )
    assert check_dataset(store, "activity_generations", DAY).fresh


@pytest.mark.parametrize("bad", ["", "not-a-time", "2026-08-16T14:40:48", 42, None])
def test_unusable_updated_at_is_a_problem(bad: object) -> None:
    store = FakeStore()
    store.put(pointer_key("activity_generations", DAY), _pointer(updated_at=bad))
    status = check_dataset(store, "activity_generations", DAY)
    assert any("timezone-aware" in p.message for p in status.problems)


def test_schema_version_and_revision_are_checked() -> None:
    store = FakeStore()
    store.put(
        pointer_key("activity_generations", DAY),
        _pointer(schema_version=ARCHIVE_SCHEMA_VERSION + 1, revision=""),
    )
    status = check_dataset(store, "activity_generations", DAY)
    messages = [p.message for p in status.problems]
    assert any("schema_version" in m for m in messages)
    assert "pointer has no revision" in messages
    assert status.revision is None


def test_read_error_is_not_reported_as_missing() -> None:
    store = FakeStore()
    store.fail_reads_with = "403 Forbidden: storage.objects.get denied"
    with pytest.raises(StoreError, match="403"):
        check_dataset(store, "activity_generations", DAY)


def test_days_to_check_never_includes_today() -> None:
    now = dt.datetime(2026, 8, 16, 7, 30, tzinfo=UTC)
    assert days_to_check(now=now, lookback=1) == [dt.date(2026, 8, 15)]
    assert days_to_check(now=now, lookback=3) == [
        dt.date(2026, 8, 15),
        dt.date(2026, 8, 14),
        dt.date(2026, 8, 13),
    ]
    with pytest.raises(ValueError, match="at least 1"):
        days_to_check(now=now, lookback=0)


def test_days_to_check_uses_utc_not_local_offset() -> None:
    # 01:30 on the 17th in UTC+3 is still the 16th in UTC: yesterday is the 15th.
    now = dt.datetime(2026, 8, 17, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert days_to_check(now=now, lookback=1) == [dt.date(2026, 8, 15)]


def test_main_exit_codes_and_problem_file(tmp_path: Any) -> None:
    now = dt.datetime(2026, 8, 16, 7, 30, tzinfo=UTC)
    lines: list[str] = []

    assert main([], store=_fresh_store(), now=now, out=lines.append) == 0
    assert any(line.startswith("archive is fresh for 4 dataset(s) on 2026-08-15") for line in lines)

    problems_file = tmp_path / "problems.txt"
    stale = _fresh_store()
    del stale.objects[pointer_key("synthetic_probe_samples", DAY)]
    lines.clear()
    code = main(
        ["--problems-file", str(problems_file)],
        store=stale,
        now=now,
        out=lines.append,
    )
    assert code == 1
    written = problems_file.read_text()
    assert "synthetic_probe_samples day=2026-08-15" in written
    assert "activity_generations" not in written
    assert any(line.startswith("::error::1 archive freshness problem") for line in lines)

    broken = _fresh_store()
    broken.fail_reads_with = "403 Forbidden"
    lines.clear()
    code = main(["--problems-file", str(problems_file)], store=broken, now=now, out=lines.append)
    assert code == 2
    written = problems_file.read_text()
    assert "could not read the archive: 403 Forbidden" in written
    assert any("could not be read, so freshness is unknown" in line for line in lines)
    # Every dataset is reported even though the very first read failed. The
    # sweep used to abort on it, so 3 of 4 datasets went unreported.
    for dataset in DATASETS:
        assert dataset in written


def test_main_day_and_dataset_overrides() -> None:
    now = dt.datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
    store = _fresh_store(DAY)  # only 2026-08-15 is archived
    lines: list[str] = []
    assert main(["--day", "2026-08-15"], store=store, now=now, out=lines.append) == 0
    lines.clear()
    assert (
        main(
            ["--day", "2026-08-15", "--dataset", "activity_generations"],
            store=store,
            now=now,
            out=lines.append,
        )
        == 0
    )
    assert any("fresh for 1 dataset(s)" in line for line in lines)
    # Yesterday relative to `now` (08-19) is not archived, so the default is stale.
    assert main([], store=store, now=now, out=lines.append) == 1


class FakeRunner:
    def __init__(self, responses: dict[str, CompletedCommand]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CompletedCommand:
        self.calls.append(argv)
        return self.responses[argv[2] + " " + argv[3]]


def test_gcloud_store_distinguishes_absent_from_forbidden() -> None:
    bucket = "example-archive"
    key = pointer_key("activity_generations", DAY)
    url = f"gs://{bucket}/{key}"
    runner = FakeRunner(
        {
            f"cat {url}": CompletedCommand(0, json.dumps(_pointer()), ""),
            f"cat gs://{bucket}/absent.json": CompletedCommand(
                1,
                "",
                "ERROR: (gcloud.storage.cat) The following URLs matched no objects"
                " or files: gs://example-archive/absent.json",
            ),
            f"cat gs://{bucket}/denied.json": CompletedCommand(
                1,
                "",
                "ERROR: (gcloud.storage.cat) HTTPError 403: tr-x@... does not have"
                " storage.objects.get access",
            ),
            f"cat gs://{bucket}/garbage.json": CompletedCommand(0, "not json", ""),
            f"ls gs://{bucket}/present": CompletedCommand(0, f"gs://{bucket}/present\n", ""),
            f"ls gs://{bucket}/gone": CompletedCommand(
                1, "", "ERROR: (gcloud.storage.ls) One or more URLs matched no objects."
            ),
            f"ls gs://{bucket}/denied": CompletedCommand(1, "", "HTTPError 403: forbidden"),
        }
    )
    store = GcloudStore(bucket=bucket, runner=runner)

    assert store.read_json(key) == _pointer()
    assert store.read_json("absent.json") is None
    with pytest.raises(StoreError, match="403"):
        store.read_json("denied.json")
    with pytest.raises(StoreError, match="not valid JSON"):
        store.read_json("garbage.json")
    assert store.exists("present") is True
    assert store.exists("gone") is False
    with pytest.raises(StoreError, match="403"):
        store.exists("denied")
    assert runner.calls[0][:3] == ["gcloud", "storage", "cat"]


def test_one_unreadable_dataset_does_not_hide_another_that_is_stale(tmp_path: Any) -> None:
    """The masking case, and the reason the sweep no longer aborts.

    Before this, check_days let a StoreError propagate, so the first denied read
    ended the run. A dataset that had genuinely stopped archiving could sit
    behind an unrelated permissions error on a different dataset and never be
    reported -- the 2026-08-16 shape, where the mechanism that should have
    raised the alarm was itself the broken thing.
    """

    now = dt.datetime(2026, 8, 16, 7, 30, tzinfo=UTC)
    store = _fresh_store()
    # provider_benchmark_samples is unreadable ...
    store.fail_reads_with = "403 Forbidden"
    store.fail_only_keys_containing = ("provider_benchmark_samples",)
    # ... and a DIFFERENT dataset was genuinely never archived.
    del store.objects[pointer_key("activity_generations", DAY)]

    problems_file = tmp_path / "problems.txt"
    lines: list[str] = []
    code = main(
        ["--problems-file", str(problems_file)],
        store=store,
        now=now,
        out=lines.append,
    )

    written = problems_file.read_text()
    # The genuine staleness is reported even though another dataset errored.
    assert "activity_generations day=2026-08-15" in written
    assert "the day was never archived" in written
    # And the unreadable one is reported as unreadable, not as fresh.
    assert "could not read the archive: 403 Forbidden" in written
    # Unknown outranks stale: exit 2 says this check could not see everything.
    assert code == 2
    # The datasets that were readable and fine are not dragged in as problems.
    assert "synthetic_status_rollups" not in written
