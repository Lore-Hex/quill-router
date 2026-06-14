from __future__ import annotations

import pytest

from trusted_router.evals.fusion_micro import (
    BLOCKED_MODEL_SUBSTRINGS,
    DRACO_JUDGE_MODEL,
    EXA_SEARCH_WITH_CONTENT_MICRODOLLARS,
    BudgetExceededError,
    EvalConfig,
    assert_model_allowed,
    build_draco_eval_plan,
    build_micro_run_plan,
    call_estimates_for_config,
    cost_artifact,
    default_draco_configs,
    default_micro_configs,
    draco_cost_artifact,
    estimate_config,
    frontier_draco_configs,
    frontier_fusion_draco_configs,
    frontier_solo_draco_configs,
    select_micro_tasks,
    write_draco_eval_artifacts,
    write_micro_artifacts,
)


def test_micro_hybrid_plan_stays_under_default_cap() -> None:
    plan = build_micro_run_plan(mode="micro-hybrid")

    assert plan.total_cost_microdollars <= 1_250_000
    assert plan.model_cost_microdollars > 0
    assert plan.search_cost_microdollars > 0
    assert {segment.id for segment in plan.segments} == {"offline", "search-smoke"}


def test_micro_offline_has_no_live_search_and_is_cheaper() -> None:
    offline = build_micro_run_plan(mode="micro-offline")
    hybrid = build_micro_run_plan(mode="micro-hybrid")

    assert offline.search_cost_microdollars == 0
    assert offline.total_cost_microdollars < hybrid.total_cost_microdollars
    assert offline.total_cost_microdollars < 700_000


def test_search_smoke_is_fusion_only_and_search_cost_is_explicit() -> None:
    plan = build_micro_run_plan(mode="micro-search-smoke")

    assert len(plan.segments) == 1
    segment = plan.segments[0]
    assert segment.task_count == 5
    assert all(item.kind == "fusion" for item in segment.config_estimates)
    expected_search = sum(
        item.task_count * item.search_requests_per_task * EXA_SEARCH_WITH_CONTENT_MICRODOLLARS
        for item in segment.config_estimates
    )
    assert segment.search_cost_microdollars == expected_search


def test_budget_guard_rejects_plan_before_any_live_calls() -> None:
    with pytest.raises(BudgetExceededError):
        build_micro_run_plan(mode="micro-hybrid", max_cost_microdollars=10_000)


def test_expensive_and_blocked_models_are_rejected() -> None:
    assert BLOCKED_MODEL_SUBSTRINGS
    for model_id in (
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
    ):
        with pytest.raises(ValueError):
            assert_model_allowed(model_id)


def test_default_configs_use_catalog_models_and_no_blocked_models() -> None:
    configs = default_micro_configs(include_kimi_2_6=True)

    assert {config.id for config in configs} >= {
        "fusion_main_micro",
        "fusion_ultra_micro",
        "solo_kimi_k2_7",
        "solo_kimi_k2_6",
    }


def test_task_selection_is_deterministic_and_domain_balanced() -> None:
    first = select_micro_tasks(20, seed=123)
    second = select_micro_tasks(20, seed=123)
    domains = {task.domain for task in first}

    assert [task.id for task in first] == [task.id for task in second]
    assert len(first) == 20
    assert len(domains) == 10
    for domain in domains:
        assert sum(1 for task in first if task.domain == domain) == 2


def test_cost_artifact_has_no_prompt_or_output_text() -> None:
    plan = build_micro_run_plan(mode="micro-offline")
    artifact = cost_artifact(plan)

    assert artifact["schema"] == "trustedrouter.fusion_micro.costs.v1"
    serialized = str(artifact)
    assert "Recommend five real albums" not in serialized
    assert "output_text" not in serialized
    assert artifact["tasks"][0].keys() == {"id", "domain"}


def test_artifact_writer_outputs_expected_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    plan = build_micro_run_plan(mode="micro-search-smoke")

    write_micro_artifacts(plan, tmp_path)

    assert (tmp_path / "costs.json").exists()
    assert (tmp_path / "scores.json").exists()
    assert (tmp_path / "frontier.svg").read_text(encoding="utf-8").startswith("<svg")
    assert "Estimate-only" in (tmp_path / "README.md").read_text(encoding="utf-8")


def test_invalid_fusion_config_requires_final_model() -> None:
    config = EvalConfig(
        id="bad",
        label="Bad",
        kind="fusion",
        generation_models=("mistralai/mistral-small-2603",),
        final_model=None,
    )

    with pytest.raises(ValueError):
        call_estimates_for_config(config)


def test_full_draco_plan_is_costed_and_avoids_blocked_models() -> None:
    plan = build_draco_eval_plan(task_count=100)

    assert plan.task_count == 100
    assert plan.judge_passes == 3
    assert plan.total_cost_microdollars > plan.model_cost_microdollars
    assert plan.search_cost_microdollars > 0
    assert plan.total_cost_microdollars < 45_000_000
    artifact = draco_cost_artifact(plan)
    called_models = {
        call["model"].lower() for config in artifact["configs"] for call in config["calls"]
    }
    for model_id in called_models:
        for blocked in BLOCKED_MODEL_SUBSTRINGS:
            assert blocked not in model_id


def test_default_draco_configs_use_openrouter_post_judge_model() -> None:
    configs = default_draco_configs()

    assert configs
    assert {config.judge_model for config in configs} == {DRACO_JUDGE_MODEL}


def test_frontier_solo_draco_configs_are_explicit_opt_in() -> None:
    defaults = {config.id for config in default_draco_configs()}
    frontier = {config.id: config for config in frontier_solo_draco_configs()}

    assert "solo_opus_4_8" not in defaults
    assert frontier["solo_opus_4_8"].generation_models == ("anthropic/claude-opus-4.8",)
    estimate = estimate_config(
        frontier["solo_opus_4_8"],
        task_count=1,
        live_search=True,
        judge_passes=1,
        allow_blocked_models=True,
    )
    assert estimate.total_cost_microdollars > 0


def test_frontier_fusion_draco_configs_are_explicit_opt_in() -> None:
    defaults = {config.id for config in default_draco_configs()}
    frontier = {config.id: config for config in frontier_fusion_draco_configs()}

    assert "fusion_mythos_candidate_6" not in defaults
    assert "fusion_mythos_candidate_7" not in defaults
    six = frontier["fusion_mythos_candidate_6"]
    seven = frontier["fusion_mythos_candidate_7"]
    assert six.kind == "fusion"
    assert len(six.generation_models) == 6
    assert seven.kind == "fusion"
    assert len(seven.generation_models) == 7
    assert seven.generation_models[:-1] == six.generation_models
    assert seven.generation_models[-1] == "google/gemini-3.1-pro-preview"
    assert six.final_model == "anthropic/claude-opus-4.8"
    estimate = estimate_config(
        seven,
        task_count=1,
        live_search=True,
        judge_passes=1,
        allow_blocked_models=True,
    )
    assert estimate.total_cost_microdollars > 0


def test_frontier_draco_configs_include_solo_and_fusion_opt_ins() -> None:
    ids = {config.id for config in frontier_draco_configs()}

    assert "solo_opus_4_8" in ids
    assert "fusion_mythos_candidate_6" in ids
    assert "fusion_mythos_candidate_7" in ids


def test_full_draco_pilot_scales_down_from_full_plan() -> None:
    full = build_draco_eval_plan(task_count=100)
    pilot = build_draco_eval_plan(task_count=10)

    assert pilot.total_cost_microdollars * 10 == full.total_cost_microdollars
    assert pilot.search_cost_microdollars * 10 == full.search_cost_microdollars


def test_draco_artifact_writer_outputs_expected_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    plan = build_draco_eval_plan(task_count=10)

    write_draco_eval_artifacts(plan, tmp_path)

    assert (tmp_path / "draco-costs.json").exists()
    assert (tmp_path / "draco-frontier.svg").read_text(encoding="utf-8").startswith("<svg")
    assert "Estimate-only" in (tmp_path / "README.md").read_text(encoding="utf-8")
