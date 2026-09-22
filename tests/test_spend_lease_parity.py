"""P1/P2 evidence must survive the production sink with admission disabled."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_operational_analytics_direct import _load_drainer, _row
from tests.test_stage_c_admission import _base_body, _canonical, _request, _seed_store, _settings
from trusted_router.operational_analytics_direct import normalise_operational_event
from trusted_router.receipt_keys import b64url_decode
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.spend_leases import (
    SpendLeaseEchoValue,
    SpendLeaseSigner,
    build_spend_lease_shadow_event,
)


@pytest.mark.parametrize(("enclave", "label"), [(99, "estimate_low"), (100, "estimate_equal"), (101, "estimate_high")])
def test_exact_frozen_parity_and_binding_reach_both_sinks(enclave: int, label: str) -> None:
    event = build_spend_lease_shadow_event(
        event_id="evt-1", created_at="2026-09-01T00:00:00Z", workspace_id="ws", key_hash="key",
        boot_kid="boot", boot_verified=True, no_lease_reason=None, binding_outcome="reuse_bound",
        echo=SpendLeaseEchoValue("lease", "active", 1000, enclave, "frozen", True),
        server_estimate_micro=200, frozen_server_estimate_micro=100,
        comparison_catalog_version="frozen", applicability_drift="catalog_changed", server_verdict="accepted",
    )
    assert event.divergence == label
    payload = event.payload()
    drainer = _load_drainer()
    for normalise in (normalise_operational_event, drainer.normalise_operational_event):
        row = normalise(_row("spend_lease_shadow", payload))[0].row
        assert row["binding_outcome"] == "reuse_bound"
        assert row["divergence"] == label
        assert row["enclave_estimate_micro"] == enclave
        assert row["server_estimate_micro"] == 200
        assert row["frozen_server_estimate_micro"] == 100
        assert row["comparison_catalog_version"] == "frozen"
        assert row["applicability_drift"] == "catalog_changed"


@pytest.mark.parametrize("comparison_version", [None, "different"])
def test_missing_or_wrong_snapshot_never_counts_as_equality(comparison_version: str | None) -> None:
    event = build_spend_lease_shadow_event(
        event_id="evt-1", created_at="now", workspace_id="ws", key_hash="key", boot_kid="boot",
        boot_verified=True, no_lease_reason=None,
        echo=SpendLeaseEchoValue("lease", "active", 1000, 100, "frozen", True),
        server_estimate_micro=100, frozen_server_estimate_micro=100,
        comparison_catalog_version=comparison_version, server_verdict="accepted",
    )
    assert event.divergence == "not_comparable"


def test_ordinary_binding_observes_retained_frozen_catalog_with_admission_off(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _database, key, private, boot = _seed_store()
    settings = _settings(key.workspace_id, boot.image_digest).model_copy(update={"spend_lease_admission_accept": False})
    contexts: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway, "_spend_lease_signer", lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))))
    monkeypatch.setattr(gateway, "_record_spend_lease_shadow", lambda context, **_kwargs: contexts.append(dict(context)))
    mint = _base_body(key, "parity-mint")
    raw = _canonical(mint)
    result = gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**mint), settings, raw)
    claims = json.loads(b64url_decode(result["data"]["spend_lease"]["token"].split(".")[1]))
    assert claims.get("local_admission_allowed", False) is False
    estimate = result["data"]["estimated_cost_microdollars"]
    body = {**_base_body(key, "parity-observe"), "spend_lease_echo": {
        "lease_id": claims["lease_id"], "state": "active", "remaining_micro": claims["cap_micro"] - estimate,
        "enclave_estimate_micro": estimate, "catalog_version": claims["catalog"]["version"], "would_admit": True,
    }}
    # A live price change must affect current estimates, never frozen parity.
    original = gateway.freeze_spend_lease_catalog

    def changed_catalog(*args: Any, **kwargs: Any) -> Any:
        return {**original(*args, **kwargs), "version": "changed-live-catalog"}

    monkeypatch.setattr(gateway, "freeze_spend_lease_catalog", changed_catalog)
    raw = _canonical(body)
    gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**body), settings, raw)
    context = contexts[-1]
    assert context["frozen_server_estimate_micro"] == estimate
    assert context["comparison_catalog_version"] == claims["catalog"]["version"]
    assert context["applicability_drift"] == "catalog_changed"
    assert context["binding_outcome"] == "reuse_bound"

    active = store.get_active_spend_lease(key.hash, boot.kid)
    assert active is not None
    monkeypatch.setattr(type(store), "get_active_spend_lease", lambda *_args: replace(active, lease_id="replaced"))
    body["idempotency_key"] = "parity-missing"
    raw = _canonical(body)
    gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**body), settings, raw)
    assert contexts[-1].get("frozen_server_estimate_micro") is None
    assert contexts[-1]["applicability_drift"] == "snapshot_unavailable"


def test_parity_migrations_are_forward_only_replicated_single_node_pair() -> None:
    root = Path(__file__).resolve().parents[1] / "clickhouse"
    replicated = (root / "016_spend_lease_parity.sql").read_text()
    single = (root / "017_spend_lease_parity_single_node.sql").read_text()
    assert replicated.replace("tr.spend_lease_shadow ON CLUSTER trustedrouter", "spend_lease_shadow") == single
    for name in ("binding_outcome", "frozen_server_estimate_micro", "comparison_catalog_version", "applicability_drift"):
        assert f"ADD COLUMN IF NOT EXISTS {name} Nullable(" in replicated


@pytest.mark.parametrize("binding_outcome", ["mint_bound", "reuse_bound"])
@pytest.mark.parametrize(("router_id", "echo_id", "has_echo"), [
    ("router-lease", None, False),
    ("router-lease", None, True),
    ("same-lease", "same-lease", True),
    ("router-lease", "different-enclave-lease", True),
    (None, "enclave-only-lease", True),
    (None, None, False),
])
def test_router_and_echo_identities_survive_both_sinks(
    router_id: str | None, echo_id: str | None, has_echo: bool, binding_outcome: Any,
) -> None:
    event = build_spend_lease_shadow_event(
        event_id="evt-1", created_at="2026-09-21T00:00:00Z", workspace_id="ws",
        key_hash="key", boot_kid="boot", boot_verified=True, no_lease_reason=None,
        router_lease_id=router_id, binding_outcome=binding_outcome,
        echo=SpendLeaseEchoValue(echo_id, "active", 1000, 100, "catalog", True) if has_echo else None,
        server_estimate_micro=100, server_verdict="accepted",
    )
    assert event.lease_id == router_id
    assert event.echo_lease_id == echo_id
    from trusted_router import operational_analytics_direct as direct

    drainer = _load_drainer()
    for sink in (direct, drainer):
        assert {"lease_id", "echo_lease_id", "binding_outcome"} <= set(sink.SPEND_LEASE_SHADOW_COLUMNS)
        row = sink.normalise_operational_event(_row("spend_lease_shadow", event.payload()))[0].row
        assert row["lease_id"] == router_id
        assert row["echo_lease_id"] == echo_id
        assert row["binding_outcome"] == binding_outcome
        # Old queued payloads stay readable without inventing enclave evidence.
        old_payload = event.payload()
        old_payload.pop("echo_lease_id")
        assert sink.normalise_operational_event(_row("spend_lease_shadow", old_payload))[0].row["echo_lease_id"] is None


def test_authoritative_mint_and_reuse_without_echo_record_committed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key, private, boot = _seed_store()
    settings = _settings(key.workspace_id, boot.image_digest).model_copy(update={
        "spend_lease_admission_accept": False, "spend_lease_admission_workspace_ids": "",
    })
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway, "_spend_lease_signer", lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))))
    monkeypatch.setattr(gateway._SPEND_LEASE_SHADOW_DISPATCHER, "submit", lambda _id, payload: rows.append(payload))
    lease_ids = []
    for index, expected_outcome in enumerate(("mint_bound", "reuse_bound")):
        body = _base_body(key, f"no-echo-{index}")
        raw = _canonical(body)
        result = gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**body), settings, raw)
        authorization = store.get_gateway_authorization(result["data"]["authorization_id"])
        assert authorization is not None
        claims = json.loads(b64url_decode(result["data"]["spend_lease"]["token"].split(".")[1]))
        assert claims.get("local_admission_allowed", False) is False
        assert rows[-1]["lease_id"] == authorization.spend_lease_id == claims["lease_id"]
        assert rows[-1]["echo_lease_id"] is None
        assert rows[-1]["binding_outcome"] == expected_outcome
        lease_ids.append(claims["lease_id"])
    assert lease_ids[0] == lease_ids[1]
    replay = gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**body), settings, raw)
    assert replay["data"]["idempotent_replay"] is True
    assert rows[-1]["lease_id"] == lease_ids[0]
    assert rows[-1]["echo_lease_id"] is None
    assert rows[-1]["binding_outcome"] == "replay"


def test_failed_binding_never_records_provisional_lease_as_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.spend_lease_authorize import SpendLeaseMintLost
    from trusted_router.storage_gcp_spend_lease_authorize import BindingPlan

    store, _database, key, private, boot = _seed_store()
    settings = _settings(key.workspace_id, boot.image_digest).model_copy(update={
        "spend_lease_admission_accept": False, "spend_lease_admission_workspace_ids": "",
    })
    rows: list[dict[str, Any]] = []
    provisional_ids: list[str] = []

    def lose_mint(self: Any, *_args: Any) -> Any:
        provisional_ids.append(self.artifact.lease_id)
        raise SpendLeaseMintLost("lost")

    monkeypatch.setattr(BindingPlan, "transaction_hook", lose_mint)
    monkeypatch.setattr(gateway, "_spend_lease_signer", lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))))
    monkeypatch.setattr(gateway._SPEND_LEASE_SHADOW_DISPATCHER, "submit", lambda _id, payload: rows.append(payload))
    body = _base_body(key, "lost-mint")
    raw = _canonical(body)
    result = gateway._authorize_gateway_sync(_request(raw, private, boot.kid), GatewayAuthorizeRequest(**body), settings, raw)
    authorization = store.get_gateway_authorization(result["data"]["authorization_id"])
    assert authorization is not None
    assert provisional_ids
    assert authorization.spend_lease_id is None
    assert rows[-1]["lease_id"] is None
    assert rows[-1]["echo_lease_id"] is None
    assert rows[-1]["binding_outcome"] == "mint_lost"


def test_identity_migrations_are_forward_only_and_wired_before_ingester() -> None:
    root = Path(__file__).resolve().parents[1]
    replicated = (root / "clickhouse/018_spend_lease_identity.sql").read_text()
    single = (root / "clickhouse/019_spend_lease_identity_single_node.sql").read_text()
    assert replicated.replace("tr.spend_lease_shadow ON CLUSTER trustedrouter", "spend_lease_shadow") == single
    assert "ADD COLUMN IF NOT EXISTS echo_lease_id Nullable(String)" in replicated
    deploy = (root / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    assert 'SPEND_LEASE_IDENTITY_SCHEMA="${ROOT}/clickhouse/018_spend_lease_identity.sql"' in deploy
    assert deploy.index('node_query 0 "$spend_lease_identity_schema"') < deploy.index('build_clickhouse_bundle "$ROOT"')
