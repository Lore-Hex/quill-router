"""ScaleDown task contracts: exact input billing, native canaries, no guessed APIs."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import scaledown
from scripts.pricing.refresh import PROVIDER_SLUGS
from tests.pinned_manifests import serve_manifest_rows
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
from trusted_router.catalog_ingest import _supplemental_provider_models_and_endpoints
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _customer_price, provider_manifest_price_profile_is_valid
from trusted_router.provider_contracts import provider_model_requires_exact_global_settlement
from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
from trusted_router.services.inference_errors import default_provider_secret_ref
from trusted_router.storage import STORE

PRICE = "## Pricing\nPublic API: $0.05 per 1M tokens\n- All four models included\n- Flat pricing, no tiers\n"
FAQ = "ScaleDown does not charge for output tokens. We exclusively charge for input tokens"
RESULTS = {
    "compress": {
        "input_tokens": 30,
        "successful": True,
        "results": {"success": True, "compressed_prompt": "Friday"},
    },
    "summarize": {"input_tokens": 83, "summary": "Acme will review feedback Friday."},
    "extract": {"input_tokens": 170, "entities": []},
    "classify": {"input_tokens": 394, "top_label": "billing"},
}


def _discovered_task_rows(monkeypatch, directory: Path) -> list[dict]:
    """ScaleDown's task rows as its hourly discovery writes them once every
    native canary passes (test_native_canaries_and_manifest checks the same
    pipeline)."""
    path = directory / "discovered" / "scaledown.json"
    path.parent.mkdir(parents=True)
    real_client = httpx.Client

    def handle(req):
        task = next(task for task, spec in scaledown.TASKS.items() if spec["path"] == req.url.path)
        return httpx.Response(200, json=RESULTS[task])

    with monkeypatch.context() as patch:
        patch.setenv("SCALEDOWN_API_KEY", "test-only")
        patch.setattr(scaledown, "fetch_html", lambda url: PRICE)
        patch.setattr(scaledown, "_website_code", lambda: FAQ)
        patch.setattr(scaledown, "MANIFEST_PATH", path)
        patch.setattr(scaledown, "_ROWS", scaledown._ROWS)
        patch.setattr(
            scaledown.httpx,
            "Client",
            lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
        )
        scaledown.write_provider_manifest(scaledown.fetch())
    return json.loads(path.read_text())["models"]


@pytest.fixture
def task_routes(monkeypatch, tmp_path) -> None:
    """ScaleDown's four task routes, built from the rows its discovery writes:
    the task contracts below hold whatever ScaleDown lists today."""
    serve_manifest_rows(
        monkeypatch, tmp_path, "scaledown", _discovered_task_rows(monkeypatch, tmp_path)
    )


def test_exact_input_only_price():
    assert scaledown._input_price(PRICE, FAQ) == ModelPrice(50_000, 0)
    assert scaledown._input_price(PRICE.replace("0.05", "0.055"), FAQ) == ModelPrice(55_000, 0)


@pytest.mark.parametrize(
    "source,faq",
    [
        ("", FAQ),
        (PRICE, ""),
        (PRICE.replace("Flat pricing, no tiers", "Tiered"), FAQ),
        (PRICE.replace("0.05", "0.0000001"), FAQ),
        (PRICE.replace("0.05", "NaN"), FAQ),
        (PRICE.replace("0.05", "0"), FAQ),
        (PRICE + "Public API: $0.10 per 1M tokens\n", FAQ),
    ],
)
def test_ambiguous_or_changed_billing_contract_fails_closed(source, faq):
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, faq)


@pytest.mark.parametrize("bad", [None, 0, -1, True, 1.5, "30", 1 << 40])
def test_bad_usage_is_not_a_healthy_canary(bad):
    for task, result in RESULTS.items():
        assert not scaledown._valid_result(task, {**result, "input_tokens": bad})


def test_native_canaries_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALEDOWN_API_KEY", "test-only")
    monkeypatch.setattr(scaledown, "fetch_html", lambda url: PRICE)
    monkeypatch.setattr(scaledown, "_website_code", lambda: FAQ)
    monkeypatch.setattr(scaledown, "MANIFEST_PATH", tmp_path / "scaledown.json")
    real_client = httpx.Client
    requests = []

    def handle(req):
        assert req.url.host == "api.scaledown.xyz"
        assert req.headers["x-api-key"] == "test-only"
        assert "authorization" not in req.headers
        task = next(task for task, spec in scaledown.TASKS.items() if spec["path"] == req.url.path)
        assert json.loads(req.content) == scaledown.TASKS[task]["input"]
        requests.append(task)
        return httpx.Response(200, json=RESULTS[task])

    monkeypatch.setattr(
        scaledown.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
    )
    result = scaledown.fetch()
    scaledown.write_provider_manifest(result)
    manifest = json.loads(scaledown.MANIFEST_PATH.read_text())
    assert len(requests) == 4 and len(manifest["models"]) == 4
    for row in manifest["models"]:
        assert row["output_token_price_per_m"] == 0
        assert row["input_token_price_per_m"] == 50_000
        assert row["routable"] is True
        assert row["documentation"]["example_input"]
        assert provider_manifest_price_profile_is_valid(row)


def test_failed_canary_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALEDOWN_API_KEY", "test-only")
    monkeypatch.setattr(scaledown, "fetch_html", lambda url: PRICE)
    monkeypatch.setattr(scaledown, "_website_code", lambda: FAQ)
    monkeypatch.setattr(scaledown, "MANIFEST_PATH", tmp_path / "scaledown.json")
    real_client = httpx.Client
    monkeypatch.setattr(
        scaledown.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(403, json={"error": "not authorized"})
            ),
            **kw,
        ),
    )
    scaledown.write_provider_manifest(scaledown.fetch())
    assert all(
        r["routable"] is False for r in json.loads(scaledown.MANIFEST_PATH.read_text())["models"]
    )


@pytest.mark.usefixtures("task_routes")
def test_task_health_probes_use_catalog_examples_and_skip_throughput():
    from trusted_router.synthetic.probes import _rotation_prompt, rotation_candidates
    from trusted_router.synthetic.throughput import throughput_candidates

    pool = rotation_candidates()
    assert set(pool["scaledown"]) == {f"scaledown/{task}" for task in scaledown.TASKS}
    for task, spec in scaledown.TASKS.items():
        assert json.loads(_rotation_prompt("scaledown", f"scaledown/{task}")) == spec["input"]
    assert "scaledown" not in rotation_candidates(include_input_only=False)
    assert all(provider != "scaledown" for provider, _ in throughput_candidates())


@pytest.mark.usefixtures("task_routes")
def test_task_models_cannot_be_selected_as_general_chat():
    from trusted_router.catalog import MODELS
    from trusted_router.routing_candidates import (
        InvalidAutoModelOrder,
        _is_regular_chat_model,
        validate_auto_model_order,
    )

    for task in scaledown.TASKS:
        model_id = f"scaledown/{task}"
        assert not _is_regular_chat_model(MODELS[model_id])
        with pytest.raises(InvalidAutoModelOrder, match="task"):
            validate_auto_model_order(model_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("task", list(scaledown.TASKS))
@pytest.mark.usefixtures("task_routes")
async def test_task_rotation_request_passes_native_input(task):
    from trusted_router.synthetic.probes import SyntheticTarget, provider_rotation_probe

    def handle(request):
        body = json.loads(request.content)
        assert body["provider"] == {"only": ["scaledown"]}
        assert json.loads(body["messages"][0]["content"]) == scaledown.TASKS[task]["input"]
        event = {"choices": [{"delta": {"content": json.dumps(RESULTS[task])}}]}
        return httpx.Response(200, content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sample = await provider_rotation_probe(
            client,
            SyntheticTarget("test", "https://api.example/v1", "us-central1"),
            monitor_region="us-central1",
            api_key="test-only",
            provider="scaledown",
            model=f"scaledown/{task}",
        )
    assert sample.status == "success"


def test_free_output_is_a_reviewed_contract_not_a_generic_loophole():
    row = {
        "id": "scaledown/extract",
        "input_token_price_per_m": 50_000,
        "output_token_price_per_m": 0,
    }
    assert provider_manifest_price_profile_is_valid(row)
    for change in (
        {"id": "unknown/free"},
        {"input_token_price_per_m": 0},
        {"output_token_price_per_m": 1},
        {"price_tiers": []},
        {"cached_input_token_price_per_m": 0},
    ):
        assert not provider_manifest_price_profile_is_valid({**row, **change})
    assert _customer_price(0) > 0  # Ordinary provider floor unchanged.


def test_provider_privacy_and_hourly_discovery_contracts():
    p = PROVIDERS["scaledown"]
    assert p.supports_prepaid and not p.supports_byok
    assert p.provider_zero_data_retention and not p.stores_content and not p.provider_e2ee
    assert "scaledown" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "scaledown" in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert "scaledown" in PROVIDER_SLUGS
    assert default_provider_secret_ref("scaledown") == "env://SCALEDOWN_API_KEY"
    root = Path(__file__).resolve().parents[1]
    assert (
        "SCALEDOWN_API_KEY:trustedrouter-scaledown-api-key"
        in (root / ".github/workflows/refresh-prices.yml").read_text()
    )


def test_live_manifest_publishes_four_credits_routes_with_free_output():
    # Each routable task row of the committed manifest becomes one Credits
    # route; a row the refresh tombstoned is simply not expected. The input
    # price a route bills is checked on the rows discovery writes
    # (test_task_routes_bill_the_listed_input_price_plus_markup); the price
    # ScaleDown lists today is test_scaledown_still_charges_its_input_price.
    raw = json.loads(scaledown.MANIFEST_PATH.read_text())
    routable = [row["id"] for row in raw["models"] if row.get("routable") is not False]
    models, endpoints = _supplemental_provider_models_and_endpoints()
    routes = [e for e in endpoints.values() if e.provider == "scaledown"]
    assert sorted(e.model_id for e in routes) == sorted(routable)
    for e in routes:
        assert e.usage_type == "Credits"
        assert e.completion_price_microdollars_per_million_tokens == 0
        assert e.price_tiers[0].completion_price_microdollars_per_million_tokens == 0
        assert models[e.model_id].documentation.example_input
        assert provider_model_requires_exact_global_settlement(e.provider, e.model_id)


@pytest.mark.usefixtures("task_routes")
def test_task_routes_bill_the_listed_input_price_plus_markup():
    # Discovery read $0.05 per million input tokens off the pricing page: each
    # task route bills that plus the 5.5% markup, and nothing for output.
    from trusted_router.catalog import MODEL_ENDPOINTS

    for task in scaledown.TASKS:
        route = MODEL_ENDPOINTS[f"scaledown/{task}@scaledown/prepaid"]
        assert route.prompt_price_microdollars_per_million_tokens == 52_750
        assert route.completion_price_microdollars_per_million_tokens == 0


@pytest.mark.provider_health
def test_scaledown_still_charges_its_input_price():
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    _models, endpoints = _supplemental_provider_models_and_endpoints()
    routes = [e for e in endpoints.values() if e.provider == "scaledown"]
    for e in routes:
        assert e.prompt_price_microdollars_per_million_tokens == _customer_price(50_000)


@pytest.mark.provider_health
def test_scaledown_serves_its_four_task_routes():
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    from trusted_router.catalog import MODEL_ENDPOINTS

    for task in scaledown.TASKS:
        assert f"scaledown/{task}@scaledown/prepaid" in MODEL_ENDPOINTS


@pytest.mark.usefixtures("task_routes")
def test_gateway_settles_provider_preprocessing_exactly_once(client):
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "scaledown-test@example.com"},
        json={"name": "task billing"},
    )
    assert created.status_code == 201, created.text
    key = created.json()["data"]
    auth = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "scaledown/classify",
            "estimated_input_tokens": 10,
            "max_output_tokens": 64,
        },
    )
    assert auth.status_code == 200, auth.text
    authorization = auth.json()["data"]
    assert authorization["provider"] == "scaledown"
    payload = {
        "authorization_id": authorization["authorization_id"],
        "actual_input_tokens": 394,
        "actual_output_tokens": 0,
        "request_id": "scaledown-billing-test",
        "elapsed_seconds": 0.5,
    }
    first = client.post("/v1/internal/gateway/settle", json=payload)
    assert first.status_code == 200, first.text
    expected = token_cost_microdollars(394, _customer_price(50_000))
    assert first.json()["data"]["cost_microdollars"] == expected
    generation = STORE.get_generation(first.json()["data"]["generation_id"])
    assert generation.tokens_prompt == 394 and generation.tokens_completion == 0
    second = client.post("/v1/internal/gateway/settle", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["generation_id"] == generation.id
    assert second.json()["data"]["already_settled"]


@pytest.mark.usefixtures("task_routes")
def test_public_pages_and_usage_examples(client):
    assert client.get("/providers/scaledown").status_code == 200
    for task in scaledown.TASKS:
        page = client.get(f"/models/scaledown/{task}")
        assert page.status_code == 200
        assert "input-only" in page.text.lower()


@pytest.mark.parametrize("task", scaledown.TASKS)
@pytest.mark.usefixtures("task_routes")
def test_free_output_is_visible_in_all_model_prices(task, monkeypatch):
    from trusted_router.catalog import MODELS
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.dashboard import _model_detail_view, _model_route_evidence, _model_view

    # This checks price formatting, not freshness: the hourly refresh's
    # validation run must not fail because this manifest reached its deadline.
    monkeypatch.setattr(ModelEndpoint, "catalog_is_current", lambda _self, *, at=None: True)
    model = MODELS[f"scaledown/{task}"]
    listing = _model_view(model, test_mode=True)
    detail = _model_detail_view(model, test_mode=True)
    evidence = _model_route_evidence(model, test_mode=True)
    assert listing["completion_price"] == "$0/1M"
    assert listing["completion_price_sort"] == 0
    assert detail["completion_price"] == "$0/1M"
    assert all(endpoint["completion_price"] == "$0/1M" for endpoint in detail["endpoints"])
    assert evidence["lowest_completion_price"] == "$0/1M"


@pytest.mark.usefixtures("task_routes")
def test_unknown_zero_prices_still_mean_selected_route():
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.dashboard import _endpoint_price_range, _price

    # Formatting needs any real endpoint, current or not: the hourly refresh's
    # validation run must not fail because this manifest reached its deadline.
    endpoint = next(e for e in MODEL_ENDPOINTS.values() if e.model_id == "scaledown/compress")
    attr = "completion_price_microdollars_per_million_tokens"
    assert _price(0) == "selected route"
    assert _price(-1, include_zero=True) == "selected route"
    assert _endpoint_price_range((replace(endpoint, provider="unknown"),), attr) == "selected route"
    assert _endpoint_price_range((replace(endpoint, model_id="unknown"),), attr) == "selected route"
    assert (
        _endpoint_price_range(
            (
                endpoint,
                replace(endpoint, completion_price_microdollars_per_million_tokens=1_000_000),
            ),
            attr,
        )
        == "$0/1M to $1/1M"
    )
