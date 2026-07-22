from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import AcquisitionAttribution, iso_now

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
            return record

        return run_in_transaction_with_retry(self._io.database, txn)
