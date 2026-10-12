"""Pure contextual completion and receipt-derived A1 report obligations."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a1_deltas as d
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_schema import (
    LOCATOR,
    array,
    day,
    digest,
    identity,
    integer,
    literal,
    nullable,
    require,
    sequence,
    sha256,
    versioned,
)


def resolve_event(page: Any, locator: Any) -> dict[str, Any]:
    """An exact supplied immutable page is mandatory; memory absence is not proof."""
    p1.PAGE(page)
    LOCATOR(locator)
    require(all(page[k] == locator[k] for k in ("day", "boot", "first_seq")), "retry_original_gap")
    matches = [event for event in page["events"] if event["seq"] == locator["event_seq"]]
    require(len(matches) == 1 and sha256(matches[0]) == locator["event_sha256"], "retry_original_gap")
    return dict(matches[0])


def event_locator(page: Any, event_seq: Any) -> dict[str, Any]:
    p1.PAGE(page)
    sequence(event_seq)
    matches = [event for event in page["events"] if event["seq"] == event_seq]
    require(len(matches) == 1, "retry_original_gap")
    return {**{k: page[k] for k in ("day", "boot", "first_seq")},
            "event_seq": event_seq, "event_sha256": sha256(matches[0])}


def validate_completion(original_page: Any, original_locator: Any,
                        completion_page: Any, completion_locator: Any) -> None:
    """The single A1 completion choke point for finalize, reconcile and HWM callers."""
    original = resolve_event(original_page, original_locator)
    completion = resolve_event(completion_page, completion_locator)
    require(original["phase"] in ("settle", "refund") and completion["phase"] == "completion", "retry_original_gap")
    require(original_page["boot"] == completion_page["boot"], "retry_original_gap")
    require(original["receipt_at_us"] <= completion["event_at_us"]
            and original["event_at_us"] <= completion["event_at_us"], "retry_original_gap")
    require(completion["completion_of"] == original["seq"], "retry_original_gap")
    reference = {"day": original_page["day"], "boot": original_page["boot"], "event_seq": original["seq"]}
    cross_day = d.broadening("a1-cross-day-sequence", {"original_day": original_page["day"], "completion_day": completion_page["day"]})
    require(original_page["day"] <= completion_page["day"], "retry_original_gap")
    if cross_day:
        require(completion["retry_original"] == reference, "retry_original_gap")
        # Only sequence ordering is broadened. P1 still owns every identity,
        # proof, winner, mismatch, prediction and event-time preservation rule.
        original = {**original, "seq": "00000000000000000001"}
        completion = {**completion, "seq": "00000000000000000002", "completion_of": "00000000000000000001"}
    elif completion["retry_original"] is not None:
        require(completion["retry_original"] == reference, "retry_original_gap")
    p1.validate_completion(original, completion)


def _obligation(v: Any) -> None:
    if v["status"] == "open":
        require(v["terminal_reference"] is None and v["gap"] is None)
    elif v["status"] == "terminal_reconciled":
        require(v["terminal_reference"] is not None and v["gap"] is None)
    else:
        require(v["gap"] is not None)


AUTHORIZATION_OBLIGATION = versioned({"contract_revision": literal("a1"), "original_receipt": LOCATOR,
    "identity_sha256": digest, "horizon_at_us": integer, "status": literal("open", "terminal_reconciled", "blocked"),
    "terminal_reference": nullable(LOCATOR),
    "gap": nullable(literal("authorization_link_gap", "authorization_horizon_gap", "authorization_terminal_conflict"))}, 2048, _obligation)


def _obligation_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    loc = entry["original_receipt"]
    return loc["day"], loc["boot"], loc["event_seq"]


def _obligation_page(v: Any) -> None:
    keys = [_obligation_key(entry) for entry in v["entries"]]
    require(keys == sorted(set(keys)))
    require(all(key[:2] == (v["day"], v["boot"]) for key in keys))
    require((v["previous_sha256"] is None) == (int(v["page_seq"]) == 1))


AUTHORIZATION_OBLIGATION_PAGE = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "page_seq": sequence, "previous_sha256": nullable(digest),
    "entries": array(AUTHORIZATION_OBLIGATION, 1000)}, 262144, _obligation_page)
OBLIGATION_PROJECTION = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "closure_sha256": digest, "open_obligation_count": integer, "open_obligations_root": digest,
    "reconciliation_root": digest}, 1024)


def obligation_projection(closure: Any, entries: Any) -> dict[str, Any]:
    a1.CLOSURE(closure)
    array(AUTHORIZATION_OBLIGATION, p1.EVENT_CAP)(entries)
    ordered = sorted(entries, key=_obligation_key)
    require(len({_obligation_key(entry) for entry in ordered}) == len(ordered), "authorization_obligation_gap")
    require(all(_obligation_key(entry)[:2] == (closure["day"], closure["boot"]) for entry in ordered), "authorization_obligation_gap")
    unresolved = [entry for entry in ordered if entry["status"] in ("open", "blocked")]
    projection = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a1",
        "day": closure["day"], "boot": closure["boot"], "closure_sha256": sha256(closure),
        "open_obligation_count": len(unresolved), "open_obligations_root": sha256(unresolved),
        "reconciliation_root": sha256(ordered)}
    OBLIGATION_PROJECTION(projection)
    return projection


def authorization_obligation(page: Any, event_seq: Any, *, original_page: Any = None,
                             original_event_seq: Any = None, proof_expires_at_us: Any = None) -> dict[str, Any]:
    """Derive an OPEN entry per issuance/replay, retaining its own exact locator.

    The caller resolves immutable original pages, factual backing and proof expiry;
    no expiry or local durability can itself produce a reconciled status.
    """
    locator = event_locator(page, event_seq)
    event = resolve_event(page, locator)
    require(event["phase"] in ("authorize", "replay"), "authorization_obligation_gap")
    original = event
    if event["phase"] == "replay":
        old_locator = event_locator(original_page, original_event_seq)
        original = resolve_event(original_page, old_locator)
        require(original["phase"] == "authorize" and event["retry_original"] == {
            "day": old_locator["day"], "boot": old_locator["boot"], "event_seq": old_locator["event_seq"]}, "authorization_link_gap")
        require(all(original[k] == event[k] for k in ("identity", "decision", "authorization_day", "authorization_id"))
                and original["event_at_us"] <= event["event_at_us"], "authorization_link_gap")
    else:
        require(original_page is None and original_event_seq is None, "authorization_link_gap")
    horizon = original["event_at_us"] + 172800000000
    if proof_expires_at_us is not None:
        integer(proof_expires_at_us)
        require(proof_expires_at_us > original["event_at_us"], "authorization_horizon_gap")
        horizon = min(horizon, proof_expires_at_us)
    integer(horizon)
    result = {"v": 2, "policy_version": "shadow-v2", "contract_revision": "a1", "original_receipt": locator,
        "identity_sha256": sha256(original["identity"]), "horizon_at_us": horizon,
        "status": "open", "terminal_reference": None, "gap": None}
    AUTHORIZATION_OBLIGATION(result)
    return result


def validate_terminal_join(obligation: Any, issuance_page: Any, terminal_page: Any, terminal_locator: Any,
                           *, original_page: Any = None, original_event_seq: Any = None,
                           proof_expires_at_us: Any = None) -> None:
    """Necessary structural join checks, after P5 independently reconciles backing.

    This function does not assert signature, complete attempt coverage or report
    PASS. Those remain P5 obligations; no schema can prove an IO history.
    """
    AUTHORIZATION_OBLIGATION(obligation)
    issuance = resolve_event(issuance_page, obligation["original_receipt"])
    derived = authorization_obligation(issuance_page, obligation["original_receipt"]["event_seq"],
        original_page=original_page, original_event_seq=original_event_seq, proof_expires_at_us=proof_expires_at_us)
    require(all(derived[k] == obligation[k] for k in ("original_receipt", "identity_sha256", "horizon_at_us")), "authorization_link_gap")
    original = (resolve_event(original_page, event_locator(original_page, original_event_seq))
                if issuance["phase"] == "replay" else issuance)
    terminal = resolve_event(terminal_page, terminal_locator)
    require(issuance["phase"] in ("authorize", "replay") and terminal["phase"] in ("settle", "refund"), "authorization_link_gap")
    require(terminal["partition"] in ("A", "B") and terminal["identity"] == issuance["identity"]
        and sha256(terminal["identity"]) == obligation["identity_sha256"]
        and terminal["decision"] == issuance["decision"]
        and terminal["authorization_day"] == issuance["authorization_day"], "authorization_terminal_conflict")
    require(original["event_at_us"] <= terminal["event_at_us"]
            and terminal["receipt_at_us"] < obligation["horizon_at_us"], "authorization_horizon_gap")
    if obligation["terminal_reference"] is not None:
        require(obligation["terminal_reference"] == terminal_locator, "authorization_link_gap")
