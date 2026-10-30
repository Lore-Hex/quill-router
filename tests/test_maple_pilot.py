import json
import shlex
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from tests.fixture_routes import bypass_catalog_caches
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.provider_contracts import MAPLE_MODEL_ID, MAPLE_NATIVE_ID
from trusted_router.provider_precision import endpoint_precision
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS


def test_maple_uses_only_the_measured_managed_adapter():
    provider = PROVIDERS["maple"]
    assert provider.supports_prepaid and not provider.supports_byok
    assert provider.attested_gateway and provider.provider_confidential_compute and provider.provider_e2ee
    assert provider.prepaid_zero_data_retention and not provider.provider_zero_data_retention
    assert "maple" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "maple" not in OPENAI_COMPATIBLE_PROVIDERS


@pytest.mark.parametrize("variant", ["approved", "stock", "other-model", "wrong-native", "missing-native", "byok"])
def test_only_exact_managed_maple_route_qualifies_for_confidential(variant):
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id=MAPLE_MODEL_ID, upstream_id=MAPLE_NATIVE_ID, usage_type="Credits")
    if variant == "stock":
        endpoint = replace(endpoint, model_id="deepseek/deepseek-v4.1-flash",
                           upstream_id="deepseek-v4-1-flash")
    elif variant == "other-model":
        endpoint = replace(endpoint, model_id="maple/future-model")
    elif variant == "wrong-native":
        endpoint = replace(endpoint, upstream_id="deepseek-v4-1-flash")
    elif variant == "missing-native":
        endpoint = replace(endpoint, upstream_id=None)
    elif variant == "byok":
        endpoint = replace(endpoint, usage_type="BYOK")
    for requirement in (PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION):
        assert endpoint_meets_privacy_requirement(endpoint, requirement) is (variant == "approved")


def test_maple_provider_page_explains_exact_scope(client):
    response = client.get("/providers/maple")
    assert response.status_code == 200
    for text in ("Maple", "Transport V2", "Unexpected measurements fail closed",
                 "other models or BYOK", "https://maple.ai/pricing"):
        assert text in response.text


@pytest.mark.parametrize("path", ["/providers/maple", f"/models/{MAPLE_MODEL_ID}"])
def test_maple_api_example_is_executable_and_requires_exact_confidential_route(client, path):
    response = client.get(path)
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    example = page.select_one("#maple-api-request")
    assert example is not None
    command = shlex.split(example.get_text().replace("\\\n", ""))
    assert command[:3] == ["curl", "--fail-with-body", "--no-buffer"]
    assert command[3] == "https://api.trustedrouter.com/v1/chat/completions"
    assert "Authorization: Bearer ${TRUSTEDROUTER_API_KEY:?Set your TrustedRouter API key}" in command
    assert "X-OpenRouter-Metadata: enabled" in command
    payload = json.loads(command[command.index("-d") + 1])
    assert payload == {
        "model": MAPLE_MODEL_ID,
        "messages": [{"role": "user", "content": "Explain a binary search in three sentences."}],
        "provider": {"only": ["maple"], "allow_fallbacks": False, "min_privacy": "confidential"},
        "max_tokens": 512,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert "MAPLE_API_KEY" not in example.get_text()
    assert "enclave.trymaple.ai" not in example.get_text()
    assert "--retry" not in command
    section = page.select_one("#maple-api")
    assert "Images, tools, structured-output schemas, and BYOK are not currently advertised" in section.get_text()
    assert "Gateway region is not the GPU location" in section.get_text()
    assert page.select_one('script[src^="/static/content-layout.js"]') is not None
    assert "maple-api-example" in example.parent["class"]


@pytest.mark.parametrize("path", ["/providers/maple", f"/models/{MAPLE_MODEL_ID}"])
def test_maple_api_example_is_hidden_when_managed_route_is_not_published(client, monkeypatch, path):
    for key, endpoint in list(MODEL_ENDPOINTS.items()):
        if endpoint.provider == "maple":
            monkeypatch.delitem(MODEL_ENDPOINTS, key)
    bypass_catalog_caches(monkeypatch)
    response = client.get(path)
    assert response.status_code == 200
    assert 'id="maple-api-request"' not in response.text


def test_maple_precision_preserves_mixed_weights_and_exact_scope(client):
    endpoint = next(e for e in MODEL_ENDPOINTS.values() if e.provider == "maple")
    precision = endpoint_precision(endpoint)
    assert precision is not None
    assert precision.weight_formats == ("fp8", "fp4")
    assert precision.kv_cache_dtype == "fp8_ds_mla"
    assert precision.model_revision == "c19ca5ca4eaeb79d29fdf08aea8327da95b1116a"
    assert precision.runtime_verified is False
    assert precision.evidence_type == "published_serving_config"
    assert endpoint_precision(replace(endpoint, upstream_id="deepseek-v4-1-flash")) is None
    assert endpoint_precision(replace(endpoint, provider="tinfoil")) is None
    response = client.get(f"/models/{MAPLE_MODEL_ID}")
    assert response.status_code == 200
    assert "FP8 + FP4 experts" in response.text
    assert precision.model_revision in response.text
