"""`workspace_id` reaches analytics storage but never a public surface.

`ProviderBenchmarkSample` carries a tenant id so per-customer usage history
survives Spanner's 30-day `tr_generation` deletion policy. The same rows feed
public pages (leaderboard, model/provider rankings, /apps), so the boundary
these tests pin is: the field flows into the ClickHouse row shape, and does
NOT appear in any public response.

If you add a public consumer of benchmark samples, add it to
`test_public_surfaces_never_expose_workspace_id`.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.analytics_sink import _row_from_sample
from trusted_router.apps import aggregate_apps
from trusted_router.storage_models import Generation, ProviderBenchmarkSample
from trusted_router.synthetic.leaderboard import aggregate_leaderboard

WORKSPACE = "ws-secret-tenant-id"


def _generation() -> Generation:
    return Generation(
        id="gen-abc",
        request_id="req-abc",
        workspace_id=WORKSPACE,
        key_hash="key-hash",
        model="deepseek/deepseek-v4-flash-0731",
        provider_name="DeepSeek",
        app="attested-gateway",
        tokens_prompt=1_000,
        tokens_completion=50,
        total_cost_microdollars=120,
        usage_type="Credits",
        speed_tokens_per_second=40.0,
        finish_reason="stop",
        status="success",
        streamed=True,
        usage_estimated=False,
        provider="deepseek",
    )


def _sample() -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample.from_generation(_generation())


def test_sample_carries_workspace_id_from_generation() -> None:
    assert _sample().workspace_id == WORKSPACE


def test_clickhouse_row_includes_workspace_id() -> None:
    """The durable analytics row must carry the tenant, or ClickHouse cannot
    answer per-customer usage beyond Spanner's 30-day window."""
    row = _row_from_sample(_sample())
    assert row["workspace_id"] == WORKSPACE


def test_error_samples_default_to_unattributed() -> None:
    """Error paths without an authorization in scope produce '' — treated as
    unattributed, never as a real workspace."""

    class _Model:
        id = "deepseek/deepseek-v4-flash-0731"
        provider = "deepseek"

    sample = ProviderBenchmarkSample.from_provider_error(
        model=_Model(),
        provider_name="DeepSeek",
        input_tokens=10,
        elapsed_seconds=0.5,
        streamed=False,
        usage_type="Credits",
        error_status=502,
        error_type="provider_error",
        region=None,
    )
    assert sample.workspace_id == ""
    assert _row_from_sample(sample)["workspace_id"] == ""


def test_aggregators_never_emit_workspace_id() -> None:
    """The two aggregators that public pages render must not project the
    tenant id, no matter what the samples carry."""
    samples = [_sample()]
    assert WORKSPACE not in repr(aggregate_leaderboard(samples, min_samples=1))
    assert WORKSPACE not in repr(aggregate_apps(samples))


def test_public_surfaces_never_expose_workspace_id(client: TestClient) -> None:
    """End-to-end: no public page or feed may contain a workspace id."""
    for path in (
        "/leaderboard",
        "/apps",
        "/rankings",
        "/benchmarks",
        "/status.json",
        "/models",
        "/providers",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "workspace_id" not in response.text, f"{path} leaked the workspace_id field"
        assert WORKSPACE not in response.text, f"{path} leaked a workspace id value"
