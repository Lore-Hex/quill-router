from __future__ import annotations

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
    api_base_url: str = "https://api.quillrouter.com/v1"
    trusted_domain: str = "trustedrouter.com"

    enable_live_providers: bool = False
    local_keys_file: Path = Path("~/.quill_cloud_keys.private").expanduser()
    storage_backend: str = "memory"
    gcp_project_id: str = "quill-cloud-proxy"
    spanner_instance_id: str | None = None
    spanner_database_id: str | None = None
    bigtable_instance_id: str | None = None
    bigtable_generation_table: str = "trustedrouter-generations"

    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.05

    # Axiom log shipping. Token + org-id read from env at startup
    # (`AXIOM_API_TOKEN`, `AXIOM_ORG_ID`) since those names match
    # axiom-py / axiom-cli conventions; dataset is plain config so it
    # can be overridden per environment (e.g. `staging-trusted-router`).
    # Empty token at startup → handler is not registered (graceful no-op).
    axiom_dataset: str = "trusted-router"
    axiom_url: str = "https://api.axiom.co"
    # Levels at and above this go to Axiom. INFO captures rate-limit
    # decisions, structured business events, and the Bigtable swallowed-
    # error log lines we just enriched. DEBUG would flood; ERROR alone
    # would miss the request_id correlation in 429s.
    axiom_log_level: str = "INFO"
    enable_sentry_test_route: bool = False
    sentry_floodgate_enabled: bool = True
    sentry_floodgate_window_seconds: int = 60 * 60
    sentry_floodgate_max_events_per_fingerprint: int = 3
    sentry_floodgate_max_events_per_window: int = 50
    sentry_floodgate_max_fingerprints: int = 2048

    trust_gcp_source_commit: str | None = None
    trust_gcp_image_reference: str | None = None
    trust_gcp_image_digest: str | None = None

    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_ip_per_window: int = 240
    rate_limit_key_per_window: int = 1200
    rate_limit_internal_per_window: int = 6000

    internal_gateway_token: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_secret_key: str | None = None
    paypal_client_id: str | None = None
    paypal_client_secret: str | None = None
    paypal_webhook_id: str | None = None
    paypal_api_base_url: str = "https://api-m.paypal.com"
    bootstrap_management_key: str | None = None
    byok_kms_key_name: str | None = None
    byok_envelope_key_b64: str | None = None
    byok_envelope_key_ref: str = "trustedrouter/byok-envelope-key/v1"

    auth_session_ttl_seconds: int = 60 * 60 * 24 * 30
    oauth_authorization_code_ttl_seconds: int = 10 * 60

    # OAuth providers — independently optional. Each is enabled iff both
    # client_id and client_secret are present. Routes 404 if disabled.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_oauth_redirect_url: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_oauth_redirect_url: str | None = None
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

    stablecoin_checkout_enabled: bool = True
    multi_region_enabled: bool = True
    # Operational read-only flag. When set, write paths (credit
    # reservations, gateway authorize, signup, etc.) return 503 with
    # `Retry-After`; reads keep working. Used for the Spanner →
    # nam6 migration and any future maintenance window that needs
    # writes paused without dropping connections. Off in production
    # by default; flipped via `gcloud run services update --update-env-vars
    # TR_READ_ONLY=1` per region during a planned cutover. See the
    # multi-region expansion plan for the cutover sequence.
    read_only: bool = False
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
    regions: str = "us-central1,europe-west4"
    primary_region: str = "us-central1"
    regional_api_hostname_template: str = "api-{region}.quillrouter.com"
    synthetic_monitor_region: str | None = None
    synthetic_monitor_api_key: str | None = None
    synthetic_monitor_model: str = "trustedrouter/monitor"
    # 30s, not 10s. The pong probes hit /v1/chat/completions and
    # /v1/responses with a real LLM call; cold-start on a regional Cloud
    # Run revision plus the upstream provider's first-token latency
    # routinely costs 5–9 seconds (we measured p95=9.0s in europe-west4).
    # 10s clipped the slow tail and turned cold-starts into false-down
    # events that tanked the 24h rollup. 30s catches genuine outages
    # while leaving headroom for cold-start.
    synthetic_monitor_timeout_seconds: float = 30.0
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
        "openai/gpt-5.4-mini,google/gemini-2.5-flash,"
        "deepseek/deepseek-v4-flash,moonshotai/kimi-k2.6,"
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
        if self.environment.lower() != "production":
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
            missing.append("TR_STORAGE_BACKEND=spanner-bigtable")
        if self.storage_backend == "spanner-bigtable":
            if not self.spanner_instance_id:
                missing.append("TR_SPANNER_INSTANCE_ID")
            if not self.spanner_database_id:
                missing.append("TR_SPANNER_DATABASE_ID")
            if not self.bigtable_instance_id:
                missing.append("TR_BIGTABLE_INSTANCE_ID")
        if not self.byok_kms_key_name:
            missing.append("TR_BYOK_KMS_KEY_NAME")
        # OAuth providers are independently optional in production. We DO
        # enforce that no provider is half-configured: a client_id without
        # the matching client_secret would cause silent runtime failures.
        if bool(self.google_client_id) != bool(self.google_client_secret):
            missing.append("TR_GOOGLE_CLIENT_ID and TR_GOOGLE_CLIENT_SECRET must both be set or both unset")
        if bool(self.github_client_id) != bool(self.github_client_secret):
            missing.append("TR_GITHUB_CLIENT_ID and TR_GITHUB_CLIENT_SECRET must both be set or both unset")
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
    def paypal_enabled(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def ses_enabled(self) -> bool:
        return bool(self.aws_access_key_id and self.aws_secret_access_key and self.ses_from_email)


# Names that flow from `~/.quill_cloud_keys.private` into Settings as a
# fallback when the matching `TR_<UPPER>` env var isn't set. Provider API
# keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) stay in the LocalKeyFile
# flow used by ProviderClient — those don't belong in Settings.
_LOCAL_KEY_FALLBACKS: tuple[str, ...] = (
    "google_client_id",
    "google_client_secret",
    "google_oauth_redirect_url",
    "github_client_id",
    "github_client_secret",
    "github_oauth_redirect_url",
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
