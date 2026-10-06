"""Cloudflare catalog refresh cannot activate external API providers."""

import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.providers import cloudflare_workers_ai as cloudflare


@pytest.mark.parametrize("passthrough", [
    "openai/gpt-future", "anthropic/claude-future", "google/gemini-future",
    "new-author/new-api",
])
def test_refresh_and_relisting_preserve_native_hosting_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passthrough: str,
) -> None:
    manifest = tmp_path / "cloudflare-workers-ai.json"
    monkeypatch.setattr(cloudflare, "MANIFEST_PATH", manifest)
    monkeypatch.setenv("CLOUDFLARE_WORKERS_AI_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_WORKERS_AI_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("TR_CLOUDFLARE_WORKERS_AI_ROUTABLE", "1")
    native = [
        "@cf/openai/gpt-oss-120b", "@cf/google/gemma-4-26b-a4b-it",
        "@cf/moonshotai/kimi-k2.7-code",
    ]
    payload = {"result": [{
        "id": "opaque-id", "name": name, "task": {"name": "Text Generation"},
        "properties": {"price": [
            {"unit": "input tokens", "price": "0.5"},
            {"unit": "output tokens", "price": "1.0"},
        ]},
    } for name in [*native, passthrough]]}
    monkeypatch.setattr(
        cloudflare.httpx, "HTTPTransport",
        lambda **_kwargs: httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )
    for previous_reason in (None, "delisted-upstream", "third-party-passthrough-disabled"):
        if previous_reason is not None:
            previous = json.loads(manifest.read_text())
            row = next(row for row in previous["models"] if row["id"] == passthrough)
            row.update(routable=False, routable_reason=previous_reason)
            manifest.write_text(json.dumps(previous))
        cloudflare.write_provider_manifest(cloudflare.fetch())
        rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
        assert rows[passthrough]["routable"] is False
        assert rows[passthrough]["routable_reason"] == "third-party-passthrough-disabled"
        for native_id in [*native, "moonshotai/kimi-k3"]:
            assert rows[cloudflare._canonical_model_id(native_id)]["routable"] is True
