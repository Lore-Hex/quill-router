"""Pure A2 output relationships and explicit supplied-preimage checks.

These checks cannot certify evidence evaluation or verifier execution. P5 owns
those facts. Detail arguments are supplied pages; verifiers is the independently
supplied VERIFIERS record. No callable is invoked here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as e
from trusted_router import async_settle_shadow_v2_a2_roots as r
from trusted_router.async_settle_shadow_v2_certificate import ReceiptVerification
from trusted_router.async_settle_shadow_v2_schema import integer, require, sha256
from trusted_router.detached_jws import TrustedKey, canonical


class ShadowClaims(Protocol):
    """Opaque structural return: the gate does not inspect or revalidate claims."""


class BindingVerifier(Protocol):
    def __call__(self, token: str, keys: Sequence[TrustedKey], now: int) -> ShadowClaims: ...


class CertificateVerifier(Protocol):
    def __call__(self, token: str, keys: Sequence[TrustedKey], expected: Mapping[str, Any],
                 now: int, *, returned_key: tuple[str, str], page: bytes | None,
                 attempt: dict[str, Any], server_attempt: dict[str, Any],
                 booking: str) -> ReceiptVerification: ...


# Minimum required rule sets explicitly stated in §8. P5 owns additional
# stage/applicability predicates, including transport and post-flip proof scope.
REQUIRED_RULES = {
    "transport": ("G1",), "fleet-budgets-pre": ("G1", "G7"),
    "fleet-budgets": ("G1", "G2", "G5", "G6", "G7", "G8"),
    "shadow-serving": tuple(f"G{i}" for i in range(1, 11)),
    "pre-flip": tuple(f"G{i}" for i in range(1, 12)), "post-flip": ("G1", "G11"),
}


def _sum(values: Iterable[int | None]) -> int | None:
    items = list(values)
    if any(item is None for item in items):
        return None
    total = sum(item for item in items if item is not None)
    integer(total)
    return total


def _metrics(v: dict[str, Any]) -> None:
    for field, numerator, denominator in (
        ("comparator", v["comparator"]["compared"], v["comparator"]["eligible"]),
        ("admission", _sum(v["admission"][k] for k in ("Y", "N")), v["admission"]["E"]),
    ):
        ratio = v[field]["coverage"]
        if ratio["state"] in ("evaluated", "not_exercised"):
            require(ratio["numerator"] == numerator and ratio["denominator"] == denominator)
        if ratio["state"] == "not_exercised":
            require(all(v["admission"][k] == 0 for k in ("E", "Y", "N", "U"))
                    and all(x is not None for x in v["partition"].values()) and v["partition"]["C"] == 0)
    timing = v["timing"]
    require(v["null_counts"]["timing"] == _sum(stat["null_count"] for stat in timing.values()))
    delta = v["deltas"]
    require(v["null_counts"]["delta"] == delta["null_count"])
    if delta["count"] is None:
        require(all(delta[k] is None for k in ("null_count", "min_micro", "max_micro"))
                and all(x is None for x in delta["bins"].values()))
    else:
        require(delta["null_count"] is not None and _sum(delta["bins"].values()) == delta["count"])
        if delta["count"] == 0:
            require(delta["min_micro"] is delta["max_micro"] is None)
        else:
            require(delta["min_micro"] is not None and delta["max_micro"] is not None
                    and delta["min_micro"] <= delta["max_micro"])
    # These are population relationships, not a reconstruction from attempts.
    populations = [_sum((timing[k]["count"], timing[k]["null_count"])) for k in e.TIMINGS[:8]]
    populations.append(_sum((delta["count"], delta["null_count"])))
    known = [n for n in populations if n is not None]
    require(not known or len(set(known)) == 1)
    self_timing = timing["evidence_write_us"]
    if self_timing["count"] is not None:
        require(self_timing["count"] == 0)
    if known:
        for key in ("eligibility", "booking"):
            require(v["null_counts"][key] is None or v["null_counts"][key] <= known[0])
    jt = [timing[k] for k in e.TIMINGS[8:10]]
    jcounts = [_sum((s["count"], s["null_count"])) for s in jt]
    require(None in jcounts or jcounts[0] == jcounts[1])
    for key in e.TIMINGS[9:]:
        require(timing[key]["count"] is None or timing[key]["null_count"] == 0)
    hcounts = [timing[k]["count"] for k in ("close_us", "handoff_us")]
    require(None in hcounts or hcounts[0] == hcounts[1])


def _pass_aggregates(report: dict[str, Any]) -> None:
    rules = report["rules"]
    dependent = {
        "G3": ("attempts",), "G4": ("partition",), "G5": ("comparator", "admission", "partition"),
        "G6": ("publisher",), "G8": ("checkpoint", "handoff"),
    }
    for rule, fields in dependent.items():
        if rules[rule] != "pass":
            continue
        for field in fields:
            require(all(value is not None for key, value in report[field].items() if key not in ("coverage", "peaks")))
    for field in ("comparator", "admission"):
        if rules["G5"] == "pass":
            require(report[field]["coverage"]["state"] in ("evaluated", "not_exercised"))
    # Required sample/slot/close populations may be empty, but not unavailable.
    required_timings = (() if rules["G5"] != "pass" else e.TIMINGS[:8]) + (() if rules["G6"] != "pass" else e.TIMINGS[8:10]) + (() if rules["G8"] != "pass" else e.TIMINGS[10:])
    require(all(report["timing"][k]["count"] is not None for k in required_timings))
    if rules["G5"] == "pass":
        require(report["deltas"]["count"] is not None)
        require(all(report["null_counts"][key] is not None for key in
                    ("eligibility", "comparison", "prediction", "booking", "delta")))


def validate_gate_output(output: Any, report: Any, *, failures: Iterable[dict[str, Any]],
                         writers: Iterable[dict[str, Any]], regions: Iterable[dict[str, Any]],
                         inputs: Iterable[dict[str, Any]], reset_history: Iterable[dict[str, Any]],
                         verifiers: Any) -> None:
    """Validate commitments; independent expected sets/prefix evaluation stay P5."""
    a1.GATE_OUTPUT(output)
    e.GATE_REPORT(report)
    e.VERIFIERS(verifiers)
    require(output["coverage_sha256"] == sha256(report), "artifact_gap")
    for field in ("gate", "completed_at_us", "proof_manifest_sha256", "prerequisite_ledger_root", "clock_start_us"):
        require(output[field] == report[field])
    require(canonical(verifiers) == canonical(report["verifiers"]), "artifact_gap")
    streams = {"failures": failures, "writers": writers, "regions": regions,
               "input_inventory": inputs, "reset_history": reset_history}
    listed: dict[str, int] = dict.fromkeys((row.name for row in e.FAILURE_RULES), 0)
    for stream, pages in streams.items():
        require(isinstance(pages, Iterable) and not isinstance(pages, (str, bytes, dict)))
        materialized = list(pages)
        r.validate_detail(report[stream], materialized, stream=stream)
        if stream == "failures":
            for page in materialized:
                for failure in page["entries"]:
                    listed[failure["gap"]] += 1
    require(all(tally["listed"] == listed[name] for name, tally in report["failure_counts"].items()))
    gaps = sorted(name for name, tally in report["failure_counts"].items() if tally["count"] > 0)
    require(output["gaps"] == gaps)
    require((output["verdict"] == "BLOCKED") == bool(gaps))
    require(report["complete"] or "artifact_gap" in gaps)
    for rule in REQUIRED_RULES[report["gate"]]:
        require(report["rules"][rule] != "not_required")
    for rule, state in report["rules"].items():
        if state in ("fail", "unevaluable"):
            require(any(row.rule == rule and row.name in gaps for row in e.FAILURE_RULES))
    if report["elapsed_seconds"] is None:
        require(output["elapsed_seconds"] == 0 and report["clock_start_us"] is None)
        require(report["gate"] not in ("shadow-serving", "pre-flip") or output["verdict"] == "BLOCKED")
    else:
        require(output["elapsed_seconds"] == report["elapsed_seconds"] and report["clock_start_us"] is not None)
        require(report["clock_start_us"] <= report["window"]["end_us"])
        require(report["elapsed_seconds"] == (report["window"]["end_us"] - report["clock_start_us"]) // 1000000)
    if report["clock_start_us"] is not None:
        require(report["clock_start_us"] == report["first_seed_at_us"])
    _metrics(report)
    if output["verdict"] == "PASS":
        require(report["complete"] and all(state in ("pass", "not_required") for state in report["rules"].values()))
        require(verifiers["certificate"] is not None and verifiers["binding"] is not None, "artifact_gap")
        _pass_aggregates(report)


def display_failure(failure: Any) -> str:
    e.GATE_FAILURE(failure)
    s = failure["subject"]
    dims, obj = s["dimensions"] or {}, s["object"] or {}
    fields = [("gap", failure["gap"]), *((k, s[k]) for k in ("scope", "day", "role", "boot", "phase")),
        ("adapter", dims.get("adapter")), ("route", dims.get("route_type")), ("streamed", dims.get("streamed")),
        *((k, obj.get(k)) for k in ("kind", "key", "event_seq")), ("at_us", s["at_us"]), ("revision", s["revision"])]
    def render(value: Any) -> str:
        if value is None:
            return "~"
        text = str(value).lower() if type(value) is bool else str(value)
        return "".join(chr(b) if 65 <= b <= 90 or 97 <= b <= 122 or 48 <= b <= 57 or b in (95, 45, 46)
                       else f"%{b:02X}" for b in text.encode("utf-8"))
    return ";".join(k + "=" + render(v) for k, v in fields)


# Field interpretation is closed. Artifacts maps these qualified field names to
# ORIGINAL bytes, plus relative FILE_REF paths to bytes. Roots maps entry-root
# fields to [{key, bodies:[bytes,...]}], frozen day coverage fields to A1 pages,
# and cross-day coverage fields to [{day,pages:[A1 pages,...]}]. This explicit
# in-memory seam does not discover files or imply evidence-set completeness.
OBJECT_PREIMAGES = {
    "manifest.independent_inventory_sha256": e.INDEPENDENT_INVENTORY,
    "proof.shutdown_inventory_sha256": e.SHUTDOWN_INVENTORY,
    "proof.cohort_seed_policy_sha256": e.COHORT_SEED_POLICY,
    "proof.pre_budget_sha256": e.BUDGET_ENVELOPE, "proof.post_budget_sha256": e.BUDGET_ENVELOPE,
    "proof.external_proofs_sha256": e.EXTERNAL_PROOFS, "proof.resolution_dag_sha256": e.RESOLUTION_DAG,
}
FILE_PREIMAGES = ("manifest.config_sha256", "manifest.fixture_sha256", "proof.evaluator_fixture_sha256",
    "proof.binding_fixture_sha256", "proof.wire_fixture_sha256")
ENTRY_PREIMAGES = {"manifest." + k: k for k in ("counter_root", "closure_root", "receipts_root", "prerequisite_ledger_root")} | {
    "proof.ledger_root": "ledger_root", "proof.publisher_slot_trace_sha256": "publisher_slot_trace_sha256"}
COVERAGE_PREIMAGES = {"manifest." + k + "_root": k for k in ("close_result", "publisher_trace", "health_basis")}
CROSS_DAY_PREIMAGES = {"proof." + k + "_root": k for k in a1.COVERAGE_STREAMS}


def _object_bytes(raw: bytes, schema: e.Schema) -> dict[str, Any]:
    # Frozen Schema.decode declares the single fixture LF outside the object.
    # F still includes it; all original object bytes go to the delegated parser.
    require(type(raw) is bytes, "artifact_gap")
    return schema.parse(raw[:-1] if raw.endswith(b"\n") else raw)


def _references(value: Any, artifacts: Mapping[str, bytes]) -> None:
    if type(value) is dict:
        if set(value) == set(e.FILE_REF.fields):
            e.FILE_REF(value)
            require(value["path"] in artifacts, "artifact_gap")
            raw = artifacts[value["path"]]
            require(r.file_digest(raw) == value["sha256"] and len(raw) == value["bytes"], "artifact_gap")
        else:
            for child in value.values():
                _references(child, artifacts)
    elif type(value) is list:
        for child in value:
            _references(child, artifacts)


def validate_preimages(manifest: Any, proof: Any, *, artifacts: Mapping[str, bytes], roots: Mapping[str, Any]) -> None:
    a1.MANIFEST(manifest)
    a1.PROOF_MANIFEST(proof)
    require(type(artifacts) is dict and type(roots) is dict)
    expected_roots = set(ENTRY_PREIMAGES) | set(COVERAGE_PREIMAGES) | set(CROSS_DAY_PREIMAGES)
    require(set(roots) == expected_roots, "evidence_schema_gap" if set(roots) - expected_roots else "artifact_gap")
    objects = {"manifest": manifest, "proof": proof}
    for qualified in (*FILE_PREIMAGES, *OBJECT_PREIMAGES):
        require(qualified in artifacts, "artifact_gap")
        raw = artifacts[qualified]
        actual = r.file_digest(raw)
        if qualified in OBJECT_PREIMAGES:
            decoded = _object_bytes(raw, OBJECT_PREIMAGES[qualified])
            _references(decoded, artifacts)
            if qualified in ("proof.pre_budget_sha256", "proof.post_budget_sha256"):
                require(decoded["stage"] == ("pre" if qualified == "proof.pre_budget_sha256" else "post"))
            actual = sha256(decoded)
        owner, field = qualified.split(".")
        require(objects[owner][field] == actual, "artifact_gap")
    for qualified, stream in ENTRY_PREIMAGES.items():
        supplied = roots[qualified]
        require(type(supplied) is list)
        entries = []
        for candidate in supplied:
            require(type(candidate) is dict and set(candidate) == {"key", "bodies"})
            entry = r._root_entry(candidate["key"], candidate["bodies"])
            # Every retained candidate has a digest-qualified named inventory key.
            for body in candidate["bodies"]:
                digest = r.file_digest(body)
                require(any(type(path) is str and digest in path and data == body for path, data in artifacts.items()), "artifact_gap")
            entries.append(entry)
        owner, field = qualified.split(".")
        require(objects[owner][field] == r.entry_root(entries, stream=stream), "artifact_gap")
    for qualified, stream in COVERAGE_PREIMAGES.items():
        pages = roots[qualified]
        require(type(pages) is list)
        actual = a1.coverage_root(pages)
        require(all(page["stream"] == stream and page["day"] == manifest["day"] for page in pages))
        require(manifest[qualified.split(".")[1]] == actual, "artifact_gap")
    for qualified, stream in CROSS_DAY_PREIMAGES.items():
        days = roots[qualified]
        require(type(days) is list)
        entries = []
        for item in days:
            require(type(item) is dict and set(item) == {"day", "pages"})
            e.day(item["day"])
            require(type(item["pages"]) is list)
            root = a1.coverage_root(item["pages"])
            require(all(page["day"] == item["day"] and page["stream"] == stream for page in item["pages"]))
            entries.append({"day": item["day"], "root": root, "count": sum(len(page["entries"]) for page in item["pages"])})
        require(proof[qualified.split(".")[1]] == r.entry_root(entries, stream=stream + "_root"), "artifact_gap")
    if manifest["proof_manifest_sha256"] is not None:
        require("proof_manifest" in artifacts, "artifact_gap")
        raw = artifacts["proof_manifest"]
        require(_object_bytes(raw, a1.PROOF_MANIFEST) == proof and r.file_digest(raw) == manifest["proof_manifest_sha256"], "artifact_gap")
    pre = _object_bytes(artifacts["proof.pre_budget_sha256"], e.BUDGET_ENVELOPE)
    post = _object_bytes(artifacts["proof.post_budget_sha256"], e.BUDGET_ENVELOPE)
    require(post["pre_envelope_sha256"] == sha256(pre), "artifact_gap")
