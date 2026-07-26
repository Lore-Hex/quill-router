from __future__ import annotations

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
    GoogleAdsConversion,
    iso_now,
)

_KIND = "acquisition_attribution"


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
            self._write_google_conversion_tx(
                transaction,
                record,
                "signup_completed",
                occurred_at=record.signup_at,
            )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

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
            claimed: list[str] = []
            for name in milestones:
                if name not in record.milestones and name not in claimed:
                    claimed.append(name)
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

    def list_google_ads_conversions(
        self,
        *,
        since: str,
        limit: int,
    ) -> list[GoogleAdsConversion]:
        since_at = parse_utc_timestamp(since)
        rows: list[GoogleAdsConversion] = []
        for kind in google_ads_conversion_kinds_since(since_at):
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            rows.extend(
                self._io.list_entities(
                    kind,
                    cls=GoogleAdsConversion,
                    limit=remaining,
                )
            )
        rows = [
            conversion
            for conversion in rows
            if parse_utc_timestamp(conversion.occurred_at) >= since_at
        ]
        rows.sort(key=lambda item: (item.occurred_at, item.order_id))
        return rows[:limit]

    def backfill_google_ads_conversions(self, *, limit: int) -> int:
        records = self._io.list_entities(
            _KIND,
            cls=AcquisitionAttribution,
            limit=limit,
        )
        created = 0
        for candidate in records:
            workspace_id = candidate.workspace_id

            def txn(
                transaction: Any,
                workspace_id: str = workspace_id,
            ) -> int:
                record = self._io.read_entity_tx(
                    transaction,
                    _KIND,
                    workspace_id,
                    AcquisitionAttribution,
                )
                if record is None:
                    return 0
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
                inserted = 0
                for event, occurred_at in events:
                    conversion = build_google_ads_conversion(
                        record,
                        event,
                        occurred_at=occurred_at,
                    )
                    if conversion is None:
                        continue
                    kind = google_ads_conversion_kind(conversion.occurred_at)
                    entity_id = google_ads_conversion_entity_id(conversion)
                    existing = self._io.read_entity_tx(
                        transaction,
                        kind,
                        entity_id,
                        GoogleAdsConversion,
                    )
                    if existing is None:
                        self._io.write_entity_tx(
                            transaction,
                            kind,
                            entity_id,
                            conversion,
                        )
                        inserted += 1
                return inserted

            created += run_in_transaction_with_retry(self._io.database, txn)
        return created

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
        self._io.write_entity_tx(
            transaction,
            google_ads_conversion_kind(conversion.occurred_at),
            google_ads_conversion_entity_id(conversion),
            conversion,
        )
