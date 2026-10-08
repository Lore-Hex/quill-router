"""Bounded diagnostic transport. Refusals contain only fixed reason codes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from trusted_router import billing_snapshot as billing
from trusted_router.detached_jws import b64decode, canonical

HEADER_BYTES = 12288
JSON_BYTES = 8192
INLINE_BYTES = 6144
LOCAL_BYTES = 65536
CANDIDATES = 128
ERRORS = frozenset({"usage_missing", "usage_estimated", "unsupported_observed", "malformed_usage", "arithmetic_overflow", "evaluator_failed"})
KEYS = frozenset({"v", "billing_shadow_binding", "billing_snapshot", "raw_usage", "observed", "terminal", "payload_hash", "go_error", "go_evaluator", "go_revision", "handoff_prepare_us"})
REVISION = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
SURROGATE = re.compile(r"[\ud800-\udfff]")


class Rejection(ValueError):
    pass


def _refuse(reason: str) -> Any:
    raise Rejection(reason)


def _integer(lexeme: str) -> int:
    digits = lexeme.removeprefix("-")
    if len(digits) > 19 or not digits.isascii() or not digits.isdigit():
        return _refuse("integer")
    value = int(lexeme)
    if not 0 <= value <= billing.MAX_INT:
        return _refuse("integer")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _refuse("json_duplicate")
        result[key] = value
    return result


def bounded_json(raw: bytes, limit: int = JSON_BYTES, *, signed: bool = False) -> Any:
    if len(raw) > limit:
        _refuse("header_size")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return _refuse("json_encoding")
    if text.startswith("\ufeff") or "\x00" in text:
        _refuse("json_encoding")
    depth = 0
    quoted = escaped = False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in "[{":
            depth += 1
            if depth > 16:
                _refuse("json_shape")
        elif ch in "]}":
            depth -= 1
    try:
        # Wire counts remain unsigned. Stored evidence additionally carries
        # signed deltas; schema validation still rejects negative counts/times.
        integer = (lambda token: -_integer(token[1:]) if token.startswith("-") else _integer(token)) if signed else _integer
        value = json.loads(text, object_pairs_hook=_pairs, parse_int=integer,
                           parse_float=lambda _: _refuse("integer"),
                           parse_constant=lambda _: _refuse("integer"))
    except (json.JSONDecodeError, RecursionError):
        return _refuse("json_shape")
    def strings(item: Any) -> None:
        if isinstance(item, str):
            if SURROGATE.search(item):
                _refuse("json_encoding")
        elif isinstance(item, dict):
            for key, child in item.items():
                strings(key)
                strings(child)
        elif isinstance(item, list):
            for child in item:
                strings(child)
    strings(value)
    return value


def snapshot_from_object(value: Any) -> billing.BillingSnapshot:
    if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
        return _refuse("json_shape")
    value = dict(value)
    candidates = []
    for candidate in value["candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("tiers"), list):
            return _refuse("json_shape")
        candidates.append({**candidate, "tiers": tuple(candidate["tiers"])})
    value["candidates"] = tuple(candidates)
    snapshot = billing.BillingSnapshot.model_validate(value)
    if any(len(c.endpoint_id) > 128 or len(c.model_id) > 128 for c in snapshot.candidates):
        _refuse("identity")
    return snapshot


@lru_cache(maxsize=128)
def _inline_snapshot(raw: bytes) -> billing.BillingSnapshot:
    # Only canonical bytes from bounded_json enter this cache. Every new value
    # still gets full DTO validation; no signature or per-request fact is cached.
    return snapshot_from_object(json.loads(raw))


@dataclass(frozen=True)
class Envelope:
    proof: str
    snapshot: billing.BillingSnapshot | None
    raw: billing.RawUsage | None
    observed: billing.Eligibility
    terminal: billing.TerminalEnvelope | None
    payload_hash: str | None
    go_error: str | None
    go_revision: str
    handoff_prepare_us: int
    # Retain a structurally valid, nonzero refund terminal for disagreement.
    invalid_refund_charge: int | None = None


def parse_header(headers: tuple[str, ...]) -> Envelope:
    if len(headers) != 1:
        return _refuse("header_duplicate" if headers else "missing_envelope")
    header = headers[0]
    if len(header) > HEADER_BYTES or not header.isascii():
        _refuse("header_size")
    try:
        raw = b64decode(header)
    except ValueError:
        return _refuse("base64")
    value = bounded_json(raw)
    if not isinstance(value, dict) or set(value) not in (KEYS, KEYS - {"billing_snapshot"}):
        _refuse("json_shape")
    if type(value["v"]) is not int or value["v"] != 1:
        _refuse("integer")
    proof = value["billing_shadow_binding"]
    if not isinstance(proof, str) or not proof.isascii() or len(proof) > 2048:
        _refuse("proof_signature")
    if (value["go_evaluator"] != "billing-v1" or not isinstance(value["go_revision"], str)
            or REVISION.fullmatch(value["go_revision"]) is None):
        _refuse("json_shape")
    if type(value["handoff_prepare_us"]) is not int:
        _refuse("integer")
    failure = value["go_error"]
    if failure is not None and (not isinstance(failure, str) or failure not in ERRORS):
        _refuse("go_failure")
    if failure is not None:
        if value["terminal"] is not None or value["payload_hash"] is not None:
            _refuse("json_shape")
    elif value["terminal"] is None or value["raw_usage"] is None:
        _refuse("json_shape")
    invalid_refund = None
    terminal: billing.TerminalEnvelope | None
    try:
        snapshot = None
        if "billing_snapshot" in value:
            snapshot_bytes = canonical(value["billing_snapshot"])
            if len(snapshot_bytes) > INLINE_BYTES:
                _refuse("header_size")
            snapshot = _inline_snapshot(snapshot_bytes)
        usage = billing.RawUsage.model_validate(value["raw_usage"]) if value["raw_usage"] is not None else None
        observed = billing.Eligibility.model_validate(value["observed"])
        terminal_value = value["terminal"]
        if isinstance(terminal_value, dict) and terminal_value.get("terminal_kind") == "refund" and terminal_value.get("charge_micro") != 0:
            # Validate its complete shape as settle, preserving the received amount.
            terminal = billing.TerminalEnvelope.model_validate({**terminal_value, "terminal_kind": "settle"})
            invalid_refund = terminal.charge_micro
            terminal = terminal.model_copy(update={"terminal_kind": "refund"})
        else:
            terminal = billing.TerminalEnvelope.model_validate(terminal_value) if terminal_value is not None else None
        if terminal is not None:
            for name in ("authorization_id", "workspace_id", "key_id", "invocation_nonce"):
                if len(getattr(terminal, name)) > 64:
                    _refuse("identity")
            for name in ("generation_id", "journal_region", "selected_endpoint"):
                if len(getattr(terminal, name)) > 128:
                    _refuse("identity")
            if not isinstance(value["payload_hash"], str) or not DIGEST.fullmatch(value["payload_hash"]):
                _refuse("hash")
    except ValidationError as exc:
        if any(error["type"] in {"int_type", "int_parsing", "greater_than_equal", "less_than_equal"} for error in exc.errors()):
            return _refuse("integer")
        return _refuse("json_shape")
    return Envelope(proof, snapshot, usage, observed, terminal, value["payload_hash"], failure,
                    value["go_revision"], value["handoff_prepare_us"], invalid_refund)
