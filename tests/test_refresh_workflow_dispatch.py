from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_accepts_explicit_dispatch_for_bot_commits() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch: {}" in workflow


def test_price_refresh_checks_exact_branch_sha_before_advancing_main() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    branch_ci_dispatch = "gh workflow run ci.yml \\\n"
    deploy_dispatch = (
        'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}"'
    )

    assert 'BRANCH="automation/price-refresh-${GITHUB_RUN_ID}"' in workflow
    assert "SNAPSHOT_SHA=$(git rev-parse HEAD)" in workflow
    assert 'git push origin "HEAD:refs/heads/${BRANCH}"' in workflow
    assert branch_ci_dispatch in workflow
    assert '--ref "${BRANCH}"' in workflow
    assert '--branch "${BRANCH}"' in workflow
    assert 'select(.headSha == \\"${SNAPSHOT_SHA}\\")' in workflow
    assert 'if [ "${conclusion}" = "success" ]' in workflow
    assert "statuses: write" in workflow
    assert 'ci_run_id=$(jq -r .databaseId <<<"${run_json}")' in workflow
    assert 'if [ "$(git rev-parse origin/main)" != "${GITHUB_SHA}" ]' in workflow
    assert "git rebase" not in workflow
    status_bridge = '"repos/${GITHUB_REPOSITORY}/statuses/${SNAPSHOT_SHA}"'
    assert status_bridge in workflow
    assert 'for context in "lint" "test (1)" "test (2)" "test (3)"' in workflow
    assert '-f target_url="${ci_url}"' in workflow
    assert 'expected_status_url="${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/statuses/${SNAPSHOT_SHA}"' in workflow
    assert "status_id=$(jq -r '.id // empty'" in workflow
    assert '$(jq -r .url <<<"${status_json}")' in workflow
    assert '$(jq -r .context <<<"${status_json}")' in workflow
    assert '$(jq -r .state <<<"${status_json}")' in workflow
    assert '--jq "map(select(.id == ${status_id}))[0] // empty"' in workflow
    assert '$(jq -r .creator.login <<<"${verified_status}")' in workflow
    assert '!= "github-actions[bot]"' in workflow
    main_push = 'git push origin "HEAD:refs/heads/main"'
    assert main_push in workflow
    assert deploy_dispatch in workflow
    assert workflow.index(branch_ci_dispatch) < workflow.index(main_push)
    assert workflow.index('if [ "${conclusion}" = "success" ]') < workflow.index(
        status_bridge
    )
    assert workflow.index(
        'if [ "$(git rev-parse origin/main)" != "${GITHUB_SHA}" ]'
    ) < workflow.index(status_bridge)
    assert workflow.index(status_bridge) < workflow.index(main_push)
    assert workflow.index(main_push) < workflow.index(deploy_dispatch)
    assert "WARN: failed to dispatch deploy.yml" not in workflow


def test_price_refresh_validates_generated_catalog_before_committing() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    validation_step = "- name: Validate generated catalog before commit"
    commit_step = "- name: Commit, verify, and push if changed"

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
    commit_step = "- name: Commit, verify, and push if changed"
    final_alert = "- name: Reconcile model-discovery coverage issue"

    assert workflow.index(coverage_step) < workflow.index(commit_step)
    assert workflow.index(commit_step) < workflow.index(final_alert)
    coverage = workflow[
        workflow.index(coverage_step) : workflow.index(commit_step)
    ]
    assert "id: coverage_audit" in coverage
    assert "continue-on-error: true" in coverage
    assert "--strict-model-discovery" in coverage
    assert "tee /tmp/coverage-summary.txt" in coverage
    alert = workflow[workflow.index(final_alert) :]
    assert "COVERAGE_OUTCOME: ${{ steps.coverage_audit.outcome }}" in alert
    assert 'title="[bot] Model discovery coverage gaps"' in alert
    assert "gh issue create" in alert
    assert "gh issue edit" in alert
    assert "gh issue close" in alert
    assert "exit 1" not in alert
    assert "issues: write" in workflow

    spike_gate = workflow[
        workflow.index("- name: Sanity-check for price spikes + record deltas") :
        workflow.index("- name: Refresh provider social cards")
    ]
    assert "continue-on-error" not in spike_gate
