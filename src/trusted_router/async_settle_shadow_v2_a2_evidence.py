"""Additive A2 report shapes and assertion identities; no evidence evaluation."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_schema import (
    COORDINATES,
    Check,
    Schema,
    array,
    ascii_text,
    boolean,
    day,
    digest,
    identity,
    integer,
    literal,
    name,
    nullable,
    require,
    revision,
    sequence,
    sha256,
    signed_integer,
    text,
    versioned,
)
from trusted_router.detached_jws import canonical

FIXTURE_MANIFEST_SHA256 = "ae3835553ddacc18a12eb5206f355cff92732b8a401cdc2a74a2afa13697cc78"
ROOT_ENTRY = a1.COVERAGE_ENTRY
GATE_OUTPUT = a1.GATE_OUTPUT
TAG = {"contract_revision": literal("a2")}
ROLE = literal("router", "enclave")
GATE = p1.GATE_OUTPUT.fields["gate"]
RESET_REASONS = tuple("hash identity normalization evaluator_disagreement counter_mismatch reviewed_resolution revision_rollback exclusion_conflict".split())
TIMINGS = tuple("authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us evidence_write_us workspace_age_us health_age_us publisher_cycle_us publisher_slot_rpc_elapsed_us close_us handoff_us drain_us".split())
BINS = tuple("0,+1,-1,+2-10,-2-10,+11-100,-11-100,+101-1000,-101-1000,+>1000,->1000".split(","))
RETAINED_ENTRY_POINTS = tuple("pilot_enablement." + n for n in "fleet_budgets_pre fleet_budgets shadow_serving pre_flip post_flip counter_coverage_report roster_coverage".split()) + ("shadow_report.collect_rollbacks",)


def _a2(fields: Mapping[str, Check], limit: int = 262144, invariant: Check | None = None) -> Schema:
    return versioned({**TAG, **fields}, limit, invariant)


def _bounded(maximum: int) -> Check:
    def check(value: Any) -> None:
        integer(value)
        require(value <= maximum)
    return check


def _sorted(value: list[Any]) -> None:
    require(value == sorted(set(value)))


def L(*components: str) -> str:
    """Canonical length-prefixed ASCII tuple, shared by roots and virtual keys."""
    require(bool(components))
    for component in components:
        ascii_text(512, 1)(component)
    result = "/".join(str(len(c)) + ":" + c for c in components)
    ascii_text(512, 1)(result)
    return result


def decode_key(value: Any) -> tuple[str, ...]:
    ascii_text(512, 1)(value)
    parts: list[str] = []
    position = 0
    while position < len(value):
        colon = value.find(":", position)
        require(colon > position)
        length = value[position:colon]
        require(length.isdecimal() and length.isascii() and length[0] != "0")
        size = int(length)
        end = colon + 1 + size
        require(end <= len(value))
        parts.append(value[colon + 1:end])
        require(end == len(value) or value[end] == "/")
        position = end + 1
    require(L(*parts) == value)
    return tuple(parts)


def _subject(scope: str, **fields: Any) -> dict[str, Any]:
    result = dict.fromkeys("day role boot phase dimensions object at_us revision".split())
    result.update(scope=scope, **fields)
    FAILURE_SUBJECT(result)
    return result


def N() -> dict[str, Any]:
    return _subject("run")


def D(d: str) -> dict[str, Any]:
    return _subject("day", day=d)


def W(d: str, r: str, b: str) -> dict[str, Any]:
    return _subject("writer", day=d, role=r, boot=b)


def B(d: str, b: str, p: str, x: dict[str, Any]) -> dict[str, Any]:
    return _subject("bucket", day=d, role="router", boot=b, phase=p, dimensions=x)


def O(k: str, x: str) -> dict[str, Any]:  # noqa: E743 - normative constructor name
    return _subject("object", object={"kind": k, "key": x, "event_seq": None})


def E(d: str, b: str, f: str, q: str, *, event: dict[str, Any]) -> dict[str, Any]:
    """Caller supplies independently verified coordinates; invalid events use I."""
    COORDINATES({"day": d, "boot": b, "first_seq": f, "event_seq": q})
    p1.EVENT(event)
    require(event["seq"] == q and p1.day_at(event["event_at_us"]) == d)
    return _subject("object", day=d, role="router", boot=b, phase=event["phase"],
        dimensions=event["dimensions"], object={"kind": "async_settle_shadow_receipts_v2",
        "key": d + "/" + b + "/" + f, "event_seq": q})


def I(d: str, b: str, f: str, i: int) -> dict[str, Any]:  # noqa: E743 - normative constructor name
    day(d)
    identity(b)
    sequence(f)
    integer(i)
    require(i < p1.PAGE_EVENTS)
    return _subject("object", day=d, role="router", boot=b,
        object={"kind": "a2_invalid_receipt", "key": L(d + "/" + b + "/" + f, "event", str(i)), "event_seq": None})


def V(r: str) -> dict[str, Any]:
    name(r)
    return O("a2_region", L("region", r))


def K(t: str, r: str | None) -> dict[str, Any]:
    literal("pre", "post")(t)
    nullable(name)(r)
    return O("a2_budget", L(t, "fleet", "cap") if r is None else L(t, "region", r, "cap"))


def A(t: str) -> dict[str, Any]:
    literal("pre", "post")(t)
    return O("a2_budget", L(t, "approval"))


def Q(z: dict[str, Any]) -> dict[str, Any]:
    RESET_ENTRY(z)
    return _subject("reset", at_us=z["at_us"], revision=z["revision"],
        object={"kind": "a2_reset", "key": L("null" if z["at_us"] is None else str(z["at_us"]),
        z["revision"], z["reason"], z["support"]["root"]), "event_seq": None})


def P(g: str, n: str) -> dict[str, Any]:
    GATE(g)
    name(n)
    return O("a2_proof", L(g, n))


@dataclass(frozen=True)
class FailureRule:
    name: str
    rule: str
    population: str
    constructor: tuple[str, ...]
    unevaluable: str


# ONE row per name. P5 imports this table; schemas and routing derive from it.
FAILURE_RULES = (
    FailureRule('policy_version_gap', 'G1', 'Each expected source/artifact logical object O, or receipt E/I, for its own policy/cohort check; cohort configuration uses its O', ('O', 'E', 'I'), 'self'),
    FailureRule('evidence_schema_gap', 'G1', 'Each expected source/artifact logical object O, or receipt E/I, for schema/identity/commitment checks', ('O', 'E', 'I'), 'self'),
    FailureRule('artifact_gap', 'G1', 'Each expected source/artifact logical object O for availability/digest/provenance; interrupted/incomplete run N', ('O', 'N'), 'self'),
    FailureRule('writer_roster_gap', 'G2', 'Each scoped day D; all manifest/inventory writer-set disagreements that day', ('D',), 'self'),
    FailureRule('regional_roster_unknown', 'G2', 'Each inventory/observed writer-day W', ('W',), 'self'),
    FailureRule('legacy_region_label', 'G2', 'Each inventory/observed writer-day W', ('W',), 'self'),
    FailureRule('legacy_sample_region', 'G2', 'Each retained sample logical key O', ('O',), 'self'),
    FailureRule('flag_interval_gap', 'G2', 'Each inventory/observed writer-day W; all four flags/lifetime segments together', ('W',), 'self'),
    FailureRule('boot_prerequisite_gap', 'G2', 'Each traffic writer-day W; all readiness/ledger prerequisites together', ('W',), 'self'),
    FailureRule('authorize_coverage_gap', 'G3', 'Each router writer-day W; fresh/replay counts and receipt-chain coverage together', ('W',), 'self'),
    FailureRule('authorization_link_gap', 'G3', 'Each receipt-derived authorization obligation E at its original_receipt', ('E',), 'self'),
    FailureRule('authorization_obligation_gap', 'G3', 'Each router writer-day W; projection/count/root completeness together', ('W',), 'self'),
    FailureRule('authorization_terminal_conflict', 'G3', 'Each receipt-derived authorization obligation E at its original_receipt', ('E',), 'self'),
    FailureRule('authorization_horizon_gap', 'G3', 'Each receipt-derived authorization obligation E at its original_receipt', ('E',), 'self'),
    FailureRule('observed_partition_gap', 'G4', 'Each settle/refund bucket B; partition equation and C=0', ('B',), 'self'),
    FailureRule('terminal_coverage_gap', 'G4', 'Each original or retry settle/refund receipt E; proof/booking/once-only coverage after valid completion reconciliation', ('E',), 'self'),
    FailureRule('phase_coverage_gap', 'G4', 'Each router writer-day W; phase/aggregate/retained-array equations together', ('W',), 'self'),
    FailureRule('exclusion_proof_gap', 'G4', 'Each original or retry settle/refund receipt E; applicable exclusion proofs and original-receipt worker subcodes together', ('E',), 'self'),
    FailureRule('eligible_coverage_gap', 'G5', 'Each router writer-day W; independent eligible-attempt enumeration and comparator coverage together', ('W',), 'self'),
    FailureRule('prediction_demand_gap', 'G5', 'Each settle/refund bucket B; independent E and Y+N+U equation/known-demand checks', ('B',), 'self'),
    FailureRule('prediction_unknown_gap', 'G5', 'Each settle/refund bucket B; unchanged unknown fraction', ('B',), 'self'),
    FailureRule('observer_cadence_gap', 'G6', 'Each router writer-day W; all observer intervals together', ('W',), 'self'),
    FailureRule('observer_budget_gap', 'G6', 'Each router writer-day W; all observer read/failure/streak/degraded limits together', ('W',), 'self'),
    FailureRule('shadow_health_gap', 'G6', 'Each router writer-day W; all required health coverage/usability checks together', ('W',), 'self'),
    FailureRule('health_source_gap', 'G6', 'Each router writer-day W; all source/isolation checks together', ('W',), 'self'),
    FailureRule('health_timestamp_gap', 'G6', "Each router writer-day W; all referenced health bases' O/C/R timestamp checks together", ('W',), 'self'),
    FailureRule('health_elapsed_gap', 'G6', "Each router writer-day W; all referenced bases' q/e/deadline checks together", ('W',), 'self'),
    FailureRule('health_slot_gap', 'G6', 'Each router writer-day W; all reader references and publisher claim/slot/version fences together', ('W',), 'self'),
    FailureRule('publisher_accounting_gap', 'G6', 'Each router writer-day W; all scheduled/attempted/outcome and operation conservation checks together', ('W',), 'self'),
    FailureRule('publisher_trace_gap', 'G6', 'Each router writer-day W; all required slots/batches/chains/histogram reconciliation together', ('W',), 'self'),
    FailureRule('simultaneous_capacity_gap', 'G7', 'Each required budget stage and fleet/actual-region scope K; all simultaneous/configured/startup/churn cap checks together', ('K',), 'self'),
    FailureRule('pre_enable_budget', 'G7', 'Required pre-enable budget approval/prerequisites A(pre), once per run', ('A',), 'self'),
    FailureRule('publisher_budget_gap', 'G7', 'Each router writer-day W; all contender/publication RPC/read/write limits together', ('W',), 'self'),
    FailureRule('fleet_headroom_gap', 'G7', 'Each required budget stage and fleet/actual-region scope K; CPU/headroom/approval/pre-post matching together', ('K',), 'self'),
    FailureRule('freshness_budget_gap', 'G7', 'Each router writer-day W; all conservative-chain/component-deadline evidence together', ('W',), 'self'),
    FailureRule('evidence_export_budget_gap', 'G7', 'Run N; complete approved plan, every pass/rerun/cleanup and envelope approval together', ('N',), 'self'),
    FailureRule('close_handoff_budget_gap', 'G7', 'Each closing router writer-day W; final-pass/result/burst/grace measurements together', ('W',), 'self'),
    FailureRule('checkpoint_reconciliation_gap', 'G8', 'Each router writer-day W; every checkpoint/page/HWM/recovery check together', ('W',), 'self'),
    FailureRule('evidence_loss_gap', 'G8', 'Each router writer-day W; all loss/overflow diagnostics together', ('W',), 'self'),
    FailureRule('writer_closure_gap', 'G8', 'Each expected closing router writer-day W; original close/closure/acknowledgement together', ('W',), 'self'),
    FailureRule('retry_original_gap', 'G8', 'Each retry or completion receipt E; all original/context/order checks together', ('E',), 'self'),
    FailureRule('close_result_identity_gap', 'G8', 'Each expected close-result row O(async_settle_shadow_close_result_v2,day+"/"+boot)', ('O',), 'self'),
    FailureRule('close_result_conflict', 'G8', 'Each expected close-result row O(async_settle_shadow_close_result_v2,day+"/"+boot); all conflicting bodies together', ('O',), 'self'),
    FailureRule('handoff_coverage_gap', 'G8', 'Each closing router writer-day W; expected result/coverage/accounting together', ('W',), 'self'),
    FailureRule('hash', 'G9', 'Each retained reset fact Q of reason hash, subject to retained active/reset predicate', ('Q',), 'support'),
    FailureRule('identity', 'G9', 'Each retained reset fact Q of reason identity, subject to retained active/reset predicate', ('Q',), 'support'),
    FailureRule('normalization', 'G9', 'Each retained reset fact Q of reason normalization, subject to retained active/reset predicate', ('Q',), 'support'),
    FailureRule('evaluator_disagreement', 'G9', 'Each retained reset fact Q of that reason, subject to retained active/reset predicate', ('Q',), 'support'),
    FailureRule('counter_mismatch', 'G9', 'Each retained reset fact Q of reason counter_mismatch, including unsampled history', ('Q',), 'support'),
    FailureRule('reviewed_resolution', 'G9', 'Each retained reset fact Q of reason reviewed_resolution; all resolution checks together', ('Q',), 'support'),
    FailureRule('revision_rollback', 'G9', 'Each retained reset fact Q of reason revision_rollback; all rollback checks together', ('Q',), 'support'),
    FailureRule('missing_support', 'G9', 'Each retained reset fact Q whose full provenance cannot be established', ('Q',), 'self'),
    FailureRule('exclusion_conflict', 'G9', 'Each retained reset fact Q of reason exclusion_conflict; G5 forwards evidence to this producer', ('Q',), 'support'),
    FailureRule('positive_seed_gap', 'G10', 'Run N', ('N',), 'self'),
    FailureRule('regional_seed_gap', 'G10', 'Each policy-required traffic-serving router region V', ('V',), 'self'),
    FailureRule('continuous_window_gap', 'G10', 'Run N', ('N',), 'self'),
    FailureRule('external_proofs_missing', 'G11', 'Each stage-required external proof P(stage,name); all checks/dependencies/approval together', ('P',), 'self'),
    FailureRule('pilot_prerequisite_gap', 'G11', 'Each required pilot stage P(stage,pilot); all retained pilot prerequisites together', ('P',), 'self'),
)
# Intrinsic run populations, distinct from the common unenumerable fallback.
RUN_SCOPED_FAILURES = frozenset(("evidence_export_budget_gap",
    "positive_seed_gap", "continuous_window_gap"))
GAP_NAME = literal(*(row.name for row in FAILURE_RULES))


def _subject_shape(v: Any) -> None:
    allowed = {"run": (), "day": ("day",), "writer": ("day", "role", "boot"),
        "bucket": ("day", "role", "boot", "phase", "dimensions"),
        "reset": ("at_us", "revision", "object"), "object": ("object",)}[v["scope"]]
    obj = v["object"]
    if v["scope"] == "object" and obj is not None:
        kind = obj["kind"]
        if kind == "a2_invalid_receipt":
            parts = decode_key(obj["key"])
            require(len(parts) == 3 and parts[1] == "event")
            _receipt_subject(v, parts[0])
            require(parts[2].isascii() and parts[2].isdecimal() and str(int(parts[2])) == parts[2]
                    and int(parts[2]) < p1.PAGE_EVENTS and obj["event_seq"] is None)
            allowed = ("day", "role", "boot", "object")
        elif obj["event_seq"] is not None:
            require(kind == "async_settle_shadow_receipts_v2")
            first = _receipt_subject(v, obj["key"])
            require(first <= obj["event_seq"] and v["phase"] is not None and v["dimensions"] is not None)
            allowed = ("day", "role", "boot", "phase", "dimensions", "object")
        elif kind.startswith("a2_"):
            require(kind in ("a2_file", "a2_region", "a2_budget", "a2_proof"))
            if kind == "a2_file":
                _path(obj["key"])
            else:
                parts = decode_key(obj["key"])
                if kind == "a2_region":
                    require(len(parts) == 2 and parts[0] == "region")
                    name(parts[1])
                elif kind == "a2_proof":
                    require(len(parts) == 2)
                    GATE(parts[0])
                    name(parts[1])
                else:
                    require(len(parts) in (2, 3, 4))
                    literal("pre", "post")(parts[0])
                    require(parts[1:] == ("approval",) or parts[1:] == ("fleet", "cap")
                            or len(parts) == 4 and parts[1] == "region" and parts[3] == "cap")
                    if len(parts) == 4:
                        name(parts[2])
    for field in FAILURE_SUBJECT.fields:
        if field != "scope":
            require(field in allowed or v[field] is None)
    for field in allowed:
        require(v[field] is not None or field == "at_us")
    if v["scope"] == "bucket":
        require(v["role"] == "router" and v["phase"] in ("authorize", "settle", "refund"))
    if v["scope"] == "reset":
        require(obj["kind"] == "a2_reset" and obj["event_seq"] is None)
        parts = decode_key(obj["key"])
        require(len(parts) == 4 and parts[0] == ("null" if v["at_us"] is None else str(v["at_us"]))
                and parts[1] == v["revision"] and parts[2] in RESET_REASONS)
        digest(parts[3])
        require(v["at_us"] is not None or parts[2] == "counter_mismatch")


def _receipt_subject(v: Any, key: str) -> str:
    parts = key.split("/")
    require(len(parts) == 3 and parts[:2] == [v["day"], v["boot"]] and v["role"] == "router")
    sequence(parts[2])
    return str(parts[2])


FAILURE_SUBJECT = Schema({"scope": literal("run", "day", "writer", "bucket", "object", "reset"),
    "day": nullable(day), "role": nullable(ROLE), "boot": nullable(identity),
    "phase": nullable(literal("authorize", "replay", "settle", "refund", "completion", "worker")),
    "dimensions": nullable(p1.DIMENSIONS), "object": nullable(Schema({"kind": name,
        "key": ascii_text(512, 1), "event_seq": nullable(sequence)}, 700)),
    "at_us": nullable(integer), "revision": nullable(revision)}, 768, _subject_shape)


def _failure(v: Any) -> None:
    row = next(row for row in FAILURE_RULES if row.name == v["gap"])
    subject = v["subject"]
    scope = subject["scope"]
    if scope == "run":
        return  # Listed fallback completeness is checked by validate_gate_output.
    code = {"day": "D", "writer": "W", "bucket": "B", "reset": "Q", "object": "O"}[scope]
    if scope == "object":
        obj = subject["object"]
        code = {"a2_invalid_receipt": "I", "a2_region": "V", "a2_proof": "P"}.get(obj["kind"], "O")
        if obj["kind"] == "a2_budget":
            code = "A" if decode_key(obj["key"])[1] == "approval" else "K"
        if obj["event_seq"] is not None:
            code = "E"
    require(code in row.constructor or "E" in row.constructor and code in ("I", "O"))
    if code == "O" and "O" not in row.constructor:
        # E may fall back only to the expected receipt page, never an arbitrary O.
        require(subject["object"]["kind"] == "async_settle_shadow_receipts_v2")
        parts = subject["object"]["key"].split("/")
        require(len(parts) == 3)
        day(parts[0])
        identity(parts[1])
        sequence(parts[2])
    if code == "W" and row.rule not in ("G2",):
        require(subject["role"] == "router")
    if code == "E" and row.rule in ("G3", "G4"):
        require(subject["phase"] in (("authorize", "replay") if row.rule == "G3" else ("settle", "refund")))
    if code == "B":
        require(subject["phase"] in ("settle", "refund"))
    if code == "Q" and v["gap"] != "missing_support":
        require(decode_key(subject["object"]["key"])[2] == v["gap"])
    if v["gap"] in ("close_result_identity_gap", "close_result_conflict"):
        require(subject["object"]["kind"] == "async_settle_shadow_close_result_v2")
        parts = subject["object"]["key"].split("/")
        require(len(parts) == 2)
        day(parts[0])
        identity(parts[1])
    if v["gap"] == "pre_enable_budget":
        require(subject["object"]["key"] == L("pre", "approval"))
    if v["gap"] == "pilot_prerequisite_gap":
        require(decode_key(subject["object"]["key"])[1] == "pilot")


GATE_FAILURE = _a2({"gap": GAP_NAME, "subject": FAILURE_SUBJECT, "count": literal(1)}, 1024, _failure)


def _ratio(v: Any) -> None:
    if v["state"] == "evaluated":
        require(v["numerator"] is not None and v["denominator"] is not None
                and 0 < v["denominator"] and v["numerator"] <= v["denominator"])
    elif v["state"] == "not_exercised":
        require(v["numerator"] == v["denominator"] == 0)
    else:
        require(v["numerator"] is v["denominator"] is None)


RATIO = Schema({"state": literal("evaluated", "not_exercised", "unevaluable", "not_required"),
    "numerator": nullable(integer), "denominator": nullable(integer)}, 256, _ratio)


def _stat(v: Any) -> None:
    if v["count"] is None:
        require(all(value is None for value in v.values()))
    else:
        require(v["null_count"] is not None)
        values = [v[k] for k in ("min", "p50", "p95", "p99", "max") if v[k] is not None]
        require(values == sorted(values))
        if v["count"] == 0:
            require(not values)
        # Each declared nullable statistic may be independently unavailable.


STAT = Schema(dict.fromkeys("count null_count min p50 p95 p99 max".split(), nullable(integer)), 512, _stat)
RATIONAL = Schema({"numerator": integer, "denominator": integer}, 128,
    lambda v: require(v["denominator"] > 0 and math.gcd(v["numerator"], v["denominator"]) == 1))
PEAKS = Schema({**dict.fromkeys("simultaneous_routers simultaneous_publishers configured_routers configured_publishers startup_operations_1s close_operations_1s export_operations_1s".split(), nullable(integer)),
    **dict.fromkeys("reads_per_second pending_rows_per_second trust_rows_per_second health_rows_per_second claim_operations_per_second scan_operations_per_second publication_operations_per_second export_operations_per_second export_bytes_per_second configured_reads_per_second configured_pending_rows_per_second cpu_percent".split(), nullable(RATIONAL))}, 4096)


def _counts(fields: str) -> Schema:
    return Schema(dict.fromkeys(fields.split(), nullable(integer)), 2048)


COMPARATOR = Schema({**_counts("eligible compared exact explained mismatch unevaluable").fields, "coverage": RATIO}, 2048)
ADMISSION = Schema({**_counts("E Y N U").fields, "coverage": RATIO}, 2048)
PARTITION = _counts("T A B C")
ATTEMPTS = _counts("authorize_fresh authorize_replay terminal_original terminal_retry completion")
CHECKPOINT = _counts("attempts failures recoveries unknown_commits unreconciled_commits irrecoverable_losses evidence_loss")
HANDOFF = _counts("expected landed attempts_lower attempts_upper failures_lower failures_upper unknown_reply_upper")
PUBLISHER = Schema({**_counts("scheduled_slots claim_attempts claims lost_claims scans publications failures unknown_outcomes late_cycles missed_slots").fields, "peaks": PEAKS}, 6144)
TIMING = Schema(dict.fromkeys(TIMINGS, STAT), 8192)
DELTAS = Schema({"count": nullable(integer), "null_count": nullable(integer),
    "min_micro": nullable(signed_integer), "max_micro": nullable(signed_integer),
    "bins": Schema(dict.fromkeys(BINS, nullable(integer)), 1024)}, 2048)
NULL_COUNTS = _counts("eligibility comparison prediction booking timing delta")
DETAIL = Schema({"root": digest, "count": integer, "pages": integer, "tail_sha256": nullable(digest)}, 512,
    lambda v: require((v["count"] > 0 and 0 < v["pages"] <= v["count"] and v["tail_sha256"] is not None)
        or v["count"] == v["pages"] == 0 and v["tail_sha256"] is None and v["root"] == sha256([])))


def _tally(v: Any) -> None:
    require(v["listed"] == min(v["count"], 16) and v["complete"] == (v["count"] == v["listed"]))
    require(all(n <= v["count"] for n in v["day_counts"]))


FAILURE_TALLY = Schema({"count": integer, "listed": _bounded(16), "complete": boolean,
    "day_counts": array(integer, 32)}, 768, _tally)
FAILURE_COUNTS = Schema(dict.fromkeys((row.name for row in FAILURE_RULES), FAILURE_TALLY), 49152)
VERIFIER_USE = Schema({"entry_point": name, "source_sha256": digest, "fixture_sha256": digest,
    "keyset_sha256": nullable(digest), "calls": integer}, 1024)


def _verifiers(v: Any) -> None:
    keys = [item["entry_point"] for item in v["retained_tools"]]
    require(keys == sorted(keys))
    require(all(item["keyset_sha256"] is None for item in v["retained_tools"]))
    for key in ("certificate", "binding"):
        if v[key] is not None:
            require(v[key]["keyset_sha256"] is not None)
            expected = ("trusted_router.async_settle_shadow_v2_certificate.verify_certificate" if key == "certificate"
                        else "trusted_router.async_settle_shadow_binding.verify_binding")
            require(v[key]["entry_point"] == expected)


VERIFIERS = Schema({"certificate": nullable(VERIFIER_USE), "binding": nullable(VERIFIER_USE),
    "retained_tools": array(VERIFIER_USE, 8)}, 12288, _verifiers)


def _report(v: Any) -> None:
    _sorted(v["days"])
    require(v["window"]["start_us"] < v["window"]["end_us"])
    require(all(len(t["day_counts"]) == len(v["days"]) for t in v["failure_counts"].values()))
    # Reserve the stated wrapper/remainder budget independently of the map.
    require(len(canonical(v)) - len(canonical(v["failure_counts"])) <= 16384, "evidence_size")


GATE_REPORT = replace(_a2({"gate": GATE, "completed_at_us": integer, "proof_manifest_sha256": digest,
    "prerequisite_ledger_root": digest, "days": array(day, 32, 1), "window": p1.INTERVAL,
    "rules": Schema(dict.fromkeys((f"G{i}" for i in range(1, 12)), literal("pass", "fail", "unevaluable", "not_required")), 512),
    "comparator": COMPARATOR, "admission": ADMISSION, "partition": PARTITION, "attempts": ATTEMPTS,
    "checkpoint": CHECKPOINT, "handoff": HANDOFF, "publisher": PUBLISHER, "fleet_peaks": PEAKS,
    "timing": TIMING, "deltas": DELTAS, "null_counts": NULL_COUNTS,
    **dict.fromkeys("first_seed_at_us first_reset_at_us first_gap_at_us clock_start_us elapsed_seconds".split(), nullable(integer)),
    "failure_counts": FAILURE_COUNTS,
    **dict.fromkeys("writers regions failures input_inventory reset_history".split(), DETAIL),
    "verifiers": VERIFIERS, "complete": boolean}, 65536, _report),
    optional_fields={"producer_registrations": lambda v: array(FILE_REF, 32)(v)})
WRITER_REPORT = _a2({"day": day, "boot": identity, "role": ROLE, "revision": revision,
    "region": nullable(name), "interval": p1.INTERVAL, "partition": PARTITION, "admission": ADMISSION,
    "comparator": COMPARATOR, "attempts": ATTEMPTS, "checkpoint": CHECKPOINT,
    **dict.fromkeys("first_seed_at_us first_reset_at_us first_gap_at_us".split(), nullable(integer))}, 8192,
    lambda v: require(v["role"] != "enclave" or all(v[k]["coverage"]["state"] == "not_required"
        and all(x is None for field, x in v[k].items() if field != "coverage") for k in ("admission", "comparator"))))
REGION_REPORT = _a2({"region": name, "writer_days": integer, "peaks": PEAKS, "timing": TIMING,
    "seed_at_us": nullable(integer)}, 16384)


def _path(v: Any) -> None:
    text(512)(v)
    require(not v.startswith("/") and "://" not in v and all(p != ".." for p in v.split("/")))


FILE_REF: Schema = Schema({"path": _path, "sha256": digest, "bytes": _bounded(262144)}, 768)
APPROVAL = Schema({"subject_sha256": digest, "reviewer": name, "reviewed_at_us": integer,
    "decision": literal("approved"), "record": FILE_REF}, 1024)
INPUT_LEAF = _a2({"keys": array(FILE_REF, 10000), "complete": boolean}, invariant=lambda v: _sorted([r["path"] for r in v["keys"]]))
INPUT_INVENTORY_ENTRY = Schema({"bundle": FILE_REF, "first_key": ascii_text(512, 1),
    "last_key": ascii_text(512, 1), "objects": integer}, 2048,
    lambda v: require(v["first_key"] <= v["last_key"]))
INDEPENDENT_INVENTORY = _a2({"day": day, "serving": FILE_REF, "deployment": FILE_REF, "approval": APPROVAL, "complete": boolean})
SHUTDOWN_ENTRY = Schema({"day": day, "boot": identity, "termination_applies": boolean,
    "reason": literal("utc_boundary", "shutdown", "opt_out", "hard_kill"), "interval_end_us": integer,
    "grace_us": nullable(integer), "original_close": nullable(FILE_REF), "close_result": nullable(FILE_REF), "measurement": FILE_REF}, 2048)
SHUTDOWN_INVENTORY = _a2({"writers": DETAIL, "measurements": FILE_REF, "approval": APPROVAL, "complete": boolean})
def _cohort(v: Any) -> None:
    _sorted(v["workspace_fingerprints"])
    _sorted(v["required_regions"])


COHORT_SEED_POLICY = _a2({"workspace_fingerprints": array(digest, 32, 1),
    "regional_seed_policy": literal("each_traffic_region", "fleet_only"), "required_regions": array(name, 4096),
    "primary_region": name, "maximum_publishers": integer, "configuration": FILE_REF, "approval": APPROVAL},
    invariant=_cohort)
DELEGATED_PAYLOAD = _a2({"entry_point": literal(*RETAINED_ENTRY_POINTS), "payload": FILE_REF,
    "dependencies": DETAIL, "source": FILE_REF})
LOAD_SEGMENT = Schema({"day": day, "boot": identity, "region": name, "start_us": integer, "end_us": integer,
    "role": ROLE, "contender": boolean, "workspace_count": _bounded(32), "peaks": PEAKS}, 8192,
    lambda v: require(v["start_us"] <= v["end_us"]))
EXPORT_OPERATION = Schema({"object": FILE_REF, "pass": integer, "at_us": integer,
    "operation": literal("get", "put", "delete"), "native_rpcs": integer, "bytes": integer, "elapsed_us": integer}, 2048)
LOAD_EVIDENCE = _a2({"interval": p1.INTERVAL, "segments": DETAIL, "operations": DETAIL, "limits": FILE_REF, "approval": APPROVAL})
BUDGET_ENVELOPE = _a2({"stage": literal("pre", "post"), "retained": DELEGATED_PAYLOAD, "output": FILE_REF,
    "pre_envelope_sha256": nullable(digest), "approval": APPROVAL, "v2_load": LOAD_EVIDENCE},
    invariant=lambda v: require((v["pre_envelope_sha256"] is None) == (v["stage"] == "pre")
        and v["retained"]["entry_point"] == ("pilot_enablement.fleet_budgets_pre" if v["stage"] == "pre" else "pilot_enablement.fleet_budgets")))
EXTERNAL_REQUIREMENT = Schema({"name": name, "stage": GATE, "router_revision": revision,
    "enclave_revision": revision, "evidence": FILE_REF, "approval": APPROVAL}, 2048)
EXTERNAL_PROOFS = _a2({"requirements": array(EXTERNAL_REQUIREMENT, 64), "pilot": nullable(DELEGATED_PAYLOAD)},
    invariant=lambda v: require(len({(r["stage"], r["name"]) for r in v["requirements"]}) == len(v["requirements"])))
RESET_ENTRY = Schema({"at_us": nullable(integer), "revision": revision, "reason": literal(*RESET_REASONS),
    "support": DETAIL, "resolved_at_us": nullable(integer), "fixed_revision": nullable(revision), "invalidated_at_us": nullable(integer)}, 2048,
    lambda v: require(v["at_us"] is not None or v["reason"] == "counter_mismatch"))
RESOLUTION_DAG = _a2({"retained": DELEGATED_PAYLOAD, "history": DETAIL, "approvals": array(APPROVAL, 128)},
    invariant=lambda v: require(v["retained"]["entry_point"] == "shadow_report.collect_rollbacks"))
PROOF_RESULT = _a2({"criterion": name, "router_revision": revision, "enclave_revision": revision,
    "completed_at_us": integer, "producer": name, "checks": array(Schema({"name": name,
        "result": literal("pass", "fail"), "evidence": FILE_REF}, 1024), 128), "sources": DETAIL})
REVIEW_RECORD = _a2({"subject_sha256": digest, "reviewer": name, "reviewed_at_us": integer,
    "decision": literal("approved", "rejected")}, 1024)
SHUTDOWN_MEASUREMENT = _a2({"day": day, "boot": identity,
    **dict.fromkeys("grace_us final_pass_us close_us result_us export_wait_us preparation_us".split(), integer),
    "outcome": literal("graceful", "hard_kill"), "source": FILE_REF}, 2048)


def _page(check: Check, count: int, limit: int) -> Schema:
    return _a2({"page_seq": sequence, "previous_sha256": nullable(digest), "entries": array(check, count, 1)}, limit,
        lambda v: require((v["previous_sha256"] is None) == (int(v["page_seq"]) == 1)))


FAILURE_PAGE = _page(GATE_FAILURE, 255, 262144)
WRITER_REPORT_PAGE = _page(WRITER_REPORT, 16, 132096)
REGION_REPORT_PAGE = _page(REGION_REPORT, 8, 132096)
ROOT_ENTRY_PAGE = _page(ROOT_ENTRY, 128, 132096)
ATTACHMENT_TYPES = (INPUT_INVENTORY_ENTRY, SHUTDOWN_ENTRY, LOAD_SEGMENT, EXPORT_OPERATION, RESET_ENTRY, FILE_REF)


def _attachment(v: Any) -> None:
    require(type(v) is dict)
    matches = [schema for schema in ATTACHMENT_TYPES if set(v) == set(schema.fields)]
    require(len(matches) == 1)
    matches[0](v)


def _attachment_page(v: Any) -> None:
    require((v["previous_sha256"] is None) == (int(v["page_seq"]) == 1))
    fields = set(v["entries"][0])
    require(all(set(entry) == fields for entry in v["entries"]))
    refs = fields == set(FILE_REF.fields)
    require(len(v["entries"]) <= (128 if refs else 64))
    require(len(canonical(v)) <= (99328 if refs else 132096), "evidence_size")


ATTACHMENT_PAGE = _a2({"page_seq": sequence, "previous_sha256": nullable(digest),
    "entries": array(_attachment, 128, 1)}, 132096, _attachment_page)
