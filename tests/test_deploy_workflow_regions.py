from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ROLLOUT_SECONDARIES_DEPLOY_GATE = (
    "if: ${{ github.repository == 'Lore-Hex/quill-router' && success()"
    " && needs.deploy.result == 'success' }}"
)


def _assert_rollout_secondaries_requires_successful_deploy(workflow: str) -> None:
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]
    assert ROLLOUT_SECONDARIES_DEPLOY_GATE in rollout, (
        "rollout-secondaries must require needs.deploy.result == 'success' "
        "before importing deploy outputs"
    )


def test_every_load_balanced_control_plane_region_is_staged() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    deploy = workflow.split("\n  deploy:\n", 1)[1].split("\n  rollout-secondaries:\n", 1)[0]
    ramp = (ROOT / "scripts/deploy/ramp_secondaries.sh").read_text(encoding="utf-8")

    assert "deploy_cold_regions:" not in workflow
    assert "steps.optional.outputs.deploy_cold_regions" not in workflow
    assert "regions=(us-central1 europe-west4 us-east4 southamerica-east1)" in deploy
    assert 'TR_DEPLOY_TARGET_REGIONS="${region}"' in deploy
    for region in ("europe-west4", "us-east4", "southamerica-east1"):
        assert region in ramp


def test_prod_smoke_checks_public_origins_and_converges_private_regions() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "for region in us-central1 us-east4 europe-west4 southamerica-east1; do" in workflow
    smoke = workflow.split("- name: Smoke test prod", 1)[1]
    assert "source scripts/deploy/_cloud_run_revision_probe.sh" in smoke
    check_url = smoke.split("check_url() {", 1)[1].split("\n          }", 1)[0]
    assert "for attempt in 1 2 3; do" in check_url
    assert "--connect-timeout 5 --max-time 15" in check_url
    assert 'if [[ "$code" == "200" ]]; then' in check_url
    assert 'if [ "$attempt" -lt 3 ]; then' in check_url
    assert 'echo "${name} failed after 3 attempts: ${url}" >&2' in check_url
    assert "return 1" in check_url
    assert "service_ingress=$(cloud_run_service_ingress" in smoke
    assert 'if [ "${service_ingress}" = "all" ]; then' in smoke
    direct = smoke.split('if [ "${service_ingress}" = "all" ]; then', 1)[1].split("else", 1)[0]
    assert 'check_url "ready_${region}" "${service_url}/ready"' in direct
    assert 'check_url "status_${region}" "${service_url}/status.json"' in direct
    assert 'check_url "status_page_${region}" "${service_url}/status"' in direct
    assert 'check_url "leaderboard_${region}" "${service_url}/leaderboard"' in direct
    assert 'check_url "video_leaderboard_${region}" "${service_url}/leaderboard/video"' in direct
    assert "private Cloud Run ingress" in smoke
    assert "active_revision=$(jq -r" in smoke
    assert '[ "${active_count}" != "1" ]' in smoke
    assert '[ "${active_revision}" != "${latest_ready}" ]' in smoke
    assert '[ "${latest_ready}" != "${latest_created}" ]' in smoke
    assert 'gcloud run revisions describe "${active_revision}"' in smoke
    assert '[ "${active_release}" != "${SHA}" ]' in smoke
    assert '[ "${retired_secret_count}" != "0" ]' in smoke


def test_deploy_syncs_the_shared_public_snapshot_worker() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "- name: Sync shared public analytics snapshots" in workflow
    assert "scripts/deploy/sync_public_analytics_snapshots.sh --apply" in workflow


def test_snapshot_sync_cannot_gate_the_control_plane_deploy() -> None:
    """A public-analytics sync must never hold the control plane hostage.

    It did on 2026-09-04: `gcloud compute scp` pushes a generated SSH key into
    project metadata, that write timed out twice at 1800s behind a backlog of
    stuck setCommonInstanceMetadata operations, and the whole deploy failed
    ~61 minutes in -- with a Stripe-credit fix sitting in the image it was
    carrying. Bounded and non-fatal, so a snapshot problem degrades snapshots.
    """
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    step = workflow.split("- name: Sync shared public analytics snapshots", 1)[1].split(
        "- name:", 1
    )[0]
    assert "continue-on-error: true" in step, (
        "the snapshot sync must not fail the deploy job"
    )
    # The deadline belongs on the upload, not the step: a step-level timeout
    # can sever SSH between the two `mv`s of the remote swap and leave the
    # node with no live source tree, and the next run deletes the backup.
    assert "timeout-minutes:" not in step, (
        "a step deadline can interrupt the remote swap; bound the upload instead"
    )
    script = (ROOT / "scripts/deploy/sync_public_analytics_snapshots.sh").read_text(
        encoding="utf-8"
    )
    upload = script.split("compute scp", 1)[0].rsplit("\n", 3)[-1] + script.split(
        "compute scp", 1
    )[0][-80:]
    assert "timeout -k 30 300" in script, "the snapshot upload must be time-bounded"
    assert "timeout" in upload or "timeout -k 30 300 gcloud" in script, upload
    swap = script.split("compute ssh", 1)[1]
    assert "timeout" not in swap.split("--command", 1)[0], (
        "the remote swap must not carry a deadline that can interrupt it"
    )


def test_public_snapshot_worker_swap_is_verified_and_rollbackable() -> None:
    script = (ROOT / "scripts/deploy/sync_public_analytics_snapshots.sh").read_text(
        encoding="utf-8"
    )

    assert "systemctl stop tr-clickhouse-public-snapshots.timer" in script
    assert "tr-clickhouse-public-snapshots.service" in script
    assert "previous_builder=" in script
    assert "rollback()" in script
    assert r"if [ \"\$count\" != 4 ]; then" in script
    assert r"mv \"\$previous_builder\" \"\$builder\"" in script
    # Upload first, then swap (#1080): the archive must not be streamed over
    # SSH stdin. Asserted by behaviour rather than by one exact spelling, so
    # the line can carry a deadline without breaking this.
    assert "compute scp" in script
    assert '"$archive" "${NAME}:${remote_archive}"' in script
    assert '--ssh-flag="-n"' in script
    assert '--ssh-flag="-T"' in script
    assert 'tar -xzf \\"\\$archive\\" -C \\"\\$stage\\"' in script
    assert '<"$archive"' not in script


def test_all_regions_launch_together_but_only_primary_warm_gates_traffic() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    deploy = workflow.split("\n  deploy:\n", 1)[1].split("\n  rollout-secondaries:\n", 1)[0]
    start = deploy.index("- name: Warm all four regions in parallel (no traffic)")
    end = deploy.index("- name: Stage traffic 10/50/100 (us-central1)", start)
    warm = deploy[start:end]

    assert "regions=(us-central1 europe-west4 us-east4 southamerica-east1)" in warm
    assert 'if ! TR_DEPLOY_TARGET_REGIONS="${region}"' in warm
    primary_wait = warm.index('if wait "${pids[0]}"; then')
    for region, reconcile_lb, log_index in (
        ("us-central1", "1", 0),
        ("europe-west4", "0", 1),
        ("us-east4", "0", 2),
        ("southamerica-east1", "0", 3),
    ):
        invocation = (
            f'(run_warm "${{status_files[{log_index}]}}" '
            f'"{region}" "{reconcile_lb}" '
            f'"${{revision_files[{log_index}]}}") '
            f'>"${{logs[{log_index}]}}" 2>&1 &'
        )
        assert warm.index(invocation) < primary_wait
    assert warm.count('pids+=("$!")') == 4
    assert "secondary-warms.tsv" in warm
    assert '>>"${secondary_state}"' in warm
    assert "printf '\\n=== warmup: %s ===\\n' \"${regions[0]}\"" in warm
    assert 'cat "${logs[0]}"' in warm
    assert "us-central1 no-traffic warmup failed; no traffic moved" in warm
    assert "TR_DEPLOY_RECONCILE_LB" in warm
    assert "Only the primary process receives 1" in warm

    primary_ramp = deploy.index("- name: Stage traffic 10/50/100 (us-central1)")
    primary_canary = deploy.index("- name: Canary gate — watch us-central1 only")
    secondary_wait = deploy.index("- name: Wait for secondary no-traffic warms")
    assert primary_wait < primary_ramp < primary_canary < secondary_wait
    collector = deploy[
        secondary_wait : deploy.index(
            "- name: Release production deployment mutex after primary-live failure",
            secondary_wait,
        )
    ]
    assert "if: ${{ always() }}" in collector
    assert 'kill -0 "${pid}"' in collector
    assert 'done <"${secondary_state}"' in collector
    assert 'key="${region//-/_}_revision"' in collector
    for region in ("europe-west4", "us-east4", "southamerica-east1"):
        key = region.replace("-", "_") + "_revision"
        assert f"steps.wait_secondary_warms.outputs.{key}" in deploy
    assert "no secondary traffic moved" in collector
    assert "staged_traffic.sh europe-west4" not in deploy
    assert "staged_traffic.sh us-east4" not in deploy
    assert "staged_traffic.sh southamerica-east1" not in deploy
    assert "ramp_secondary" not in deploy
    assert "timeout-minutes: 25" in deploy
    release = deploy.index("- name: Release production deployment mutex after primary-live failure")
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
    _assert_rollout_secondaries_requires_successful_deploy(workflow)
    assert "timeout-minutes: 55" in rollout
    first_step = rollout.index("- name: Import production deployment mutex fence")
    checkout = rollout.index("- uses: actions/checkout@v4")
    assert first_step < checkout
    assert "needs.deploy.outputs.TR_DEPLOY_MUTEX_OPERATION" in rollout
    assert "needs.deploy.outputs.TR_DEPLOY_MUTEX_GENERATION" in rollout
    assert 'echo "TR_DEPLOY_MUTEX_OPERATION=${DEPLOY_MUTEX_OPERATION}"' in rollout
    assert 'echo "TR_DEPLOY_MUTEX_GENERATION=${DEPLOY_MUTEX_GENERATION}"' in rollout

    ramp_step = rollout.index("- name: Ramp secondaries serially while reconciler deploys")
    ramp = rollout[ramp_step : rollout.index("- name: Deploy synthetic monitor", ramp_step)]
    assert "run: bash scripts/deploy/ramp_secondaries.sh" in ramp
    assert "run: |" not in ramp

    script = (ROOT / "scripts/deploy/ramp_secondaries.sh").read_text(encoding="utf-8")
    staged_call = script.index('bash "${SCRIPT_DIR}/staged_traffic.sh"')
    stamp = script.index('rollout_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"')
    billing_gate = script.index("assert_no_billing_5xx.sh")
    assert stamp < staged_call < billing_gate

    reconciler = script.index("regional_quota_reconciler.sh")
    ramp_loop = script.index("for region in europe-west4 us-east4 southamerica-east1")
    wait = script.index('wait "${reconciler_pid}"')
    assert reconciler < ramp_loop < wait
    assert 'regional_quota_reconciler.sh" >"${reconciler_log}" 2>&1 &' in script
    assert "printf '\\n=== regional quota reconciler deploy ===\\n'" in script
    assert "Later regions remain warm at zero traffic and never received traffic" in script
    assert "#695 (billing 5xx, 2026-08-20)" in script
    assert "--slo-class router_core" in script
    assert "secondary-started-" not in script

    release = rollout.index("- name: Release production deployment mutex")
    assert "if: always()" in rollout[release : release + 180]
    assert "deploy_mutex.sh release" in rollout[release : release + 180]


def test_rollout_secondaries_deploy_result_gate_rejects_bare_success_mutation() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    mutated = workflow.replace(
        ROLLOUT_SECONDARIES_DEPLOY_GATE,
        "if: ${{ github.repository == 'Lore-Hex/quill-router' && success() }}",
        1,
    )
    assert mutated != workflow, "mutation target must exist in deploy.yml"

    with pytest.raises(AssertionError, match="must require needs.deploy.result"):
        _assert_rollout_secondaries_requires_successful_deploy(mutated)


def test_full_convergence_jobs_need_rollout_secondaries() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    companion = workflow.split("\n  public-surface-companion:\n", 1)[1].split(
        "\n  verify-cloud-complete:\n", 1
    )[0]
    verify = workflow.split("\n  verify-cloud-complete:\n", 1)[1]

    assert "needs: [rollout-secondaries]" in companion
    assert "needs: [rollout-secondaries]" in verify
    assert "needs.rollout-secondaries.result != 'skipped'" in verify


def test_regional_hold_is_read_by_both_traffic_jobs() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    deploy = workflow.split("\n  deploy:\n", 1)[1].split(
        "\n  rollout-secondaries:\n", 1
    )[0]
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]

    binding = "TR_DEPLOY_HOLD_REGIONS: ${{ vars.TR_DEPLOY_HOLD_REGIONS }}"
    assert binding in deploy
    assert binding in rollout
    assert "deploy_region_is_held us-central1" in deploy
    assert "deploy_region_is_held \"$region\"" in deploy


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
    assert '--to-revisions="${RAMP_BASELINE_US}=100"' in rollout
    assert '--to-revisions="${PREV_US}=100"' not in rollout


def test_primary_ramp_resolves_current_baseline_and_only_logs_snapshot() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    start = workflow.index("- name: Resolve primary ramp baseline")
    end = workflow.index("- name: Canary gate — watch us-central1 only", start)
    primary = workflow[start:end]

    assert "scripts/deploy/resolve_active_revision.py" in primary
    assert "snapshot=${SNAPSHOT_REVISION} current=${current_revision}" in primary
    assert "outputs.us_central1_revision }}\" \"${{ steps.primary_baseline.outputs.revision" in primary
    assert "new revision ${NEW_REVISION} is older than current serving revision" in primary
    assert "operator intervention:" in primary


def test_billing_path_gate_only_attributes_errors_to_candidate_revision() -> None:
    script = (ROOT / "scripts/deploy/assert_no_billing_5xx.sh").read_text(encoding="utf-8")

    assert 'REVISION="${3:?usage:' in script
    assert 'resource.labels.revision_name=\\"${REVISION}\\"' in script
    ramp = (ROOT / "scripts/deploy/ramp_secondaries.sh").read_text(encoding="utf-8")
    assert '"${region}" "${rollout_started_at}" "${revision}"' in ramp


def test_superseded_push_stops_before_production_mutation() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    confirm = workflow.index("confirm-current-main:")
    deploy = workflow.index("\n  deploy:", confirm)
    mutation = workflow.index("- name: Capture pre-deploy revisions", deploy)

    assert confirm < deploy < mutation
    assert 'git/ref/heads/main" --jq' in workflow[confirm:deploy]
    assert 'echo "proceed=false" >> "$GITHUB_OUTPUT"' in workflow[confirm:deploy]
    assert (
        "if: ${{ github.repository == 'Lore-Hex/quill-router' && "
        "needs.confirm-current-main.outputs.proceed == 'true' }}" in workflow
    )


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

    assert workflow.count('"us-central1" "1" "${revision_files[0]}"') == 1
    for region in ("europe-west4", "us-east4", "southamerica-east1"):
        assert workflow.count(f'"{region}" "0"') == 1
    assert 'TR_DEPLOY_RECONCILE_LB="${reconcile_lb}"' in workflow


def test_probe_tag_is_hoisted_and_watchdog_baseline_overlaps_traffic_shift() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    staged = (ROOT / "scripts/deploy/staged_traffic.sh").read_text(encoding="utf-8")

    assert "warm_no_traffic_candidate" in rollout
    assert "cloud_run_probe_tag_reconcile" in rollout
    assert '"${base_url}/ready"' in rollout
    verify = staged.index("probe tag already resolves to warmed revision")
    reconcile = staged.index("cloud_run_probe_tag_reconcile")
    assert verify < reconcile

    watchdog_start = staged.index('python3 "${SCRIPT_DIR}/watchdog.py"')
    watchdog_background = staged.index("&", watchdog_start)
    shift = staged.index('shift_traffic "$stage_pct"', watchdog_background)
    gate = staged.index(': >"$WATCHDOG_GATE_FILE"', shift)
    wait = staged.index('wait "$WATCHDOG_PID"', gate)
    assert watchdog_start < watchdog_background < shift < gate < wait
    assert "--start-gate-file" in staged[watchdog_start:shift]
    final_baseline = staged.index("start_final_baseline")
    final_shift = staged.rindex("shift_traffic 100")
    assert final_baseline < final_shift

    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "final-watchdog-baseline-us-central1.json" in workflow
    assert '--baseline-input "${RUNNER_TEMP}/final-watchdog-baseline-us-central1.json"' in workflow
    ramp = (ROOT / "scripts/deploy/ramp_secondaries.sh").read_text(encoding="utf-8")
    assert 'watchdog_args+=(--baseline-input "$watchdog_baseline")' in ramp


def test_full_convergence_metrics_are_reported_before_mutex_release() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    rollout = workflow.split("\n  rollout-secondaries:\n", 1)[1].split(
        "\n  public-surface-companion:\n", 1
    )[0]

    report = rollout.index("- name: Report full convergence timing")
    release = rollout.index("- name: Release production deployment mutex", report)
    metric = rollout[report:release]
    assert report < release
    assert "if: ${{ success() }}" in metric
    assert "deploy.full_convergence_seconds=${full_convergence_seconds}" in metric
    assert "primary_live_seconds=${PRIMARY_LIVE_SECONDS}" in metric
    assert '>>"$GITHUB_STEP_SUMMARY"' in metric
    assert "gcloud logging write tr-deploy-metrics" in metric
    assert "--payload-type=json" in metric


def test_mutex_acquire_step_cannot_swallow_a_blocked_exit() -> None:
    """A blocked acquire must fail the step.

    GitHub's default run shell is `bash -e` WITHOUT pipefail, so piping
    acquire into tee makes the step's exit code tee's — a refused lock
    would deploy WITHOUT holding it. The acquire invocation must therefore
    never sit on the left of an unguarded pipe.
    """
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    for line in workflow.splitlines():
        if "deploy_mutex.sh acquire" in line and "|" in line.split("acquire", 1)[1]:
            raise AssertionError(
                "deploy_mutex.sh acquire is piped; a blocked acquire would "
                "exit 0 through the pipe under bash -e without pipefail: " + line.strip()
            )
