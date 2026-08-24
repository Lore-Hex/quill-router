from __future__ import annotations

import datetime as dt
import threading
import uuid

from trusted_router.google_ads_conversions import build_google_ads_conversion
from trusted_router.storage_models import (
    AcquisitionAttribution,
    ActivationReminderTask,
    GoogleAdsConversion,
    activation_reminder_tasks,
    iso_now,
)


class InMemoryAcquisitionAttribution:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.records: dict[str, AcquisitionAttribution] = {}
        self.reminders: dict[str, ActivationReminderTask] = {}
        self.google_ads_conversions: dict[str, GoogleAdsConversion] = {}
        self.google_click_expirations: dict[str, str] = {}

    def reset(self) -> None:
        self.records.clear()
        self.reminders.clear()
        self.google_ads_conversions.clear()
        self.google_click_expirations.clear()

    def create(self, record: AcquisitionAttribution) -> bool:
        with self._lock:
            if record.workspace_id in self.records:
                return False
            self.records[record.workspace_id] = record
            if record.encrypted_google_click_id and record.google_click_expires_at:
                self.google_click_expirations[
                    f"{record.google_click_expires_at}#{record.workspace_id}"
                ] = record.workspace_id
            for reminder in activation_reminder_tasks(record):
                self.reminders[reminder.id] = reminder
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
            claimed = [
                name
                for name in dict.fromkeys(milestones)
                if name not in record.milestones
            ]
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

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        since_at = _parse_timestamp(since)
        repaired = 0
        with self._lock:
            rows = sorted(
                self.google_ads_conversions.values(),
                key=lambda item: (item.occurred_at, item.order_id),
            )
            for conversion in rows:
                if repaired >= limit:
                    break
                if _parse_timestamp(conversion.occurred_at) < since_at:
                    continue
                if conversion.delivery_status == "not_scheduled":
                    conversion.delivery_status = "pending"
                    conversion.next_attempt_at = iso_now()
                    conversion.last_error = None
                    conversion.updated_at = conversion.next_attempt_at
                    repaired += 1
        return repaired

    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int:
        purged = 0
        with self._lock:
            for pointer_id in sorted(self.google_click_expirations):
                if purged >= limit or pointer_id.split("#", 1)[0] > before:
                    break
                workspace_id = self.google_click_expirations.pop(pointer_id)
                record = self.records.get(workspace_id)
                if (
                    record is not None
                    and record.google_click_expires_at is not None
                    and record.google_click_expires_at <= before
                ):
                    record.google_click_id_kind = None
                    record.encrypted_google_click_id = None
                    record.google_click_expires_at = None
                    record.updated_at = iso_now()
                purged += 1
        return purged

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
            for conversion in self.google_ads_conversions.values():
                if (
                    conversion.delivery_status == "pending"
                    and conversion.click_expires_at is not None
                    and conversion.click_expires_at <= now
                ):
                    conversion.delivery_status = "dead"
                    conversion.encrypted_click_id = None
                    conversion.last_error = "google_click_identifier_expired"
                    conversion.updated_at = now
            rows = [
                conversion
                for conversion in self.google_ads_conversions.values()
                if _google_delivery_is_due(conversion, now)
            ]
            rows.sort(key=lambda item: (item.next_attempt_at, item.occurred_at, item.order_id))
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
            conversion.encrypted_click_id = None
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
                    _google_delivery_backoff_seconds(conversion.delivery_attempts)
                )
            else:
                conversion.delivery_status = "dead"
                conversion.encrypted_click_id = None
            return conversion

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

    def list_reminders(self, *, limit: int) -> list[ActivationReminderTask]:
        with self._lock:
            return sorted(self.reminders.values(), key=lambda item: item.id)[:limit]

    def delete_reminders(self, reminder_ids: list[str]) -> None:
        with self._lock:
            for reminder_id in reminder_ids:
                self.reminders.pop(reminder_id, None)

    def claim_reminder(
        self,
        workspace_id: str,
        stage: str,
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, bool]:
        milestone = f"activation_reminder_{stage}_sent"
        with self._lock:
            record = self.records.get(workspace_id)
            if record is None:
                return None, False
            if (
                "first_successful_api_call" in record.milestones
                or milestone in record.milestones
            ):
                return record, False
            record.milestones[milestone] = occurred_at
            record.updated_at = iso_now()
            return record, True


def _google_delivery_backoff_seconds(attempts: int) -> int:
    return min(6 * 60 * 60, 30 * (2 ** max(attempts - 1, 0)))


def _iso_after_seconds(seconds: int) -> str:
    return (
        dt.datetime.now(dt.UTC).replace(microsecond=0)
        + dt.timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _google_delivery_is_due(conversion: GoogleAdsConversion, now: str) -> bool:
    if conversion.delivery_status != "pending" or conversion.next_attempt_at > now:
        return False
    return not conversion.leased_until or conversion.leased_until <= now
