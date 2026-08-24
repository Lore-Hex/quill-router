from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router import benchmark_scores as bm
from trusted_router.benchmark_scores import (
    BENCHMARK_DEFS,
    models_with_scores,
    scores_for_model,
)
from trusted_router.catalog import MODELS
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


def test_shipped_benchmark_data_integrity() -> None:
    # Guards against a bad future edit shipping a fabricated/orphan score:
    # every row must be renderable (class A/B), cite a real http source, map to
    # a known benchmark key, and attach to a real catalog model.
    rows = bm._raw_scores()
    assert rows, "expected at least one shipped benchmark score"
    for row in rows:
        assert row["source_class"] in {"A", "B", "T"}, row
        assert str(row["source_url"]).startswith("http"), row
        assert row["benchmark_key"] in BENCHMARK_DEFS, row
        assert row["model_id"] in MODELS, f"score attached to unknown model: {row['model_id']}"
        # Class "T" (TrustedRouter's own runs) must cite a published replay in
        # the trustedrouter-benchmarks repo — that link is the reproducibility
        # guarantee that justifies showing a first-party number.
        if row["source_class"] == "T":
            assert "trustedrouter-benchmarks" in row["source_url"], row


def test_models_with_scores_are_all_in_catalog() -> None:
    assert models_with_scores() <= set(MODELS)


def test_benchmarks_page_renders_cited_scores() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    resp = client.get("/models/anthropic/claude-sonnet-4.5/benchmarks")
    assert resp.status_code == 200
    body = resp.text
    assert "Published benchmark scores" in body
    assert "SWE-bench Verified" in body
    assert "77.2%" in body
    # Every score links to its primary source.
    assert "anthropic.com/news/claude-sonnet-4-5" in body
