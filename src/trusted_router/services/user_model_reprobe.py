"""Bounded periodic probes for online user-provided models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from trusted_router.config import Settings
from trusted_router.services.user_model_probe import probe_user_model
from trusted_router.storage import STORE


@dataclass(frozen=True)
class ReprobeRecord:
    model_id: str
    kind: str
    status: str
    detail: str


@dataclass(frozen=True)
class ReprobeReport:
    scanned: int
    attempted: int
    passed: int
    failed: int
    dry_run: bool
    records: list[ReprobeRecord]


async def reprobe_user_models(
    settings: Settings,
    *,
    store: Any = STORE,
    limit: int = 100,
    kind: Literal["machine", "agent", "human"] | None = None,
    apply: bool = False,
) -> ReprobeReport:
    """Probe bounded online models, or list them without network I/O."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    models = [
        model
        for model in store.list_public_user_models(kind=kind)
        if model.online
    ][:limit]
    records: list[ReprobeRecord] = []
    passed = 0
    failed = 0
    for model in models:
        if not apply:
            records.append(
                ReprobeRecord(
                    model_id=model.id,
                    kind=model.kind,
                    status="would_probe",
                    detail="Dry run; probe not sent",
                )
            )
            continue
        result = await probe_user_model(model, settings, store=store)
        if result.ok:
            passed += 1
        else:
            failed += 1
        records.append(
            ReprobeRecord(
                model_id=model.id,
                kind=model.kind,
                status="ok" if result.ok else "failed",
                detail=result.detail,
            )
        )
    return ReprobeReport(
        scanned=len(models),
        attempted=len(models) if apply else 0,
        passed=passed,
        failed=failed,
        dry_run=not apply,
        records=records,
    )
