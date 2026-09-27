"""Exercise emitted SQL variants using production builders and parameter maps."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import product
from types import SimpleNamespace
from typing import Any

from google.cloud.spanner_v1 import param_types as pt
from google.rpc.status_pb2 import Status

from trusted_router import storage_gcp_counter_dml as counters
from trusted_router.spend_windows import window_floors
from trusted_router.storage_gcp_batch_dml import DmlStatement
from trusted_router.storage_gcp_generation_records import generation_insert_statement
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_gcp_request_records import (
    _SETTLED_PAYLOAD_SQL,
    _settled_payload_sql,
    gateway_authorization_insert_statement,
    gateway_authorization_retention_clear_statement,
    gateway_authorization_settled_statement,
)
from trusted_router.storage_gcp_settle_outbox import (
    _DONE_ROW_SQL,
    SpannerSettleOutbox,
    done_retention_statements,
    speculative_done_statements,
)
from trusted_router.storage_gcp_strict_budget import reserve_strict_key
from trusted_router.storage_models import GatewayAuthorization, Generation, SettleOutboxRow
from trusted_router.types import UsageType

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class SQLCase:
    name: str
    statements: list[DmlStatement]
    batch: bool = False
    seed: list[DmlStatement] | None = None
    expected_counts: list[int] | None = None


class Capture:
    """Only records emitted SQL; does not interpret or accept any SQL dialect."""
    def __init__(self):
        self.statements = []

    def execute_update(self, sql, *, params=None, param_types=None):
        self.statements.append((sql, params or {}, param_types or {}))
        return 0  # also collect the release_key rollover fallback

    def batch_update(self, statements):
        self.statements.extend(statements)
        return Status(), [1] * len(statements)

    def execute_sql(self, sql, *, params=None, param_types=None):
        self.statements.append((sql, params or {}, param_types or {}))
        return []


def builder_cases() -> list[SQLCase]:
    cases = []
    authorization = GatewayAuthorization(
        id="acceptance-auth", workspace_id="acceptance-ws", key_hash="acceptance-key",
        model_id="acceptance-model", provider="acceptance-provider", usage_type=UsageType.CREDITS,
        estimated_microdollars=1, credit_reservation_id="acceptance-reservation",
        finalization_outcome="settled", finalized_cost_microdollars=1,
    )
    generation = Generation(
        id="acceptance-generation", request_id="acceptance-request", workspace_id="acceptance-ws",
        key_hash="acceptance-key", model="acceptance-model", provider_name="acceptance-provider",
        app="acceptance", tokens_prompt=1, tokens_completion=1, total_cost_microdollars=1,
        usage_type=UsageType.CREDITS, speed_tokens_per_second=1.0, finish_reason="stop",
        status="success", streamed=False, created_at=NOW.isoformat(),
    )
    outbox = SpannerOperationalAnalyticsOutbox(None, pt)
    insert_auth = gateway_authorization_insert_statement(pt, authorization, created_at=NOW)
    insert_admission = gateway_authorization_insert_statement(
        pt, replace(authorization, spend_lease_admission_receipt="{}", spend_lease_receipt_hash="a" * 64),
        created_at=NOW,
    )
    insert_reservation = counters.reservation_insert_statement(
        pt, reservation_id="acceptance-reservation", workspace_id="acceptance-ws",
        key_hash="acceptance-key", ws_shard=0, credit_shard=0, key_shard=0,
        credit_reserved_micro=1, key_reserved_micro=1, hold_usage_type="credits",
        authorization_id="acceptance-auth", idempotency_scope="acceptance-scope",
        idempotency_fingerprint="acceptance-fingerprint", expires_at=NOW, created_at=NOW,
    )
    entity = counters.entity_insert_statement(pt, "acceptance", "acceptance", "{}")
    reserve_key = counters.reserve_key_statement(pt, "acceptance-key", 1, is_byok=False)
    gen = generation_insert_statement(pt, generation, terminal_at=NOW)
    activity = outbox.activity_insert_statement(generation)
    for name, statement in (("authorization", insert_auth), ("admission", insert_admission),
                            ("reservation", insert_reservation), ("entity", entity),
                            ("reserve-key", reserve_key), ("generation", gen), ("activity", activity)):
        cases.append(SQLCase(name, [statement]))
    # The common speculative authorize shapes, including fallback ownership.
    cases.append(SQLCase("authorize-batch", [reserve_key, insert_reservation, insert_auth], batch=True))
    cases.append(SQLCase("authorize-legacy-batch", [reserve_key, insert_reservation, entity], batch=True))
    cases.append(SQLCase("authorize-sequential-batch", [insert_reservation, insert_auth], batch=True))
    cases.append(SQLCase("authorize-sequential-legacy-batch", [insert_reservation, entity], batch=True))
    assert _settled_payload_sql() == _SETTLED_PAYLOAD_SQL
    settled = gateway_authorization_settled_statement(pt, authorization)
    cases.append(SQLCase("settle-metadata-batch", [settled, gen, activity], batch=True, seed=[insert_auth]))
    # Materialize all six preserved-heartbeat null/non-null combinations. These
    # are DATA variants of one SQL shape, not 64 hand-written SQL alternatives.
    from trusted_router.storage_gcp_request_records import _AUTHORIZATION_HEARTBEAT_FIELDS

    heartbeat_values: dict[str, Any] = {
        "started_at": NOW.isoformat(), "heartbeat_seq": 1, "heartbeat_at": NOW.isoformat(),
        "heartbeat_hash": "hash", "selected_endpoint_id": "endpoint", "delivered_usage": {"tokens": 1},
    }
    for bits in product((False, True), repeat=len(_AUTHORIZATION_HEARTBEAT_FIELDS)):
        fields = {field: heartbeat_values[field] if bit else None for field, bit in zip(_AUTHORIZATION_HEARTBEAT_FIELDS, bits, strict=True)}
        seed = gateway_authorization_insert_statement(pt, replace(authorization, **fields), created_at=NOW)
        cases.append(SQLCase("settled-heartbeat-" + "".join(str(int(bit)) for bit in bits), [settled], seed=[seed], expected_counts=[1]))
    for guarded, expired, deferred in product((False, True), repeat=3):
        claim = counters.claim_reservation_statement(
            pt, "acceptance-reservation", actual_micro=1, settled_usage_type="credits",
            terminal_at=NOW, outbox_available=guarded, expires_before=NOW if expired else None,
            defer_retention=deferred,
        )
        cases.append(SQLCase(f"claim-{guarded}-{expired}-{deferred}", [claim]))
    clear = [gateway_authorization_retention_clear_statement(pt, "acceptance-auth"),
             counters.reservation_retention_clear_statement(pt, "acceptance-reservation")]
    cases.append(SQLCase("enqueue-retention-clear-batch", clear, batch=True))
    for reservation_id in (None, "acceptance-reservation"):
        statements = done_retention_statements(pt, authorization_id="acceptance-auth", intent_kind="settle",
                                              reservation_id=reservation_id, now=NOW.isoformat())
        cases.append(SQLCase(f"done-retention-{reservation_id}", statements, batch=True))
    cases.append(SQLCase("speculative-done-batch", speculative_done_statements(
        pt, authorization_id="acceptance-auth", intent_kind="settle", reservation_id="acceptance-reservation",
    ), batch=True))
    for byok, amounts in product((False, True), repeat=2):
        capture = Capture()
        counters.release_key(capture, pt, "acceptance-key", 1, 1, book_to_byok=byok,
                             window_floors=window_floors(NOW),
                             window_amounts={"daily": 1, "weekly": 1, "monthly": 1} if amounts else None)
        for index, statement in enumerate(capture.statements):
            cases.append(SQLCase(f"release-key-{byok}-{amounts}-{index}", [statement]))
    for enforce in (False, True):
        capture = Capture()
        reserve_strict_key(capture, pt, "acceptance-key", 1, is_byok=False, enforce_windows=enforce)
        for index, statement in enumerate(capture.statements):
            cases.append(SQLCase(f"strict-key-{enforce}-{index}", [statement]))
    for claim_hold, done_outbox, include_generation, include_activity in product((False, True), repeat=4):
        writes = []
        if claim_hold:
            writes.append(counters.claim_reservation_statement(
                pt, "acceptance-reservation", actual_micro=1, settled_usage_type="credits", terminal_at=NOW,
            ))
        writes.append(settled)
        if done_outbox:
            writes.extend(speculative_done_statements(
                pt, authorization_id="acceptance-auth", intent_kind="settle", reservation_id="acceptance-reservation",
            ))
        if include_generation:
            writes.append(gen)
        if include_activity:
            writes.append(activity)
        cases.append(SQLCase(f"settle-batch-{claim_hold}-{done_outbox}-{include_generation}-{include_activity}", writes, batch=True, seed=[insert_auth, insert_reservation]))
    for has_reservation, refill in product((False, True), repeat=2):
        capture = Capture()
        database = SimpleNamespace(run_in_transaction=lambda fn, capture=capture, **_kw: fn(capture))
        row = SettleOutboxRow(
            authorization_id="acceptance-auth", intent_kind="settle", settle_origin="typed",
            actual_cost_micro=1, reservation_id="acceptance-reservation" if has_reservation else None,
            auto_refill_workspace_id="acceptance-ws" if refill else None,
        )
        SpannerSettleOutbox(database, pt).enqueue(row)
        cases.append(SQLCase(f"outbox-enqueue-batch-{has_reservation}-{refill}", capture.statements, batch=True))
        if has_reservation and not refill:
            params = {"aid": "acceptance-auth", "kind": "settle", "lease_owner": None,
                      "status": "done", "now": NOW.isoformat()}
            types = {name: pt.TIMESTAMP if name == "now" else pt.STRING for name in params}
            cases.append(SQLCase("guarded-done-returning", [(_DONE_ROW_SQL, params, types)],
                                 seed=[capture.statements[0]], expected_counts=[1]))
    for floors in (None, {0: NOW}, {shard: NOW for shard in range(32)}):
        capture = Capture()
        database = SimpleNamespace(snapshot=lambda capture=capture: nullcontext(capture))
        SpannerOperationalAnalyticsOutbox(database, pt).oldest_enqueued_at(floors=floors)
        cases.append(SQLCase(f"oldest-full-shards-{len(floors or {})}", capture.statements))
    return cases
