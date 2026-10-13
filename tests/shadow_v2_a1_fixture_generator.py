"""Offline A1 release builder with independent literal expectations; no src imports."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

BASE = Path(__file__).parent / "fixtures/async_settlement/shadow_v2"
OUT = BASE.with_name("shadow_v2_a1")
CHANGED = frozenset("health health_claim counter closure controls proof_manifest prerequisite_ledger gate_outputs".split())
ADDED = frozenset("health_read_metadata health_basis publisher_slot_trace close_result handoff_accounting authorization_obligations completion_context".split())
ZERO = "0" * 64
NOW = 1791676800000000
NS = NOW * 1000
DAY = "2026-10-11"
TAG = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a1"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def baseline(name: str, index: int = 0) -> dict[str, Any]:
    return json.loads((BASE / (name + ".json")).read_bytes())["variants"][index]["object"]


def page(event: dict[str, Any], boot: str = "issuer", day: str = DAY) -> dict[str, Any]:
    return {"v": 2, "policy_version": "shadow-v2", "day": day, "boot": boot,
        "first_seq": event["seq"], "last_seq": event["seq"], "previous_page_sha256": None, "events": [event]}


def locator(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in ("day", "boot", "first_seq")} | {
        "event_seq": p["events"][0]["seq"], "event_sha256": digest(p["events"][0])}


def build() -> dict[str, bytes]:
    files: dict[str, list[dict[str, Any]]] = {name: [] for name in CHANGED | ADDED}

    def add(file: str, name: str, schema: str, obj: Any, expected: str = "accept", **extra: Any) -> None:
        files[file].append({"name": name, "target": schema, "object": copy.deepcopy(obj), "expected": expected, **extra})

    health = baseline("health")
    del health["observed_at_us"], health["publisher_heartbeat_us"]
    health.update(contract_revision="a1", observed_at_ns=NS, publisher_heartbeat_ns=NS, backlog_age_at_seconds=1791676800.125)
    claim = baseline("health_claim")
    claim["claim_at_ns"] = claim.pop("claim_at_us") * 1000
    claim["contract_revision"] = "a1"
    metadata = {"committed_at_ns": NS + 400000000, "read_at_ns": NS + 500000000}
    add("health", "empty", "HEALTH", health)
    add("health_claim", "server-slot", "HEALTH_CLAIM", claim)
    add("health_read_metadata", "metadata", "HEALTH_READ_METADATA", metadata)
    add("health_read_metadata", "int64-endpoint", "HEALTH_READ_METADATA", dict.fromkeys(metadata, (1 << 63) - 1))
    for field in metadata:
        add("health_read_metadata", "int64-over-" + field, "HEALTH_READ_METADATA", {**metadata, field: 1 << 63}, "reject")
    for file, schema, obj in (("health", "HEALTH", health), ("health_claim", "HEALTH_CLAIM", claim)):
        add(file, "baseline-not-a1", schema, baseline(file), "reject")
        add(file, "wrong-revision", schema, {**obj, "contract_revision": "a2"}, "reject")
    add("health", "heartbeat-different", "HEALTH", {**health, "publisher_heartbeat_ns": NS + 1}, "reject")
    for value, label, expected in ((0, "integer-zero", "accept"), (1.0, "float-one", "accept"),
        (-0.0, "negative-zero", "accept"), (1.7976931348623157e308, "max-finite", "accept"),
        (9007199254740992, "exact-large-int", "accept"), (9007199254740993, "lossy-large-int", "reject"),
        (True, "bool", "reject"), ("1.0", "string", "reject"), (-1.0, "negative", "reject")):
        add("health", "wall-" + label, "HEALTH", {**health, "backlog_age_at_seconds": value}, expected)
    for lexeme, value, expected in (
        ("1", 1, "accept"), ("1.0", 1.0, "accept"), ("-0.0", -0.0, "accept"),
        ("0.0001", 0.0001, "accept"), ("1e-05", 1e-5, "accept"),
        ("1000000000000000.0", 1e15, "accept"), ("1e+16", 1e16, "accept"),
        ("1e-07", 1e-7, "accept"), ("1e+20", 1e20, "accept"),
        ("5e-324", 5e-324, "accept"), ("1.7976931348623157e+308", 1.7976931348623157e308, "accept"),
        ("1267650600228229401496703205376", 2**100, "accept"),
        ("1.00", 1.0, "reject"), ("1E-07", 1e-7, "reject"), ("1e20", 1e20, "reject"),
        ("1e-7", 1e-7, "reject"), ("1e-007", 1e-7, "reject"), ("-0", 0, "reject"),
        ("01", 1, "reject"), ("+1", 1, "reject"), ("1e0", 1.0, "reject")):
        add("health", "spelling-" + lexeme, "HEALTH", {**health, "backlog_age_at_seconds": value},
            expected, number_lexeme=lexeme)
    for offset, expected in ((0, "accept"), (250000000, "accept"), (250000001, "reject"), (-1, "reject"), (2000000000, "reject")):
        add("health_claim", "slot-offset-" + str(offset), "HEALTH_CLAIM", {**claim, "claim_at_ns": NS + offset}, expected)
    readings = [
        ("fresh", 0, 400000000, 500000000, 0, 0, True),
        ("commit-after-read", 0, 400000001, 400000000, 0, 0, False),
        ("observation-before-slot", -1, 400000000, 500000000, 0, 0, False),
        ("observation-end", 250000000, 400000000, 500000000, 0, 0, True),
        ("observation-over", 250000001, 400000000, 500000000, 0, 0, False),
        ("commit-before-observation", 100000000, 99999999, 500000000, 0, 0, False),
        ("commit-end", 0, 750000000, 750000000, 0, 0, True),
        ("commit-over", 0, 750000001, 750000001, 0, 0, False),
        ("read-end", 0, 400000000, 500000000, 250000000, 0, True),
        ("read-over", 0, 400000000, 500000000, 250000001, 0, False),
        ("cache-end", 0, 400000000, 500000000, 250000000, 1250000000, True),
        ("cache-over", 0, 400000000, 500000000, 250000000, 1250000001, False),
        ("age-last-nanosecond", 0, 400000000, 3499999999, 250000000, 1250000000, True),
        ("age-exact-five", 0, 400000000, 5000000000, 0, 0, False),
        ("age-q-required", 0, 400000000, 4750000000, 250000000, 0, False),
        ("age-e-required", 0, 400000000, 4999999999, 0, 1, False),
        ("conservative-450", 0, 400000000, 4250000000, 250000000, 0, True),
        ("dd1-late-commit-fresh", 100000000, 501000000, 600000000, 0, 0, True),
        ("dd1-start-250-commit-900", 250000000, 900000000, 1000000000, 0, 0, False),
        ("dd1-late-or-lost-reply-fresh", 100000000, 400000000, 800000000, 0, 0, True),
        ("four-second-old-observation", -4000000000, 400000000, 800000000, 0, 0, False),
    ]
    for name, o, c, r, q, elapsed, expected in readings:
        obj = {"body": {**health, "observed_at_ns": NS + o, "publisher_heartbeat_ns": NS + o},
            "metadata": {"committed_at_ns": NS + c, "read_at_ns": NS + r}, "read_elapsed_ns": q, "since_reply_ns": elapsed}
        add("health_read_metadata", name, "health_usable", obj, "accept", usable=expected)
    for field, value in (("complete", False), ("sample_count", 10001)):
        obj = copy.deepcopy(next(v["object"] for v in files["health_read_metadata"] if v["name"] == "fresh"))
        obj["body"][field] = value
        if field == "sample_count":
            obj["body"]["backlog_count"] = value
        add("health_read_metadata", "unknown-" + field, "health_usable", obj, usable=False)
    basis = {**TAG, "reader_boot": "reader", "source_state": "installed", "body": health, **metadata,
        "read_elapsed_ns": 250000000, "since_reply_ns": 0}
    add("health_basis", "installed", "HEALTH_BASIS", basis)
    for state in ("empty", "read_failed"):
        add("health_basis", state, "HEALTH_BASIS", {**basis, "source_state": state, "body": None, "committed_at_ns": None, "read_at_ns": None})
    add("health_basis", "unusable-stale", "HEALTH_BASIS", {**basis, "source_state": "unusable", "read_at_ns": NS + 5000000000})
    add("health_basis", "installed-stale", "HEALTH_BASIS", {**basis, "read_at_ns": NS + 5000000000}, "reject")
    add("health_basis", "unusable-cannot-hide-usable", "HEALTH_BASIS", {**basis, "source_state": "unusable"}, "reject")
    for field in ("body", "committed_at_ns", "read_at_ns"):
        add("health_basis", "installed-null-" + field, "HEALTH_BASIS", {**basis, field: None}, "reject")
    batch = {**TAG, "day": DAY, "boot": "reader", "sequence": f"{1:020}", "previous_sha256": None, "bases": [basis]}
    add("health_basis", "batch", "HEALTH_BASIS_BATCH", batch)
    bases = sorted([{**basis, "since_reply_ns": i} for i in range(11)], key=digest)
    add("health_basis", "ten-bases", "HEALTH_BASIS_BATCH", {**batch, "bases": bases[:10]})
    add("health_basis", "eleven-bases", "HEALTH_BASIS_BATCH", {**batch, "bases": bases}, "reject")
    add("health_basis", "duplicate-basis", "HEALTH_BASIS_BATCH", {**batch, "bases": [basis, basis]}, "reject")
    counter = baseline("counter")
    counter["contract_revision"] = "a1"
    counter["publisher"].update(scheduled_slots=0, lost_claims=0, unknown_outcomes=0, late_cycles=0)
    add("counter", "closed-idle", "COUNTER", counter)
    publisher = counter["publisher"]
    add("counter", "publisher-idle", "PUBLISHER_OPERATIONS", publisher)
    add("counter", "publisher-final-idle", "validate_publisher_final", publisher)
    for field in ("scheduled_slots", "claim_attempts", "lost_claims", "publications", "failures", "unknown_outcomes", "late_cycles", "scans", "claims"):
        add("counter", "conservation-" + field, "validate_publisher_final", {**publisher, field: 1}, "reject")
    add("counter", "intermediate-outstanding", "COUNTER", {**counter, "closed": False, "publisher": {**publisher, "scheduled_slots": 1}})
    add("counter", "closed-outstanding", "COUNTER", {**counter, "publisher": {**publisher, "scheduled_slots": 1}}, "reject")
    trace = {**TAG, "day": DAY, "boot": "issuer", "slot": NS // 2000000000,
        "claim_start_us": NOW, "claim_at_ns": NS, "observed_at_ns": NS, "publisher_heartbeat_ns": NS,
        "body_sha256": digest(health), "ack_at_us": NOW + 400000, "cycle_us": 400000,
        "outcome": "published", "unknown_outcome": False, "late_cycle": False, "rpc_count": 6, "rpc_elapsed_us": 400000}
    add("publisher_slot_trace", "published", "PUBLISHER_SLOT_TRACE", trace)
    for name, changes in (
        ("missed", {**dict.fromkeys("claim_start_us claim_at_ns observed_at_ns publisher_heartbeat_ns body_sha256 ack_at_us cycle_us".split()), "outcome": "missed", "rpc_count": 0, "rpc_elapsed_us": 0}),
        ("lost-claim", {**dict.fromkeys("observed_at_ns publisher_heartbeat_ns body_sha256 ack_at_us".split()), "outcome": "lost_claim"}),
        ("unknown-reply", {"outcome": "failed", "unknown_outcome": True, "ack_at_us": None}),
        ("late-reply", {"outcome": "failed", "late_cycle": True, "cycle_us": 800000, "ack_at_us": NOW + 800000}),
        ("unknown-and-late", {"outcome": "failed", "late_cycle": True, "unknown_outcome": True, "cycle_us": 800000, "ack_at_us": None}),
        ("failed-claim", {**dict.fromkeys("claim_at_ns observed_at_ns publisher_heartbeat_ns body_sha256 ack_at_us".split()), "outcome": "failed", "unknown_outcome": True}),
    ):
        add("publisher_slot_trace", name, "PUBLISHER_SLOT_TRACE", {**trace, **changes})
    lost = next(v["object"] for v in files["publisher_slot_trace"] if v["name"] == "lost-claim")
    for offset, expected in ((-1, "reject"), (0, "accept"), (250000000, "accept"), (250000001, "reject")):
        add("publisher_slot_trace", "lost-slot-" + str(offset), "PUBLISHER_SLOT_TRACE",
            {**lost, "claim_at_ns": NS + offset}, expected)
    reconciled = copy.deepcopy(publisher)
    reconciled.update(scheduled_slots=1, claim_attempts=1, claims=1, scans=1, publications=1)
    reconciled["cycle_us"]["counts"][-1] = 1
    reconciled["rpc_us"]["counts"][0] = 5
    reconciled["rpc_us"]["counts"][-1] = 1
    evidence = dict(counter=reconciled, entries=[trace], day=DAY, boot="issuer",
                    first_slot=trace["slot"], last_slot=trace["slot"])
    add("publisher_slot_trace", "reconciled-histograms", "validate_publisher_traces", evidence)
    for field in ("cycle_us", "rpc_us"):
        bad = copy.deepcopy(evidence)
        bad["counter"][field]["counts"] = [0] * 9
        add("publisher_slot_trace", "missing-" + field, "validate_publisher_traces", bad, "reject")
    add("publisher_slot_trace", "late-is-not-published", "PUBLISHER_SLOT_TRACE", {**trace, "cycle_us": 500001}, "reject")
    add("publisher_slot_trace", "published-at-deadline", "PUBLISHER_SLOT_TRACE", {**trace, "cycle_us": 500000})
    add("publisher_slot_trace", "unknown-cannot-invent-ack", "PUBLISHER_SLOT_TRACE", {**trace, "outcome": "failed", "unknown_outcome": True}, "reject")
    trace_page = {**TAG, "day": DAY, "boot": "issuer", "first_slot": trace["slot"], "last_slot": trace["slot"], "previous_sha256": None, "entries": [trace]}
    add("publisher_slot_trace", "page", "PUBLISHER_TRACE_PAGE", trace_page)
    missed = next(v["object"] for v in files["publisher_slot_trace"] if v["name"] == "missed")
    entries = [{**missed, "slot": trace["slot"] + i} for i in range(61)]
    add("publisher_slot_trace", "sixty-slots", "PUBLISHER_TRACE_PAGE", {**trace_page, "entries": entries[:60], "last_slot": entries[59]["slot"]})
    add("publisher_slot_trace", "sixty-one-slots", "PUBLISHER_TRACE_PAGE", {**trace_page, "entries": entries, "last_slot": entries[60]["slot"]}, "reject")
    add("publisher_slot_trace", "page-hole", "PUBLISHER_TRACE_PAGE", {**trace_page, "last_slot": trace["slot"] + 1}, "reject")
    closure = {**baseline("closure"), "contract_revision": "a1", "authorization_obligations": "receipt_chain", "counter_sha256": digest(counter)}
    add("closure", "shutdown", "CLOSURE", closure)
    add("closure", "missing-obligation-marker", "CLOSURE", {k: v for k, v in closure.items() if k != "authorization_obligations"}, "reject")
    result = {**TAG, "day": DAY, "boot": "issuer", "router_revision": "a" * 40, "closure_sha256": digest(closure),
        "final_checkpoint_seq": 1, "attempt_started_at_us": NOW, "observed_at_us": NOW + 1000000,
        "elapsed_us": 1000000, "physical_commit_ack_at_us": NOW + 1000000, "outcome": "acknowledged",
        "flush_remaining_us": 4000000, "shutdown_remaining_us": 4000000, "handoff_attempt_limit": 1}
    add("close_result", "shutdown", "CLOSE_RESULT", result)
    for reason in ("shutdown", "utc_boundary", "opt_out"):
        cl = {**closure, "reason": reason}
        for terminating in (False, True):
            applies = terminating or reason == "shutdown"
            res = {**result, "closure_sha256": digest(cl), "shutdown_remaining_us": 4000000 if applies else None}
            add("close_result", reason + "-termination-" + str(terminating), "validate_close_result",
                {"result": res, "closure": cl, "termination_applies": terminating})
            add("close_result", reason + "-false-budget-" + str(terminating), "validate_close_result",
                {"result": {**res, "shutdown_remaining_us": None if applies else 4000000}, "closure": cl, "termination_applies": terminating}, "reject")
    for field, value in (("handoff_attempt_limit", 2), ("handoff_attempt_limit", True), ("outcome", "unknown"),
                         ("elapsed_us", 2000001), ("flush_remaining_us", 999999), ("shutdown_remaining_us", 999999),
                         ("physical_commit_ack_at_us", NOW)):
        add("close_result", "invalid-" + field + "-" + str(value), "CLOSE_RESULT", {**result, field: value}, "reject")
    add("close_result", "deadline-equality", "CLOSE_RESULT", {**result, "elapsed_us": 2000000, "flush_remaining_us": 2000000, "shutdown_remaining_us": 2000000})
    add("close_result", "wall-step-diagnostic", "CLOSE_RESULT", {**result, "attempt_started_at_us": NOW + 2000000})
    sealed = {**TAG, "day": DAY, "boot": "issuer", "closure_sha256": digest(closure),
        "physical_commit_ack_at_us": result["observed_at_us"], "close_result_sha256": digest(result)}
    add("closure", "sealed", "SEALED_CLOSURE", sealed)
    for landed in (False, True):
        handoff = {**TAG, "day": DAY, "boot": "issuer", "expected_handoffs": 1,
            "attempts_lower": int(landed), "attempts_upper": 1, "landed": landed, "reply_status": "not_proven",
            "failures_lower": 0, "failures_upper": 1, "unknown_reply_upper": 1, "result_sha256": digest(result) if landed else None}
        add("handoff_accounting", "landed-" + str(landed), "HANDOFF_ACCOUNTING", handoff)
        add("handoff_accounting", "false-zero-failures-" + str(landed), "HANDOFF_ACCOUNTING", {**handoff, "failures_upper": 0}, "reject")
    for file, schema in (("controls", "MANIFEST"), ("proof_manifest", "PROOF_MANIFEST"),
                         ("prerequisite_ledger", "PREREQUISITE_LEDGER"), ("gate_outputs", "GATE_OUTPUT")):
        obj = {**baseline(file), "contract_revision": "a1"}
        if schema in ("MANIFEST", "PROOF_MANIFEST"):
            obj.update(dict.fromkeys("close_result_root publisher_trace_root health_basis_root".split(), ZERO))
        if schema == "PROOF_MANIFEST":
            obj.update(handoff_accounting_root=ZERO, authorization_obligation_root=ZERO)
        add(file, "a1", schema, obj)
        add(file, "baseline-reject", schema, baseline(file), "reject")
    add("controls", "cap", "CAP", {**baseline("controls", 1), "contract_revision": "a1"})
    retention = {**baseline("controls", 2), "contract_revision": "a1"}
    retention["kinds"].append("async_settle_shadow_close_result_v2")
    add("controls", "retention", "RETENTION", retention)
    add("controls", "missing-close-kind", "RETENTION", {**retention, "kinds": retention["kinds"][:-1]}, "reject")
    for stream in ("close_result", "publisher_trace", "handoff_accounting", "health_basis", "authorization_obligation"):
        coverage = {**TAG, "day": DAY, "stream": stream, "page_seq": f"{1:020}", "previous_sha256": None,
            "entries": [{"key": "a/present", "sha256": ZERO, "bytes": 1, "state": "present"},
                        {"key": "b/missing", "sha256": None, "bytes": 0, "state": "missing"},
                        {"key": "c/conflict", "sha256": "1" * 64, "bytes": 2, "state": "conflict"}]}
        add("controls", "coverage-" + stream, "COVERAGE_PAGE", coverage)
        add("controls", "coverage-unsorted-" + stream, "COVERAGE_PAGE", {**coverage, "entries": list(reversed(coverage["entries"]))}, "reject")
    for label, keys, expected in (
        ("empty-middle", ["a", None, "z"], "accept"),
        ("several-empty", ["a", None, None, None, "z"], "accept"),
        ("duplicate-across-empty", ["z", None, "z"], "reject"),
        ("reversed-across-empty", ["z", None, "a"], "reject"),
        ("first-empty", [None, "a"], "accept"), ("last-empty", ["a", None], "accept"),
        ("all-empty", [None, None], "accept"), ("empty-set", [], "accept")):
        pages = []
        for key in keys:
            pages.append({**coverage, "page_seq": f"{len(pages) + 1:020}",
                "previous_sha256": digest(pages[-1]) if pages else None,
                "entries": [] if key is None else [{"key": key, "sha256": None, "bytes": 0, "state": "missing"}]})
        add("controls", "coverage-" + label, "coverage_root", pages, expected)
    for gap in "authorization_obligation_gap authorization_terminal_conflict authorization_horizon_gap health_timestamp_gap health_elapsed_gap health_slot_gap publisher_accounting_gap publisher_trace_gap freshness_budget_gap evidence_export_budget_gap close_handoff_budget_gap retry_original_gap close_result_identity_gap close_result_conflict handoff_coverage_gap".split():
        gate = {**baseline("gate_outputs"), "contract_revision": "a1", "gaps": [gap]}
        add("gate_outputs", gap, "GATE_OUTPUT", gate)
        add("gate_outputs", "false-pass-" + gap, "GATE_OUTPUT", {**gate, "verdict": "PASS"}, "reject")
    original = baseline("receipt_pages", 2)
    original["seq"] = f"{2:020}"
    complete = {**copy.deepcopy(original), "phase": "completion", "seq": f"{3:020}", "completion_of": original["seq"]}
    op, cp = page(original), page(complete)
    context = {"original_page": op, "original_locator": locator(op), "completion_page": cp, "completion_locator": locator(cp)}
    add("completion_context", "same-day", "validate_completion", context)
    cross = copy.deepcopy(context)
    cross_event = cross["completion_page"]["events"][0]
    cross_event.update(seq=f"{2:020}", event_at_us=NOW + 86400000000, receipt_at_us=NOW + 86400000000,
        retry_original={"day": DAY, "boot": "issuer", "event_seq": original["seq"]})
    cross["completion_page"] = page(cross_event, day="2026-10-12")
    cross["completion_locator"] = locator(cross["completion_page"])
    add("completion_context", "cross-day-equal-sequence", "validate_completion", cross)
    for name in ("missing-page", "cross-boot", "same-day-later-original", "cross-day-no-reference", "original-authorize", "original-receipt-after-completion", "reversed-days", "digest-mismatch"):
        ctx = copy.deepcopy(cross if name.startswith("cross-day") or name == "reversed-days" else context)
        if name == "missing-page":
            ctx["original_page"] = None
        elif name == "cross-boot":
            ctx["original_page"]["boot"] = "other"
            ctx["original_locator"] = locator(ctx["original_page"])
        elif name == "same-day-later-original":
            ce = ctx["completion_page"]["events"][0]
            ce["seq"] = f"{1:020}"
            ce["retry_original"] = {"day": DAY, "boot": "issuer", "event_seq": original["seq"]}
            ctx["completion_page"] = page(ce)
            ctx["completion_locator"] = locator(ctx["completion_page"])
        elif name == "cross-day-no-reference":
            ctx["completion_page"]["events"][0]["retry_original"] = None
            ctx["completion_locator"] = locator(ctx["completion_page"])
        elif name == "original-authorize":
            ctx["original_page"] = baseline("receipt_pages")
            ctx["original_locator"] = locator(ctx["original_page"])
            ce = ctx["completion_page"]["events"][0]
            ce["completion_of"] = ctx["original_page"]["events"][0]["seq"]
            ctx["completion_locator"] = locator(ctx["completion_page"])
        elif name == "original-receipt-after-completion":
            ctx["original_page"]["events"][0]["receipt_at_us"] += 1
            ctx["original_locator"] = locator(ctx["original_page"])
        elif name == "reversed-days":
            ctx["original_page"]["day"] = "2026-10-13"
            oe = ctx["original_page"]["events"][0]
            oe["event_at_us"] = oe["receipt_at_us"] = NOW + 2 * 86400000000
            ctx["original_locator"] = locator(ctx["original_page"])
        else:
            ctx["original_locator"]["event_sha256"] = ZERO
        add("completion_context", name, "validate_completion", ctx, "reject")
    fresh_page = baseline("receipt_pages")
    fresh = fresh_page["events"][0]
    replay = {**copy.deepcopy(original), "phase": "replay", "seq": f"{4:020}", "partition": "C",
        "original_receipt": None, "certificate_sha256": None, "proof_kind": "none", "comparison": None,
        "retry_original": {"day": DAY, "boot": "issuer", "event_seq": fresh["seq"]},
        "booking": {"outcome": "not_attempted", "reference": None, "winner": None}}
    replay_page = page(replay)
    entries = []
    for name, pg in (("fresh", fresh_page), ("replay", replay_page)):
        obligation = {**TAG, "original_receipt": locator(pg), "identity_sha256": digest(fresh["identity"]),
            "horizon_at_us": NOW + 172800000000, "status": "open", "terminal_reference": None, "gap": None}
        entries.append(obligation)
        add("authorization_obligations", name, "AUTHORIZATION_OBLIGATION", obligation)
        inputs = {"page": pg, "event_seq": pg["events"][0]["seq"]}
        if name == "replay":
            inputs.update(original_page=fresh_page, original_event_seq=fresh["seq"])
        add("authorization_obligations", "derive-" + name, "authorization_obligation", inputs, result=obligation)
    for status, terminal, gap, expected in (
        ("terminal_reconciled", locator(op), None, "accept"),
        ("blocked", None, "authorization_horizon_gap", "accept"),
        ("blocked", locator(op), "authorization_terminal_conflict", "accept"),
        ("open", locator(op), None, "reject"), ("open", None, "authorization_link_gap", "reject"),
        ("terminal_reconciled", None, None, "reject"), ("blocked", None, None, "reject")):
        add("authorization_obligations", status + "-" + str(terminal is not None) + "-" + str(gap), "AUTHORIZATION_OBLIGATION",
            {**entries[0], "status": status, "terminal_reference": terminal, "gap": gap}, expected)
    obligation_page = {**TAG, "day": DAY, "boot": "issuer", "page_seq": f"{1:020}", "previous_sha256": None, "entries": entries}
    add("authorization_obligations", "page", "AUTHORIZATION_OBLIGATION_PAGE", obligation_page)
    add("authorization_obligations", "page-unsorted", "AUTHORIZATION_OBLIGATION_PAGE", {**obligation_page, "entries": list(reversed(entries))}, "reject")
    for name, values in (("empty", []), ("unsorted", list(reversed(entries)))):
        ordered = sorted(values, key=lambda v: (v["original_receipt"]["day"], v["original_receipt"]["boot"], v["original_receipt"]["event_seq"]))
        projection = {**TAG, "day": DAY, "boot": "issuer", "closure_sha256": digest(closure),
            "open_obligation_count": len(values), "open_obligations_root": digest(ordered), "reconciliation_root": digest(ordered)}
        add("authorization_obligations", "projection-" + name, "OBLIGATION_PROJECTION", projection)
        add("authorization_obligations", "compute-" + name, "obligation_projection", {"closure": closure, "entries": values}, result=projection)
    # Independent scalar endpoint vectors for every new top-level integer field.
    # Invariants may narrow a field; numeric-only validation is exercised by tests.
    # Every complete schema receives literal/missing/extra and actual byte edges.
    limits = {"HEALTH": 4096, "HEALTH_CLAIM": 1024, "HEALTH_READ_METADATA": 128,
        "HEALTH_BASIS": 6144, "HEALTH_BASIS_BATCH": 65536, "PUBLISHER_OPERATIONS": 2048,
        "PUBLISHER_SLOT_TRACE": 1024, "PUBLISHER_TRACE_PAGE": 65536, "COUNTER": 65536,
        "CLOSURE": 4096, "CLOSE_RESULT": 4096, "SEALED_CLOSURE": 4096, "HANDOFF_ACCOUNTING": 1024,
        "MANIFEST": 262144, "PROOF_MANIFEST": 262144, "PREREQUISITE_LEDGER": 262144,
        "GATE_OUTPUT": 262144, "CAP": 4096, "RETENTION": 4096, "COVERAGE_PAGE": 262144,
        "AUTHORIZATION_OBLIGATION": 2048,
        "AUTHORIZATION_OBLIGATION_PAGE": 262144, "OBLIGATION_PROJECTION": 1024}
    seen = set()
    for file, variants in files.items():
        for vector in list(variants):
            schema = vector["target"]
            if schema not in limits or schema in seen or vector["expected"] != "accept":
                continue
            seen.add(schema)
            obj = vector["object"]
            add(file, schema + "-extra", schema, {**obj, "extra": None}, "reject")
            key = next(iter(obj))
            add(file, schema + "-missing", schema, {k: v for k, v in obj.items() if k != key}, "reject")
            if "contract_revision" in obj:
                add(file, schema + "-literal", schema, {**obj, "contract_revision": "A1"}, "reject")
            raw = canonical(obj)
            for over in (0, 1):
                # Compact transport padding specification avoids duplicating megabytes.
                add(file, schema + "-bytes-" + str(limits[schema] + over), schema, obj,
                    "reject" if over else "accept", transport_bytes=limits[schema] + over)
            assert len(raw) <= limits[schema]
    output = {p.name: p.read_bytes() for p in BASE.glob("*.json") if p.stem not in CHANGED | {"manifest"}}
    for file, variants in files.items():
        output[file + ".json"] = canonical({**TAG, "variants": variants}) + b"\n"
    manifest = {**TAG, "policy_sha256": hashlib.sha256(b"shadow-v2").hexdigest(),
        "baseline_manifest_sha256": "dc364e152a770b2904c41f13b3af54625da4fcb7652571129e7481e195b1d9e7",
        "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(),
            "variants": [v["name"] for v in json.loads(raw)["variants"]],
            "variant_sha256": {v["name"]: digest(v) for v in json.loads(raw)["variants"]}} for name, raw in sorted(output.items())}}
    output["manifest.json"] = canonical(manifest) + b"\n"
    return output


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, raw in build().items():
        (OUT / name).write_bytes(raw)
    print(hashlib.sha256((OUT / "manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
