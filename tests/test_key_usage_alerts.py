"""The key-usage-unbooked line, held to the metric and policy that watch it
(infra/key_usage_alerts.tf): the filter's textPayload substring is read from
the Terraform and matched against the lines the storage layer writes, rendered
by the formatter Cloud Run's textPayload carries, on an API service and behind
the settle drain worker's output boundary."""

from __future__ import annotations

import enum
import logging
import re
from pathlib import Path

from trusted_router import storage_gcp_authorize
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.services import settle_drain_observability, settle_outbox_worker

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "key_usage_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbook.md"
LOGGER = "trusted_router.storage_gcp_authorize"


def _payload_match() -> str:
    """The quoted textPayload substring the filter carries."""
    found = re.findall(r'"textPayload:\\"(.*?)\\""', TERRAFORM.read_text(encoding="utf-8"))
    assert len(found) == 1, found
    return found[0]


class _Lines(logging.Handler):
    """The storage logger's lines, as the console handler renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(_ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _rendered(warnings: list[dict[str, object]], *, worker: bool = False) -> list[str]:
    """The lines as an API service writes them, or as the worker does: the
    worker's lifespan puts its boundary on every handler already attached."""
    assert storage_gcp_authorize.log.name == LOGGER
    handler = _Lines()
    storage_gcp_authorize.log.addHandler(handler)
    remove = settle_drain_observability.install_logging() if worker else None
    try:
        if worker:
            assert any(isinstance(item, settle_drain_observability.WorkerLogFilter)
                       for item in handler.filters)
        storage_gcp_authorize._log_missing_key_releases({"missing_key_releases": warnings})
    finally:
        if remove is not None:
            remove()
        storage_gcp_authorize.log.removeHandler(handler)
    return handler.lines


_UNBOOKED = {"key_hash": "kh-alert", "hold_micro": 0, "shard": 3, "actual_micro": 8,
             "book_to_byok": False, "usage_unbooked": True}
_RECOVERED = {"key_hash": "kh-recovered", "hold_micro": 0, "missing_shard": 3, "actual_micro": 8}
_SKIPPED = {"key_hash": "kh-skipped", "hold_micro": 5}


def test_the_filter_matches_the_unbooked_line_and_no_other() -> None:
    match = _payload_match()
    lines = _rendered([dict(_RECOVERED), dict(_UNBOOKED), dict(_SKIPPED)])
    assert len(lines) == 3, lines
    hits = [line for line in lines if match in line]
    assert len(hits) == 1, (match, lines)
    assert hits[0].startswith(match + "value=1 key_hash=kh-alert shard=3 actual_micro=8 "), hits
    # A longer metric name, another level or another logger is not it.
    assert match not in f"WARNING {LOGGER} metric=key_usage_unbooked_total_x value=1"
    assert match not in f"ERROR {LOGGER} metric=key_usage_unbooked_total value=1"
    assert match not in "WARNING trusted_router.other metric=key_usage_unbooked_total value=1"
    assert match not in f"WARNING {LOGGER} metric=key_usage_shard_fallback_total value=1"


def test_the_filter_matches_the_line_the_settle_drain_worker_writes() -> None:
    match = _payload_match()
    lines = _rendered([dict(_RECOVERED), dict(_UNBOOKED), dict(_SKIPPED)], worker=True)
    tail = f"logger={LOGGER} level=WARNING exception_class=none"
    assert lines == [
        f"WARNING {LOGGER} metric=key_usage_shard_fallback_total {tail}",
        f"WARNING {LOGGER} metric=key_usage_unbooked_total {tail}",
        f"WARNING {LOGGER} {tail}",
    ], lines
    assert [match in line for line in lines] == [False, True, False]
    # The worker's boundary still keeps every value of the record out.
    text = "\n".join(lines)
    for value in ("kh-", "shard=", "actual_micro", "book_to_byok", "value=", "hold_micro"):
        assert value not in text, (value, text)
    # The boundary is the one the worker's lifespan installs.
    source = Path(settle_outbox_worker.__file__).read_text(encoding="utf-8")
    assert "cleanups.append(install_logging())" in source


def test_the_worker_keeps_only_a_listed_counter_name() -> None:
    scrub = settle_drain_observability.WorkerLogFilter().filter

    def line(metric: object) -> str:
        record = logging.LogRecord(LOGGER, logging.WARNING, "", 0, "private-message %s",
                                   ("private-arg",), None)
        record.metric = metric
        record.key_hash = "private-key"
        scrubbed = scrub(record)
        assert isinstance(scrubbed, logging.LogRecord) and scrubbed is not record
        assert not {"metric", "key_hash"} & vars(scrubbed).keys()
        return scrubbed.getMessage()

    plain = f"logger={LOGGER} level=WARNING exception_class=none"
    assert settle_drain_observability._COUNTED_METRICS == {
        "key_usage_unbooked_total", "key_usage_shard_fallback_total"}
    assert line("key_usage_unbooked_total") == f"metric=key_usage_unbooked_total {plain}"
    for other in ("private-metric", "key_usage_unbooked_total private-suffix",
                  "async_settle_accepted", None, 7, ("key_usage_unbooked_total",),
                  ["key_usage_unbooked_total"], {"key_usage_unbooked_total": 1},
                  b"key_usage_unbooked_total"):
        assert line(other) == plain, other

    # Only an exact str is read. A subclass may render, hash or compare as it
    # likes, so it takes the plain line, and the boundary never raises.
    class Rendered(str):
        def __str__(self) -> str:
            return "private-rendered"

    class AnyName(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash("key_usage_unbooked_total")

    class Unhashable(str):
        __hash__ = None  # type: ignore[assignment]

    class Raising(str):
        def __hash__(self) -> int:
            raise RuntimeError("private-hash")

        def __str__(self) -> str:
            raise RuntimeError("private-str")

    class Named(str, enum.Enum):  # noqa: UP042 - the mixin form renders its class name
        UNBOOKED = "key_usage_unbooked_total"

    for subclass in (Rendered("key_usage_unbooked_total"), AnyName("private-name"),
                     Unhashable("key_usage_unbooked_total"), Raising("key_usage_unbooked_total"),
                     Named.UNBOOKED):
        assert line(subclass) == plain, type(subclass).__name__


def test_the_metric_and_the_policy_read_one_filter_and_page_as_the_fast_path_does() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    assert tf.count("filter      = local.key_usage_unbooked_filter") == 1
    assert tf.count("filter = local.key_usage_unbooked_filter") == 1
    assert '"resource.type=\\"cloud_run_revision\\""' in tf
    assert re.search(r'notification_rate_limit \{\n\s+period = "300s"\n\s+\}\n\s+auto_close = "1800s"', tf)
    assert "notification_channels = local.fastpath_alert_channels" in tf
    assert 'metric_kind = "DELTA"' in tf and 'value_type  = "INT64"' in tf


def test_the_runbook_names_the_policy_the_filter_and_the_scoped_rearm() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    name = re.search(r'display_name = "(TR Settle: [^"]+)"', tf)
    assert name and f"**Alert `{name.group(1)}`**" in runbook
    assert "Key usage unbooked" in tf and '<a id="key-usage-unbooked"></a>Key usage unbooked' in runbook
    assert f'textPayload:"{_payload_match()}"' in runbook
    # The re-arm is scoped to one row and to the error the storage layer raises.
    raised = "key usage booking failed reservation_id="
    storage = Path(storage_gcp_authorize.__file__).read_text(encoding="utf-8")
    assert re.search(r'raise RuntimeError\(\n\s+f"' + re.escape(raised), storage)
    # The drain stores the error as "<class>: <text>", the prefix the re-arm reads.
    worker = Path(settle_outbox_worker.__file__).read_text(encoding="utf-8")
    assert 'error_note = f"{type(exc).__name__}: {exc}"' in worker
    section = runbook.split('<a id="key-usage-unbooked"></a>', 1)[1].split("Monitoring signals:", 1)[0]
    assert section.count("UPDATE tr_settle_outbox") == 1
    rearm = section.split("UPDATE tr_settle_outbox", 1)[1].split("```", 1)[0]
    assert "WHERE authorization_id='<authorization_id>' AND intent_kind='settle'" in rearm
    assert "AND status='dead'" in rearm
    assert f"STARTS_WITH(last_error, 'RuntimeError: {raised}')" in rearm
    assert "attempts" not in rearm and "`attempts`" in section
    # The counter read is by the key's hash, never a table-wide read.
    assert section.count("FROM tr_key_limit") == 1
    assert "SELECT shard FROM tr_key_limit WHERE key_hash='<key_hash>'\"" in section
    # Both forms of the line are named, the worker's without a key or an amount.
    assert f"`WARNING {LOGGER} metric=key_usage_unbooked_total logger={LOGGER} " in section
