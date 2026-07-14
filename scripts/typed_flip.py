"""Two-phase typed-billing flip tool.

This is a per-workspace operator wrapper around the existing typed-counter
runbook primitives. It never edits rollout config and every mutating step is
behind --apply.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_counter_reconcile import (
    audit_typed_invariants,
    reconcile_for_flip,
)
from trusted_router.storage_models import ApiKey, CreditAccount, Reservation, Workspace

_DEFAULT_ENV = {
    "TR_STORAGE_BACKEND": "spanner-bigtable",
    "TR_GCP_PROJECT_ID": "quill-cloud-proxy",
    "TR_SPANNER_INSTANCE_ID": "trusted-router-nam6",
    "TR_SPANNER_DATABASE_ID": "trusted-router",
    "TR_BIGTABLE_INSTANCE_ID": "trusted-router-logs",
    "TR_BIGTABLE_GENERATION_TABLE": "trustedrouter-generations",
}


@dataclass(frozen=True)
class TypedCreditRow:
    total_credits: int
    total_usage: int
    reserved: int


@dataclass
class Readiness:
    workspace_id: str
    workspace: Workspace | None
    credit: CreditAccount | None
    keys: list[ApiKey] = field(default_factory=list)
    legacy_open_reservations: int = 0
    typed_reservation_rows: int = 0
    typed_open_reservations: int = 0
    typed_credit: TypedCreditRow | None = None
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def billing_paused(self) -> bool | None:
        return None if self.workspace is None else bool(self.workspace.billing_paused)

    @property
    def pause_required(self) -> bool:
        return self.workspace is not None and not self.workspace.billing_paused

    @property
    def key_reserved_total(self) -> int:
        return sum(int(key.reserved_microdollars) for key in self.keys)

    @property
    def key_reserved_count(self) -> int:
        return sum(1 for key in self.keys if int(key.reserved_microdollars) != 0)

    @property
    def drain_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.credit is not None and int(self.credit.reserved_microdollars) != 0:
            reasons.append(f"JSON credit reserved={self.credit.reserved_microdollars}")
        if self.legacy_open_reservations != 0:
            reasons.append(f"{self.legacy_open_reservations} open legacy reservations")
        if self.key_reserved_total != 0:
            reasons.append(
                f"{self.key_reserved_count} JSON key holds reserved={self.key_reserved_total}"
            )
        return reasons

    @property
    def reasons(self) -> list[str]:
        reasons = list(self.blocking_reasons)
        reasons.extend(self.drain_reasons)
        if self.pause_required:
            reasons.append("workspace not billing-paused")
        return reasons

    @property
    def verdict(self) -> str:
        if self.typed_open_reservations != 0:
            return "NOT_READY"
        if self.typed_reservation_rows != 0:
            return "ALREADY_TYPED"
        if self.blocking_reasons or self.drain_reasons or self.pause_required:
            return "NOT_READY"
        return "READY"

    @property
    def ready_except_pause(self) -> bool:
        return (
            self.workspace is not None
            and self.credit is not None
            and not self.blocking_reasons
        )


def _create_default_store() -> Any:
    for key, value in _DEFAULT_ENV.items():
        os.environ.setdefault(key, value)
    return create_store(Settings())


def _backend_allows_apply() -> bool:
    return os.environ.get("TR_STORAGE_BACKEND", "spanner-bigtable").lower() == "spanner-bigtable"


def _require_apply_backend(apply: bool) -> bool:
    if not apply or _backend_allows_apply():
        return True
    print(
        "ERROR: refusing to mutate because TR_STORAGE_BACKEND is not spanner-bigtable",
        file=sys.stderr,
    )
    return False


def _typed_credit_row(store: Any, workspace_id: str) -> TypedCreditRow | None:
    pt = store._param_types
    with store._database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT total_credits, total_usage, reserved FROM tr_credit_balance "
                "WHERE workspace_id=@pk AND shard=0",
                params={"pk": workspace_id},
                param_types={"pk": pt.STRING},
            )
        )
    if not rows:
        return None
    total_credits, total_usage, reserved = rows[0]
    return TypedCreditRow(
        total_credits=int(total_credits),
        total_usage=int(total_usage),
        reserved=int(reserved),
    )


def _typed_reservation_counts(store: Any, workspace_id: str) -> tuple[int, int]:
    pt = store._param_types
    with store._database.snapshot(multi_use=True) as snapshot:
        total = list(
            snapshot.execute_sql(
                "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws",
                params={"ws": workspace_id},
                param_types={"ws": pt.STRING},
            )
        )[0][0]
        open_rows = list(
            snapshot.execute_sql(
                "SELECT COUNT(*) FROM tr_reservation "
                "WHERE workspace_id=@ws AND settled = false",
                params={"ws": workspace_id},
                param_types={"ws": pt.STRING},
            )
        )[0][0]
    return int(total), int(open_rows)


def _legacy_open_reservation_count(store: Any, workspace_id: str) -> int:
    reservations = store._list_entities("reservation", cls=Reservation)
    return sum(
        1
        for reservation in reservations
        if reservation.workspace_id == workspace_id and not reservation.settled
    )


def assess_readiness(store: Any, workspace_id: str) -> Readiness:
    workspace = store.get_workspace(workspace_id)
    credit = store.get_credit_account(workspace_id)
    keys = store.list_keys(workspace_id) if workspace is not None else []
    legacy_open = _legacy_open_reservation_count(store, workspace_id)
    typed_rows, typed_open = _typed_reservation_counts(store, workspace_id)
    typed_credit = _typed_credit_row(store, workspace_id)

    readiness = Readiness(
        workspace_id=workspace_id,
        workspace=workspace,
        credit=credit,
        keys=keys,
        legacy_open_reservations=legacy_open,
        typed_reservation_rows=typed_rows,
        typed_open_reservations=typed_open,
        typed_credit=typed_credit,
    )
    if workspace is None:
        readiness.blocking_reasons.append("workspace not found")
    if credit is None:
        readiness.blocking_reasons.append("no credit account")
    if typed_rows != 0:
        readiness.blocking_reasons.append(f"ALREADY_TYPED: {typed_rows} tr_reservation rows")
    if typed_open != 0:
        readiness.blocking_reasons.append(f"{typed_open} open tr_reservation rows")
    return readiness


def _format_verdict(readiness: Readiness) -> str:
    if readiness.verdict != "NOT_READY":
        return readiness.verdict
    reasons = "; ".join(readiness.reasons) if readiness.reasons else "unknown"
    return f"NOT_READY({reasons})"


def _print_readiness(readiness: Readiness) -> None:
    print(f"{readiness.workspace_id}: {_format_verdict(readiness)}")
    print(f"  billing_paused={readiness.billing_paused}")
    if readiness.credit is None:
        print("  JSON credit: missing")
    else:
        print(
            "  JSON credit: "
            f"total_credits={readiness.credit.total_credits_microdollars} "
            f"total_usage={readiness.credit.total_usage_microdollars} "
            f"reserved={readiness.credit.reserved_microdollars}"
        )
    print(
        "  holds: "
        f"legacy_open_reservations={readiness.legacy_open_reservations} "
        f"key_reserved_count={readiness.key_reserved_count} "
        f"key_reserved_total={readiness.key_reserved_total}"
    )
    print(
        "  tr_reservation: "
        f"rows={readiness.typed_reservation_rows} open={readiness.typed_open_reservations}"
    )
    if readiness.typed_credit is None:
        print("  typed credit: missing")
    else:
        print(
            "  typed credit: "
            f"total_credits={readiness.typed_credit.total_credits} "
            f"total_usage={readiness.typed_credit.total_usage} "
            f"reserved={readiness.typed_credit.reserved}"
        )

def _workspace_ids(store: Any, args: argparse.Namespace) -> list[str]:
    if args.workspace:
        return [args.workspace]
    workspaces = store._list_entities("workspace", cls=Workspace)
    return [workspace.id for workspace in workspaces if not workspace.deleted]


def run_readiness(store: Any, args: argparse.Namespace) -> int:
    if not args.workspace and not args.all:
        args.all = True
    counts = {"READY": 0, "NOT_READY": 0, "ALREADY_TYPED": 0}
    workspace_ids = _workspace_ids(store, args)
    for workspace_id in workspace_ids:
        readiness = assess_readiness(store, workspace_id)
        counts[readiness.verdict] += 1
        _print_readiness(readiness)
    print(
        "SUMMARY "
        f"total={len(workspace_ids)} READY={counts['READY']} "
        f"NOT_READY={counts['NOT_READY']} ALREADY_TYPED={counts['ALREADY_TYPED']}"
    )
    return 0


def _pause_workspace(store: Any, workspace_id: str, *, reason: str) -> bool:
    updated = store.update_workspace(
        workspace_id,
        billing_paused=True,
        billing_pause_reason=reason,
    )
    if updated is None:
        print(f"ERROR: workspace {workspace_id} not found", file=sys.stderr)
        return False
    reloaded = store.get_workspace(workspace_id)
    if reloaded is None or not reloaded.billing_paused:
        print(f"ERROR: failed to verify workspace {workspace_id} is paused", file=sys.stderr)
        return False
    print(f"{workspace_id}: billing_paused=True")
    return True


def _unpause_workspace(store: Any, workspace_id: str) -> bool:
    updated = store.update_workspace(
        workspace_id,
        billing_paused=False,
        billing_pause_reason="",
    )
    if updated is None:
        print(f"ERROR: workspace {workspace_id} not found", file=sys.stderr)
        return False
    reloaded = store.get_workspace(workspace_id)
    if reloaded is None or reloaded.billing_paused:
        print(f"ERROR: failed to verify workspace {workspace_id} is unpaused", file=sys.stderr)
        return False
    print(f"{workspace_id}: billing_paused=False")
    return True


def _verify_seeded_credit(store: Any, workspace_id: str) -> tuple[bool, str]:
    credit = store.get_credit_account(workspace_id)
    typed = _typed_credit_row(store, workspace_id)
    if credit is None:
        return False, "JSON credit account missing"
    if typed is None:
        return False, "typed tr_credit_balance row missing"
    expected = (
        int(credit.total_credits_microdollars),
        int(credit.total_usage_microdollars),
        0,
    )
    actual = (typed.total_credits, typed.total_usage, typed.reserved)
    if actual != expected:
        return False, f"typed credit mismatch expected={expected} actual={actual}"
    return True, f"typed credit verified total_credits={actual[0]} total_usage={actual[1]} reserved=0"


def _run_audit(store: Any) -> bool:
    report = audit_typed_invariants(store)
    print(f"audit_typed_invariants (global): {report.summary()}")
    for scope, detail in report.samples.items():
        print(f"  VIOLATION {scope}: {detail}")
    return report.clean


def _print_prepare_next_steps(workspace_id: str) -> None:
    print("NEXT STEPS:")
    print(
        "deploy the C1 typed-capability route after verifying the prepared "
        f"workspace {workspace_id} is seeded,"
    )
    print(
        "verify the SERVING revision, then run "
        f"`typed_flip.py finish --workspace {workspace_id} --allowlist-deployed`."
    )
    print("Workspace REMAINS PAUSED.")


def run_prepare(store: Any, args: argparse.Namespace) -> int:
    readiness = assess_readiness(store, args.workspace)
    _print_readiness(readiness)
    if not args.apply:
        if not readiness.ready_except_pause:
            print(
                "DRY-RUN: would refuse before pausing because HARD blockers are present: "
                + "; ".join(readiness.blocking_reasons)
            )
            return 0
        print("DRY-RUN: would set billing_paused=True")
        print("DRY-RUN: would re-check JSON reserved/open holds after pause")
        if readiness.drain_reasons:
            print("DRY-RUN: would park paused until in-flight requests settle")
            for reason in readiness.drain_reasons:
                print(f"  drain-blocked: {reason}")
            return 0
        print("DRY-RUN: would run reconcile_for_flip(..., apply=True)")
        print("DRY-RUN: would verify typed credit row and audit_typed_invariants")
        print("DRY-RUN: workspace would remain PAUSED")
        return 0
    if not _require_apply_backend(args.apply):
        return 1
    if not readiness.ready_except_pause:
        print(
            "ERROR: prepare requires READY except for billing pause; "
            f"current verdict is {_format_verdict(readiness)}",
            file=sys.stderr,
        )
        return 1
    if not readiness.billing_paused:
        if not _pause_workspace(store, args.workspace, reason="typed-billing flip prepare"):
            return 1
    else:
        print(f"{args.workspace}: already billing_paused=True")

    after_pause = assess_readiness(store, args.workspace)
    if after_pause.drain_reasons:
        print("re-run prepare after in-flight requests settle")
        print("Workspace is paused and will remain paused; this is the desired parked state.")
        for reason in after_pause.drain_reasons:
            print(f"  drain-blocked: {reason}")
        return 2

    flip = reconcile_for_flip(store, args.workspace, apply=True)
    if not flip.ready:
        print(f"ERROR: reconcile_for_flip refused: {flip.reasons}", file=sys.stderr)
        return 1
    print(f"reconcile_for_flip: applied={flip.applied} credit={flip.credit_seeded} keys={flip.keys_seeded}")

    ok, message = _verify_seeded_credit(store, args.workspace)
    print(message)
    if not ok:
        return 1
    if not _run_audit(store):
        return 1
    _print_prepare_next_steps(args.workspace)
    return 0


def _print_watch_items() -> None:
    print("POST-FLIP WATCH ITEMS:")
    print("  audit_typed_invariants daily")
    print("  the 'release row-count != 1' log alert")


def run_finish(store: Any, args: argparse.Namespace) -> int:
    if not args.allowlist_deployed:
        print(
            "ERROR: refusing finish without --allowlist-deployed. This is the "
            "operator attestation that the allowlist deploy reached the serving "
            "revision; the script cannot read the serving revision's env itself.",
            file=sys.stderr,
        )
        return 1
    if not _require_apply_backend(args.apply):
        return 1
    ok, message = _verify_seeded_credit(store, args.workspace)
    print(message)
    if not ok:
        return 1
    if not _run_audit(store):
        return 1
    workspace = store.get_workspace(args.workspace)
    if workspace is None:
        print(f"ERROR: workspace {args.workspace} not found", file=sys.stderr)
        return 1
    if not workspace.billing_paused:
        print("ERROR: workspace is not billing-paused; finish requires the parked state", file=sys.stderr)
        return 1
    if not args.apply:
        print("DRY-RUN: would unpause workspace")
        return 0
    if not _unpause_workspace(store, args.workspace):
        return 1
    _print_watch_items()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    readiness = sub.add_parser("readiness", help="read-only per-workspace flip readiness")
    target = readiness.add_mutually_exclusive_group()
    target.add_argument("--workspace")
    target.add_argument("--all", action="store_true")
    readiness.set_defaults(func=run_readiness)

    prepare = sub.add_parser("prepare", help="pause, drain-check, seed typed counters")
    prepare.add_argument("--workspace", required=True)
    prepare.add_argument("--apply", action="store_true")
    prepare.set_defaults(func=run_prepare)

    finish = sub.add_parser(
        "finish",
        help=(
            "unpause after the operator verifies the serving revision allowlist env; "
            "the script cannot read that serving revision env itself"
        ),
    )
    finish.add_argument("--workspace", required=True)
    finish.add_argument(
        "--allowlist-deployed",
        action="store_true",
        help=(
            "operator attestation that the allowlist deploy reached the serving revision; "
            "the script cannot read the serving revision's env itself"
        ),
    )
    finish.add_argument("--apply", action="store_true")
    finish.set_defaults(func=run_finish)
    return parser


def _with_default_subcommand(argv: list[str]) -> list[str]:
    commands = {"readiness", "prepare", "finish"}
    if argv and (argv[0] in commands or argv[0] in {"-h", "--help"}):
        return argv
    return ["readiness", *argv]


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = _build_parser()
    normalized = _with_default_subcommand(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(normalized)
    active_store = _create_default_store() if store is None else store
    return args.func(active_store, args)


if __name__ == "__main__":
    sys.exit(main())
