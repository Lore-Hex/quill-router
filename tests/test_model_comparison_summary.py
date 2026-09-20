from dataclasses import replace

from trusted_router.catalog import MODELS
from trusted_router.dashboard import _comparison_summary


def test_summary_groups_all_advantages_under_one_model_name() -> None:
    left = replace(MODELS["z-ai/glm-5.3"], name="Alpha", context_length=200_000)
    right = replace(left, name="Beta", context_length=100_000)
    summary = _comparison_summary(
        left, right, left_total=100, right_total=200, left_routes=3, right_routes=2,
        left_measured=100, right_measured=200,
    )
    assert summary.count("Alpha") == 1
    assert "lower published input-plus-output rate" in summary
    assert "more Credits provider routes" in summary
    assert "larger context window" in summary
    assert "lower measured p50 time to first token" in summary
    assert "Beta has" not in summary


def test_summary_keeps_price_and_speed_winners_separate() -> None:
    left = replace(MODELS["z-ai/glm-5.3"], name="Alpha", context_length=200_000)
    right = replace(left, name="Beta", context_length=100_000)
    summary = _comparison_summary(
        left, right, left_total=200, right_total=100, left_routes=2, right_routes=3,
        left_measured=100, right_measured=200,
    )
    assert "Alpha has the larger context window and the lower measured p50 time to first token." in summary
    assert "Beta has the lower published input-plus-output rate and more Credits provider routes." in summary


def test_summary_reports_ties_without_declaring_a_winner() -> None:
    left = replace(MODELS["z-ai/glm-5.3"], name="Alpha", context_length=100_000)
    right = replace(left, name="Beta")
    summary = _comparison_summary(
        left, right, left_total=100, right_total=100, left_routes=2, right_routes=2,
        left_measured=100, right_measured=100,
    )
    assert "Alpha has" not in summary and "Beta has" not in summary
    assert "rates are equal" in summary
    assert "Both have 2 Credits provider routes" in summary
    assert "context windows are the same size" in summary
    assert "equal p50 time to first token" in summary


def test_summary_does_not_treat_missing_prices_or_probes_as_winners() -> None:
    left = replace(MODELS["z-ai/glm-5.3"], name="Alpha", context_length=0)
    right = replace(left, name="Beta", context_length=100_000)
    summary = _comparison_summary(
        left, right, left_total=0, right_total=100, left_routes=0, right_routes=2,
        left_measured=None, right_measured=200,
    )
    assert "price comparison is not available" in summary
    assert "not enough recent probe data" in summary
    assert "lower" not in summary
    assert "larger context window" not in summary
