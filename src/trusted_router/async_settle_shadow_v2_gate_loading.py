"""P5 loading diagnostics and the limits shape specified in design §7.5.4.

Invariant: invalid values never erase independently framed siblings at any
level. Scalar decoding is local, containers use bounded iterative frames, and
original evidence strings retain their exact decoded bytes. Rejections become
permanent scoped G1 issues; proof and scope survive unrelated damage.

The frozen Bundle has no diagnostic field. ``loading_issues`` in a2_streams is
an internal handoff (not evidence and never hashed as an A2 attachment). Keep
its producer/consumer here until that interface can acquire a typed field.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum, auto
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


def error_detail(exc: Exception) -> str:
    """Only frozen diagnostic labels cross the output boundary, never Python text."""
    labels = {rule.name for rule in a2.FAILURE_RULES} | {'a1-publisher-conservation'}
    text = str(exc)
    return text if text in labels else 'evidence_schema_gap'


def retained_json(raw: bytes, *, limit: int = 262144, allow_float: bool = True, max_depth: int = 64, bounded_integers: bool = True) -> Any:
    """Transport decode only; the retained callable owns the payload schema.

    Do not run v1 binary64 measurements through the integer-only A2 decoder.
    Duplicate fields and nonfinite numbers must not disappear before delegation.
    """
    try:
        value, issues = decode_original(raw, limit, allow_float=allow_float,
            max_depth=max_depth + 1, bounded_integers=bounded_integers)
        require(not issues)
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


def validate_original_bytes(bundle: Bundle, ports: Ports | None, *, gate: str = 'pre-flip') -> list[dict[str, Any]]:
    """Exercise the existing row/preimage boundary during loading as well.

    Evaluators still recheck this boundary; loading never treats unchecked rows
    or manifest commitments as evidence merely because transport JSON decoded.
    Only diagnostics are carried out, not the mutable evaluation context.
    """
    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle

    ctx = decode_bundle(bundle, ports, gate)
    return [dict(gap=issue.gap, detail=issue.detail,
                 subject=issue.subject.wire or a2.N())
            for issue in ctx.issues if not issue.detail.startswith('undefined preimage:')]


# Bounds apply to transport structure, never to the contents of original strings.
MAX_TRANSPORT_DEPTH = 16
_INVALID = object()


class DecoderStatus(Enum):
    """Internal decoder outcomes, separate from JSON member paths."""

    DISAGREEMENT = auto()


DecodeIssue = tuple[Any, ...] | DecoderStatus


def same_decoded(left: Any, right: Any) -> bool:
    """Compare types and insertion order as well as scalar values, iteratively."""
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if type(left) is not type(right):
            return False
        if type(left) is dict:
            if list(left) != list(right):
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif type(left) is list:
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right, strict=True))
        elif left != right:
            return False
    return True


def decode_original(raw: bytes, limit: int, *, allow_float: bool = False,
                    max_depth: int = MAX_TRANSPORT_DEPTH, bounded_integers: bool = True
                    ) -> tuple[Any, list[DecodeIssue]]:
    """The only original-byte decoder: clean requires both decoders to agree.

    Standard rejection never discards locally recovered siblings. A local clean
    result after standard rejection (or a different value) gets a fixed root
    issue, without carrying exception text into evidence.
    """
    require(type(raw) is bytes and len(raw) <= limit, 'artifact_gap')
    def invalid(token: str) -> Any:
        raise ValueError('evidence_schema_gap')
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in items:
            require(key not in value)
            value[key] = child
        return value
    def finite(token: str) -> float:
        number = float(token)
        require(math.isfinite(number))
        return number
    standard = _INVALID
    try:
        standard = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                              parse_float=finite, parse_constant=invalid, strict=True)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        pass
    value, paths = _local_values(raw, allow_float=allow_float,
        max_depth=max_depth, bounded_integers=bounded_integers)
    issues: list[DecodeIssue] = list(paths)
    if not issues and (standard is _INVALID or not same_decoded(value, standard)):
        issues.append(DecoderStatus.DISAGREEMENT)
    return value, issues


def transport_values(raw: bytes, limit: int) -> tuple[dict[str, Any], list[DecodeIssue]]:
    value, issues = decode_original(raw, limit)
    require(type(value) is dict)
    return value, issues


def _local_values(raw: bytes, *, allow_float: bool, max_depth: int,
                  bounded_integers: bool) -> tuple[Any, list[tuple[Any, ...]]]:
    """Iterative JSON frames isolate every value, including malformed scalars.

    Only delimiters/quotes frame children; decoding a token cannot consume its
    sibling. Balanced containers beyond the bound are skipped with a counter,
    not a Python call stack. Original strings are decoded exactly once.
    """
    import re
    text = raw.decode('utf-8', errors='surrogateescape')
    errors: list[tuple[Any, ...]] = []
    # frame: container, path, next key, grammar state
    frames: list[list[Any]] = []
    root: Any = _INVALID
    skip = 0

    def scalar(token: str) -> Any:
        def integer(value: str) -> int:
            if bounded_integers and not allow_float:
                require(len(value.lstrip('-')) <= 19)
            number = int(value)
            if bounded_integers and not allow_float:
                require(-(1 << 63) <= number < (1 << 63))
            return number
        def invalid(value: str) -> Any:
            raise ValueError('evidence_schema_gap')
        def finite(token: str) -> float:
            require(allow_float)
            number = float(token)
            require(math.isfinite(number))
            return number
        try:
            value = json.loads(token, parse_int=integer, parse_float=finite, parse_constant=invalid)
            if isinstance(value, str):
                value.encode('utf-8')
            return value
        except (ValueError, UnicodeError):
            return _INVALID

    string_decoder = json.JSONDecoder()

    def tokens() -> Any:
        # CPython's scalar string scanner is iterative and fast on large
        # original-byte strings. A failed scalar uses only its lexical boundary;
        # it never invokes a whole-document decoder or consumes a sibling.
        at = 0
        literal = re.compile(r'[^\s{}\[\],:"]+')
        quoted = re.compile(r'"(?:[^"\\]|\\[\s\S])*(?:"|$)')
        while at < len(text):
            character = text[at]
            if character.isspace():
                if character not in ' \t\r\n' and ('transport', 'whitespace') not in errors:
                    errors.append(('transport', 'whitespace'))
                at += 1
            elif character == '"':
                try:
                    value, end = string_decoder.raw_decode(text, at)
                    value.encode('utf-8')
                except (ValueError, UnicodeError):
                    match = quoted.match(text, at)
                    assert match is not None
                    value, end = _INVALID, match.end()
                yield '', value
                at = end
            elif character in '{}[],:':
                yield character, None
                at += 1
            else:
                match = literal.match(text, at)
                assert match is not None
                yield '', scalar(match.group())
                at = match.end()

    def path() -> tuple[Any, ...]:
        if not frames:
            return ('transport',)
        container, prefix, key, _ = frames[-1]
        return (*prefix, len(container) if isinstance(container, list) else key)

    def put(value: Any) -> None:
        nonlocal root
        location = path()
        if value is _INVALID:
            errors.append(location)
        if not frames:
            require(root is _INVALID)
            root = value
            return
        frame = frames[-1]
        container, _, key, state = frame
        require(state in ('value', 'first'))
        if isinstance(container, list):
            container.append(value)
        else:
            require(key is not None)
            if key in container:
                errors.append(location)
                # An equal original string retains its exact commitment, but
                # duplication always leaves a permanent scoped G1 rejection.
                if type(value) is not str or container[key] != value:
                    container[key] = _INVALID
            else:
                container[key] = value
        frame[3] = 'comma'

    for lexeme, decoded in tokens():
        if skip:
            skip += (lexeme in ('[', '{')) - (lexeme in (']', '}'))
            continue
        if frames:
            frame = frames[-1]
            container, _, _, state = frame
            if lexeme in (']', '}'):
                require(isinstance(container, list) == (lexeme == ']'))
                if state == 'value':
                    put(_INVALID)
                if state == 'key':
                    errors.append((*frame[1], 'trailing-comma'))
                require(frame[3] in ('first', 'comma', 'key'))
                frames.pop()
                continue
            if state in ('key', 'first') and isinstance(container, dict):
                key = decoded
                if type(key) is not str:
                    key = 'invalid-key'
                    errors.append((*frame[1], 'invalid-key'))
                frame[2:4] = [key, 'colon']
                continue
            if state == 'colon':
                require(lexeme == ':')
                frame[3] = 'value'
                continue
            if lexeme == ',':
                if state == 'value' or state == 'first' and isinstance(container, list):
                    put(_INVALID)
                require(frame[3] == 'comma')
                frame[3] = 'value' if isinstance(container, list) else 'key'
                continue
        if lexeme in ('{', '['):
            location = path()
            if len(frames) >= max_depth:
                put(_INVALID)
                skip = 1
            else:
                child: Any = {} if lexeme == '{' else []
                put(child)
                frames.append([child, location, None, 'first'])
        elif lexeme == '':
            put(decoded)
        else:
            put(_INVALID)
    require(not frames and not skip and root is not _INVALID, 'evidence_schema_gap')
    return root, errors


def transport_children(raw: bytes, limit: int) -> tuple[dict[str, Any], list[LoadingIssue]]:
    """Reject only damaged evidence elements, preserving independent siblings."""
    value, errors = transport_values(raw, limit)
    issues: list[LoadingIssue] = []
    cleaned: dict[str, Any] = {}

    def reject(path: str) -> None:
        try:
            a2.FILE_REF.fields['path'](path)
        except (ValueError, UnicodeError):
            path = 'transport/invalid-child'
        issue = LoadingIssue('evidence_schema_gap', path, 'invalid transport child')
        if issue not in issues:
            issues.append(issue)

    def valid(child: Any, path: str) -> bool:
        pending = [child]
        while pending:
            item = pending.pop()
            if item is _INVALID:
                reject(path)
                return False
            if type(item) is dict:
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
        return True

    arrays = {'rows': {'kind', 'id', 'raw'}, 'attachments': {'kind', 'id', 'raw'},
              'preimages': {'name', 'raw'}, 'ledgers': None, 'days': None}
    allowed = {'v', 'policy_version', 'contract_revision', 'proof', 'transport', 'fleet',
               'pre', 'serving', 'completed_at_us', 'window', 'producer_registrations', *arrays}
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
    for error in errors:
        if error is DecoderStatus.DISAGREEMENT:
            reject('transport/decoder-disagreement')
            continue
        # Member paths remain typed until rendering. A name containing '/'
        # stays a single segment, separate from an array index.
        assert isinstance(error, tuple)
        parts = error
        key = parts[1] if len(parts) > 1 else ''
        if key not in allowed:
            reject('transport/extra')
        elif key in arrays and len(parts) > 2 and type(parts[2]) is int and type(value.get(key)) is list:
            index = parts[2]
            item = value[key][index]
            path = 'transport/' + key + '/' + str(index)
            if type(item) is dict and type(item.get('name')) is str:
                path = item['name']
            elif type(item) is dict and all(type(item.get(k)) is str for k in ('kind', 'id')):
                path = 'rows/' + item['kind'] + '/' + item['id']
            reject(path)
        else:
            reject('transport/' + key)
    return cleaned, issues
