"""Offline combined release builder: independent JSON/hash oracles, no src imports.

Run .venv/bin/python -m tests.shadow_v2_a2_fixture_generator. The A1 copy is
verified against its own manifest on every build, including variant hashes.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
BASE = ROOT / "tests/fixtures/async_settlement/shadow_v2"
A1 = BASE.with_name("shadow_v2_a1")
OUT = BASE.with_name("shadow_v2_a2")
FILES = tuple("gate_report gate_outputs_a2 gate_failures failure_pages writer_reports region_reports input_inventory root_preimages independent_inventory shutdown_inventory cohort_seed_policy budget_envelopes external_proofs resolution_dag verifier_ports delegated_payloads".split())
TAG = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a2"}
DAY = "2026-10-12"
REV = "a" * 40
MAX = (1 << 63) - 1
TIMINGS = "authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us evidence_write_us workspace_age_us health_age_us publisher_cycle_us publisher_slot_rpc_elapsed_us close_us handoff_us drain_us".split()
BINS = "0,+1,-1,+2-10,-2-10,+11-100,-11-100,+101-1000,-101-1000,+>1000,->1000".split(",")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def F(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def C(value: Any) -> str:
    return F(canonical(value))


def L(*parts: str) -> str:
    return "/".join(str(len(p)) + ":" + p for p in parts)


def rules() -> list[tuple[str, str, str, str]]:
    table = (ROOT / "docs/design/async-settle-shadow-v2.md").read_text().split("| GAP_NAME | Single producer |")[1].split("\n\n")[0]
    return [tuple(c.strip() for c in line.split("|")[1:-1]) for line in table.splitlines()[2:]]  # type: ignore[misc]


def subject(scope: str, **fields: Any) -> dict[str, Any]:
    return {"scope": scope, **dict.fromkeys("day role boot phase dimensions object at_us revision".split()), **fields}


def object_subject(kind: str, key: str) -> dict[str, Any]:
    return subject("object", object={"kind": kind, "key": key, "event_seq": None})


def ref(path: str = "evidence/source.json") -> dict[str, Any]:
    return {"path": path, "sha256": F(b"{}\n"), "bytes": 3}


def detail(entries: list[Any], pages: list[Any]) -> dict[str, Any]:
    return {"root": C(entries), "count": len(entries), "pages": len(pages), "tail_sha256": C(pages[-1]) if pages else None}


def pages(entries: list[Any], limit: int) -> list[Any]:
    result: list[Any] = []
    for start in range(0, len(entries), limit):
        result.append({**TAG, "page_seq": f"{len(result) + 1:020}", "previous_sha256": C(result[-1]) if result else None,
                       "entries": entries[start:start + limit]})
    return result


def build() -> dict[str, bytes]:
    files: dict[str, list[dict[str, Any]]] = {file: [] for file in FILES}
    def add(file: str, label: str, target: str, value: Any, expected: str = "accept", **extra: Any) -> None:
        files[file].append({"name": label, "target": target, "object": copy.deepcopy(value), "expected": expected, **extra})
    empty = detail([], [])
    approval = {"subject_sha256": F(b"{}\n"), "reviewer": "release-reviewer", "reviewed_at_us": 1,
        "decision": "approved", "record": ref()}
    interval = {"start_us": 1, "end_us": 1000001}
    peak_ints = "simultaneous_routers simultaneous_publishers configured_routers configured_publishers startup_operations_1s close_operations_1s export_operations_1s".split()
    peak_ratios = "reads_per_second pending_rows_per_second trust_rows_per_second health_rows_per_second claim_operations_per_second scan_operations_per_second publication_operations_per_second export_operations_per_second export_bytes_per_second configured_reads_per_second configured_pending_rows_per_second cpu_percent".split()
    peaks = dict.fromkeys(peak_ints + peak_ratios)
    stat = dict.fromkeys("count null_count min p50 p95 p99 max".split())
    ratio = {"state": "not_required", "numerator": None, "denominator": None}
    comparator = {**dict.fromkeys("eligible compared exact explained mismatch unevaluable".split()), "coverage": ratio}
    admission = {**dict.fromkeys("E Y N U".split()), "coverage": ratio}
    partition = dict.fromkeys("T A B C".split())
    attempts = dict.fromkeys("authorize_fresh authorize_replay terminal_original terminal_retry completion".split())
    checkpoint = dict.fromkeys("attempts failures recoveries unknown_commits unreconciled_commits irrecoverable_losses evidence_loss".split())
    verifier = {"entry_point": "trusted_router.async_settle_shadow_v2_certificate.verify_certificate",
        "source_sha256": F(b"certificate source"), "fixture_sha256": F(b"certificate fixtures"), "keyset_sha256": F(b"public descriptors"), "calls": 0}
    verifiers = {"certificate": verifier, "binding": {**verifier, "entry_point": "trusted_router.async_settle_shadow_binding.verify_binding"}, "retained_tools": []}
    report = {**TAG, "gate": "fleet-budgets-pre", "completed_at_us": 1000001,
        "proof_manifest_sha256": F(b"proof\n"), "prerequisite_ledger_root": C([]), "days": [DAY], "window": interval,
        "rules": {f"G{i}": "pass" if i in (1, 7) else "not_required" for i in range(1, 12)},
        "comparator": comparator, "admission": admission, "partition": partition, "attempts": attempts, "checkpoint": checkpoint,
        "handoff": dict.fromkeys("expected landed attempts_lower attempts_upper failures_lower failures_upper unknown_reply_upper".split()),
        "publisher": {**dict.fromkeys("scheduled_slots claim_attempts claims lost_claims scans publications failures unknown_outcomes late_cycles missed_slots".split()), "peaks": peaks},
        "fleet_peaks": peaks, "timing": dict.fromkeys(TIMINGS, stat),
        "deltas": {"count": None, "null_count": None, "min_micro": None, "max_micro": None, "bins": dict.fromkeys(BINS)},
        "null_counts": dict.fromkeys("eligibility comparison prediction booking timing delta".split()),
        **dict.fromkeys("first_seed_at_us first_reset_at_us first_gap_at_us clock_start_us elapsed_seconds".split()),
        "failure_counts": {row[0]: {"count": 0, "listed": 0, "complete": True, "day_counts": [0]} for row in rules()},
        **dict.fromkeys("writers regions failures input_inventory reset_history".split(), empty), "verifiers": verifiers, "complete": True}
    def output_arguments(rep: dict[str, Any], failure_pages: list[Any] | None = None) -> dict[str, Any]:
        gaps = sorted(k for k, v in rep["failure_counts"].items() if v["count"] > 0)
        outer = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a1",
            **{k: rep[k] for k in ("gate", "completed_at_us", "proof_manifest_sha256", "prerequisite_ledger_root", "clock_start_us")},
            "elapsed_seconds": rep["elapsed_seconds"] if rep["elapsed_seconds"] is not None else 0,
            "coverage_sha256": C(rep), "gaps": gaps, "verdict": "BLOCKED" if gaps else "PASS"}
        return {"output": outer, "report": rep, "failures": failure_pages or [], "writers": [], "regions": [], "inputs": [], "reset_history": [], "verifiers": rep["verifiers"]}
    # All G7 sweep components are projected before opt-in; CPU is measured.
    # Actual S/T/J/H observation series and publisher counters remain unavailable.
    known_peaks = {**dict.fromkeys(peak_ints, 0),
        **{k: {"numerator": 0, "denominator": 1} for k in peak_ratios}}
    report["fleet_peaks"] = copy.deepcopy(known_peaks)
    add("gate_report", "pre-enable-null-populations", "GATE_REPORT", report)
    add("gate_outputs_a2", "pre-enable-structural-pass", "validate_gate_output", output_arguments(report))
    add("verifier_ports", "certificate", "VERIFIER_USE", verifier)
    add("verifier_ports", "retained-tool-null-keyset", "VERIFIER_USE", {**verifier, "entry_point": "pilot_enablement.fleet_budgets_pre", "keyset_sha256": None})
    add("verifier_ports", "both-records", "VERIFIERS", verifiers)
    add("gate_report", "ratio-not-required", "RATIO", ratio)
    for state, n, d in (("evaluated", 1, 2), ("not_exercised", 0, 0), ("unevaluable", None, None)):
        add("gate_report", state, "RATIO", {"state": state, "numerator": n, "denominator": d})
    add("gate_report", "ratio-null-is-not-zero", "RATIO", {"state": "not_exercised", "numerator": None, "denominator": None}, "reject")
    add("gate_report", "reduced", "RATIONAL", {"numerator": MAX, "denominator": MAX - 1})
    add("gate_report", "unreduced", "RATIONAL", {"numerator": 2, "denominator": 2}, "reject")
    add("gate_report", "empty-stat", "STAT", {**stat, "count": 0, "null_count": 0})
    add("gate_report", "unknown-stat", "STAT", stat)
    add("gate_report", "empty-detail", "DETAIL", empty)
    add("gate_report", "peaks", "PEAKS", peaks)
    max_report = copy.deepcopy(report)
    max_report["days"] = [f"2026-10-{i:02}" for i in range(1, 32)] + ["2026-11-01"]
    max_report["failure_counts"] = {k: {"count": MAX, "listed": 16, "complete": False, "day_counts": [MAX] * 32} for k in report["failure_counts"]}
    max_report["fleet_peaks"] = {**dict.fromkeys(peak_ints, MAX), **dict.fromkeys(peak_ratios, {"numerator": MAX, "denominator": MAX - 1})}
    # Conjunctive schema maximum witness, not a claimed evaluated gate PASS.
    for field in ("completed_at_us", "first_seed_at_us", "first_reset_at_us", "first_gap_at_us", "clock_start_us", "elapsed_seconds"):
        max_report[field] = MAX
    max_report["window"] = {"start_us": 0, "end_us": MAX}
    for field in ("comparator", "admission", "partition", "attempts", "checkpoint", "handoff", "null_counts"):
        for key in max_report[field]:
            if key == "coverage":
                max_report[field][key] = {"state": "evaluated", "numerator": MAX, "denominator": MAX}
            else:
                max_report[field][key] = MAX
    max_report["publisher"] = {key: copy.deepcopy(max_report["fleet_peaks"]) if key == "peaks" else MAX for key in max_report["publisher"]}
    max_report["timing"] = {key: dict.fromkeys(stat, MAX) for key in TIMINGS}
    max_report["deltas"] = {"count": MAX, "null_count": MAX, "min_micro": -MAX, "max_micro": MAX, "bins": dict.fromkeys(BINS, MAX)}
    for field in ("writers", "regions", "failures", "input_inventory", "reset_history"):
        max_report[field] = {"root": "f" * 64, "count": MAX, "pages": MAX, "tail_sha256": "f" * 64}
    max_report["verifiers"] = {"certificate": {**verifier, "calls": MAX}, "binding": {**verifiers["binding"], "calls": MAX},
        "retained_tools": [{**verifier, "entry_point": "tool" + str(i) + "x" * 123, "keyset_sha256": None, "calls": MAX} for i in range(8)]}
    add("gate_report", "maximum-tallies-fit", "GATE_REPORT", max_report,
        canonical_bytes=len(canonical(max_report)), count_map_bytes=len(canonical(max_report["failure_counts"])))
    assert len(canonical(max_report)) <= 65536
    w = {**TAG, "day": DAY, "boot": "w1", "role": "router", "revision": REV, "region": None, "interval": interval,
        "partition": partition, "admission": admission, "comparator": comparator, "attempts": attempts, "checkpoint": checkpoint,
        **dict.fromkeys("first_seed_at_us first_reset_at_us first_gap_at_us".split())}
    add("writer_reports", "unknown-region", "WRITER_REPORT", w)
    add("writer_reports", "enclave", "WRITER_REPORT", {**w, "role": "enclave", "region": "us-central1"})
    add("writer_reports", "page", "WRITER_REPORT_PAGE", pages([w], 16)[0])
    region = {**TAG, "region": "us-central1", "writer_days": 1, "peaks": peaks, "timing": dict.fromkeys(TIMINGS, stat), "seed_at_us": None}
    add("region_reports", "region", "REGION_REPORT", region)
    add("region_reports", "page", "REGION_REPORT_PAGE", pages([region], 8)[0])
    reset = {"at_us": 1, "revision": REV, "reason": "hash", "support": empty,
        "resolved_at_us": None, "fixed_revision": None, "invalidated_at_us": None}
    constructors = {
        "N": subject("run"), "D": subject("day", day=DAY), "W": subject("writer", day=DAY, role="router", boot="w1"),
        "B": subject("bucket", day=DAY, role="router", boot="w1", phase="settle", dimensions={"adapter": "openai", "route_type": "responses", "streamed": False}),
        "O": object_subject("async_settle_shadow_close_result_v2", DAY + "/w1"),
        "I": subject("object", day=DAY, role="router", boot="w1", object={"kind": "a2_invalid_receipt", "key": "34:2026-10-12/w1/00000000000000000001/5:event/1:0", "event_seq": None}),
        "V": object_subject("a2_region", L("region", "us-central1")),
        "K": object_subject("a2_budget", L("pre", "fleet", "cap")),
        "A": object_subject("a2_budget", L("pre", "approval")),
        "P": object_subject("a2_proof", L("pre-flip", "pilot")),
    }
    # Constructor event uses frozen baseline valid terminal bytes and independent coordinates.
    event = json.loads((BASE / "receipt_pages.json").read_bytes())["variants"][2]["object"]
    event["event_at_us"] += 86400000000
    event["receipt_at_us"] += 86400000000
    constructors["E"] = subject("object", day=DAY, role="router", boot="w1", phase=event["phase"], dimensions=event["dimensions"],
        object={"kind": "async_settle_shadow_receipts_v2", "key": DAY + "/w1/00000000000000000001", "event_seq": event["seq"]})
    import re
    for gap, rule, population, route in rules():
        code = re.findall(r"\b([NDWBOEIVKAQP])\b", population)[0]
        if code == "Q":
            reason = "hash" if gap == "missing_support" else gap
            sub = subject("reset", at_us=1, revision=REV,
                object={"kind": "a2_reset", "key": L("1", REV, reason, empty["root"]), "event_seq": None})
        else:
            sub = copy.deepcopy(constructors[code])
            if code == "E" and rule == "G3":
                sub["phase"] = "authorize"
        add("gate_failures", "constructor-" + gap, "GATE_FAILURE", {**TAG, "gap": gap, "subject": sub, "count": 1},
            constructor=code, subject_bytes=canonical(sub).decode(), producer=rule.split()[0], population=population, unevaluable=route)
    for code, sub in constructors.items():
        add("gate_failures", "subject-" + code, "FAILURE_SUBJECT", sub, subject_bytes=canonical(sub).decode(), event=event if code == "E" else None)
    # Eight worked identity vectors: bases, slots, accounting, invalid child,
    # budget fleet/region/approval and missing/conflicting expected row.
    for label, gap, sub in (("two-health-bases", "health_timestamp_gap", constructors["W"]),
        ("three-slots-two-batches", "publisher_trace_gap", constructors["W"]),
        ("same-slots-accounting", "publisher_accounting_gap", constructors["W"]),
        ("invalid-first-event-claims-99", "evidence_schema_gap", constructors["I"]),
        ("fleet-cap", "simultaneous_capacity_gap", constructors["K"]),
        ("region-cap", "simultaneous_capacity_gap", object_subject("a2_budget", L("pre", "region", "us-central1", "cap"))),
        ("approval", "pre_enable_budget", constructors["A"]),
        ("expected-row-conflict", "close_result_conflict", constructors["O"])):
        add("gate_failures", label, "GATE_FAILURE", {**TAG, "gap": gap, "subject": sub, "count": 1},
            subject_bytes=canonical(sub).decode(), tally={"count": 1, "listed": 1, "complete": True, "day_counts": [1]})
    for total in (15, 16, 17, 130):
        failures = [{**TAG, "gap": "writer_closure_gap", "subject": subject("writer", day=DAY, role="router", boot=f"w{i:04}"), "count": 1} for i in range(min(total, 16))]
        fp = pages(failures, 255)
        blocked = copy.deepcopy(report)
        blocked["failure_counts"]["writer_closure_gap"] = {"count": total, "listed": min(total, 16), "complete": total <= 16, "day_counts": [total]}
        blocked["rules"]["G8"] = "fail"
        blocked["failures"] = detail(failures, fp)
        add("gate_outputs_a2", "closures-" + str(total), "validate_gate_output", output_arguments(blocked, fp))
    one = files["gate_outputs_a2"][-1]["object"]
    add("failure_pages", "listed-prefix", "FAILURE_PAGE", one["failures"][0])
    add("failure_pages", "prefix-detail", "validate_detail", {"detail": one["report"]["failures"], "pages": one["failures"], "stream": "failures"})
    for label in ("wrong-report-hash", "gap-without-count", "count-without-gap", "pass-nonzero", "missing-port", "null-as-zero", "elapsed-zero-real", "wrong-subject", "dropped-page", "reordered-list"):
        args = copy.deepcopy(one if label in ("count-without-gap", "pass-nonzero", "wrong-subject", "dropped-page", "reordered-list") else output_arguments(report))
        if label == "wrong-report-hash":
            args["output"]["coverage_sha256"] = "f" * 64
        elif label == "gap-without-count":
            args["output"].update(gaps=["artifact_gap"], verdict="BLOCKED")
        elif label == "count-without-gap":
            args["output"]["gaps"] = []
        elif label == "pass-nonzero":
            args["output"]["verdict"] = "PASS"
        elif label == "missing-port":
            args["report"]["verifiers"]["binding"] = None
        elif label == "null-as-zero":
            args["report"]["null_counts"]["timing"] = 0
        elif label == "elapsed-zero-real":
            args["report"]["elapsed_seconds"] = 0
        elif label == "wrong-subject":
            args["failures"][0]["entries"][0]["subject"]["boot"] = "wrong"
        elif label == "dropped-page":
            args["failures"] = []
        else:
            args["failures"][0]["entries"].reverse()
        if label != "wrong-report-hash":
            args["output"]["coverage_sha256"] = C(args["report"])
        add("gate_outputs_a2", label, "validate_gate_output", args, "reject")
    # Independently pack a reachable four-page listing, then damage the graph.
    many = []
    for variant in files["gate_failures"]:
        if not variant["name"].startswith("constructor-"):
            continue
        base_failure = variant["object"]
        sub = base_failure["subject"]
        for i in range(16 if sub["scope"] in ("writer", "bucket", "reset") or
                       sub["scope"] == "object" and not sub["object"]["kind"].startswith("a2_") else 1):
            changed = copy.deepcopy(base_failure)
            target = changed["subject"]
            if sub["scope"] in ("writer", "bucket"):
                target["boot"] = f"w{i:02}"
            elif sub["scope"] == "reset":
                reason = "hash" if changed["gap"] == "missing_support" else changed["gap"]
                target["at_us"] = i
                target["object"]["key"] = L(str(i), REV, reason, empty["root"])
            elif sub["scope"] == "object" and sub["object"]["event_seq"] is not None:
                target["object"]["event_seq"] = f"{i + 1:020}"
            elif sub["scope"] == "object" and not sub["object"]["kind"].startswith("a2_"):
                target["object"]["key"] = f"{DAY}/w{i:02}"
            many.append(changed)
    many.sort(key=lambda failure: canonical([failure["gap"], failure["subject"]]))
    many_pages = pages(many, 255)
    assert len(many_pages) == 4
    many_report = copy.deepcopy(report)
    many_report["rules"] = dict.fromkeys(many_report["rules"], "fail")
    many_report["failures"] = detail(many, many_pages)
    for gap in many_report["failure_counts"]:
        n = sum(item["gap"] == gap for item in many)
        many_report["failure_counts"][gap] = {"count": n, "listed": n, "complete": True, "day_counts": [n]}
    many_args = output_arguments(many_report, many_pages)
    add("gate_outputs_a2", "four-page-output", "validate_gate_output", many_args)
    add("failure_pages", "four-page-stream", "validate_detail", {"detail": detail(many, many_pages), "pages": many_pages, "stream": "failures"})
    for label, damaged in (("dropped-failure-page", many_pages[:-1]), ("reordered-failure-pages", list(reversed(many_pages)))):
        bad = copy.deepcopy(many_args)
        bad["failures"] = damaged
        add("gate_outputs_a2", label, "validate_gate_output", bad, "reject")
    seventeenth = copy.deepcopy(one)
    last_failure = copy.deepcopy(seventeenth["failures"][0]["entries"][-1])
    last_failure["subject"]["boot"] = "w9999"
    seventeenth["failures"][0]["entries"].append(last_failure)
    add("gate_outputs_a2", "seventeen-listed", "validate_gate_output", seventeenth, "reject")
    # Literal S/T/J observations: no evaluator is introduced into P1-A2.
    metric = {"S": 1, "T": 2, "references": ["original", "retry", "completion"], "booked_micro": 100,
        "delta": -3, "eligibility_observed": None, "terminal_comparisons": [None, "explained-by-catalog-change"],
        "terminal_predictions": ["yes", "unknown"], "slots": [{"rpc_count": 3, "rpc_elapsed_us": 150}, {"rpc_count": 0, "rpc_elapsed_us": 0}],
        "deltas": {"count": 1, "null_count": 0, "min_micro": -3, "max_micro": -3, "bins": {b: int(b == "-2-10") for b in BINS}},
        "null_counts": {"eligibility": 1, "comparison": 1, "prediction": 0, "booking": 0, "delta": 0},
        "evidence_write_us": {**stat, "count": 0, "null_count": 1},
        "publisher_slot_rpc_elapsed_us": {"count": 2, "null_count": 0, "min": 0, "p50": 0, "p95": 150, "p99": 150, "max": 150}}
    add("gate_report", "A2-R3-populations", "metric_population", metric)
    add("gate_report", "A2-R3-deltas", "DELTAS", metric["deltas"])
    add("gate_report", "A2-R3-slot-stat", "STAT", metric["publisher_slot_rpc_elapsed_us"])
    # All conflict orders, including duplicate exports and a separate missing key.
    key = "10:2026-10-12/2:w1"
    for n in (2, 3):
        bodies = [b"{}", b"{}\n", b" {}"][:n]
        hashes = sorted(F(b) for b in bodies)
        entry = {"key": key, "state": "conflict", "sha256": C(hashes), "bytes": sum(map(len, bodies))}
        for i, permutation in enumerate(itertools.permutations(bodies)):
            for duplicate in (False, True):
                retained = [*permutation, permutation[0]] if duplicate else list(permutation)
                add("root_preimages", f"conflict-{n}-{i}-{duplicate}", "root_entry", {"key": key, "bodies_hex": [b.hex() for b in retained]},
                    result=entry, preimage=canonical(hashes).decode(), root=C([entry]), candidates=n)
        add("root_preimages", f"conflict-entry-{n}", "ROOT_ENTRY", entry)
    conflict = {"bytes": 5, "key": key, "sha256": "0b896b4ad8e80334b52d5f5de239a1bee9f008464e9c5f019974159aea183a0c", "state": "conflict"}
    assert C([conflict]) == "129591526645105fa353f07120da3ca4905ce6ec7ac74914a96604ec9fb1e2f1"
    missing = {"key": L(DAY, "w2"), "sha256": None, "bytes": 0, "state": "missing"}
    add("root_preimages", "conflict-beside-missing", "entry_root", {"entries": [missing, conflict], "stream": "counter_root"}, result=C([conflict, missing]))
    add("root_preimages", "root-page", "ROOT_ENTRY_PAGE", pages([conflict, missing], 128)[0])
    add("root_preimages", "LF-sensitive", "file_digest", {"hex": b"{}\n".hex()}, result="ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356")
    add("root_preimages", "undefined-stream", "entry_root", {"entries": [], "stream": "invented"}, "reject")
    root_keys = {
        "counter_root": (DAY, "w1"), "closure_root": (DAY, "w1"),
        "receipts_root": (DAY, "w1", "00000000000000000001"),
        "prerequisite_ledger_root": ("router", "w1", REV, "us-central1", "10"),
        "ledger_root": ("router", "w1", REV, "us-central1", "10"),
        "publisher_slot_trace_sha256": (DAY, "w1", "10"),
    }
    for stream, components in root_keys.items():
        entry = {**conflict, "key": L(*components)}
        add("root_preimages", "literal-" + stream, "entry_root", {"entries": [entry], "stream": stream},
            result=C([entry]), preimage=canonical([entry]).decode())
    a1_controls = json.loads((A1 / "controls.json").read_bytes())["variants"]
    for stream in ("close_result", "publisher_trace", "health_basis", "handoff_accounting", "authorization_obligation"):
        page = next(v["object"] for v in a1_controls if v["name"] == "coverage-" + stream)
        frozen_root = C([C(page)])
        add("root_preimages", "frozen-page-root-" + stream, "coverage_root", [page], result=frozen_root,
            preimage=canonical([C(page)]).decode())
        entry = {"day": page["day"], "root": frozen_root, "count": len(page["entries"])}
        add("root_preimages", "cross-day-" + stream, "entry_root", {"entries": [entry], "stream": stream + "_root"},
            result=C([entry]), preimage=canonical([entry]).decode())
    leaf = {**TAG, "keys": [ref()], "complete": True}
    inv = {"bundle": ref("inventory/leaf.json"), "first_key": "a", "last_key": "z", "objects": 1}
    add("input_inventory", "leaf", "INPUT_LEAF", leaf)
    add("input_inventory", "entry", "INPUT_INVENTORY_ENTRY", inv)
    add("input_inventory", "zero-objects", "INPUT_INVENTORY_ENTRY", {**inv, "objects": 0})
    add("input_inventory", "page", "ATTACHMENT_PAGE", pages([inv], 64)[0])
    independent = {**TAG, "day": DAY, "serving": ref(), "deployment": ref(), "approval": approval, "complete": True}
    add("independent_inventory", "inventory", "INDEPENDENT_INVENTORY", independent)
    shutdown_entry = {"day": DAY, "boot": "w1", "termination_applies": True, "reason": "shutdown", "interval_end_us": 1,
        "grace_us": 4000000, "original_close": None, "close_result": None, "measurement": ref()}
    shutdown = {**TAG, "writers": empty, "measurements": ref(), "approval": approval, "complete": True}
    measurement = {**TAG, "day": DAY, "boot": "w1", "grace_us": 4000000, "final_pass_us": 4000000,
        "close_us": 2000000, "result_us": 1000000, "export_wait_us": 500000, "preparation_us": 500000, "outcome": "graceful", "source": ref()}
    add("shutdown_inventory", "inventory", "SHUTDOWN_INVENTORY", shutdown)
    add("shutdown_inventory", "missing-close", "SHUTDOWN_ENTRY", shutdown_entry)
    add("shutdown_inventory", "measurement", "SHUTDOWN_MEASUREMENT", measurement)
    policy = {**TAG, "workspace_fingerprints": [F(b"workspace")], "regional_seed_policy": "each_traffic_region",
        "required_regions": ["us-central1"], "primary_region": "us-central1", "maximum_publishers": 1, "configuration": ref(), "approval": approval}
    add("cohort_seed_policy", "policy", "COHORT_SEED_POLICY", policy)
    delegated = {**TAG, "entry_point": "pilot_enablement.fleet_budgets_pre", "payload": ref(), "dependencies": empty, "source": ref()}
    for entry_point in ["pilot_enablement." + n for n in "fleet_budgets_pre fleet_budgets shadow_serving pre_flip post_flip counter_coverage_report roster_coverage".split()] + ["shadow_report.collect_rollbacks"]:
        add("delegated_payloads", entry_point, "DELEGATED_PAYLOAD", {**delegated, "entry_point": entry_point})
    load = {**TAG, "interval": interval, "segments": empty, "operations": empty, "limits": ref(), "approval": approval}
    pre = {**TAG, "stage": "pre", "retained": delegated, "output": ref(), "pre_envelope_sha256": None, "approval": approval, "v2_load": load}
    post = {**pre, "stage": "post", "pre_envelope_sha256": C(pre), "retained": {**delegated, "entry_point": "pilot_enablement.fleet_budgets"}}
    add("budget_envelopes", "pre", "BUDGET_ENVELOPE", pre)
    add("budget_envelopes", "post", "BUDGET_ENVELOPE", post)
    add("budget_envelopes", "load", "LOAD_EVIDENCE", load)
    add("budget_envelopes", "segment", "LOAD_SEGMENT", {"day": DAY, "boot": "w1", "region": "us-central1", **interval, "role": "router", "contender": True, "workspace_count": 1, "peaks": peaks})
    add("budget_envelopes", "operation", "EXPORT_OPERATION", {"object": ref(), "pass": 0, "at_us": 1, "operation": "get", "native_rpcs": 1, "bytes": 3, "elapsed_us": 1})
    requirement = {"name": "criterion", "stage": "pre-flip", "router_revision": REV, "enclave_revision": REV, "evidence": ref(), "approval": approval}
    external = {**TAG, "requirements": [requirement], "pilot": None}
    add("external_proofs", "requirements", "EXTERNAL_PROOFS", external)
    add("external_proofs", "requirement", "EXTERNAL_REQUIREMENT", requirement)
    add("external_proofs", "result", "PROOF_RESULT", {**TAG, "criterion": "criterion", "router_revision": REV,
        "enclave_revision": REV, "completed_at_us": 1, "producer": "ci", "checks": [{"name": "check", "result": "pass", "evidence": ref()}], "sources": empty})
    add("external_proofs", "review", "REVIEW_RECORD", {**TAG, **{k: approval[k] for k in ("subject_sha256", "reviewer", "reviewed_at_us", "decision")}})
    add("external_proofs", "approval", "APPROVAL", approval)
    add("external_proofs", "file", "FILE_REF", ref())
    dag = {**TAG, "retained": {**delegated, "entry_point": "shadow_report.collect_rollbacks"}, "history": empty, "approvals": [approval]}
    add("resolution_dag", "dag", "RESOLUTION_DAG", dag)
    add("resolution_dag", "reset", "RESET_ENTRY", reset)
    add("resolution_dag", "unknown-reset", "RESET_ENTRY", {**reset, "at_us": None, "reason": "counter_mismatch"})
    # Explicit preimage field inventory with independent C/F/R calculations.
    manifest = json.loads((A1 / "controls.json").read_bytes())["variants"][0]["object"]
    proof = json.loads((A1 / "proof_manifest.json").read_bytes())["variants"][0]["object"]
    manifest.update(day=DAY, proof_manifest_sha256=None)
    proof["rosters_by_day"] = {DAY: {"router": manifest["router_writers"], "enclave": manifest["enclave_writers"]}}
    proof["instance_regions_by_day"] = {DAY: {boot: writer["region"] for boot, writer in manifest["router_writers"].items()}}
    artifacts: dict[str, bytes] = {"evidence/source.json": b"{}\n"}
    object_fields = {"manifest.independent_inventory_sha256": independent, "proof.shutdown_inventory_sha256": shutdown,
        "proof.cohort_seed_policy_sha256": policy, "proof.pre_budget_sha256": pre, "proof.post_budget_sha256": post,
        "proof.external_proofs_sha256": external, "proof.resolution_dag_sha256": dag}
    file_fields = "manifest.config_sha256 manifest.fixture_sha256 proof.evaluator_fixture_sha256 proof.binding_fixture_sha256 proof.wire_fixture_sha256".split()
    for field, obj in object_fields.items():
        artifacts[field] = canonical(obj) + b"\n"
        owner, key_field = field.split(".")
        (manifest if owner == "manifest" else proof)[key_field] = C(obj)
    for field in file_fields:
        artifacts[field] = b"{}\n"
        owner, key_field = field.split(".")
        (manifest if owner == "manifest" else proof)[key_field] = F(artifacts[field])
    root_fields = "manifest.counter_root manifest.closure_root manifest.receipts_root manifest.prerequisite_ledger_root proof.ledger_root proof.publisher_slot_trace_sha256".split()
    coverage_fields = ["manifest." + n + "_root" for n in ("close_result", "publisher_trace", "health_basis")]
    cross_fields = ["proof." + n + "_root" for n in ("close_result", "publisher_trace", "health_basis", "handoff_accounting", "authorization_obligation")]
    roots = dict.fromkeys(root_fields + coverage_fields + cross_fields, [])
    for field in roots:
        owner, key_field = field.split(".")
        (manifest if owner == "manifest" else proof)[key_field] = C([])
    artifacts["proof_manifest"] = canonical(proof) + b"\n"
    manifest["proof_manifest_sha256"] = F(artifacts["proof_manifest"])
    args = {"manifest": manifest, "proof": proof, "artifacts_hex": {k: v.hex() for k, v in artifacts.items()}, "roots": roots}
    add("root_preimages", "all-defined-preimages-empty", "validate_preimages", args)
    bad = copy.deepcopy(args)
    bad["roots"]["undefined_digest"] = "0" * 64
    add("root_preimages", "undefined-preimage", "validate_preimages", bad, "reject")
    for field in [*file_fields, *object_fields, *roots]:
        bad = copy.deepcopy(args)
        owner, key_field = field.split(".")
        bad[owner][key_field] = "0" * 64
        add("root_preimages", "substitution-" + field, "validate_preimages", bad, "reject")
    # Round-2 relationship witnesses: expected verdicts and all commitments are
    # independent stdlib calculations, never calls into the contract under test.
    for component in peak_ints + peak_ratios:
        bad_report = copy.deepcopy(report)
        bad_report["fleet_peaks"][component] = None
        add("gate_outputs_a2", "missing-G7-" + component, "validate_gate_output",
            output_arguments(bad_report), "reject")
    blocked = copy.deepcopy(one)
    blocked["report"]["rules"]["G5"] = "pass"
    blocked["output"]["coverage_sha256"] = C(blocked["report"])
    add("gate_outputs_a2", "blocked-G5-unavailable", "validate_gate_output", blocked, "reject")

    actual = copy.deepcopy(report)
    actual["gate"] = "fleet-budgets"
    for rule in ("G2", "G5", "G6", "G8"):
        actual["rules"][rule] = "pass"
    for field in ("admission", "comparator"):
        actual[field] = {k: 0 for k in actual[field] if k != "coverage"} | {
            "coverage": {"state": "not_exercised", "numerator": 0, "denominator": 0}}
    for field in ("partition", "checkpoint", "handoff", "null_counts"):
        actual[field] = dict.fromkeys(actual[field], 0)
    actual["publisher"] = {k: 0 for k in actual["publisher"] if k != "peaks"} | {"peaks": copy.deepcopy(known_peaks)}
    actual["timing"] = {k: {**stat, "count": 0, "null_count": 0} for k in TIMINGS}
    actual["deltas"] = {"count": 0, "null_count": 0, "min_micro": None, "max_micro": None, "bins": dict.fromkeys(BINS, 0)}
    add("gate_outputs_a2", "closed-actual-empty", "validate_gate_output", output_arguments(actual))
    def regional_arguments(rep: dict[str, Any], regs: list[dict[str, Any]]) -> dict[str, Any]:
        region_pages = pages(regs, 8)
        rep["regions"] = detail(regs, region_pages)
        result = output_arguments(rep)
        result["regions"] = region_pages
        return result
    for field in ("count", "null_count"):
        bad_report = copy.deepcopy(actual)
        reg = {**copy.deepcopy(region), "peaks": copy.deepcopy(known_peaks), "timing": copy.deepcopy(actual["timing"])}
        if field == "count":
            for k in ("close_us", "handoff_us"):
                bad_report["timing"][k] = {"count": 1, "null_count": 0, **dict.fromkeys(("min", "p50", "p95", "p99", "max"), 1)}
                reg["timing"][k] = {**bad_report["timing"][k], "count": 2}
        else:
            # Regional nullable S population contradicts the known empty fleet.
            for k in TIMINGS[:8]:
                reg["timing"][k]["null_count"] = 1
        add("gate_outputs_a2", "regional-contradiction-" + field, "validate_gate_output",
            regional_arguments(bad_report, [reg]), "reject")
    bad_report = copy.deepcopy(actual)
    bad_report["timing"]["drain_us"] = {"count": 2, "null_count": 0, **dict.fromkeys(("min", "p50", "p95", "p99", "max"), 1)}
    for k in ("close_us", "handoff_us"):
        bad_report["timing"][k] = {**bad_report["timing"]["drain_us"], "count": 1}
    reg = {**copy.deepcopy(region), "peaks": copy.deepcopy(known_peaks), "timing": copy.deepcopy(bad_report["timing"])}
    add("gate_outputs_a2", "Hterm-exceeds-H", "validate_gate_output", regional_arguments(bad_report, [reg]), "reject")
    fallback = {**TAG, "gap": "writer_closure_gap", "subject": subject("run"), "count": 1}
    bad_report = copy.deepcopy(actual)
    fallback_pages = pages([fallback], 255)
    bad_report["failures"] = detail([fallback], fallback_pages)
    bad_report["failure_counts"]["writer_closure_gap"] = {"count": 1, "listed": 1, "complete": True, "day_counts": [1]}
    bad_report["rules"]["G8"] = "unevaluable"
    add("gate_outputs_a2", "complete-listed-fallback-N", "validate_gate_output",
        output_arguments(bad_report, fallback_pages), "reject")
    for label in ("inventory", "counter_root", "closure_root", "receipts_root"):
        bad = copy.deepcopy(args)
        if label == "inventory":
            decoded = {**independent, "day": "2026-10-11"}
            bad["artifacts_hex"]["manifest.independent_inventory_sha256"] = (canonical(decoded) + b"\n").hex()
            bad["manifest"]["independent_inventory_sha256"] = C(decoded)
        else:
            source_file, variant_name = {"counter_root": ("counter", "closed-idle"),
                "closure_root": ("closure", "closed-idle"), "receipts_root": ("receipt_pages", "one-authorize")} [label]
            variants = json.loads((A1 / (source_file + ".json")).read_bytes())["variants"]
            # Select frozen accepted row; the test separately proves A1 validity.
            row = copy.deepcopy(next((v["object"] for v in variants if v["name"] == variant_name),
                next(v["object"] for v in variants if v["expected"] == "accept")))
            row["day"] = "2026-10-11"
            key = L(row["day"], row["boot"], *([row["first_seq"]] if label == "receipts_root" else []))
            body = canonical(row)
            bad["artifacts_hex"]["candidates/" + F(body) + ".json"] = body.hex()
            bad["roots"]["manifest." + label] = [{"key": key, "bodies_hex": [body.hex()]}]
            bad["manifest"][label] = C([{"key": key, "sha256": F(body), "bytes": len(body), "state": "present"}])
        add("root_preimages", "wrong-day-" + label, "validate_preimages", bad, "reject")
    for role in ("router", "enclave"):
        bad = copy.deepcopy(args)
        decoded = copy.deepcopy(external)
        decoded["requirements"][0][role + "_revision"] = "b" * 40
        bad["artifacts_hex"]["proof.external_proofs_sha256"] = canonical(decoded).hex()
        bad["proof"]["external_proofs_sha256"] = C(decoded)
        bad["manifest"]["proof_manifest_sha256"] = None
        add("root_preimages", "wrong-external-" + role + "-revision", "validate_preimages", bad, "reject")
    # Round-3 independent regional availability matrix: projections before
    # opt-in, observed populations only at actual-evidence stages.
    for stage in ("transport", "fleet-budgets-pre", "fleet-budgets", "shadow-serving", "pre-flip", "post-flip"):
        rep = copy.deepcopy(actual if stage in ("fleet-budgets", "shadow-serving", "pre-flip") else report)
        rep["gate"] = stage
        required = {"transport": (1,), "fleet-budgets-pre": (1, 7),
            "fleet-budgets": (1, 2, 5, 6, 7, 8), "shadow-serving": tuple(range(1, 11)),
            "pre-flip": tuple(range(1, 12)), "post-flip": (1, 11)}[stage]
        rep["rules"] = {f"G{i}": "pass" if i in required or i == 7 else "not_required" for i in range(1, 12)}
        if stage in ("shadow-serving", "pre-flip"):
            rep["attempts"] = dict.fromkeys(rep["attempts"], 0)
            rep["first_seed_at_us"] = rep["clock_start_us"] = rep["window"]["start_us"]
            rep["elapsed_seconds"] = 1
        reg = {**copy.deepcopy(region), "peaks": copy.deepcopy(known_peaks), "timing": copy.deepcopy(rep["timing"])}
        add("gate_outputs_a2", "regional-G7-" + stage, "validate_gate_output", regional_arguments(rep, [reg]))
        for component in peak_ints + peak_ratios:
            bad = copy.deepcopy(reg)
            bad["peaks"][component] = None
            add("gate_outputs_a2", "regional-G7-" + stage + "-missing-" + component,
                "validate_gate_output", regional_arguments(copy.deepcopy(rep), [bad]), "reject")
    segment = next(v["object"] for v in files["budget_envelopes"] if v["target"] == "LOAD_SEGMENT")
    add("budget_envelopes", "equal-endpoints", "LOAD_SEGMENT", {**segment, "end_us": segment["start_us"]})
    interior = [{**copy.deepcopy(segment), "start_us": start, "end_us": end}
                for start, end in ((1, 5), (2, 2))]
    add("budget_envelopes", "legal-empty-interior-segment-root", "entry_root",
        {"entries": interior, "stream": "segments"}, result=C(interior))
    add("budget_envelopes", "empty-interior-cannot-hide-overlap", "entry_root",
        {"entries": [*interior, {**copy.deepcopy(segment), "start_us": 3, "end_us": 6}],
         "stream": "segments"}, "reject")
    for complete in (False, True):
        rep = copy.deepcopy(report)
        failure = {**TAG, "gap": "artifact_gap", "subject": subject("run"), "count": 1}
        fp = pages([failure], 255)
        rep["failures"] = detail([failure], fp)
        rep["failure_counts"]["artifact_gap"] = {"count": 1, "listed": 1, "complete": True, "day_counts": [1]}
        rep["rules"]["G1"] = "fail"
        rep["complete"] = complete
        add("gate_outputs_a2", "artifact-N-" + ("complete" if complete else "incomplete"),
            "validate_gate_output", output_arguments(rep, fp), "reject" if complete else "accept")
    # Unknown-only regional identity: retain known fleet H observations.
    rep = copy.deepcopy(actual)
    for key in ("close_us", "handoff_us"):
        rep["timing"][key] = {"count": 1, "null_count": 0, **dict.fromkeys(("min", "p50", "p95", "p99", "max"), 1)}
    failures3 = [{**TAG, "gap": gap, "subject": (subject("writer", day=DAY, role="router", boot="unknown-region-boot")
        if gap == "regional_roster_unknown" else subject("run")), "count": 1}
        for gap in ("artifact_gap", "fleet_headroom_gap", "regional_roster_unknown")]
    fp = pages(failures3, 255)
    rep["failures"] = detail(failures3, fp)
    for failure3 in failures3:
        rep["failure_counts"][failure3["gap"]] = {"count": 1, "listed": 1, "complete": True, "day_counts": [1]}
    rep["rules"].update(G1="unevaluable", G2="fail", G7="unevaluable")
    rep["complete"] = False
    rep["fleet_peaks"] = copy.deepcopy(peaks)
    rep["publisher"]["peaks"] = copy.deepcopy(peaks)
    writer3 = copy.deepcopy(next(v["object"] for v in files["writer_reports"] if v["name"] == "unknown-region"))
    writer3["boot"] = "unknown-region-boot"
    wp = pages([writer3], 16)
    rep["writers"] = detail([writer3], wp)
    unknown3 = output_arguments(rep, fp)
    unknown3["writers"] = wp
    add("gate_outputs_a2", "unknown-only-region-known-fleet", "validate_gate_output", unknown3)
    # Known regional observations remain a subset even with unknown attribution.
    mixed = copy.deepcopy(unknown3)
    reg = {**copy.deepcopy(region), "timing": copy.deepcopy(mixed["report"]["timing"])}
    unknown_writer = mixed["writers"][0]["entries"][0]
    known_writer = {**copy.deepcopy(unknown_writer), "boot": "known-region-boot", "region": reg["region"]}
    unknown_writer["interval"]["end_us"] = mixed["report"]["window"]["end_us"] + 1
    known_writer["interval"]["end_us"] = mixed["report"]["window"]["end_us"]
    for field in ("comparator", "admission", "partition", "attempts", "checkpoint"):
        known_writer[field] = copy.deepcopy(mixed["report"][field])
    mixed["writers"] = pages([known_writer, unknown_writer], 16)
    mixed["report"]["writers"] = detail([known_writer, unknown_writer], mixed["writers"])
    mixed["regions"] = pages([reg], 8)
    mixed["report"]["regions"] = detail([reg], mixed["regions"])
    mixed["output"]["coverage_sha256"] = C(mixed["report"])
    add("gate_outputs_a2", "known-and-unknown-valid-base", "validate_gate_output", mixed)
    for key in ("close_us", "handoff_us"):
        reg["timing"][key]["count"] = 2
    mixed["regions"] = pages([reg], 8)
    mixed["report"]["regions"] = detail([reg], mixed["regions"])
    mixed["output"]["coverage_sha256"] = C(mixed["report"])
    add("gate_outputs_a2", "impossible-region-rejected", "validate_gate_output", mixed, "reject")
    # Required-value null is impossible even when the fleet source is unavailable.
    rep = copy.deepcopy(actual)
    reg = {**copy.deepcopy(region), "peaks": copy.deepcopy(known_peaks), "timing": copy.deepcopy(rep["timing"])}
    for key in ("close_us", "handoff_us"):
        reg["timing"][key] = {"count": 1, "null_count": 1, **dict.fromkeys(("min", "p50", "p95", "p99", "max"), 1)}
        rep["timing"][key] = copy.deepcopy(stat)
    rep["null_counts"]["timing"] = None
    failures3 = [{**TAG, "gap": gap, "subject": (object_subject("a2_file", "missing-close.json")
        if gap == "artifact_gap" else subject("writer", day=DAY, role="router", boot="issuer")), "count": 1}
        for gap in ("artifact_gap", "close_handoff_budget_gap", "writer_closure_gap")]
    fp = pages(failures3, 255)
    rep["failures"] = detail(failures3, fp)
    for failure3 in failures3:
        rep["failure_counts"][failure3["gap"]] = {"count": 1, "listed": 1, "complete": True, "day_counts": [1]}
    rep["rules"].update(G1="unevaluable", G7="unevaluable", G8="unevaluable")
    rep["complete"] = False
    null3 = regional_arguments(rep, [reg])
    null3["failures"] = fp
    add("gate_outputs_a2", "region-required-null-under-unavailable-fleet", "validate_gate_output", null3, "reject")
    # Complete invariant audit: sorted does not declare unique retained tools;
    # relative FILE_REF keys have no declared normalized-path restriction.
    tool3 = {**verifier, "entry_point": "pilot_enablement.fleet_budgets_pre", "keyset_sha256": None}
    add("verifier_ports", "repeated-sorted-tool", "VERIFIERS", {**verifiers, "retained_tools": [tool3, tool3]})
    for path3 in ("evidence//source.json", "evidence/./source.json"):
        add("input_inventory", "relative-key-" + path3, "FILE_REF", ref(path3))
    add("gate_report", "independently-known-percentile", "STAT",
        {"count": 1, "null_count": 0, "min": None, "p50": 1, "p95": None, "p99": None, "max": None})
    # Every new schema receives independent byte-bound/missing/extra variants.
    limits = {"GATE_REPORT": 65536, "FAILURE_SUBJECT": 768, "GATE_FAILURE": 1024, "RATIO": 256,
        "STAT": 512, "RATIONAL": 128, "PEAKS": 4096, "DETAIL": 512, "WRITER_REPORT": 8192,
        "REGION_REPORT": 16384, "FAILURE_PAGE": 262144, "WRITER_REPORT_PAGE": 132096,
        "REGION_REPORT_PAGE": 132096, "ROOT_ENTRY_PAGE": 132096, "ATTACHMENT_PAGE": 132096,
        "ROOT_ENTRY": 1024, "VERIFIER_USE": 1024, "VERIFIERS": 12288, "FILE_REF": 768,
        "APPROVAL": 1024, "INPUT_INVENTORY_ENTRY": 2048, "SHUTDOWN_ENTRY": 2048,
        "LOAD_SEGMENT": 8192, "EXPORT_OPERATION": 2048, "EXTERNAL_REQUIREMENT": 2048,
        "RESET_ENTRY": 2048, "REVIEW_RECORD": 1024, "SHUTDOWN_MEASUREMENT": 2048, "DELTAS": 2048}
    for target in "INPUT_LEAF INDEPENDENT_INVENTORY SHUTDOWN_INVENTORY COHORT_SEED_POLICY DELEGATED_PAYLOAD BUDGET_ENVELOPE LOAD_EVIDENCE EXTERNAL_PROOFS RESOLUTION_DAG PROOF_RESULT".split():
        limits[target] = 262144
    seen: set[str] = set()
    for file, variants in files.items():
        for vector in list(variants):
            target = vector["target"]
            if target in seen or target not in limits or vector["expected"] != "accept":
                continue
            seen.add(target)
            value = vector["object"]
            add(file, target + "-extra", target, {**value, "extra": None}, "reject")
            add(file, target + "-missing", target, {k: v for k, v in value.items() if k != next(iter(value))}, "reject")
            for offset in (0, 1):
                add(file, target + "-bytes-" + str(limits[target] + offset), target, value,
                    "reject" if offset else "accept", transport_bytes=limits[target] + offset)
    output = {file + ".json": canonical({**TAG, "variants": variants}) + b"\n" for file, variants in files.items()}
    a1_manifest_raw = (A1 / "manifest.json").read_bytes()
    a1_manifest = json.loads(a1_manifest_raw)
    assert {p.name for p in A1.iterdir()} == {*a1_manifest["files"], "manifest.json"}
    for file, entry in a1_manifest["files"].items():
        raw = (A1 / file).read_bytes()
        assert F(raw) == entry["sha256"]
        variants = json.loads(raw)["variants"]
        assert entry["variants"] == [v["name"] for v in variants]
        assert entry["variant_sha256"] == {v["name"]: C(v) for v in variants}
        output["a1/" + file] = raw
    output["a1/manifest.json"] = a1_manifest_raw
    manifest_release = {**TAG, "source_a1_manifest_sha256": F(a1_manifest_raw), "baseline_manifest_sha256": F((BASE / "manifest.json").read_bytes()),
        "files": {file: {"sha256": F(raw), "variants": [v["name"] for v in json.loads(raw).get("variants", [])],
            "variant_sha256": {v["name"]: C(v) for v in json.loads(raw).get("variants", [])}} for file, raw in sorted(output.items())}}
    output["manifest.json"] = canonical(manifest_release) + b"\n"
    return output


def main() -> None:
    for path, raw in build().items():
        destination = OUT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    print(F((OUT / "manifest.json").read_bytes()))


if __name__ == "__main__":
    main()
