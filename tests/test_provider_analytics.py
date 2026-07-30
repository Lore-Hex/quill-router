from __future__ import annotations

import httpx
import pytest

from trusted_router.provider_analytics import ProviderAnalyticsClient


@pytest.mark.anyio
async def test_summary_uses_fixed_parameterized_provider_queries() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        query = request.content.decode()
        if "GROUP BY model" in query:
            data = [
                {
                    "model": "n/model",
                    "attempts": 30,
                    "completed": 28,
                    "failed": 2,
                    "provider_organic_requests": 30,
                    "model_organic_requests": 100,
                    "provider_completed_organic_requests": 28,
                    "model_completed_organic_requests": 80,
                    "active_organic_providers": 3,
                }
            ]
        elif "GROUP BY error_type" in query:
            data = []
        elif "GROUP BY day" in query:
            data = [{"day": "2026-07-30", "attempts": 2, "completed": 2, "failed": 0}]
        else:
            data = [{"attempts": 2, "completed": 2, "failed": 0}]
        return httpx.Response(200, json={"data": data})

    client = ProviderAnalyticsClient(
        base_url="http://clickhouse.internal:8123",
        user="reader",
        password="secret",  # noqa: S106 - test-only fake transport credential
        transport=httpx.MockTransport(handler),
    )
    result = await client.summary("neurometric", days=7)

    assert result["totals"]["completion_rate"] == 1
    assert result["models"][0]["offered_traffic_share"] == 0.3
    assert result["models"][0]["completed_traffic_share"] == 0.35
    assert result["minimum_traffic_share_samples"] == 20
    assert len(seen) == 4
    for request in seen:
        assert request.url.params["param_provider"] == "neurometric"
        assert request.url.params["param_days"] == "7"
        assert "{provider:String}" in request.content.decode()
        assert "neurometric" not in request.content.decode()
        assert request.url.params["readonly"] == "2"
    model_query = next(
        request.content.decode()
        for request in seen
        if "GROUP BY model" in request.content.decode()
    )
    assert "source = 'organic'" in model_query
    assert "uniqExactIf(provider" in model_query


@pytest.mark.anyio
async def test_csv_export_is_bounded_and_excludes_sensitive_columns() -> None:
    captured_query = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_query
        captured_query = request.content.decode()
        return httpx.Response(200, content=b"request_metadata_id,provider\nid-1,neurometric\n")

    client = ProviderAnalyticsClient(
        base_url="http://clickhouse.internal:8123",
        user="reader",
        password="secret",  # noqa: S106 - test-only fake transport credential
        transport=httpx.MockTransport(handler),
    )
    export = await client.open_csv_export("neurometric", days=60)
    payload = b"".join([chunk async for chunk in export.chunks()])

    assert b"id-1,neurometric" in payload
    assert "prompt" not in captured_query
    assert "workspace" not in captured_query
    assert "\n  app," not in captured_query
    assert "error_message" not in captured_query
    assert "{provider:String}" in captured_query


@pytest.mark.anyio
async def test_csv_export_rejects_more_than_sixty_days() -> None:
    client = ProviderAnalyticsClient(
        base_url="http://clickhouse.internal:8123",
        user="reader",
        password="secret",  # noqa: S106 - test-only fake transport credential
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"data": []})
        ),
    )
    with pytest.raises(ValueError, match="between 1 and 60"):
        await client.open_csv_export("neurometric", days=61)


def test_clickhouse_identifiers_are_not_user_controlled_sql() -> None:
    with pytest.raises(ValueError, match="identifier"):
        ProviderAnalyticsClient(
            base_url="http://clickhouse.internal:8123",
            user="reader",
            password="secret",  # noqa: S106 - test-only fake transport credential
            table="rows; DROP TABLE rows",
        )
