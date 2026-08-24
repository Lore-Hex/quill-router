from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_load_balanced_control_plane_region_is_staged() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    start = workflow.index("- name: Warm secondaries in parallel, ramp serially")
    end = workflow.index("- name: Deploy synthetic monitor Cloud Run Job", start)
    rollout = workflow[start:end]

    assert "deploy_cold_regions:" not in workflow
    assert "steps.optional.outputs.deploy_cold_regions" not in workflow
    assert "regions=(europe-west4 us-east4 southamerica-east1)" in rollout
    assert 'TR_DEPLOY_TARGET_REGIONS="${region}"' in rollout


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


def test_warm_secondary_regions_warm_in_parallel_then_ramp_serially() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    primary_canary = workflow.index("- name: Canary gate — watch us-central1 only")
    start = workflow.index("- name: Warm secondaries in parallel, ramp serially")
    end = workflow.index("- name: Deploy synthetic monitor Cloud Run Job", start)
    rollout = workflow[start:end]

    assert primary_canary < start
    assert "regions=(europe-west4 us-east4 southamerica-east1)" in rollout
    assert "PREV_SOUTHAMERICA_EAST1" in rollout
    assert "southamerica-east1)" in rollout
    assert 'if ! TR_DEPLOY_TARGET_REGIONS="${region}"' in rollout
    assert 'if ! bash scripts/deploy/staged_traffic.sh \\' in rollout
    assert 'if ! active_traffic="$(gcloud run services describe' in rollout
    assert '[ "${active_traffic}" != "1" ]' in rollout
    first_wait = rollout.index('if wait "${pids[$idx]}"; then')
    for region, log_index in (
        ("europe-west4", 0),
        ("us-east4", 1),
        ("southamerica-east1", 2),
    ):
        invocation = (
            f'(warm_secondary "{region}" "${{revision_files[{log_index}]}}") '
            f'>"${{logs[{log_index}]}}" 2>&1 &'
        )
        assert rollout.index(invocation) < first_wait
    assert rollout.count('pids+=("$!")') == 3
    assert 'printf \'\\n=== %s ===\\n\' "${regions[$idx]}"' in rollout
    assert 'cat "${logs[$idx]}"' in rollout
    assert 'if [ "$failed" -ne 0 ]; then' in rollout
    assert 'rollback_region "${region}"' in rollout
    assert 'TR_DEPLOY_RECONCILE_LB: "0"' in rollout
    assert "TR_DEPLOY_MUTEX_OPERATION is inherited by every warmup" in rollout
    assert "#695 (billing 5xx, 2026-08-20)" in rollout
    assert (
        "overlapping revision warmups and traffic ramps can amplify billing\n"
        "          # transaction contention"
    ) in rollout
    assert "assert_no_billing_5xx.sh" in rollout
    assert "--slo-class router_core" in rollout

    warmup_failure = rollout.index(
        "Secondary warmup failed; no secondary traffic moved"
    )
    ramp_eu = rollout.index('ramp_secondary "europe-west4"')
    ramp_us = rollout.index('ramp_secondary "us-east4"')
    ramp_sa = rollout.index('ramp_secondary "southamerica-east1"')
    assert first_wait < warmup_failure < ramp_eu < ramp_us < ramp_sa
    assert "Later regions remain warm at zero traffic and never received traffic" in rollout

    staged_call = rollout.index("bash scripts/deploy/staged_traffic.sh")
    staged_line = rollout[staged_call : rollout.index("\n", staged_call)]
    assert "&" not in staged_line
    ramp_start = rollout.index("ramp_secondary()")
    staged_call = rollout.index("bash scripts/deploy/staged_traffic.sh", ramp_start)
    stamp = rollout.index(
        'rollout_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"',
        ramp_start,
    )
    billing_gate = rollout.index("assert_no_billing_5xx.sh", ramp_start)
    assert ramp_start < stamp < staged_call < billing_gate
    assert "secondary-started-" not in rollout


def test_primary_rollout_gates_on_router_core_and_billing_path_errors() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    primary = workflow.index("- name: Deploy us-central1 (no-traffic)")
    secondary = workflow.index("- name: Warm secondaries in parallel, ramp serially")
    rollout = workflow[primary:secondary]

    assert "TR_WATCHDOG_SLO_CLASS: router_core" in rollout
    assert "--slo-class router_core" in rollout
    assert "- name: Billing-path gate + rollback (us-central1)" in rollout
    assert "scripts/deploy/assert_no_billing_5xx.sh" in rollout
    assert '"${{ steps.new_us.outputs.revision }}"' in rollout
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
    end = workflow.index("- name: Detect optional deploy surfaces", start)
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

    assert workflow.count('TR_DEPLOY_RECONCILE_LB: "1"') == 1
    assert workflow.count('TR_DEPLOY_RECONCILE_LB: "0"') == 1
