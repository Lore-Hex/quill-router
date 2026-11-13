"""/v1/models publishes each model's output-token cap.

top_provider.max_completion_tokens was always null, so catalog consumers
(models.dev's sync, opencode, catwalk) fell back to guessing an output limit,
often the whole context window. The cap now comes from the source catalogs:
the OpenRouter snapshot's per-host `max_completion_tokens` and model
`top_provider`, and the provider manifests' `max_output_tokens`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trusted_router import catalog, catalog_ingest
from trusted_router.catalog_data import Model, ModelEndpoint

DATA = Path(catalog_ingest.__file__).parent / "data"


def _model(**overrides: object) -> Model:
    fields: dict[str, object] = {
        "id": "fixture/output-cap",
        "name": "Fixture",
        "provider": "deepinfra",
        "context_length": 131_072,
    }
    fields.update(overrides)
    return Model(**fields)  # type: ignore[arg-type]


def _endpoint(provider: str, cap: int | None) -> ModelEndpoint:
    return ModelEndpoint(
        id=f"fixture/output-cap@{provider}/prepaid",
        model_id="fixture/output-cap",
        provider=provider,
        usage_type="Credits",
        max_completion_tokens=cap,
    )


def test_the_makers_own_route_cap_wins_over_other_hosts_claims():
    # GLM-5.3: Z.ai states 131,072 while other hosts claim up to the window.
    model = _model(id="z-ai/glm-5.3", provider="zai", context_length=1_048_576, max_completion_tokens=943_718)
    endpoints = [_endpoint("zai", 131_072), _endpoint("inceptron", 1_048_576), _endpoint("novita", None)]
    assert catalog.published_max_completion_tokens(model, endpoints) == 131_072


def test_without_a_maker_route_the_source_model_cap_stands():
    model = _model(provider="mistral", max_completion_tokens=32_768)
    endpoints = [_endpoint("mistral", None), _endpoint("amazon-bedrock", 8000), _endpoint("novita", 131_072)]
    assert catalog.published_max_completion_tokens(model, endpoints) == 32_768
    assert catalog.published_max_completion_tokens(model, []) == 32_768


def test_with_neither_the_largest_route_cap_is_the_last_resort():
    endpoints = [_endpoint("amazon-bedrock", 4096), _endpoint("deepinfra", 65_536), _endpoint("novita", None)]
    assert catalog.published_max_completion_tokens(_model(), endpoints) == 65_536
    assert catalog.published_max_completion_tokens(_model(), [_endpoint("deepinfra", None)]) is None
    assert catalog.published_max_completion_tokens(_model(), []) is None


def test_the_cap_never_exceeds_what_every_route_allows():
    # Every route serving the model stops at 32,768: publishing more would let
    # clients request output no route produces.
    endpoints = [_endpoint("google-vertex", 32_768), _endpoint("google-ai-studio", 32_768)]
    model = _model(id="google/gemini-4-carbon", provider="google-ai-studio", context_length=1_048_576, max_completion_tokens=943_718)
    assert catalog.published_max_completion_tokens(model, endpoints) == 32_768


def test_a_cap_never_exceeds_the_window_routing_lets_a_request_use():
    # Routing turns away prompt + max_tokens beyond the route's window.
    model = _model(context_length=65_536, max_completion_tokens=262_144)
    assert catalog.published_max_completion_tokens(model, [_endpoint("deepinfra", 262_144)]) == 65_536
    narrow = ModelEndpoint(
        id="fixture/output-cap@novita/prepaid", model_id=model.id, provider="novita",
        usage_type="Credits", context_length=32_768, max_completion_tokens=100_000,
    )
    assert catalog.published_max_completion_tokens(_model(context_length=131_072), [narrow]) == 32_768
    # A fallback cap is bounded by the routes' windows even when they state no cap.
    uncapped = ModelEndpoint(
        id="fixture/output-cap@novita/prepaid", model_id=model.id, provider="novita",
        usage_type="Credits", context_length=262_144,
    )
    inkling = _model(context_length=1_048_576, max_completion_tokens=471_859)
    assert catalog.published_max_completion_tokens(inkling, [uncapped]) == 262_144


def test_orchestration_models_publish_no_cap():
    for model_id in sorted(catalog.META_MODEL_IDS):
        row = catalog.model_to_openrouter_shape(catalog.MODELS[model_id])
        assert row["top_provider"]["max_completion_tokens"] is None, model_id


def test_snapshot_caps_match_the_source_rows():
    snapshot = json.loads((DATA / "openrouter_snapshot.json").read_text(encoding="utf-8"))
    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    checked_models = checked_endpoints = 0
    for raw in snapshot["models"]:
        model = models.get(raw["id"])
        if model is None:
            continue
        expected = (raw.get("top_provider") or {}).get("max_completion_tokens") or None
        assert model.max_completion_tokens == expected, raw["id"]
        checked_models += expected is not None
        # A host can list several rows for one model (quantizations); its
        # route is built from one of them.
        caps_by_host: dict[str, set[int | None]] = {}
        for raw_ep in raw.get("endpoints") or []:
            caps_by_host.setdefault(raw_ep.get("tr_provider_slug"), set()).add(
                raw_ep.get("max_completion_tokens") or None
            )
        for slug, caps in caps_by_host.items():
            for suffix in ("prepaid", "byok"):
                endpoint = endpoints.get(f"{raw['id']}@{slug}/{suffix}")
                if endpoint is None:
                    continue
                assert endpoint.max_completion_tokens in caps, endpoint.id
                checked_endpoints += endpoint.max_completion_tokens is not None
    # Not vacuous: the snapshot states caps for most of its models and routes.
    assert checked_models > 100
    assert checked_endpoints > 100


def test_manifest_caps_match_the_source_rows():
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    checked = 0
    for path in sorted((DATA / "provider_models").glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        slug = manifest.get("provider")
        for raw in manifest.get("models") or []:
            for suffix in ("prepaid", "byok"):
                endpoint = endpoints.get(f"{raw.get('id')}@{slug}/{suffix}")
                if endpoint is None:
                    continue
                expected = None if raw.get("limits_source") == "placeholder" else (
                    raw.get("max_output_tokens") or raw.get("max_completion_tokens") or None
                )
                window = raw.get("context_length")
                if expected is not None and window and expected > window:
                    expected = None  # larger than the row's own window
                assert endpoint.max_completion_tokens == expected, endpoint.id
                checked += expected is not None
    assert checked > 500


@pytest.mark.catalog_as_built
def test_v1_models_publishes_caps_for_most_tool_models(client):
    rows = client.get("/v1/models").json()["data"]
    tool_models = [
        row for row in rows
        if "tools" in row["supported_parameters"] and not row["id"].startswith("trustedrouter/")
    ]
    capped = [row for row in tool_models if row["top_provider"]["max_completion_tokens"]]
    assert len(capped) > len(tool_models) // 2
    for row in capped:
        assert isinstance(row["top_provider"]["max_completion_tokens"], int), row["id"]
        assert row["top_provider"]["max_completion_tokens"] > 0, row["id"]


def test_bedrock_placeholder_rows_state_no_cap():
    manifest = json.loads((DATA / "provider_models" / "amazon-bedrock.json").read_text(encoding="utf-8"))
    placeholders = [raw for raw in manifest["models"] if raw.get("limits_source") == "placeholder"]
    assert placeholders, "positive control: the manifest marks its placeholder rows"
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for raw in placeholders:
        endpoint = endpoints.get(f"{raw['id']}@amazon-bedrock/prepaid")
        if endpoint is not None:
            assert endpoint.max_completion_tokens is None, endpoint.id


def test_the_assembled_registry_keeps_a_snapshot_cap_a_manifest_does_not_state():
    _snapshot_models, snapshot = catalog_ingest._ingested_models_and_endpoints()
    _manifest_models, manifest = catalog_ingest._supplemental_provider_models_and_endpoints()
    overridden = [
        endpoint_id for endpoint_id, endpoint in manifest.items()
        if endpoint.max_completion_tokens is None
        and endpoint_id in snapshot and snapshot[endpoint_id].max_completion_tokens
    ]
    assert overridden, "positive control: some manifest routes replace a capped snapshot route"
    published = [endpoint_id for endpoint_id in overridden if endpoint_id in catalog.MODEL_ENDPOINTS]
    assert published, "positive control: at least one such route is served"
    for endpoint_id in published:
        assert catalog.MODEL_ENDPOINTS[endpoint_id].max_completion_tokens == (
            snapshot[endpoint_id].max_completion_tokens
        ), endpoint_id


def test_deepinfra_caps_that_restate_the_window_are_marked_and_not_caps():
    # DeepInfra's older rows (Gemini 2.5: 1M) repeat the window as
    # max_output_tokens; the adapter cannot confirm them, so they are marked.
    manifest = json.loads((DATA / "provider_models" / "deepinfra.json").read_text(encoding="utf-8"))
    restating = [
        raw for raw in manifest["models"]
        if raw.get("max_output_tokens") and raw.get("context_length")
        and raw["max_output_tokens"] >= raw["context_length"]
    ]
    assert restating, "positive control: DeepInfra rows restate their window"
    assert all(raw.get("limits_source") == "placeholder" for raw in restating)
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for raw in restating:
        endpoint = endpoints.get(f"{raw['id']}@deepinfra/prepaid")
        if endpoint is not None:
            assert endpoint.max_completion_tokens is None, endpoint.id


def test_a_cap_equal_to_the_window_is_kept_when_stated():
    # Chutes states max_output_length explicitly; 262,144 of 262,144 is real.
    manifest = json.loads((DATA / "provider_models" / "chutes.json").read_text(encoding="utf-8"))
    equal = [
        raw for raw in manifest["models"]
        if raw.get("max_output_tokens") and raw.get("max_output_tokens") == raw.get("context_length")
        and raw.get("limits_source") != "placeholder"
    ]
    assert equal, "positive control: Chutes states a cap equal to the window"
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for raw in equal:
        endpoint = endpoints.get(f"{raw['id']}@chutes/prepaid")
        if endpoint is not None:
            assert endpoint.max_completion_tokens == raw["max_output_tokens"], endpoint.id


def test_a_fallback_host_named_as_provider_is_not_the_maker():
    # tencent/hy3's Model.provider can name a reseller; Tencent's cap wins.
    model = _model(id="tencent/hy3", provider="novita", context_length=262_144)
    endpoints = [_endpoint("novita", 262_144), _endpoint("tencent", 131_072)]
    assert catalog.published_max_completion_tokens(model, endpoints) == 131_072
