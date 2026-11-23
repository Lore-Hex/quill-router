from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import refresh
from scripts.pricing.providers import nscale
from trusted_router import provider_lifecycle, provider_manifest_policy

_CUTOFF = datetime(2026, 12, 11, tzinfo=UTC)
_CUTOFF_STAMP = "2026-12-11T00:00:00Z"
_RETIRING = {
    "qwen/qwen3.8-flash-next-fp8": "Qwen/Qwen3.8-Flash-Next-FP8",
    "qwen/qwen3-embedding-8b": "Qwen/Qwen3-Embedding-8B",
}
# Announced for the same date ("Moves to dedicated models"), but Nscale refused
# it on serverless from 2026-11-23 00:39 UTC while the survivors kept serving.
_EARLY_ID = "mistralai/mistral-large-3-675b-instruct-2512-nvfp4"
_EARLY_NATIVE_ID = "mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4"
_EARLY_CUTOFF = datetime(2026, 11, 23, 0, 39, tzinfo=UTC)
_EARLY_CUTOFF_STAMP = "2026-11-23T00:39:00Z"
_SURVIVING = {
    "z-ai/glm-5.3": "zai-org/GLM-5.3",
    "qwen/qwen3.8-27b": "Qwen/Qwen3.8-27B",
}
_FIXTURES = Path(__file__).parent / "fixtures/pricing"
_POST_DECEMBER = _FIXTURES / "nscale_models_post_cutover_2026-12-11.json"
_POST_NOVEMBER = _FIXTURES / "nscale_models_post_cutover_2026-11-07.json"


def _payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _at(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: when)


def _serve(monkeypatch: pytest.MonkeyPatch, manifest: Path, payload: dict[str, Any]) -> None:
    monkeypatch.setenv("NSCALE_API_KEY", "test-only-key")
    monkeypatch.setattr(nscale, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(nscale, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(nscale, "fetch_json", lambda url, *, extra_headers: payload)
    monkeypatch.setattr(nscale, "_probe", lambda api_key, model_id: True)


def _manifest_stage() -> tuple[list[str], list[tuple[str, str]]]:
    # The refresh's own manifest stage: the provider writer, then the outer
    # mass-prune gate that rolls the file back when it trips.
    return refresh._write_provider_manifests({nscale.SLUG: nscale.fetch()})


def _committed_copy(tmp_path: Path) -> Path:
    manifest = tmp_path / "nscale.json"
    manifest.write_text(nscale.MANIFEST_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return manifest


def _rows(manifest: Path) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]}


def _valid_until(manifest: Path) -> datetime:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    valid_until = provider_manifest_policy.provider_manifest_valid_until(nscale.SLUG, raw)
    assert valid_until is not None
    return valid_until


def test_nscale_december_cutoff_is_the_announced_date_at_midnight_utc() -> None:
    assert provider_lifecycle.NSCALE_DECEMBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize("model_id", sorted(_RETIRING))
def test_nscale_december_entry_names_no_replacement(model_id: str) -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "nscale" and model_id in entry.model_ids
    ]
    assert len(entries) == 1
    # No replacement is named, so none is quoted or substituted.
    assert entries[0].replacement_model_ids == ()
    assert entries[0].notice_id == f"nscale:{_CUTOFF.isoformat()}"


def test_nscale_december_survivors_keep_routing() -> None:
    for model_id, native_id in _SURVIVING.items():
        assert not provider_lifecycle.provider_model_retired(
            "nscale", model_id, native_id, at=_CUTOFF + timedelta(days=365),
        )


def test_nscale_manifest_retirement_dates_match_the_lifecycle() -> None:
    rows = _rows(nscale.MANIFEST_PATH)
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == _CUTOFF_STAMP
    assert rows[_EARLY_ID]["upstream_id"] == _EARLY_NATIVE_ID
    assert rows[_EARLY_ID]["retirement_at"] == _EARLY_CUTOFF_STAMP
    for model_id in _SURVIVING:
        assert "retirement_at" not in rows[model_id]
    # Every recorded retirement_at is exactly when routing stops.
    for model_id, row in rows.items():
        if "retirement_at" not in row:
            continue
        at = datetime.fromisoformat(row["retirement_at"].replace("Z", "+00:00"))
        retired = provider_lifecycle.provider_model_retired
        assert retired("nscale", model_id, row["upstream_id"], at=at), model_id
        assert not retired(
            "nscale", model_id, row["upstream_id"], at=at - timedelta(microseconds=1),
        ), model_id


def test_nscale_post_december_catalog_renews_and_tombstones_the_retired_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    manifest = _committed_copy(tmp_path)
    committed_valid_until = _valid_until(manifest)
    _at(monkeypatch, _CUTOFF + timedelta(hours=1))
    _serve(monkeypatch, manifest, _payload(_POST_DECEMBER))

    # The shared writer stamps a fresh miss, then tombstones on the next one:
    # the first two hourly refreshes after the cutover.
    for _ in range(2):
        notes, failures = _manifest_stage()
        assert failures == []
        assert not any("mass-prune" in note for note in notes)
        assert any(note.startswith("nscale: refreshed provider_models/nscale.json") for note in notes)

    assert _valid_until(manifest) > committed_valid_until
    rows = _rows(manifest)
    for model_id in _SURVIVING:
        assert rows[model_id].get("routable") is not False, model_id
        assert rows[model_id]["input_token_price_per_m"] > 0
    for model_id in _RETIRING:
        assert rows[model_id]["routable"] is False, model_id
        assert rows[model_id]["routable_reason"] == "delisted-upstream"
        assert rows[model_id]["missing_since"]
        assert rows[model_id]["retirement_at"] == _CUTOFF_STAMP
    assert rows[_EARLY_ID]["routable"] is False
    assert rows[_EARLY_ID]["retirement_at"] == _EARLY_CUTOFF_STAMP


def test_nscale_unannounced_single_delisting_above_the_floor_still_refreshes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Ordinary churn keeps the shared writer's two-miss path: three priced chat
    # models need no retirement record, even before any cutover.
    payload = _payload(_POST_NOVEMBER)
    payload["data"] = [row for row in payload["data"] if row["id"] != "Qwen/Qwen3.8-Flash-Next-FP8"]
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF - timedelta(days=7))
    _serve(monkeypatch, manifest, payload)

    notes, failures = _manifest_stage()

    assert failures == []
    assert not any("mass-prune" in note for note in notes)
    assert _rows(manifest)["qwen/qwen3.8-flash-next-fp8"]["missing_since"]


# Positive controls: each listing below must fail closed, before any write.


def _assert_fails_closed(manifest: Path, match: str) -> None:
    before = manifest.read_bytes()
    with pytest.raises(RuntimeError, match=match):
        nscale.fetch()
    assert manifest.read_bytes() == before


def test_nscale_post_december_listing_before_the_cutover_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Models dropped early have no retirement in effect yet.
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF - timedelta(hours=1))
    _serve(monkeypatch, manifest, _payload(_POST_DECEMBER))

    _assert_fails_closed(manifest, r"no longer prices \[.*'qwen/qwen3.8-flash-next-fp8'.*no retirement")


def test_nscale_post_december_listing_without_the_retirement_record_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The December lifecycle entry, not the smaller number, is what lets two publish.
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", tuple(
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.effective_at != _CUTOFF
    ))
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF + timedelta(hours=1))
    _serve(monkeypatch, manifest, _payload(_POST_DECEMBER))

    _assert_fails_closed(manifest, "no retirement in effect")


def test_nscale_listing_missing_an_unretired_survivor_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    payload = _payload(_POST_DECEMBER)
    payload["data"] = [
        row if row["id"] != "Qwen/Qwen3.8-27B" else {**row, "id": "ExampleOrg/Unannounced-Chat"}
        for row in payload["data"]
    ]
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF + timedelta(hours=1))
    _serve(monkeypatch, manifest, payload)

    _assert_fails_closed(manifest, r"no longer prices \['qwen/qwen3.8-27b'\]")


def test_nscale_one_model_listing_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    payload = _payload(_POST_DECEMBER)
    payload["data"] = payload["data"][:1]
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF + timedelta(hours=1))
    _serve(monkeypatch, manifest, payload)

    _assert_fails_closed(manifest, "fewer than 2 priced chat models")


def test_nscale_previous_floor_would_freeze_the_post_december_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The freeze this change prevents: a fixed floor of three fails every refresh.
    monkeypatch.setattr(nscale, "MIN_PRICED_CHAT_MODELS", 3)
    manifest = _committed_copy(tmp_path)
    _at(monkeypatch, _CUTOFF + timedelta(hours=1))
    _serve(monkeypatch, manifest, _payload(_POST_DECEMBER))

    _assert_fails_closed(manifest, "fewer than 3 priced chat models")


def test_nscale_december_entry_is_a_separate_notice_from_november() -> None:
    december = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "nscale" and entry.effective_at == _CUTOFF
    ]
    assert len(december) == 1
    assert december[0].model_ids == frozenset(_RETIRING)
    assert december[0].upstream_ids == frozenset(_RETIRING.values())
    assert december[0].notice_id != (
        f"nscale:{provider_lifecycle.NSCALE_NOVEMBER_2026_RETIREMENT_AT.isoformat()}"
    )


def test_nscale_mistral_large_3_retires_early_in_its_own_entry() -> None:
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "nscale" and _EARLY_ID in entry.model_ids
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert provider_lifecycle.NSCALE_MISTRAL_LARGE_3_RETIREMENT_AT == _EARLY_CUTOFF
    assert entry.effective_at == _EARLY_CUTOFF < _CUTOFF
    assert entry.model_ids == frozenset({_EARLY_ID})
    assert entry.upstream_ids == frozenset({_EARLY_NATIVE_ID})
    # No replacement is named, so none is quoted or substituted.
    assert entry.replacement_model_ids == ()
    assert entry.notice_id == f"nscale:{_EARLY_CUTOFF.isoformat()}"


def test_nscale_mistral_large_3_stops_at_the_first_refusal_and_siblings_keep_routing() -> None:
    retired = provider_lifecycle.provider_model_retired
    assert retired("nscale", _EARLY_ID, _EARLY_NATIVE_ID, at=_EARLY_CUTOFF)
    assert not retired(
        "nscale", _EARLY_ID, _EARLY_NATIVE_ID, at=_EARLY_CUTOFF - timedelta(microseconds=1),
    )
    # The December pair and the survivors are untouched by the early stop.
    for model_id, native_id in {**_RETIRING, **_SURVIVING}.items():
        assert not retired("nscale", model_id, native_id, at=_EARLY_CUTOFF), model_id
