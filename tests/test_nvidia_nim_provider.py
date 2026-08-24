from __future__ import annotations

import json

import httpx

from scripts.pricing.providers import nvidia_nim
from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS


def test_nvidia_nim_discovery_is_authenticated_and_never_routable(
    monkeypatch, tmp_path
) -> None:
    seen_auth = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_auth
        seen_auth = request.headers.get("Authorization", "")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": f"vendor/model-{index}", "object": "model"}
                    for index in range(12)
                ]
            },
        )

    class FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler))

    manifest = tmp_path / "nvidia-nim.json"
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")
    monkeypatch.setattr(nvidia_nim.httpx, "Client", FakeClient)
    monkeypatch.setattr(nvidia_nim, "MANIFEST_PATH", manifest)

    result = nvidia_nim.fetch()
    nvidia_nim.write_provider_manifest(result)

    assert seen_auth == "Bearer nim-test-key"
    payload = json.loads(manifest.read_text())
    assert payload["model_count"] == 12
    assert all(row["routable"] is False for row in payload["models"])
    assert {
        row["routable_reason"] for row in payload["models"]
    } == {"production-entitlement-required"}
    assert "nvidia-nim" not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert PROVIDERS["nvidia-nim"].supports_prepaid is False
