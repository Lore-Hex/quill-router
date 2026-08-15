from __future__ import annotations

import json

from fastapi.testclient import TestClient

from trusted_router.benchmark_reports import monthly_benchmark_reports


def test_checked_in_monthly_reports_cover_june_and_july() -> None:
    reports = monthly_benchmark_reports()

    assert [report["period"] for report in reports] == ["2026-07", "2026-06"]
    assert reports[0]["row_count"] >= 200_000
    assert reports[1]["row_count"] >= 900_000
    assert all(report["top_providers"] for report in reports)
    assert all(report["top_model_routes"] for report in reports)


def test_monthly_report_pages_and_downloads_are_public(client: TestClient) -> None:
    index = client.get("/benchmarks/reports")

    assert index.status_code == 200
    assert "June 2026" in index.text
    assert "July 2026" in index.text
    assert "ItemList" in index.text

    for period in ("2026-06", "2026-07"):
        page = client.get(f"/benchmarks/reports/{period}")
        download = client.get(f"/benchmarks/reports/{period}.json")

        assert page.status_code == 200
        assert "Dataset" in page.text
        assert "Wilson confidence" in page.text
        assert download.status_code == 200
        assert download.headers["cache-control"].endswith("s-maxage=86400")
        assert download.json()["data"]["period"] == period


def test_monthly_report_json_contains_no_tenant_content_or_spend() -> None:
    payload = json.dumps(monthly_benchmark_reports()).lower()

    for forbidden in (
        "api_key",
        "workspace_id",
        "user_id",
        "prompt_text",
        "output_text",
        "cost_microdollars",
        "input_tokens",
        "output_tokens",
    ):
        assert forbidden not in payload


def test_unknown_monthly_report_is_custom_404(client: TestClient) -> None:
    page = client.get("/benchmarks/reports/2020-01")
    download = client.get("/benchmarks/reports/2020-01.json")

    assert page.status_code == 404
    assert "Page Not Found" in page.text
    assert download.status_code == 404
    assert download.json()["error"]["type"] == "not_found"


def test_monthly_reports_are_linked_from_high_intent_pages(client: TestClient) -> None:
    for path in (
        "/benchmarks",
        "/compare/models",
        "/docs/agent-setup",
        "/eu",
        "/deepseek-api-privacy",
    ):
        response = client.get(path)

        assert response.status_code == 200
        assert 'href="/benchmarks/reports"' in response.text
