from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TR_", env_file=".env", extra="ignore")

    environment: str = "local"
    release: str = "local"
    service_name: str = "trusted-router"
    api_base_url: str = "https://api.trustedrouter.com/v1"
    trusted_domain: str = "trustedrouter.com"
    # Additional first-party control-plane domains. They serve the same
    # application without redirecting to the canonical domain, so they remain
    # usable as independent operational aliases. SEO canonicals still point at
    # ``trusted_domain``. The corresponding inference hostname is derived as
    # ``api.<alias>/v1`` and still terminates inside the attested gateway.
    trusted_domain_aliases: str = "allyrouter.com,uptimerouter.com"
    legal_entity_name: str = "Lore Hex Corp"
    legal_entity_type: str = "Delaware C Corporation"
    legal_entity_address: str = "1111 Brickell Ave, Floor 10, Miami, FL 33131"
    legal_entity_phone: str = "+1-305-239-7350"
    legal_entity_ein: str = "41-5339728"
    legal_entity_duns: str = "144992055"
    legal_entity_date_established: str | None = None
    legal_signatory_name: str = "Joseph Perla"
    legal_signatory_title: str = "CEO"
    security_contact_email: str = "security@trustedrouter.com"

    enable_live_providers: bool = False
    local_keys_file: Path = Path("~/.quill_cloud_keys.private").expanduser()
    storage_backend: str = "memory"
    # Postgres-wire system of record for the non-GCP deployments. One
    # implementation covers Azure (Flexible Server / Citus), AWS (Aurora DSQL)
    # and Spanner's PostgreSQL dialect — see
    # docs/storage-portability/multi-cloud-separation.md.
    postgres_dsn: str = ""
    # Aurora DSQL passwords are short-lived IAM tokens. ``aws-dsql`` refreshes
    # the token for every physical pool connection; empty preserves ordinary
    # static-password Postgres behaviour.
    postgres_iam_auth: str = ""
    # Normally inferred from ``<id>.dsql.<region>.on.aws``. This override is
    # available for endpoints whose hostname does not encode the AWS region.
    postgres_iam_region: str = ""
    gcp_project_id: str = "quill-cloud-proxy"
    spanner_instance_id: str | None = None
    spanner_database_id: str | None = None
    bigtable_instance_id: str | None = None
    bigtable_generation_table: str = "trustedrouter-generations"
    # Migration controls. ``spanner-clickhouse`` never constructs a Bigtable
    # client. The mirror flag lets ``spanner-bigtable`` stop new writes before
    # the final backend switch while retaining legacy reads during soak.
    bigtable_mirror_writes_enabled: bool = True
    generation_records_enabled: bool = False
    # Legacy in-process ClickHouse mirror. Empty URL keeps it disabled.
    clickhouse_url: str = ""
    clickhouse_user: str = ""
    clickhouse_password: str = ""
    clickhouse_benchmark_table: str = "provider_benchmark_samples"
    # Private, read-only provider portal connection. This intentionally uses a
    # separate ClickHouse account from ingestion and is reachable only through
    # the service's VPC egress path.
    provider_analytics_clickhouse_url: str = ""
    provider_analytics_clickhouse_user: str = "tr_provider_read"
    provider_analytics_clickhouse_password: str = ""
    provider_analytics_clickhouse_database: str = "tr"
    provider_analytics_clickhouse_table: str = "provider_benchmark_samples"
    operational_analytics_clickhouse_url: str = ""
    operational_analytics_clickhouse_user: str = "tr_control_read"
    operational_analytics_clickhouse_password: str = ""
    operational_analytics_clickhouse_database: str = "tr"
    # dual returns Bigtable and compares ClickHouse; clickhouse reverses those
    # roles for the second soak; clickhouse-only never calls Bigtable.
    analytics_read_mode: str = "bigtable"
    analytics_dual_read_grace_seconds: int = 30
    analytics_dual_read_started_at: str = ""
    analytics_clickhouse_primary_started_at: str = ""
    # Stage 1 live analytics outbox. This is intentionally off by default and
    # must remain off until the shadow ingester and reconciler are observed.
    analytics_outbox_enabled: bool = False
    # Durable tenant-activity and synthetic-status stream. Kept independent
    # from provider analytics so its privacy and cutover can be controlled.
    operational_analytics_outbox_enabled: bool = False

    # Starter credit granted exactly once with a new email/OAuth account's
    # first workspace. Wallet-only and secondary workspaces receive no grant.
    # $0.10 = 100,000 microdollars.
    signup_trial_credit_microdollars: int = 100_000

    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.05
    sentry_local_enabled: bool = False

    # Axiom log shipping. Token + org-id read from env at startup
    # (`AXIOM_API_TOKEN`, `AXIOM_ORG_ID`) since those names match
    # axiom-py / axiom-cli conventions; dataset is plain config so it
    # can be overridden per environment (e.g. `staging-trusted-router`).
    # Empty token at startup → handler is not registered (graceful no-op).
    axiom_dataset: str = "trusted-router-logs"
    axiom_url: str = "https://api.axiom.co"
    # Levels at and above this go to Axiom. This sets both the Axiom
    # handler threshold and the `trusted_router` package logger level;
    # root stays at uvicorn's WARNING, so third-party loggers are
    # unaffected and their INFO does not ship. INFO captures rate-limit
    # decisions, structured business events, and the Bigtable swallowed-
    # error log lines we just enriched. DEBUG would flood; ERROR alone
    # would miss the request_id correlation in 429s.
    axiom_log_level: str = "INFO"
    # Google Ads Data Manager pulls a metadata-only CSV over authenticated
    # HTTPS. The password is optional so local/test installs stay simple; when
    # absent the feed route is an intentional 404.
    google_ads_conversion_feed_username: str = "trustedrouter-data-manager"
    google_ads_conversion_feed_password: str | None = None
    google_ads_conversion_feed_retention_days: int = 90
    google_ads_conversion_feed_max_rows: int = 100_000
    # Direct server-to-server Google Ads Data Manager reporting. The control
    # plane leaves this disabled; a scheduled Cloud Run job enables it and
    # uploads only metadata-only signup and settled-purchase rows.
    google_data_manager_enabled: bool = False
    google_data_manager_account_id: str | None = None
    google_data_manager_login_account_id: str | None = None
    google_data_manager_signup_action_id: str | None = None
    google_data_manager_activated_action_id: str | None = None
    google_data_manager_purchase_action_id: str | None = None
    google_data_manager_batch_size: int = 500
    google_data_manager_lease_seconds: int = 300
    google_data_manager_max_attempts: int = 8
    google_data_manager_timeout_seconds: float = 20.0
    google_data_manager_repair_lookback_days: int = 90
    enable_sentry_test_route: bool = False
    sentry_floodgate_enabled: bool = True
    sentry_floodgate_window_seconds: int = 60 * 60
    sentry_floodgate_max_events_per_fingerprint: int = 3
    sentry_floodgate_max_events_per_window: int = 50
    sentry_floodgate_max_fingerprints: int = 2048

    trust_gcp_source_commit: str | None = None
    trust_gcp_image_reference: str | None = None
    trust_gcp_image_digest: str | None = None
    # Production control-plane trust aliases resolve the current gateway
    # release from this canonical, independently published record. The
    # deploy-time values above remain the offline/local fallback.
    trust_gcp_release_url: str = ""
    # Independently hosted copies keep the backup-domain trust pages live when
    # the canonical DNS zone is unavailable. Every fetched record is subjected
    # to the same strict release validation before it enters the cache.
    trust_gcp_release_fallback_urls: str = ""

    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_ip_per_window: int = 240
    rate_limit_key_per_window: int = 1200
    rate_limit_internal_per_window: int = 6000

    internal_gateway_token: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_secret_key: str | None = None
    # Standard US Stripe processing schedules. These are grossed up against
    # the complete charge so the requested credit principal remains intact.
    # They are explicit config because negotiated Stripe pricing can differ.
    stripe_card_fee_basis_points: int = 290
    stripe_card_fee_fixed_cents: int = 30
    stripe_stablecoin_fee_basis_points: int = 150
    stripe_stablecoin_fee_fixed_cents: int = 0
    stripe_ach_fee_basis_points: int = 80
    stripe_ach_fee_fixed_cents: int = 0
    stripe_ach_fee_max_cents: int = 500
    paypal_client_id: str | None = None
    paypal_client_secret: str | None = None
    paypal_webhook_id: str | None = None
    paypal_api_base_url: str = "https://api-m.paypal.com"
    bootstrap_management_key: str | None = None
    byok_kms_key_name: str | None = None
    byok_envelope_key_b64: str | None = None
    byok_envelope_key_ref: str = "trustedrouter/byok-envelope-key/v1"
    # BYOK key REGISTRATION wraps the secret with the byok-envelope KMS key.
    # Production is GCP-primary and encrypt-enabled; set False on any future
    # read-only replica so registration is refused cleanly.
    byok_registration_enabled: bool = True

    auth_session_ttl_seconds: int = 60 * 60 * 24 * 30
    oauth_authorization_code_ttl_seconds: int = 10 * 60

    # OAuth providers — independently optional. Each is enabled iff both
    # client_id and client_secret are present. Routes 404 if disabled.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_oauth_redirect_url: str | None = None
    # Backup domains use independent provider credentials so login remains
    # available even if the canonical domain or its OAuth app is unavailable.
    # Each provider has its own Secret Manager value so credentials can rotate
    # independently without exposing one provider while updating the other.
    google_alias_credentials_json: str = "{}"
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_oauth_redirect_url: str | None = None
    # GitHub OAuth Apps permit exactly one callback URL. Each independent
    # first-party domain therefore has its own app credentials, supplied as a
    # single Secret Manager JSON value:
    # {"allyrouter.com":{"client_id":"...","client_secret":"..."}}
    github_alias_credentials_json: str = "{}"
    # MetaMask uses public-key crypto (no shared secret). The SIWE message
    # carries this domain so wallet UIs show the right hostname.
    siwe_domain: str | None = None

    # Amazon SES — used for optional wallet email attach/verification.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "us-east-1"
    ses_from_email: str | None = None
    ses_from_name: str = "TrustedRouter"
    # Configuration set used on every SendEmail call so SES emits bounce +
    # complaint events to our SNS topic (subscribed at /internal/ses/notifications).
    ses_configuration_set: str | None = "trustedrouter-default"
    # Destination for TrustedOS partner-inquiry form submissions (/trustedos).
    # Falls back to ses_from_email when unset so the lead never silently drops.
    partner_inquiry_email: str | None = None

    stablecoin_checkout_enabled: bool = True
    x402_enabled: bool = False
    x402_allow_mock_payments: bool = False
    x402_network: str = "base"
    x402_network_id: str = "eip155:8453"
    x402_stripe_api_version: str = "2026-03-04.preview"
    x402_max_fund_dollars: str = "500"
    x402_rate_limit_window_seconds: int = 60
    x402_rate_limit_key_per_window: int = 10
    x402_rate_limit_workspace_per_window: int = 30
    x402_settle_rate_limit_per_window: int = 30
    x402_settle_workspace_per_window: int = 120
    multi_region_enabled: bool = True
    # Regional quota leases are a future latency optimization for prepaid
    # authorization. The state machine and design are intentionally dark: the
    # exact typed Spanner counters remain the only production authority until
    # a durable regional ledger and reconciliation worker have passed the
    # rollout gates in docs/design/regional-quota-leases.md.
    regional_quota_leases_enabled: bool = False
    regional_quota_lease_pilot_workspace_ids: str = ""
    regional_quota_lease_ttl_seconds: int = 60
    regional_quota_lease_max_microdollars: int = 10_000_000
    regional_quota_lease_max_available_basis_points: int = 1_000
    # Operational read-only flag. When set, write paths (credit
    # reservations, gateway authorize, signup, etc.) return 503 with
    # `Retry-After`; reads keep working. Used for the Spanner →
    # nam6 migration and any future maintenance window that needs
    # writes paused without dropping connections. Off in production
    # by default; flipped via `gcloud run services update --update-env-vars
    # TR_READ_ONLY=1` per region during a planned cutover. See the
    # multi-region expansion plan for the cutover sequence.
    read_only: bool = False
    # Durable settle outbox (docs/design/durable-settle-outbox.md). Default OFF:
    # the settle path is byte-identical and nothing is enqueued. When True, the
    # settle handler durably records each settle intent before applying it, and
    # the /internal/gateway/settle-outbox/drain worker recovers any that were
    # lost so the reaper never releases a completed request for free. Enabling is
    # a billing prod-behavior change — flip deliberately, per the design's
    # rollout section, after the shadow metrics are clean.
    settle_outbox_enabled: bool = False
    # Expand/contract switch for per-request Spanner records. ``legacy`` keeps
    # writing gateway authorizations and generation repair rows to tr_entities
    # while every region learns to read the typed table. ``typed`` moves new
    # authorizations to tr_gateway_authorization and uses the settle outbox as
    # the bounded repair source for Bigtable activity metadata.
    request_record_write_mode: str = "legacy"
    # Bigtable application profile name. The default profile uses
    # single-cluster routing; `tr-multi` enables
    # multi-cluster-routing-use-any once we have ≥3 BT clusters
    # provisioned. Settable via env var so we can roll out the
    # change region-by-region without re-deploying code.
    bigtable_app_profile_id: str = ""
    # Local/test drains broadcast jobs opportunistically after settlement so
    # tests and demos are deterministic. Production should leave this false:
    # settlement enqueues durable jobs and a separate internal worker drains
    # them via /v1/internal/broadcast/drain.
    broadcast_inline_drain_enabled: bool = False
    # Attested gateway regions. Each entry is a Confidential Space VM
    # that terminates TLS *inside the enclave* — the trust property the
    # product is sold on (no third party ever sees prompt plaintext, not
    # even GCP edge). VMs run 24/7 (~$144/mo each), so we only enumerate
    # regions where we've actually deployed a VM. Adding a region here
    # without an actual VM in that region is dishonest — the cert SAN
    # mismatch breaks TLS and the attestation page lies.
    regions: str = "us-central1,us-east4,europe-west4"
    marketing_regions: str = (
        "us-central1,europe-west4,us-east4,"
        "asia-northeast1,asia-east2,asia-southeast1,"
        "southamerica-east1,"
        # Standalone deployments on other clouds (multi-cloud-separation.md).
        "aws-eu-west-1,aws-eu-north-1,azure-australiaeast"
    )
    # Non-GCP deployments that have PASSED their own end-to-end smoke
    # (verify_deployment.sh --expect-monitor). Listing here turns the map dot
    # from "staged" to "live", so an entry is a factual claim, not decoration.
    # Stockholm (aws-eu-north-1) is deliberately absent: it replicates the
    # AWS-EU database but has no compute yet.
    external_live_regions: str = "aws-eu-west-1,azure-australiaeast"
    primary_region: str = "us-central1"
    regional_api_hostname_template: str = "api-{region}.quillrouter.com"
    synthetic_monitor_region: str | None = None
    synthetic_monitor_api_key: str | None = None
    synthetic_monitor_model: str = "trustedrouter/monitor"
    # Per-probe HTTP timeout for real provider-effective synthetic checks.
    # Keep this aligned with the gateway's first-byte budget. A successful
    # /responses probe in europe-west4 can legitimately take >10s on slow
    # cheap monitor routes, so 10s creates false downtime.
    synthetic_monitor_timeout_seconds: float = 20.0
    synthetic_status_sample_limit: int = 5000
    synthetic_status_raw_retention_days: int = 14
    synthetic_status_rollup_retention_months: int = 24
    synthetic_status_us_url: str = "https://status-us.trustedrouter.com/status.json"
    synthetic_status_eu_url: str = "https://status-eu.trustedrouter.com/status.json"
    # IDs follow OpenRouter naming exactly to line up with the ingest
    # snapshot — `moonshotai/...` not `kimi/...`, `mistralai/...` not
    # `mistral/...`, `meta-llama/...` for Cerebras-served Llama, etc.
    auto_model_order: str = (
        "anthropic/claude-opus-4.7,anthropic/claude-sonnet-4.6,"
        "openai/gpt-4.1-mini,google/gemini-2.5-flash,"
        "deepseek/deepseek-v4-flash,minimax/minimax-m3,moonshotai/kimi-k2.6,"
        "mistralai/mistral-small-2603,z-ai/glm-4.6"
    )

    max_request_body_bytes: int = 4 * 1024 * 1024

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = priority (highest first). Init kwargs and env vars still
        # win; the local key file is a fallback when neither is set, before
        # built-in defaults take over.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _LocalKeyFileSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def production_is_fail_closed(self) -> Settings:
        environment = self.environment.lower()
        if self.signup_trial_credit_microdollars < 0:
            raise ValueError("TR_SIGNUP_TRIAL_CREDIT_MICRODOLLARS cannot be negative")
        for name, value in (
            ("TR_STRIPE_CARD_FEE_BASIS_POINTS", self.stripe_card_fee_basis_points),
            (
                "TR_STRIPE_STABLECOIN_FEE_BASIS_POINTS",
                self.stripe_stablecoin_fee_basis_points,
            ),
            ("TR_STRIPE_ACH_FEE_BASIS_POINTS", self.stripe_ach_fee_basis_points),
        ):
            if not 0 <= value < 10_000:
                raise ValueError(f"{name} must be between 0 and 9999")
        for name, value in (
            ("TR_STRIPE_CARD_FEE_FIXED_CENTS", self.stripe_card_fee_fixed_cents),
            (
                "TR_STRIPE_STABLECOIN_FEE_FIXED_CENTS",
                self.stripe_stablecoin_fee_fixed_cents,
            ),
            ("TR_STRIPE_ACH_FEE_FIXED_CENTS", self.stripe_ach_fee_fixed_cents),
            ("TR_STRIPE_ACH_FEE_MAX_CENTS", self.stripe_ach_fee_max_cents),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 1 <= self.google_ads_conversion_feed_retention_days <= 90:
            raise ValueError(
                "TR_GOOGLE_ADS_CONVERSION_FEED_RETENTION_DAYS must be between 1 and 90"
            )
        if self.request_record_write_mode not in {"legacy", "typed"}:
            raise ValueError(
                "TR_REQUEST_RECORD_WRITE_MODE must be 'legacy' or 'typed'"
            )
        if self.analytics_read_mode not in {
            "bigtable",
            "dual",
            "clickhouse",
            "clickhouse-only",
        }:
            raise ValueError(
                "TR_ANALYTICS_READ_MODE must be bigtable, dual, clickhouse, "
                "or clickhouse-only"
            )
        if self.analytics_dual_read_grace_seconds < 0:
            raise ValueError("TR_ANALYTICS_DUAL_READ_GRACE_SECONDS cannot be negative")
        if not 5 <= self.regional_quota_lease_ttl_seconds <= 300:
            raise ValueError(
                "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS must be between 5 and 300"
            )
        if self.regional_quota_lease_max_microdollars <= 0:
            raise ValueError(
                "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS must be positive"
            )
        if not 1 <= self.regional_quota_lease_max_available_basis_points <= 5_000:
            raise ValueError(
                "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS must be "
                "between 1 and 5000"
            )
        if self.regional_quota_leases_enabled:
            if environment not in {"local", "test"}:
                raise ValueError(
                    "TR_REGIONAL_QUOTA_LEASES_ENABLED is not production-ready; "
                    "the durable regional ledger and reconciliation gates are incomplete"
                )
            if not self.regional_quota_lease_pilot_workspace_ids.strip():
                raise ValueError(
                    "TR_REGIONAL_QUOTA_LEASES_ENABLED requires "
                    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS"
                )
        if self.google_ads_conversion_feed_max_rows < 1:
            raise ValueError("TR_GOOGLE_ADS_CONVERSION_FEED_MAX_ROWS must be positive")
        if not 1 <= self.google_data_manager_batch_size <= 2_000:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_BATCH_SIZE must be between 1 and 2000"
            )
        if self.google_data_manager_lease_seconds < 30:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_LEASE_SECONDS must be at least 30"
            )
        if not 1 <= self.google_data_manager_max_attempts <= 20:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_MAX_ATTEMPTS must be between 1 and 20"
            )
        if not 1.0 <= self.google_data_manager_timeout_seconds <= 120.0:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_TIMEOUT_SECONDS must be between 1 and 120"
            )
        if not 1 <= self.google_data_manager_repair_lookback_days <= 90:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_REPAIR_LOOKBACK_DAYS must be between 1 and 90"
            )
        if self.google_data_manager_enabled:
            missing_google_data_manager = [
                name
                for name, value in (
                    (
                        "TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID",
                        self.google_data_manager_account_id,
                    ),
                    (
                        "TR_GOOGLE_DATA_MANAGER_SIGNUP_ACTION_ID",
                        self.google_data_manager_signup_action_id,
                    ),
                    (
                        "TR_GOOGLE_DATA_MANAGER_ACTIVATED_ACTION_ID",
                        self.google_data_manager_activated_action_id,
                    ),
                    (
                        "TR_GOOGLE_DATA_MANAGER_PURCHASE_ACTION_ID",
                        self.google_data_manager_purchase_action_id,
                    ),
                )
                if not value
            ]
            if missing_google_data_manager:
                raise ValueError(
                    "Google Data Manager is enabled but missing "
                    + ", ".join(missing_google_data_manager)
                )
        if ":" in self.google_ads_conversion_feed_username:
            raise ValueError("TR_GOOGLE_ADS_CONVERSION_FEED_USERNAME cannot contain ':'")
        if (
            self.google_ads_conversion_feed_password is not None
            and len(self.google_ads_conversion_feed_password) < 32
        ):
            raise ValueError(
                "TR_GOOGLE_ADS_CONVERSION_FEED_PASSWORD must contain at least 32 characters"
            )
        if self.x402_allow_mock_payments and environment not in {"local", "test"}:
            raise ValueError("TR_X402_ALLOW_MOCK_PAYMENTS is only allowed in local/test")
        if (
            self.x402_enabled
            and environment not in {"local", "test"}
            and (not self.stripe_secret_key or not self.stripe_webhook_secret)
        ):
            raise ValueError(
                "TR_X402_ENABLED requires TR_STRIPE_SECRET_KEY and "
                "TR_STRIPE_WEBHOOK_SECRET outside local/test"
            )
        if environment != "production":
            return self
        missing = []
        if not self.internal_gateway_token:
            missing.append("TR_INTERNAL_GATEWAY_TOKEN")
        if not self.stripe_webhook_secret:
            missing.append("TR_STRIPE_WEBHOOK_SECRET")
        if not self.stripe_secret_key:
            missing.append("TR_STRIPE_SECRET_KEY")
        if not self.sentry_dsn:
            missing.append("TR_SENTRY_DSN")
        if self.bootstrap_management_key:
            missing.append("unset TR_BOOTSTRAP_MANAGEMENT_KEY")
        if self.storage_backend == "memory":
            missing.append("TR_STORAGE_BACKEND=spanner-bigtable or spanner-clickhouse")
        if self.storage_backend in {"spanner-bigtable", "spanner-clickhouse"}:
            if not self.spanner_instance_id:
                missing.append("TR_SPANNER_INSTANCE_ID")
            if not self.spanner_database_id:
                missing.append("TR_SPANNER_DATABASE_ID")
        if self.storage_backend == "spanner-bigtable":
            if not self.bigtable_instance_id:
                missing.append("TR_BIGTABLE_INSTANCE_ID")
        if self.storage_backend == "spanner-clickhouse":
            if self.analytics_read_mode != "clickhouse-only":
                missing.append(
                    "TR_ANALYTICS_READ_MODE=clickhouse-only with "
                    "TR_STORAGE_BACKEND=spanner-clickhouse"
                )
            if not self.generation_records_enabled:
                missing.append("TR_GENERATION_RECORDS_ENABLED=true")
            if not self.operational_analytics_outbox_enabled:
                missing.append("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true")
            if not self.analytics_outbox_enabled:
                missing.append("TR_ANALYTICS_OUTBOX_ENABLED=true")
            if self.bigtable_mirror_writes_enabled:
                missing.append("TR_BIGTABLE_MIRROR_WRITES_ENABLED=false")
            if self.request_record_write_mode != "typed":
                missing.append("TR_REQUEST_RECORD_WRITE_MODE=typed")
        if self.analytics_read_mode != "bigtable":
            if not self.operational_analytics_clickhouse_url:
                missing.append("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL")
            if not self.operational_analytics_clickhouse_password:
                missing.append("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD")
        if self.request_record_write_mode == "typed" and not self.settle_outbox_enabled:
            missing.append(
                "TR_SETTLE_OUTBOX_ENABLED=true when "
                "TR_REQUEST_RECORD_WRITE_MODE=typed"
            )
        if not self.byok_kms_key_name:
            missing.append("TR_BYOK_KMS_KEY_NAME")
        if not self.trust_gcp_release_url:
            self.trust_gcp_release_url = (
                "https://trust.trustedrouter.com/trust/gcp-release.json"
            )
        elif not self.trust_gcp_release_url.startswith("https://"):
            missing.append("TR_TRUST_GCP_RELEASE_URL=https://...")
        invalid_release_fallbacks = [
            url for url in self.trust_gcp_release_fallback_url_list
            if not url.startswith("https://")
        ]
        if invalid_release_fallbacks:
            missing.append("TR_TRUST_GCP_RELEASE_FALLBACK_URLS must contain HTTPS URLs")
        # OAuth providers are independently optional in production. We DO
        # enforce that no provider is half-configured: a client_id without
        # the matching client_secret would cause silent runtime failures.
        if bool(self.google_client_id) != bool(self.google_client_secret):
            missing.append("TR_GOOGLE_CLIENT_ID and TR_GOOGLE_CLIENT_SECRET must both be set or both unset")
        if bool(self.github_client_id) != bool(self.github_client_secret):
            missing.append("TR_GITHUB_CLIENT_ID and TR_GITHUB_CLIENT_SECRET must both be set or both unset")
        configured_aliases = {
            value.strip().lower().rstrip(".")
            for value in self.trusted_domain_aliases.split(",")
            if value.strip()
        }
        for provider, canonical_enabled, raw_credentials in (
            ("GOOGLE", self.google_oauth_enabled, self.google_alias_credentials_json),
            ("GITHUB", self.github_oauth_enabled, self.github_alias_credentials_json),
        ):
            setting_name = f"TR_{provider}_ALIAS_CREDENTIALS_JSON"
            try:
                alias_credentials = _parse_oauth_alias_credentials(
                    raw_credentials,
                    setting_name,
                )
            except ValueError as exc:
                missing.append(str(exc))
                alias_credentials = {}
            unknown_oauth_aliases = sorted(set(alias_credentials) - configured_aliases)
            if unknown_oauth_aliases:
                missing.append(
                    f"{setting_name} contains unconfigured domain(s): "
                    + ", ".join(unknown_oauth_aliases)
                )
            if canonical_enabled:
                missing_oauth_aliases = sorted(
                    configured_aliases - set(alias_credentials)
                )
                if missing_oauth_aliases:
                    missing.append(
                        f"{setting_name} is missing configured domain(s): "
                        + ", ".join(missing_oauth_aliases)
                    )
        paypal_fields = [
            self.paypal_client_id,
            self.paypal_client_secret,
            self.paypal_webhook_id,
        ]
        if any(paypal_fields) and not all(paypal_fields):
            missing.append(
                "TR_PAYPAL_CLIENT_ID, TR_PAYPAL_CLIENT_SECRET, and TR_PAYPAL_WEBHOOK_ID "
                "must all be set or all unset"
            )
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"production configuration is not fail-closed: {joined}")
        return self

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def trust_gcp_release_fallback_url_list(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value.strip()
                for value in self.trust_gcp_release_fallback_urls.split(",")
                if value.strip()
            )
        )

    @property
    def google_alias_credentials(self) -> dict[str, tuple[str, str]]:
        return _parse_oauth_alias_credentials(
            self.google_alias_credentials_json,
            "TR_GOOGLE_ALIAS_CREDENTIALS_JSON",
        )

    @property
    def github_alias_credentials(self) -> dict[str, tuple[str, str]]:
        return _parse_oauth_alias_credentials(
            self.github_alias_credentials_json,
            "TR_GITHUB_ALIAS_CREDENTIALS_JSON",
        )

    @property
    def paypal_enabled(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def regional_quota_lease_pilot_workspaces(self) -> frozenset[str]:
        return frozenset(
            workspace_id.strip()
            for workspace_id in self.regional_quota_lease_pilot_workspace_ids.split(",")
            if workspace_id.strip()
        )

    @property
    def ses_enabled(self) -> bool:
        return bool(self.aws_access_key_id and self.aws_secret_access_key and self.ses_from_email)


def _parse_oauth_alias_credentials(
    raw_json: str,
    setting_name: str,
) -> dict[str, tuple[str, str]]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{setting_name} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{setting_name} must be a JSON object")

    credentials: dict[str, tuple[str, str]] = {}
    for raw_domain, raw_config in payload.items():
        domain = str(raw_domain).strip().lower().rstrip(".")
        if not domain or not isinstance(raw_config, dict):
            raise ValueError(f"{setting_name} has an invalid entry")
        client_id = raw_config.get("client_id")
        client_secret = raw_config.get("client_secret")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError(f"{setting_name} entry is missing client_id")
        if not isinstance(client_secret, str) or not client_secret.strip():
            raise ValueError(f"{setting_name} entry is missing client_secret")
        credentials[domain] = (client_id.strip(), client_secret.strip())
    return credentials


# Names that flow from `~/.quill_cloud_keys.private` into Settings as a
# fallback when the matching `TR_<UPPER>` env var isn't set. Provider API
# keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) stay in the LocalKeyFile
# flow used by ProviderClient — those don't belong in Settings.
_LOCAL_KEY_FALLBACKS: tuple[str, ...] = (
    "google_client_id",
    "google_client_secret",
    "google_oauth_redirect_url",
    "google_alias_credentials_json",
    "github_client_id",
    "github_client_secret",
    "github_oauth_redirect_url",
    "github_alias_credentials_json",
    "siwe_domain",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_region",
    "ses_from_email",
    "ses_from_name",
    "internal_gateway_token",
    "stripe_webhook_secret",
    "stripe_secret_key",
    "paypal_client_id",
    "paypal_client_secret",
    "paypal_webhook_id",
    "paypal_api_base_url",
    "sentry_dsn",
    "google_ads_conversion_feed_username",
    "google_ads_conversion_feed_password",
    "google_data_manager_account_id",
    "google_data_manager_login_account_id",
    "google_data_manager_signup_action_id",
    "google_data_manager_activated_action_id",
    "google_data_manager_purchase_action_id",
    "bootstrap_management_key",
    "byok_kms_key_name",
    "byok_envelope_key_b64",
    "byok_envelope_key_ref",
    "synthetic_monitor_api_key",
)


class _LocalKeyFileSource(PydanticBaseSettingsSource):
    """Pydantic settings source that reads `~/.quill_cloud_keys.private` so
    a single dotenv-style file can carry OAuth + SES creds for local dev
    without us mutating `os.environ` from a getter. Lower-priority than
    env vars and `.env`, so a developer can still override anything
    locally without touching the keys file."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values: dict[str, str] = {}
        if _running_under_pytest() and os.environ.get("TR_ALLOW_LOCAL_KEY_FILE_IN_TESTS") != "1":
            return
        path = settings_cls.model_fields["local_keys_file"].default
        if isinstance(path, Path) and path.exists():
            from trusted_router.secrets import LocalKeyFile

            keys = LocalKeyFile(path)
            for field in _LOCAL_KEY_FALLBACKS:
                value = keys.get(field.upper())
                if value:
                    self._values[field] = value

    def get_field_value(
        self,
        field: Any,  # FieldInfo — typed loosely so we don't depend on pydantic-internal symbols.
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def get_settings() -> Settings:
    """Build Settings. The LocalKeyFile source is wired in via
    `_settings_customise_sources` on Settings itself, so this is now a
    pure factory with no side effects."""
    return Settings()
