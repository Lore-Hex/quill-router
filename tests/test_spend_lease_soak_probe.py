from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.synthetic.components import OPS_PROBE_TYPES
from trusted_router.synthetic.probes import (
    SPEND_LEASE_SOAK_MODEL,
    SPEND_LEASE_SOAK_ROUTE_TYPE,
    SyntheticTarget,
    spend_lease_soak_probe,
)


def test_spend_lease_soak_probe_flag_defaults_false_mutation_guard() -> None:
    """Mutation guard: changing the Settings default to True must fail this test."""
    assert Settings(environment="test").spend_lease_soak_probe_enabled is False


def test_spend_lease_soak_probe_secret_name_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings(environment="test")
    assert (
        defaults.spend_lease_probe_key_secret
        == "trustedrouter-spend-lease-probe-key"  # noqa: S105 - resource name.
    )

    monkeypatch.setenv("TR_SPEND_LEASE_PROBE_KEY_SECRET", "custom-soak-key")
    assert (
        Settings(environment="test").spend_lease_probe_key_secret
        == "custom-soak-key"  # noqa: S105 - resource name.
    )


@pytest.mark.asyncio
async def test_spend_lease_soak_request_stays_inside_stage_a_cohort() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-soak",
                "model": SPEND_LEASE_SOAK_MODEL,
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"cost_microdollars": 53},
            },
        )

    target = SyntheticTarget(
        "canonical",
        "https://api.trustedrouter.com/v1",
        "us-central1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await spend_lease_soak_probe(
            client,
            target,
            monitor_region="us-central1",
            api_key="sk-tr-soak-test",
        )

    assert sample.status == "up"
    assert sample.probe_type == "spend_lease_soak"
    assert sample.cost_microdollars == 53
    assert SPEND_LEASE_SOAK_ROUTE_TYPE == "chat.completions"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-tr-soak-test"
    body = json.loads(request.content)
    assert body == {
        "model": "anthropic/claude-haiku-4.5",
        "messages": [{"role": "user", "content": "Reply OK."}],
        "max_tokens": 8,
        "metadata": {
            "trustedrouter_synthetic": "true",
            "probe": "spend_lease_soak",
        },
    }
    # These are the Stage A disqualifiers a public request could introduce.
    # Route type comes solely from the chat.completions path asserted above.
    assert {
        "app",
        "app_id",
        "app_markup_basis_points",
        "additional_cost_microdollars",
        "additional_cost_reservation_microdollars",
        "native_batch",
        "provider",
        "route_type",
    }.isdisjoint(body)


@pytest.mark.asyncio
async def test_spend_lease_soak_job_loads_named_key_and_ingests_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import spend_lease_soak as soak_job

    seen_secret: list[tuple[str, str]] = []
    seen_paths: list[str] = []
    ingested: list[dict[str, Any]] = []

    def fake_loader(*, project_id: str, secret_name: str) -> Any:
        seen_secret.append((project_id, secret_name))
        return lambda: "sk-tr-dedicated-soak"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat/completions":
            assert request.headers["authorization"] == "Bearer sk-tr-dedicated-soak"
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-soak-job",
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"cost_microdollars": 53},
                },
            )
        if request.url.path == "/v1/internal/synthetic/samples":
            assert request.headers["x-trustedrouter-internal-token"] == "observer-test"
            ingested.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"recorded": 1}})
        return httpx.Response(404)

    settings = Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
        gcp_project_id="probe-project",
        observer_internal_token="observer-test",  # noqa: S106 - test placeholder.
        spend_lease_soak_probe_enabled=True,
        spend_lease_probe_key_secret="custom-soak-key",  # noqa: S106 - secret name.
    )
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(soak_job, "get_settings", lambda: settings)
    monkeypatch.setattr(soak_job, "secret_manager_text_loader", fake_loader)
    monkeypatch.setattr(soak_job.httpx, "AsyncClient", client_factory)
    monkeypatch.setenv(
        "TR_SYNTHETIC_INGEST_URL",
        "https://trustedrouter.com/v1/internal/synthetic/samples",
    )

    result = await soak_job.run()

    assert result == 0
    assert seen_secret == [("probe-project", "custom-soak-key")]
    assert seen_paths == ["/v1/chat/completions", "/v1/internal/synthetic/samples"]
    assert ingested[0]["samples"][0]["probe_type"] == "spend_lease_soak"


def test_spend_lease_soak_probe_is_registered_on_one_minute_schedule() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/deploy/synthetic.sh"
    body = script.read_text()
    section = body.split("Stage A spend-lease soak", maxsplit=1)[1].split(
        "# Image generation",
        maxsplit=1,
    )[0]

    assert 'spend_lease_scheduler_name="${spend_lease_job_name}-every-minute"' in section
    assert '--args="-m,trusted_router.synthetic.spend_lease_soak"' in section
    assert "spend_lease_soak" in OPS_PROBE_TYPES
    assert '"TR_SPEND_LEASE_SOAK_PROBE_ENABLED=${spend_lease_probe_enabled}"' in section
    assert '"TR_SPEND_LEASE_PROBE_KEY_SECRET=${spend_lease_probe_key_secret}"' in section
    assert '"* * * * *"' in section
    assert "--max-retries 0" in section
