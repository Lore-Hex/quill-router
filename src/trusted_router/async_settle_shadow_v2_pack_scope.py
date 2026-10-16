"""Section 11 source-check scope, independent of supplied result check lists."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_pack_external import proof_result
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_policy import (  # noqa: F401
    CHECK_SCOPE,
    MONEY_SCOPE,
    STAGE_CHECKS,
    check_scope,
)
from trusted_router.async_settle_shadow_v2_schema import require


def scoped_result(files: PackFiles, *, name: str, stage: str, approved_checks: list[str],
                  **kwargs: Any) -> dict[str, Any]:
    expected = check_scope(name, stage)
    require(tuple(sorted(approved_checks)) == expected, 'artifact_gap')
    return proof_result(files, name=name, stage=stage, approved_checks=list(expected), **kwargs)


def scope_inventory() -> dict[str, dict[str, list[str]]]:
    return {stage: {name: list(checks) for name, checks in names.items()}
            for stage, names in STAGE_CHECKS.items() if names}
