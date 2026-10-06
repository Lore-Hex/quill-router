from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from trusted_router import benchmark_scores as bm
from trusted_router.benchmark_scores import (
    BENCHMARK_DEFS,
    models_with_scores,
    scores_for_model,
)
from trusted_router.catalog import META_MODEL_IDS, MODELS
from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.config import Settings
from trusted_router.main import create_app


def _settings() -> Settings:
    return Settings(
        environment="test",
        sentry_dsn=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        google_client_id=None,
        google_client_secret=None,
        google_oauth_redirect_url=None,
        github_client_id=None,
        github_client_secret=None,
        github_oauth_redirect_url=None,
    )


def test_scores_for_known_model_are_sourced_and_sorted() -> None:
    rows = scores_for_model("anthropic/claude-sonnet-4.5")
    assert rows, "expected seeded scores for claude-sonnet-4.5"
    swe = next(r for r in rows if r["label"] == "SWE-bench Verified")
    assert swe["display"] == "77.2%"
    assert swe["source_url"].startswith("https://www.anthropic.com/")
    assert swe["config_note"]  # checkpoint config surfaced
    categories = [r["category"] for r in rows]
    assert categories == sorted(categories)


def test_scores_filtering_drops_class_c_missing_url_and_unknown_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bm,
        "_raw_scores",
        lambda: [
            # class C (ToS-restricted aggregator) — must be dropped.
            {"model_id": "m/x", "benchmark_key": "mmlu", "score": 90, "unit": "percent",
             "source_name": "AA", "source_url": "https://artificialanalysis.ai", "source_class": "C"},
            # missing source_url — must be dropped.
            {"model_id": "m/x", "benchmark_key": "mmlu", "score": 90, "unit": "percent",
             "source_name": "x", "source_url": "", "source_class": "A"},
            # unknown benchmark key — must be dropped.
            {"model_id": "m/x", "benchmark_key": "made_up", "score": 90, "unit": "percent",
             "source_name": "x", "source_url": "https://x", "source_class": "A"},
            # valid.
            {"model_id": "m/x", "benchmark_key": "mmlu", "score": 88.0, "unit": "percent",
             "source_name": "Vendor", "source_url": "https://vendor.example", "source_class": "A"},
        ],
    )
    rows = scores_for_model("m/x")
    assert len(rows) == 1
    assert rows[0]["label"] == "MMLU"
    assert rows[0]["display"] == "88.0%"


def _manifest_model_ids() -> set[str]:
    # The price refresh tombstones a delisted row; it never deletes one.
    ids: set[str] = set()
    for path in _PROVIDER_MODELS_DIR.glob("*.json"):
        rows = json.loads(path.read_text(encoding="utf-8")).get("models", [])
        ids.update(row["id"] for row in rows if isinstance(row, dict) and row.get("id"))
    return ids


def test_shipped_benchmark_data_integrity() -> None:
    # Guards against a bad future edit shipping a fabricated/orphan score:
    # every row must be renderable (class A/B), cite a real http source, map to
    # a known benchmark key, and attach to a model the catalog or a provider
    # manifest names. A delisted model keeps its manifest row, and its scores.
    rows = bm._raw_scores()
    known_models = set(MODELS) | _manifest_model_ids()
    assert rows, "expected at least one shipped benchmark score"
    for row in rows:
        assert row["source_class"] in {"A", "B", "T"}, row
        assert str(row["source_url"]).startswith("http"), row
        assert row["benchmark_key"] in BENCHMARK_DEFS, row
        assert row["model_id"] in known_models, (
            f"score attached to unknown model: {row['model_id']}"
        )
        # Class "T" (TrustedRouter's own runs) must cite a published replay in
        # the trustedrouter-benchmarks repo — that link is the reproducibility
        # guarantee that justifies showing a first-party number.
        if row["source_class"] == "T":
            assert "trustedrouter-benchmarks" in row["source_url"], row


def test_a_score_outlives_its_model_in_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # Historical scores are data, not inventory. A score for a model the
    # catalog no longer carries stays in the data, no page links to it, and the
    # model's benchmarks page is the model-not-found page, not an error.
    gone = "retired/benchmarked-model"
    assert gone not in MODELS
    shipped = bm._raw_scores()
    monkeypatch.setattr(bm, "_raw_scores", lambda: [*shipped, {**shipped[0], "model_id": gone}])
    client = TestClient(create_app(_settings(), init_observability=False))

    assert gone in models_with_scores()
    assert scores_for_model(gone)
    assert client.get(f"/models/{gone}/benchmarks").status_code == 404
    for path in ("/benchmarks", "/models"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert gone not in page.text, path


def test_benchmarks_page_renders_cited_scores() -> None:
    model_id = next(
        model_id
        for model_id in sorted(models_with_scores())
        if model_id in MODELS and model_id not in META_MODEL_IDS
    )
    client = TestClient(create_app(_settings(), init_observability=False))
    resp = client.get(f"/models/{model_id}/benchmarks")
    assert resp.status_code == 200
    body = resp.text
    assert "Published benchmark scores" in body
    rows = scores_for_model(model_id)
    assert rows
    for row in rows:
        assert str(escape(row["label"])) in body
        assert row["display"] in body
        # Every score links to its primary source.
        assert f'href="{escape(row["source_url"])}"' in body
