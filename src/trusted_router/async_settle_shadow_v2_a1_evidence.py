"""Additive A1 evidence shapes and close-result binding; no live integration."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a1_deltas as d
from trusted_router import async_settle_shadow_v2_a1_health as h
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_schema import (
    Schema,
    array,
    ascii_text,
    boolean,
    day,
    digest,
    identity,
    integer,
    literal,
    nullable,
    require,
    revision,
    sequence,
    sha256,
    versioned,
)

KINDS = (*p1.KINDS, d.CLOSE_KIND)
RETENTION_KINDS = KINDS
EXPORT_KINDS = KINDS
COVERAGE_STREAMS = ("close_result", "publisher_trace", "handoff_accounting", "health_basis", "authorization_obligation")
FIXTURE_MANIFEST_SHA256 = "df704c8d597c2118c487edb6cdb84f6600f20356054e9c6c59edbd196b2aa6db"


def _counter(v: Any) -> None:
    if v["closed"]:
        h.validate_publisher_final(v["publisher"])


COUNTER = d.extend(p1.COUNTER, {"publisher": h.PUBLISHER_OPERATIONS},
    projection=d.counter_projection, invariant=_counter)
CLOSURE = d.extend(p1.CLOSURE, {"authorization_obligations": literal("receipt_chain")})
MANIFEST = d.extend(p1.MANIFEST, dict.fromkeys(d.DAY_ROOTS, digest))
PROOF_MANIFEST = d.extend(p1.PROOF_MANIFEST, dict.fromkeys(d.PROOF_ROOTS, digest))
PREREQUISITE_LEDGER = d.extend(p1.PREREQUISITE_LEDGER, {})
GATE_OUTPUT = d.extend(p1.GATE_OUTPUT, {})
CAP = d.extend(p1.CAP, {})


def _retention(v: dict[str, Any]) -> dict[str, Any]:
    return {**d.without(v, "contract_revision"), "kinds": [k for k in v["kinds"] if not d.broadening("a1-close-kind", k)]}


RETENTION = d.extend(p1.RETENTION, {"kinds": array(literal(*KINDS), len(KINDS), len(KINDS), unique=True)}, projection=_retention)


def _close_result(v: Any) -> None:
    require(v["physical_commit_ack_at_us"] == v["observed_at_us"])
    require(v["elapsed_us"] <= 2000000 and v["elapsed_us"] <= v["flush_remaining_us"])
    require(v["shutdown_remaining_us"] is None or v["elapsed_us"] <= v["shutdown_remaining_us"])


CLOSE_RESULT = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "router_revision": revision, "closure_sha256": digest,
    **{k: integer for k in "final_checkpoint_seq attempt_started_at_us observed_at_us elapsed_us physical_commit_ack_at_us flush_remaining_us".split()},
    "outcome": literal("acknowledged"), "shutdown_remaining_us": nullable(integer),
    "handoff_attempt_limit": literal(1)}, 4096, _close_result)
SEALED_CLOSURE = d.extend(p1.SEALED_CLOSURE, {"day": day, "boot": identity, "close_result_sha256": digest})


def validate_close_result(result: Any, closure: Any, *, termination_applies: Any) -> None:
    CLOSE_RESULT(result)
    CLOSURE(closure)
    boolean(termination_applies)
    require(result["closure_sha256"] == sha256(closure)
        and all(result[k] == closure[k] for k in ("day", "boot", "router_revision", "final_checkpoint_seq")), "close_result_identity_gap")
    required = termination_applies or closure["reason"] == "shutdown"
    require((result["shutdown_remaining_us"] is not None) == required, "close_result_identity_gap")


def sealed_closure(result: Any, closure: Any, counter: Any, *, lifecycle: Any,
                   termination_applies: Any) -> dict[str, Any]:
    """Project only already-covered evidence; cannot originate an acknowledgement."""
    validate_close_result(result, closure, termination_applies=termination_applies)
    COUNTER(counter)
    # Reuse the independent inventory's exact baseline writer/lifetime shape.
    p1.ROSTER(lifecycle)
    require(set(lifecycle) == {closure["boot"]}, "close_result_identity_gap")
    writer = lifecycle[closure["boot"]]
    require(writer["revision"] == closure["router_revision"] and writer["region"] == closure["region"], "close_result_identity_gap")
    require(writer["lifetime"]["start_us"] <= closure["interval_end_us"] <= writer["lifetime"]["end_us"], "close_result_identity_gap")
    require(closure["counter_sha256"] == sha256(counter) and counter["closed"]
        and all(counter[k] == closure[k] for k in ("day", "boot", "router_revision", "region"))
        and counter["checkpoint_seq"] == closure["final_checkpoint_seq"]
        and counter["event_seq"] == counter["durable_event_hwm"] == closure["final_event_seq"]
        and counter["loss_count"] == closure["loss_count"] == 0
        and counter["irrecoverable_losses"] == counter["unreconciled_commits"] == 0, "writer_closure_gap")
    require(counter["receipt_chain_sha256"] == closure["receipt_chain_sha256"]
            or counter["receipt_chain_sha256"] is None and counter["event_seq"] == counter["receipt_count"] == 0, "writer_closure_gap")
    value = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a1",
        "day": closure["day"], "boot": closure["boot"], "closure_sha256": sha256(closure),
        "physical_commit_ack_at_us": result["physical_commit_ack_at_us"], "close_result_sha256": sha256(result)}
    SEALED_CLOSURE(value)
    return value


def _handoff(v: Any) -> None:
    require(v["attempts_lower"] == int(v["landed"]))
    require((v["result_sha256"] is not None) == v["landed"])


HANDOFF_ACCOUNTING = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "expected_handoffs": literal(1), "attempts_lower": integer, "attempts_upper": literal(1),
    "landed": boolean, "reply_status": literal("not_proven"), "failures_lower": literal(0),
    "failures_upper": literal(1), "unknown_reply_upper": literal(1), "result_sha256": nullable(digest)}, 1024, _handoff)


def _coverage_entry(v: Any) -> None:
    if v["state"] == "missing":
        require(v["sha256"] is None and v["bytes"] == 0)
    else:
        require(v["sha256"] is not None and v["bytes"] > 0)


COVERAGE_ENTRY = Schema({"key": ascii_text(512), "sha256": nullable(digest),
    "bytes": integer, "state": literal("present", "missing", "conflict")}, 1024, _coverage_entry)


def _coverage(v: Any) -> None:
    keys = [entry["key"] for entry in v["entries"]]
    require(keys == sorted(set(keys)))
    require((v["previous_sha256"] is None) == (int(v["page_seq"]) == 1))


COVERAGE_PAGE = versioned({"contract_revision": literal("a1"), "day": day,
    "stream": literal(*COVERAGE_STREAMS), "page_seq": sequence, "previous_sha256": nullable(digest),
    "entries": array(COVERAGE_ENTRY, 1000)}, 262144, _coverage)


def coverage_root(pages: Any) -> str:
    array(COVERAGE_PAGE, 10000)(pages)
    previous = None
    last_key = None
    for i, page in enumerate(pages):
        require(int(page["page_seq"]) == i + 1 and page["previous_sha256"] == previous)
        if i:
            require(all(page[k] == pages[0][k] for k in ("day", "stream")))
        if page["entries"]:
            require(last_key is None or last_key < page["entries"][0]["key"])
            last_key = page["entries"][-1]["key"]
        previous = sha256(page)
    return sha256([sha256(page) for page in pages])


def validate_row(kind: Any, key: Any, body: Any) -> dict[str, Any]:
    require(type(kind) is str and type(key) is str)
    if kind in p1.KINDS[:3]:
        return p1.validate_row(kind, key, body)
    schemas = {p1.KINDS[3]: COUNTER, p1.KINDS[4]: CLOSURE, p1.KINDS[5]: MANIFEST,
               p1.KINDS[6]: h.HEALTH, p1.KINDS[7]: h.HEALTH_CLAIM, d.CLOSE_KIND: CLOSE_RESULT}
    require(kind in schemas)
    schema = schemas[kind]
    if kind == p1.KINDS[5]:
        schema = RETENTION if key == "retention-v2" else CAP if key.endswith("/cap-v2") else MANIFEST
    value = schema.parse(body)
    if schema in (COUNTER, CLOSURE, CLOSE_RESULT):
        expected = value["day"] + "/" + value["boot"]
    elif schema in (MANIFEST, CAP):
        expected = value["day"] + ("/cap-v2" if schema is CAP else "/manifest-v2")
    else:
        expected = "retention-v2" if schema is RETENTION else "local/fleet" if schema is h.HEALTH else "local/publish"
    require(key == expected, "evidence_identity_gap")
    return value
