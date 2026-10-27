"""Transactional NYTE reservation ledger, independent of all balance/credit APIs.

All entities use complete primary keys. Backends serialize commands using a
single NYTE mutex; the transaction-local cache also gives Spanner mutations
read-your-writes semantics. FIFO and pending-refund queues are linked, so no
entity grows with the lifetime order history. No external I/O occurs here.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from typing import Any

Row = dict[str, Any]
REFUND_WINDOW = 30 * 86400


def reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def refund_cents(price: int, paid: int, fee_bps: int) -> tuple[int, int]:
    """Round net proceeds half-up PER LOT, then cap; fee is the remainder."""
    if price <= 0 or paid <= 0 or not 0 <= fee_bps < 10000:
        raise ValueError("Invalid resale amounts")
    net = (price * (10000 - fee_bps) + 5000) // 10000
    return price - net, min(net, paid)


def execute(read: Callable[[str, str], Row], write: Callable[[str, str, Row], None], command: Row) -> Row:
    cache: dict[tuple[str, str], Row] = {}
    dirty: set[tuple[str, str]] = set()

    def get(kind: str, key: str) -> Row:
        pair = (kind, key)
        if pair not in cache:
            cache[pair] = deepcopy(read(kind, key))
        return cache[pair]

    def put(kind: str, key: str, value: Row) -> None:
        cache[kind, key] = value
        dirty.add((kind, key))

    result = _execute(get, put, deepcopy(command))
    for kind, key in sorted(dirty):
        write(kind, key, cache[kind, key])
    return deepcopy(result)


def _execute(get: Callable[[str, str], Row], put: Callable[[str, str, Row], None], c: Row) -> Row:
    op, now = c["op"], c["now"]

    def queue_append(kind: str, queue: str, key: str, row: Row) -> None:
        head = get("nyte_queue", queue)
        tail = head.get("tail")
        row.update(previous=tail, next=None)
        if tail:
            previous = get(kind, tail)
            if kind == "nyte_listing":
                # Timestamp order cannot go backwards if requests wait for
                # the transaction lock or a host clock lags another host.
                row["listed_at"] = max(row["listed_at"], previous["listed_at"])
            previous["next"] = key
            put(kind, tail, previous)
        else:
            head["head"] = key
        head["tail"] = key
        put("nyte_queue", queue, head)
        put(kind, key, row)

    def queue_remove(kind: str, queue: str, key: str, row: Row) -> None:
        head = get("nyte_queue", queue)
        for direction, other in (("previous", "next"), ("next", "previous")):
            neighbor = row.get(direction)
            if neighbor:
                item = get(kind, neighbor)
                item[other] = row.get(other)
                put(kind, neighbor, item)
        if head.get("head") == key:
            head["head"] = row.get("next")
        if head.get("tail") == key:
            head["tail"] = row.get("previous")
        row.update(previous=None, next=None)
        put("nyte_queue", queue, head)
        put(kind, key, row)

    def block_position(p: Row) -> None:
        # Full-refund requests and completed refunds already block selling;
        # preserve their ops acknowledgement lifecycle when Stripe reports it.
        if p["status"] not in {"refund_requested", "refunded", "needs_ops"}:
            p["status"] = "needs_review"
        p["payment_review"] = True
        for lid in p["listings"]:
            listing = get("nyte_listing", lid)
            if listing["status"] == "active":
                listing.update(status="withdrawn", withdrawal_reason="payment_review")
                queue_remove("nyte_listing", "grade:" + p["grade"], lid, listing)
        p["listed_lots"] = 0
        put("nyte_position", p["session_id"], p)

    def remember_payment_review(payment: str, state: Row) -> Row:
        # Stripe may deliver charge.refunded before the refund worker's final
        # commit. Match Stripe's refund to the durable fill, not its lease/status.
        own_refunds = 0
        seen: set[str] = set()
        for refund in state.get("refunds", []):
            metadata = refund.get("metadata") or {}
            fid = metadata.get("fill_id") or metadata.get("nyte_fill_id")
            if not fid or fid in seen:
                continue
            fill = get("nyte_fill", fid)
            if (fill and fill["payment_intent"] == payment
                    and refund.get("payment_intent") == payment
                    and refund.get("amount") == fill["refund_amount_cents"]
                    and refund.get("status") in {"pending", "succeeded", "requires_action"}
                    and refund.get("id")
                    and fill.get("stripe_refund_id") in {None, refund["id"]}):
                own_refunds += refund["amount"]
                seen.add(fid)
        evidence = get("nyte_payment_review", payment)
        external = max(0, state.get("amount_refunded", 0) - own_refunds)
        if state.get("disputed") or state.get("unpaid") or external:
            evidence.update(disputed=bool(evidence.get("disputed") or state.get("disputed")),
                            unpaid=bool(evidence.get("unpaid") or state.get("unpaid")),
                            external_refund_cents=max(evidence.get("external_refund_cents", 0), external),
                            observed_at=now)
            put("nyte_payment_review", payment, evidence)
        return evidence

    def reconcile(p: Row, state: Row) -> None:
        if remember_payment_review(p.get("payment_intent") or "missing:" + p["session_id"], state):
            block_position(p)

    if op == "reconcile_payment":
        # The durable mapping, not a feature flag or metadata, owns event scope.
        owner = get("nyte_payment", c["payment_intent"])
        if not owner:
            return {}
        p = get("nyte_position", owner["session_id"])
        reconcile(p, c["payment_state"])
        return p

    if op == "purchase":
        p = c["position"]
        session = p["session_id"]
        event = get("nyte_event", c["event_id"])
        if event and event["session_id"] != session:
            raise ValueError("Stripe event already allocated")
        existing = get("nyte_position", session)
        notification = get("reserved_notification", session)
        if existing:
            p = existing
            # Compatibility for positions written before allocation_status.
            p.setdefault("allocation_status", "historical" if notification.get("sent") and p.get("event_id", "").startswith("backfill:")
                         else "pending" if p.get("event_id", "").startswith("backfill:") else "complete")
        else:
            payment = p.get("payment_intent")
            if isinstance(payment, str) and payment:
                owner = get("nyte_payment", payment)
                if owner and owner["session_id"] != session:
                    raise ValueError("Stripe payment already belongs to a position")
                put("nyte_payment", payment, {"session_id": session})
            p.update(sold_lots=0, listed_lots=0, status=p.get("status", "held"), fills=[], listings=[], new_lots=p["lots"])
            holder = get("nyte_holder", p["holder"])
            p["holder_next"] = holder.get("head")
            put("nyte_holder", p["holder"], {"head": session})
            p["allocation_status"] = "historical" if c.get("backfill") and notification.get("sent") else "pending"
        if not existing or p.get("purchase_completed_at") is None:
            p["purchase_completed_at"] = notification.get("purchase_completed_at", c["position"].get("purchase_completed_at"))
        if c["position"].get("status") == "needs_review":
            block_position(p)
        reconcile(p, c.get("payment_state") or {})
        put("nyte_event", c["event_id"], {"session_id": session})
        put("nyte_position", session, p)
        if p["allocation_status"] != "pending" or c.get("backfill"):
            p.setdefault("event_id", c["event_id"])
            return p
        remaining = p["lots"]
        if c.get("allocate") and p["status"] == "held":
            queue = "grade:" + p["grade"]
            while remaining:
                key = get("nyte_queue", queue).get("head")
                if not key:
                    break
                listing = get("nyte_listing", key)
                seller = get("nyte_position", listing["position"])
                if seller["status"] != "held":
                    block_position(seller)
                    continue
                count = min(remaining, listing["lots_listed"] - listing["lots_sold"])
                fee, refund = refund_cents(p["per_lot_cents"], seller["per_lot_cents"], c["fee_bps"])
                fill_id = reference("nyte-fill:" + session + ":" + key)
                fill = {"id": fill_id, "kind": "resale_fill", "listing": key, "buyer_session": session,
                        "position_id": seller["id"],
                        "event_id": c["event_id"], "lots": count, "resale_price_cents": p["per_lot_cents"],
                        "fee_bps": c["fee_bps"], "fee_cents": fee * count,
                        "refund_amount_cents": refund * count, "payment_intent": seller["payment_intent"],
                        "stripe_refund_id": None, "status": "pending", "created_at": now}
                queue_append("nyte_fill", "refunds", fill_id, fill)
                p["fills"].append(fill_id)
                listing["lots_sold"] += count
                seller["sold_lots"] += count
                seller["listed_lots"] -= count
                remaining -= count
                if listing["lots_sold"] == listing["lots_listed"]:
                    listing["status"] = "sold"
                    queue_remove("nyte_listing", queue, key, listing)
                put("nyte_listing", key, listing)
                put("nyte_position", listing["position"], seller)
        p["allocation_status"] = "complete"
        p["new_lots"] = remaining
        p["event_id"] = c["event_id"]
        put("nyte_event", c["event_id"], {"session_id": session})
        put("nyte_position", session, p)
        return p

    if op == "positions":
        key = c.get("cursor") or get("nyte_holder", c["holder"]).get("head")
        positions: list[Row] = []
        while key and len(positions) < 100:
            p = get("nyte_position", key)
            if p.get("holder") != c["holder"]:
                raise ValueError("Invalid position cursor")
            positions.append({**p, "listing_rows": [get("nyte_listing", lid) for lid in p["listings"]]})
            key = p.get("holder_next")
        return {"positions": positions, "cursor": key}

    if op in {"list", "withdraw", "request_refund"}:
        p = get("nyte_position", c["position"])
        if not p or p["holder"] != c["holder"]:
            raise ValueError("Reservation unavailable")
        if op in {"list", "request_refund"} and (p["status"] == "needs_review" or p.get("allocation_status") == "pending"):
            raise ValueError("Reservation needs ops review or allocation completion")
        receipt = reference(c["holder"] + ":" + c["request_id"])
        previous = get("nyte_action", receipt)
        if previous:
            if previous["command"] != {k: v for k, v in c.items() if k != "now"}:
                raise ValueError("Request ID already used")
            return previous["result"]
        purchased = p.get("purchase_completed_at")
        if op == "list":
            if purchased is None:
                raise ValueError("Purchase time needs ops review")
            if now <= purchased + REFUND_WINDOW:
                raise ValueError("Within 30 days, request a refund instead")
            lots = c["lots"]
            if p["status"] != "held" or not p.get("payment_intent") or type(lots) is not int or not 1 <= lots <= p["lots"] - p["sold_lots"] - p["listed_lots"]:
                raise ValueError("Lots unavailable")
            lid = reference("nyte-list:" + receipt)
            listing = {"id": lid, "position": c["position"], "lots_listed": lots, "lots_sold": 0,
                       "listed_at": now, "status": "active"}
            queue_append("nyte_listing", "grade:" + p["grade"], lid, listing)
            p["listed_lots"] += lots
            p["listings"].append(lid)
            result = {"listing_id": lid, "status": "active"}
        elif op == "withdraw":
            listing = get("nyte_listing", c["listing"])
            if listing.get("position") != c["position"]:
                raise ValueError("Listing unavailable")
            if listing["status"] == "active":
                p["listed_lots"] -= listing["lots_listed"] - listing["lots_sold"]
                listing["status"] = "withdrawn"
                queue_remove("nyte_listing", "grade:" + p["grade"], c["listing"], listing)
            result = {"status": listing["status"]}
        else:
            if p["status"] == "refund_requested":
                return {"status": "refund_requested"}
            if purchased is None or not purchased <= now <= purchased + REFUND_WINDOW or p["status"] != "held" or p["sold_lots"] or p["listed_lots"]:
                raise ValueError("Full refund window has closed or reservation is unavailable")
            p.update(status="refund_requested", refund_requested_at=now)
            queue_append("nyte_refund_request", "full_refunds", c["position"], {"position": c["position"], "requested_at": now, "status": "pending"})
            result = {"status": "refund_requested"}
        put("nyte_position", c["position"], p)
        put("nyte_action", receipt, {"command": {k: v for k, v in c.items() if k != "now"}, "result": result})
        return result

    if op == "payment":
        return get("nyte_payment", c["payment_intent"])

    if op == "position":
        return get("nyte_position", c["position"])

    if op == "pending":
        queue = c.get("queue", "refunds")
        kind = "nyte_fill" if queue in {"refunds", "refunds_needs_ops"} else "nyte_refund_request"
        key = c.get("cursor") or get("nyte_queue", queue).get("head")
        rows: list[Row] = []
        while key and len(rows) < 100:
            row = get(kind, key)
            rows.append(row)
            key = row.get("next")
        return {"rows": rows, "cursor": key}

    def obligation(kind: str, key: str) -> Row:
        if kind not in {"resale_fill", "full_refund"}:
            return {}
        row = get("nyte_fill" if kind == "resale_fill" else "nyte_refund_request", key)
        if not row:
            return {}
        row.update(id=key, kind=kind)
        if kind == "full_refund":
            p = get("nyte_position", key)
            row.setdefault("status", "refunded" if p["status"] == "refunded" else "pending")
            row.update(payment_intent=p["payment_intent"], refund_amount_cents=p["lots"] * p["per_lot_cents"],
                       position_id=p.get("id", key))
            row.setdefault("stripe_refund_id", p.get("stripe_refund_id"))
        elif "position_id" not in row:
            listing = get("nyte_listing", row["listing"])
            row["position_id"] = get("nyte_position", listing["position"])["id"]
        return row

    def location(row: Row) -> tuple[str, str, str]:
        return (("nyte_fill", "refunds", "refunds_needs_ops") if row["kind"] == "resale_fill"
                else ("nyte_refund_request", "full_refunds", "full_refunds_needs_ops"))

    def save(row: Row) -> None:
        put(location(row)[0], row["id"], row)

    def history(row: Row, action: str, target: str) -> None:
        row.setdefault("history", []).append({"at": now, "action": action, "from": row["status"], "to": target})
        save(row)

    def transition(row: Row, target: str, *, owner: str | None = None, reason: str | None = None) -> Row:
        """The sole completion rule for every NYTE refund obligation."""
        entity, active, ops = location(row)
        if target == "needs_ops":
            if row["status"] != "needs_ops":
                history(row, "refund_failure", target)
                if row["status"] != "refunded":
                    queue_remove(entity, active, row["id"], row)
                queue_append(entity, ops, row["id"], row)
            row.update(status=target, failure_status=reason, owner=None, lease_until=0)
        else:
            if row["status"] != "in_flight" or (owner is not None and row.get("owner") != owner):
                history(row, "stale_write", target)
                return {}
            if target not in {"refunded", "retry"}:
                raise ValueError("Invalid refund transition")
            history(row, "worker_finish", target)
            row.update(status=target, owner=None, lease_until=0)
            if target == "refunded":
                queue_remove(entity, active, row["id"], row)
        save(row)
        if row["kind"] == "full_refund" and target in {"refunded", "needs_ops"}:
            p = get("nyte_position", row["id"])
            p.update(status=target, stripe_refund_id=row["stripe_refund_id"])
            put("nyte_position", row["id"], p)
        return row

    def indexed_identity(index: Row) -> tuple[str, str]:
        # Compatibility with the old fill-only refund index.
        return ("resale_fill", index["fill"]) if "fill" in index else (index.get("kind", ""), index.get("id", ""))

    def bind(row: Row, refund: Row) -> bool:
        rid = refund.get("id")
        if (not rid or refund.get("payment_intent") != row["payment_intent"]
                or refund.get("amount") != row["refund_amount_cents"]):
            return False
        index = get("nyte_refund", rid)
        if index and indexed_identity(index) != (row["kind"], row["id"]):
            return False
        put("nyte_refund", rid, {"kind": row["kind"], "id": row["id"],
                                "payment_intent": row["payment_intent"], "amount": row["refund_amount_cents"]})
        row["stripe_refund_id"] = rid
        save(row)
        # A metadata-free Dashboard failure can precede acknowledgement/binding.
        failure = get("nyte_refund_failure", rid)
        if (failure and failure["payment_intent"] == row["payment_intent"] and failure["amount"] == row["refund_amount_cents"]
                and (not failure.get("fill_id") or (row["kind"] == "resale_fill" and failure["fill_id"] == row["id"]))
                and (not failure.get("position_id") or (row["kind"] == "full_refund" and failure["position_id"] == row["position_id"]))):
            transition(row, "needs_ops", reason=failure["status"])
        return True

    def apply_refund(row: Row, refund: Row, *, owner: str | None = None) -> Row:
        """Verify identity, bind and apply any API response or lifecycle event."""
        if not bind(row, refund):
            return {}
        status = refund.get("status")
        if status in {"failed", "canceled"}:
            return transition(row, "needs_ops", reason=status)
        if status == "succeeded":
            return transition(row, "refunded", owner=owner)
        return row

    if op in {"refund_lifecycle", "refund_failed"}:
        refund = c["refund"]
        rid, status = refund.get("id"), refund.get("status")
        payment = get("nyte_payment", refund.get("payment_intent") or "")
        if not payment:
            return {}
        if not rid or status not in {"pending", "requires_action", "succeeded", "failed", "canceled"}:
            return {}
        if op == "refund_failed" and status not in {"failed", "canceled"}:
            return {}
        index = get("nyte_refund", rid)
        metadata = refund.get("metadata") or {}
        kind, key = indexed_identity(index)
        if not key and metadata.get("kind") == "full_refund" and metadata.get("position_id"):
            p = get("nyte_position", payment["session_id"])
            kind, key = ("full_refund", p["session_id"]) if p.get("id") == metadata["position_id"] else ("", "")
        elif not key and (metadata.get("fill_id") or metadata.get("nyte_fill_id")):
            kind, key = "resale_fill", metadata.get("fill_id") or metadata["nyte_fill_id"]
        elif not key:
            # Previously acknowledged full refunds predate the shared index.
            p = get("nyte_position", payment["session_id"])
            if p.get("stripe_refund_id") == rid:
                kind, key = "full_refund", p["session_id"]
        row = obligation(kind, key)
        if not row:
            if (status in {"failed", "canceled"}
                    and type(refund.get("amount")) is int and not get("nyte_refund_failure", rid)):
                put("nyte_refund_failure", rid, {"payment_intent": refund["payment_intent"],
                                               "amount": refund["amount"], "status": status, "observed_at": now,
                                               "fill_id": metadata.get("fill_id") or metadata.get("nyte_fill_id"),
                                               "position_id": metadata.get("position_id") if metadata.get("kind") == "full_refund" else None})
            return {}
        result = apply_refund(row, refund)
        # A validated stale success is handled, but cannot close ops work.
        if not result and get("nyte_refund", rid) and indexed_identity(get("nyte_refund", rid)) == (kind, key):
            if refund.get("payment_intent") == row["payment_intent"] and refund.get("amount") == row["refund_amount_cents"]:
                return row
        return result

    if op == "refund_obligation":
        return obligation(c["kind"], c["id"])

    if op == "refund_acknowledge":
        row = obligation("full_refund", c["position"])
        refund = c["refund"]
        if not row or not bind(row, refund):
            raise ValueError("Stripe refund does not match a full original-payment refund request")
        if row["status"] in {"pending", "retry"}:
            history(row, "claim", "in_flight")
            row.update(status="in_flight", owner=c["owner"], lease_until=now + 120)
            save(row)
        apply_refund(row, refund)
        return row

    if op == "fill_claim":
        row = obligation("resale_fill", c["fill"])
        if not row or row["status"] in {"refunded", "needs_ops"} or row.get("lease_until", 0) > now:
            return {}
        history(row, "claim", "in_flight")
        row.update(status="in_flight", owner=c["owner"], lease_until=now + 120)
        row.setdefault("first_attempt_at", now)
        save(row)
        return row

    if op == "fill_finish":
        row = obligation("resale_fill", c["fill"])
        if not row:
            return {}
        rid = c.get("stripe_refund_id") or row.get("stripe_refund_id")
        target = c["status"]
        if rid:
            status = "succeeded" if target == "refunded" else c.get("failure_status") if target == "needs_ops" else "pending"
            result = apply_refund(row, {"id": rid, "payment_intent": row["payment_intent"],
                                       "amount": row["refund_amount_cents"], "status": status}, owner=c["owner"])
            if target != "retry" or not result:
                return result
        # Transport retries and zero-cent capped fills still obey the same CAS.
        if target == "refunded" and row["refund_amount_cents"] != 0 and row["status"] == "in_flight" and row.get("owner") == c["owner"]:
            raise ValueError("A paid refund requires its Stripe refund ID")
        return transition(row, target, owner=c["owner"])

    if op == "token_create":
        put("nyte_access", c["key"], {"holder": c["holder"], "expires_at": now + 900, "used": False})
        return {}
    if op == "token_consume":
        token = get("nyte_access", c["key"])
        if not token or token["used"] or token["expires_at"] <= now:
            raise ValueError("Link expired or already used; request a new link")
        token["used"] = True
        put("nyte_access", c["key"], token)
        put("nyte_access", c["session"], {"holder": token["holder"], "expires_at": now + 900, "session": True})
        return {"holder": token["holder"]}
    if op == "session":
        token = get("nyte_access", c["key"])
        if not token.get("session") or token["expires_at"] <= now:
            raise ValueError("Session expired; request a new link")
        return {"holder": token["holder"]}
    raise ValueError("Unknown reservation command")
