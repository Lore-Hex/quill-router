# mypy: disable-error-code=assignment
# The original script reuses a loop variable with different types; preserve its verbatim body.
"""Verbatim v1 rules frozen at policy shadow-v2 (design §7).

A deliberate divergence requires a new policy id. Tests pin every copied
function/class AST and constant value to its original, including dependencies.
No live module is imported. The counter's function-local REASONS import is
resolved in that function's private builtins to the frozen constant below;
the copied function body, including that import, is verbatim.
"""
from __future__ import annotations

import builtins
import datetime as dt
import itertools
import re
import types
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from trusted_router.billing_snapshot import Digest, Frozen, Identity, UInt
from trusted_router.detached_jws import canonical

# Source: src/trusted_router/async_settle_ticket.py
NativeIdentity = Annotated[Identity, Field(max_length=64)]

# Source: src/trusted_router/async_settle_ticket.py
class TicketClaims(Frozen):
    authorization_id: NativeIdentity
    generation_id: Identity
    workspace_id: NativeIdentity
    key_id: NativeIdentity
    invocation_nonce: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    billing_authority: Literal["local"]
    journal_region: Identity
    epoch: UInt
    snapshot_version: Literal[1]
    snapshot_hash: Digest
    route_type: Literal["chat.completions", "responses"]
    streamed: Annotated[bool, Field(strict=True)]
    reservation_id: NativeIdentity
    settle_origin: Literal["typed"]
    async_eligible: Annotated[bool, Field(strict=True)]
    iss: Identity
    aud: Identity
    iat: UInt
    exp: UInt

# Source: src/trusted_router/storage_models.py
def generation_id_for_authorization(authorization_id: str) -> str:
    """Return the stable generation id for one gateway authorization.

    Settlement can commit while its HTTP response is lost.  The durable outbox
    then rebuilds and re-indexes the same metadata.  A deterministic id makes
    that repair idempotent in ClickHouse instead of creating duplicate activity
    rows on every replay.
    """
    return f"gen-{uuid.uuid5(uuid.NAMESPACE_URL, f'trustedrouter:{authorization_id}').hex}"

# Source: src/trusted_router/async_settle_shadow_binding.py
LIFETIME = 172800

# Source: src/trusted_router/async_settle_shadow_binding.py
FIXTURE_SHA256 = "68568699b000210413a20916ffa1f56093ab0223bb6a65d935ae453a9d96a28a"

# Source: src/trusted_router/async_settle_shadow_binding.py
class ShadowClaims(TicketClaims):
    authorization_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    generation_id: Annotated[str, Field(max_length=128)]
    journal_region: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./:@+\-]+$")]
    aud: Literal["router-shadow"]
    async_eligible: Literal[False]

# Source: src/trusted_router/async_settle_shadow_binding.py
def binding_valid_at(iat: int, exp: int, now: int) -> bool:
    """Whole-second claims, inclusive issuance and exclusive expiration."""
    return exp - iat == LIFETIME and iat <= now < exp

# Source: src/trusted_router/async_settle_shadow_binding.py
def validate_claims(claims: dict[str, Any], now: int) -> ShadowClaims:
    try:
        parsed = ShadowClaims.model_validate(claims)
    except ValidationError as exc:
        raise ValueError("proof_signature") from exc
    if (type(claims.get("async_eligible")) is not bool or parsed.epoch < 1
            or parsed.exp - parsed.iat != LIFETIME
            or parsed.generation_id != generation_id_for_authorization(parsed.authorization_id)):
        raise ValueError("proof_signature")
    if not binding_valid_at(parsed.iat, parsed.exp, now):
        raise ValueError("proof_expired")
    return parsed

# Source: src/trusted_router/async_settle_shadow_evidence.py
ADAPTERS = ("openai", "anthropic", "other", "unknown")

# Source: src/trusted_router/async_settle_shadow_evidence.py
ROUTES = ("chat.completions", "responses", "other", "unknown")

# Source: src/trusted_router/async_settle_shadow_evidence.py
DIMENSIONS = tuple(itertools.product(ADAPTERS, ROUTES, (False, True, None)))

# Source: src/trusted_router/async_settle_shadow_evidence.py
COUNT_FIELDS = "authorize_attempts authorize_fresh authorize_replay header_absent requested_eligible snapshot_sent settle_attempts refund_attempts envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable".split()

# Source: src/trusted_router/async_settle_shadow_evidence.py
PHASE_FIELDS = "envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown".split()

# Source: src/trusted_router/async_settle_shadow_evidence.py
REASONS = frozenset("""untyped non_credits settlement_authority unsupported_route service_tier app_markup custom_markup receipt_fee request_fee custom_model user_model tool_cost search_cost image_cost video_cost partner liberty native_batch fusion polyphemus private_tier_basis unsupported_adapter unknown_parameters replay header_absent snapshot_unavailable usage_missing usage_estimated malformed_usage arithmetic_overflow booking_pending booking_unknown rebuild_unavailable snapshot_reconstruction_failed catalog_change go_failure configuration_conflict missing_envelope snapshot_size winner_polarity legacy_oracle_unavailable counter_overflow header_duplicate header_size base64 json_encoding json_duplicate json_shape integer proof_signature proof_expired hash identity raw_usage rate_limit queue_full daily_cap store_unavailable evidence_size worker_error""".split())

# Source: src/trusted_router/async_settle_shadow_evidence.py
CLASSIFICATIONS = frozenset({"hash", "identity", "normalization", "evaluator_disagreement", "requires_review", "unevaluable", "exact", "explained-by-catalog-change"})

# Source: src/trusted_router/async_settle_shadow_evidence.py
SAMPLE_FIELDS = frozenset("""v policy_version authorization_id authorization_day observed_at_us authorize_at_us booking_observed_at_us workspace_fingerprint adapter route_type streamed model_id endpoint_id snapshot_hash payload_hash rebuilt_snapshot_hash raw_usage python_usage go_usage legacy_usage frozen_micro python_micro go_micro booked_micro rebuilt_micro legacy_frozen_micro python_minus_go booked_minus_frozen rebuilt_minus_frozen booked_minus_rebuilt classification reason_codes eligibility booking admission timing deployment provenance""".split())

# Source: src/trusted_router/async_settle_shadow_evidence.py
OBJECT_FIELDS = {
    "eligibility": "requested observed exclusion",
    "booking": "attempted_kind outcome source price_source",
    "admission": "prediction reason tier pending_micro cap_micro workspace_age_us health_age_us health_p95_us",
    "timing": "authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us evidence_write_us",
    "deployment": "region instance router_revision go_revision python_evaluator go_evaluator",
    "provenance": "binding_verified raw_matches_body rebuild_matches_booking_view snapshot_transport s0_reconstruction legacy_oracle fixture_sha256",
}

# Source: src/trusted_router/async_settle_shadow_evidence.py
def validate_sample(body: dict[str, Any], identity: str) -> None:
    import re

    if set(body) != SAMPLE_FIELDS or len(canonical(body)) > 8192:
        raise ValueError("evidence_size")
    if (type(body["v"]) is not int or body["v"] != 1 or body["policy_version"] != "shadow-v1"
            or body["classification"] not in CLASSIFICATIONS
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body["authorization_id"])
            or identity != body["authorization_day"] + "/" + body["authorization_id"]):
        raise ValueError("invalid sample")
    dt.date.fromisoformat(body["authorization_day"])
    for key, fields in OBJECT_FIELDS.items():
        if not isinstance(body[key], dict) or set(body[key]) != set(fields.split()):
            raise ValueError("invalid sample object")
    if (body["adapter"], body["route_type"], body["streamed"]) not in DIMENSIONS:
        raise ValueError("invalid dimensions")
    reasons = body["reason_codes"]
    if not isinstance(reasons, list) or len(reasons) > 8 or sorted(set(reasons)) != reasons or not set(reasons) <= REASONS:
        raise ValueError("invalid reasons")
    for name in ("snapshot_hash", "payload_hash", "rebuilt_snapshot_hash", "workspace_fingerprint"):
        if body[name] is not None and (not isinstance(body[name], str) or not re.fullmatch(r"[0-9a-f]{64}", body[name])):
            raise ValueError("invalid hash")
    for name, value in body.items():
        if name.endswith(("_micro", "_us")) or "_minus_" in name:
            if value is not None and (type(value) is not int or abs(value) > (1 << 63) - 1 or value < 0 and "_minus_" not in name):
                raise ValueError("invalid integer")
    for name, expected in (("raw_usage", "input_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens"),
                           *((key, "uncached_input_tokens total_prompt_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens") for key in ("python_usage", "go_usage", "legacy_usage"))):
        value = body[name]
        if value is not None and (not isinstance(value, dict) or set(value) != set(expected.split()) or
                                  any(type(n) is not int or not 0 <= n < 1 << 63 for n in value.values())):
            raise ValueError("invalid usage")
    if body["timing"]["evidence_write_us"] is not None:
        raise ValueError("self timing")
    if body["provenance"]["fixture_sha256"] != FIXTURE_SHA256:
        raise ValueError("fixture")
    for name in ("model_id", "endpoint_id"):
        if body[name] is not None and (not isinstance(body[name], str) or re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,128}", body[name]) is None):
            raise ValueError("invalid catalog identity")
    for name in ("observed_at_us", "authorize_at_us"):
        if body[name] is None:
            raise ValueError("missing time")
    if dt.datetime.fromtimestamp(body["authorize_at_us"]/1e6, dt.UTC).date().isoformat() != body["authorization_day"]:
        raise ValueError("authorize day")
    if type(body["streamed"]) not in (bool, type(None)):
        raise ValueError("stream type")
    eligibility, booking, admission = body["eligibility"], body["booking"], body["admission"]
    if (type(eligibility["requested"]) is not bool or type(eligibility["observed"]) not in (bool, type(None))
            or eligibility["exclusion"] is not None and eligibility["exclusion"] not in REASONS):
        raise ValueError("eligibility")
    if (booking["attempted_kind"] not in {"settle", "refund"} or booking["outcome"] not in {"settled", "refunded", "pending", "unknown"}
            or booking["source"] not in {"finalized_authorization", "none"}
            or booking["price_source"] not in {"catalog_at_authorize_time", "stage_d_document", "unknown"}):
        raise ValueError("booking")
    if (admission["prediction"] not in {"yes", "no", "unknown"} or admission["reason"] not in {
            "eligible", "ineligible_tier", "cap_exceeded", "drain_unhealthy", "cache_missing", "cache_stale", "cache_busy", "invalid_data"}):
        raise ValueError("admission")
    for value in [*(v for k, v in admission.items() if k not in {"prediction", "reason"}), *body["timing"].values()]:
        if value is not None and (type(value) is not int or not 0 <= value < 1 << 63):
            raise ValueError("nested integer")
    provenance = body["provenance"]
    if any(type(provenance[k]) is not bool for k in ("binding_verified", "raw_matches_body", "rebuild_matches_booking_view")):
        raise ValueError("provenance boolean")
    if (provenance["snapshot_transport"] not in {"full", "hash_only", "unknown"}
            or provenance["s0_reconstruction"] not in {"not_needed", "verified", "failed", "not_attempted"}
            or provenance["legacy_oracle"] not in {"stage_d_candidate_v1", "unavailable"}):
        raise ValueError("provenance enum")
    deployment = body["deployment"]
    for key, value in deployment.items():
        if key == "go_revision" and value is None:
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isascii():
            raise ValueError("deployment")
        if key.endswith("revision") and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("deployment revision")
        if key.endswith("evaluator") and value != "billing-v1":
            raise ValueError("evaluator identity")
    if body["classification"] in {"exact", "explained-by-catalog-change"}:
        if (provenance["binding_verified"] is not True or provenance["raw_matches_body"] is not True
                or any(body[k] is None for k in ("python_micro", "go_micro", "booked_micro", "legacy_frozen_micro"))
                or not body["python_micro"] == body["go_micro"] == body["legacy_frozen_micro"]
                or body["python_usage"] != body["go_usage"] or body["python_usage"] != body["legacy_usage"]
                or (booking["attempted_kind"], booking["outcome"]) not in {("settle", "settled"), ("refund", "refunded")}):
            raise ValueError("contradictory clean sample")
        if body["classification"] == "exact" and body["booked_micro"] != body["python_micro"]:
            raise ValueError("contradictory equality")
        if body["classification"] == "explained-by-catalog-change" and (
                body["booked_micro"] != body["rebuilt_micro"] or body["snapshot_hash"] == body["rebuilt_snapshot_hash"]
                or body["booked_micro"] == body["python_micro"] or booking["attempted_kind"] != "settle"
                or provenance["rebuild_matches_booking_view"] is not True):
            raise ValueError("contradictory explanation")
    if "winner_polarity" in body["reason_codes"] and (body["booked_minus_frozen"] is not None or body["booked_minus_rebuilt"] is not None):
        raise ValueError("winner delta")
    for delta, left, right in (("python_minus_go", "python_micro", "go_micro"),
                               ("booked_minus_frozen", "booked_micro", "frozen_micro"),
                               ("rebuilt_minus_frozen", "rebuilt_micro", "frozen_micro"),
                               ("booked_minus_rebuilt", "booked_micro", "rebuilt_micro")):
        if body[delta] is not None and (body[left] is None or body[right] is None or body[delta] != body[left] - body[right]):
            raise ValueError("invalid delta")

# Source: scripts/async_settle/shadow_report.py
COUNTER_FIELDS = set("v instance region router_revision policy_version started_at_us flushed_at_us sequence closed counts terminal_counts exclusions rejections drops dimension_overflow counter_overflow comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown first_evidence_at_us last_mismatch_at_us first_gap_at_us authorize_shadow_hist evidence_write_hist admission_observer".split())

# Source: scripts/async_settle/shadow_report.py
def _uint(value: Any) -> bool:
    return type(value) is int and 0 <= value < 1 << 63

# Source: scripts/async_settle/shadow_report.py
def validate_counter(identity: str, body: dict[str, Any], *, partitions: bool = True) -> None:
    _validate_counter(identity, body, partitions=partitions, revision_pattern=r"[0-9a-f]{40}")

# Source: scripts/async_settle/shadow_report.py
def _validate_counter(identity: str, body: dict[str, Any], *, partitions: bool, revision_pattern: str) -> None:
    import uuid
    if set(body) != COUNTER_FIELDS or len(canonical(body)) > 65536:
        raise ValueError("counter schema")
    day, boot = identity.split("/")
    dt.date.fromisoformat(day)
    if str(uuid.UUID(boot)) != boot or body["instance"] != boot or body["v"] != 1 or type(body["v"]) is not int:
        raise ValueError("counter identity")
    if body["policy_version"] != "shadow-v1" or type(body["closed"]) is not bool:
        raise ValueError("counter policy")
    if not all(_uint(body[k]) for k in ("started_at_us", "flushed_at_us", "sequence", "dimension_overflow", "comparison_attempts", "comparison_dropped", "samples_inserted", "duplicate_samples", "conflicting_samples", "booking_pending", "booking_unknown")):
        raise ValueError("counter integer")
    if body["flushed_at_us"] < body["started_at_us"] or body["sequence"] < 1:
        raise ValueError("counter interval")
    if type(body["counter_overflow"]) is not bool:
        raise ValueError("overflow flag")
    for key in ("first_evidence_at_us", "last_mismatch_at_us", "first_gap_at_us"):
        if body[key] is not None and not _uint(body[key]):
            raise ValueError("counter timestamp")
    if not isinstance(body["router_revision"], str) or not re.fullmatch(revision_pattern, body["router_revision"]):
        raise ValueError("counter revision")
    if not isinstance(body["region"], str) or not 1 <= len(body["region"]) <= 128:
        raise ValueError("counter region")
    observer_fields = set("workspace_reads health_reads read_failures missed_ticks late_installs max_consecutive_failures degraded_seconds prediction_yes prediction_no prediction_unknown".split())
    observer = body["admission_observer"]
    if set(observer) != observer_fields or not all(_uint(n) for n in observer.values()):
        raise ValueError("observer schema")
    buckets = body["counts"]
    if not isinstance(buckets, list) or len(buckets) != len(DIMENSIONS):
        raise ValueError("counter buckets")
    found = []
    for bucket in buckets:
        if set(bucket) != {"adapter", "route_type", "streamed", *COUNT_FIELDS} or not all(_uint(bucket[k]) for k in COUNT_FIELDS):
            raise ValueError("counter bucket schema")
        found.append((bucket["adapter"], bucket["route_type"], bucket["streamed"]))
        if partitions and (bucket["observed_attempts"] != bucket["settle_attempts"] + bucket["refund_attempts"]
                or bucket["observed_attempts"] != sum(bucket[k] for k in ("observed_eligible", "observed_ineligible", "observed_unknown"))
                or bucket["authorize_attempts"] != bucket["authorize_fresh"] + bucket["authorize_replay"]
                or bucket["evaluable"] != bucket["exact"] + bucket["explained"]
                or bucket["envelope_present"] > bucket["observed_attempts"]
                or bucket["header_absent"] > bucket["authorize_attempts"]):
            raise ValueError("counter partition")
    if tuple(found) != DIMENSIONS:
        raise ValueError("counter dimensions")
    outcomes = sum(bucket[k] for bucket in buckets for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
    if partitions and (outcomes > body["comparison_attempts"] or body["samples_inserted"] > body["comparison_attempts"]):
        raise ValueError("counter comparison partition")
    if sum(bucket["mismatch"] for bucket in buckets) and body["last_mismatch_at_us"] is None:
        raise ValueError("missing mismatch timestamp")
    if sum(len(body[k]) for k in ("exclusions", "rejections", "drops")) > 128:
        raise ValueError("counter dimensions overflow")
    from trusted_router.async_settle_shadow_evidence import REASONS
    for name in ("exclusions", "rejections", "drops"):
        seen = set()
        for row in body[name]:
            if (set(row) != {"phase", "adapter", "route_type", "streamed", "reason", "count"}
                    or row["phase"] not in {"authorize", "settle", "refund", "worker"}
                    or row["reason"] not in REASONS or not _uint(row["count"])
                    or (row["adapter"], row["route_type"], row["streamed"]) not in DIMENSIONS):
                raise ValueError("reason schema")
            key = tuple(row[k] for k in ("phase", "adapter", "route_type", "streamed", "reason"))
            if key in seen:
                raise ValueError("duplicate reason")
            seen.add(key)
    seen_phases = set()
    if not isinstance(body["terminal_counts"], list) or len(body["terminal_counts"]) > 2 * len(DIMENSIONS):
        raise ValueError("terminal counter bounds")
    for row in body["terminal_counts"]:
        if (set(row) != {"phase", "adapter", "route_type", "streamed", *PHASE_FIELDS}
                or row["phase"] not in {"settle", "refund"}
                or (row["adapter"], row["route_type"], row["streamed"]) not in DIMENSIONS
                or not all(_uint(row[k]) for k in PHASE_FIELDS)):
            raise ValueError("terminal counter schema")
        key = (row["phase"], row["adapter"], row["route_type"], row["streamed"])
        if key in seen_phases:
            raise ValueError("duplicate terminal counter")
        seen_phases.add(key)
    for key in ("authorize_shadow_hist", "evidence_write_hist"):
        if len(body[key]) != 9 or not all(_uint(n) for n in body[key]):
            raise ValueError("histogram")

# Keep the local import verbatim without importing the live evidence module.
def _frozen_import(name: str, globals: Any = None, locals: Any = None,
                   fromlist: Any = (), level: int = 0) -> Any:
    if name == "trusted_router.async_settle_shadow_evidence" and fromlist == ("REASONS",) and level == 0:
        return types.SimpleNamespace(REASONS=REASONS)
    return builtins.__import__(name, globals, locals, fromlist, level)


_counter_globals = {**globals(), "__builtins__": {**vars(builtins), "__import__": _frozen_import}}
_frozen_counter = types.FunctionType(_validate_counter.__code__, _counter_globals,
                                     _validate_counter.__name__)
_frozen_counter.__kwdefaults__ = _validate_counter.__kwdefaults__
_validate_counter = _frozen_counter
