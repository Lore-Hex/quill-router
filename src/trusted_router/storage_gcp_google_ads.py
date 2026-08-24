"""Spanner-only adapter for the isolated Google Ads conversion worker."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, TypeVar

from trusted_router.config import Settings
from trusted_router.services.google_data_manager import GoogleAdsDeliveryStore
from trusted_router.storage_gcp_attribution import SpannerAcquisitionAttribution
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_io import SpannerIO, configure_spanner_rpc_deadlines
from trusted_router.storage_models import GoogleAdsConversion

T = TypeVar("T")


class SpannerGoogleAdsDeliveryStore:
    """Expose only conversion rows, without constructing prompt-path storage."""

    entity_table = "tr_entities"

    def __init__(
        self,
        *,
        project_id: str,
        spanner_instance_id: str,
        spanner_database_id: str,
    ) -> None:
        if not spanner_instance_id or not spanner_database_id:
            raise ValueError("Spanner instance and database IDs are required")
        try:
            from google.cloud import spanner
            from google.cloud.spanner_v1 import FixedSizePool, param_types
        except ImportError as exc:  # pragma: no cover - production dependency.
            raise RuntimeError(
                "Install google-cloud-spanner for the Google Data Manager worker"
            ) from exc
        self._spanner = spanner
        self._param_types = param_types
        self._database = (
            spanner.Client(project=project_id, disable_builtin_metrics=True)
            .instance(spanner_instance_id)
            .database(spanner_database_id, pool=FixedSizePool(size=2))
        )
        configure_spanner_rpc_deadlines(self._database)
        io = SpannerIO(
            database=self._database,
            spanner_module=self._spanner,
            param_types=self._param_types,
            write_entity_batch=self._write_entity_batch,
            read_entity_tx=self._read_entity_tx,
            write_entity_tx=self._write_entity_tx,
            write_entity=self._write_entity,
            read_entity=self._read_entity,
            list_entities=self._list_entities,
            delete_entities=self._delete_entities,
            delete_entities_tx=self._delete_entities_tx,
        )
        self._attribution = SpannerAcquisitionAttribution(io)

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int:
        return self._attribution.repair_google_ads_delivery_queue(since=since, limit=limit)

    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int:
        return self._attribution.purge_expired_google_ads_click_ids(
            before=before,
            limit=limit,
        )

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]:
        return self._attribution.claim_google_ads_deliveries(
            limit=limit,
            lease_seconds=lease_seconds,
        )

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None:
        return self._attribution.mark_google_ads_delivery_submitted(
            order_id=order_id,
            occurred_at=occurred_at,
            lease_owner=lease_owner,
            request_id=request_id,
        )

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
        return self._attribution.mark_google_ads_delivery_failed(
            order_id=order_id,
            occurred_at=occurred_at,
            lease_owner=lease_owner,
            error=error,
            retryable=retryable,
            max_attempts=max_attempts,
        )

    def _read_entity(self, kind: str, entity_id: str, cls: type[T]) -> T | None:
        with self._database.snapshot() as snapshot:
            return self._read_entity_from(snapshot, kind, entity_id, cls)

    def _read_entity_tx(
        self,
        transaction: Any,
        kind: str,
        entity_id: str,
        cls: type[T],
    ) -> T | None:
        return self._read_entity_from(transaction, kind, entity_id, cls)

    def _read_entity_from(
        self,
        reader: Any,
        kind: str,
        entity_id: str,
        cls: type[T],
    ) -> T | None:
        rows = list(
            reader.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
                params={"kind": kind, "id": entity_id},
                param_types={
                    "kind": self._param_types.STRING,
                    "id": self._param_types.STRING,
                },
            )
        )
        return self._decode_entity(rows[0][0], cls) if rows else None

    def _list_entities(
        self,
        kind: str,
        *,
        cls: type[T],
        prefix: str | None = None,
        suffix: str | None = None,
        limit: int | None = None,
    ) -> list[T]:
        where = "kind=@kind"
        params: dict[str, Any] = {"kind": kind}
        param_types: dict[str, Any] = {"kind": self._param_types.STRING}
        if prefix is not None:
            where += " AND STARTS_WITH(id, @prefix)"
            params["prefix"] = prefix
            param_types["prefix"] = self._param_types.STRING
        if suffix is not None:
            where += " AND ENDS_WITH(id, @suffix)"
            params["suffix"] = suffix
            param_types["suffix"] = self._param_types.STRING
        tail = " ORDER BY id"
        if limit is not None:
            tail += " LIMIT @limit"
            params["limit"] = int(limit)
            param_types["limit"] = self._param_types.INT64
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                f"SELECT body FROM tr_entities WHERE {where}{tail}",  # noqa: S608
                params=params,
                param_types=param_types,
            )
            return [self._decode_entity(row[0], cls) for row in rows]

    @staticmethod
    def _decode_entity(body: str, cls: type[T]) -> T:
        data = json.loads(body)
        if cls is dict:
            return data
        if dataclasses.is_dataclass(cls):
            known = {field.name for field in dataclasses.fields(cls)}
            data = {key: value for key, value in data.items() if key in known}
        return cls(**data)

    def _write_entity(self, kind: str, entity_id: str, value: Any) -> None:
        with self._database.batch() as batch:
            self._write_entity_batch(batch, kind, entity_id, value)

    def _write_entity_batch(
        self,
        batch: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> None:
        batch.insert_or_update(
            table=self.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[
                (kind, entity_id, json_body(value), self._spanner.COMMIT_TIMESTAMP)
            ],
        )

    def _write_entity_tx(
        self,
        transaction: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> None:
        transaction.insert_or_update(
            table=self.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[
                (kind, entity_id, json_body(value), self._spanner.COMMIT_TIMESTAMP)
            ],
        )

    def _delete_entities(self, kind: str, entity_ids: list[str]) -> None:
        with self._database.batch() as batch:
            batch.delete(
                self.entity_table,
                self._spanner.KeySet(keys=[(kind, entity_id) for entity_id in entity_ids]),
            )

    def _delete_entities_tx(
        self,
        transaction: Any,
        kind: str,
        entity_ids: list[str],
    ) -> None:
        transaction.delete(
            self.entity_table,
            self._spanner.KeySet(keys=[(kind, entity_id) for entity_id in entity_ids]),
        )


def create_google_ads_delivery_store(settings: Settings) -> GoogleAdsDeliveryStore:
    return SpannerGoogleAdsDeliveryStore(
        project_id=settings.gcp_project_id,
        spanner_instance_id=settings.spanner_instance_id or "",
        spanner_database_id=settings.spanner_database_id or "",
    )
