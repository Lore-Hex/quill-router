from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.test_company_affiliations import NOW, SHEET, row
from trusted_router.company_affiliations import build_snapshot
from trusted_router.storage_errors import StoreConflict


def test_postgres_snapshot_roundtrip_and_lost_initial_race() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    docs = build_snapshot([row()], source_sheet_url=SHEET, now=NOW)
    with patch.object(store, "_insert_entity_once_tx", return_value=False):
        with pytest.raises(StoreConflict):
            store.publish_company_affiliation_documents(docs, expected_revision=None)
    assert conn.count_entities("company_affiliation") == 0
    store.publish_company_affiliation_documents(docs, expected_revision=None)
    assert store.get_company_affiliation_document("current") == docs["current"]


def test_postgres_failure_rolls_back_pointer_and_all_buckets() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    first = build_snapshot([row()], source_sheet_url=SHEET, now=NOW)
    store.publish_company_affiliation_documents(first, expected_revision=None)
    second = build_snapshot([row(company_name="Renamed")], source_sheet_url=SHEET, now=NOW)
    original = store._write_entity_tx

    def failing_write(connection, kind, key, value):
        original(connection, kind, key, value)
        if key != "current":
            raise RuntimeError("injected failure after write")

    with patch.object(store, "_write_entity_tx", side_effect=failing_write):
        with pytest.raises(RuntimeError):
            store.publish_company_affiliation_documents(second, expected_revision=first["current"]["revision"])
    assert store.get_company_affiliation_document("current") == first["current"]
    assert conn.count_entities("company_affiliation") == len(first)
