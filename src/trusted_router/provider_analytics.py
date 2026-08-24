"""Read-only ClickHouse queries for provider operational analytics."""

# ruff: noqa: S608
# Table/database identifiers are allowlist-validated and every request value is
# bound through ClickHouse HTTP query parameters.

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PROVIDER_EXPORT_DAYS = 60
MIN_TRAFFIC_SHARE_SAMPLES = 20


def _identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a ClickHouse identifier")
    return value


@dataclass
class ClickHouseExport:
    response: httpx.Response
    client: httpx.AsyncClient

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.response.aclose()
            await self.client.aclose()


class ProviderAnalyticsClient:
    """Fixed, parameterized analytics queries over privacy-safe request rows."""

    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        password: str,
        database: str = "tr",
        table: str = "provider_benchmark_samples",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("provider analytics ClickHouse URL is required")
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._database = _identifier(database, label="database")
        self._table = _identifier(table, label="table")
        self._transport = transport

    @property
    def qualified_table(self) -> str:
        return f"{self._database}.{self._table}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=(self._user, self._password),
            timeout=httpx.Timeout(20.0),
            transport=self._transport,
        )

    async def _json_query(
        self,
        query: str,
        *,
        provider: str,
        days: int,
    ) -> list[dict[str, Any]]:
        days = _validated_days(days)
        async with self._client() as client:
            response = await client.post(
                self._base_url,
                params={
                    "database": self._database,
                    "param_provider": provider,
                    "param_days": str(days),
                },
                content=query,
                headers={"content-type": "text/plain; charset=utf-8"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("ClickHouse response did not contain data rows")
        return [dict(row) for row in rows if isinstance(row, dict)]

    async def summary(self, provider: str, *, days: int = 7) -> dict[str, Any]:
        days = _validated_days(days)
        table = self.qualified_table
        where = (
            "provider = {provider:String} "
            "AND created_at >= now() - toIntervalDay({days:UInt8})"
        )
        totals_query = f"""  # noqa: S608 - identifiers are validated; values are bound.
SELECT
  count() AS attempts,
  countIf(status = 'success') AS completed,
  countIf(status != 'success') AS failed,
  countIf(source = 'organic') AS organic_requests,
  countIf(source LIKE 'synthetic%') AS synthetic_requests,
  sum(input_tokens) AS input_tokens,
  sum(output_tokens) AS output_tokens,
  sum(total_cost_microdollars) AS total_cost_microdollars,
  quantileTDigestIf(0.50)(first_token_milliseconds, first_token_milliseconds IS NOT NULL) AS p50_ttft_ms,
  quantileTDigestIf(0.95)(first_token_milliseconds, first_token_milliseconds IS NOT NULL) AS p95_ttft_ms,
  quantileTDigestIf(0.50)(elapsed_milliseconds, elapsed_milliseconds IS NOT NULL) AS p50_elapsed_ms,
  quantileTDigestIf(0.95)(elapsed_milliseconds, elapsed_milliseconds IS NOT NULL) AS p95_elapsed_ms
FROM {table} FINAL
WHERE {where}
FORMAT JSON
"""
        models_query = f"""  # noqa: S608 - identifiers are validated; values are bound.
SELECT
  model,
  countIf(provider = {{provider:String}}) AS attempts,
  countIf(provider = {{provider:String}} AND status = 'success') AS completed,
  countIf(provider = {{provider:String}} AND status != 'success') AS failed,
  countIf(provider = {{provider:String}} AND source = 'organic') AS provider_organic_requests,
  countIf(source = 'organic') AS model_organic_requests,
  countIf(
    provider = {{provider:String}} AND source = 'organic' AND status = 'success'
  ) AS provider_completed_organic_requests,
  countIf(source = 'organic' AND status = 'success') AS model_completed_organic_requests,
  uniqExactIf(provider, source = 'organic') AS active_organic_providers,
  quantileTDigestIf(0.50)(
    first_token_milliseconds,
    provider = {{provider:String}} AND first_token_milliseconds IS NOT NULL
  ) AS p50_ttft_ms,
  quantileTDigestIf(0.95)(
    first_token_milliseconds,
    provider = {{provider:String}} AND first_token_milliseconds IS NOT NULL
  ) AS p95_ttft_ms,
  quantileTDigestIf(0.50)(
    speed_tokens_per_second,
    provider = {{provider:String}} AND speed_tokens_per_second IS NOT NULL
  ) AS p50_tokens_per_second
FROM {table} FINAL
WHERE created_at >= now() - toIntervalDay({{days:UInt8}})
GROUP BY model
HAVING attempts > 0
ORDER BY attempts DESC, model
LIMIT 250
FORMAT JSON
"""
        errors_query = f"""  # noqa: S608 - identifiers are validated; values are bound.
SELECT
  ifNull(error_type, 'unknown') AS error_type,
  ifNull(toString(error_status), '') AS error_status,
  count() AS occurrences
FROM {table} FINAL
WHERE {where} AND status != 'success'
GROUP BY error_type, error_status
ORDER BY occurrences DESC, error_type
LIMIT 25
FORMAT JSON
"""
        daily_query = f"""  # noqa: S608 - identifiers are validated; values are bound.
SELECT
  toString(toDate(created_at)) AS day,
  count() AS attempts,
  countIf(status = 'success') AS completed,
  countIf(status != 'success') AS failed
FROM {table} FINAL
WHERE {where}
GROUP BY day
ORDER BY day
FORMAT JSON
"""
        totals, models, errors, daily = await asyncio.gather(
            self._json_query(totals_query, provider=provider, days=days),
            self._json_query(models_query, provider=provider, days=days),
            self._json_query(errors_query, provider=provider, days=days),
            self._json_query(daily_query, provider=provider, days=days),
        )
        total = totals[0] if totals else {}
        attempts = int(total.get("attempts") or 0)
        completed = int(total.get("completed") or 0)
        total["completion_rate"] = completed / attempts if attempts else None
        for row in models:
            model_organic = int(row.get("model_organic_requests") or 0)
            provider_organic = int(row.get("provider_organic_requests") or 0)
            model_completed = int(row.get("model_completed_organic_requests") or 0)
            provider_completed = int(
                row.get("provider_completed_organic_requests") or 0
            )
            enough_samples = model_organic >= MIN_TRAFFIC_SHARE_SAMPLES
            row["offered_traffic_share"] = (
                provider_organic / model_organic
                if enough_samples and model_organic
                else None
            )
            row["completed_traffic_share"] = (
                provider_completed / model_completed
                if enough_samples and model_completed
                else None
            )
        return {
            "provider": provider,
            "days": days,
            "minimum_traffic_share_samples": MIN_TRAFFIC_SHARE_SAMPLES,
            "totals": total,
            "models": models,
            "errors": errors,
            "daily": daily,
        }

    async def open_csv_export(
        self,
        provider: str,
        *,
        days: int = MAX_PROVIDER_EXPORT_DAYS,
    ) -> ClickHouseExport:
        days = _validated_days(days)
        table = self.qualified_table
        # Intentionally excludes app, error_message, workspace, key, prompt,
        # and output fields. This fixed projection is the provider data
        # contract; callers cannot submit SQL or choose extra columns.
        query = f"""  # noqa: S608 - identifiers are validated; values are bound.
SELECT
  id AS request_metadata_id,
  created_at,
  provider,
  model,
  status,
  usage_type,
  source,
  streamed,
  input_tokens,
  output_tokens,
  total_cost_microdollars,
  speed_tokens_per_second,
  elapsed_milliseconds,
  first_token_milliseconds,
  ttfb_milliseconds,
  finish_reason,
  error_type,
  error_status,
  region
FROM {table} FINAL
WHERE provider = {{provider:String}}
  AND created_at >= now() - toIntervalDay({{days:UInt8}})
ORDER BY created_at DESC, id DESC
FORMAT CSVWithNames
"""
        client = self._client()
        request = client.build_request(
            "POST",
            self._base_url,
            params={
                "database": self._database,
                "param_provider": provider,
                "param_days": str(days),
            },
            content=query,
            headers={"content-type": "text/plain; charset=utf-8"},
        )
        try:
            response = await client.send(request, stream=True)
            response.raise_for_status()
        except Exception:
            await client.aclose()
            raise
        return ClickHouseExport(response=response, client=client)


def _validated_days(days: int) -> int:
    if not 1 <= int(days) <= MAX_PROVIDER_EXPORT_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_PROVIDER_EXPORT_DAYS}")
    return int(days)
