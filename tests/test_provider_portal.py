from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def _login(client: TestClient, email: str = "rob@neurometric.ai") -> str:
    user = STORE.ensure_user(email, email=email, trial_credit_microdollars=0)
    raw, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label=email,
        ttl_seconds=3600,
    )
    client.cookies.set("tr_session", raw)
    return user.id


class _FakeAnalytics:
    async def summary(self, provider: str, *, days: int = 7) -> dict[str, object]:
        return {
            "provider": provider,
            "days": days,
            "totals": {
                "attempts": 4,
                "completed": 3,
                "failed": 1,
                "completion_rate": 0.75,
                "organic_requests": 3,
                "synthetic_requests": 1,
                "p50_ttft_ms": 120,
                "p95_ttft_ms": 300,
            },
            "models": [
                {
                    "model": "ibm-granite/granite-4.1-8b",
                    "attempts": 4,
                    "completed": 3,
                    "failed": 1,
                    "p50_ttft_ms": 120,
                    "p95_ttft_ms": 300,
                    "p50_tokens_per_second": 40,
                }
            ],
            "errors": [
                {"error_type": "provider_error", "error_status": "503", "occurrences": 1}
            ],
            "daily": [],
        }

    async def open_csv_export(self, provider: str, *, days: int = 60) -> object:
        @dataclass
        class Export:
            async def chunks(self) -> AsyncIterator[bytes]:
                yield f"provider\n{provider}\n".encode()

        return Export()


def test_provider_portal_requires_explicit_grant(client: TestClient) -> None:
    _login(client)

    response = client.get("/provider")

    assert response.status_code == 403


def test_provider_portal_renders_only_granted_provider(
    client: TestClient,
    monkeypatch,
) -> None:
    user_id = _login(client)
    STORE.grant_provider_access(user_id, "neurometric")
    monkeypatch.setattr(
        "trusted_router.routes.provider_portal._client",
        lambda _settings: _FakeAnalytics(),
    )

    response = client.get("/provider?provider=neurometric&days=30")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "neurometric operations" in response.text
    assert "75.000%" in response.text
    assert "ibm-granite/granite-4.1-8b" in response.text
    assert "Prompts, outputs, customer identities" in response.text
    assert client.get("/provider?provider=together").status_code == 403


def test_provider_csv_download_is_private_and_bounded(
    client: TestClient,
    monkeypatch,
) -> None:
    user_id = _login(client)
    STORE.grant_provider_access(user_id, "neurometric")
    monkeypatch.setattr(
        "trusted_router.routes.provider_portal._client",
        lambda _settings: _FakeAnalytics(),
    )

    response = client.get("/provider/requests.csv?provider=neurometric&days=60")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].endswith(
        '"trustedrouter-neurometric-requests-60d.csv"'
    )
    assert response.text == "provider\nneurometric\n"
    assert client.get("/provider/requests.csv?days=61").status_code == 400
