"""Tests for scripts/check_price_coverage.py (price-source coverage audit)."""

from __future__ import annotations

import datetime as dt

from scripts import check_price_coverage
from scripts.check_price_coverage import audit


def _known_provider_model_payload(url: str, _env_names: tuple[str, ...]) -> dict:
    if "cerebras.ai" in url:
        return {"data": [{"id": "gpt-oss-120b"}]}
    if "generativelanguage.googleapis.com" in url:
        return {"models": [{"name": "models/gemini-3.5-flash"}]}
    if "api.minimax.io" in url:
        return {"data": [{"id": "MiniMax-M3"}]}
    if "api.fireworks.ai" in url:
        return {"data": [{"id": "accounts/fireworks/models/gpt-oss-120b"}]}
    if "tokenfactory.nebius.com" in url:
        return {"data": [{"id": "meta-llama/Llama-3.3-70B-Instruct"}]}
    if "api.novita.ai" in url:
        return {"data": [{"id": "deepseek/deepseek-v4-flash"}]}
    if "api.friendli.ai" in url:
        return {"data": [{"id": "meta-llama-3.3-70b-instruct"}]}
    if "inference.baseten.co" in url:
        return {"data": [{"id": "zai-org/GLM-5.2"}]}
    if "pass.wafer.ai" in url:
        return {"data": [{"id": "GLM-5.2"}]}
    return {"data": []}


def test_audit_flags_uncovered_provider_and_reports_covered() -> None:
    # Real repo state: Cohere is a prepaid provider with no scraper + no
    # manifest (hand-coded embedding prices) -> must be flagged as a gap.
    # If a Cohere scraper/manifest is ever added, update this expectation.
    now = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
    warnings, info = audit(max_age_days=14, now=now, check_model_discovery=False)
    assert any("cohere" in w for w in warnings), warnings
    # Live-scraped providers are reported as covered.
    assert any("openai" in i for i in info), info


def test_zai_model_discovery_extracts_glm_ids_from_docs() -> None:
    text = """
    The GLM Coding Plan now supports GLM-5.2.
    Use `ANTHROPIC_DEFAULT_OPUS_MODEL`: `glm-5.2[1m]`.
    Fallbacks: GLM-4.7 and GLM-4.5-Air.
    """

    assert check_price_coverage._discover_zai_coding_plan_models(text) == {
        "z-ai/glm-4.5-air",
        "z-ai/glm-4.7",
        "z-ai/glm-5.2",
    }


def test_provider_glm_model_discovery_normalizes_native_ids() -> None:
    payload = {
        "data": [
            {"id": "zai-org/GLM-5.2"},
            {"id": "accounts/fireworks/models/glm-5p2"},
            {"id": "zai-org/glm-5.1"},
            {"id": "not-a-glm-model"},
        ]
    }

    assert check_price_coverage._provider_glm_model_ids(payload) == {
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }


def test_provider_glm_required_gate_targets_current_flagships() -> None:
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.2")
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.3")
    assert check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-6")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5.1")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-5-turbo")
    assert not check_price_coverage._is_required_provider_glm_model_id("z-ai/glm-4.7-h")


def test_model_discovery_warns_when_docs_mention_unpublished_model() -> None:
    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2, GLM-4.7",
        fetch_json=_known_provider_model_payload,
        published_model_ids={"z-ai/glm-4.7"},
    )

    assert any(item.startswith("cerebras: model discovery matched catalog") for item in info)
    assert len(warnings) == 1
    assert "z-ai/glm-5.2" in warnings[0]


def test_model_discovery_reports_match_when_docs_models_are_published() -> None:
    warnings, info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2, GLM-4.7",
        fetch_json=_known_provider_model_payload,
        published_model_ids={"z-ai/glm-5.2", "z-ai/glm-4.7"},
    )

    assert warnings == []
    assert "zai: model discovery matched catalog (2 docs model(s)) ✓" in info
    assert any(item.startswith("minimax: model discovery matched catalog") for item in info)


def test_provider_model_discovery_warns_on_unpublished_manifest_model() -> None:
    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "api.minimax.io" in url:
            return {"data": [{"id": "MiniMax-M9"}]}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-5.2",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-5.2"},
    )

    assert any("minimax/minimax-m9" in warning for warning in warnings)


def test_provider_glm_discovery_warns_on_unpublished_route(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "deepinfra.com" in url:
            return {"data": [{"id": "zai-org/GLM-5.2"}]}
        if "fireworks.ai" in url:
            return {"data": [{"id": "accounts/fireworks/models/glm-5p2"}]}
        if "novita.ai" in url:
            return {"data": [{"id": "zai-org/glm-5.2"}]}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-4.7",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-4.7"},
    )

    assert any(
        "deepinfra: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )
    assert any(
        "fireworks: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )
    assert any(
        "novita: live GLM current model API lists unpublished model(s) z-ai/glm-5.2" in warning
        for warning in warnings
    )


def test_provider_glm_discovery_keeps_legacy_variants_visibility_only(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(check_price_coverage, "MANIFEST_DIR", tmp_path)

    def fake_fetch_json(url: str, _env_names: tuple[str, ...]) -> dict:
        if "novita.ai" in url:
            return {"data": [{"id": "zai-org/glm-4.7-h"}]}
        if "inference.baseten.co" in url or "pass.wafer.ai" in url:
            return {"data": []}
        return _known_provider_model_payload(url, _env_names)

    warnings, _info = check_price_coverage._model_discovery_audit(
        fetch_text=lambda _url: "Supported Models: GLM-4.7",
        fetch_json=fake_fetch_json,
        published_model_ids={"z-ai/glm-4.7"},
    )

    assert any(
        "novita: live GLM variant model API lists unpublished model(s) z-ai/glm-4.7-h" in warning
        for warning in warnings
    )
    assert not any("current model API" in warning for warning in warnings)


def test_strict_model_discovery_fails_glm_provider_warnings(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        warning = (
            "deepinfra: live GLM current model API lists unpublished model(s) "
            "z-ai/glm-5.3 — add/update provider_models/deepinfra.json"
        )
        return ([warning], ["openai: live scraper ✓"], [warning])

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    rc = check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])

    assert rc == 1
    assert "z-ai/glm-5.3" in capsys.readouterr().out


def test_strict_model_discovery_fails_only_discovery_warnings(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (
            [
                "cohere: NO price source",
                "zai: Coding Plan docs mention unpublished model(s) z-ai/glm-5.3",
            ],
            ["openai: live scraper ✓"],
            ["zai: Coding Plan docs mention unpublished model(s) z-ai/glm-5.3"],
        )

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    rc = check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])

    assert rc == 1
    assert "z-ai/glm-5.3" in capsys.readouterr().out


def test_strict_model_discovery_allows_visibility_only_price_warnings(
    monkeypatch,
) -> None:
    def fake_run_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (["cohere: NO price source"], ["openai: live scraper ✓"], [])

    monkeypatch.setattr(check_price_coverage, "_run_audit", fake_run_audit)

    assert (
        check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])
        == 0
    )


def test_strict_model_discovery_does_not_fail_provider_api_visibility_warning(
    monkeypatch,
) -> None:
    def fake_model_discovery_audit(*args, **kwargs):  # noqa: ANN001, ANN202
        return (
            ["novita: live model API lists unpublished model(s) test/model"],
            ["zai: model discovery matched catalog (1 docs model(s)) ✓"],
        )

    monkeypatch.setattr(
        check_price_coverage,
        "_model_discovery_audit",
        fake_model_discovery_audit,
    )

    assert (
        check_price_coverage.main(["--strict-model-discovery", "--now", "2026-06-14T00:00:00Z"])
        == 0
    )
