from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_accepts_explicit_dispatch_for_bot_commits() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch: {}" in workflow


def test_price_refresh_dispatches_ci_before_deploy_and_fails_closed() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    ci_dispatch = 'gh workflow run ci.yml --ref main --repo "${GITHUB_REPOSITORY}"'
    deploy_dispatch = (
        'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}"'
    )

    assert ci_dispatch in workflow
    assert deploy_dispatch in workflow
    assert workflow.index(ci_dispatch) < workflow.index(deploy_dispatch)
    assert "WARN: failed to dispatch deploy.yml" not in workflow


def test_price_refresh_validates_generated_catalog_before_committing() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    validation_step = "- name: Validate generated catalog before commit"
    commit_step = "- name: Commit and push if changed"

    assert validation_step in workflow
    assert workflow.index(validation_step) < workflow.index(commit_step)
    validation = workflow[
        workflow.index(validation_step) : workflow.index(commit_step)
    ]
    assert "uv run ruff check ." in validation
    assert "uv run mypy" in validation
    assert "uv run pytest -q" in validation


def test_model_discovery_gap_alerts_without_freezing_safe_provider_updates() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    coverage_step = "- name: Price-source coverage audit"
    commit_step = "- name: Commit and push if changed"
    final_alert = "- name: Fail for unresolved model-discovery gaps"

    assert workflow.index(coverage_step) < workflow.index(commit_step)
    assert workflow.index(commit_step) < workflow.index(final_alert)
    coverage = workflow[
        workflow.index(coverage_step) : workflow.index(commit_step)
    ]
    assert "id: coverage_audit" in coverage
    assert "continue-on-error: true" in coverage
    assert "--strict-model-discovery" in coverage
    alert = workflow[workflow.index(final_alert) :]
    assert "steps.coverage_audit.outcome == 'failure'" in alert
    assert "exit 1" in alert

    spike_gate = workflow[
        workflow.index("- name: Sanity-check for price spikes + record deltas") :
        workflow.index("- name: Refresh provider social cards")
    ]
    assert "continue-on-error" not in spike_gate
