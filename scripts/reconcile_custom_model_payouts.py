"""Reconcile user-model generations against their exactly-once payouts.

The sweep is read-only by default and emits JSON Lines. Pass ``--apply`` to
credit missing payouts through the existing event-claim primitive.

Examples:
  uv run python scripts/reconcile_custom_model_payouts.py
  uv run python scripts/reconcile_custom_model_payouts.py --since 2026-08-15T00:00:00Z --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.custom_model_billing import (
    owner_share_microdollars,
    user_model_payout_event_id,
)
from trusted_router.storage import create_store
from trusted_router.storage_models import (
    CreditMovement,
    GatewayAuthorization,
    Generation,
)

DEFAULT_SINCE_HOURS = 48
_PAYOUT_KIND = "custom_model_payout"
_PAYOUT_PREFIX = "custom_model_payout:"
T = TypeVar("T")


@dataclass
class ReconciliationSummary:
    authorizations_scanned: int = 0
    generations_checked: int = 0
    missing_payouts: int = 0
    payouts_applied: int = 0
    owner_unknown: int = 0
    generation_missing: int = 0
    attribution_mismatches: int = 0
    orphan_payouts: int = 0


def reconcile_custom_model_payouts(
    store: Any,
    *,
    since: datetime,
    apply: bool = False,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> ReconciliationSummary:
    """Find missing and orphaned user-model payouts in one bounded window."""
    output = emit or _emit_json
    cutoff = _as_utc(since)
    authorizations = _scan_authorizations(store, cutoff)
    payouts = _scan_payout_movements(store, cutoff)
    payout_by_account_and_event = {
        (movement.account_id, movement.movement_id): movement for movement in payouts
    }
    summary = ReconciliationSummary(authorizations_scanned=len(authorizations))
    generation_cache: dict[str, Generation | None] = {}

    def generation(generation_id: str) -> Generation | None:
        if generation_id not in generation_cache:
            generation_cache[generation_id] = store.get_generation(generation_id)
        return generation_cache[generation_id]

    for authorization in authorizations:
        if (
            not authorization.user_provided_model_id
            or authorization.finalization_outcome != "settled"
            or not authorization.finalized_generation_id
        ):
            continue
        stored_generation = generation(authorization.finalized_generation_id)
        if stored_generation is None:
            summary.generation_missing += 1
            output(
                {
                    "type": "generation_missing",
                    "authorization_id": authorization.id,
                    "generation_id": authorization.finalized_generation_id,
                }
            )
            continue
        if (
            stored_generation.custom_model_id is None
            or stored_generation.custom_model_id
            != authorization.user_provided_model_id
        ):
            summary.attribution_mismatches += 1
            output(
                {
                    "type": "generation_attribution_mismatch",
                    "authorization_id": authorization.id,
                    "generation_id": stored_generation.id,
                    "authorization_model_id": authorization.user_provided_model_id,
                    "generation_model_id": stored_generation.custom_model_id,
                }
            )
            continue
        summary.generations_checked += 1
        # The payee is whoever owned the model when the request was
        # authorized — frozen on the authorization by Phase 5 — so a model
        # deleted since then is still reconcilable. The live model is only a
        # fallback for authorizations that predate the frozen field.
        owner_user_id = authorization.user_model_owner_user_id
        if not owner_user_id:
            model = store.get_user_model(stored_generation.custom_model_id)
            owner_user_id = model.owner_user_id if model is not None else None
        if not owner_user_id:
            summary.owner_unknown += 1
            output(
                {
                    "type": "owner_unknown",
                    "authorization_id": authorization.id,
                    "generation_id": stored_generation.id,
                    "model_id": stored_generation.custom_model_id,
                }
            )
            continue
        expected_amount = owner_share_microdollars(
            stored_generation.total_cost_microdollars
        )
        if expected_amount <= 0:
            continue
        event_id = user_model_payout_event_id(authorization.id)
        account_id = f"user:{owner_user_id}"
        if (account_id, event_id) in payout_by_account_and_event:
            continue
        summary.missing_payouts += 1
        applied = False
        if apply:
            applied = bool(
                store.credit_user_earnings(
                    owner_user_id,
                    expected_amount,
                    event_id,
                    custom_model_id=stored_generation.custom_model_id,
                    payer_workspace_id=stored_generation.workspace_id,
                )
            )
            if applied:
                summary.payouts_applied += 1
        output(
            {
                "type": "missing_payout",
                "authorization_id": authorization.id,
                "generation_id": stored_generation.id,
                "model_id": stored_generation.custom_model_id,
                "owner_user_id": owner_user_id,
                "payer_workspace_id": stored_generation.workspace_id,
                "expected_amount_microdollars": expected_amount,
                "applied": applied,
            }
        )

    authorization_cache = {authorization.id: authorization for authorization in authorizations}
    for movement in payouts:
        authorization_id = _payout_authorization_id(movement)
        authorization = (
            authorization_cache.get(authorization_id)
            if authorization_id is not None
            else None
        )
        if authorization is None and authorization_id is not None:
            authorization = store.get_gateway_authorization(authorization_id)
        generation_id = (
            authorization.finalized_generation_id if authorization is not None else None
        )
        if generation_id and generation(generation_id) is not None:
            continue
        summary.orphan_payouts += 1
        output(
            {
                "type": "orphan_payout",
                "movement_id": movement.movement_id,
                "account_id": movement.account_id,
                "authorization_id": authorization_id,
                "generation_id": generation_id,
                "amount_microdollars": movement.amount_microdollars,
            }
        )

    output(
        {
            "type": "summary",
            "since": cutoff.isoformat().replace("+00:00", "Z"),
            "apply": apply,
            **dataclasses.asdict(summary),
        }
    )
    return summary


def _scan_authorizations(store: Any, since: datetime) -> list[GatewayAuthorization]:
    target = _store_target(store)
    if hasattr(target, "api_keys") and hasattr(
        target.api_keys, "gateway_authorizations"
    ):
        return sorted(
            (
                authorization
                for authorization in target.api_keys.gateway_authorizations.values()
                if authorization.settled
                and _parse_iso(authorization.created_at) >= since
            ),
            key=lambda authorization: (authorization.created_at, authorization.id),
        )
    if hasattr(target, "_database") and hasattr(target, "_param_types"):
        with target._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT payload FROM tr_gateway_authorization "
                    "WHERE settled=true AND created_at>=@since "
                    "ORDER BY created_at, authorization_id",
                    params={"since": since},
                    param_types={"since": target._param_types.TIMESTAMP},
                )
            )
        return [
            _dataclass_from_json(row[0], GatewayAuthorization)
            for row in rows
            if row[0]
        ]
    if hasattr(target, "_run_transaction"):
        def read(conn: Any) -> list[GatewayAuthorization]:
            rows = conn.execute(
                "SELECT body FROM tr_entities "
                "WHERE kind = %s AND updated_at >= %s ORDER BY updated_at, id",
                ("gateway_authorization", since),
            ).fetchall()
            return [
                authorization
                for row in rows
                if (
                    (authorization := _dataclass_from_json(
                        row[0], GatewayAuthorization
                    )).settled
                    and _parse_iso(authorization.created_at) >= since
                )
            ]

        return cast(list[GatewayAuthorization], target._run_transaction(read))
    raise TypeError(f"unsupported reconciliation store: {type(target).__name__}")


def _scan_payout_movements(store: Any, since: datetime) -> list[CreditMovement]:
    target = _store_target(store)
    if hasattr(target, "credit_movements"):
        return sorted(
            (
                movement
                for movement in target.credit_movements.values()
                if movement.kind == _PAYOUT_KIND
                and _parse_iso(movement.created_at) >= since
            ),
            key=lambda movement: (movement.created_at, movement.movement_id),
        )
    if hasattr(target, "_database") and hasattr(target, "_param_types"):
        with target._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT account_id, movement_id, kind, amount_microdollars, "
                    "counterparty_account_id, custom_model_id, authorization_id, created_at "
                    "FROM tr_credit_movement "
                    "WHERE kind='custom_model_payout' AND created_at>=@since "
                    "ORDER BY created_at, account_id, movement_id",
                    params={"since": since},
                    param_types={"since": target._param_types.TIMESTAMP},
                )
            )
        return [_movement_from_row(row) for row in rows]
    if hasattr(target, "_run_transaction"):
        def read(conn: Any) -> list[CreditMovement]:
            rows = conn.execute(
                "SELECT account_id, movement_id, kind, amount_microdollars, "
                "counterparty_account_id, custom_model_id, authorization_id, created_at "
                "FROM tr_credit_movement "
                "WHERE kind = %s AND created_at >= %s "
                "ORDER BY created_at, account_id, movement_id",
                (_PAYOUT_KIND, since),
            ).fetchall()
            return [_movement_from_row(row) for row in rows]

        return cast(list[CreditMovement], target._run_transaction(read))
    raise TypeError(f"unsupported reconciliation store: {type(target).__name__}")


def _movement_from_row(row: Any) -> CreditMovement:
    return CreditMovement(
        account_id=str(row[0]),
        movement_id=str(row[1]),
        kind=str(row[2]),
        amount_microdollars=int(row[3]),
        counterparty_account_id=None if row[4] is None else str(row[4]),
        custom_model_id=None if row[5] is None else str(row[5]),
        authorization_id=None if row[6] is None else str(row[6]),
        created_at=_timestamp_string(row[7]),
    )


def _payout_authorization_id(movement: CreditMovement) -> str | None:
    if movement.authorization_id:
        return movement.authorization_id
    if movement.movement_id.startswith(_PAYOUT_PREFIX):
        suffix = movement.movement_id.removeprefix(_PAYOUT_PREFIX)
        return suffix or None
    return None


def _dataclass_from_json(raw: Any, cls: type[T]) -> T:
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    known = {field.name for field in dataclasses.fields(cast(Any, cls))}
    return cls(**{key: value for key, value in data.items() if key in known})


def _store_target(store: Any) -> Any:
    return getattr(store, "target", store)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _timestamp_string(value: Any) -> str:
    if isinstance(value, str):
        return _parse_iso(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        return _as_utc(value).isoformat().replace("+00:00", "Z")
    raise TypeError("movement timestamp is not a datetime")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _since_argument(raw: str) -> datetime:
    try:
        return _parse_iso(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("since must be an ISO timestamp") from exc


def _emit_json(record: dict[str, Any]) -> None:
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=_since_argument,
        default=datetime.now(UTC) - timedelta(hours=DEFAULT_SINCE_HOURS),
        help="ISO timestamp; defaults to 48 hours ago",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = create_store(Settings())
    summary = reconcile_custom_model_payouts(
        store,
        since=args.since,
        apply=args.apply,
    )
    unresolved_missing = summary.missing_payouts - summary.payouts_applied
    has_unresolved_findings = any(
        (
            unresolved_missing,
            summary.owner_unknown,
            summary.generation_missing,
            summary.attribution_mismatches,
            summary.orphan_payouts,
        )
    )
    return 1 if has_unresolved_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
