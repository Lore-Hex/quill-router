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

    if op == "purchase":
        p = c["position"]
        session = p["session_id"]
        event = get("nyte_event", c["event_id"])
        if event and event["session_id"] != session:
            raise ValueError("Stripe event already allocated")
        existing = get("nyte_position", session)
        if existing and (existing.get("purchase_completed_at") is not None or c.get("backfill")):
            put("nyte_event", c["event_id"], {"session_id": session})
            return existing
        # A Stripe import may race the first paid webhook. Complete the
        # provisional row in that webhook's allocation transaction without
        # linking the holder index to itself.
        provisional = existing if existing else None
        payment = p.get("payment_intent")
        if isinstance(payment, str) and payment:
            owner = get("nyte_payment", payment)
            if owner and owner["session_id"] != session:
                raise ValueError("Stripe payment already belongs to a position")
            put("nyte_payment", payment, {"session_id": session})
        # Read the old notification key on import, never session.created.
        notification = get("reserved_notification", session)
        p["purchase_completed_at"] = notification.get("purchase_completed_at", p.get("purchase_completed_at"))
        if provisional and provisional.get("status") == "needs_review":
            p["status"] = "needs_review"
        p.update(sold_lots=0, listed_lots=0, status=p.get("status", "held"), fills=[], listings=[])
        holder = get("nyte_holder", p["holder"])
        p["holder_next"] = provisional.get("holder_next") if provisional else holder.get("head")
        if not provisional:
            put("nyte_holder", p["holder"], {"head": session})
        remaining = p["lots"]
        if c.get("allocate"):
            queue = "grade:" + p["grade"]
            while remaining:
                key = get("nyte_queue", queue).get("head")
                if not key:
                    break
                listing = get("nyte_listing", key)
                seller = get("nyte_position", listing["position"])
                count = min(remaining, listing["lots_listed"] - listing["lots_sold"])
                fee, refund = refund_cents(p["per_lot_cents"], seller["per_lot_cents"], c["fee_bps"])
                fill_id = reference("nyte-fill:" + session + ":" + key)
                fill = {"id": fill_id, "listing": key, "buyer_session": session,
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
            queue_append("nyte_refund_request", "full_refunds", c["position"], {"position": c["position"], "requested_at": now})
            result = {"status": "refund_requested"}
        put("nyte_position", c["position"], p)
        put("nyte_action", receipt, {"command": {k: v for k, v in c.items() if k != "now"}, "result": result})
        return result

    if op == "refund_complete":
        p = get("nyte_position", c["position"])
        if p.get("status") == "refunded" and p.get("stripe_refund_id") == c["stripe_refund_id"]:
            return p
        if p.get("status") != "refund_requested":
            raise ValueError("No full refund request")
        request = get("nyte_refund_request", c["position"])
        p.update(status="refunded", stripe_refund_id=c["stripe_refund_id"])
        queue_remove("nyte_refund_request", "full_refunds", c["position"], request)
        put("nyte_position", c["position"], p)
        return p

    if op == "payment":
        return get("nyte_payment", c["payment_intent"])

    if op == "position":
        return get("nyte_position", c["position"])

    if op == "pending":
        queue = c.get("queue", "refunds")
        kind = "nyte_fill" if queue == "refunds" else "nyte_refund_request"
        key = c.get("cursor") or get("nyte_queue", queue).get("head")
        rows: list[Row] = []
        while key and len(rows) < 100:
            row = get(kind, key)
            rows.append(row)
            key = row.get("next")
        return {"rows": rows, "cursor": key}

    if op == "fill_claim":
        fill = get("nyte_fill", c["fill"])
        if not fill or fill["status"] == "refunded" or fill.get("lease_until", 0) > now:
            return {}
        fill.update(owner=c["owner"], lease_until=now + 120)
        fill.setdefault("first_attempt_at", now)
        put("nyte_fill", c["fill"], fill)
        return fill

    if op == "fill_finish":
        fill = get("nyte_fill", c["fill"])
        if fill.get("owner") != c["owner"]:
            return {}
        fill.update(status=c["status"], stripe_refund_id=c.get("stripe_refund_id") or fill.get("stripe_refund_id"), lease_until=0)
        if c["status"] == "refunded":
            queue_remove("nyte_fill", "refunds", c["fill"], fill)
        put("nyte_fill", c["fill"], fill)
        return fill

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
