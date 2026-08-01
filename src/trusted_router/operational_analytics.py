"""Read-only ClickHouse adapter for operational metadata."""

# ruff: noqa: S608
# Identifiers are fixed constants and every request value is server-bound.

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import struct
from typing import Any, TypeVar, cast

import httpx

from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)
from trusted_router.types import UsageType

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
T = TypeVar("T")


def _identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a ClickHouse identifier")
    return value


def _iso(value: Any) -> str:
    text = str(value)
    if text.endswith("Z") or "+" in text[10:]:
        return text
    return text.replace(" ", "T") + "Z"


class OperationalAnalyticsClient:
    """Fixed, parameterized reads over replicated ClickHouse tables."""

    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        password: str,
        database: str = "tr",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("operational analytics ClickHouse URL is required")
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._database = _identifier(database, label="database")
        self._transport = transport

    def _query(
        self,
        sql: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> list[dict[str, Any]]:
        query_params: dict[str, str] = {"database": self._database}
        for key, value in (params or {}).items():
            query_params[f"param_{key}"] = str(value)
        with httpx.Client(
            auth=(self._user, self._password),
            timeout=httpx.Timeout(20.0),
            transport=self._transport,
        ) as client:
            response = client.post(
                self._base_url,
                params=query_params,
                content=sql,
                headers={"content-type": "text/plain; charset=utf-8"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("ClickHouse response did not contain data rows")
        return [dict(row) for row in rows if isinstance(row, dict)]

    def benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        if provider is not None:
            clauses.append("provider = {provider:String}")
            params["provider"] = provider
        if model is not None:
            clauses.append("model = {model:String}")
            params["model"] = model
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM provider_benchmark_samples FINAL
WHERE """
            + where
            + """
ORDER BY created_at DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_benchmark_sample(row) for row in rows]

    def balanced_benchmark_samples(
        self,
        *,
        cutoff: str | None,
        per_provider_limit: int,
        limit: int,
    ) -> list[ProviderBenchmarkSample]:
        """Read one provider-balanced window without application-side fanout."""
        params: dict[str, str | int] = {
            "per_provider_limit": max(1, per_provider_limit),
            "limit": max(1, limit),
        }
        where = "1"
        if cutoff is not None:
            where = "created_at >= parseDateTime64BestEffort({cutoff:String}, 3)"
            params["cutoff"] = cutoff
        rows = self._query(
            """
SELECT
  id, model, provider, provider_name, status, usage_type, streamed,
  input_tokens, output_tokens, total_cost_microdollars,
  speed_tokens_per_second, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, finish_reason,
  error_type, error_status, error_message, region, source, app, created_at
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY provider ORDER BY created_at DESC, id DESC
  ) AS provider_rank
  FROM provider_benchmark_samples FINAL
  WHERE """
            + where
            + """
)
WHERE provider_rank <= {per_provider_limit:UInt32}
ORDER BY created_at DESC, id DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_benchmark_sample(row) for row in rows]

    def activity_generations(
        self,
        *,
        tenant_id: str,
        key_id: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        date: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 5001,
    ) -> list[Generation]:
        clauses = ["tenant_id = {tenant_id:String}"]
        params: dict[str, str | int] = {
            "tenant_id": tenant_id,
            "limit": max(1, limit),
        }
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        if key_id is not None:
            clauses.append("key_id = {key_id:String}")
            params["key_id"] = key_id
        if tag_key is not None:
            clauses.append("mapContains(tags, {tag_key:String})")
            params["tag_key"] = tag_key
            if tag_value is not None:
                clauses.append("tags[{tag_key:String}] = {tag_value:String}")
                params["tag_value"] = tag_value
        if start_at is not None:
            clauses.append("created_at >= parseDateTime64BestEffort({start_at:String}, 3)")
            params["start_at"] = start_at
        if end_at is not None:
            clauses.append("created_at < parseDateTime64BestEffort({end_at:String}, 3)")
            params["end_at"] = end_at
        rows = self._query(
            """
SELECT
  generation_id, request_id, key_id, model, provider, provider_name, app,
  tokens_prompt, tokens_completion, cached_input_tokens, reasoning_tokens,
  total_cost_microdollars, usage_type, speed_tokens_per_second,
  finish_reason, status, streamed, usage_estimated, elapsed_milliseconds,
  first_token_milliseconds, ttfb_milliseconds, region, user, session_id,
  http_referer, app_categories, tags, created_at
FROM activity_generations FINAL
WHERE """
            + " AND ".join(clauses)
            + """
ORDER BY created_at DESC
LIMIT {limit:UInt32}
FORMAT JSON
""",
            params=params,
        )
        return [_generation(row, tenant_id=tenant_id) for row in rows]

    def synthetic_samples(
        self,
        *,
        date: str | None = None,
        target: str | None = None,
        probe_type: str | None = None,
        monitor_region: str | None = None,
        limit: int = 1000,
    ) -> list[SyntheticProbeSample]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        for column, value in (
            ("target", target),
            ("probe_type", probe_type),
            ("monitor_region", monitor_region),
        ):
            if value is not None:
                clauses.append(f"{column} = {{{column}:String}}")
                params[column] = value
        if date is not None:
            clauses.append("toDate(created_at) = toDate({date:String})")
            params["date"] = date
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            "SELECT * EXCEPT ingest_version FROM synthetic_probe_samples FINAL "
            f"WHERE {where} ORDER BY created_at DESC LIMIT {{limit:UInt32}} FORMAT JSON",
            params=params,
        )
        return [_dataclass_from_row(SyntheticProbeSample, row) for row in rows]

    def synthetic_rollups(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_histograms: bool = True,
        limit: int = 1000,
    ) -> list[SyntheticRollup]:
        clauses: list[str] = []
        params: dict[str, str | int] = {"limit": max(1, limit)}
        if period is not None:
            clauses.append("period = {period:String}")
            params["period"] = period
        if since is not None:
            clauses.append("period_start >= parseDateTimeBestEffort({since:String})")
            params["since"] = since
        if until is not None:
            clauses.append("period_start <= parseDateTimeBestEffort({until:String})")
            params["until"] = until
        where = " AND ".join(clauses) if clauses else "1"
        rows = self._query(
            "SELECT * EXCEPT ingest_version FROM synthetic_status_rollups FINAL "
            f"WHERE {where} ORDER BY period_start DESC LIMIT {{limit:UInt32}} FORMAT JSON",
            params=params,
        )
        rollups = [_dataclass_from_row(SyntheticRollup, row) for row in rows]
        if not include_histograms:
            rollups = [
                dataclasses.replace(
                    rollup,
                    latency_histogram={},
                    ttfb_histogram={},
                    dns_histogram={},
                    tcp_connect_histogram={},
                    tls_handshake_histogram={},
                    gateway_processing_histogram={},
                )
                for rollup in rollups
            ]
        return rollups

    def public_snapshot(self, name: str) -> dict[str, Any] | None:
        if name not in {"leaderboard", "apps"}:
            raise ValueError("unsupported public analytics snapshot")
        rows = self._query(
            """
SELECT payload
FROM public_analytics_snapshots FINAL
WHERE name = {name:String}
ORDER BY generated_at DESC
LIMIT 1
FORMAT JSON
""",
            params={"name": name},
        )
        if not rows:
            return None
        payload = json.loads(str(rows[0].get("payload") or "{}"))
        return payload if isinstance(payload, dict) else None


def _dataclass_from_row(cls: type[T], row: dict[str, Any]) -> T:
    allowed = {field.name for field in dataclasses.fields(cls)}  # type: ignore[arg-type]
    payload = {key: value for key, value in row.items() if key in allowed}
    if cls is SyntheticProbeSample:
        for key in ("connection_reused", "output_match"):
            if payload.get(key) is not None:
                payload[key] = bool(payload[key])
    if cls is SyntheticRollup and payload.get("target_region") == "":
        payload["target_region"] = None
    for key in ("created_at", "period_start", "updated_at", "last_checked_at"):
        if payload.get(key) is not None:
            payload[key] = _iso(payload[key])
    return cls(**payload)


def _benchmark_sample(row: dict[str, Any]) -> ProviderBenchmarkSample:
    row = dict(row)
    row["created_at"] = _iso(row["created_at"])
    row["streamed"] = bool(row["streamed"])
    return _dataclass_from_row(ProviderBenchmarkSample, row)


def _generation(row: dict[str, Any], *, tenant_id: str) -> Generation:
    return Generation(
        id=str(row["generation_id"]),
        request_id=str(row["request_id"]),
        workspace_id=tenant_id,
        key_hash=str(row["key_id"]),
        model=str(row["model"]),
        provider_name=str(row["provider_name"]),
        app=str(row["app"]),
        tokens_prompt=int(row["tokens_prompt"]),
        tokens_completion=int(row["tokens_completion"]),
        total_cost_microdollars=int(row["total_cost_microdollars"]),
        usage_type=UsageType.coerce(row["usage_type"]),
        speed_tokens_per_second=float(row["speed_tokens_per_second"]),
        finish_reason=str(row["finish_reason"]),
        status=str(row["status"]),
        streamed=bool(row["streamed"]),
        usage_estimated=bool(row["usage_estimated"]),
        cached_input_tokens=int(row["cached_input_tokens"]),
        reasoning_tokens=int(row["reasoning_tokens"]),
        provider=str(row["provider"] or "") or None,
        elapsed_milliseconds=_optional_int(row.get("elapsed_milliseconds")),
        first_token_milliseconds=_optional_int(row.get("first_token_milliseconds")),
        ttfb_milliseconds=_optional_int(row.get("ttfb_milliseconds")),
        region=str(row["region"]) if row.get("region") is not None else None,
        user=str(row["user"]) if row.get("user") is not None else None,
        session_id=(
            str(row["session_id"]) if row.get("session_id") is not None else None
        ),
        http_referer=(
            str(row["http_referer"])
            if row.get("http_referer") is not None
            else None
        ),
        app_categories=[str(item) for item in row.get("app_categories") or []],
        tags={str(key): str(value) for key, value in (row.get("tags") or {}).items()},
        created_at=_iso(row["created_at"]),
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def stable_rows_fingerprint(rows: list[Any], *, grace_seconds: int = 30) -> tuple[int, str]:
    """Fingerprint only rows old enough to have drained from the outbox."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=max(0, grace_seconds))
    stable: list[dict[str, Any]] = []
    for row in rows:
        payload = (
            dataclasses.asdict(cast(Any, row))
            if dataclasses.is_dataclass(row)
            else dict(row)
        )
        # Rebuild timestamps are expected to differ across stores. Raw tenant
        # and key identifiers are intentionally replaced by opaque surrogates
        # in ClickHouse, so they are not parity fields either.
        for volatile in ("updated_at", "workspace_id", "key_hash"):
            payload.pop(volatile, None)
        speed = payload.get("speed_tokens_per_second")
        if speed is not None and "input_tokens" in payload:
            # The long-lived provider benchmark table intentionally stores
            # this one metric as Float32. Canonicalize the Bigtable value to
            # the same representation before comparing the two stores.
            payload["speed_tokens_per_second"] = struct.unpack(
                "!f",
                struct.pack("!f", float(speed)),
            )[0]
        created_at = payload.get("created_at") or payload.get("period_start")
        if created_at:
            try:
                parsed = dt.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            except ValueError:
                parsed = cutoff
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
            if parsed > cutoff:
                continue
        stable.append(payload)
    canonical_rows = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        for row in stable
    )
    encoded = "\n".join(canonical_rows)
    return len(stable), hashlib.sha256(encoded.encode("utf-8")).hexdigest()
