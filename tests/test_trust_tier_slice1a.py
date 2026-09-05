from __future__ import annotations

import ast
import datetime as dt
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trusted_router.trust_tier_cli import run as run_trust_tier_job
from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from trusted_router.config import Settings
from trusted_router.spend_lease_state import Created, SpendLease
from trusted_router.spend_leases import SpendLeaseArtifact, mint_shadow_spend_lease
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp_counter_dml import reserve_credit_for_spend_lease
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_COLUMNS,
    CREDIT_BALANCE_TABLE,
    CREDIT_BALANCE_TRUST_COLUMNS,
)
from trusted_router.storage_gcp_credit_shard_admin import _RESHARD_COLUMNS
from trusted_router.storage_gcp_spend_lease_authorize import BindingPlan, prepare_candidate
from trusted_router.storage_gcp_trust import TRUST_EVENT_COLUMNS as GCP_TRUST_EVENT_COLUMNS
from trusted_router.storage_models import (
    CreditAccount,
    CreditProvenance,
    TrustEvent,
    User,
    Workspace,
)
from trusted_router.trust_tiers import (
    TRUST_EVENT_DEBIT_STATUSES,
    TRUST_EVENT_KINDS,
    TRUST_EVENT_LIFECYCLE_STATUSES,
    TRUST_EVENT_PROVIDERS,
    compute_trust_tier,
    payment_or_grant_event,
)

ROOT = Path(__file__).parents[1]
NOW = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
TRUST_COLUMNS = (
    "trust_tier",
    "trust_computed_at",
    "trust_latched_at",
    "trust_override_tier",
    "billing_pause_causes",
    "pause_epoch",
    "trust_reconciled_through",
)
TRUST_EVENT_COLUMNS = (
    "workspace_id",
    "event_id",
    "kind",
    "provider",
    "amount_micro",
    "original_payment_ref",
    "adverse_ref",
    "occurred_at",
    "recorded_at",
    "payment_amount_micro",
    "currency",
    "credited_micro",
    "recovered_micro",
    "provider_subtype",
    "lifecycle_status",
    "cumulative_refunded",
    "recovery_target",
    "debit_status",
    "unrecovered_micro",
    "provider_ordering_watermark",
)


def _create_columns(ddl: str, table: str) -> tuple[str, ...]:
    match = re.search(
        rf"CREATE TABLE(?: IF NOT EXISTS)? {table} \((.*?)\n\s*(?:\) PRIMARY KEY|\);)",
        ddl,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(
        line.strip().split()[0]
        for line in match.group(1).splitlines()
        if line.strip()
        and not line.strip().startswith(("CONSTRAINT", "PRIMARY KEY"))
    )


def _event(
    *,
    kind: str = "payment",
    provider: str = "stripe",
    credited_micro: int = 50_000_000,
    payment_amount_micro: int | None = None,
    occurred_at: dt.datetime = NOW - dt.timedelta(days=31),
    lifecycle_status: str = "succeeded",
) -> TrustEvent:
    return TrustEvent(
        workspace_id="workspace",
        event_id=f"{kind}-{provider}-{occurred_at.timestamp()}",
        kind=kind,
        provider=provider,
        amount_micro=payment_amount_micro or credited_micro,
        original_payment_ref="pi_qualifying" if kind == "payment" else None,
        adverse_ref="adverse" if kind in {"refund", "dispute"} else None,
        occurred_at=occurred_at,
        recorded_at=NOW,
        payment_amount_micro=(payment_amount_micro or credited_micro)
        if kind == "payment"
        else None,
        currency="USD",
        credited_micro=credited_micro,
        recovered_micro=0,
        provider_subtype="test",
        lifecycle_status=lifecycle_status,
        cumulative_refunded=0,
        recovery_target=0,
        debit_status=None,
        unrecovered_micro=0,
        provider_ordering_watermark=None,
    )


def test_trust_event_schemas_have_exact_columns_statuses_and_dedup_keys() -> None:
    ddl = (ROOT / "scripts/deploy/migrate_typed_counters.sh").read_text()
    postgres = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()

    assert _create_columns(ddl, "tr_trust_event") == TRUST_EVENT_COLUMNS
    assert _create_columns(postgres, "tr_trust_event") == TRUST_EVENT_COLUMNS
    assert len(TRUST_EVENT_COLUMNS) == 20
    assert GCP_TRUST_EVENT_COLUMNS == TRUST_EVENT_COLUMNS
    assert all(f"'{status}'" in ddl for status in TRUST_EVENT_LIFECYCLE_STATUSES)
    assert all(f"'{status}'" in postgres for status in TRUST_EVENT_LIFECYCLE_STATUSES)
    for allowed in (
        TRUST_EVENT_KINDS,
        TRUST_EVENT_PROVIDERS,
        TRUST_EVENT_DEBIT_STATUSES,
    ):
        assert all(f"'{value}'" in ddl for value in allowed)
        assert all(f"'{value}'" in postgres for value in allowed)
    assert (
        "CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_adverse_dedup\n"
        "    ON tr_trust_event (provider, adverse_ref, kind)"
    ) in ddl
    assert (
        "CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_payment_dedup\n"
        "    ON tr_trust_event (provider, original_payment_ref, kind)"
    ) in ddl


def test_postgres_sql_has_no_targetless_on_conflict_do_nothing() -> None:
    postgres_source = (ROOT / "src/trusted_router/storage_postgres.py").read_text()
    targetless = re.compile(
        r"\bON\s+CONFLICT\s+DO\s+NOTHING\b",
        flags=re.IGNORECASE,
    )
    violations = [
        node.lineno
        for node in ast.walk(ast.parse(postgres_source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and targetless.search(node.value)
    ]

    assert violations == []
    assert "ON CONFLICT (workspace_id, event_id) DO NOTHING" in postgres_source
    assert (
        "ON CONFLICT (provider, original_payment_ref, kind) DO NOTHING"
        in postgres_source
    )


def test_every_balance_schema_copy_and_validation_site_uses_same_seven_columns() -> None:
    spanner = (ROOT / "scripts/deploy/migrate_typed_counters.sh").read_text()
    postgres = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    backfill = (
        ROOT / "scripts/deploy/backfill_credit_balance_trust.sql"
    ).read_text()
    fresh_spanner = _create_columns(spanner, "tr_credit_balance")
    fresh_postgres = _create_columns(postgres, "tr_credit_balance")
    assert len(fresh_spanner) == len(fresh_postgres) == 14
    backfill_columns = tuple(
        match.group(1)
        for match in re.finditer(
            r"^\s*(?:SET )?([a-z_]+)\s*=", backfill, flags=re.MULTILINE
        )
    )
    ensure_columns = tuple(
        match.group(1)
        for match in re.finditer(
            r"ensure_column tr_credit_balance ([a-z_]+)", spanner
        )
    )
    sites = {
        "canonical": CREDIT_BALANCE_TRUST_COLUMNS,
        "creation seed": CREDIT_BALANCE_COLUMNS[3:10],
        "reshard copy": _RESHARD_COLUMNS[5:12],
        "fresh Spanner DDL": tuple(c for c in fresh_spanner if c in TRUST_COLUMNS),
        "existing Spanner DDL": ensure_columns,
        "fresh Postgres DDL": tuple(c for c in fresh_postgres if c in TRUST_COLUMNS),
        "historical backfill": backfill_columns,
    }

    assert sites == {name: TRUST_COLUMNS for name in sites}


def test_new_balance_shards_seed_all_seven_trust_columns() -> None:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace(owner_user_id="owner", name="trust seed")

    rows = [
        row
        for (workspace_id, _), row in database.typed[CREDIT_BALANCE_TABLE].items()
        if workspace_id == workspace.id
    ]
    assert rows
    assert all(
        (
            row["trust_tier"],
            row["trust_computed_at"],
            row["trust_latched_at"],
            row["trust_override_tier"],
            tuple(row["billing_pause_causes"]),
            row["pause_epoch"],
            row["trust_reconciled_through"],
        )
        == (0, None, None, None, (), 0, None)
        for row in rows
    )


def test_starter_credit_records_a_nonqualifying_provisioning_fact_atomically() -> None:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace(
        owner_user_id="owner",
        name="starter credit",
        trial_credit_microdollars=123,
    )

    event_id = f"provisioning:{workspace.id}"
    fact = database.typed["tr_trust_event"][(workspace.id, event_id)]
    assert (
        fact["kind"],
        fact["provider"],
        fact["provider_subtype"],
        fact["original_payment_ref"],
        fact["credited_micro"],
    ) == ("grant", "system", "provisioning", None, 123)
    balance_version = database.typed_versions[(CREDIT_BALANCE_TABLE, (workspace.id, 0))]
    assert database.typed_versions[("tr_trust_event", (workspace.id, event_id))] == balance_version


def test_credit_provenance_is_mandatory_validated_and_committed_with_credit() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "workspace-payment-fact"
    store._write_entity("credit", workspace_id, CreditAccount(workspace_id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": 0,
        "total_usage": 0,
        "reserved": 0,
    }
    provenance = CreditProvenance("checkout", "stripe", "pi_123", NOW)

    with pytest.raises(TypeError, match="provenance"):
        store.credit_workspace_typed_direct(workspace_id, 100, "event-missing")
    assert store.credit_workspace_typed_direct(
        workspace_id,
        100,
        "event-payment",
        provenance=provenance,
        payment_amount_microdollars=120,
        currency="usd",
    )

    fact = database.typed["tr_trust_event"][(workspace_id, "event-payment")]
    assert (
        fact["kind"],
        fact["provider"],
        fact["original_payment_ref"],
        fact["amount_micro"],
        fact["payment_amount_micro"],
        fact["currency"],
        fact["credited_micro"],
    ) == (
        "payment",
        "stripe",
        "pi_123",
        120,
        120,
        "USD",
        100,
    )
    marker_version = database.rows[("stripe_event", "event-payment")].version
    assert database.typed_versions[(CREDIT_BALANCE_TABLE, (workspace_id, 0))] == marker_version
    assert database.typed_versions[("tr_trust_event", (workspace_id, "event-payment"))] == marker_version
    assert not store.credit_workspace_typed_direct(
        workspace_id,
        100,
        "different-webhook-event",
        provenance=provenance,
        payment_amount_microdollars=120,
        currency="USD",
    )
    assert (
        database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["total_credits"]
        == 100
    )
    assert (workspace_id, "different-webhook-event") not in database.typed["tr_trust_event"]

    with pytest.raises(ValueError, match="provider object reference"):
        payment_or_grant_event(
            workspace_id,
            "webhook-id-is-not-a-reference",
            100,
            CreditProvenance("checkout", "stripe", None, NOW),
            recorded_at=NOW,
        )
    with pytest.raises(ValueError, match="PaymentIntent id"):
        payment_or_grant_event(
            workspace_id,
            "webhook-id-is-not-a-reference",
            100,
            CreditProvenance("checkout", "stripe", "evt_webhook", NOW),
            recorded_at=NOW,
            payment_amount_microdollars=100,
            currency="USD",
        )


def test_legacy_credit_wrapper_preserves_missing_account_error() -> None:
    store = InMemoryStore()

    with pytest.raises(ValueError, match="^credit_account_not_found$"):
        store.credit_workspace_once("missing", 100, "missing-credit-account")

    assert not store.stripe_events
    assert not store.trust_events


def test_postgres_payment_reference_dedup_is_atomic_with_credit() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "payment dedup")
    provenance = CreditProvenance("checkout", "stripe", "pi_pg_dedup", NOW)

    assert store.credit_workspace_typed_direct(
        workspace.id,
        100,
        "webhook-a",
        provenance=provenance,
        payment_amount_microdollars=120,
        currency="USD",
    )
    assert not store.credit_workspace_typed_direct(
        workspace.id,
        100,
        "webhook-b",
        provenance=provenance,
        payment_amount_microdollars=120,
        currency="USD",
    )
    total = conn.execute(
        "SELECT total_credits FROM tr_credit_balance WHERE workspace_id = %s AND shard = 0",
        (workspace.id,),
    ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM tr_trust_event WHERE workspace_id = %s",
        (workspace.id,),
    ).fetchone()
    assert total is not None and int(total[0]) == 100
    assert count is not None and int(count[0]) == 1


def test_postgres_starter_credit_uses_primary_key_dedup_target() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)

    workspace = store.create_workspace(
        "owner",
        "starter credit",
        trial_credit_microdollars=123,
    )

    row = conn.execute(
        "SELECT kind, provider, credited_micro FROM tr_trust_event "
        "WHERE workspace_id = %s AND event_id = %s",
        (workspace.id, f"provisioning:{workspace.id}"),
    ).fetchone()
    assert row == ("grant", "system", 123)


def test_every_credit_ingress_pins_the_provider_object_reference_kind() -> None:
    sources = {
        "Stripe Checkout PaymentIntent": (
            "src/trusted_router/routes/internal/webhook.py",
            'source="checkout"',
            'provider="stripe"',
            'external_ref=str(obj.get("payment_intent") or "")',
        ),
        "Stripe auto-refill PaymentIntent": (
            "src/trusted_router/routes/internal/webhook.py",
            'source="auto_refill"',
            'provider="stripe"',
            'external_ref=str(obj.get("id") or "")',
        ),
        "PayPal capture": (
            "src/trusted_router/services/paypal_billing.py",
            'source="capture"',
            'provider="paypal"',
            "external_ref=capture_id",
        ),
        "Adyen authorisation pspReference": (
            "src/trusted_router/services/adyen_billing.py",
            'source="authorisation"',
            'provider="adyen"',
            "external_ref=result.psp_reference",
        ),
        "x402 PaymentIntent": (
            "src/trusted_router/services/x402_billing.py",
            'source="x402"',
            'provider="x402"',
            "external_ref=payment_intent_id",
        ),
        "operator grant": (
            "scripts/grant_credit.py",
            'source="grant"',
            'provider="operator"',
            "external_ref=None",
        ),
        "system provisioning": (
            "src/trusted_router/synthetic/funding.py",
            'source="provisioning"',
            'provider="system"',
            "external_ref=None",
        ),
        "creator pilot provisioning": (
            "scripts/provision_creator_pilot.py",
            'source="provisioning"',
            'provider="system"',
            "external_ref=None",
        ),
        "synthetic monitor provisioning": (
            "scripts/provision_synthetic_monitor.py",
            'source="provisioning"',
            'provider="system"',
            "external_ref=None",
        ),
        "earnings transfer ingress": (
            "src/trusted_router/storage_gcp.py",
            'source="grant"',
            'provider="system"',
            "external_ref=None",
        ),
        "federated credit-transfer ingress": (
            "src/trusted_router/storage_gcp_credit_transfer.py",
            'source="grant"',
            'provider="system"',
            "external_ref=None",
        ),
    }

    for label, (path, source, provider, external_ref) in sources.items():
        text = (ROOT / path).read_text()
        assert source in text, label
        assert provider in text, label
        assert external_ref in text, label


@pytest.mark.parametrize(
    ("events", "identity", "latch", "override", "computed", "effective"),
    [
        ([], "none", None, None, 0, 0),
        ([_event()], "none", None, None, 1, 1),
        ([_event()], "approved", None, None, 3, 3),
        (
            [_event(payment_amount_micro=49_999_999)],
            "approved",
            None,
            None,
            2,
            2,
        ),
        (
            [_event(payment_amount_micro=50_000_000)],
            "approved",
            None,
            None,
            3,
            3,
        ),
        (
            [_event(occurred_at=NOW - dt.timedelta(days=30) + dt.timedelta(microseconds=1))],
            "approved",
            None,
            None,
            2,
            2,
        ),
        (
            [_event(occurred_at=NOW - dt.timedelta(days=30))],
            "approved",
            None,
            None,
            3,
            3,
        ),
        (
            [_event(credited_micro=49_000_000, payment_amount_micro=50_000_000)],
            "approved",
            None,
            None,
            3,
            3,
        ),
        ([_event(provider="paypal")], "approved", None, None, 0, 0),
        ([_event(lifecycle_status="pending")], "approved", None, None, 0, 0),
        ([_event(), _event(kind="refund")], "approved", None, None, 2, 2),
        ([], "approved", None, 2, 0, 2),
        ([], "none", None, 3, 0, 1),
        ([_event()], "approved", NOW, 3, 3, 0),
    ],
)
def test_pure_tier_computation(
    events: list[TrustEvent],
    identity: str,
    latch: dt.datetime | None,
    override: int | None,
    computed: int,
    effective: int,
) -> None:
    decision = compute_trust_tier(
        events,
        owner_identity_status=identity,
        trust_latched_at=latch,
        trust_override_tier=override,
        qualifying_providers=frozenset({"stripe", "x402"}),
        tier3_min_days=30,
        tier3_min_paid_microdollars=50_000_000,
        now=NOW,
    )

    assert (decision.computed_tier, decision.effective_tier) == (computed, effective)


def test_tier_job_updates_every_active_shard_atomically_and_never_clears_latch() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "workspace-tier-job"
    store._write_entity(
        "user", "owner", User(id="owner", email="owner@example.com", identity_status="approved")
    )
    store._write_entity(
        "workspace",
        workspace_id,
        Workspace(id=workspace_id, name="tier", owner_user_id="owner"),
    )
    store._write_entity(
        "credit", workspace_id, CreditAccount(workspace_id=workspace_id, shard_count=2)
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard in range(2):
        table[(workspace_id, shard)] = {
            "workspace_id": workspace_id,
            "shard": shard,
            "total_credits": 0,
            "total_usage": 0,
            "reserved": 0,
            "trust_tier": 0,
            "trust_computed_at": None,
            "trust_latched_at": None,
            "trust_override_tier": None,
            "billing_pause_causes": [],
            "pause_epoch": 0,
            "trust_reconciled_through": None,
        }
    assert store.credit_workspace_typed_direct(
        workspace_id,
        50_000_000,
        "payment-event",
        provenance=CreditProvenance(
            "checkout", "stripe", "pi_tier3", NOW - dt.timedelta(days=31)
        ),
        payment_amount_microdollars=50_000_000,
        currency="USD",
    )

    assert store.recompute_workspace_trust_tier(
        workspace_id,
        qualifying_providers=frozenset({"stripe", "x402"}),
        tier3_min_days=30,
        tier3_min_paid_microdollars=50_000_000,
        now=NOW,
    ) == 3
    assert {(row["trust_tier"], row["trust_computed_at"]) for row in table.values()} == {
        (3, NOW)
    }

    for row in table.values():
        row["trust_latched_at"] = NOW
    assert store.recompute_workspace_trust_tier(
        workspace_id,
        qualifying_providers=frozenset({"stripe", "x402"}),
        tier3_min_days=30,
        tier3_min_paid_microdollars=50_000_000,
        now=NOW + dt.timedelta(minutes=1),
    ) == 0
    assert {row["trust_latched_at"] for row in table.values()} == {NOW}


def test_cloud_run_tier_job_enumerates_every_workspace_with_pinned_policy() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Store:
        def list_trust_tier_workspace_ids(self) -> tuple[str, ...]:
            return ("workspace-a", "workspace-b")

        def recompute_workspace_trust_tier(
            self, workspace_id: str, **kwargs: Any
        ) -> int:
            calls.append((workspace_id, kwargs))
            return 1

    settings = SimpleNamespace(
        trust_qualifying_provider_set=frozenset({"stripe", "x402"}),
        trust_tier3_min_days=30,
        trust_tier3_min_paid_microdollars=50_000_000,
    )

    assert run_trust_tier_job(Store(), settings, now=NOW) == 2
    assert [workspace_id for workspace_id, _ in calls] == [
        "workspace-a",
        "workspace-b",
    ]
    assert all(
        kwargs
        == {
            "qualifying_providers": frozenset({"stripe", "x402"}),
            "tier3_min_days": 30,
            "tier3_min_paid_microdollars": 50_000_000,
            "now": NOW,
        }
        for _, kwargs in calls
    )


class _RecordingTransaction:
    def __init__(self, *, count: int = 1, tier: int = 1, latch: Any = None) -> None:
        self.count = count
        self.tier = tier
        self.latch = latch
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def execute_update(
        self, sql: str, *, params: dict[str, Any], param_types: dict[str, Any]
    ) -> int:
        self.calls.append((sql, params, param_types))
        return self.count

    def execute_sql(self, *_args: Any, **_kwargs: Any) -> list[list[Any]]:
        return [[self.tier, self.latch]]


def test_arm_off_mint_sql_is_byte_for_byte_origin_main_golden() -> None:
    transaction = _RecordingTransaction()

    assert reserve_credit_for_spend_lease(
        transaction,
        _ParamTypes,
        "workspace",
        123,
        shard=7,
        trust_eligibility_enabled=False,
        expected_trust_tier=None,
    )

    assert transaction.calls == [
        (
            "UPDATE tr_credit_balance SET reserved = reserved + @est "
            "WHERE workspace_id=@ws AND shard=@shard "
            "AND (total_credits - total_usage - reserved) >= @est",
            {"est": 123, "ws": "workspace", "shard": 7},
            {"est": "INT64", "ws": "STRING", "shard": "INT64"},
        )
    ]


def test_armed_mint_requires_expected_trust_tier_before_dml() -> None:
    transaction = _RecordingTransaction()

    with pytest.raises(
        ValueError,
        match="expected_trust_tier is required while trust eligibility is armed",
    ):
        reserve_credit_for_spend_lease(
            transaction,
            _ParamTypes,
            "workspace",
            123,
            shard=7,
            trust_eligibility_enabled=True,
            expected_trust_tier=None,
        )

    assert transaction.calls == []


@pytest.mark.parametrize("trust_snapshot", [(0, None), (1, NOW)])
def test_armed_binding_precheck_rejects_unpaid_snapshot_before_dml(
    monkeypatch: pytest.MonkeyPatch,
    trust_snapshot: tuple[int, dt.datetime | None],
) -> None:
    import trusted_router.storage_gcp_spend_lease_authorize as authorize

    store, database, _ = make_fake_store()
    monkeypatch.setattr(authorize, "reservation_exists", lambda *_args: False)
    monkeypatch.setattr(
        type(store),
        "typed_credit_trust_snapshot",
        lambda _self, _workspace_id: trust_snapshot,
    )

    result = store.prepare_gateway_spend_lease_binding(
        workspace_id="workspace",
        key_hash="key",
        authorization_id="authorization",
        idempotency_key="idempotency",
        idempotency_fingerprint="fingerprint",
        estimate=1,
        boot_kid="boot",
        region="region",
        signer=object(),
        catalog={},
        ttl_seconds=60,
        skew_seconds=10,
        max_microdollars=100,
        max_available_basis_points=10_000,
        echo_lease_id=None,
        echo_state=None,
        trust_eligibility_enabled=True,
    )

    assert result == (None, "unpaid_workspace")
    assert database.transaction_execute_update_calls == 0


def _binding_plan(transaction_tier: int, latch: Any = None) -> BindingPlan:
    return BindingPlan(
        ledger=SimpleNamespace(),
        scope="scope",
        fence_id="fence",
        region="region",
        provisional_id="authorization",
        artifact=SpendLeaseArtifact(
            "token", "lease", 123, 1, 1, 2, "issuer", "boot", "catalog"
        ),
        allocation_micro=1,
        admission_deadline=NOW,
        mode="mint",
        candidate=None,
        observed_gen=0,
        incumbent_lease_id=None,
        incumbent_window_closed=True,
        authoritative_exhaustion=False,
        trust_eligibility_enabled=True,
        expected_trust_tier=1,
    )


@pytest.mark.parametrize(
    ("tier", "latch", "reason"),
    [(0, None, "unpaid_workspace"), (1, NOW, "unpaid_workspace"), (1, None, "escrow_headroom")],
)
def test_armed_zero_row_rereads_selected_shard_and_classifies_reason(
    tier: int, latch: dt.datetime | None, reason: str
) -> None:
    transaction = _RecordingTransaction(count=0, tier=tier, latch=latch)

    result = _binding_plan(tier, latch)._mint_hook(
        transaction, _ParamTypes, "workspace", 7
    )

    assert result["no_lease_reason"] == reason
    sql, params, _ = transaction.calls[0]
    assert sql.endswith(
        "AND trust_tier = @expected_trust_tier AND trust_tier >= 1 "
        "AND trust_latched_at IS NULL"
    )
    assert params["expected_trust_tier"] == 1


@pytest.mark.parametrize(
    ("tier", "latch"),
    [(0, None), (1, NOW), (2, None)],
)
def test_armed_admission_reuse_rechecks_trust_before_registration(
    tier: int,
    latch: dt.datetime | None,
) -> None:
    transaction = _RecordingTransaction(tier=tier, latch=latch)
    plan = BindingPlan(
        ledger=SimpleNamespace(),
        scope="scope",
        fence_id="fence",
        region="region",
        provisional_id="authorization",
        artifact=SpendLeaseArtifact(
            "token", "lease", 123, 1, 1, 2, "issuer", "boot", "catalog"
        ),
        allocation_micro=1,
        admission_deadline=NOW,
        mode="reuse",
        candidate=None,
        observed_gen=1,
        incumbent_lease_id="lease",
        incumbent_window_closed=False,
        authoritative_exhaustion=False,
        trust_eligibility_enabled=True,
        expected_trust_tier=1,
    )

    result = plan.transaction_hook(
        transaction,
        _ParamTypes,
        "workspace",
        7,
    )

    assert result == {
        "bound": False,
        "no_lease_reason": "unpaid_workspace",
        "spend_lease_outcome": "escrow_refused",
    }
    assert transaction.calls == []


class _CapturingSigner:
    kid = "issuer"

    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []

    def sign(self, claims: dict[str, Any]) -> str:
        self.claims.append(dict(claims))
        return "signed"


def test_arm_off_shadow_claim_is_byte_for_byte_origin_main_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_id = uuid.UUID("00000000-0000-0000-0000-000000000123")
    monkeypatch.setattr("trusted_router.spend_leases.uuid.uuid4", lambda: lease_id)
    signer = _CapturingSigner()

    mint_shadow_spend_lease(
        signer=signer,
        key_hash="key",
        workspace_id="workspace",
        boot_kid="boot",
        cap_micro=123,
        gen=4,
        catalog={"version": "v1", "entries": []},
        ttl_seconds=60,
        now=1_700_000_000,
    )

    assert signer.claims == [
        {
            "v": 1,
            "typ": "spend-lease+jws",
            "authoritative": False,
            "lease_id": str(lease_id),
            "key_hash": "key",
            "workspace_id": "workspace",
            "cohort": "credits-chat-v1",
            "cap_micro": 123,
            "gen": 4,
            "iat": 1_700_000_000,
            "exp": 1_700_000_060,
            "boot_kid": "boot",
            "catalog": {"version": "v1", "entries": []},
        }
    ]


class _Ledger:
    def initialize(self, lease: SpendLease, **_kwargs: Any) -> None:
        self.lease = lease

    def allocate(self, _view: Any, _lease_id: str, **kwargs: Any) -> Created:
        result = self.lease.allocate(
            authorization_view=None,
            idempotency_scope=kwargs["idempotency_scope"],
            provisional_authorization_id=kwargs["provisional_authorization_id"],
            request_fingerprint=kwargs["request_fingerprint"],
            allocated_micro=kwargs["allocated_micro"],
            abandon_after=kwargs["abandon_after"],
            now=kwargs["now"],
        )
        assert isinstance(result, Created)
        self.lease = result.lease
        return result


def test_arm_off_authoritative_claim_has_origin_main_fields_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.storage_gcp_spend_lease_authorize as authorize

    class FrozenDatetime:
        @classmethod
        def now(cls, timezone: dt.tzinfo) -> dt.datetime:
            assert timezone is dt.UTC
            return NOW

    monkeypatch.setattr(authorize, "datetime", FrozenDatetime)
    signer = _CapturingSigner()
    database = FakeSpannerDatabase()

    prepare_candidate(
        database=database,
        param_types=_ParamTypes,
        ledger=_Ledger(),
        signer=signer,
        scope="scope",
        fence_id="fence",
        provisional_id="authorization",
        workspace_id="workspace",
        key_hash="key",
        boot_kid="boot",
        region="region",
        gen=1,
        cap_micro=123,
        allocation_micro=1,
        ttl_seconds=60,
        skew_seconds=10,
        request_fingerprint="fingerprint",
        catalog={"version": "v1", "entries": []},
        observed_gen=0,
        observed_predecessor_count=0,
        incumbent_lease_id=None,
        incumbent_window_closed=True,
        authoritative_exhaustion=False,
    )

    assert signer.claims[0] == {
        "v": 1,
        "typ": "spend-lease+jws",
        "authoritative": True,
        "lease_id": signer.claims[0]["lease_id"],
        "key_hash": "key",
        "workspace_id": "workspace",
        "cohort": "credits-chat-v1",
        "cap_micro": 123,
        "gen": 1,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 60,
        "boot_kid": "boot",
        "catalog": {"version": "v1", "entries": []},
    }
    assert "trust_tier" not in signer.claims[0]


def test_armed_shadow_and_authoritative_claims_carry_the_effective_tier() -> None:
    shadow_signer = _CapturingSigner()
    mint_shadow_spend_lease(
        signer=shadow_signer,
        key_hash="key",
        workspace_id="workspace",
        boot_kid="boot",
        cap_micro=123,
        gen=4,
        catalog={"version": "v1", "entries": []},
        ttl_seconds=60,
        now=1_700_000_000,
        trust_tier=2,
    )
    assert shadow_signer.claims[0]["trust_tier"] == 2

    authoritative_signer = _CapturingSigner()
    prepare_candidate(
        database=FakeSpannerDatabase(),
        param_types=_ParamTypes,
        ledger=_Ledger(),
        signer=authoritative_signer,
        scope="scope",
        fence_id="fence",
        provisional_id="authorization",
        workspace_id="workspace",
        key_hash="key",
        boot_kid="boot",
        region="region",
        gen=1,
        cap_micro=123,
        allocation_micro=1,
        ttl_seconds=60,
        skew_seconds=10,
        request_fingerprint="fingerprint",
        catalog={"version": "v1", "entries": []},
        observed_gen=0,
        observed_predecessor_count=0,
        incumbent_lease_id=None,
        incumbent_window_closed=True,
        authoritative_exhaustion=False,
        trust_eligibility_enabled=True,
        expected_trust_tier=2,
    )
    assert authoritative_signer.claims[0]["trust_tier"] == 2


def test_new_settings_are_inert_and_pinned_to_scope_defaults() -> None:
    settings = Settings(environment="test")

    assert settings.spend_lease_trust_eligibility_enabled is False
    assert settings.trust_qualifying_provider_set == frozenset({"stripe", "x402"})
    assert settings.trust_tier3_min_days == 30
    assert settings.trust_tier3_min_paid_microdollars == 50_000_000
    assert settings.max_workspaces_per_owner == 25
    assert settings.operator_token == ""
    assert settings.operator_identities == ""
    assert settings.trust_reconcile_interval_seconds == 900
    assert settings.trust_reconcile_max_age_seconds == 3_600
    assert (
        settings.spend_lease_tier1_cap_microdollars,
        settings.spend_lease_tier2_cap_microdollars,
        settings.spend_lease_tier3_cap_microdollars,
    ) == (5_000_000, 25_000_000, 100_000_000)
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    assert '"TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"' in rollout


def test_reconciliation_freshness_covers_delay_plus_two_cadences() -> None:
    with pytest.raises(ValueError, match="consistency delay"):
        Settings(
            environment="test",
            trust_reconcile_interval_seconds=300,
            trust_reconcile_max_age_seconds=1_499,
        )

    settings = Settings(
        environment="test",
        trust_reconcile_interval_seconds=300,
        trust_reconcile_max_age_seconds=1_500,
    )
    assert settings.trust_reconcile_max_age_seconds == 1_500

    with pytest.raises(ValueError, match="consistency delay"):
        Settings(environment="test", trust_qualifying_providers="paypal")
    paypal = Settings(
        environment="test",
        trust_qualifying_providers="paypal",
        trust_reconcile_max_age_seconds=12_600,
    )
    assert paypal.trust_reconcile_max_age_seconds == 12_600
