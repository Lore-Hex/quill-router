from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from trusted_router.config import Settings
from trusted_router.routes import public
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic.probes import _rotation_max_tokens
from trusted_router.synthetic.status import history_payload, status_snapshot


def test_monthly_history_reads_months_not_fifty_daily_dimensions(monkeypatch, client) -> None:
    calls = []
    now = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
    months = [
        SyntheticRollup(
            id=f"month-{month}",
            period="month",
            period_start=f"2026-{month:02d}-01T00:00:00Z",
            component="canonical_api",
            target="canonical",
            probe_type="tls_health",
            monitor_region="us-central1",
            sample_count=100,
            up_count=99,
            down_count=1,
            latency_histogram={"120": 100},
            last_checked_at=f"2026-{month:02d}-05T00:00:00Z",
        )
        for month in range(5, 10)
    ]

    def read(_store, **kwargs):
        calls.append(kwargs)
        return months if kwargs["period"] == "month" else []

    monkeypatch.setattr(public, "utcnow", lambda: now)
    monkeypatch.setattr(type(public.STORE.target), "synthetic_rollups", read)
    rows = public._status_rollups("monthly")
    result = history_payload([], "monthly", rollups=rows)
    assert [r["period_start"][:7] for r in result["data"]] == [
        "2026-09",
        "2026-08",
        "2026-07",
        "2026-06",
        "2026-05",
    ]
    assert calls[0]["period"] == "month"
    assert calls[0].get("include_histograms", True)
    assert calls[0]["since"] == "2024-10-01T00:00:00Z"
    assert result["data"][0]["groups"][0]["p50_latency_milliseconds"] == 120
    response = client.get("/status/history?window=monthly", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "Monthly rollups" in response.text
    for month in range(5, 10):
        assert f"2026-{month:02d}" in response.text


@pytest.mark.parametrize(
    "probe,component",
    [
        ("gateway_authorize", "billing_settlement"),
        ("gateway_settle", "billing_settlement"),
        ("provider_fallback", "provider_fallback"),
        ("openai_sdk_pong", "model_inference"),
        ("responses_pong", "model_inference"),
    ],
)
def test_public_status_declares_monitoring_without_sender_secrets(probe, component) -> None:
    now = dt.datetime.now(dt.UTC)
    settings = Settings(
        environment="test",
        internal_gateway_token=None,
        synthetic_monitor_api_key=None,
        synthetic_status_probe_types="gateway_authorize,gateway_settle,provider_fallback,openai_sdk_pong,responses_pong",
    )
    sample = SyntheticProbeSample(
        id=f"test-{probe}",
        probe_type=probe,
        target="canonical" if component == "model_inference" else "control-plane",
        target_url="https://example.test",
        monitor_region="us-central1",
        status="down",
        created_at=now.isoformat(),
    )
    result = status_snapshot(
        [sample, replace(sample, id=f"eu-{probe}", monitor_region="europe-west4")],
        now=now,
        settings=settings,
    )
    row = next(r for r in result["components"] if r["id"] == component)
    assert row["status"] == "down"
    assert result["overall_status"] != "up"
    if component != "model_inference":
        assert result["slo_classes"]["router_core"]["windows"]["5m"]["bad_count"] == 2
    assert settings.internal_gateway_token is None
    assert settings.synthetic_monitor_api_key is None


@pytest.mark.parametrize(
    "provider,model",
    [
        ("inception", "inception/mercury-2"),
        ("sakana", "sakana-ai/fugu-ultra-v1.1"),
        ("cerebras", "cerebras/qwen-3.8-27b"),
        ("azure", "openai/gpt-5-mini"),
        ("tinfoil", "moonshotai/kimi-k3"),
        ("sail-research", "deepseek/deepseek-v4-flash-0731"),
        ("together", "google/gemma-4-31b-it"),
        ("telnyx", "minimax/minimax-m2.7"),
    ],
)
def test_reasoning_probe_budgets_are_publisher_aware(provider, model) -> None:
    assert 512 <= _rotation_max_tokens(provider, model) <= 2048


def test_missing_transaction_evidence_cannot_leave_a_green_banner() -> None:
    now = dt.datetime.now(dt.UTC)
    settings = Settings(environment="test", synthetic_status_probe_types="gateway_authorize,gateway_settle")
    live = SyntheticProbeSample(
        id="live", probe_type="tls_health", target="canonical", target_url="https://example.test",
        monitor_region="us-central1", status="up", created_at=now.isoformat(),
    )
    result = status_snapshot([live], now=now, settings=settings)
    assert result["overall_status"] == "degraded"
    assert result["summary"] == "Monitoring coverage incomplete"
    assert result["monitoring_gaps"] == ["gateway_authorize", "gateway_settle"]
    assert result["slo_classes"]["router_core"]["windows"]["5m"]["bad_count"] == 0


def test_invalid_expected_probe_fails_at_configuration_time() -> None:
    with pytest.raises(ValueError, match="unknown transaction probe"):
        Settings(environment="test", synthetic_status_probe_types="gateway_settlee")
