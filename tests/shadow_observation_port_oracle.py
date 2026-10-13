"""Round-seven full-validation paths, retained only as differential oracles."""
import copy
from typing import Any

from trusted_router.async_settle_shadow_v2_a1_evidence import COUNTER
from trusted_router.async_settle_shadow_v2_evidence import BUCKET, COUNTS, EVENT
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.services.async_settle_shadow_health import Port


class FullPort(Port):
    def fold_counts(self, day: str, bucket: dict[str, Any]) -> bool:
        """Fold one P1 BUCKET delta atomically, including eligible demand.

        Unknown demand remains unknown; no cumulative field can be decremented.
        P4 supplies each attempt exactly once after its immutable result exists.
        """
        def action() -> bool:
            BUCKET(bucket)
            svc = self.service
            require(not svc.journal.days[day].closed and not svc.journal.days[day].sealing, "closed_day")
            body = copy.deepcopy(svc.counters[day])
            target = next((v for v in body["buckets"] if (v["phase"], v["dimensions"]) ==
                           (bucket["phase"], bucket["dimensions"])), None)
            if target is None:
                target = dict(phase=bucket["phase"], dimensions=copy.deepcopy(bucket["dimensions"]),
                              **dict.fromkeys(COUNTS, 0), eligible_demand=0)
                body["buckets"].append(target)
            for key in COUNTS:
                target[key] += bucket[key]
                body[key] += bucket[key]
            for obj in (target, body):
                obj["eligible_demand"] = (None if obj["eligible_demand"] is None or bucket["eligible_demand"] is None
                                          else obj["eligible_demand"] + bucket["eligible_demand"])
            dims = bucket["dimensions"]
            legacy = next(v for v in body["counts"] if all(v[k] == ("unknown" if dims[k] is None and k != "streamed" else dims[k]) for k in dims))
            for key in ("authorize_attempts", "authorize_fresh", "authorize_replay", "settle_attempts", "refund_attempts"):
                legacy[key] += bucket[key]
            terminals = bucket["settle_attempts"] + bucket["refund_attempts"]
            legacy["observed_attempts"] += terminals
            eligible = bucket["eligible_demand"] or 0
            legacy["observed_eligible"] += eligible
            legacy["observed_ineligible"] += bucket["B"]
            legacy["observed_unknown"] += terminals - eligible - bucket["B"]
            # Validate the prospective checkpoint without creating an attempt.
            check = dict(body, sequence=max(1, body["sequence"]), checkpoint_seq=max(1, body["sequence"]),
                         checkpoint_attempts=max(1, body["checkpoint_attempts"]),
                         flushed_at_us=body["started_at_us"])
            COUNTER(check)
            svc.counters[day] = body
            return True
        return bool(self.call(action))

    def finalize(self, reservation, event):
        def action():
            require(not self.service.journal.days[reservation.day].sealing, "closed_day")
            slot = self.service.journal.slot(reservation)
            body = EVENT.serialize(event)
            require(event["seq"] == f"{reservation.event_seq:020}" and event["event_at_us"] == reservation.observed_us, "reservation_identity")
            if event["receipt_locator"] is not None:
                require(event["receipt_locator"] == reservation.coordinates, "reservation_identity")
            require(slot.body is None or slot.body == body, "slot_conflict")
            slot.body = body
            return body
        return self.call(action)
