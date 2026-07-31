from __future__ import annotations

import datetime as dt
import threading
import uuid

from trusted_router.google_ads_conversions import (
    build_google_ads_conversion,
    is_google_ads_direct_delivery,
)
from trusted_router.storage_models import (
    AcquisitionAttribution,
    GoogleAdsConversion,
    iso_now,
)


class InMemoryAcquisitionAttribution:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.records: dict[str, AcquisitionAttribution] = {}
        self.google_ads_conversions: dict[str, GoogleAdsConversion] = {}

    def reset(self) -> None:
        self.records.clear()
        self.google_ads_conversions.clear()

    def create(self, record: AcquisitionAttribution) -> bool:
        with self._lock:
            if record.workspace_id in self.records:
                return False
            self.records[record.workspace_id] = record
            self._record_google_conversion(
                record,
                "signup_completed",
                occurred_at=record.signup_at,
            )
            return True

    def get(self, workspace_id: str) -> AcquisitionAttribution | None:
        with self._lock:
            return self.records.get(workspace_id)

    def claim_milestones(
        self,
        workspace_id: str,
        milestones: list[str],
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, list[str]]:
        with self._lock:
            record = self.records.get(workspace_id)
            if record is None:
                return None, []
            claimed: list[str] = []
            for name in milestones:
                if name not in record.milestones and name not in claimed:
                    claimed.append(name)
            for name in claimed:
                record.milestones[name] = occurred_at
                self._record_google_conversion(
                    record,
                    name,
                    occurred_at=occurred_at,
                )
            if claimed:
                record.updated_at = iso_now()
            return record, claimed

    def record_purchase(
        self,
        workspace_id: str,
        *,
        amount_microdollars: int,
        occurred_at: str,
    ) -> AcquisitionAttribution | None:
        with self._lock:
            record = self.records.get(workspace_id)
            if record is None:
                return None
            record.purchase_count += 1
            record.purchase_microdollars += amount_microdollars
            record.first_purchase_at = record.first_purchase_at or occurred_at
            record.last_purchase_at = occurred_at
            record.updated_at = iso_now()
            self._record_google_conversion(
                record,
                "credit_purchase_completed",
                occurred_at=occurred_at,
                value_microdollars=amount_microdollars,
                ordinal=record.purchase_count,
            )
            return record

    def list_google_ads_conversions(
        self,
        *,
        since: str,
        limit: int,
    ) -> list[GoogleAdsConversion]:
        since_at = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        with self._lock:
            rows = [
                conversion
                for conversion in self.google_ads_conversions.values()
                if dt.datetime.fromisoformat(conversion.occurred_at.replace("Z", "+00:00"))
                >= since_at
            ]
            rows.sort(key=lambda item: (item.occurred_at, item.order_id))
            return rows[:limit]

    def backfill_google_ads_conversions(self, *, limit: int) -> int:
        """Backfill deterministic non-purchase events from existing records.

        Historic individual purchases cannot be reconstructed from the
        aggregate attribution row without risking duplicate or invented
        conversions, so purchase export starts when this pipeline is deployed.
        """
        created = 0
        with self._lock:
            records = sorted(
                self.records.values(),
                key=lambda item: (item.signup_at, item.workspace_id),
            )[:limit]
            for record in records:
                events = [("signup_completed", record.signup_at)]
                events.extend(
                    (name, occurred_at)
                    for name, occurred_at in record.milestones.items()
                    if name
                    in {
                        "first_successful_api_call",
                        "retained_api_usage_7d",
                    }
                )
                for event, occurred_at in events:
                    conversion = build_google_ads_conversion(
                        record,
                        event,
                        occurred_at=occurred_at,
                    )
                    if (
                        conversion is not None
                        and conversion.order_id not in self.google_ads_conversions
                    ):
                        self.google_ads_conversions[conversion.order_id] = conversion
                        created += 1
        return created

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]:
        now = iso_now()
        owner = f"gdm_{uuid.uuid4().hex}"
        leased_until = _iso_after_seconds(lease_seconds)
        with self._lock:
            rows = [
                conversion
                for conversion in self.google_ads_conversions.values()
                if _google_delivery_is_due(conversion, now)
            ]
            rows.sort(
                key=lambda item: (
                    item.next_attempt_at,
                    item.occurred_at,
                    item.order_id,
                )
            )
            claimed = rows[:limit]
            for conversion in claimed:
                conversion.lease_owner = owner
                conversion.leased_until = leased_until
                conversion.updated_at = now
            return claimed

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None:
        del occurred_at
        with self._lock:
            conversion = self.google_ads_conversions.get(order_id)
            if conversion is None or conversion.lease_owner != lease_owner:
                return None
            conversion.delivery_status = "submitted"
            conversion.delivery_attempts += 1
            conversion.last_error = None
            conversion.lease_owner = None
            conversion.leased_until = None
            conversion.google_request_id = request_id
            conversion.submitted_at = iso_now()
            conversion.updated_at = conversion.submitted_at
            return conversion

    def mark_google_ads_delivery_failed(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> GoogleAdsConversion | None:
        del occurred_at
        with self._lock:
            conversion = self.google_ads_conversions.get(order_id)
            if conversion is None or conversion.lease_owner != lease_owner:
                return None
            conversion.delivery_attempts += 1
            conversion.last_error = error[:500]
            conversion.lease_owner = None
            conversion.leased_until = None
            conversion.google_request_id = None
            conversion.submitted_at = None
            conversion.updated_at = iso_now()
            if retryable and conversion.delivery_attempts < max_attempts:
                conversion.delivery_status = "pending"
                conversion.next_attempt_at = _iso_after_seconds(
                    _google_delivery_backoff_seconds(
                        conversion.delivery_attempts
                    )
                )
            else:
                conversion.delivery_status = "dead"
            return conversion

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        since_at = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        repaired = 0
        with self._lock:
            rows = sorted(
                self.google_ads_conversions.values(),
                key=lambda item: (item.occurred_at, item.order_id),
            )
            for conversion in rows:
                if repaired >= limit:
                    break
                occurred_at = dt.datetime.fromisoformat(
                    conversion.occurred_at.replace("Z", "+00:00")
                )
                if occurred_at < since_at:
                    continue
                if (
                    is_google_ads_direct_delivery(conversion)
                    and conversion.delivery_status == "not_scheduled"
                ):
                    conversion.delivery_status = "pending"
                    conversion.next_attempt_at = iso_now()
                    conversion.updated_at = conversion.next_attempt_at
                    repaired += 1
        return repaired

    def _record_google_conversion(
        self,
        record: AcquisitionAttribution,
        event: str,
        *,
        occurred_at: str,
        value_microdollars: int = 0,
        ordinal: int = 0,
    ) -> None:
        conversion = build_google_ads_conversion(
            record,
            event,
            occurred_at=occurred_at,
            value_microdollars=value_microdollars,
            ordinal=ordinal,
        )
        if conversion is not None:
            self.google_ads_conversions.setdefault(conversion.order_id, conversion)


def _google_delivery_backoff_seconds(attempts: int) -> int:
    return min(6 * 60 * 60, 30 * (2 ** max(attempts - 1, 0)))


def _iso_after_seconds(seconds: int) -> str:
    return (
        dt.datetime.now(dt.UTC).replace(microsecond=0)
        + dt.timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _google_delivery_is_due(
    conversion: GoogleAdsConversion,
    now: str,
) -> bool:
    if conversion.delivery_status != "pending":
        return False
    if conversion.next_attempt_at > now:
        return False
    return not conversion.leased_until or conversion.leased_until <= now
