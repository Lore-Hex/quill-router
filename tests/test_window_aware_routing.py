"""Chat routing sends a request only to models whose context window holds it.

The request's size is the prompt estimate its hold is reserved on (the
gateway's estimated_input_tokens, plus a custom model's hidden prompt) plus the
max_tokens the caller set. A model whose window cannot hold that is not a
candidate; a model whose window the catalog does not know (0) is. With no
candidate left, authorize answers 400 with OpenAI's context_length_exceeded
wording, before any hold.

Because routing filters, a routing pool's published context_length is the
largest window among the models it routes to: a request up to that size has a
model in the pool, and a larger one has none.

Each refusal below is checked against a positive control: the same request
without the window check (the behaviour on main), or a smaller request.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fixture_routes import serve_on_fixture_route
from tests.test_custom_models import _create_custom_model
from trusted_router import authorize_candidates, catalog, routing
from trusted_router.catalog_data import AUTO_MODEL_ID, Model
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway
from trusted_router.routing import candidates_fitting_context, chat_route_endpoint_candidates
from trusted_router.storage import STORE, InMemoryStore

SMALL = "fixture/window-small"
LARGE = "fixture/window-large"
UNKNOWN = "fixture/window-unknown"
SMALL_WINDOW = 8_000
LARGE_WINDOW = 200_000
AUTHORIZE = "/v1/internal/gateway/authorize"


def _serve(monkeypatch: pytest.MonkeyPatch, model_id: str, window: int, host: str) -> None:
    serve_on_fixture_route(
        monkeypatch, model_id, host, author="deepinfra",
        model=Model(id=model_id, name=model_id, provider="deepinfra", context_length=window),
    )


def _settings(*ladder: str) -> Settings:
    return Settings(environment="test", auto_model_order=",".join(ladder))


@pytest.fixture
def ladder(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """trustedrouter/auto over an 8k model and a 200k model."""
    _serve(monkeypatch, SMALL, SMALL_WINDOW, "novita")
    _serve(monkeypatch, LARGE, LARGE_WINDOW, "deepinfra")
    settings = _settings(SMALL, LARGE)
    monkeypatch.setattr(routing, "_app_settings", lambda: settings)
    return settings


def _fit(model_id: str, settings: Settings, prompt: int, completion: int = 0) -> list[str]:
    candidates = list(chat_route_endpoint_candidates({"model": model_id}, settings))
    fitting = candidates_fitting_context(
        candidates, prompt_tokens=prompt, completion_tokens=completion,
    )
    return [model.id for model, _endpoint in fitting]


def test_a_request_skips_models_whose_window_cannot_hold_it(ladder):
    assert _fit(AUTO_MODEL_ID, ladder, prompt=1_000) == [SMALL, LARGE]
    assert _fit(AUTO_MODEL_ID, ladder, prompt=10_000) == [LARGE]


def test_max_tokens_counts_and_its_absence_does_not(ladder):
    assert _fit(AUTO_MODEL_ID, ladder, prompt=6_000) == [SMALL, LARGE]
    assert _fit(AUTO_MODEL_ID, ladder, prompt=6_000, completion=2_000) == [SMALL, LARGE]
    assert _fit(AUTO_MODEL_ID, ladder, prompt=6_000, completion=2_001) == [LARGE]


def test_a_window_the_catalog_does_not_know_is_kept(monkeypatch):
    _serve(monkeypatch, UNKNOWN, 0, "novita")
    _serve(monkeypatch, LARGE, LARGE_WINDOW, "deepinfra")
    assert _fit(AUTO_MODEL_ID, _settings(UNKNOWN, LARGE), prompt=900_000) == [UNKNOWN]


def test_no_model_that_fits_is_openai_context_length_exceeded(ladder):
    with pytest.raises(HTTPException) as raised:
        _fit(AUTO_MODEL_ID, ladder, prompt=190_000, completion=20_000)
    assert raised.value.status_code == 400
    assert cast(dict[str, Any], raised.value.detail)["error"] == {
        "code": 400,
        "message": (
            "This model's maximum context length is 200000 tokens. However, you requested "
            "210000 tokens (190000 in the messages, 20000 in the completion). Please reduce "
            "the length of the messages or completion."
        ),
        "type": "context_length_exceeded",
        "source": "router",
        "param": "messages",
    }
    with pytest.raises(HTTPException) as raised:
        _fit(LARGE, ladder, prompt=200_001)
    assert cast(dict[str, Any], raised.value.detail)["error"]["message"] == (
        "This model's maximum context length is 200000 tokens. However, your messages "
        "resulted in 200001 tokens. Please reduce the length of the messages."
    )


def _client_and_key(settings: Settings) -> tuple[TestClient, str]:
    client = TestClient(create_app(settings, init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "window routing"},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]["hash"]


def _authorize(client: TestClient, key_hash: str, key: str, **body: Any) -> Any:
    return client.post(
        AUTHORIZE,
        json={
            "api_key_hash": key_hash,
            "model": AUTO_MODEL_ID,
            "route_type": "chat.completions",
            "max_output_tokens": 512,
            "idempotency_key": key,
            **body,
        },
    )


def _models(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return list(dict.fromkeys(c["model"] for c in response.json()["data"]["route_candidates"]))


def test_authorize_routes_a_long_prompt_only_to_a_model_that_holds_it(ladder, monkeypatch):
    client, key_hash = _client_and_key(ladder)
    assert _models(_authorize(client, key_hash, "short", estimated_input_tokens=1_000)) == [SMALL, LARGE]
    long_prompt = _authorize(client, key_hash, "long", estimated_input_tokens=10_000)
    assert _models(long_prompt) == [LARGE]
    assert long_prompt.json()["data"]["model"] == LARGE
    # max_tokens is the caller's own; the gateway's default output estimate
    # (max_output_tokens 512) is not a request for room.
    assert _models(_authorize(
        client, key_hash, "room", estimated_input_tokens=7_900,
    )) == [SMALL, LARGE]
    assert _models(_authorize(
        client, key_hash, "capped", estimated_input_tokens=7_000, max_tokens=1_500,
    )) == [LARGE]

    # Positive control: without the window check (main), the long prompt is
    # sent to the 8k model first.
    with monkeypatch.context() as patch:
        patch.setattr(
            authorize_candidates, "candidates_fitting_context",
            lambda candidates, **_tokens: list(candidates),
        )
        assert _models(_authorize(client, key_hash, "unchecked", estimated_input_tokens=10_000)) == [
            SMALL, LARGE,
        ]


def test_authorize_refuses_a_request_no_model_holds_before_any_hold(ladder):
    client, key_hash = _client_and_key(ladder)
    store = cast(InMemoryStore, STORE)
    assert _models(_authorize(client, key_hash, "fits", estimated_input_tokens=150_000)) == [LARGE]
    reservations = len(store.api_keys.reservations)

    refused = _authorize(
        client, key_hash, "too-long", estimated_input_tokens=150_000, max_tokens=60_000,
    )
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["type"] == "context_length_exceeded"
    assert error["param"] == "messages"
    assert error["message"].startswith("This model's maximum context length is 200000 tokens.")
    assert "(150000 in the messages, 60000 in the completion)" in error["message"]
    assert len(store.api_keys.reservations) == reservations


def test_a_custom_models_hidden_prompt_counts(ladder, monkeypatch):
    # The window check reads the prompt estimate the hold is reserved on,
    # which adds the custom model's hidden prompt to the gateway's count.
    monkeypatch.setattr(gateway, "estimate_tokens_from_text", lambda text: 5_000)
    client, key_hash = _client_and_key(ladder)
    custom = _create_custom_model(client, base_model_id=SMALL, hidden_prompt="policy")
    # Positive control: 4,000 tokens fit the 8k base model on their own.
    assert _models(_authorize(
        client, key_hash, "base", model=SMALL, estimated_input_tokens=4_000,
    )) == [SMALL]
    refused = _authorize(client, key_hash, "custom", model=custom["id"], estimated_input_tokens=4_000)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["type"] == "context_length_exceeded"
    assert "your messages resulted in 9000 tokens" in refused.json()["error"]["message"]


def test_a_pool_publishes_the_largest_window_it_routes_to(ladder, client):
    rows = {row["id"]: row for row in client.get("/v1/models").json()["data"]}
    assert rows[AUTO_MODEL_ID]["context_length"] == LARGE_WINDOW
    assert rows[AUTO_MODEL_ID]["top_provider"]["context_length"] == LARGE_WINDOW
    # The published size routes; one token more does not.
    assert _fit(AUTO_MODEL_ID, ladder, prompt=LARGE_WINDOW) == [LARGE]
    with pytest.raises(HTTPException):
        _fit(AUTO_MODEL_ID, ladder, prompt=LARGE_WINDOW + 1)


def test_orchestration_models_and_empty_pools_keep_their_declared_window():
    for model_id in ("trustedrouter/synth", "trustedrouter/prometheus"):
        model = catalog.MODELS[model_id]
        assert catalog.published_context_length(model) == model.context_length
    pool = catalog.MODELS[AUTO_MODEL_ID]
    empty = Settings(environment="test", auto_model_order="fixture/not-in-the-catalog")
    assert routing.routing_pool_context_length(AUTO_MODEL_ID, empty) == 0
    assert pool.context_length > 0


ROUTING_POOLS = sorted(
    model_id for model_id in catalog.META_MODEL_IDS
    if catalog.orchestration_role(model_id) == "routing_pool"
)


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("pool", ROUTING_POOLS)
def test_each_pool_publishes_a_window_routing_can_serve_and_no_more(pool):
    settings = Settings(environment="test")
    model = catalog.MODELS[pool]
    published = catalog.published_context_length(model)
    try:
        candidates = list(chat_route_endpoint_candidates({"model": pool}, settings))
    except HTTPException:
        assert published == model.context_length
        return
    windows = [endpoint.effective_context_length(candidate) for candidate, endpoint in candidates]
    assert published == max(windows)
    reached = candidates_fitting_context(candidates, prompt_tokens=published, completion_tokens=0)
    assert reached and all(
        endpoint.effective_context_length(candidate) <= 0
        or endpoint.effective_context_length(candidate) >= published
        for candidate, endpoint in reached
    )
    if all(window > 0 for window in windows):
        with pytest.raises(HTTPException):
            candidates_fitting_context(candidates, prompt_tokens=published + 1, completion_tokens=0)


@pytest.fixture
def different_host_windows(monkeypatch):
    model_id = "fixture/minimax-flash"
    _serve(monkeypatch, model_id, 1_048_576, "novita")
    _serve(monkeypatch, model_id, 1_048_576, "deepinfra")
    for endpoint in catalog.endpoints_for_model(model_id):
        window = 1_000_000 if endpoint.provider == "novita" else 1_048_576
        monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, replace(endpoint, context_length=window))
    return model_id, _settings(model_id)


@pytest.mark.parametrize("prompt,completion,hosts", [
    (999_999, 1, {"novita", "deepinfra"}),
    (1_000_000, 0, {"novita", "deepinfra"}),
    (1_000_000, 1, {"deepinfra"}),
    (1_048_575, 1, {"deepinfra"}),
])
def test_same_model_filters_each_hosts_window(different_host_windows, prompt, completion, hosts):
    model_id, settings = different_host_windows
    candidates = list(chat_route_endpoint_candidates({"model": model_id}, settings))
    fitted = candidates_fitting_context(candidates, prompt_tokens=prompt, completion_tokens=completion)
    assert {endpoint.provider for _, endpoint in fitted} == hosts


@pytest.mark.parametrize("route_type", ["chat.completions", "responses"])
def test_host_limit_applies_before_no_fallback_selection(different_host_windows, route_type):
    model_id, settings = different_host_windows
    client, key_hash = _client_and_key(settings)
    response = _authorize(
        client, key_hash, "host-window", model=model_id, route_type=route_type,
        estimated_input_tokens=1_000_000, max_tokens=1,
        provider={"order": ["novita", "deepinfra"], "allow_fallbacks": False},
    )
    assert response.status_code == 200, response.text
    candidates = response.json()["data"]["route_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["provider"] == "deepinfra"


def test_pinned_smaller_host_rejects_before_reservation(different_host_windows):
    model_id, settings = different_host_windows
    client, key_hash = _client_and_key(settings)
    store = cast(InMemoryStore, STORE)
    reservations = len(store.api_keys.reservations)
    response = _authorize(
        client, key_hash, "pinned-small", model=model_id,
        estimated_input_tokens=1_000_000, max_completion_tokens=1,
        provider={"only": ["novita"], "allow_fallbacks": False},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "context_length_exceeded"
    assert "1000000 tokens" in response.json()["error"]["message"]
    assert len(store.api_keys.reservations) == reservations


def test_endpoint_listing_reports_host_specific_windows(different_host_windows, client):
    model_id, _ = different_host_windows
    rows = client.get(f"/v1/models/{model_id}/endpoints").json()["data"]
    assert {row["provider"]: row["context_length"] for row in rows} == {
        "novita": 1_000_000, "deepinfra": 1_048_576,
    }
    assert catalog.MODELS[model_id].context_length == 1_048_576


@pytest.mark.parametrize("model_window,host_window,expected", [
    (1_048_576, 1_000_000, 1_000_000),
    (1_000_000, 1_048_576, 1_000_000),
    (1_048_576, 0, 1_048_576),
    (0, 1_000_000, 1_000_000),
    (0, 0, 0),
])
def test_effective_window_never_widens_known_limits(model_window, host_window, expected):
    from trusted_router.catalog_data import ModelEndpoint

    model = Model(id=SMALL, name=SMALL, provider="novita", context_length=model_window)
    endpoint = ModelEndpoint(id="fixture", model_id=SMALL, provider="novita", usage_type="Credits",
                             context_length=host_window)
    assert endpoint.effective_context_length(model) == expected
