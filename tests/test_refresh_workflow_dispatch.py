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
