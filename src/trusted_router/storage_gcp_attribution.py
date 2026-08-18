from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from trusted_router.google_ads_conversions import (
    build_google_ads_conversion,
    google_ads_conversion_entity_id,
    google_ads_conversion_kind,
    google_ads_conversion_kinds_since,
    parse_utc_timestamp,
)
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import (
    AcquisitionAttribution,
    ActivationReminderTask,
    GoogleAdsConversion,
    activation_reminder_tasks,
    iso_now,
)

_KIND = "acquisition_attribution"
_REMINDER_KIND = "activation_reminder"
_GOOGLE_DELIVERY_DUE_KIND = "google_ads_conversion_due_v2"
_GOOGLE_CLICK_EXPIRY_KIND = "google_ads_click_expiry_v2"


class SpannerAcquisitionAttribution:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(self, record: AcquisitionAttribution) -> bool:
        def txn(transaction: Any) -> bool:
            existing = self._io.read_entity_tx(
                transaction,
                _KIND,
                record.workspace_id,
                AcquisitionAttribution,
            )
            if existing is not None:
                return False
            self._io.write_entity_tx(transaction, _KIND, record.workspace_id, record)
            if record.encrypted_google_click_id and record.google_click_expires_at:
                self._io.write_entity_tx(
                    transaction,
                    _GOOGLE_CLICK_EXPIRY_KIND,
                    _google_click_expiry_id(record),
                    {
                        "workspace_id": record.workspace_id,
                        "expires_at": record.google_click_expires_at,
                    },
                )
            for reminder in activation_reminder_tasks(record):
                self._io.write_entity_tx(
                    transaction,
                    _REMINDER_KIND,
                    reminder.id,
                    reminder,
                )
            self._write_google_conversion_tx(
                transaction,
                record,
                "signup_completed",
                occurred_at=record.signup_at,
            )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int:
        pointers = self._io.list_entities(
            _GOOGLE_CLICK_EXPIRY_KIND,
            cls=dict,
            limit=limit,
        )
        purged = 0
        for pointer in pointers:
            expires_at = str(pointer.get("expires_at", ""))
            workspace_id = str(pointer.get("workspace_id", ""))
            if not expires_at or not workspace_id or expires_at > before:
                break
            pointer_id = f"{expires_at}#{workspace_id}"

            def txn(
                transaction: Any,
                workspace_id: str = workspace_id,
                expires_at: str = expires_at,
                pointer_id: str = pointer_id,
            ) -> int:
                record = self._io.read_entity_tx(
                    transaction,
                    _KIND,
                    workspace_id,
                    AcquisitionAttribution,
                )
                if (
                    record is not None
                    and record.google_click_expires_at is not None
                    and record.google_click_expires_at <= before
                ):
                    record.google_click_id_kind = None
                    record.encrypted_google_click_id = None
                    record.google_click_expires_at = None
                    record.updated_at = iso_now()
                    self._io.write_entity_tx(
                        transaction,
                        _KIND,
                        workspace_id,
                        record,
                    )
                self._io.delete_entities_tx(
                    transaction,
                    _GOOGLE_CLICK_EXPIRY_KIND,
                    [pointer_id],
                )
                return 1

            purged += run_in_transaction_with_retry(self._io.database, txn)
        return purged

    def get(self, workspace_id: str) -> AcquisitionAttribution | None:
        return self._io.read_entity(_KIND, workspace_id, AcquisitionAttribution)

    def claim_milestones(
        self,
        workspace_id: str,
        milestones: list[str],
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, list[str]]:
        def txn(
            transaction: Any,
        ) -> tuple[AcquisitionAttribution | None, list[str]]:
            record = self._io.read_entity_tx(
                transaction,
                _KIND,
                workspace_id,
                AcquisitionAttribution,
            )
            if record is None:
                return None, []
            claimed = [
                name
                for name in dict.fromkeys(milestones)
                if name not in record.milestones
            ]
            for name in claimed:
                record.milestones[name] = occurred_at
                self._write_google_conversion_tx(
                    transaction,
                    record,
                    name,
                    occurred_at=occurred_at,
                )
            if claimed:
                record.updated_at = iso_now()
                self._io.write_entity_tx(transaction, _KIND, workspace_id, record)
            return record, claimed

        return run_in_transaction_with_retry(self._io.database, txn)

    def record_purchase(
        self,
        workspace_id: str,
        *,
        amount_microdollars: int,
        occurred_at: str,
    ) -> AcquisitionAttribution | None:
        def txn(transaction: Any) -> AcquisitionAttribution | None:
            record = self._io.read_entity_tx(
                transaction,
                _KIND,
                workspace_id,
                AcquisitionAttribution,
            )
            if record is None:
                return None
            record.purchase_count += 1
            record.purchase_microdollars += amount_microdollars
            record.first_purchase_at = record.first_purchase_at or occurred_at
            record.last_purchase_at = occurred_at
            record.updated_at = iso_now()
            self._io.write_entity_tx(transaction, _KIND, workspace_id, record)
            self._write_google_conversion_tx(
                transaction,
                record,
                "credit_purchase_completed",
                occurred_at=occurred_at,
                value_microdollars=amount_microdollars,
                ordinal=record.purchase_count,
            )
            return record

        return run_in_transaction_with_retry(self._io.database, txn)

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        since_at = parse_utc_timestamp(since)
        repaired = 0
        for kind in google_ads_conversion_kinds_since(since_at):
            if repaired >= limit:
                break
            rows = self._io.list_entities(
                kind,
                cls=GoogleAdsConversion,
                limit=max(limit - repaired, 1),
            )
            for candidate in rows:
                if repaired >= limit:
                    break
                if parse_utc_timestamp(candidate.occurred_at) < since_at:
                    continue
                conversion_id = google_ads_conversion_entity_id(candidate)

                def txn(
                    transaction: Any,
                    conversion_kind: str = kind,
                    conversion_id: str = conversion_id,
                ) -> int:
                    conversion = self._io.read_entity_tx(
                        transaction,
                        conversion_kind,
                        conversion_id,
                        GoogleAdsConversion,
                    )
                    if conversion is None:
                        return 0
                    if conversion.delivery_status == "not_scheduled":
                        conversion.delivery_status = "pending"
                        conversion.next_attempt_at = iso_now()
                        conversion.last_error = None
                        conversion.updated_at = conversion.next_attempt_at
                        self._io.write_entity_tx(
                            transaction,
                            conversion_kind,
                            conversion_id,
                            conversion,
                        )
                    if conversion.delivery_status != "pending":
                        return 0
                    due_id = _google_delivery_due_id(conversion)
                    existing = self._io.read_entity_tx(
                        transaction,
                        _GOOGLE_DELIVERY_DUE_KIND,
                        due_id,
                        dict,
                    )
                    if existing is not None:
                        return 0
                    self._write_google_delivery_pointer_tx(
                        transaction,
                        conversion_kind=conversion_kind,
                        conversion_id=conversion_id,
                        conversion=conversion,
                    )
                    return 1

                repaired += run_in_transaction_with_retry(self._io.database, txn)
        return repaired

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]:
        pointers = self._io.list_entities(
            _GOOGLE_DELIVERY_DUE_KIND,
            prefix="pending#",
            cls=dict,
            limit=max(limit * 20, limit),
        )
        now = iso_now()
        candidates = [
            pointer
            for pointer in pointers
            if str(pointer.get("next_attempt_at", "")) <= now
        ]
        owner = f"gdm_{uuid.uuid4().hex}"
        leased_until = _iso_after_seconds(lease_seconds)
        claimed: list[GoogleAdsConversion] = []
        for pointer in candidates:
            if len(claimed) >= limit:
                break
            conversion = self._claim_google_ads_delivery(
                conversion_kind=str(pointer.get("conversion_kind", "")),
                conversion_id=str(pointer.get("conversion_id", "")),
                owner=owner,
                leased_until=leased_until,
                now=now,
            )
            if conversion is not None:
                claimed.append(conversion)
        return claimed

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None:
        conversion_kind = google_ads_conversion_kind(occurred_at)
        conversion_id = _google_conversion_id(order_id, occurred_at)

        def txn(transaction: Any) -> GoogleAdsConversion | None:
            conversion = self._io.read_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                GoogleAdsConversion,
            )
            if conversion is None or conversion.lease_owner != lease_owner:
                return None
            due_id = _google_delivery_due_id(conversion)
            submitted_at = iso_now()
            conversion.delivery_status = "submitted"
            conversion.delivery_attempts += 1
            conversion.last_error = None
            conversion.lease_owner = None
            conversion.leased_until = None
            conversion.google_request_id = request_id
            conversion.submitted_at = submitted_at
            conversion.updated_at = submitted_at
            conversion.encrypted_click_id = None
            self._io.write_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                conversion,
            )
            self._io.delete_entities_tx(
                transaction,
                _GOOGLE_DELIVERY_DUE_KIND,
                [due_id],
            )
            return conversion

        return run_in_transaction_with_retry(self._io.database, txn)

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
        conversion_kind = google_ads_conversion_kind(occurred_at)
        conversion_id = _google_conversion_id(order_id, occurred_at)

        def txn(transaction: Any) -> GoogleAdsConversion | None:
            conversion = self._io.read_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                GoogleAdsConversion,
            )
            if conversion is None or conversion.lease_owner != lease_owner:
                return None
            due_id = _google_delivery_due_id(conversion)
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
            self._io.write_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                conversion,
            )
            self._io.delete_entities_tx(
                transaction,
                _GOOGLE_DELIVERY_DUE_KIND,
                [due_id],
            )
            if conversion.delivery_status == "pending":
                self._write_google_delivery_pointer_tx(
                    transaction,
                    conversion_kind=conversion_kind,
                    conversion_id=conversion_id,
                    conversion=conversion,
                )
            return conversion

        return run_in_transaction_with_retry(self._io.database, txn)

    def _write_google_conversion_tx(
        self,
        transaction: Any,
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
        if conversion is None:
            return
        conversion_kind = google_ads_conversion_kind(conversion.occurred_at)
        conversion_id = google_ads_conversion_entity_id(conversion)
        existing = self._io.read_entity_tx(
            transaction,
            conversion_kind,
            conversion_id,
            GoogleAdsConversion,
        )
        if existing is not None:
            return
        self._io.write_entity_tx(
            transaction,
            conversion_kind,
            conversion_id,
            conversion,
        )
        self._write_google_delivery_pointer_tx(
            transaction,
            conversion_kind=conversion_kind,
            conversion_id=conversion_id,
            conversion=conversion,
        )

    def _claim_google_ads_delivery(
        self,
        *,
        conversion_kind: str,
        conversion_id: str,
        owner: str,
        leased_until: str,
        now: str,
    ) -> GoogleAdsConversion | None:
        if not conversion_kind or not conversion_id:
            return None

        def txn(transaction: Any) -> GoogleAdsConversion | None:
            conversion = self._io.read_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                GoogleAdsConversion,
            )
            if conversion is None or not _google_delivery_is_due(conversion, now):
                return None
            if (
                conversion.click_expires_at is not None
                and conversion.click_expires_at <= now
            ):
                due_id = _google_delivery_due_id(conversion)
                conversion.delivery_status = "dead"
                conversion.encrypted_click_id = None
                conversion.last_error = "google_click_identifier_expired"
                conversion.updated_at = now
                self._io.write_entity_tx(
                    transaction,
                    conversion_kind,
                    conversion_id,
                    conversion,
                )
                self._io.delete_entities_tx(
                    transaction,
                    _GOOGLE_DELIVERY_DUE_KIND,
                    [due_id],
                )
                return None
            conversion.lease_owner = owner
            conversion.leased_until = leased_until
            conversion.updated_at = now
            self._io.write_entity_tx(
                transaction,
                conversion_kind,
                conversion_id,
                conversion,
            )
            return conversion

        return run_in_transaction_with_retry(self._io.database, txn)

    def _write_google_delivery_pointer_tx(
        self,
        transaction: Any,
        *,
        conversion_kind: str,
        conversion_id: str,
        conversion: GoogleAdsConversion,
    ) -> None:
        self._io.write_entity_tx(
            transaction,
            _GOOGLE_DELIVERY_DUE_KIND,
            _google_delivery_due_id(conversion),
            {
                "conversion_kind": conversion_kind,
                "conversion_id": conversion_id,
                "next_attempt_at": conversion.next_attempt_at,
            },
        )

    def list_reminders(self, *, limit: int) -> list[ActivationReminderTask]:
        return self._io.list_entities(
            _REMINDER_KIND,
            cls=ActivationReminderTask,
            limit=limit,
        )

    def delete_reminders(self, reminder_ids: list[str]) -> None:
        if reminder_ids:
            self._io.delete_entities(_REMINDER_KIND, reminder_ids)

    def claim_reminder(
        self,
        workspace_id: str,
        stage: str,
        *,
        occurred_at: str,
    ) -> tuple[AcquisitionAttribution | None, bool]:
        milestone = f"activation_reminder_{stage}_sent"

        def txn(transaction: Any) -> tuple[AcquisitionAttribution | None, bool]:
            record = self._io.read_entity_tx(
                transaction,
                _KIND,
                workspace_id,
                AcquisitionAttribution,
            )
            if record is None:
                return None, False
            if (
                "first_successful_api_call" in record.milestones
                or milestone in record.milestones
            ):
                return record, False
            record.milestones[milestone] = occurred_at
            record.updated_at = iso_now()
            self._io.write_entity_tx(transaction, _KIND, workspace_id, record)
            return record, True

        return run_in_transaction_with_retry(self._io.database, txn)


def _google_conversion_id(order_id: str, occurred_at: str) -> str:
    timestamp = parse_utc_timestamp(occurred_at)
    return f"{timestamp:%Y%m%dT%H%M%SZ}#{order_id}"


def _google_click_expiry_id(record: AcquisitionAttribution) -> str:
    if not record.google_click_expires_at:
        raise ValueError("Google click expiry is required")
    return f"{record.google_click_expires_at}#{record.workspace_id}"


def _google_delivery_due_id(conversion: GoogleAdsConversion) -> str:
    return f"{conversion.delivery_status}#{conversion.next_attempt_at}#{conversion.order_id}"


def _google_delivery_backoff_seconds(attempts: int) -> int:
    return min(6 * 60 * 60, 30 * (2 ** max(attempts - 1, 0)))


def _iso_after_seconds(seconds: int) -> str:
    return (
        dt.datetime.now(dt.UTC).replace(microsecond=0)
        + dt.timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _google_delivery_is_due(conversion: GoogleAdsConversion, now: str) -> bool:
    if conversion.delivery_status != "pending" or conversion.next_attempt_at > now:
        return False
    return not conversion.leased_until or conversion.leased_until <= now
