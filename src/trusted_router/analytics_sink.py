"""Analytics fan-out: a second, non-authoritative home for benchmark samples.

Phase 2 of the storage-portability plan (docs/storage-portability/README.md).
Bigtable stays authoritative; this mirrors the same rows into ClickHouse so the
two can be compared continuously before anything reads from ClickHouse.

Three properties matter more than throughput here:

1. **It must never affect a request.** `record_provider_benchmark` is called on
   the gateway settle path, which a customer is paying for and waiting on. So
   the sink hands the row to a bounded in-memory queue and returns; a worker
   thread does the network I/O. A slow or dead ClickHouse costs analytics
   rows, never latency.
2. **It must never grow without bound.** If ClickHouse is unreachable the queue
   fills and further rows are DROPPED and counted, rather than accumulating
   until the process dies. Losing analytics is acceptable; OOM-ing the control
   plane is not.
3. **Inserts must be batched.** ClickHouse is explicitly hostile to
   one-row-per-INSERT: each insert creates a part, and a part per sample
   generates merge pressure that degrades the whole table. The worker drains
   whatever has accumulated into a single insert, and `async_insert` lets the
   server coalesce further across replicas.

This is deliberately NOT part of the `Store` protocol. Analytics is a
side-channel with different durability requirements; putting it in the
protocol would force every backend to implement it and would imply the write
is authoritative, which it is not.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Bounded so an unreachable ClickHouse cannot consume memory without limit.
#: Sized for roughly a minute of peak synthetic + organic sample volume; past
#: that, dropping is the correct behaviour.
DEFAULT_QUEUE_SIZE = 10_000

#: Rows per INSERT. ClickHouse prefers large batches; this bounds the request
#: body while still being far from the one-row-per-part anti-pattern.
DEFAULT_BATCH_SIZE = 500

#: How long the worker waits for more rows before flushing a partial batch, so
#: a quiet period does not leave rows sitting in memory indefinitely.
DEFAULT_FLUSH_SECONDS = 2.0


@runtime_checkable
class AnalyticsSink(Protocol):
    """Best-effort mirror of analytics rows. Implementations MUST NOT raise."""

    def record_benchmark_sample(self, sample: Any) -> None: ...

    def stats(self) -> dict[str, int]: ...

    def close(self) -> None: ...


class NullAnalyticsSink:
    """The default. Does nothing, cheaply.

    Chosen over `None` so call sites never branch — an `if sink is not None`
    on the settle path is one more thing to get wrong.
    """

    def record_benchmark_sample(self, sample: Any) -> None:
        return None

    def stats(self) -> dict[str, int]:
        return {"enqueued": 0, "written": 0, "dropped": 0, "failed": 0}

    def close(self) -> None:
        return None


class ClickHouseAnalyticsSink:
    """Queue rows to ClickHouse from a background worker.

    The worker is a daemon thread: analytics must not keep the process alive
    at shutdown. `close()` exists for tests and for a graceful drain.
    """

    def __init__(
        self,
        *,
        url: str,
        user: str,
        password: str,
        table: str = "provider_benchmark_samples",
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        transport: Any | None = None,
    ) -> None:
        self._url = url
        self._auth = (user, password)
        self._table = table
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._counts = {"enqueued": 0, "written": 0, "dropped": 0, "failed": 0}
        self._counts_lock = threading.Lock()
        # Injectable so tests exercise batching and failure handling without a
        # live server.
        self._transport = transport or self._post_rows
        self._worker = threading.Thread(
            target=self._run, name="clickhouse-analytics", daemon=True
        )
        self._worker.start()

    # -- producer side (request path; must be fast and total) --------------

    def record_benchmark_sample(self, sample: Any) -> None:
        try:
            row = _row_from_sample(sample)
        except Exception:  # noqa: BLE001 - a malformed row must not break settle
            self._bump("failed")
            logger.warning("analytics.row_encode_failed", exc_info=True)
            return
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            # Deliberate: drop and count. Blocking here would put ClickHouse
            # availability on the critical path of a paid request.
            self._bump("dropped")
            return
        self._bump("enqueued")

    def stats(self) -> dict[str, int]:
        with self._counts_lock:
            return dict(self._counts)

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=5.0)

    # -- consumer side (background thread) ---------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._drain()
            if batch:
                self._write(batch)
        # Final drain so an orderly close does not discard queued rows.
        remaining = self._drain(block=False)
        if remaining:
            self._write(remaining)

    def _drain(self, *, block: bool = True) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        try:
            first = self._queue.get(timeout=self._flush_seconds) if block else self._queue.get_nowait()
            batch.append(first)
        except queue.Empty:
            return batch
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, batch: list[dict[str, Any]]) -> None:
        payload = "\n".join(json.dumps(row) for row in batch).encode()
        try:
            self._transport(payload)
        except Exception:  # noqa: BLE001 - the sink is best-effort by contract
            self._bump("failed", len(batch))
            logger.warning(
                "analytics.write_failed", extra={"rows": len(batch)}, exc_info=True
            )
            return
        self._bump("written", len(batch))

    def _post_rows(self, payload: bytes) -> None:
        import httpx

        # async_insert lets the server coalesce our batches further, which
        # matters once several replicas are writing concurrently.
        # wait_for_async_insert=0 means we do not block on the server's flush.
        response = httpx.post(
            self._url,
            params={
                "query": f"INSERT INTO {self._table} FORMAT JSONEachRow",
                "async_insert": "1",
                "wait_for_async_insert": "0",
            },
            content=payload,
            auth=self._auth,
            timeout=10.0,
        )
        response.raise_for_status()

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._counts_lock:
            self._counts[key] += amount


def _row_from_sample(sample: Any) -> dict[str, Any]:
    """Flatten a ProviderBenchmarkSample into the ClickHouse column shape.

    `created_at` is normalised to ClickHouse's DateTime64 text format here so
    the worker thread does no parsing, and `usage_type` is stringified because
    it may arrive as an enum.
    """
    # `is_dataclass` also narrows to the dataclass *type*, which asdict()
    # rejects; guard on the instance so mypy and runtime agree.
    raw = (
        asdict(sample)
        if is_dataclass(sample) and not isinstance(sample, type)
        else dict(sample)
    )
    created = str(raw.get("created_at") or "")
    row = {
        "id": str(raw.get("id") or ""),
        "created_at": _clickhouse_timestamp(created),
        "provider": str(raw.get("provider") or ""),
        "model": str(raw.get("model") or ""),
        "provider_name": str(raw.get("provider_name") or ""),
        "status": str(raw.get("status") or ""),
        "usage_type": str(raw.get("usage_type") or ""),
        "source": str(raw.get("source") or ""),
        "workspace_id": str(raw.get("workspace_id") or ""),
        "streamed": 1 if raw.get("streamed") else 0,
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "total_cost_microdollars": int(raw.get("total_cost_microdollars") or 0),
        "speed_tokens_per_second": raw.get("speed_tokens_per_second"),
        "elapsed_milliseconds": raw.get("elapsed_milliseconds"),
        "first_token_milliseconds": raw.get("first_token_milliseconds"),
        "ttfb_milliseconds": raw.get("ttfb_milliseconds"),
        "finish_reason": raw.get("finish_reason"),
        "error_type": raw.get("error_type"),
        "error_status": _optional_int(raw.get("error_status")),
        "error_message": raw.get("error_message"),
        "region": raw.get("region"),
        "app": str(raw.get("app") or ""),
    }
    return row


def _clickhouse_timestamp(value: str) -> str:
    """ISO-8601 (with optional Z/offset) -> 'YYYY-MM-DD HH:MM:SS.mmm' UTC."""
    import datetime as dt

    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _optional_int(value: Any) -> int | None:
    """Coerce an HTTP status that may arrive as an int, a digit string, or None.

    The differential proof found that a string `"429"` compares differently in
    Python (`not in {429}`) than after coercion to ClickHouse's UInt16, so the
    two paths must normalise identically.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def create_analytics_sink(settings: Any) -> AnalyticsSink:
    """Build the configured sink. Defaults to the no-op."""
    url = getattr(settings, "clickhouse_url", "") or ""
    if not url:
        return NullAnalyticsSink()
    return ClickHouseAnalyticsSink(
        url=url,
        user=getattr(settings, "clickhouse_user", "") or "default",
        password=getattr(settings, "clickhouse_password", "") or "",
        table=getattr(settings, "clickhouse_benchmark_table", "")
        or "provider_benchmark_samples",
    )
