from __future__ import annotations

import threading

from trusted_router.storage_models import AcquisitionAttribution, iso_now


class InMemoryAcquisitionAttribution:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.records: dict[str, AcquisitionAttribution] = {}

    def reset(self) -> None:
        self.records.clear()

    def create(self, record: AcquisitionAttribution) -> bool:
        with self._lock:
            if record.workspace_id in self.records:
                return False
            self.records[record.workspace_id] = record
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
            return record
