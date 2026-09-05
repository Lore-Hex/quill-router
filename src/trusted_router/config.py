from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from trusted_router.enclave_regions import ENCLAVE_REGIONS
from trusted_router.trust_ownership import (
    TRUST_OWNER_MUTATION_BUDGET,
    TRUST_REPLICATED_COLUMN_COUNT,
)

# Target names the synthetic monitor already assigns itself. A configured
# entry reusing one would quietly merge two different measurements into one
# status component, so it is rejected instead.
_RESERVED_SYNTHETIC_TARGET_NAMES = frozenset({"canonical", "control-plane"})
_GATEWAY_REGION_TARGET_NAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")
_GATEWAY_REGION_CONNECT_HOST_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-.")
# An entry name that LOOKS like a cloud region ("eu-west-1", or the zonal
# "eu-west-1a") is published to the world as that region's health, so it has
# to actually be that region's endpoint. Names that are not region-shaped
# ("ireland") are left alone — there is nothing to cross-check them against.
_REGION_SHAPED_NAME = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+[a-z]?$")


def _elb_region(connect_host: str) -> str | None:
    """Region an AWS ELB hostname lives in, or None if it is not one.

    ``<lb>-<id>.elb.<region>.amazonaws.com`` and the per-AZ zonal form
    ``<az>.<lb>-<id>.elb.<region>.amazonaws.com``.
    """
    labels = connect_host.split(".")
    if labels[-2:] != ["amazonaws", "com"] or "elb" not in labels:
        return None
    region_index = labels.index("elb") + 1
    if region_index >= len(labels) - 2:
        return None
    return labels[region_index]


_SETTLEMENT_PLANE_NAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def parse_settlement_inbound_tokens(raw: str) -> dict[str, str]:
    """Parse ``TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS`` into {token: plane}.

    Keyed by TOKEN because that is the lookup the request handler performs:
    which token authenticated decides which plane is speaking. Every
    malformed entry RAISES — a skipped entry is a peer that silently cannot
    settle, which surfaces weeks later as a backlog nobody can explain.

    Duplicate token values are rejected outright: one secret matching two
    plane names would make source identity ambiguous, and the insert-once
    key (source_plane, authorization_id) is only as strong as that identity.
    """
    if not raw.strip():
        return {}
    by_token: dict[str, str] = {}
    seen_planes: set[str] = set()
    for chunk in raw.split(","):
        entry = chunk.strip()
        plane, separator, token = entry.partition("=")
        plane = plane.strip().casefold()
        token = token.strip()
        if not separator or not plane or not token:
            raise ValueError(
                "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS entries must be "
                "'plane=token'; got a malformed entry"
            )
        if not set(plane) <= _SETTLEMENT_PLANE_NAME_CHARACTERS:
            raise ValueError(
                "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS plane names may only "
                f"contain lowercase letters, digits and '-'; got {plane!r}"
            )
        if plane in seen_planes:
            raise ValueError(
                f"TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS plane {plane!r} is duplicated"
            )
        if token in by_token:
            raise ValueError(
                "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS has two planes sharing "
                "one token; source identity would be ambiguous"
            )
        if len(token) < 32:
            raise ValueError(
                "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS tokens must be at "
                "least 32 characters; this secret authorizes ledger debits"
            )
        seen_planes.add(plane)
        by_token[token] = plane
    return by_token


class GatewayRegionTarget(NamedTuple):
    """One region's probe endpoint.

    ``public_host`` is the name the probe puts in SNI and Host. It is empty for
    the normal case — every replica serving one shared public name, which is
    how GCP and AWS are built — and the probe then uses the canonical
    ``api_base_url``.

    It exists because Azure is NOT built that way yet. Each Azure region serves
    its own hostname (``api-azure`` / ``api-azure-syd``) because the shared
    ACME cache is disabled there, so probing australiaeast with the canonical
    SNI asks it for a certificate it does not hold: the handshake fails and a
    perfectly healthy region reports DOWN. A status page that cries wolf is not
    a safer failure than one that stays quiet — it is a page nobody reads.

    When the shared cache lands and both Azure regions serve one name, this
    field simply goes unset again and the canonical path takes over.
    """

    name: str
    connect_host: str
    public_host: str = ""


def parse_gateway_region_targets(raw: str) -> tuple[GatewayRegionTarget, ...]:
    """Parse ``TR_SYNTHETIC_GATEWAY_REGION_TARGETS`` into region targets.

    Format: ``name=connect_host`` or ``name=connect_host@public_host``,
    comma-separated. Blank/unset yields an empty tuple — the
    single-canonical-target behaviour every deployment had before this setting
    existed.

    Every malformed entry RAISES. Skipping one would publish a status page
    that silently stops measuring an enclave: the component would vanish (its
    probe target is gone) rather than go red, which is the failure mode this
    whole feature exists to remove.
    """
    if not raw.strip():
        return ()
    entries: list[GatewayRegionTarget] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        entry = chunk.strip()
        name, separator, endpoint = entry.partition("=")
        name = name.strip().casefold()
        connect_host, _, public_host = endpoint.strip().casefold().partition("@")
        connect_host = connect_host.strip().rstrip(".")
        public_host = public_host.strip().rstrip(".")
        if not separator or not name or not connect_host:
            raise ValueError(
                "TR_SYNTHETIC_GATEWAY_REGION_TARGETS entries must be "
                f"'name=connect_host' or 'name=connect_host@public_host'; got {entry!r}"
            )
        if not set(name) <= _GATEWAY_REGION_TARGET_NAME_CHARACTERS:
            raise ValueError(
                "TR_SYNTHETIC_GATEWAY_REGION_TARGETS name may only contain "
                f"letters, digits, '-', '_' and '.'; got {name!r}"
            )
        if name in _RESERVED_SYNTHETIC_TARGET_NAMES:
            raise ValueError(f"TR_SYNTHETIC_GATEWAY_REGION_TARGETS name {name!r} is reserved")
        if name in seen:
            raise ValueError(f"TR_SYNTHETIC_GATEWAY_REGION_TARGETS name {name!r} is duplicated")
        # A bare hostname or IP literal only: a scheme, port, or path here
        # would be silently dropped by the connect path (it dials host:443 of
        # the canonical URL), i.e. a probe measuring something other than what
        # the operator wrote.
        if not set(connect_host) <= _GATEWAY_REGION_CONNECT_HOST_CHARACTERS:
            raise ValueError(
                "TR_SYNTHETIC_GATEWAY_REGION_TARGETS connect host must be a bare "
                f"hostname or IPv4 literal; got {connect_host!r}"
            )
        # An empty public host means "@" was written with nothing after it,
        # which reads as an override and silently is not one.
        if "@" in endpoint and not public_host:
            raise ValueError(
                "TR_SYNTHETIC_GATEWAY_REGION_TARGETS public host after '@' must not "
                f"be empty; got {entry!r}"
            )
        if public_host and not set(public_host) <= _GATEWAY_REGION_CONNECT_HOST_CHARACTERS:
            raise ValueError(
                "TR_SYNTHETIC_GATEWAY_REGION_TARGETS public host must be a bare "
                f"hostname; got {public_host!r}"
            )
        # THE name/endpoint binding, cross-checked. Everything downstream —
        # the target name, its target_region, its public status component —
        # comes from `name`, and nothing else ever re-derives it from the
        # endpoint. Two sibling NLB hostnames differ only in a 16-hex-char
        # middle segment, so transposing them is an easy edit to get wrong
        # and its consequence is the worst one available: the page reports
        # Ireland's health under Paris's name, so an operator failing over
        # reads the page and evacuates the region that is actually healthy.
        elb_region = _elb_region(connect_host)
        if (
            elb_region is not None
            and _REGION_SHAPED_NAME.match(name)
            and not name.startswith(elb_region)
        ):
            raise ValueError(
                f"TR_SYNTHETIC_GATEWAY_REGION_TARGETS name {name!r} does not match "
                f"its connect host's region {elb_region!r} ({connect_host!r}); the "
                "name is what the public status page calls this endpoint"
            )
        seen.add(name)
        entries.append(GatewayRegionTarget(name, connect_host, public_host))
    return tuple(entries)


def _comma_set(raw: str, *, primary: str | None = None) -> tuple[str, ...]:
    """Parse a comma-separated setting into a de-duplicated ordered tuple.

    ``primary`` is prepended when given, so the value that should be serving
    leads the set regardless of where it appears in the configured string.
    """
    values = [primary.strip()] if primary and primary.strip() else []
    values.extend(value.strip() for value in raw.split(",") if value.strip())
    return tuple(dict.fromkeys(values))


# Credential-bearing settings are owned by explicit process surfaces.  This
# table is intentionally importable by the mutation-sensitive contract test:
# adding a credential requires naming every process allowed to receive it.
SERVICE_SURFACE_SECRET_OWNERS: dict[str, frozenset[str]] = {
    "ops_chat_webhook_secret": frozenset({"actions"}),
    "postgres_dsn": frozenset({"public", "control", "internal", "observer"}),
    "postgres_iam_auth": frozenset({"public", "control", "internal", "observer"}),
    "clickhouse_url": frozenset({"control", "internal"}),
    "clickhouse_password": frozenset({"control", "internal"}),
    "provider_analytics_clickhouse_url": frozenset({"control"}),
    "provider_analytics_clickhouse_password": frozenset({"control"}),
    "operational_analytics_clickhouse_url": frozenset(
        {"public", "control", "internal", "observer"}
    ),
    # The write USER is not in this map: it is a username with a non-empty
    # default, not a secret, exactly like the read user above it. Only the
    # credential is surface-restricted.
    "operational_analytics_clickhouse_write_password": frozenset({"control", "internal"}),
    "operational_analytics_clickhouse_password": frozenset(
        {"public", "control", "internal", "observer"}
    ),
    "sentry_dsn": frozenset({"public", "control", "internal", "observer"}),
    "google_data_manager_kms_key_name": frozenset({"control"}),
    "attribution_cookie_key": frozenset({"public", "control"}),
    "attribution_cookie_secret": frozenset({"public", "control"}),
    "internal_gateway_token": frozenset({"internal"}),
    "operator_token": frozenset({"internal"}),
    # Internal owns this only for synthetic/Sentry routes; its billing routes
    # still select internal_gateway_token by path.
    "observer_internal_token": frozenset({"internal", "observer"}),
    "stripe_webhook_secret": frozenset({"control"}),
    "stripe_secret_key": frozenset({"control"}),
    "paypal_client_id": frozenset({"control"}),
    "paypal_client_secret": frozenset({"control"}),
    "paypal_webhook_id": frozenset({"control"}),
    "routable_enabled": frozenset({"control"}),
    "routable_api_token": frozenset({"control"}),
    "routable_webhook_secret": frozenset({"control"}),
    "routable_company_id": frozenset({"control"}),
    "routable_team_member_id": frozenset({"control"}),
    "routable_withdraw_from_account_id": frozenset({"control"}),
    "adyen_enabled": frozenset({"control"}),
    "adyen_api_key": frozenset({"control"}),
    "adyen_client_key": frozenset({"control"}),
    "adyen_hmac_key": frozenset({"control"}),
    "adyen_reference_key": frozenset({"control"}),
    "byok_kms_key_name": frozenset({"control"}),
    "byok_envelope_key_b64": frozenset({"control"}),
    "google_client_id": frozenset({"control"}),
    "google_client_secret": frozenset({"control"}),
    "google_alias_credentials_json": frozenset({"control"}),
    "github_client_id": frozenset({"control"}),
    "github_client_secret": frozenset({"control"}),
    "github_alias_credentials_json": frozenset({"control"}),
    "x402_enabled": frozenset({"control"}),
    "notify_enabled": frozenset({"control"}),
    "veriff_enabled": frozenset({"control"}),
    "veriff_api_key": frozenset({"control"}),
    "veriff_shared_secret_key": frozenset({"control"}),
    "telnyx_api_key": frozenset({"control"}),
    "twilio_account_sid": frozenset({"control"}),
    "twilio_auth_token": frozenset({"control"}),
    "twilio_api_key_secret": frozenset({"control"}),
    "aws_access_key_id": frozenset({"control", "actions"}),
    "aws_secret_access_key": frozenset({"control", "actions"}),
    "synthetic_monitor_api_key": frozenset({"internal", "observer"}),
    "federation_peer_token": frozenset({"internal"}),
    "federation_home_token": frozenset({"internal"}),
    "federation_credit_inbound_token": frozenset({"internal"}),
    "federation_credit_peer_token": frozenset({"internal"}),
    "federation_settlement_inbound_tokens": frozenset({"internal"}),
    "federation_settlement_home_token": frozenset({"internal"}),
}

_OPERATOR_CREDENTIAL_PREFIXES = (
    "internal_",
    "observer_",
    "synthetic_",
    "federation_",
)
_OPERATOR_CREDENTIAL_SUFFIXES = ("_token", "_tokens", "_api_key")


def operator_credential_setting_names(settings_type: type[BaseSettings]) -> frozenset[str]:
    """Fields whose values must never share the operator credential."""

    discovered = set(SERVICE_SURFACE_SECRET_OWNERS)
    discovered.update(
        name
        for name in settings_type.model_fields
        if name.startswith(_OPERATOR_CREDENTIAL_PREFIXES)
        and name.endswith(_OPERATOR_CREDENTIAL_SUFFIXES)
    )
    discovered.discard("operator_token")
    return frozenset(discovered)


def _sensitive_setting_is_configured(field_name: str, value: object) -> bool:
    if field_name in {"google_alias_credentials_json", "github_alias_credentials_json"}:
        return value != "{}"
    return bool(value)


def operational_analytics_sink_problems(settings: Any) -> list[str]:
    """Fail-closed checks for HOW operational telemetry leaves the process.

    Pure and duck-typed so the contract is testable without constructing a
    full production Settings. Called from production_is_fail_closed for the
    spanner-clickhouse backend.
    """
    problems: list[str] = []
    sink = settings.operational_analytics_sink
    if sink not in ("outbox", "direct"):
        problems.append(
            f"TR_OPERATIONAL_ANALYTICS_SINK must be 'outbox' or 'direct' (got {sink!r})"
        )
    if not (settings.operational_analytics_outbox_enabled or sink == "direct"):
        problems.append(
            "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true (or TR_OPERATIONAL_ANALYTICS_SINK=direct)"
        )
    if sink == "direct" and not (
        settings.operational_analytics_clickhouse_url
        and settings.operational_analytics_clickhouse_write_password
    ):
        problems.append(
            "TR_OPERATIONAL_ANALYTICS_SINK=direct requires "
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL and "
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_WRITE_PASSWORD"
        )
    return problems


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TR_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: str = "local"
    release: str = "local"
    service_name: str = "trusted-router"
    # One image serves several deliberately disjoint process roles. ``combined``
    # preserves the local/test developer experience. Deployed use requires the
    # separate, temporary migration opt-in below so a missing env var cannot
    # silently put the anonymous site, account control plane, and gateway
    # billing authority back into one autoscaling/concurrency failure domain.
    service_surface: Literal[
        "combined",
        "public",
        "actions",
        "control",
        "internal",
        "observer",
    ] = "combined"
    # Temporary compatibility bridge for the pre-split GCP service.  This is
    # deliberately separate from ``service_surface`` so a missing surface env
    # var remains fail-closed.  Remove the flag and its sole production opt-in
    # when the six-service rollout in #712 replaces the legacy service.
    allow_deployed_combined_surface: bool = False
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
    # Structured components of the same address. schema.org PostalAddress wants
    # them separately, and splitting the display string at runtime would be a
    # parser guessing at commas -- wrong the first time somebody adds a suite
    # number. Kept beside the display string so the two are edited together.
    legal_entity_street: str = "1111 Brickell Ave, Floor 10"
    legal_entity_city: str = "Miami"
    legal_entity_region: str = "FL"
    legal_entity_postal_code: str = "33131"
    legal_entity_country: str = "US"
    legal_entity_phone: str = "+1-305-239-7350"
    legal_entity_ein: str = "41-5339728"
    legal_entity_duns: str = "144992055"
    legal_entity_date_established: str | None = None
    legal_signatory_name: str = "Joseph Perla"
    legal_signatory_title: str = "CEO"
    security_contact_email: str = "security@trustedrouter.com"
    support_email: str = "help@trustedrouter.com"
    # Optional three-cloud Matrix support fanout. Values are comma-separated
    # pinned node URLs; every destination receives the same signed payload and
    # Matrix room state deduplicates it across federation.
    ops_chat_webhook_urls: str = ""
    ops_chat_webhook_secret: str | None = None
    ops_chat_webhook_timeout_seconds: float = 3.0

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
    # HOW operational telemetry leaves the process. "outbox" is the historical
    # path: rows into the billing database, polled out by a drainer -- a
    # standing tax on the money path that on 2026-08-25 measured ~25% of the
    # whole Spanner instance while idle. "direct" writes canonical rows
    # straight to ClickHouse from a bounded in-process buffer
    # (operational_analytics_direct.py) and needs a WRITE-capable ClickHouse
    # credential below. Per-cloud cutover: flip this only where that
    # credential is provisioned.
    operational_analytics_sink: str = "outbox"
    operational_analytics_clickhouse_write_user: str = "tr"
    operational_analytics_clickhouse_write_password: str = ""
    # Client-observed figures include upstream provider-caused failures and can
    # understate a healthy gateway. Keep them off public surfaces until
    # per-provider attribution makes the number fair to publish.
    public_client_observed_enabled: bool = False
    # Client-observed reliability beacons remain off until the ClickHouse node
    # runbook has been completed. Every setting is available as TR_CLIENT_EVENTS_*.
    client_events_enabled: bool = False
    # Sampling policy returned to SDKs for ordinary successful requests.
    client_events_success_sample_rate: float = 0.01
    # A positive value pauses SDK delivery without reading or storing a body.
    client_events_pause_seconds: int = 0
    # Workspaces whose beacons are synthetic regardless of the client bit.
    client_events_synthetic_workspace_ids: list[str] = []
    # Hard request and abuse-control bounds for the fire-and-forget endpoint.
    client_events_max_body_bytes: int = 65_536
    client_events_key_per_minute: int = 60
    client_events_workspace_per_minute: int = 300
    # Bounds blocking outbox writes so telemetry cannot consume the money path's pool.
    client_events_write_concurrency: int = 4
    # Flush cadence returned to SDKs in the accepted policy.
    client_events_flush_seconds: int = 30

    # Starter credit granted exactly once with a new email/social OAuth account's
    # first workspace. Wallet-only and secondary workspaces receive no grant.
    # $0.30 = 300,000 microdollars.
    signup_trial_credit_microdollars: int = 300_000

    # Global, creation-only operational brake. Returning Google/GitHub/wallet
    # users continue to sign in; only the branches that would create a new
    # account are refused. Rollout preserves the live value and the dedicated
    # operator script can flip it without rebuilding an image.
    new_signups_enabled: bool = True

    # Plain-email `POST /v1/signup` (no OAuth) is a credit-farming vector: an
    # open endpoint that mints a management key + trial credit for ANY email,
    # including disposable addresses. Closed by default. This switch controls
    # the email channel; ``new_signups_enabled`` is the global creation brake
    # across email, Google/GitHub, wallet, and delegated signup.
    email_signup_enabled: bool = False

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
    # Metadata-only Google Ads conversion delivery. The application writes an
    # encrypted outbox; a separate scheduled worker decrypts only Google's own
    # click ID and sends signup, activation, and settled-purchase events.
    google_data_manager_enabled: bool = False
    google_data_manager_account_id: str | None = None
    google_data_manager_login_account_id: str | None = None
    google_data_manager_signup_action_id: str | None = None
    google_data_manager_activated_action_id: str | None = None
    google_data_manager_purchase_action_id: str | None = None
    google_data_manager_kms_key_name: str | None = None
    google_data_manager_batch_size: int = 500
    google_data_manager_lease_seconds: int = 300
    google_data_manager_max_attempts: int = 20
    google_data_manager_timeout_seconds: float = 20.0
    google_data_manager_repair_lookback_days: int = 90
    google_data_manager_status_poll_attempts: int = 6
    google_data_manager_status_poll_seconds: float = 2.0
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

    # AWS and Azure measure a different artifact than GCP does. AWS Nitro
    # measures the enclave image file into PCR0 (SHA-384); Azure Confidential
    # Containers measure the SEV-SNP hostdata, which is sha256 over the decoded
    # CCE policy. Neither plane publishes a release record the control plane can
    # fetch the way it fetches the GCP gateway's, so both are supplied at deploy
    # time.
    #
    # An unset measurement publishes "not-configured" and serves 503 rather than
    # a number that may no longer be running. That failure mode is not
    # hypothetical: trust.trustedrouter.com/pcr0.txt has served the same PCR0
    # since the initial commit and it matches no running enclave. Nothing caught
    # it because no code ever compared the published value to a live
    # attestation. scripts/verify_trust_measurements.py is that comparison, and
    # .github/workflows/trust-drift.yml is what makes it happen: for a while
    # this comment named a script that nothing in the repo ever executed, which
    # is the same "nobody compared them" failure one level up.
    #
    # Both are SETS, not scalars. During a bind window the released key is bound
    # to the old and the new measurement at once — quill-cloud-proxy's
    # tools/deploy-azure-aci.sh emits an anyOf over BOTH hostdata values so the
    # outgoing enclave keeps serving while the incoming one comes up. A verifier
    # pinned to a single value fails exactly when a rollout is in flight, which
    # is when it is least helpful to fail. The primary is what should be
    # serving; the accepted set is what a verifier should tolerate.
    trust_aws_source_commit: str | None = None
    trust_aws_image_reference: str | None = None
    trust_aws_pcr0: str | None = None
    trust_aws_accepted_pcr0s: str = ""
    trust_azure_source_commit: str | None = None
    trust_azure_image_reference: str | None = None
    trust_azure_hostdata: str | None = None
    trust_azure_accepted_hostdata: str = ""
    # Azure serves from more than one region and each region has its own MAA
    # instance, so the issuer a verifier sees depends on which region answered.
    # Comma-separated; a verifier should accept any of them.
    trust_azure_attestation_issuers: str = ""
    # Where each plane's AUTHORITATIVE record lives. The control plane mirrors
    # these; the values above are only the offline fallback for when the
    # authoritative source cannot be reached. Keeping the fallback means a
    # Sigstore or Pages outage degrades to a stale-but-verified measurement
    # rather than to no measurement, and the scheduled drift check catches it
    # either way.
    trust_aws_release_url: str = ""
    trust_azure_release_url: str = ""

    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_ip_per_window: int = 240
    rate_limit_key_per_window: int = 1200
    rate_limit_internal_per_window: int = 6000
    # A contended Spanner authorize can occupy a worker for the full RPC
    # budget. Keep one API key from filling every worker in a service instance
    # while still allowing ordinary low-latency traffic to scale horizontally.
    gateway_authorize_max_in_flight_per_key: int = 4
    # Settlement has a wider gate because it closes already-issued holds, but
    # one hot key must not consume every Spanner session in an instance.
    settle_per_key_inflight_limit: int = 16
    # Only front doors that overwrite X-TrustedRouter-Client-IP may opt into
    # edge_header. Public origins that cannot perform that overwrite must stay
    # on the safe default and aggregate into the untrusted_lb bucket.
    rate_limit_client_ip_mode: str = "untrusted"

    # Split public/control services may hold the base64-encoded, already-derived
    # cookie key without receiving the legacy root that also authorizes gateway
    # calls. A dedicated secret remains supported for independent deployments.
    attribution_cookie_key: str | None = None
    attribution_cookie_secret: str | None = None
    internal_gateway_token: str | None = None
    # Dedicated to the externally reachable observer/status processes.  It
    # authenticates only their synthetic/Sentry internal endpoints and must
    # never be accepted by billing authorize/settle/refund routes.
    observer_internal_token: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_secret_key: str | None = None
    # Standard US Stripe processing schedules. These are grossed up against
    # the complete charge so the requested credit principal remains intact.
    # They are explicit config because negotiated Stripe pricing can differ.
    stripe_card_fee_basis_points: int = 290
    stripe_card_fee_fixed_cents: int = 30
    # Card-style rails have a purchase-fee floor. ACH and stablecoin retain
    # their lower rail-specific schedules and deliberately do not use it.
    checkout_card_fee_minimum_cents: int = 80
    stripe_stablecoin_fee_basis_points: int = 150
    stripe_stablecoin_fee_fixed_cents: int = 0
    stripe_ach_fee_basis_points: int = 80
    stripe_ach_fee_fixed_cents: int = 0
    stripe_ach_fee_max_cents: int = 500
    paypal_client_id: str | None = None
    paypal_client_secret: str | None = None
    paypal_webhook_id: str | None = None
    paypal_api_base_url: str = "https://api-m.paypal.com"
    # Creator USD cash-outs. This stays dark until every required Routable
    # identifier and secret is configured. Enabling it never changes earnings
    # accounting; it only permits an already-durable cash-out reservation to
    # be submitted to Routable.
    routable_enabled: bool = False
    routable_api_token: str | None = None
    routable_webhook_secret: str | None = None
    routable_company_id: str | None = None
    routable_team_member_id: str | None = None
    routable_withdraw_from_account_id: str | None = None
    routable_api_base_url: str = "https://api.routable.com"
    # Adyen is staged dark until the test merchant, HMAC webhook, and end-to-end
    # checkout canary are all green. Keep checkout enablement separate from
    # credentials so late webhooks remain verifiable after an operator disables
    # new Adyen sessions.
    adyen_enabled: bool = False
    adyen_api_key: str | None = None
    adyen_client_key: str | None = None
    adyen_hmac_key: str | None = None
    adyen_reference_key: str | None = None
    adyen_merchant_account: str | None = None
    adyen_environment: str = "test"
    adyen_live_endpoint_prefix: str | None = None
    adyen_checkout_api_version: int = 72
    adyen_web_version: str = "6.41.0"
    # Keep processor pricing explicit. The test deployment leaves this at zero;
    # production must use the rates from the signed Adyen agreement.
    adyen_card_fee_basis_points: int = 0
    adyen_card_fee_fixed_cents: int = 0
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
    # The public service must render login links without receiving the OAuth
    # client secrets owned by the control service. These are deliberately
    # non-secret presentation flags, set from the same rollout manifest that
    # configures the control service. ``None`` is rejected on a deployed public
    # surface so a split rollout cannot silently remove a login method.
    google_oauth_login_available: bool | None = None
    # Backup domains use independent provider credentials so login remains
    # available even if the canonical domain or its OAuth app is unavailable.
    # Each provider has its own Secret Manager value so credentials can rotate
    # independently without exposing one provider while updating the other.
    google_alias_credentials_json: str = "{}"
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_oauth_redirect_url: str | None = None
    github_oauth_login_available: bool | None = None
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
    # Operational alerts use a dedicated authenticated subdomain and
    # configuration set. This keeps their mailbox-domain reputation and
    # telemetry distinct from sign-in and support mail. EmailService refuses
    # to fall back to the default sender for an alert-profile message.
    ses_alert_from_email: str | None = "alerts@alerts.trustedrouter.com"
    ses_alert_from_name: str = "TrustedRouter Alerts"
    ses_alert_configuration_set: str | None = "trustedrouter-alerts"
    # Destination for TrustedOS partner-inquiry form submissions (/trustedos).
    # Falls back to ses_from_email when unset so the lead never silently drops.
    partner_inquiry_email: str | None = None
    # Durable post-signup activation reminders. Zero keeps the in-process
    # worker disabled in local/test environments. Production runs one bounded
    # pass per minute; an atomic milestone claim prevents duplicate sends when
    # several warm regional replicas inspect the same due task.
    activation_reminder_interval_seconds: int = 0

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
    # Regional quota leases remove hot global counter mutations from eligible
    # prepaid authorization. Global Spanner still reserves every bounded grant
    # and remains the source of truth; a fixed-cluster Bigtable row is only the
    # regional escrow ledger. Production activation is workspace-allowlisted
    # and validated fail-closed below.
    # ---- notifications (email / sms / voice to the account owner) ----------
    # Delivery credentials. TrustedRouter is the registered A2P 10DLC brand and
    # every customer's notification sends from these numbers, so a customer
    # never registers anything — and sender reputation becomes a shared asset,
    # which is why notify requires a verified phone and is metered.
    notify_enabled: bool = False
    phone_verification_requires_funding: bool | None = None
    custom_models_require_verification: bool | None = None
    # Whether the gateway will AUTHORIZE requests against user-provided
    # models. Off until the settle/refund half of their billing exists:
    # authorizing without it takes a credit hold that nothing can release,
    # and the deployed enclave authorizes any custom id before it resolves,
    # so this must never default on ahead of that. Registration, probing and
    # the public section work regardless; only serving is gated.
    user_models_dispatch_enabled: bool = False
    veriff_enabled: bool = False
    veriff_api_key: str | None = None
    veriff_shared_secret_key: str | None = None
    veriff_base_url: str = "https://stationapi.veriff.com"
    identity_session_stale_after_days: int = 7
    # Flip to True once A2P 10DLC is approved on the primary SMS carrier;
    # voice needs no registration.
    notify_sms_available: bool = False
    telnyx_api_key: str | None = None
    telnyx_from_number: str | None = None
    # The ORGANIZATION id from Telnyx /v2/whoami — not the number's connection
    # id and not the TeXML application id, both of which answer 404 here.
    # Which carrier leads, per channel. These differ because A2P 10DLC
    # registration is PER CARRIER: an unregistered carrier cannot deliver a US
    # SMS at all (Telnyx answers 40010, Twilio 30034), while voice needs no
    # registration and simply goes to whoever is cheaper. Preference only
    # reorders — the other carrier is still tried, so a wrong value here costs
    # a wasted attempt, never a lost page.
    # Repeat an unanswered voice page once. iOS and Do Not Disturb both let a
    # second call from the same number within three minutes ring through, which
    # is the only reliable way to wake someone from an unsaved number.
    notify_voice_repeat_unanswered: bool = True
    notify_sms_primary_carrier: str = "twilio"
    notify_voice_primary_carrier: str = "telnyx"
    telnyx_texml_account_id: str | None = None
    # The TeXML application, which is where the outbound voice profile hangs.
    telnyx_texml_application_id: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_api_key_sid: str | None = None
    twilio_api_key_secret: str | None = None
    twilio_from_number: str | None = None
    # ONE price per notification, whatever the channel.
    #
    # Email costs us far less than a phone call, but pricing it lower makes it
    # the obvious channel to abuse, and a customer reasoning about "which
    # notification is cheap" is a customer being trained to route around the
    # expensive one. Uniform pricing keeps the product explainable.
    #
    # Set from the WORST case, not the typical one. Approximate carrier cost per
    # notification (verify against current rate cards before launch):
    #
    #     email via SES        ~$0.0001
    #     sms via Telnyx       ~$0.004 + ~$0.003 A2P carrier pass-through
    #     sms via Twilio       ~$0.008 + pass-through
    #     voice, 1-min minimum ~$0.007 Telnyx, ~$0.014 Twilio
    #
    # A price set on the typical case loses money exactly when it matters: the
    # expensive path is the FALLBACK, which fires during an incident, when
    # volume spikes.
    #
    # $0.02 is positive on every path, thinnest on a Twilio VOICE fallback
    # (~$0.006 margin on a one-minute minimum). That is a deliberate choice, not
    # an oversight — the cheap path is the common one. Two things to watch: the
    # fixed monthly 10DLC campaign fee needs volume to amortize, and a sustained
    # Telnyx outage pushes every send onto the thin path at once.
    notify_price_microdollars: int = 20_000  # $0.02 per notification
    # PUSH IS FREE, deliberately.
    #
    # Push costs us essentially nothing (APNs is free), and it is delivered by
    # our own SREChat app — so a free push channel is distribution: the cheapest
    # way for a customer's agent to reach them is to install our client. Charging
    # two cents to send an APNs payload would be pricing away the funnel.
    #
    # It is also the only channel with no carrier and no regulator in the path,
    # which makes it the one we can offer on day one without 10DLC or a
    # sandbox exit.
    notify_push_price_microdollars: int = 0
    # Per-workspace ceilings. The pager equivalent of the leash: an agent in a
    # loop must not be able to spend a customer's balance overnight.
    notify_max_per_hour: int = 30
    notify_max_voice_per_hour: int = 4

    # Fleet capability: initialize and retain the regional ledger so any
    # revision can settle/refund/reconcile leases created elsewhere.
    regional_quota_leases_enabled: bool = False
    # Traffic mutation: authorize new requests from bounded regional escrow.
    # This is deliberately independent and default-off for two-phase rollouts.
    regional_quota_lease_issuance_enabled: bool = False
    regional_quota_lease_pilot_workspace_ids: str = ""
    regional_quota_lease_ttl_seconds: int = 60
    regional_quota_lease_max_microdollars: int = 10_000_000
    regional_quota_lease_max_available_basis_points: int = 1_000
    regional_quota_lease_shard_count: int = 16
    regional_quota_bigtable_table: str = "trustedrouter-regional-quota"
    spend_lease_bigtable_table: str = "trustedrouter-spend-lease"
    # True only in the one-shot reconciliation Cloud Run Job. Serving
    # processes must never set this: it exempts the worker from duplicating the
    # traffic-issuance allowlist because the worker can only drain leases that
    # already exist.
    regional_quota_reconciler_worker: bool = False
    regional_quota_reconcile_limit: int = 25
    # Comma-separated region=single-cluster-app-profile pairs. A fixed profile
    # is required because one lease has exactly one regional writer authority.
    regional_quota_bigtable_app_profiles: str = ""
    # Bigtable budget for one ledger read or compare-and-swap, in seconds.
    # Settlement and refund callbacks may land on any control-plane region,
    # so a europe-west4 process legitimately reads the us-central1 cluster.
    # The old 2.0 s default left a 1.0 s retry deadline (the client pads one
    # second) that cross-continent reads exceeded whenever the primary was
    # busy; each miss degraded to the exact Spanner path but logged a full
    # traceback. The gateway's own request budget is 25 s.
    regional_quota_ledger_timeout_seconds: float = 4.0
    spend_lease_bigtable_app_profiles: str = ""
    # Reconciliation is deployed before binding and remains active when the
    # traffic flag is off. Only the one-shot Cloud Run Job sets worker=True.
    spend_lease_reconciler_worker: bool = False
    spend_lease_reconcile_limit: int = 25
    spend_lease_reconcile_max_attempts: int = 12
    # Stage A spend leases are signed advisory artifacts only. This one flag
    # gates both minting and shadow evidence; default-off deploys never touch
    # Secret Manager. Runtime boot acceptance comes from the separately signed
    # Stage D policy; the CSV below is only an explicit break-glass addition.
    spend_lease_issuance_enabled: bool = False
    # Stage B traffic mutation.  Keep independent from Stage A issuance so a
    # deployed revision can continue shadowing while binding remains inert.
    spend_lease_binding_enabled: bool = False
    # Stage C router-side acceptance. Receipt verification remains available
    # while this is off, but every new admission reserve is refused with the
    # closed ``not_accepting`` reason and newly minted leases do not advertise
    # local admission.
    spend_lease_admission_accept: bool = False
    spend_lease_pilot_workspace_ids: str = ""
    spend_lease_signing_secret_name: str = ""
    # Audited break-glass addition to the signed Stage D runtime policy. This
    # is deliberately empty and rollout.sh never inherits it from a revision.
    spend_lease_accepted_gcp_image_digests: str = ""
    spend_lease_ttl_seconds: int = 60
    spend_lease_skew_seconds: int = 10
    spend_lease_max_microdollars: int = 1_000_000
    spend_lease_max_available_basis_points: int = 1_000
    # Converged trust-tier policy. The eligibility flag is intentionally
    # independent and defaults off; the other values can ship inertly first.
    spend_lease_trust_eligibility_enabled: bool = False
    trust_qualifying_providers: str = "stripe,x402"
    trust_tier3_min_days: int = 30
    trust_tier3_min_paid_microdollars: int = 50_000_000
    max_workspaces_per_owner: int = 25
    operator_token: str = ""
    operator_identities: str = ""
    trust_reconcile_interval_seconds: int = 900
    trust_reconcile_max_age_seconds: int = 3_600
    spend_lease_tier1_cap_microdollars: int = 5_000_000
    spend_lease_tier2_cap_microdollars: int = 25_000_000
    spend_lease_tier3_cap_microdollars: int = 100_000_000
    # Stage D is inert until an attested enclave calls the new endpoint. Keep a
    # runtime kill switch so heartbeat writes can be stopped independently of a
    # code rollout while authorize continues to expose cohort metadata.
    stage_d_heartbeat_enabled: bool = True
    # Emergency kill added 2026-09-03: declares no request Stage D eligible.
    stage_d_eligibility_enabled: bool = False
    stage_d_pilot_workspace_ids: str = "45819281-0ce9-4811-a0cd-c660ab3a116d"
    stage_d_policy_refresh_seconds: int = 60
    stage_d_policy_cert_identity: str = (
        "https://github.com/Lore-Hex/quill-cloud-proxy/.github/workflows/"
        "publish-trust-gcp.yml@refs/heads/main"
    )
    stage_d_policy_oidc_issuer: str = "https://token.actions.githubusercontent.com"
    heartbeat_grace_seconds: int = 300
    # Decision 70's billing change. Heartbeats and guarded zero refunds ship
    # first; only an explicit rollout may book a crashed request's last durable
    # usage snapshot.
    reap_snapshot_booking_enabled: bool = False
    # Dedicated Stage A traffic. The key belongs to a Credits-only pilot
    # workspace and is loaded lazily from Secret Manager by the isolated
    # once-a-minute synthetic job; it is never placed in an environment
    # variable. Default-off keeps deploying this code behavior-neutral until
    # an operator deliberately starts the soak.
    spend_lease_soak_probe_enabled: bool = False
    spend_lease_probe_key_secret: str = "trustedrouter-spend-lease-probe-key"  # noqa: S105
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
    regions: str = ",".join(ENCLAVE_REGIONS)
    marketing_regions: str = (
        "us-central1,europe-west4,us-east4,"
        "asia-northeast1,asia-east2,asia-southeast1,"
        # Standalone deployments on other clouds (multi-cloud-separation.md).
        "aws-eu-west-1,aws-eu-west-3,aws-eu-north-1,azure-australiaeast"
    )
    # Non-GCP deployments that have PASSED their own end-to-end smoke
    # (verify_deployment.sh --expect-monitor). Listing here turns the map dot
    # from "staged" to "live", so an entry is a factual claim, not decoration.
    # Stockholm (aws-eu-north-1) is deliberately absent: it replicates the
    # AWS-EU database but has no compute yet.
    external_live_regions: str = "aws-eu-west-1,aws-eu-west-3,azure-australiaeast"
    primary_region: str = "us-central1"
    regional_api_hostname_template: str = "api-{region}.quillrouter.com"
    synthetic_monitor_region: str | None = None
    synthetic_monitor_api_key: str | None = None
    synthetic_monitor_model: str = "trustedrouter/monitor"
    # Exact HTTPS control-plane origin synthetic canaries exercise. None falls
    # back to the canonical GCP plane. Observer-authenticated requests cannot
    # override it; otherwise an internal service could be induced to send its
    # monitor or gateway credentials to an attacker-selected destination.
    synthetic_control_plane_base_url: str | None = None
    # --- Lazy API-key federation -------------------------------------------
    # HOME side: the token peers present to /internal/federation/resolve-key.
    # Its OWN secret, never internal_gateway_token: anything holding that one
    # can already authorize and settle, i.e. move money. Empty disables
    # federation serving entirely (403), so a plane cannot become a user
    # directory by accident.
    federation_peer_token: str = ""
    # PEER side: the home plane this deployment resolves unknown keys from,
    # and the token it presents. Both empty = no federation, unknown key is
    # simply unknown.
    federation_home_base_url: str = ""
    federation_home_token: str = ""
    # --- Cross-plane credit transfer ---------------------------------------
    # A THIRD token, deliberately not either of the two above.
    #
    # federation_peer_token must never move money — that is the stated reason
    # it is not the internal gateway token (see routes/internal/federation.py).
    # If accepting credits were gated on it, the low-trust directory secret
    # would silently gain the power to change a balance. So credit transfer
    # gets its own secret, and the direction of the call follows the direction
    # of the value: the plane that HOLDS the credits pushes them.
    #
    # INBOUND: what this plane requires to be credited. Empty = this plane
    # refuses every inbound transfer, so a deployment cannot be funded by
    # accident or by a leaked directory token.
    federation_credit_inbound_token: str = ""
    # OUTBOUND: the plane this deployment may push credits TO, and the token it
    # presents there. Both empty = this plane cannot send credits anywhere.
    federation_credit_peer_base_url: str = ""
    federation_credit_peer_token: str = ""

    # --- Deferred settlement -----------------------------------------------
    # Lets a PEER plane serve a federated key's CREDITS traffic while the home
    # plane is unreachable: the spend is admitted against a transactional
    # outstanding cap, recorded durably as debt, and forwarded to the home
    # ledger when home returns. Identity federates and credits do not; usage
    # is the third category — a debt record, safe to apply late because
    # applying it only ever moves a home balance DOWN and an insert-once claim
    # at home makes double application impossible.
    #
    # OFF by default. A plane with this unset behaves exactly as before: a
    # federated key with no local balance gets 402 CREDITS_NOT_ON_THIS_PLANE.
    federation_deferred_settlement_enabled: bool = False
    # The bound on how much unsettled debt one workspace may run up on this
    # plane. Enforced by a conditional UPDATE at authorize, not a read-then-
    # check — 200 concurrent authorizes must not all see the same stale total.
    # This IS the worst-case exposure per workspace per outage (plus in-flight
    # estimate-vs-actual drift). $25.
    federation_deferred_max_outstanding_microdollars: int = 25_000_000
    # How long a deferred authorization may sit unsettled before the reaper
    # reclaims its estimate and releases the key-limit escrow. The enclave
    # dying between authorize and settle is routine (every deploy); without a
    # reaper the counter inflates permanently and the cap becomes a
    # self-inflicted, unrecoverable 402. Matches the 2h idempotency horizon.
    federation_deferred_authorization_ttl_seconds: int = 7_200
    # HOME side: which peers may apply usage to this plane's ledger, as
    # "plane=token,plane=token". The plane NAME is derived from WHICH token
    # authenticated — the request body never carries it — so one peer can
    # never claim under another's identity, and the insert-once key
    # (source_plane, authorization_id) is anchored in possession of a secret
    # rather than in text on the wire. Empty = this plane accepts no
    # federated settlements. NOT the resolve-key token (directory reads must
    # never gain money powers) and NOT the credit-transfer tokens (those
    # CREDIT a plane; this DEBITS usage — different power, different secret).
    federation_settlement_inbound_tokens: str = ""
    # PEER side: the token this plane presents to the home plane's
    # apply-usage endpoint. The target is federation_home_base_url.
    federation_settlement_home_token: str = ""
    # HOME's aggregate clamp: maximum usage one peer may apply to one
    # workspace per UTC day. Per-row ceilings bound nothing when the sender
    # controls row count; this is the number that actually caps a
    # compromised peer's damage per workspace per day. Default 4x the peer's
    # own outstanding cap.
    federation_settlement_workspace_daily_cap_microdollars: int = 100_000_000
    # The canonical target serves a self-signed cert minted inside the
    # TEE (AWS Nitro standalone deployments): probes skip CA verification
    # and the attestation probe instead verifies the document binds the
    # cert served on its own connection. Leave False wherever the
    # canonical gateway has a CA-issued cert (GCP).
    synthetic_canonical_attested: bool = False
    # In-process synthetic monitor. Every cloud schedules the monitor
    # differently — GCP has Cloud Scheduler driving a Cloud Run Job, AWS has an
    # EventBridge rule calling /internal/synthetic/run — and Azure Container
    # Apps has no equivalent that survives the CLI's argument handling. A
    # per-cloud scheduler is also one more thing that can silently stop: an
    # EventBridge connection whose stored token went stale flipped to
    # DEAUTHORIZED and the status page just went quiet, with the app healthy
    # and the rule ENABLED.
    #
    # Azure temporarily runs the pass in its serving process so the monitor
    # arrives with the deployment and no new scheduled resource. Its deploy
    # must therefore pin exactly one replica. AWS keeps this disabled and its
    # one existing EventBridge rule calls the authenticated app route.
    #
    # 0 = disabled (the default, so GCP/AWS keep their existing schedulers and
    # nothing double-runs).
    # MUST stay comfortably under the status page's staleness threshold
    # (monitor_freshness.stale_after_seconds, 300s). An interval EQUAL to the
    # threshold guarantees intermittent "Monitor Data Stale" banners: a pass
    # takes 10-17s, so the newest sample is always older than the interval by
    # the time the next one lands. 0 = disabled.
    synthetic_scheduler_interval_seconds: int = 0
    # Completions per pass. This IS real inference and it costs real money;
    # it is also the only thing that puts model/provider rows on the
    # leaderboard, because a model with no sample shows no verdict at all —
    # not green, not red, absent. Matches the AWS EventBridge rule's value.
    synthetic_scheduler_rotation_count: int = 8
    # Pin the enclave's PCR0 (EIF measurement). Set at deploy time from
    # the same value tools/deploy-aws-nitro.sh pins, so the probe turns
    # trust_degraded (pcr0_mismatch) if the serving enclave ever differs
    # from the deployed measurement. None = binding-only.
    attestation_expected_pcr0: str | None = None
    # Per-region alias/Cloud-Run probing is a GCP-topology concept: it
    # derives api-{region}.quillrouter.com aliases and *.run.app direct
    # URLs from templates. On a standalone single-gateway deployment
    # those templates produce hostnames that DO NOT EXIST, painting
    # permanent failures onto the status page. False = probe only the
    # canonical target.
    synthetic_regional_probes_enabled: bool = True
    # Image generation is sampled by a SEPARATE scheduled job
    # (-m trusted_router.synthetic.image_generation), not by the 1-minute
    # synthetic run. Only scripts/deploy/synthetic.sh creates that job, so
    # a deployment without it can never produce an image sample and must
    # not publish the component at all — an "unknown" row on a public
    # status page reads as "we are not sure our own service works".
    # Defaults True (the GCP shape); standalone deployments set it False.
    synthetic_image_probe_enabled: bool = True
    # Explicit control-plane /health URL attached to the canonical
    # target. Standalone deployments set this to their own control plane
    # (e.g. the App Runner URL) so control_plane_health measures the
    # RIGHT cloud; on GCP the per-region Cloud Run URLs already cover it.
    synthetic_control_plane_health_url: str | None = None
    # Extra gateway targets that share the canonical hostname but are
    # addressed at a SPECIFIC TCP endpoint: "name=connect_host,..." (e.g.
    # "eu-west-1=quill-enclave-nlb-....elb.eu-west-1.amazonaws.com").
    #
    # Why an endpoint instead of a hostname: an anycast/global record
    # (api-aws.trustedrouter.com -> AWS Global Accelerator) routes to
    # WHICHEVER region it prefers, so every probe lands on one of them and
    # a dead sibling is invisible. The enclave mints its cert inside the
    # TEE with a single SAN — the canonical hostname — so addressing a
    # region by its own load-balancer hostname would (correctly) fail
    # hostname validation. The probes therefore connect to connect_host
    # while SNI and the Host header stay derived from api_base_url.
    #
    # Empty = today's behaviour, exactly one canonical target.
    synthetic_gateway_region_targets: str = ""
    # Per-probe HTTP timeout for real provider-effective synthetic checks.
    # Keep this aligned with the gateway's first-byte budget. A successful
    # /responses probe in europe-west4 can legitimately take >10s on slow
    # cheap monitor routes, so 10s creates false downtime.
    synthetic_monitor_timeout_seconds: float = 20.0
    # Hard wall-clock ceiling for one admitted synthetic run. Eight provider
    # calls under the billing-concurrency limit of two take four ~20s p99
    # waves; adding the normal 10-17s probe work gives ~97s. 240s is ~2.47x
    # that budget while ensuring a wedged read cannot own the run slot forever.
    synthetic_run_deadline_seconds: float = 240.0
    # Remediation is an observe-only monitor read running through an internal
    # HTTP surface during the service split. Keep its request budget far below
    # the general synthetic pass budget so an unavailable analytics replica
    # cannot occupy control-plane request capacity for minutes.
    synthetic_remediator_deadline_seconds: float = 15.0
    # Monthly self-funding for the monitor workspace, applied lazily on its
    # own gateway-authorize path (synthetic/funding.py). Each deployment has
    # its own database, so each cloud's monitor funds itself from config —
    # no cron, no per-cloud manual grant to forget. 0 disables.
    synthetic_monitor_monthly_grant_dollars: float = 200.0
    synthetic_status_sample_limit: int = 5000
    synthetic_status_raw_retention_days: int = 14
    synthetic_status_rollup_retention_months: int = 24
    synthetic_status_us_url: str = "https://status-us.trustedrouter.com/status.json"
    synthetic_status_eu_url: str = "https://status-eu.trustedrouter.com/status.json"
    # Fleet peer list ("name=base_url,..."): every deployment watches every
    # deployment's public status page — its own included, which proves the
    # local public serving path end to end. Feeds /fleet.json and the
    # peer_monitor probes (synthetic/fleet.py). Config-as-code default on
    # purpose: a peer added here reaches all clouds on their next image roll
    # with no per-cloud env edits. Empty disables (tests set environment=test,
    # which also gates the probes off — see run_synthetic_once).
    # The standing remediator (synthetic/remediator.py): "off" | "observe" |
    # "act". Observe-first is the contract — a week of recorded decisions
    # calibrates flap rates before any actuator moves traffic. "act" is
    # accepted now so the flip is config-only later, but until actuators
    # ship it behaves as observe.
    remediator_mode: str = "observe"
    remediator_interval_seconds: int = 120
    # Cloud Run request-based CPU may pause background coroutines between
    # requests. GCP drives remediation from its scheduled synthetic worker;
    # AWS reuses its existing EventBridge synthetic rule. Both disable this
    # loop. Azure retains it only while its observer is pinned to one replica.
    remediator_in_process_enabled: bool = True
    synthetic_fleet_peers: str = (
        "gcp=https://trustedrouter.com"
        ",aws=https://aws.trustedrouter.com"
        ",azure=https://azure.trustedrouter.com"
    )
    # Deliberately empty: the auto ladder lives in exactly one place,
    # DEFAULT_AUTO_MODEL_ORDER in catalog_data.py, and an empty setting is what
    # lets that default through in auto_candidate_models().
    #
    # This used to hold its own copy of the ladder. Because the setting was
    # always non-empty it always won, so the "default" in catalog_data.py never
    # applied to routing and the two silently drifted apart — the copy here
    # still led with claude-opus-4.7 and named retired model versions. Editing
    # the documented ladder changed the advertised catalog and nothing else.
    # Keep this empty; set TR_AUTO_MODEL_ORDER to override at runtime.
    #
    # IDs follow OpenRouter naming exactly to line up with the ingest
    # snapshot — `moonshotai/...` not `kimi/...`, `mistralai/...` not
    # `mistral/...`, `meta-llama/...` for Cerebras-served Llama, etc.
    auto_model_order: str = ""

    max_request_body_bytes: int = 4 * 1024 * 1024
    max_in_flight_request_body_bytes: int = 64 * 1024 * 1024
    max_concurrent_request_bodies: int = 16
    request_body_read_timeout_seconds: float = 30.0

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
        surface = self.service_surface
        if self.attribution_cookie_key and self.attribution_cookie_secret:
            raise ValueError(
                "TR_ATTRIBUTION_COOKIE_KEY and TR_ATTRIBUTION_COOKIE_SECRET must not both be set"
            )
        attribution_cookie_key_bytes: bytes | None = None
        if self.attribution_cookie_key:
            try:
                attribution_cookie_key_bytes = base64.b64decode(
                    self.attribution_cookie_key,
                    validate=True,
                )
            except (binascii.Error, ValueError) as exc:
                raise ValueError("TR_ATTRIBUTION_COOKIE_KEY must be valid base64") from exc
            if len(attribution_cookie_key_bytes) != 32:
                raise ValueError("TR_ATTRIBUTION_COOKIE_KEY must decode to exactly 32 bytes")
        deployed_combined_bridge = (
            surface == "combined"
            and environment not in {"local", "test"}
            and self.allow_deployed_combined_surface
        )
        if self.allow_deployed_combined_surface and surface != "combined":
            raise ValueError(
                "TR_ALLOW_DEPLOYED_COMBINED_SURFACE may only be set with "
                "TR_SERVICE_SURFACE=combined"
            )
        if (
            surface == "combined"
            and environment not in {"local", "test"}
            and not deployed_combined_bridge
        ):
            raise ValueError(
                "TR_SERVICE_SURFACE=combined is restricted to local/test unless the "
                "temporary TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true migration bridge "
                "is explicitly enabled"
            )
        if deployed_combined_bridge and self.rate_limit_enabled:
            raise ValueError(
                "the temporary deployed combined bridge requires "
                "TR_RATE_LIMIT_ENABLED=false until #712 installs trusted per-client "
                "edge identity"
            )
        if self.synthetic_control_plane_base_url:
            parsed_control_plane = urlsplit(self.synthetic_control_plane_base_url)
            if (
                parsed_control_plane.scheme != "https"
                or not parsed_control_plane.hostname
                or parsed_control_plane.username is not None
                or parsed_control_plane.password is not None
                or parsed_control_plane.path not in {"", "/"}
                or parsed_control_plane.query
                or parsed_control_plane.fragment
            ):
                raise ValueError(
                    "TR_SYNTHETIC_CONTROL_PLANE_BASE_URL must be an exact HTTPS origin"
                )
        if self.max_request_body_bytes <= 0:
            raise ValueError("TR_MAX_REQUEST_BODY_BYTES must be positive")
        if self.synthetic_run_deadline_seconds <= 0:
            raise ValueError("TR_SYNTHETIC_RUN_DEADLINE_SECONDS must be positive")
        if self.synthetic_remediator_deadline_seconds <= 0:
            raise ValueError("TR_SYNTHETIC_REMEDIATOR_DEADLINE_SECONDS must be positive")
        if self.max_in_flight_request_body_bytes < self.max_request_body_bytes:
            raise ValueError(
                "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES must be at least TR_MAX_REQUEST_BODY_BYTES"
            )
        if self.max_concurrent_request_bodies <= 0:
            raise ValueError("TR_MAX_CONCURRENT_REQUEST_BODIES must be positive")
        if not 0 < self.request_body_read_timeout_seconds <= 300:
            raise ValueError("TR_REQUEST_BODY_READ_TIMEOUT_SECONDS must be between 0 and 300")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("TR_RATE_LIMIT_WINDOW_SECONDS must be positive")
        for name, value in (
            ("TR_RATE_LIMIT_IP_PER_WINDOW", self.rate_limit_ip_per_window),
            ("TR_RATE_LIMIT_KEY_PER_WINDOW", self.rate_limit_key_per_window),
            ("TR_RATE_LIMIT_INTERNAL_PER_WINDOW", self.rate_limit_internal_per_window),
            (
                "TR_GATEWAY_AUTHORIZE_MAX_IN_FLIGHT_PER_KEY",
                self.gateway_authorize_max_in_flight_per_key,
            ),
            ("TR_SETTLE_PER_KEY_INFLIGHT_LIMIT", self.settle_per_key_inflight_limit),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.rate_limit_client_ip_mode not in {"untrusted", "edge_header"}:
            raise ValueError("TR_RATE_LIMIT_CLIENT_IP_MODE must be 'untrusted' or 'edge_header'")
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
                "TR_CHECKOUT_CARD_FEE_MINIMUM_CENTS",
                self.checkout_card_fee_minimum_cents,
            ),
            (
                "TR_STRIPE_STABLECOIN_FEE_FIXED_CENTS",
                self.stripe_stablecoin_fee_fixed_cents,
            ),
            ("TR_STRIPE_ACH_FEE_FIXED_CENTS", self.stripe_ach_fee_fixed_cents),
            ("TR_STRIPE_ACH_FEE_MAX_CENTS", self.stripe_ach_fee_max_cents),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.request_record_write_mode not in {"legacy", "typed"}:
            raise ValueError("TR_REQUEST_RECORD_WRITE_MODE must be 'legacy' or 'typed'")
        if self.analytics_read_mode not in {
            "bigtable",
            "dual",
            "clickhouse",
            "clickhouse-only",
        }:
            raise ValueError(
                "TR_ANALYTICS_READ_MODE must be bigtable, dual, clickhouse, or clickhouse-only"
            )
        if self.analytics_dual_read_grace_seconds < 0:
            raise ValueError("TR_ANALYTICS_DUAL_READ_GRACE_SECONDS cannot be negative")
        if not 5 <= self.regional_quota_lease_ttl_seconds <= 300:
            raise ValueError("TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS must be between 5 and 300")
        if self.regional_quota_lease_max_microdollars <= 0:
            raise ValueError("TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS must be positive")
        if not 1 <= self.regional_quota_lease_max_available_basis_points <= 5_000:
            raise ValueError(
                "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS must be between 1 and 5000"
            )
        if not 1.1 <= self.regional_quota_ledger_timeout_seconds <= 10.0:
            raise ValueError("TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS must be between 1.1 and 10")
        if not 1 <= self.regional_quota_lease_shard_count <= 64:
            raise ValueError("TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT must be between 1 and 64")
        if not 1 <= self.regional_quota_reconcile_limit <= 1_000:
            raise ValueError("TR_REGIONAL_QUOTA_RECONCILE_LIMIT must be between 1 and 1000")
        if self.regional_quota_reconciler_worker and environment != "worker":
            raise ValueError(
                "TR_REGIONAL_QUOTA_RECONCILER_WORKER is valid only in worker processes"
            )
        if self.regional_quota_lease_issuance_enabled and not self.regional_quota_leases_enabled:
            raise ValueError(
                "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED requires TR_REGIONAL_QUOTA_LEASES_ENABLED"
            )
        if self.regional_quota_lease_issuance_enabled:
            if not self.regional_quota_lease_pilot_workspace_ids.strip():
                raise ValueError(
                    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED requires "
                    "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS"
                )
        if not 5 <= self.spend_lease_ttl_seconds <= 300:
            raise ValueError("TR_SPEND_LEASE_TTL_SECONDS must be between 5 and 300")
        if not 0 <= self.spend_lease_skew_seconds <= 30:
            raise ValueError("TR_SPEND_LEASE_SKEW_SECONDS must be between 0 and 30")
        if self.spend_lease_max_microdollars <= 0:
            raise ValueError("TR_SPEND_LEASE_MAX_MICRODOLLARS must be positive")
        if not 1 <= self.spend_lease_max_available_basis_points <= 5_000:
            raise ValueError(
                "TR_SPEND_LEASE_MAX_AVAILABLE_BASIS_POINTS must be between 1 and 5000"
            )
        if not self.trust_qualifying_provider_set:
            raise ValueError("TR_TRUST_QUALIFYING_PROVIDERS must not be empty")
        if not self.trust_qualifying_provider_set <= {"stripe", "paypal", "adyen", "x402"}:
            raise ValueError("TR_TRUST_QUALIFYING_PROVIDERS contains an unsupported provider")
        if self.trust_tier3_min_days < 0:
            raise ValueError("TR_TRUST_TIER3_MIN_DAYS must not be negative")
        if self.trust_tier3_min_paid_microdollars <= 0:
            raise ValueError("TR_TRUST_TIER3_MIN_PAID_MICRODOLLARS must be positive")
        if self.max_workspaces_per_owner <= 0:
            raise ValueError("TR_MAX_WORKSPACES_PER_OWNER must be positive")
        if (
            self.max_workspaces_per_owner * 64 * TRUST_REPLICATED_COLUMN_COUNT
            > TRUST_OWNER_MUTATION_BUDGET
        ):
            raise ValueError(
                "TR_MAX_WORKSPACES_PER_OWNER × TR_CREDIT_SHARDS_MAX × 7 "
                "must not exceed the pinned 20000-mutation trust budget"
            )
        if self.operator_token and not self.operator_identities.strip():
            raise ValueError(
                "TR_OPERATOR_IDENTITIES must be set when TR_OPERATOR_TOKEN is set"
            )
        if self.trust_reconcile_interval_seconds <= 0:
            raise ValueError("TR_TRUST_RECONCILE_INTERVAL_SECONDS must be positive")
        # The scope pins 15 minutes for Stripe/x402 and three hours for PayPal.
        # Adyen's report source has no additional provider-side delay here.
        consistency_delays = {
            "stripe": 900,
            "x402": 900,
            "paypal": 10_800,
            "adyen": 0,
        }
        enabled_consistency_delay = max(
            consistency_delays[provider]
            for provider in self.trust_qualifying_provider_set
        )
        minimum_reconcile_age = (
            enabled_consistency_delay + 2 * self.trust_reconcile_interval_seconds
        )
        if self.trust_reconcile_max_age_seconds < minimum_reconcile_age:
            raise ValueError(
                "TR_TRUST_RECONCILE_MAX_AGE_SECONDS must cover the provider "
                "consistency delay plus two reconciliation cadences"
            )
        trust_caps = (
            self.spend_lease_tier1_cap_microdollars,
            self.spend_lease_tier2_cap_microdollars,
            self.spend_lease_tier3_cap_microdollars,
        )
        if any(cap <= 0 for cap in trust_caps) or trust_caps != tuple(sorted(trust_caps)):
            raise ValueError("TR_SPEND_LEASE_TIER*_CAP_MICRODOLLARS must be positive and ordered")
        configured_spend_digests = self.spend_lease_accepted_gcp_image_digests.split(",")
        for digest in configured_spend_digests:
            digest = digest.strip()
            if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(
                    "TR_SPEND_LEASE_ACCEPTED_GCP_IMAGE_DIGESTS entries must be sha256 digests"
                )
        if self.stage_d_policy_refresh_seconds <= 0:
            raise ValueError("TR_STAGE_D_POLICY_REFRESH_SECONDS must be positive")
        if not self.stage_d_policy_cert_identity.strip():
            raise ValueError("TR_STAGE_D_POLICY_CERT_IDENTITY must not be empty")
        if not self.stage_d_policy_oidc_issuer.strip():
            raise ValueError("TR_STAGE_D_POLICY_OIDC_ISSUER must not be empty")
        if self.spend_lease_issuance_enabled:
            if not self.spend_lease_pilot_workspace_ids.strip():
                raise ValueError(
                    "TR_SPEND_LEASE_ISSUANCE_ENABLED requires "
                    "TR_SPEND_LEASE_PILOT_WORKSPACE_IDS"
                )
            if not self.spend_lease_signing_secret_name.strip():
                raise ValueError(
                    "TR_SPEND_LEASE_ISSUANCE_ENABLED requires "
                    "TR_SPEND_LEASE_SIGNING_SECRET_NAME"
                )
            if not (
                self.operational_analytics_outbox_enabled
                or self.operational_analytics_sink == "direct"
            ):
                raise ValueError(
                    "TR_SPEND_LEASE_ISSUANCE_ENABLED requires the operational "
                    "analytics outbox or direct sink"
                )
        if self.spend_lease_binding_enabled and not self.spend_lease_issuance_enabled:
            raise ValueError(
                "TR_SPEND_LEASE_BINDING_ENABLED requires TR_SPEND_LEASE_ISSUANCE_ENABLED"
            )
        if self.spend_lease_admission_accept and not self.spend_lease_binding_enabled:
            raise ValueError(
                "TR_SPEND_LEASE_ADMISSION_ACCEPT requires TR_SPEND_LEASE_BINDING_ENABLED"
            )
        if self.spend_lease_soak_probe_enabled and not self.spend_lease_probe_key_secret.strip():
            raise ValueError(
                "TR_SPEND_LEASE_SOAK_PROBE_ENABLED requires "
                "TR_SPEND_LEASE_PROBE_KEY_SECRET"
            )
        if self.regional_quota_leases_enabled:
            if environment not in {"local", "test"}:
                if self.storage_backend not in {
                    "spanner-bigtable",
                    "spanner-clickhouse",
                }:
                    raise ValueError(
                        "TR_REGIONAL_QUOTA_LEASES_ENABLED requires a Spanner GCP backend"
                    )
                if self.request_record_write_mode != "typed":
                    raise ValueError(
                        "TR_REGIONAL_QUOTA_LEASES_ENABLED requires typed request records"
                    )
                if not self.settle_outbox_enabled:
                    raise ValueError("TR_REGIONAL_QUOTA_LEASES_ENABLED requires the settle outbox")
                if not self.bigtable_instance_id:
                    raise ValueError(
                        "TR_REGIONAL_QUOTA_LEASES_ENABLED requires a Bigtable instance"
                    )
                if not self.regional_quota_bigtable_app_profile_map:
                    raise ValueError(
                        "TR_REGIONAL_QUOTA_LEASES_ENABLED requires fixed regional "
                        "Bigtable app profiles"
                    )
        # Parse for effect: a malformed entry must fail the process at
        # construction, not degrade into "no extra targets" that nobody
        # notices until a dead enclave goes unreported.
        parse_gateway_region_targets(self.synthetic_gateway_region_targets)
        # Same rule for the settlement token map: a malformed entry must not
        # degrade into "that peer silently cannot settle".
        settlement_tokens = parse_settlement_inbound_tokens(
            self.federation_settlement_inbound_tokens
        )
        if self.operator_token:
            for field_name in sorted(operator_credential_setting_names(type(self))):
                raw_value = getattr(self, field_name)
                values: tuple[str, ...]
                if field_name == "federation_settlement_inbound_tokens":
                    values = tuple(settlement_tokens)
                elif isinstance(raw_value, str):
                    values = (raw_value,) if raw_value else ()
                else:
                    values = ()
                if any(value and hmac.compare_digest(self.operator_token, value) for value in values):
                    raise ValueError(
                        "TR_OPERATOR_TOKEN must differ from "
                        f"TR_{field_name.upper()}"
                    )
        if self.federation_settlement_home_token and not self.federation_home_base_url:
            raise ValueError(
                "TR_FEDERATION_SETTLEMENT_HOME_TOKEN is set but "
                "TR_FEDERATION_HOME_BASE_URL is not; the forwarder would have "
                "a token and nowhere to present it"
            )
        # ENFORCED credential separation, not just documented. The whole token
        # doctrine (routes/internal/federation.py docstring) is that no single
        # secret grants both directory reads and money movement — but a config
        # that reuses the resolve-key token as a settlement-map value would
        # quietly hand every peer holding the directory secret the power to
        # debit arbitrary workspaces up to the clamp. Reuse fails startup.
        if settlement_tokens:
            other_credentials: dict[str, str] = {
                cred_name: cred_value
                for cred_name, cred_value in (
                    ("TR_FEDERATION_PEER_TOKEN", self.federation_peer_token),
                    ("TR_FEDERATION_HOME_TOKEN", self.federation_home_token),
                    ("TR_FEDERATION_CREDIT_INBOUND_TOKEN", self.federation_credit_inbound_token),
                    ("TR_FEDERATION_CREDIT_PEER_TOKEN", self.federation_credit_peer_token),
                    ("TR_FEDERATION_SETTLEMENT_HOME_TOKEN", self.federation_settlement_home_token),
                    ("TR_INTERNAL_GATEWAY_TOKEN", self.internal_gateway_token or ""),
                )
                if cred_value
            }
            for token in settlement_tokens:
                for cred_name, cred_value in other_credentials.items():
                    if token == cred_value:
                        raise ValueError(
                            "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS reuses the value "
                            f"of {cred_name}; a settlement token must be a dedicated "
                            "secret — reuse would let a directory or gateway "
                            "credential debit workspaces"
                        )
        if not 0.1 <= self.ops_chat_webhook_timeout_seconds <= 10.0:
            raise ValueError("TR_OPS_CHAT_WEBHOOK_TIMEOUT_SECONDS must be between 0.1 and 10")
        ops_chat_urls = tuple(
            dict.fromkeys(
                value.strip().rstrip("/")
                for value in self.ops_chat_webhook_urls.split(",")
                if value.strip()
            )
        )
        if bool(ops_chat_urls) != bool(self.ops_chat_webhook_secret):
            raise ValueError(
                "TR_OPS_CHAT_WEBHOOK_URLS and TR_OPS_CHAT_WEBHOOK_SECRET "
                "must both be set or both unset"
            )
        if environment not in {"local", "test"} and any(
            not url.startswith("https://") for url in ops_chat_urls
        ):
            raise ValueError("TR_OPS_CHAT_WEBHOOK_URLS must contain only HTTPS URLs when deployed")
        if not 1 <= self.google_data_manager_batch_size <= 2_000:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_BATCH_SIZE must be between 1 and 2000")
        if self.google_data_manager_lease_seconds < 30:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_LEASE_SECONDS must be at least 30")
        if not 1 <= self.google_data_manager_max_attempts <= 20:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_MAX_ATTEMPTS must be between 1 and 20")
        if not 1.0 <= self.google_data_manager_timeout_seconds <= 120.0:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_TIMEOUT_SECONDS must be between 1 and 120")
        if not 1 <= self.google_data_manager_repair_lookback_days <= 90:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_REPAIR_LOOKBACK_DAYS must be between 1 and 90")
        if not 1 <= self.google_data_manager_status_poll_attempts <= 30:
            raise ValueError("TR_GOOGLE_DATA_MANAGER_STATUS_POLL_ATTEMPTS must be between 1 and 30")
        if not 0.1 <= self.google_data_manager_status_poll_seconds <= 30.0:
            raise ValueError(
                "TR_GOOGLE_DATA_MANAGER_STATUS_POLL_SECONDS must be between 0.1 and 30"
            )
        if self.google_data_manager_enabled:
            if environment not in {"local", "test"}:
                raise ValueError(
                    "TR_GOOGLE_DATA_MANAGER_ENABLED is forbidden outside local/test; "
                    "TrustedRouter's no-sharing policy disables outbound advertising uploads"
                )
            missing_google_data_manager = [
                name
                for name, value in (
                    ("TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID", self.google_data_manager_account_id),
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
            if environment not in {"local", "test"} and not self.google_data_manager_kms_key_name:
                missing_google_data_manager.append("TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME")
            if missing_google_data_manager:
                raise ValueError(
                    "Google Data Manager is enabled but missing "
                    + ", ".join(missing_google_data_manager)
                )
        if self.x402_allow_mock_payments and environment not in {"local", "test"}:
            raise ValueError("TR_X402_ALLOW_MOCK_PAYMENTS is only allowed in local/test")
        if self.identity_session_stale_after_days <= 0:
            raise ValueError("TR_IDENTITY_SESSION_STALE_AFTER_DAYS must be positive")
        if self.veriff_enabled and environment not in {"local", "test"}:
            missing_veriff = [
                name
                for name, value in (
                    ("TR_VERIFF_API_KEY", self.veriff_api_key),
                    ("TR_VERIFF_SHARED_SECRET_KEY", self.veriff_shared_secret_key),
                )
                if not value
            ]
            if missing_veriff:
                raise ValueError("TR_VERIFF_ENABLED requires " + ", ".join(missing_veriff))
        if self.routable_enabled:
            missing_routable = [
                name
                for name, value in (
                    ("TR_ROUTABLE_API_TOKEN", self.routable_api_token),
                    ("TR_ROUTABLE_WEBHOOK_SECRET", self.routable_webhook_secret),
                    ("TR_ROUTABLE_COMPANY_ID", self.routable_company_id),
                    ("TR_ROUTABLE_TEAM_MEMBER_ID", self.routable_team_member_id),
                    (
                        "TR_ROUTABLE_WITHDRAW_FROM_ACCOUNT_ID",
                        self.routable_withdraw_from_account_id,
                    ),
                )
                if not value
            ]
            if missing_routable:
                raise ValueError(
                    "TR_ROUTABLE_ENABLED requires " + ", ".join(missing_routable)
                )
        if not self.routable_api_base_url.startswith("https://"):
            raise ValueError("TR_ROUTABLE_API_BASE_URL must use https")
        if self.adyen_environment not in {"test", "live"}:
            raise ValueError("TR_ADYEN_ENVIRONMENT must be test or live")
        if not 0 <= self.adyen_card_fee_basis_points < 10_000:
            raise ValueError("TR_ADYEN_CARD_FEE_BASIS_POINTS must be between 0 and 9999")
        if self.adyen_card_fee_fixed_cents < 0:
            raise ValueError("TR_ADYEN_CARD_FEE_FIXED_CENTS cannot be negative")
        if not 1 <= self.adyen_checkout_api_version <= 999:
            raise ValueError("TR_ADYEN_CHECKOUT_API_VERSION must be between 1 and 999")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.adyen_web_version):
            raise ValueError("TR_ADYEN_WEB_VERSION must be an exact semantic version")
        if self.adyen_hmac_key:
            try:
                adyen_hmac_bytes = bytes.fromhex(self.adyen_hmac_key)
            except ValueError as exc:
                raise ValueError("TR_ADYEN_HMAC_KEY must be a hexadecimal key") from exc
            if len(adyen_hmac_bytes) != 32:
                raise ValueError("TR_ADYEN_HMAC_KEY must be exactly 32 bytes")
        if self.adyen_reference_key and len(self.adyen_reference_key.encode("utf-8")) < 32:
            raise ValueError("TR_ADYEN_REFERENCE_KEY must be at least 32 bytes")
        if self.adyen_enabled:
            missing_adyen = [
                name
                for name, value in (
                    ("TR_ADYEN_API_KEY", self.adyen_api_key),
                    ("TR_ADYEN_CLIENT_KEY", self.adyen_client_key),
                    ("TR_ADYEN_HMAC_KEY", self.adyen_hmac_key),
                    ("TR_ADYEN_REFERENCE_KEY", self.adyen_reference_key),
                    ("TR_ADYEN_MERCHANT_ACCOUNT", self.adyen_merchant_account),
                )
                if not value
            ]
            if self.adyen_environment == "live" and not self.adyen_live_endpoint_prefix:
                missing_adyen.append("TR_ADYEN_LIVE_ENDPOINT_PREFIX")
            if missing_adyen:
                raise ValueError("TR_ADYEN_ENABLED requires " + ", ".join(missing_adyen))
        if (
            self.x402_enabled
            and environment not in {"local", "test"}
            and (not self.stripe_secret_key or not self.stripe_webhook_secret)
        ):
            raise ValueError(
                "TR_X402_ENABLED requires TR_STRIPE_SECRET_KEY and "
                "TR_STRIPE_WEBHOOK_SECRET outside local/test"
            )
        if environment in {"local", "test"}:
            return self
        production = environment == "production"
        missing = []
        if environment != "worker" and surface in {"control", "public"}:
            if not self.attribution_cookie_key and not self.attribution_cookie_secret:
                missing.append("TR_ATTRIBUTION_COOKIE_KEY or TR_ATTRIBUTION_COOKIE_SECRET")
            elif (
                self.attribution_cookie_secret
                and len(self.attribution_cookie_secret.encode("utf-8")) < 32
            ):
                missing.append("TR_ATTRIBUTION_COOKIE_SECRET (at least 32 bytes)")
        if environment != "worker" and surface == "public":
            if self.google_oauth_login_available is None:
                missing.append("TR_GOOGLE_OAUTH_LOGIN_AVAILABLE")
            if self.github_oauth_login_available is None:
                missing.append("TR_GITHUB_OAUTH_LOGIN_AVAILABLE")
        if environment != "worker" and surface == "control":
            for provider, advertised, configured in (
                (
                    "GOOGLE",
                    self.google_oauth_login_available,
                    self.google_oauth_credentials_configured,
                ),
                (
                    "GITHUB",
                    self.github_oauth_login_available,
                    self.github_oauth_credentials_configured,
                ),
            ):
                if advertised is not None and advertised != configured:
                    missing.append(
                        f"TR_{provider}_OAUTH_LOGIN_AVAILABLE must match the control "
                        f"service's {provider} OAuth credential capability"
                    )
        if (
            self.attribution_cookie_secret
            and self.internal_gateway_token
            and hmac.compare_digest(
                self.attribution_cookie_secret.encode("utf-8"),
                self.internal_gateway_token.encode("utf-8"),
            )
        ):
            missing.append(
                "TR_ATTRIBUTION_COOKIE_SECRET must differ from TR_INTERNAL_GATEWAY_TOKEN"
            )
        if (
            attribution_cookie_key_bytes
            and self.internal_gateway_token
            and hmac.compare_digest(
                attribution_cookie_key_bytes,
                self.internal_gateway_token.encode("utf-8"),
            )
        ):
            missing.append("TR_ATTRIBUTION_COOKIE_KEY must differ from TR_INTERNAL_GATEWAY_TOKEN")
        if (
            self.observer_internal_token
            and self.internal_gateway_token
            and hmac.compare_digest(
                self.observer_internal_token.encode("utf-8"),
                self.internal_gateway_token.encode("utf-8"),
            )
        ):
            missing.append("TR_OBSERVER_INTERNAL_TOKEN must differ from TR_INTERNAL_GATEWAY_TOKEN")
        if (
            self.observer_internal_token
            and self.synthetic_monitor_api_key
            and hmac.compare_digest(
                self.observer_internal_token.encode("utf-8"),
                self.synthetic_monitor_api_key.encode("utf-8"),
            )
        ):
            missing.append(
                "TR_OBSERVER_INTERNAL_TOKEN must differ from TR_SYNTHETIC_MONITOR_API_KEY"
            )

        # #712 removes the temporary combined bridge.  Until then it retains
        # the legacy service's complete authority and therefore its complete
        # pre-split startup requirements.
        gateway_surfaces = {"internal"}
        sentry_surfaces = {"combined", "control", "internal", "observer"}
        account_surfaces = {"combined", "control"}
        email_surfaces = {"combined", "control", "actions"}
        if (surface in gateway_surfaces or deployed_combined_bridge) and not (
            self.internal_gateway_token
        ):
            missing.append("TR_INTERNAL_GATEWAY_TOKEN")
        if surface in {"internal", "observer"} and not self.observer_internal_token:
            missing.append("TR_OBSERVER_INTERNAL_TOKEN")
        if (surface == "control" or deployed_combined_bridge) and environment != "worker":
            if not self.stripe_webhook_secret:
                missing.append("TR_STRIPE_WEBHOOK_SECRET")
            if not self.stripe_secret_key:
                missing.append("TR_STRIPE_SECRET_KEY")
        paypal_fields = [
            self.paypal_client_id,
            self.paypal_client_secret,
            self.paypal_webhook_id,
        ]
        if any(paypal_fields) and not all(paypal_fields):
            missing.append(
                "TR_PAYPAL_CLIENT_ID, TR_PAYPAL_CLIENT_SECRET, and "
                "TR_PAYPAL_WEBHOOK_ID must all be set or all unset"
            )
        if production:
            if surface in sentry_surfaces and not self.sentry_dsn:
                missing.append("TR_SENTRY_DSN")
            if surface in email_surfaces and not self.aws_access_key_id:
                missing.append("TR_AWS_ACCESS_KEY_ID")
            if surface in email_surfaces and not self.aws_secret_access_key:
                missing.append("TR_AWS_SECRET_ACCESS_KEY")
            if surface in email_surfaces and not self.ses_from_email:
                missing.append("TR_SES_FROM_EMAIL")

        for field_name, owners in SERVICE_SURFACE_SECRET_OWNERS.items():
            configured_value = getattr(self, field_name)
            if (
                not deployed_combined_bridge
                and surface not in owners
                and _sensitive_setting_is_configured(field_name, configured_value)
            ):
                environment_name = f"TR_{field_name.upper()}"
                missing.append(f"unset {environment_name} for TR_SERVICE_SURFACE={surface}")
        if self.bootstrap_management_key:
            missing.append("unset TR_BOOTSTRAP_MANAGEMENT_KEY")
        storage_required = surface != "actions"
        if surface == "actions" and self.storage_backend != "memory":
            missing.append("TR_STORAGE_BACKEND=memory for TR_SERVICE_SURFACE=actions")
        if not production:
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"deployed configuration is not fail-closed: {joined}")
            return self
        if storage_required and self.storage_backend == "memory":
            missing.append("TR_STORAGE_BACKEND=spanner-bigtable or spanner-clickhouse")
        if storage_required and self.storage_backend in {"spanner-bigtable", "spanner-clickhouse"}:
            if not self.spanner_instance_id:
                missing.append("TR_SPANNER_INSTANCE_ID")
            if not self.spanner_database_id:
                missing.append("TR_SPANNER_DATABASE_ID")
        if storage_required and self.storage_backend == "spanner-bigtable":
            if not self.bigtable_instance_id:
                missing.append("TR_BIGTABLE_INSTANCE_ID")
        if storage_required and self.storage_backend == "spanner-clickhouse":
            if self.analytics_read_mode != "clickhouse-only":
                missing.append(
                    "TR_ANALYTICS_READ_MODE=clickhouse-only with "
                    "TR_STORAGE_BACKEND=spanner-clickhouse"
                )
            if not self.generation_records_enabled:
                missing.append("TR_GENERATION_RECORDS_ENABLED=true")
            missing.extend(operational_analytics_sink_problems(self))
            if not self.analytics_outbox_enabled:
                missing.append("TR_ANALYTICS_OUTBOX_ENABLED=true")
            if self.bigtable_mirror_writes_enabled:
                missing.append("TR_BIGTABLE_MIRROR_WRITES_ENABLED=false")
            if self.request_record_write_mode != "typed":
                missing.append("TR_REQUEST_RECORD_WRITE_MODE=typed")
        if storage_required and self.analytics_read_mode != "bigtable":
            if not self.operational_analytics_clickhouse_url:
                missing.append("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL")
            if not self.operational_analytics_clickhouse_password:
                missing.append("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD")
        if (
            storage_required
            and (self.request_record_write_mode == "typed" or surface == "internal")
            and not self.settle_outbox_enabled
        ):
            missing.append(
                "TR_SETTLE_OUTBOX_ENABLED=true for typed request records and the internal surface"
            )
        if surface in account_surfaces and not self.byok_kms_key_name:
            missing.append("TR_BYOK_KMS_KEY_NAME")
        if not self.trust_gcp_release_url:
            self.trust_gcp_release_url = "https://trust.trustedrouter.com/trust/gcp-release.json"
        if not self.trust_aws_release_url:
            self.trust_aws_release_url = "https://trust.trustedrouter.com/trust/aws-release.json"
        if not self.trust_azure_release_url:
            self.trust_azure_release_url = (
                "https://trust.trustedrouter.com/trust/azure-release.json"
            )
        elif not self.trust_gcp_release_url.startswith("https://"):
            missing.append("TR_TRUST_GCP_RELEASE_URL=https://...")
        invalid_release_fallbacks = [
            url
            for url in self.trust_gcp_release_fallback_url_list
            if not url.startswith("https://")
        ]
        if invalid_release_fallbacks:
            missing.append("TR_TRUST_GCP_RELEASE_FALLBACK_URLS must contain HTTPS URLs")
        # OAuth providers are independently optional in production. We DO
        # enforce that no provider is half-configured: a client_id without
        # the matching client_secret would cause silent runtime failures.
        if bool(self.google_client_id) != bool(self.google_client_secret):
            missing.append(
                "TR_GOOGLE_CLIENT_ID and TR_GOOGLE_CLIENT_SECRET must both be set or both unset"
            )
        if bool(self.github_client_id) != bool(self.github_client_secret):
            missing.append(
                "TR_GITHUB_CLIENT_ID and TR_GITHUB_CLIENT_SECRET must both be set or both unset"
            )
        configured_aliases = {
            value.strip().lower().rstrip(".")
            for value in self.trusted_domain_aliases.split(",")
            if value.strip()
        }
        for provider, canonical_enabled, raw_credentials in (
            (
                "GOOGLE",
                self.google_oauth_credentials_configured,
                self.google_alias_credentials_json,
            ),
            (
                "GITHUB",
                self.github_oauth_credentials_configured,
                self.github_alias_credentials_json,
            ),
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
            if alias_credentials and not canonical_enabled:
                missing.append(
                    f"{setting_name} requires canonical TR_{provider}_CLIENT_ID and "
                    f"TR_{provider}_CLIENT_SECRET"
                )
            if canonical_enabled:
                missing_oauth_aliases = sorted(configured_aliases - set(alias_credentials))
                if missing_oauth_aliases:
                    missing.append(
                        f"{setting_name} is missing configured domain(s): "
                        + ", ".join(missing_oauth_aliases)
                    )
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"production configuration is not fail-closed: {joined}")
        return self

    @property
    def google_oauth_enabled(self) -> bool:
        if self.service_surface == "public":
            if self.google_oauth_login_available is not None:
                return self.google_oauth_login_available
            return self.google_oauth_credentials_configured
        if self.service_surface == "observer":
            return False
        return self.google_oauth_credentials_configured

    @property
    def github_oauth_enabled(self) -> bool:
        if self.service_surface == "public":
            if self.github_oauth_login_available is not None:
                return self.github_oauth_login_available
            return self.github_oauth_credentials_configured
        if self.service_surface == "observer":
            return False
        return self.github_oauth_credentials_configured

    @property
    def google_oauth_credentials_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def github_oauth_credentials_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def trust_gcp_release_fallback_url_list(self) -> tuple[str, ...]:
        return _comma_set(self.trust_gcp_release_fallback_urls)

    @property
    def trust_aws_accepted_pcr0_list(self) -> tuple[str, ...]:
        """Every PCR0 a verifier should accept, primary first.

        The primary is always a member. A bind window that widened the accepted
        set but forgot the currently-serving value would otherwise publish a set
        that rejects the enclave answering the request.
        """
        return _comma_set(self.trust_aws_accepted_pcr0s, primary=self.trust_aws_pcr0)

    @property
    def trust_azure_accepted_hostdata_list(self) -> tuple[str, ...]:
        return _comma_set(self.trust_azure_accepted_hostdata, primary=self.trust_azure_hostdata)

    @property
    def trust_azure_attestation_issuer_list(self) -> tuple[str, ...]:
        return _comma_set(self.trust_azure_attestation_issuers)

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
    def routable_configured(self) -> bool:
        return bool(self.routable_enabled and self.routable_credentials_configured)

    @property
    def routable_credentials_configured(self) -> bool:
        return bool(
            self.routable_api_token
            and self.routable_webhook_secret
            and self.routable_company_id
            and self.routable_team_member_id
            and self.routable_withdraw_from_account_id
        )

    @property
    def phone_verification_funding_enforced(self) -> bool:
        if self.phone_verification_requires_funding is not None:
            return self.phone_verification_requires_funding
        return self.environment.lower() not in {"local", "test"}

    @property
    def custom_models_verification_enforced(self) -> bool:
        if self.custom_models_require_verification is not None:
            return self.custom_models_require_verification
        return self.environment.lower() not in {"local", "test"}

    @property
    def veriff_configured(self) -> bool:
        return bool(self.veriff_api_key and self.veriff_shared_secret_key)

    @property
    def adyen_checkout_ready(self) -> bool:
        return bool(
            self.adyen_enabled
            and self.adyen_api_key
            and self.adyen_client_key
            and self.adyen_hmac_key
            and self.adyen_reference_key
            and self.adyen_merchant_account
            and (self.adyen_environment == "test" or self.adyen_live_endpoint_prefix)
        )

    @property
    def adyen_webhook_ready(self) -> bool:
        return bool(
            self.adyen_hmac_key and self.adyen_reference_key and self.adyen_merchant_account
        )

    @property
    def regional_quota_lease_pilot_workspaces(self) -> frozenset[str]:
        return frozenset(
            workspace_id.strip()
            for workspace_id in self.regional_quota_lease_pilot_workspace_ids.split(",")
            if workspace_id.strip()
        )

    @property
    def spend_lease_pilot_workspaces(self) -> frozenset[str]:
        return frozenset(
            workspace_id.strip()
            for workspace_id in self.spend_lease_pilot_workspace_ids.split(",")
            if workspace_id.strip()
        )

    @property
    def trust_qualifying_provider_set(self) -> frozenset[str]:
        return frozenset(
            provider.strip().lower()
            for provider in self.trust_qualifying_providers.split(",")
            if provider.strip()
        )

    @property
    def operator_identity_set(self) -> frozenset[str]:
        return frozenset(
            identity.strip()
            for identity in self.operator_identities.split(",")
            if identity.strip()
        )

    @property
    def spend_lease_accepted_gcp_digests(self) -> frozenset[str]:
        return frozenset(
            digest.strip()
            for digest in self.spend_lease_accepted_gcp_image_digests.split(",")
            if digest.strip()
        )

    @property
    def stage_d_pilot_workspaces(self) -> frozenset[str]:
        return frozenset(
            workspace_id.strip()
            for workspace_id in self.stage_d_pilot_workspace_ids.split(",")
            if workspace_id.strip()
        )

    @property
    def regional_quota_bigtable_app_profile_map(self) -> dict[str, str]:
        profiles: dict[str, str] = {}
        for raw_entry in self.regional_quota_bigtable_app_profiles.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            region, separator, profile = entry.partition("=")
            region = region.strip()
            profile = profile.strip()
            if not separator or not region or not profile:
                raise ValueError(
                    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES entries must be region=app-profile"
                )
            if region in profiles:
                raise ValueError(
                    "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES contains a duplicate region"
                )
            profiles[region] = profile
        return profiles

    @property
    def spend_lease_bigtable_app_profile_map(self) -> dict[str, str]:
        profiles: dict[str, str] = {}
        for raw_entry in self.spend_lease_bigtable_app_profiles.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            region, separator, profile = entry.partition("=")
            region = region.strip()
            profile = profile.strip()
            if not separator or not region or not profile:
                raise ValueError(
                    "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES entries must be region=app-profile"
                )
            if region in profiles:
                raise ValueError(
                    "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES contains a duplicate region"
                )
            profiles[region] = profile
        return profiles

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
    "ses_alert_from_email",
    "ses_alert_from_name",
    "ses_alert_configuration_set",
    "attribution_cookie_key",
    "attribution_cookie_secret",
    "internal_gateway_token",
    "stripe_webhook_secret",
    "stripe_secret_key",
    "veriff_enabled",
    "veriff_api_key",
    "veriff_shared_secret_key",
    "veriff_base_url",
    "paypal_client_id",
    "paypal_client_secret",
    "paypal_webhook_id",
    "paypal_api_base_url",
    "routable_enabled",
    "routable_api_token",
    "routable_webhook_secret",
    "routable_company_id",
    "routable_team_member_id",
    "routable_withdraw_from_account_id",
    "routable_api_base_url",
    "adyen_api_key",
    "adyen_client_key",
    "adyen_hmac_key",
    "adyen_reference_key",
    "adyen_merchant_account",
    "adyen_live_endpoint_prefix",
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
