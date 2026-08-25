from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_load_balanced_control_plane_region_is_staged() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    deploy = workflow.split("\n  deploy:\n", 1)[1].split(
        "\n  rollout-secondaries:\n", 1
    )[0]
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]

    assert "deploy_cold_regions:" not in workflow
    assert "steps.optional.outputs.deploy_cold_regions" not in workflow
    assert "regions=(us-central1 europe-west4 us-east4 southamerica-east1)" in deploy
    assert 'TR_DEPLOY_TARGET_REGIONS="${region}"' in deploy
    for region in ("europe-west4", "us-east4", "southamerica-east1"):
        assert f'ramp_secondary "{region}"' in rollout


def test_prod_smoke_checks_each_control_plane_region_directly() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert (
        "for region in us-central1 us-east4 europe-west4 southamerica-east1; do"
        in workflow
    )
    assert 'check_url "ready_${region}" "${service_url}/ready"' in workflow
    assert 'check_url "status_${region}" "${service_url}/status.json"' in workflow
    assert 'check_url "status_page_${region}" "${service_url}/status"' in workflow
    assert 'check_url "leaderboard_${region}" "${service_url}/leaderboard"' in workflow
    assert (
        'check_url "video_leaderboard_${region}" "${service_url}/leaderboard/video"'
        in workflow
    )
    assert 'active_revision=$(jq -r' in workflow
    assert '[ "${active_count}" != "1" ]' in workflow
    assert '[ "${active_revision}" != "${latest_ready}" ]' in workflow
    assert '[ "${latest_ready}" != "${latest_created}" ]' in workflow
    assert 'gcloud run revisions describe "${active_revision}"' in workflow
    assert '[ "${active_release}" != "${SHA}" ]' in workflow
    assert '[ "${retired_secret_count}" != "0" ]' in workflow


def test_deploy_syncs_the_shared_public_snapshot_worker() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "- name: Sync shared public analytics snapshots" in workflow
    assert "scripts/deploy/sync_public_analytics_snapshots.sh --apply" in workflow


def test_public_snapshot_worker_swap_is_verified_and_rollbackable() -> None:
    script = (
        ROOT / "scripts/deploy/sync_public_analytics_snapshots.sh"
    ).read_text(encoding="utf-8")

    assert "systemctl stop tr-clickhouse-public-snapshots.timer" in script
    assert "tr-clickhouse-public-snapshots.service" in script
    assert "previous_builder=" in script
    assert "rollback()" in script
    assert r'if [ \"\$count\" != 4 ]; then' in script
    assert r'mv \"\$previous_builder\" \"\$builder\"' in script


def test_all_regions_warm_in_parallel_before_primary_traffic_moves() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    deploy = workflow.split("\n  deploy:\n", 1)[1].split(
        "\n  rollout-secondaries:\n", 1
    )[0]
    start = deploy.index("- name: Warm all four regions in parallel (no traffic)")
    end = deploy.index("- name: Stage traffic 10/50/100 (us-central1)", start)
    warm = deploy[start:end]

    assert "regions=(us-central1 europe-west4 us-east4 southamerica-east1)" in warm
    assert 'if ! TR_DEPLOY_TARGET_REGIONS="${region}"' in warm
    first_wait = warm.index('if wait "${pids[$idx]}"; then')
    for region, reconcile_lb, log_index in (
        ("us-central1", "1", 0),
        ("europe-west4", "0", 1),
        ("us-east4", "0", 2),
        ("southamerica-east1", "0", 3),
    ):
        invocation = (
            f'(warm_region "{region}" "{reconcile_lb}" '
            f'"${{revision_files[{log_index}]}}") '
            f'>"${{logs[{log_index}]}}" 2>&1 &'
        )
        assert warm.index(invocation) < first_wait
    assert warm.count('pids+=("$!")') == 4
    assert 'printf \'\\n=== warmup: %s ===\\n\' "${regions[$idx]}"' in warm
    assert 'cat "${logs[$idx]}"' in warm
    assert "Regional warmup failed; no traffic moved" in warm
    assert "TR_DEPLOY_RECONCILE_LB" in warm
    assert "Only the primary process receives 1" in warm

    primary_ramp = deploy.index("- name: Stage traffic 10/50/100 (us-central1)")
    primary_canary = deploy.index("- name: Canary gate — watch us-central1 only")
    assert first_wait < primary_ramp < primary_canary
    assert 'staged_traffic.sh europe-west4' not in deploy
    assert 'staged_traffic.sh us-east4' not in deploy
    assert 'staged_traffic.sh southamerica-east1' not in deploy
    assert "ramp_secondary" not in deploy
    assert "timeout-minutes: 25" in deploy
    release = deploy.index(
        "- name: Release production deployment mutex after primary-live failure"
    )
    assert "if: ${{ failure() || cancelled() }}" in deploy[release : release + 220]
    assert "GitHub never schedules rollout-secondaries" in deploy
    assert "the 90-minute TTL recovers" in deploy
    assert "Queued deploys fail closed" in deploy


def test_secondaries_ramp_serially_while_reconciler_deploys() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]

    assert "needs: [deploy]" in rollout
    assert "if: ${{ success() }}" in rollout
    assert "timeout-minutes: 55" in rollout
    first_step = rollout.index("- name: Import production deployment mutex fence")
    checkout = rollout.index("- uses: actions/checkout@v4")
    assert first_step < checkout
    assert "needs.deploy.outputs.TR_DEPLOY_MUTEX_OPERATION" in rollout
    assert "needs.deploy.outputs.TR_DEPLOY_MUTEX_GENERATION" in rollout
    assert 'echo "TR_DEPLOY_MUTEX_OPERATION=${DEPLOY_MUTEX_OPERATION}"' in rollout
    assert 'echo "TR_DEPLOY_MUTEX_GENERATION=${DEPLOY_MUTEX_GENERATION}"' in rollout

    ramp_step = rollout.index(
        "- name: Ramp secondaries serially while reconciler deploys"
    )
    ramp = rollout[ramp_step : rollout.index("- name: Deploy synthetic monitor", ramp_step)]
    staged_call = ramp.index("bash scripts/deploy/staged_traffic.sh")
    staged_line = ramp[staged_call : ramp.index("\n", staged_call)]
    assert "&" not in staged_line
    stamp = ramp.index('rollout_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"')
    billing_gate = ramp.index("assert_no_billing_5xx.sh")
    assert stamp < staged_call < billing_gate

    reconciler = ramp.index("regional_quota_reconciler.sh")
    ramp_eu = ramp.index('ramp_secondary "europe-west4"')
    ramp_us = ramp.index('ramp_secondary "us-east4"')
    ramp_sa = ramp.index('ramp_secondary "southamerica-east1"')
    wait = ramp.index('wait "${reconciler_pid}"')
    assert reconciler < ramp_eu < ramp_us < ramp_sa < wait
    assert 'regional_quota_reconciler.sh >"${reconciler_log}" 2>&1 &' in ramp
    assert 'printf \'\\n=== regional quota reconciler deploy ===\\n\'' in ramp
    assert '[ "${ramp_status}" -eq 0 ]' in ramp
    assert "Later regions remain warm at zero traffic and never received traffic" in ramp
    assert "#695 (billing 5xx, 2026-08-20)" in ramp
    assert "--slo-class router_core" in ramp
    assert "secondary-started-" not in ramp

    release = rollout.index("- name: Release production deployment mutex")
    assert "if: always()" in rollout[release : release + 180]
    assert "deploy_mutex.sh release" in rollout[release : release + 180]


def test_full_convergence_jobs_need_rollout_secondaries() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    companion = workflow.split("\n  public-surface-companion:\n", 1)[1].split(
        "\n  verify-cloud-complete:\n", 1
    )[0]
    verify = workflow.split("\n  verify-cloud-complete:\n", 1)[1]

    assert "needs: [rollout-secondaries]" in companion
    assert "needs: [rollout-secondaries]" in verify
    assert "needs.rollout-secondaries.result != 'skipped'" in verify


def test_primary_rollout_gates_on_router_core_and_billing_path_errors() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    primary = workflow.index("- name: Warm all four regions in parallel (no traffic)")
    secondary = workflow.index("\n  rollout-secondaries:", primary)
    rollout = workflow[primary:secondary]

    assert "TR_WATCHDOG_SLO_CLASS: router_core" in rollout
    assert "--slo-class router_core" in rollout
    assert "- name: Billing-path gate + rollback (us-central1)" in rollout
    assert "scripts/deploy/assert_no_billing_5xx.sh" in rollout
    assert '"${{ steps.warm_all.outputs.us_central1_revision }}"' in rollout
    assert '--to-revisions="${PREV_US}=100"' in rollout


def test_billing_path_gate_only_attributes_errors_to_candidate_revision() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/deploy/assert_no_billing_5xx.sh").read_text(
        encoding="utf-8"
    )

    assert 'REVISION="${3:?usage:' in script
    assert 'resource.labels.revision_name=\\"${REVISION}\\"' in script
    assert '"${region}" "${rollout_started_at}" "${revision}"' in workflow


def test_superseded_push_stops_before_production_mutation() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    confirm = workflow.index("confirm-current-main:")
    deploy = workflow.index("\n  deploy:", confirm)
    mutation = workflow.index("- name: Capture pre-deploy revisions", deploy)

    assert confirm < deploy < mutation
    assert 'git/ref/heads/main" --jq' in workflow[confirm:deploy]
    assert 'echo "proceed=false" >> "$GITHUB_OUTPUT"' in workflow[confirm:deploy]
    assert "if: ${{ needs.confirm-current-main.outputs.proceed == 'true' }}" in workflow


def test_rollback_capture_uses_the_sole_serving_revision() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    start = workflow.index("- name: Capture pre-deploy revisions")
    end = workflow.index("- name: Warm all four regions in parallel", start)
    capture = workflow[start:end]

    assert "set -euo pipefail" in capture
    assert "--format=json" in capture
    assert "scripts/deploy/resolve_active_revision.py" in capture
    assert "status.traffic[0]" not in capture
    assert "|| true" not in capture
    assert "has no unambiguous 100%-traffic rollback revision" in capture


def test_runtime_secret_validation_is_parallel_and_does_not_restore_stale_ses_keys() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    sync = workflow.index("sync-runtime-secrets:")
    confirm = workflow.index("confirm-current-main:", sync)
    section = workflow[sync:confirm]

    assert "needs: [gate-on-ci]" in section
    assert "trustedrouter-aws-access-key-id" in section
    assert "gcloud secrets versions access latest" in section
    assert "secrets.TR_AWS_ACCESS_KEY_ID" not in section
    assert "secrets.TR_AWS_SECRET_ACCESS_KEY" not in section
    assert "trustedrouter-clickhouse-provider-read-password" in section
    assert "run: bash scripts/deploy/secrets.sh" not in workflow


def test_shared_load_balancer_is_reconciled_once_per_workflow() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert workflow.count('(warm_region "us-central1" "1"') == 1
    for region in ("europe-west4", "us-east4", "southamerica-east1"):
        assert workflow.count(f'(warm_region "{region}" "0"') == 1
    assert 'TR_DEPLOY_RECONCILE_LB="${reconcile_lb}"' in workflow


def test_mutex_acquire_step_cannot_swallow_a_blocked_exit() -> None:
    """A blocked acquire must fail the step.

    GitHub's default run shell is `bash -e` WITHOUT pipefail, so piping
    acquire into tee makes the step's exit code tee's — a refused lock
    would deploy WITHOUT holding it. The acquire invocation must therefore
    never sit on the left of an unguarded pipe.
    """
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    for line in workflow.splitlines():
        if "deploy_mutex.sh acquire" in line and "|" in line.split("acquire", 1)[1]:
            raise AssertionError(
                "deploy_mutex.sh acquire is piped; a blocked acquire would "
                "exit 0 through the pipe under bash -e without pipefail: "
                + line.strip()
            )
