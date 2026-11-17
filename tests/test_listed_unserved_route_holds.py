"""Routes a provider still lists but refuses to serve stay operator-held.

Phala retired its confidential phala/glm-5.1 alias on November 11 (Sentry
QUILL-ROUTER-G5) and Makora answers "Model not available." for Muse Spark
Open 816B NVFP4 since November 10 (Sentry QUILL-ROUTER-G7). The 24-hour
unserved-route hold lifts on expiry and the routes fail again, so each one
is held until a reviewed recovery, without disabling other routes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trusted_router import catalog
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints
from trusted_router.provider_contracts import provider_model_operator_held
from trusted_router.synthetic.probes import rotation_candidates

HELD = (
    ("phala", "z-ai/glm-5.1", "z-ai/glm-5.1", "chutes", "zai-org/GLM-5.1-TEE"),
    (
        "makora",
        "meta-models/muse-spark-open-816b-a42b-nvfp4",
        "meta-models/Muse-Spark-Open-816B-A42B-NVFP4",
        "novita",
        "meta-models/muse-spark-open-816b-a42b-nvfp4",
    ),
)


@pytest.mark.parametrize(("provider", "model", "native", "other", "other_native"), HELD)
def test_listed_unserved_route_is_held_without_holding_other_routes(
    provider: str, model: str, native: str, other: str, other_native: str,
) -> None:
    assert provider_model_operator_held(provider, model, native)
    assert provider_model_operator_held(provider, model)
    assert not provider_model_operator_held(other, model, other_native)
    assert not provider_model_operator_held(provider, "moonshotai/kimi-k3", "moonshotai/kimi-k3")


@pytest.mark.parametrize(("provider", "model", "native", "other", "other_native"), HELD)
def test_stale_positive_catalog_cannot_restore_the_route(
    monkeypatch: pytest.MonkeyPatch,
    provider: str, model: str, native: str, other: str, other_native: str,
) -> None:
    endpoints = [
        ModelEndpoint(
            id=f"{model}@{host}/prepaid", model_id=model, provider=host,
            upstream_id=upstream, usage_type="Credits",
        )
        for host, upstream in ((provider, native), (other, other_native))
    ]
    monkeypatch.setattr(
        "trusted_router.catalog_ingest._provider_manifest_dark_model_ids", lambda: {},
    )
    filtered = _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint for endpoint in endpoints}, explicit_model_ids=frozenset({model}),
    )
    assert [endpoint.provider for endpoint in filtered.values()] == [other]


@pytest.mark.parametrize(("provider", "model", "native", "other", "other_native"), HELD)
def test_held_route_is_not_public_or_a_synthetic_candidate(
    provider: str, model: str, native: str, other: str, other_native: str,
) -> None:
    assert not any(endpoint.provider == provider for endpoint in catalog.endpoints_for_model(model))
    assert model not in rotation_candidates().get(provider, [])
    # The provider's other routes stay probed.
    assert rotation_candidates().get(provider)


def test_makora_refresh_keeps_the_hold_while_the_feed_lists_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.pricing.providers import makora

    native = "meta-models/Muse-Spark-Open-816B-A42B-NVFP4"
    manifest = tmp_path / "makora.json"
    manifest.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(makora, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(makora, "EXPECTED_MODELS", ["moonshotai/kimi-k3"])
    monkeypatch.setattr(makora, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("MAKORA_API_KEY", "test-key")
    monkeypatch.setattr(makora, "fetch_json", lambda *_args, **_kwargs: {
        "data": [
            {"id": native, "pricing": {"prompt": "0.00000125", "completion": "0.00000425"}},
            {"id": "moonshotai/Kimi-K3", "pricing": {"prompt": "0.00000255", "completion": "0.00001275"}},
        ],
    })

    result = makora.fetch()
    for _ in range(2):
        makora.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    held = rows["meta-models/muse-spark-open-816b-a42b-nvfp4"]
    assert (held["routable"], held["routable_reason"]) == (False, "provider-model-not-found")
    assert held["upstream_id"] == native
    assert rows["moonshotai/kimi-k3"].get("routable", True) is True


def test_committed_makora_manifest_carries_the_hold() -> None:
    import json

    from scripts.pricing.providers import makora

    rows = {row["id"]: row for row in json.loads(makora.MANIFEST_PATH.read_text())["models"]}
    held = rows["meta-models/muse-spark-open-816b-a42b-nvfp4"]
    assert (held["routable"], held["routable_reason"]) == (False, "provider-model-not-found")
