import re
from pathlib import Path

from scripts.check_price_coverage import (
    _DISCOVERABLE_MANIFEST_PROVIDERS,
    _GLM_DISCOVERABLE_PROVIDER_APIS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_top_level_deploy_passes_spanner_config_to_unshared_migration() -> None:
    deploy = (ROOT / "scripts/deploy-gcp.sh").read_text()

    assert 'source "${SCRIPT_DIR}/deploy/_lib.sh"' in deploy
    assert 'GCP_PROJECT_ID="$PROJECT_ID" \\\n' in deploy
    assert 'SPANNER_INSTANCE_ID="$SPANNER_INSTANCE_ID" \\\n' in deploy
    assert 'SPANNER_DATABASE_ID="$SPANNER_DATABASE_ID" \\\n' in deploy
    assert 'bash "${SCRIPT_DIR}/deploy/migrate_typed_counters.sh"' in deploy


def test_guarded_deploy_applies_clickhouse_delivery_schemas_before_rollout() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()

    generation = "scripts/deploy/migrate_generation_records.sh --apply"
    provider_outbox = "scripts/deploy/migrate_analytics_outbox.sh"
    operational_outbox = "scripts/deploy/migrate_operational_analytics_outbox.sh"
    rollout = "bash scripts/deploy/rollout.sh"
    assert generation in workflow
    assert provider_outbox in workflow
    assert operational_outbox in workflow
    assert workflow.index(generation) < workflow.index(rollout)
    assert workflow.index(provider_outbox) < workflow.index(rollout)
    assert workflow.index(operational_outbox) < workflow.index(rollout)


def test_deploy_pins_thirty_cent_signup_credit_policy() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert '"TR_SIGNUP_TRIAL_CREDIT_MICRODOLLARS=300000"' in rollout


def test_deploy_removes_only_explicitly_missing_optional_secrets() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    mandatory_block = rollout.split("SECRET_ENVS=(", 1)[1].split(")", 1)[0]
    assert "TR_GOOGLE_ADS_CONVERSION_FEED_PASSWORD" not in mandatory_block
    assert (
        'REMOVE_SECRET_ENVS=("TR_GOOGLE_ADS_CONVERSION_FEED_PASSWORD")' in rollout
    )
    assert "trustedrouter-google-ads-conversion-feed-password" not in rollout
    assert '[[ "$describe_error" == *"NOT_FOUND"* ]]' in rollout
    assert "cannot determine whether optional secret" in rollout
    assert 'REMOVE_SECRET_ENVS+=("${env_name}")' in rollout
    assert 'REMOVE_SECRETS_ARGS=(--remove-secrets ' in rollout
    assert '"${REMOVE_SECRETS_ARGS[@]}"' in rollout


def test_deploy_wires_three_cloud_ops_chat_support_fanout() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    assert "TR_OPS_CHAT_WEBHOOK_URLS=https://a.uptimerouter.com," in rollout
    assert "https://b.trustedrouter.com,https://c.allyrouter.com" in rollout
    assert "trustedrouter-ops-chat-webhook-secret" in rollout
    assert '"OPS_SUPPORT_HOOK_SECRET"' in secrets
    assert (
        'grant_tr_deploy_secret_access "trustedrouter-ops-chat-webhook-secret"'
        in secrets
    )


def test_deploy_serves_user_provided_models() -> None:
    """The switch that lets the gateway authorize a user-provided model.

    It stayed off until BOTH halves existed: settle/refund of the synthetic
    user-model endpoint with its exactly-once payout (#608), and the attested
    enclave that dispatches to the owner's URL. Pinning it here means turning
    it back off is a deliberate edit with a test to update, not a quiet drift.
    """
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert '"TR_USER_MODELS_DISPATCH_ENABLED=true"' in rollout


def test_deploy_wires_veriff_config_and_secrets() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    # A PAIR that must agree: enabling the custom-model gate without a
    # reachable Veriff would 403 every create/edit with an unsatisfiable
    # requirement. Activated together 2026-08-16 (secrets exist); if either
    # ever flips back, the other must flip with it in the same commit.
    veriff_enabled = '"TR_VERIFF_ENABLED=true"' in rollout
    gate_enabled = '"TR_CUSTOM_MODELS_REQUIRE_VERIFICATION=true"' in rollout
    assert veriff_enabled == gate_enabled, (
        "TR_VERIFF_ENABLED and TR_CUSTOM_MODELS_REQUIRE_VERIFICATION must flip together"
    )
    assert veriff_enabled, "identity verification is meant to be live in prod"
    assert '"TR_VERIFF_BASE_URL=https://stationapi.veriff.com"' in rollout
    assert (
        'add_secret_env_if_exists "TR_VERIFF_API_KEY" "trustedrouter-veriff-api-key"'
        in rollout
    )
    assert "TR_VERIFF_SHARED_SECRET_KEY" in rollout
    assert '"VERIFF_API_KEY" "trustedrouter-veriff-api-key"' in secrets
    assert "VERIFF_SHARED_SECRET_KEY" in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-veriff-api-key"' in secrets
    assert (
        'grant_tr_deploy_secret_access "trustedrouter-veriff-shared-secret-key"'
        in secrets
    )


def test_all_attested_control_plane_regions_remain_warm() -> None:
    library = (ROOT / "scripts/deploy/_lib.sh").read_text()
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert (
        'TR_REGIONS="${TR_REGIONS:-us-central1,us-east4,europe-west4,'
        'southamerica-east1}"' in library
    )
    assert (
        'TR_WARM_REGIONS="${TR_WARM_REGIONS:-us-central1,europe-west4,us-east4,'
        'southamerica-east1}"'
        in library
    )
    assert '--min-instances "$min_instances"' in rollout


def test_production_deploy_preserves_allowlisted_regional_quota_canary() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert 'read_primary_env "TR_REGIONAL_QUOTA_LEASES_ENABLED" "false"' in rollout
    assert (
        '"TR_REGIONAL_QUOTA_LEASES_ENABLED=${REGIONAL_QUOTA_LEASES_ENABLED}"'
        in rollout
    )
    assert '"TR_REGIONAL_QUOTA_LEASES_ENABLED=false"' not in rollout
    assert "regional quota canary requires pilot workspaces" in rollout
    assert (
        '"TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"'
        in rollout
    )


def test_production_deploy_provisions_and_schedules_regional_quota_reconciliation() -> None:
    orchestrator = (ROOT / "scripts/deploy-gcp.sh").read_text()
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    library = (ROOT / "scripts/deploy/_lib.sh").read_text()
    provisioner = (ROOT / "scripts/deploy/regional_quota_ledger.sh").read_text()
    reconciler = (ROOT / "scripts/deploy/regional_quota_reconciler.sh").read_text()

    assert 'deploy/regional_quota_ledger.sh' in orchestrator
    assert 'deploy/regional_quota_reconciler.sh' in orchestrator
    assert "bash scripts/deploy/regional_quota_ledger.sh" in workflow
    assert "bash scripts/deploy/regional_quota_reconciler.sh" in workflow
    assert workflow.index("bash scripts/deploy/regional_quota_ledger.sh") < workflow.index(
        "bash scripts/deploy/rollout.sh"
    )
    assert workflow.index("bash scripts/deploy/regional_quota_reconciler.sh") > workflow.index(
        "- name: Roll secondary warm regions sequentially"
    )
    assert "--transactional-writes" in provisioner
    assert "trusted-router-logs-c1" in library
    assert "us-central1=tr-quota-us-central1" in library
    assert "europe-west4=tr-quota-europe-west4" not in library
    assert 'SCHEDULE="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULE:-* * * * *}"' in reconciler
    assert "/v1/internal/gateway/regional-quota/reconcile?limit=250" in reconciler
    assert "x-trustedrouter-internal-token=${internal_token}" in reconciler
    assert "echo \"$internal_token\"" not in reconciler


def test_deploy_preserves_request_record_mode_without_silent_legacy_fallback() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert '--format=json 2>/dev/null' in rollout
    assert 'select(.name == "TR_REQUEST_RECORD_WRITE_MODE")' in rollout
    assert 'cannot determine TR_REQUEST_RECORD_WRITE_MODE' in rollout
    assert '*) REQUEST_RECORD_WRITE_MODE="legacy"' not in rollout
    assert "[?name='TR_REQUEST_RECORD_WRITE_MODE']" not in rollout


def test_deploy_provider_secrets_include_priced_glm52_backends() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    expected = {
        "DEEPINFRA_API_KEY": "trustedrouter-deepinfra-api-key",
        "FIREWORKS_API_KEY": "trustedrouter-fireworks-api-key",
        "NOVITA_API_KEY": "trustedrouter-novita-api-key",
        "BASETEN_API_KEY": "trustedrouter-baseten-api-key",
        "TELNYX_API_KEY": "trustedrouter-telnyx-api-key",
        "THINKING_MACHINES_API_KEY": "trustedrouter-thinking-machines-api-key",
        "WAFER_API_KEY": "trustedrouter-wafer-api-key",
        "CRUSOE_API_KEY": "trustedrouter-crusoe-api-key",
        "MAKORA_API_KEY": "trustedrouter-makora-api-key",
    }
    for env_name, secret_name in expected.items():
        assert f'add_secret_env_if_exists "{env_name}" "{secret_name}"' in rollout
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets


def test_deploy_prefers_explicit_google_ai_studio_key() -> None:
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    assert (
        'ensure_secret_from_env_file "GOOGLE_AI_STUDIO_KEY" '
        '"trustedrouter-gemini-api-key" "GEMINI_API_KEY"'
    ) in secrets
    assert 'for alias in "$@"; do' in secrets
    assert 'value="${!alias:-}"' in secrets


def test_deploy_wires_athena_worker_prompt_secret() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    assert "ATHENA_PROMPTS_FILE" in secrets
    assert (
        'ensure_secret_from_prompt_file "trustedrouter-athena-worker-prompt-v1" '
        '"$ATHENA_PROMPTS_FILE" "Worker Prompt V1"'
    ) in secrets
    assert (
        'add_secret_env_if_exists "TR_ATHENA_WORKER_PROMPT" "trustedrouter-athena-worker-prompt-v1"'
    ) in rollout


def test_hourly_kimi_discovery_has_narrow_secret_access_wiring() -> None:
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text()

    assert 'grant_tr_deploy_secret_access "trustedrouter-kimi-api-key"' in secrets
    assert "KIMI_API_KEY:trustedrouter-kimi-api-key" in workflow
    assert "no project-wide Secret" in workflow


def test_hourly_cloudflare_discovery_uses_funded_account() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text()

    assert "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID=2698c706fd4793c818af14adad4e1a39" in workflow
    assert "TR_CLOUDFLARE_WORKERS_AI_ROUTABLE=1" in workflow
    assert "96781cbfaebf2b28d851b9c677dd2e81" not in workflow


def test_every_authenticated_discovery_feed_is_wired_to_narrow_secret_access() -> None:
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    workflow_pairs = dict(
        re.findall(r"\b([A-Z][A-Z0-9_]+):(trustedrouter-[a-z0-9-]+)", workflow)
    )
    # Together is intentionally loaded in its own documented workflow step.
    workflow_pairs["TOGETHER_API_KEY"] = "trustedrouter-together-api-key"

    feeds = [
        (provider, env_names)
        for provider, _url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
        if env_names
    ]
    feeds.extend(
        (provider, env_names)
        for provider, _url, env_names in _GLM_DISCOVERABLE_PROVIDER_APIS
    )

    for provider, env_names in feeds:
        wired_env = next((name for name in env_names if name in workflow_pairs), None)
        assert wired_env is not None, (
            f"{provider} discovery requires one of {env_names}, but the hourly "
            "workflow loads none of them"
        )
        secret_name = workflow_pairs[wired_env]
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets, (
            f"{provider} discovery loads {secret_name}, but secrets.sh does not "
            "grant the refresh service account narrow access"
        )


def test_provider_portal_uses_private_vpc_and_dedicated_clickhouse_reader() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    reader = (
        ROOT / "scripts/deploy/clickhouse_provider_reader.sh"
    ).read_text(encoding="utf-8")

    assert "compute addresses describe tr-clickhouse-ilb" in rollout
    assert 'PROVIDER_ANALYTICS_CLICKHOUSE_URL="http://${clickhouse_ilb_ip}:8123"' in rollout
    assert 'PROVIDER_ANALYTICS_CLICKHOUSE_URL="http://10.128.15.214:8123"' in rollout
    assert "TR_PROVIDER_ANALYTICS_CLICKHOUSE_USER=tr_provider_read" in rollout
    assert "--vpc-egress private-ranges-only" in rollout
    assert '--network "${TR_CLOUD_RUN_NETWORK:-default}"' in rollout
    assert '--subnet "${TR_CLOUD_RUN_SUBNET:-default}"' in rollout

    for content in (secrets, rollout, reader):
        assert "trustedrouter-clickhouse-provider-read-password" in content

    assert "<readonly>1</readonly>" in reader
    assert "<max_execution_time>60</max_execution_time>" in reader
    assert "<max_memory_usage>536870912</max_memory_usage>" in reader
    assert "<max_result_bytes>536870912</max_result_bytes>" in reader
    assert "GRANT SELECT ON tr.provider_benchmark_samples" in reader
    assert "GRANT SELECT ON tr.provider_analytics_hourly" in reader
    assert "GRANT SELECT ON tr.provider_analytics_daily" in reader
    assert "GRANT SELECT ON tr.provider_analytics_monthly" in reader
    assert "refusing configuration: $NAME has external IP" in reader
    assert (
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD: "
        "${{ secrets.TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD }}" in workflow
    )
    assert '"trustedrouter-clickhouse-provider-read-password"' in workflow
    assert "TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD is required" in workflow
