"""Committed Featherless routes must outlive the bounded discovery window."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import pytest

from scripts.pricing.providers import featherless

AGED_OUT_NATIVE = "Qwen/Qwen3.5-9B"
AGED_OUT_MODEL = "qwen/qwen3.5-9b"


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    committed = json.loads(featherless.MANIFEST_PATH.read_text())
    manifest = tmp_path / "featherless.json"
    manifest.write_text(json.dumps(committed))
    # Only curated models remain in the recent-model listing. All other
    # committed routes must be checked through authenticated model details.
    details = {
        row["upstream_id"]: {
            "id": row["upstream_id"],
            "available_on_current_plan": True,
            "context_length": 32_768,
            "pricing": {"prompt": "0.00000017", "completion": "0.00000025"},
        }
        for row in committed["models"]
        if row.get("routable", True)
    }
    listing = [deepcopy(details[native]) for native in featherless.CURATED_NATIVE_MODELS]
    requests = []

    def api(url, *, extra_headers):
        assert extra_headers == {"Authorization": "Bearer test-key"}
        requests.append(url)
        if url.startswith(featherless.URL + "?"):
            return {"data": listing, "total": 22_185}
        payload = details[unquote(url.removeprefix(featherless.URL + "/"))]
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.txt"))
    monkeypatch.setattr(featherless, "fetch_json", api)
    html = (Path(__file__).parent / "fixtures/pricing/featherless_request_pricing_2026-10-11.html")
    monkeypatch.setattr(featherless, "fetch_html", lambda _url: html.read_text())
    monkeypatch.setattr(featherless.CATALOG, "manifest_path", manifest)
    monkeypatch.setattr(featherless.CATALOG, "upstream_id_map", {})
    monkeypatch.setattr(featherless.CATALOG, "discovered_rows", {})
    monkeypatch.setattr(featherless.CATALOG, "_fetched", False)
    return details, listing, requests, manifest


def test_fetch_keeps_committed_models_outside_discovery_window(catalog):
    details, _, requests, manifest = catalog
    result = featherless.fetch()

    expected = {
        row["id"] for row in json.loads(manifest.read_text())["models"]
        if row.get("routable", True)
    }
    assert set(result.prices) == expected
    price = result.prices[AGED_OUT_MODEL]
    assert price.prompt_micro_per_m == 170_000
    assert price.completion_micro_per_m == 250_000
    assert price.tiers[0].prompt_cached_micro_per_m is None
    # Details are fetched once even for curated/listed manifest rows, and
    # explicitly unroutable manifest rows do not trigger detail lookups.
    assert Counter(requests[1:]) == Counter(
        f"{featherless.URL}/{quote(native, safe='')}" for native in details
    )
    featherless.write_provider_manifest(result)
    assert all(
        "missing_since" not in row and "unresolved_since" not in row
        for row in json.loads(manifest.read_text())["models"] if row["id"] in expected
    )


@pytest.mark.parametrize("missing", [False, True], ids=["unavailable", "404"])
def test_unavailable_committed_detail_follows_normal_delisting(catalog, missing):
    details, listing, _, manifest = catalog
    # The committed aged-out row may already have a miss from the old loader.
    # Start without that marker to exercise both steps of normal delisting.
    committed = json.loads(manifest.read_text())
    for row in committed["models"]:
        if row["id"] == AGED_OUT_MODEL:
            row.pop("missing_since", None)
    manifest.write_text(json.dumps(committed))
    # A stale listing must not override an unavailable/missing model detail.
    listing.append(deepcopy(details[AGED_OUT_NATIVE]))
    if missing:
        response = httpx.Response(404, request=httpx.Request("GET", featherless.URL))
        details[AGED_OUT_NATIVE] = httpx.HTTPStatusError(
            "missing model", request=response.request, response=response,
        )
    else:
        details[AGED_OUT_NATIVE]["available_on_current_plan"] = False

    for miss in (1, 2):
        result = featherless.fetch()
        assert AGED_OUT_MODEL not in result.prices
        assert AGED_OUT_MODEL not in featherless.CATALOG.discovered_rows
        featherless.write_provider_manifest(result)
        row = next(
            row for row in json.loads(manifest.read_text())["models"]
            if row["id"] == AGED_OUT_MODEL
        )
        assert row["missing_since"]
        if miss == 1:
            assert row.get("routable", True)
        else:
            assert row["routable"] is False
            assert row["routable_reason"] == "delisted-upstream"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_committed_detail_request_failure_holds_provider(catalog, status):
    details, _, _, _ = catalog
    response = httpx.Response(status, request=httpx.Request("GET", featherless.URL))
    details[AGED_OUT_NATIVE] = httpx.HTTPStatusError(
        "detail request failed", request=response.request, response=response,
    )
    with pytest.raises(httpx.HTTPStatusError):
        featherless.fetch()


def test_malformed_committed_detail_holds_provider(catalog):
    details, _, _, _ = catalog
    details[AGED_OUT_NATIVE] = []
    with pytest.raises(RuntimeError, match="invalid model detail"):
        featherless.fetch()
