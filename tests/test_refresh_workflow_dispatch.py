import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Run the actual publication shell with git/gh replaced by in-process fakes:
# no commits, remotes, API calls, or sleeps. The decision helper and jq are real.
PUBLISH_STUBS = r'''
set -euo pipefail
mock_head=snapshot0
main_version=0
fetches=0
git() {
  echo "git $*" >> "$CALL_LOG"
  case "$*" in
    "rev-parse HEAD") echo "$mock_head" ;;
    "rev-parse origin/main") echo "base${main_version}" ;;
    "fetch -q origin main")
      fetches=$((fetches + 1))
      if [ "$fetches" -le "$MAIN_MOVES" ]; then main_version=$fetches; fi
      if [ "$SCENARIO" = push_race ] && [ "$fetches" -gt 2 ]; then main_version=2; fi
      ;;
    "merge-base "*) [ "$SCENARIO" != diverged ] ;;
    "diff "*)
      if [ "$SCENARIO" = diff_failure ]; then return 1; fi
      if [ "$SCENARIO" = relevant ] || { [ "$SCENARIO" = later_relevant ] && [ "$fetches" -gt 1 ]; }; then
        printf 'tests/unrelated.py\0scripts/pricing/refresh.py\0'
      else
        printf 'scripts/security/check_repo_security_coverage.py\0'
      fi
      ;;
    "checkout --detach "*) : ;;
    "cherry-pick --abort") [ "$SCENARIO" != cherry_pick_error ] ;;
    "cherry-pick snapshot0")
      if [ "$SCENARIO" = conflict ] || [ "$SCENARIO" = cherry_pick_error ]; then return 1; fi
      mock_head="snapshot${main_version}"
      ;;
    "push origin HEAD:refs/heads/main")
      echo "publish $mock_head" >> "$CALL_LOG"
      [ "$SCENARIO" != push_race ] && [ "$SCENARIO" != push_rejected ]
      ;;
    "push "*) : ;;
    *) echo "unexpected git: $*" >&2; return 1 ;;
  esac
}
gh() {
  echo "gh $*" >> "$CALL_LOG"
  case "$*" in
    "workflow run ci.yml "*) echo "dispatch $mock_head" >> "$CALL_LOG" ;;
    "workflow run refresh-prices.yml "*|"workflow run deploy.yml "*) : ;;
    "run list "*)
      local result=success head="$mock_head"
      if [ "$SCENARIO" = ci_failure ] && [ "$main_version" -gt 0 ]; then result=failure; fi
      if [ "$SCENARIO" = wrong_sha ] && [ "$main_version" -gt 0 ]; then head=other; fi
      jq -n --arg head "$head" --arg result "$result" --argjson id "$((100 + main_version))" '
        [{databaseId: $id, headSha: $head, status: "completed", conclusion: $result},
         {databaseId: 100, headSha: "snapshot0", status: "completed", conclusion: "success"}]' |
        jq -c "${!#}"
      ;;
    "api --method POST "*)
      [[ "$*" == *"repos/${GITHUB_REPOSITORY}/statuses/${mock_head} "* ]] || return 1
      [[ "$*" == *"target_url=${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/$((100 + main_version))" ]] || return 1
      local arg context="" state=success
      for arg in "$@"; do
        case "$arg" in context=*) context=${arg#context=} ;; esac
      done
      if [ "$SCENARIO" = bad_status ]; then state=failure; fi
      jq -n --arg url "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/statuses/${mock_head}" \
        --arg context "$context" --arg state "$state" --arg target "${ci_url}" \
        '{id: 42, url: $url, context: $context, state: $state, target_url: $target}'
      ;;
    "api repos/"*)
      [[ "$*" == *"repos/${GITHUB_REPOSITORY}/statuses/${mock_head} "* ]] || return 1
      local creator='github-actions[bot]'
      if [ "$SCENARIO" = bad_creator ]; then creator=other; fi
      jq --arg creator "$creator" '. + {creator: {login: $creator}}' <<< "$status_json"
      ;;
    *) echo "unexpected gh: $*" >&2; return 1 ;;
  esac
}
python() { "$TEST_PYTHON" "$@"; }
sleep() { :; }
'''


@pytest.mark.parametrize(
    ("scenario", "moves", "ci_count", "published", "recompute", "exit_code"),
    [
        ("success", 0, 1, True, False, 0),
        ("success", 1, 2, True, False, 0),
        ("success", 3, 4, True, False, 0),
        ("success", 4, 4, False, True, 0),
        ("relevant", 1, 1, False, True, 0),
        ("later_relevant", 2, 2, False, True, 0),
        ("conflict", 1, 1, False, True, 0),
        ("cherry_pick_error", 1, 1, False, True, 0),
        ("diverged", 1, 1, False, True, 0),
        ("diff_failure", 1, 1, False, False, 1),
        ("ci_failure", 1, 2, False, False, 1),
        ("wrong_sha", 1, 2, False, False, 1),
        ("bad_status", 1, 2, False, False, 1),
        ("bad_creator", 1, 2, False, False, 1),
        ("push_race", 1, 2, False, True, 0),
        ("push_rejected", 1, 2, False, False, 1),
    ],
)
def test_publication_flow(
    tmp_path: Path, scenario: str, moves: int, ci_count: int,
    published: bool, recompute: bool, exit_code: int,
) -> None:
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("publication shell requires bash and jq")
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["refresh"]["steps"]
        if step.get("name") == "Commit, verify, and push if changed"
    )
    # Earlier validation/commit steps are covered by the structural tests below.
    publication = step["run"][step["run"].index('BRANCH="automation/price-refresh-'):]
    log = tmp_path / "calls.log"
    result = subprocess.run(  # noqa: S603 - local workflow with git/gh mocked
        ["bash", "-c", PUBLISH_STUBS + publication],  # noqa: S607
        cwd=ROOT,
        env={
            **os.environ, "SCENARIO": scenario, "MAIN_MOVES": str(moves),
            "CALL_LOG": str(log), "TMPDIR": str(tmp_path), "TEST_PYTHON": sys.executable,
            "GITHUB_SHA": "base0", "GITHUB_RUN_ID": "123",
            "GITHUB_REPOSITORY": "example/repo", "GITHUB_SERVER_URL": "https://github.example",
            "GITHUB_API_URL": "https://api.github.example",
        },
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert [line for line in calls if line.startswith("dispatch ")] == [
        f"dispatch snapshot{i}" for i in range(ci_count)
    ]
    assert sum(line.startswith("gh workflow run refresh-prices.yml ") for line in calls) == int(recompute)
    assert any(line.startswith("gh workflow run deploy.yml ") for line in calls) == published
    assert calls[-1] == "git push origin --delete automation/price-refresh-123"
    assert all("base0..base" in line for line in calls if line.startswith("git diff "))
    if published:
        assert f"publish snapshot{moves}" in calls
        assert sum(line.startswith("gh api --method POST ") for line in calls) == 4
        assert sum(line.startswith("gh api repos/") for line in calls) == 4
    elif scenario not in {"push_race", "push_rejected"}:
        assert not any(line.startswith("publish ") for line in calls)
    if scenario in {"conflict", "cherry_pick_error"}:
        assert "git cherry-pick --abort" in calls


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
    assert 'if [ "${MAIN_SHA}" = "${BASE_SHA}" ]' in workflow
    assert "git rebase" not in workflow
    assert 'BASE_SHA=${GITHUB_SHA}' in workflow
    assert 'GENERATED_SHA=${SNAPSHOT_SHA}' in workflow
    assert 'git diff --name-only --no-renames -z "${GITHUB_SHA}..${MAIN_SHA}"' in workflow
    assert 'python scripts/pricing/publish_decision.py' in workflow
    assert 'if [ "${rebase_attempts}" -ge 3 ]' in workflow
    assert 'git checkout --detach "${BASE_SHA}"' in workflow
    assert 'git cherry-pick "${GENERATED_SHA}"' in workflow
    assert 'git cherry-pick --abort' in workflow
    assert 'git push --force origin "HEAD:refs/heads/${BRANCH}"' in workflow
    assert 'trap cleanup_branch EXIT' in workflow
    assert 'git push origin --delete "${BRANCH}"' in workflow
    assert 'if [ "$(git rev-parse origin/main)" != "${BASE_SHA}" ]' in workflow
    loop = workflow[workflow.index("          while true; do"):workflow.index("          # GitHub's protected-branch hook")]
    assert branch_ci_dispatch in loop
    assert 'ci_passed=""' in loop and 'ci_run_id=""' in loop
    assert 'SNAPSHOT_SHA=$(git rev-parse HEAD)' in loop
    assert loop.index(branch_ci_dispatch) < loop.index('git cherry-pick "${GENERATED_SHA}"')
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
        'if [ "${MAIN_SHA}" = "${BASE_SHA}" ]'
    ) < workflow.index(status_bridge)
    assert workflow.index(status_bridge) < workflow.index(main_push)
    assert workflow.index(main_push) < workflow.index(deploy_dispatch)
    assert "deploy_synthetic_monitor" not in workflow
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
    assert "uv run pytest -q -n 4 --dist loadgroup" in validation


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
