"""P5 loading diagnostics and the limits shape specified in design §7.5.4.

The frozen Bundle has no diagnostic field. ``loading_issues`` in a2_streams is
an internal handoff (not evidence and never hashed as an A2 attachment). Keep
its producer/consumer here until that interface can acquire a typed field.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_schema import Schema, array, name, require

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Ports

LIMITS = Schema({
    'regions': array(Schema({'region': name, 'approved': a2.PEAKS}, 8192), 4096),
    'fleet': a2.PEAKS,
}, 262144, lambda value: require(
    len({row['region'] for row in value['regions']}) == len(value['regions'])))


@dataclass(frozen=True)
class LoadingIssue:
    gap: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return dict(gap=self.gap, path=self.path, detail=self.detail)


class PayloadError(ValueError):
    """A dependency failure keeps the exact input-inventory path as identity."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(reason)
        self.path = path


def error_gap(exc: Exception) -> str:
    """Missing/mismatched bytes are artifacts; undefined shapes are schemas."""
    return 'artifact_gap' if str(exc) in ('artifact_gap', 'evidence_size') else 'evidence_schema_gap'


def retained_json(raw: bytes, *, limit: int = 262144, allow_float: bool = True, max_depth: int = 64) -> Any:
    """Transport decode only; the retained callable owns the payload schema.

    Do not run v1 binary64 measurements through the integer-only A2 decoder.
    Duplicate fields and nonfinite numbers must not disappear before delegation.
    """
    require(type(raw) is bytes and len(raw) <= limit, 'artifact_gap')

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    def finite(token: str) -> float:
        require(allow_float)
        value = float(token)
        require(math.isfinite(value))
        return value

    def invalid(token: str) -> Any:
        raise ValueError('evidence_schema_gap')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_float=finite, parse_constant=invalid)
        pending = [(value, 0)]
        while pending:
            child, depth = pending.pop()
            require(depth <= max_depth)
            if isinstance(child, (dict, list)):
                pending.extend((v, depth + 1) for v in
                               (child.values() if isinstance(child, dict) else child))
            elif isinstance(child, str):
                child.encode('utf-8')
            elif not allow_float and type(child) is int:
                require(-(1 << 63) <= child < (1 << 63))
        return value
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError('evidence_schema_gap') from exc


def validate_original_bytes(bundle: Bundle, ports: Ports | None) -> list[dict[str, Any]]:
    """Exercise the existing row/preimage boundary during loading as well.

    Evaluators still recheck this boundary; loading never treats unchecked rows
    or manifest commitments as evidence merely because transport JSON decoded.
    Only diagnostics are carried out, not the mutable evaluation context.
    """
    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle

    ctx = decode_bundle(bundle, ports, 'fleet-budgets-pre')
    return [dict(gap=issue.gap, detail=issue.detail,
                 subject=issue.subject.wire or a2.N())
            for issue in ctx.issues if not issue.detail.startswith('undefined preimage:')]
