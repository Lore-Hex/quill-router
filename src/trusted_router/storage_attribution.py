from __future__ import annotations

import datetime as dt
import threading

from trusted_router.google_ads_conversions import build_google_ads_conversion
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
