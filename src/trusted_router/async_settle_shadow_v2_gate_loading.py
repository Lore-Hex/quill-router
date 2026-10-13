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


def retained_json(raw: bytes, *, limit: int = 262144, allow_float: bool = True, max_depth: int = 64, bounded_integers: bool = True) -> Any:
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
            elif not allow_float and bounded_integers and type(child) is int:
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


def transport_children(raw: bytes, limit: int) -> tuple[dict[str, Any], list[LoadingIssue]]:
    """Decode siblings before validation; damage has only its own object scope.

    Duplicate keys are ambiguous at that key, not at independently decoded
    siblings. No repaired child is evidence: every rejection is a permanent G1
    issue, and original JSON strings are never normalized before validation.
    """
    damaged = object()
    duplicates = []
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                duplicates.append(key)
                # Equal original strings still supply their exact commitments.
                if type(value) is not str or value != result[key]:
                    result[key] = damaged
            else:
                result[key] = value
        return result

    require(type(raw) is bytes and len(raw) <= limit, 'artifact_gap')
    text = raw.decode('utf-8', errors='surrogateescape')
    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except RecursionError:
        # Python's recursive JSON decoder must not discard independent proof
        # strings merely because a sibling exceeds its recursion limit. Scan
        # only the outer member boundaries, iteratively and string-aware.
        decoder = json.JSONDecoder(object_pairs_hook=pairs)
        at = 0
        members = []
        try:
            require(text.lstrip().startswith('{'))
            at = text.index('{') + 1
            while True:
                while at < len(text) and text[at].isspace():
                    at += 1
                if text[at] == '}':
                    break
                key, at = decoder.raw_decode(text, at)
                require(type(key) is str)
                while text[at].isspace():
                    at += 1
                require(text[at] == ':')
                at += 1
                start = at
                stack: list[str] = []
                while at < len(text):
                    character = text[at]
                    if character == '"':
                        _, at = decoder.raw_decode(text, at)
                        continue
                    if not stack and character in ',}':
                        break
                    if character in '[{':
                        stack.append(character)
                    elif character in ']}':
                        require(bool(stack) and stack.pop() == ('[' if character == ']' else '{'))
                    at += 1
                require(at < len(text) and not stack)
                try:
                    child = json.loads(text[start:at], object_pairs_hook=pairs)
                except (ValueError, RecursionError):
                    child = damaged
                members.append((key, child))
                if text[at] == '}':
                    break
                at += 1
            require(not text[at + 1:].strip())
            value = pairs(members)
        except (ValueError, IndexError) as exc:
            raise ValueError('evidence_schema_gap') from exc
    except (ValueError, UnicodeError) as exc:
        raise ValueError('evidence_schema_gap') from exc
    require(type(value) is dict, 'evidence_schema_gap')
    issues: list[LoadingIssue] = [LoadingIssue('evidence_schema_gap', 'transport/duplicate-key',
        'duplicate transport member') for _ in duplicates[:1]]
    cleaned: dict[str, Any] = {}

    def reject(path: str) -> None:
        # Invalid identifiers cannot become a contract object subject.
        try:
            a2.FILE_REF.fields['path'](path)
        except (ValueError, UnicodeError):
            path = 'transport/invalid-child'
        issues.append(LoadingIssue('evidence_schema_gap', path, 'invalid transport child'))

    def valid(child: Any, path: str) -> bool:
        try:
            pending = [(child, 0)]
            while pending:
                item, depth = pending.pop()
                require(depth <= 16)
                if type(item) is dict:
                    for key, value in item.items():
                        key.encode('utf-8')
                        pending.append((value, depth + 1))
                elif type(item) is list:
                    pending.extend((value, depth + 1) for value in item)
                elif type(item) is str:
                    item.encode('utf-8')
                elif type(item) is int:
                    require(-(1 << 63) <= item < (1 << 63))
                else:
                    require(item is None or type(item) is bool)
            return True
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            reject(path)
            return False

    arrays = {'rows': {'kind', 'id', 'raw'}, 'attachments': {'kind', 'id', 'raw'},
              'preimages': {'name', 'raw'}, 'ledgers': None}
    allowed = {'v', 'policy_version', 'contract_revision', 'proof', 'transport', 'fleet',
               'pre', 'serving', 'completed_at_us', 'days', 'window', *arrays}
    for key, child in value.items():
        if key not in allowed:
            reject('transport/extra')
        elif key in arrays:
            cleaned[key] = []
            if type(child) is not list or len(child) > (4096 if key == 'ledgers' else 10000):
                reject('transport/' + key)
                continue
            for index, item in enumerate(child):
                path = 'transport/' + key + '/' + str(index)
                fields = arrays[key]
                if type(item) is dict and type(item.get('name')) is str:
                    path = item['name']
                elif type(item) is dict and all(type(item.get(k)) is str for k in ('kind', 'id')):
                    path = 'rows/' + item['kind'] + '/' + item['id']
                shape = type(item) is str if fields is None else (
                    type(item) is dict and set(item) == fields and all(type(v) is str for v in item.values()))
                if not shape:
                    reject(path)
                elif valid(item, path):
                    if key in ('rows', 'attachments'):
                        try:
                            a2.O(item['kind'], item['id'])
                            from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
                            a2.FILE_REF.fields['path'](path + '/' + file_digest(item['raw'].encode()))
                        except ValueError:
                            reject(path)
                            continue
                    if key == 'preimages':
                        try:
                            a2.FILE_REF.fields['path'](item['name'])
                        except ValueError:
                            reject(path)
                            continue
                    cleaned[key].append(item)
        elif valid(child, 'transport/' + key):
            cleaned[key] = child
    return cleaned, issues
