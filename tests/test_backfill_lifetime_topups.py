from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import backfill_lifetime_topups as backfill
from tests.fakes.spanner import make_fake_store
from trusted_router.services import adyen_billing, paypal_billing
from trusted_router.storage_models import Workspace
from trusted_router.typed_balance import live_credit_summary

PRE_CUTOVER = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
POST_CUTOVER = int(datetime(2026, 8, 16, 6, tzinfo=UTC).timestamp())
REFERENCE_KEY = "backfill-test-reference-signing-key-123456789"


class _Page:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def auto_paging_iter(self) -> Any:
        yield from self.rows


class _ListEndpoint:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Page:
        self.calls.append(kwargs)
        return _Page(self.rows)


class FakeStripe:
    def __init__(
        self,
        *,
        sessions: list[dict[str, Any]] | None = None,
        payment_intents: list[dict[str, Any]] | None = None,
        charges: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session_endpoint = _ListEndpoint(sessions or [])
        self.payment_intent_endpoint = _ListEndpoint(payment_intents or [])
        self.charge_endpoint = _ListEndpoint(charges or [])
        self.checkout = SimpleNamespace(
            Session=SimpleNamespace(list=self.session_endpoint.list)
        )
        self.PaymentIntent = SimpleNamespace(list=self.payment_intent_endpoint.list)
        self.Charge = SimpleNamespace(list=self.charge_endpoint.list)


def _user_and_workspace(store: Any, email: str) -> tuple[Any, Any]:
    user = store.ensure_user(email)
    workspace = store.list_workspaces_for_user(user.id)[0]
    return user, workspace


def _session(
    payment_intent_id: str,
    workspace_id: str | None,
    amount_total: int,
    *,
    created: int = PRE_CUTOVER,
    mode: str = "payment",
    payment_status: str = "paid",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    combined = dict(metadata or {})
    if workspace_id is not None:
        combined.setdefault("workspace_id", workspace_id)
    return {
        "id": f"cs_{payment_intent_id}",
        "mode": mode,
        "payment_status": payment_status,
        "payment_intent": {"id": payment_intent_id},
        "amount_total": amount_total,
        "created": created,
        "metadata": combined,
    }


def _charge(
    payment_intent_id: str,
    amount: int,
    *,
    refunded: int = 0,
    disputed: bool = False,
) -> dict[str, Any]:
    return {
        "payment_intent": payment_intent_id,
        "amount": amount,
        "amount_refunded": refunded,
        "disputed": disputed,
    }


def _read_report(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return records[:-1], records[-1]


def _record_by_ref(records: list[dict[str, Any]], provider_ref: str) -> dict[str, Any]:
    return next(record for record in records if record["provider_ref"] == provider_ref)


def _seed_claim(store: Any, event_id: str, created_at: str = "2026-08-01T00:00:05Z") -> None:
    store._write_entity("stripe_event", event_id, {"created_at": created_at})


def test_stripe_dry_run_covers_amount_rules_refunds_filters_and_aggregation(
    tmp_path: Path,
) -> None:
    store, _database, _bigtable = make_fake_store()
    alice, alice_first = _user_and_workspace(store, "alice@example.com")
    alice_second = store.create_workspace(alice.id, "Alice Two")
    bob, bob_workspace = _user_and_workspace(store, "bob@example.com")
    _charlie, charlie_workspace = _user_and_workspace(store, "charlie@example.com")

    net_metadata = {
        "credit_amount_microdollars": "2000000",
        "processing_fee_cents": "80",
        "charge_amount_cents": "280",
    }
    sessions = [
        _session("pi_legacy", alice_first.id, 500),
        _session("pi_net", alice_second.id, 280, metadata=net_metadata),
        _session("pi_bob", bob_workspace.id, 2500),
        _session("pi_partial", alice_first.id, 500),
        _session("pi_refunded", alice_first.id, 100),
        _session("pi_disputed", alice_first.id, 300),
        _session("pi_feather", None, 24900, metadata={"tier": "feather"}),
        _session("pi_setup", alice_first.id, 0, mode="setup", payment_status="no_payment_required"),
        _session("pi_unpaid", alice_first.id, 400, payment_status="unpaid"),
        _session(
            "pi_post",
            alice_first.id,
            100,
            created=POST_CUTOVER,
            metadata={"initiating_user_id": alice.id},
        ),
        # stamped but created before the constant: the stamp decides
        # (Phase 2 accrued it) and the row is flagged
        _session(
            "pi_initiator",
            alice_first.id,
            100,
            metadata={"initiating_user_id": alice.id},
        ),
        _session("pi_unknown", "missing-workspace", 100),
    ]
    payment_intents = [
        {
            "id": "pi_auto",
            "status": "succeeded",
            "amount": 300,
            "created": PRE_CUTOVER,
            "metadata": {
                "workspace_id": alice_second.id,
                "auto_refill": "true",
                "amount_microdollars": "3000000",
            },
        },
        {
            "id": "pi_x402",
            "status": "succeeded",
            "amount": 500,
            "amount_received": 400,
            "created": PRE_CUTOVER,
            "metadata": {
                "workspace_id": bob_workspace.id,
                "payment_method": "x402",
                "amount_microdollars": "5000000",
            },
        },
        # The secondary feed must dedupe a PI already represented by Checkout.
        {
            "id": "pi_legacy",
            "status": "succeeded",
            "amount": 500,
            "created": PRE_CUTOVER,
            "metadata": {
                "workspace_id": alice_first.id,
                "auto_refill": "true",
                "amount_microdollars": "5000000",
            },
        },
    ]
    stripe = FakeStripe(
        sessions=sessions,
        payment_intents=payment_intents,
        charges=[
            _charge("pi_partial", 500, refunded=100),
            _charge("pi_refunded", 100, refunded=100),
            _charge("pi_disputed", 300, disputed=True),
        ],
    )
    report = tmp_path / "report.jsonl"
    rows_before = set(_database.rows)

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report)],
            store=store,
            stripe_client=stripe,
        )
        == 0
    )

    assert store.get_lifetime_topup_microdollars(alice.id) == 0
    assert store.get_lifetime_topup_microdollars(bob.id) == 0
    assert set(_database.rows) == rows_before
    records, summary = _read_report(report)
    required_fields = {
        "source",
        "provider_ref",
        "workspace_id",
        "user_id",
        "user_email",
        "amount_microdollars",
        "gross_cents",
        "provider_created_at",
        "payment_method",
        "decision",
        "reason",
        "backfill_event_id",
        "status",
    }
    assert all(required_fields <= record.keys() for record in records)
    assert summary["record_type"] == "summary"
    assert _record_by_ref(records, "pi_legacy")["amount_microdollars"] == 5_000_000
    assert _record_by_ref(records, "pi_net")["amount_microdollars"] == 2_000_000
    partial = _record_by_ref(records, "pi_partial")
    assert partial["amount_microdollars"] == 4_000_000
    assert partial["partial_refund"] is True
    assert _record_by_ref(records, "pi_refunded")["reason"] == "refunded"
    assert _record_by_ref(records, "pi_disputed")["reason"] == "disputed"
    assert _record_by_ref(records, "pi_feather")["reason"] == "no_workspace_metadata"
    assert _record_by_ref(records, "pi_setup")["decision"] == "exclude"
    assert _record_by_ref(records, "pi_unpaid")["decision"] == "exclude"
    post = _record_by_ref(records, "pi_post")
    assert post["reason"] == "post_cutover" and post["cutover_witness_disagreement"] is False
    initiator = _record_by_ref(records, "pi_initiator")
    assert initiator["reason"] == "post_cutover"
    assert initiator["cutover_witness_disagreement"] is True
    assert summary["cutover_witness_disagreements"] == ["pi_initiator"]
    assert _record_by_ref(records, "pi_unknown")["reason"] == "unknown_workspace"
    assert len([record for record in records if record["provider_ref"] == "pi_legacy"]) == 1
    per_user = {row["user_id"]: row for row in summary["per_user"]}
    assert per_user[alice.id]["delta"] == 14_000_000
    assert per_user[bob.id]["delta"] == 29_000_000
    assert summary["would_apply_microdollars"] == 43_000_000
    assert summary["users_crossing_25_dollars"] == [bob.id]
    assert stripe.session_endpoint.calls == [
        {"limit": 100, "expand": ["data.payment_intent"]}
    ]
    assert stripe.payment_intent_endpoint.calls == [{"limit": 100}]
    assert stripe.charge_endpoint.calls == [{"limit": 100}]


def test_deleted_workspace_attributes_raw_unknown_and_federated_skip(tmp_path: Path) -> None:
    store, _database, _bigtable = make_fake_store()
    owner, deleted_workspace = _user_and_workspace(store, "deleted@example.com")
    store.update_workspace(deleted_workspace.id, deleted=True)
    assert store.get_workspace(deleted_workspace.id) is None
    federated_id = "federated-workspace"
    store._write_entity(
        "workspace",
        federated_id,
        Workspace(
            id=federated_id,
            name="Shadow",
            owner_user_id=owner.id,
            federated_home="aws",
        ),
    )
    stripe = FakeStripe(
        sessions=[
            _session("pi_deleted", deleted_workspace.id, 500),
            _session("pi_missing", "unknown-workspace", 300),
            _session("pi_federated", federated_id, 200),
        ]
    )
    report = tmp_path / "raw-workspaces.jsonl"

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report)],
            store=store,
            stripe_client=stripe,
        )
        == 0
    )

    records, summary = _read_report(report)
    deleted = _record_by_ref(records, "pi_deleted")
    assert deleted["decision"] == "include"
    assert deleted["user_id"] == owner.id
    assert _record_by_ref(records, "pi_missing")["reason"] == "unknown_workspace"
    assert _record_by_ref(records, "pi_federated")["reason"] == "no_owner"
    assert summary["would_apply_microdollars"] == 5_000_000


def test_post_cutover_user_with_existing_lifetime_total_has_zero_delta(
    tmp_path: Path,
) -> None:
    store, _database, _bigtable = make_fake_store()
    joseph, workspace = _user_and_workspace(store, "joseph@example.com")
    assert store.add_lifetime_topup(joseph.id, 25_000_000, "evt_post_cutover_topup")
    report = tmp_path / "joseph.jsonl"
    stripe = FakeStripe(
        sessions=[
            _session(
                "pi_joseph_post_cutover",
                workspace.id,
                2500,
                created=POST_CUTOVER,
                metadata={"initiating_user_id": joseph.id},
            )
        ]
    )

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report)],
            store=store,
            stripe_client=stripe,
        )
        == 0
    )

    records, summary = _read_report(report)
    record = _record_by_ref(records, "pi_joseph_post_cutover")
    assert record["reason"] == "post_cutover"
    assert record["user_id"] == joseph.id
    assert summary["would_apply_microdollars"] == 0
    assert summary["per_user"] == [
        {
            "user_id": joseph.id,
            "email": "joseph@example.com",
            "before": 25_000_000,
            "delta": 0,
            "after": 25_000_000,
        }
    ]


def test_paypal_claim_feed_supports_all_three_custom_id_formats(tmp_path: Path) -> None:
    store, _database, _bigtable = make_fake_store()
    bare_user, bare_workspace = _user_and_workspace(store, "bare@example.com")
    tr1_user, tr1_workspace = _user_and_workspace(store, "tr1@example.com")
    json_owner, json_workspace = _user_and_workspace(store, "json-owner@example.com")
    initiator, _initiator_workspace = _user_and_workspace(store, "initiator@example.com")
    captures = {
        "cap_bare": {
            "id": "cap_bare",
            "status": "COMPLETED",
            "custom_id": bare_workspace.id,
            "amount": {"currency_code": "USD", "value": "5.00"},
            "create_time": "2026-08-01T00:00:00Z",
        },
        "cap_tr1": {
            "id": "cap_tr1",
            "status": "COMPLETED",
            "custom_id": f"tr1|{tr1_workspace.id}|200|280",
            "amount": {"currency_code": "USD", "value": "2.80"},
            "create_time": "2026-08-01T00:01:00Z",
        },
        "cap_json": {
            "id": "cap_json",
            "status": "COMPLETED",
            "custom_id": json.dumps(
                {"w": json_workspace.id, "u": initiator.id, "c": 2500, "t": 2580}
            ),
            "amount": {"currency_code": "USD", "value": "25.80"},
            "create_time": "2026-08-01T00:02:00Z",
        },
    }
    for capture_id in captures:
        _seed_claim(store, f"paypal_capture:{capture_id}")
    report = tmp_path / "paypal.jsonl"

    assert (
        backfill.main(
            ["--report", str(report)],
            store=store,
            stripe_client=FakeStripe(),
            paypal_fetch=lambda capture_id: captures[capture_id],
        )
        == 0
    )

    records, summary = _read_report(report)
    assert _record_by_ref(records, "cap_bare")["user_id"] == bare_user.id
    assert _record_by_ref(records, "cap_bare")["amount_microdollars"] == 5_000_000
    assert _record_by_ref(records, "cap_tr1")["user_id"] == tr1_user.id
    assert _record_by_ref(records, "cap_tr1")["amount_microdollars"] == 2_000_000
    assert json_owner.id != initiator.id
    assert _record_by_ref(records, "cap_json")["user_id"] == initiator.id
    assert _record_by_ref(records, "cap_json")["amount_microdollars"] == 25_000_000
    assert summary["would_apply_microdollars"] == 32_000_000


def test_paypal_csv_supplements_claims_without_credentials(tmp_path: Path) -> None:
    store, _database, _bigtable = make_fake_store()
    user, workspace = _user_and_workspace(store, "paypal-csv@example.com")
    csv_path = tmp_path / "paypal.csv"
    csv_path.write_text(
        "capture_id,workspace_id,credit_amount_microdollars,created_at\n"
        f"cap_csv,{workspace.id},7000000,2026-08-01T00:00:00Z\n",
        encoding="utf-8",
    )
    report = tmp_path / "paypal-csv.jsonl"

    assert (
        backfill.main(
            ["--paypal-csv", str(csv_path), "--report", str(report)],
            store=store,
            stripe_client=FakeStripe(),
        )
        == 0
    )

    records, summary = _read_report(report)
    assert _record_by_ref(records, "cap_csv")["user_id"] == user.id
    assert summary["would_apply_microdollars"] == 7_000_000


def test_adyen_claim_uses_signed_merchant_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TR_ADYEN_REFERENCE_KEY", REFERENCE_KEY)
    store, _database, _bigtable = make_fake_store()
    user, workspace = _user_and_workspace(store, "adyen-backfill@example.com")
    merchant_reference = adyen_billing._new_checkout_reference(
        workspace_id=workspace.id,
        credit_amount_cents=500,
        charge_amount_cents=580,
        reference_key=REFERENCE_KEY,
    )
    _seed_claim(store, f"adyen_checkout:{merchant_reference}")
    report = tmp_path / "adyen.jsonl"

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report)],
            store=store,
            stripe_client=FakeStripe(),
        )
        == 0
    )

    records, summary = _read_report(report)
    record = _record_by_ref(records, merchant_reference)
    assert record["user_id"] == user.id
    assert record["amount_microdollars"] == 5_000_000
    assert record["gross_cents"] == 580
    assert summary["would_apply_microdollars"] == 5_000_000


def test_manual_grants_are_off_by_default_and_only_explicit_csv_is_included(
    tmp_path: Path,
) -> None:
    store, _database, _bigtable = make_fake_store()
    user, _workspace = _user_and_workspace(store, "manual@example.com")
    _seed_claim(store, "manual_makeup_pi_3TaKM1")
    report_off = tmp_path / "manual-off.jsonl"

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report_off)],
            store=store,
            stripe_client=FakeStripe(),
        )
        == 0
    )
    records_off, summary_off = _read_report(report_off)
    assert records_off == []
    assert summary_off["would_apply_microdollars"] == 0

    csv_path = tmp_path / "manual.csv"
    csv_path.write_text(
        "event_id,user_id,amount_microdollars\n"
        f"approved_support_grant,{user.id},3000000\n",
        encoding="utf-8",
    )
    report_on = tmp_path / "manual-on.jsonl"
    assert (
        backfill.main(
            [
                "--no-paypal",
                "--manual-grants-csv",
                str(csv_path),
                "--report",
                str(report_on),
            ],
            store=store,
            stripe_client=FakeStripe(),
        )
        == 0
    )
    records_on, summary_on = _read_report(report_on)
    assert [record["provider_ref"] for record in records_on] == ["approved_support_grant"]
    assert records_on[0]["backfill_event_id"] == (
        "lifetime_backfill:manual:approved_support_grant"
    )
    assert summary_on["would_apply_microdollars"] == 3_000_000


def test_pre_rotation_paid_sessions_count_but_manual_makeup_claim_does_not(
    tmp_path: Path,
) -> None:
    store, _database, _bigtable = make_fake_store()
    _user, workspace = _user_and_workspace(store, "pre-rotation@example.com")
    _seed_claim(store, "manual_makeup_pre_rotation")
    stripe = FakeStripe(
        sessions=[
            _session("pi_3TaKM1", workspace.id, 500),
            _session("pi_3TaKNR", workspace.id, 200),
        ]
    )
    report = tmp_path / "pre-rotation.jsonl"

    assert (
        backfill.main(
            ["--no-paypal", "--report", str(report)],
            store=store,
            stripe_client=stripe,
        )
        == 0
    )

    records, summary = _read_report(report)
    assert {record["provider_ref"] for record in records} == {"pi_3TaKM1", "pi_3TaKNR"}
    assert summary["would_apply_microdollars"] == 7_000_000


def test_expected_total_mismatch_refuses_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, database, _bigtable = make_fake_store()
    user, workspace = _user_and_workspace(store, "mismatch@example.com")
    before_rows = set(database.rows)

    rc = backfill.main(
        [
            "--no-paypal",
            "--apply",
            "--expected-total-microdollars",
            "1",
        ],
        store=store,
        stripe_client=FakeStripe(sessions=[_session("pi_mismatch", workspace.id, 500)]),
    )

    assert rc == 2
    assert store.get_lifetime_topup_microdollars(user.id) == 0
    assert set(database.rows) == before_rows


def test_apply_only_user_is_idempotent_and_prints_all_verification_passes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, database, _bigtable = make_fake_store()
    alice, alice_workspace = _user_and_workspace(store, "apply-alice@example.com")
    bob, bob_workspace = _user_and_workspace(store, "apply-bob@example.com")
    alice_credit_before = live_credit_summary(alice_workspace.id, store=store)
    bob_credit_before = live_credit_summary(bob_workspace.id, store=store)
    stripe = FakeStripe(
        sessions=[
            _session("pi_apply_alice", alice_workspace.id, 2500),
            _session("pi_apply_bob", bob_workspace.id, 500),
        ]
    )
    report = tmp_path / "apply.jsonl"
    argv = [
        "--no-paypal",
        "--only-user",
        alice.id,
        "--apply",
        "--expected-total-microdollars",
        "25000000",
        "--report",
        str(report),
    ]

    assert backfill.main(argv, store=store, stripe_client=stripe) == 0

    assert store.get_lifetime_topup_microdollars(alice.id) == 25_000_000
    assert store.get_lifetime_topup_microdollars(bob.id) == 0
    assert live_credit_summary(alice_workspace.id, store=store) == alice_credit_before
    assert live_credit_summary(bob_workspace.id, store=store) == bob_credit_before
    assert ("stripe_event", "lifetime_backfill:stripe:pi_apply_alice") in database.rows
    assert ("stripe_event", "lifetime_backfill:stripe:pi_apply_bob") not in database.rows
    output = capsys.readouterr().out
    assert "VERIFY balances: PASS users=1" in output
    assert "VERIFY lifetime_backfill event count: PASS" in output
    assert "VERIFY second dry-run: PASS would_apply_microdollars=0" in output
    records, summary = _read_report(report)
    assert _record_by_ref(records, "pi_apply_alice")["status"] == "applied"
    assert _record_by_ref(records, "pi_apply_bob")["status"] == "skipped"
    assert summary["verification"]["second_dry_run"]["would_apply_microdollars"] == 0

    rerun_argv = [*argv]
    expected_index = rerun_argv.index("25000000")
    rerun_argv[expected_index] = "0"
    assert backfill.main(rerun_argv, store=store, stripe_client=stripe) == 0
    assert store.get_lifetime_topup_microdollars(alice.id) == 25_000_000
    rerun_records, rerun_summary = _read_report(report)
    assert _record_by_ref(rerun_records, "pi_apply_alice")["status"] == "already_applied"
    assert rerun_summary["applied_microdollars"] == 0


def test_apply_requires_spanner_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "memory")
    store, _database, _bigtable = make_fake_store()

    assert (
        backfill.main(
            ["--no-paypal", "--apply", "--expected-total-microdollars", "0"],
            store=store,
            stripe_client=FakeStripe(),
        )
        == 2
    )


def test_stripe_claim_cross_check_is_clearly_heuristic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, _database, _bigtable = make_fake_store()
    _user, workspace = _user_and_workspace(store, "cross-check@example.com")
    provider_created = datetime.fromtimestamp(PRE_CUTOVER, tz=UTC)
    _seed_claim(
        store,
        "evt_match",
        (provider_created.replace(second=5)).isoformat().replace("+00:00", "Z"),
    )
    _seed_claim(store, "evt_unmatched", "2026-07-01T00:00:00Z")

    assert (
        backfill.main(
            ["--no-paypal"],
            store=store,
            stripe_client=FakeStripe(
                sessions=[_session("pi_cross_check", workspace.id, 500)]
            ),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "STRIPE CLAIM CROSS-CHECK (HEURISTIC +/-600s): included=1 pre_cutover_claims=2" in output
    assert 'STRIPE unmatched provider rows (heuristic): []' in output
    assert 'STRIPE unmatched claims (heuristic): ["evt_unmatched"]' in output


def test_paypal_authenticated_get_reuses_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "cap_test", "status": "COMPLETED"})

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            self.client = real_client(transport=httpx.MockTransport(handler))

        def __enter__(self) -> httpx.Client:
            return self.client

        def __exit__(self, *_args: Any) -> None:
            self.client.close()

    monkeypatch.setattr(paypal_billing, "_access_token", lambda _settings: "oauth-token")
    monkeypatch.setattr(paypal_billing.httpx, "Client", Client)
    settings = backfill.Settings(
        environment="test",
        paypal_client_id="client",
        paypal_client_secret="secret",  # noqa: S106 - inert unit-test credential
    )

    capture = paypal_billing.fetch_paypal_capture(settings, "cap_test")

    assert capture["id"] == "cap_test"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url).endswith("/v2/payments/captures/cap_test")
    assert requests[0].headers["authorization"] == "Bearer oauth-token"
    with pytest.raises(ValueError, match="invalid PayPal capture id"):
        paypal_billing.fetch_paypal_capture(settings, "../secret")


def test_cutover_witnesses_stamp_decides_and_disagreements_are_flagged(
    tmp_path: Path,
) -> None:
    """The initiating_user_id stamp is the fingerprint of the accruing code
    path, so it decides; a creation time on the other side of the cut-over
    is flagged for the operator, never guessed silently."""
    store, _database, _bigtable = make_fake_store()
    alice, workspace = _user_and_workspace(store, "alice@example.com")
    report = tmp_path / "cutover.jsonl"
    stripe = FakeStripe(
        sessions=[
            # created BEFORE the constant but stamped → old constant too late →
            # Phase 2 accrued it → excluded, flagged
            _session(
                "pi_early_stamped",
                workspace.id,
                1000,
                created=PRE_CUTOVER,
                metadata={"initiating_user_id": alice.id},
            ),
            # created AFTER the constant but unstamped → old instance still
            # crediting during rollout → NOT accrued → included, flagged
            _session("pi_late_unstamped", workspace.id, 700, created=POST_CUTOVER),
        ]
    )
    assert (
        backfill.main(["--no-paypal", "--report", str(report)], store=store, stripe_client=stripe)
        == 0
    )
    records, summary = _read_report(report)
    early = _record_by_ref(records, "pi_early_stamped")
    late = _record_by_ref(records, "pi_late_unstamped")
    assert early["decision"] == "exclude" and early["reason"] == "post_cutover"
    assert early["cutover_witness_disagreement"] is True
    assert late["decision"] == "include" and late["status"] == "would_apply"
    assert late["cutover_witness_disagreement"] is True
    assert sorted(summary["cutover_witness_disagreements"]) == [
        "pi_early_stamped",
        "pi_late_unstamped",
    ]
    assert summary["would_apply_microdollars"] == 7_000_000
