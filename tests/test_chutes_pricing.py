from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import chutes

GLM = "zai-org/GLM-5.2-TEE"
KIMI = "moonshotai/Kimi-K2.6-TEE"
QWEN = "Qwen/Qwen3-235B-A22B-Thinking-2507-TEE"


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHUTES_API_KEY", "test-catalog-key")
    monkeypatch.setattr(chutes, "UPSTREAM_ID_MAP", dict(chutes.UPSTREAM_ID_MAP))
    monkeypatch.setattr(chutes, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(chutes, "_OPERATOR_HOLD_REASONS", dict(chutes._OPERATOR_HOLD_REASONS))


def _row(native_id: object, **updates: Any) -> dict[str, Any]:
    return {
        "id": native_id,
        "confidential_compute": True,
        "context_length": 262_144,
        "max_output_length": 65_536,
        "pricing": {"prompt": "0.24", "completion": "2.2", "input_cache_read": "0.024"},
        **updates,
    }


def _catalog(*rows: object) -> dict[str, Any]:
    return {"data": [_row(GLM), _row(KIMI), *rows]}


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=payload, headers=headers)

    monkeypatch.setattr(chutes.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(respond))
    return requests


def _manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "chutes.json"
    monkeypatch.setattr(chutes, "MANIFEST_PATH", path)
    return path


def _manifest_rows(path: Path) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in json.loads(path.read_text())["models"]}


@pytest.mark.parametrize(
    ("native_id", "model_id"),
    [
        ("Qwen/Qwen3.8-27B-TEE", "qwen/qwen3.8-27b"),
        ("moonshotai/Kimi-K3-TEE", "moonshotai/kimi-k3"),
        ("deepseek-ai/DeepSeek-V4-Flash-0731-TEE", "deepseek/deepseek-v4-flash-0731"),
        ("Nemotron-3-Nano-Omni-30B-TEE", "nvidia/nemotron-3-nano-omni"),
        ("FutureVendor/Novel-42B-Preview-Turbo-TEE", "futurevendor/novel-42b-preview-turbo"),
        ("FutureVendor/Novel-TEE-Preview-TEE", "futurevendor/novel-tee-preview"),
    ],
)
def test_fetch_discovers_new_models_with_exact_native_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_id: str, model_id: str,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    requests = _serve(
        monkeypatch,
        _catalog(_row(
            native_id,
            root="other-vendor/not-the-request-id",
            input_modalities=["image", "text"],
            output_modalities=["text"],
            supported_features=["tools", "reasoning"],
        )),
    )

    result = chutes.fetch()
    assert result.prices[model_id] == ModelPrice(
        240_000, 2_200_000, prompt_cached_micro_per_m=24_000,
    )
    assert chutes.UPSTREAM_ID_MAP[model_id] == native_id
    assert len(requests) == 1
    assert str(requests[0].url) == chutes.URL
    assert requests[0].headers["Authorization"] == "Bearer test-catalog-key"
    assert requests[0].headers["Accept"] == "application/json"
    chutes.write_provider_manifest(result)
    row = _manifest_rows(path)[model_id]
    assert row["upstream_id"] == native_id
    assert row["display_name"] == native_id.removesuffix("-TEE")
    assert row["confidential_compute"] is True
    assert row["context_length"] == 262_144
    assert row["max_output_tokens"] == 65_536
    assert row["input_modalities"] == ["text", "image"]
    assert row["supported_features"] == ["tools", "reasoning"]


def test_explicit_aliases_are_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"data": [_row(native) for native in chutes._NATIVE_TO_MODEL_ID]})
    result = chutes.fetch()
    assert set(result.prices) == set(chutes._NATIVE_TO_MODEL_ID.values())
    for native, canonical in chutes._NATIVE_TO_MODEL_ID.items():
        assert chutes._DISCOVERED_MANIFEST_ROWS[canonical]["upstream_id"] == native
        assert chutes.UPSTREAM_ID_MAP[canonical] == native
    assert "mistralai/mistral-nemo" in result.prices
    assert "unsloth/mistral-nemo-instruct-2407" not in result.prices


@pytest.mark.parametrize("flag", [False, None, 0, 1, "true", "false"])
def test_non_confidential_rows_cannot_overwrite_tee_prices_or_upstream(
    monkeypatch: pytest.MonkeyPatch, flag: object,
) -> None:
    tee = "FutureVendor/Novel-42B-TEE"
    canonical = "futurevendor/novel-42b"
    _serve(monkeypatch, _catalog(
        _row(tee),
        _row(tee, confidential_compute=flag, pricing={"prompt": "999", "completion": "999"}),
        _row(tee.removesuffix("-TEE"), confidential_compute=flag),
        _row("FutureVendor/Plaintext-TEE", confidential_compute=flag),
    ))
    result = chutes.fetch()
    assert result.prices[canonical].prompt_micro_per_m == 240_000
    assert chutes.UPSTREAM_ID_MAP[canonical] == tee
    assert chutes._DISCOVERED_MANIFEST_ROWS[canonical]["upstream_id"] == tee
    assert "futurevendor/plaintext" not in result.prices
    assert "futurevendor/plaintext" not in chutes._DISCOVERED_MANIFEST_ROWS


def test_missing_confidential_flag_is_not_inferred_from_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row("FutureVendor/Unverified-TEE")
    del row["confidential_compute"]
    _serve(monkeypatch, _catalog(row))
    assert "futurevendor/unverified" not in chutes.fetch().prices
    assert "futurevendor/unverified" not in chutes._DISCOVERED_MANIFEST_ROWS


@pytest.mark.parametrize("native_id", [
    None, 42, "", "opaque-uuid-TEE", "UnknownModel-TEE", "Qwen4-Unknown-TEE",
    "/MissingAuthor-TEE", "Vendor/-TEE", "Vendor/Nested/Model-TEE",
    "https://vendor.test/Model-TEE", " Vendor/Model-TEE", "Vendor/Model-TEE ",
])
def test_opaque_or_malformed_ids_do_not_invent_namespaces(
    monkeypatch: pytest.MonkeyPatch, native_id: object,
) -> None:
    _serve(monkeypatch, _catalog(_row(native_id), None, "not-a-row"))
    assert set(chutes.fetch().prices) == {"z-ai/glm-5.2", "moonshotai/kimi-k2.6"}
    assert set(chutes._DISCOVERED_MANIFEST_ROWS) == {"z-ai/glm-5.2", "moonshotai/kimi-k2.6"}


@pytest.mark.parametrize(("value", "expected"), [
    ("0", 0), ("0.0000005", 1), ("0.00000049", 0),
    ("0.0245", 24_500), ("0.023999999999999994", 24_000),
    ("NaN", None), ("sNaN", None), ("Infinity", None), ("-Infinity", None),
    (float("nan"), None), (float("inf"), None), ("1e999999999", None),
    ("-1", None), (None, None), (True, None), ({}, None), ("bad-price", None),
])
def test_money_is_finite_nonnegative_decimal(value: object, expected: int | None) -> None:
    assert chutes._dollars_per_m_to_micro_per_m(value) == expected


@pytest.mark.parametrize("field", ["prompt", "completion", "input_cache_read"])
@pytest.mark.parametrize("bad_price", ["NaN", "sNaN", "Infinity", "-Infinity", "-1", None])
def test_bad_prices_do_not_poison_other_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, bad_price: object,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    row = _row("FutureVendor/Novel-42B-TEE")
    row["pricing"][field] = bad_price
    _serve(monkeypatch, _catalog(row))
    result = chutes.fetch()
    model_id = "futurevendor/novel-42b"
    assert "z-ai/glm-5.2" in result.prices
    assert model_id in chutes._DISCOVERED_MANIFEST_ROWS
    chutes.write_provider_manifest(result)
    published = _manifest_rows(path)[model_id]
    if field == "input_cache_read":
        assert result.prices[model_id].tiers[0].prompt_cached_micro_per_m is None
        assert "cached_input_token_price_per_m" not in published
    else:
        assert model_id not in result.prices
        assert published["routable"] is False
        assert published["routable_reason"] == "awaiting-price"
        assert "input_token_price_per_m" not in published


def test_each_fetch_replaces_discovery_and_exact_upstream_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _catalog(_row("FutureVendor/Old-TEE"), _row("Qwen/Qwen3-32B-TEE"))
    _serve(monkeypatch, payload)
    chutes.fetch()
    assert "futurevendor/old" in chutes.UPSTREAM_ID_MAP
    payload["data"] = _catalog(_row("FutureVendor/New-TEE"), _row("qwen/qwen3-32b-TEE"))["data"]
    result = chutes.fetch()
    assert "futurevendor/old" not in result.prices
    assert "futurevendor/old" not in chutes._DISCOVERED_MANIFEST_ROWS
    assert "futurevendor/old" not in chutes.UPSTREAM_ID_MAP
    assert chutes.UPSTREAM_ID_MAP["qwen/qwen3-32b"] == "qwen/qwen3-32b-TEE"
    assert chutes._DISCOVERED_MANIFEST_ROWS["qwen/qwen3-32b"]["upstream_id"] == "qwen/qwen3-32b-TEE"


@pytest.mark.parametrize("failure", ["key", "http", "network", "json", "shape", "validation"])
def test_failed_fetch_cannot_leave_previous_or_partial_discovery(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _serve(monkeypatch, _catalog(_row("FutureVendor/Old-TEE")))
    chutes.fetch()
    if failure == "key":
        monkeypatch.delenv("CHUTES_API_KEY")
    elif failure == "http":
        _serve(monkeypatch, {}, status=503)
    elif failure == "network":
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("test connection failure", request=request)
        monkeypatch.setattr(chutes.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(fail))
    elif failure == "json":
        monkeypatch.setattr(chutes.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-json"),
        ))
    elif failure == "shape":
        _serve(monkeypatch, {"data": "not-a-list"})
    else:
        _serve(monkeypatch, {"data": [_row("FutureVendor/Partial-TEE", pricing=None)]})
    with pytest.raises((RuntimeError, httpx.HTTPError, ValueError)):
        chutes.fetch()
    assert chutes._DISCOVERED_MANIFEST_ROWS == {}
    assert chutes.UPSTREAM_ID_MAP == {
        canonical: native for native, canonical in chutes._NATIVE_TO_MODEL_ID.items()
    }


@pytest.mark.parametrize("location", [
    "https://other.test/models", "http://llm.chutes.ai/v1/models", "/redirected-models",
])
def test_authenticated_fetch_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch, location: str,
) -> None:
    requests = _serve(monkeypatch, {}, status=307, headers={"Location": location})
    with pytest.raises(httpx.HTTPStatusError):
        chutes.fetch()
    assert len(requests) == 1
    assert str(requests[0].url) == chutes.URL
    assert chutes._DISCOVERED_MANIFEST_ROWS == {}


@pytest.mark.parametrize("reason", [None, "tee-runtime-version-unsupported", "provider-canary-failed"])
def test_refresh_preserves_existing_safety_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str | None,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    native_id = "Qwen/Qwen3-32B-TEE"
    model_id = chutes._NATIVE_TO_MODEL_ID[native_id]
    held = {"id": model_id, "upstream_id": native_id, "routable": False, "operator_note": "keep held"}
    if reason is not None:
        held["routable_reason"] = reason
    path.write_text(json.dumps({"models": [held]}))
    _serve(monkeypatch, _catalog(_row(native_id)))
    chutes.write_provider_manifest(chutes.fetch())
    row = _manifest_rows(path)[model_id]
    assert row["routable"] is False
    assert row.get("routable_reason") == reason
    assert row["operator_note"] == "keep held"
    assert row["upstream_id"] == native_id
    if reason is None:
        assert row == held


def test_two_fresh_misses_delist_and_relist_preserves_operator_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    payload = _catalog(_row(QWEN), _row("FutureVendor/Steady-TEE"))
    _serve(monkeypatch, payload)
    chutes.write_provider_manifest(chutes.fetch())
    payload["data"] = [_row(GLM), _row(QWEN), _row("FutureVendor/Steady-TEE")]
    kimi_id = chutes._NATIVE_TO_MODEL_ID[KIMI]
    chutes.write_provider_manifest(chutes.fetch())
    first = _manifest_rows(path)[kimi_id]
    assert first["missing_since"]
    assert first.get("routable") is not False
    chutes.write_provider_manifest(chutes.fetch())
    assert _manifest_rows(path)[kimi_id]["routable_reason"] == "delisted-upstream"

    payload["data"].append(_row(KIMI))
    chutes.write_provider_manifest(chutes.fetch())
    recovered = _manifest_rows(path)[kimi_id]
    assert recovered["routable"] is True
    assert "missing_since" not in recovered
    assert "routable_reason" not in recovered

    monkeypatch.setattr(chutes, "_OPERATOR_HOLD_REASONS", {kimi_id: "tee-runtime-version-unsupported"})
    chutes.write_provider_manifest(chutes.fetch())
    payload["data"].pop()
    for _ in range(2):
        chutes.write_provider_manifest(chutes.fetch())
    payload["data"].append(_row(KIMI))
    chutes.write_provider_manifest(chutes.fetch())
    held = _manifest_rows(path)[kimi_id]
    assert held["routable"] is False
    assert held["routable_reason"] == "tee-runtime-version-unsupported"
    assert "missing_since" not in held


def test_invalid_price_removes_old_price_but_does_not_mark_model_delisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    native_id = "Qwen/Qwen3-32B-TEE"
    payload = _catalog(_row(native_id))
    _serve(monkeypatch, payload)
    chutes.write_provider_manifest(chutes.fetch())
    payload["data"][-1]["pricing"]["prompt"] = "NaN"
    for _ in range(2):
        chutes.write_provider_manifest(chutes.fetch())
    model_id = chutes._NATIVE_TO_MODEL_ID[native_id]
    held = _manifest_rows(path)[model_id]
    assert held["routable"] is False
    assert held["routable_reason"] == "price-unavailable"
    assert "missing_since" not in held
    assert "input_token_price_per_m" not in held
    payload["data"][-1]["pricing"]["prompt"] = "0.24"
    chutes.write_provider_manifest(chutes.fetch())
    assert _manifest_rows(path)[model_id].get("routable") is not False


def test_explicit_runtime_hold_applies_even_when_mass_prune_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = _manifest(monkeypatch, tmp_path)
    payload = _catalog(_row(QWEN))
    _serve(monkeypatch, payload)
    chutes.write_provider_manifest(chutes.fetch())
    payload["data"] = [_row(QWEN)]
    chutes.write_provider_manifest(chutes.fetch())
    model_id = chutes._NATIVE_TO_MODEL_ID[QWEN]
    monkeypatch.setattr(chutes, "_OPERATOR_HOLD_REASONS", {model_id: "tee-runtime-version-unsupported"})
    notes = chutes.write_provider_manifest(chutes.fetch())
    rows = _manifest_rows(path)
    assert rows[model_id]["routable"] is False
    assert rows[model_id]["routable_reason"] == "tee-runtime-version-unsupported"
    assert rows["z-ai/glm-5.2"].get("routable") is not False
    assert any("mass-prune guard" in note for note in notes)


def test_confirmed_qwen_evidence_failure_is_a_persistent_operator_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    model_id = chutes._NATIVE_TO_MODEL_ID[QWEN]
    reason = "attestation-evidence-unavailable"
    assert chutes._OPERATOR_HOLD_REASONS[model_id] == reason
    committed = _manifest_rows(chutes.MANIFEST_PATH)[model_id]
    assert committed["routable"] is False
    assert committed["routable_reason"] == reason
    path = _manifest(monkeypatch, tmp_path)
    path.write_text(json.dumps({"models": [{
        "id": model_id,
        "routable": False,
        "routable_reason": "delisted-upstream",
        "missing_since": "2026-10-01",
    }]}))
    payload = _catalog(_row(QWEN))
    _serve(monkeypatch, payload)
    for _ in range(2):
        chutes.write_provider_manifest(chutes.fetch())
        held = _manifest_rows(path)[model_id]
        assert held["routable"] is False
        assert held["routable_reason"] == reason
        assert held["upstream_id"] == QWEN
    payload["data"].pop()
    for _ in range(2):
        chutes.write_provider_manifest(chutes.fetch())
    payload["data"].append(_row(QWEN))
    chutes.write_provider_manifest(chutes.fetch())
    held = _manifest_rows(path)[model_id]
    assert held["routable"] is False
    assert held["routable_reason"] == reason


def test_operator_held_prices_cannot_reappear_in_the_shared_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.pricing.refresh import _index_provider_prices

    _serve(monkeypatch, _catalog(_row(QWEN)))
    result = chutes.fetch()
    model_id = chutes._NATIVE_TO_MODEL_ID[QWEN]
    assert model_id in result.prices
    assert model_id not in _index_provider_prices({"chutes": result})
    assert set(_index_provider_prices({"chutes": result})) == {"z-ai/glm-5.2", "moonshotai/kimi-k2.6"}


def test_committed_chutes_snapshot_routes_match_their_manifest_model_and_price() -> None:
    rows = _manifest_rows(chutes.MANIFEST_PATH)
    snapshot_path = chutes.MANIFEST_PATH.parent.parent / "openrouter_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    seen = set()
    for model in snapshot["models"]:
        for endpoint in model.get("endpoints", []):
            if endpoint.get("tr_provider_slug") != "chutes":
                continue
            row = rows[model["id"]]
            assert row.get("routable") is not False
            assert row["confidential_compute"] is True
            assert endpoint["model_id"] == row["upstream_id"]
            assert chutes._canonical_model_id(endpoint["model_id"]) == model["id"]
            for price_field, manifest_field in (
                ("prompt", "input_token_price_per_m"),
                ("completion", "output_token_price_per_m"),
                ("input_cache_read", "cached_input_token_price_per_m"),
            ):
                assert Decimal(endpoint["pricing"][price_field]) * 10**12 == row[manifest_field]
            seen.add(model["id"])
    assert seen
    assert not seen.intersection(chutes._OPERATOR_HOLD_REASONS)


def test_committed_chutes_catalog_excludes_holds_for_credits_and_byok() -> None:
    from tests import catalog_vehicles

    rows = _manifest_rows(chutes.MANIFEST_PATH)
    endpoints = [
        endpoint for endpoint in catalog_vehicles.registry_endpoints().values()
        if endpoint.provider == "chutes"
    ]
    assert endpoints
    for endpoint in endpoints:
        row = rows[endpoint.model_id]
        assert row.get("routable") is not False
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.model_id not in chutes._OPERATOR_HOLD_REASONS
