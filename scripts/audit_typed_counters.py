"""Daily read-only typed-billing invariant audit.

Runs the typed-side reserved invariant auditor against the production
Spanner/Bigtable store. Exit codes are intentionally distinct for scheduled
workflow alerting:

  0: invariant report clean
  1: invariant violation
  2: infrastructure/configuration failure
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants

_DEFAULT_ENV = {
    "TR_STORAGE_BACKEND": "spanner-bigtable",
    "TR_GCP_PROJECT_ID": "quill-cloud-proxy",
    "TR_SPANNER_INSTANCE_ID": "trusted-router-nam6",
    "TR_SPANNER_DATABASE_ID": "trusted-router",
    "TR_BIGTABLE_INSTANCE_ID": "trusted-router-logs",
    "TR_BIGTABLE_GENERATION_TABLE": "trustedrouter-generations",
}
_MAX_SAMPLES = 100_000


class AuditReport(Protocol):
    samples: Mapping[str, object]

    @property
    def clean(self) -> bool: ...

    def summary(self) -> str: ...


class AuditFunc(Protocol):
    def __call__(self, store: Any, *, max_samples: int = ...) -> AuditReport: ...


def _bootstrap_prod_env() -> None:
    for key, value in _DEFAULT_ENV.items():
        os.environ.setdefault(key, value)


def _print_report(name: str, sample_label: str, report: AuditReport) -> None:
    print(f"{name}: {report.summary()}")
    for scope, detail in report.samples.items():
        print(f"  {sample_label} {scope}: {detail}")


def run_audit(
    store: Any,
    *,
    invariant_audit: AuditFunc = audit_typed_invariants,
) -> int:
    invariant_report = invariant_audit(store, max_samples=_MAX_SAMPLES)
    _print_report("audit_typed_invariants", "VIOLATION", invariant_report)

    return 0 if invariant_report.clean else 1


def main(
    *,
    store: Any | None = None,
    settings_factory: Callable[[], Any] = Settings,
    store_factory: Callable[[Any], Any] = create_store,
    invariant_audit: AuditFunc = audit_typed_invariants,
) -> int:
    _bootstrap_prod_env()
    try:
        settings = settings_factory()
        backend = str(getattr(settings, "storage_backend", "")).lower()
        if backend != "spanner-bigtable":
            print(
                "ERROR: refusing to audit because TR_STORAGE_BACKEND is not "
                f"spanner-bigtable (resolved {backend or '<empty>'})",
                file=sys.stderr,
            )
            return 2
        if store is None:
            store = store_factory(settings)
        return run_audit(store, invariant_audit=invariant_audit)
    except Exception as exc:
        print(f"ERROR: infrastructure failure during typed-counter audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
