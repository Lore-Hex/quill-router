from __future__ import annotations

import threading

from trusted_router.storage_models import (
    AcquisitionAttribution,
    ActivationReminderTask,
    activation_reminder_tasks,
    iso_now,
)


class InMemoryAcquisitionAttribution:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.records: dict[str, AcquisitionAttribution] = {}
        self.reminders: dict[str, ActivationReminderTask] = {}

    def reset(self) -> None:
        self.records.clear()
        self.reminders.clear()

    def create(self, record: AcquisitionAttribution) -> bool:
        with self._lock:
            if record.workspace_id in self.records:
                return False
            self.records[record.workspace_id] = record
            for reminder in activation_reminder_tasks(record):
                self.reminders[reminder.id] = reminder
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
