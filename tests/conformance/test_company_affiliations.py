from __future__ import annotations

import datetime as dt

import pytest

from trusted_router.company_affiliations import build_snapshot
from trusted_router.storage_errors import StoreConflict


def _documents(*, name: str = "Example") -> dict:
    return build_snapshot([{
        "enabled": "TRUE", "listing_status": "active", "domain": "example.com",
        "company_name": name, "company_url": "https://example.com",
        "funding_organization": "Y Combinator",
        "directory_url": "https://www.ycombinator.com/companies/example",
        "founding_year": "", "verified_at": "2026-09-12",
    }], source_sheet_url="https://docs.google.com/spreadsheets/d/example/edit",
        now=dt.datetime(2026, 9, 12, tzinfo=dt.UTC))


def test_affiliation_atomic_publish_and_read(store) -> None:
    assert store.get_company_affiliation_document("missing") is None
    old = store.get_company_affiliation_document("current")
    docs = _documents()
    store.publish_company_affiliation_documents(docs, expected_revision=old["revision"] if old else None)
    assert store.get_company_affiliation_document("current") == docs["current"]
    for key in docs:
        assert store.get_company_affiliation_document(key) == docs[key]


def test_affiliation_stale_writer_cannot_partially_publish(store) -> None:
    old = store.get_company_affiliation_document("current")
    first, second = _documents(), _documents(name="Other")
    store.publish_company_affiliation_documents(first, expected_revision=old["revision"] if old else None)
    with pytest.raises(StoreConflict):
        store.publish_company_affiliation_documents(second, expected_revision="stale-revision")
    assert store.get_company_affiliation_document("current") == first["current"]
    for key in second:
        if key != "current":
            assert store.get_company_affiliation_document(key) is None


def test_affiliation_malformed_snapshot_rejected_before_write(store) -> None:
    before = store.get_company_affiliation_document("current")
    with pytest.raises(ValueError):
        store.publish_company_affiliation_documents({"current": {"revision": "wrong"}}, expected_revision=None)
    assert store.get_company_affiliation_document("current") == before
