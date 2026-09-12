from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest

from trusted_router.company_affiliations import (
    AffiliationDirectory,
    build_snapshot,
    normalize_company_domain,
    validate_documents,
)

NOW = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
SHEET = "https://docs.google.com/spreadsheets/d/test-directory/edit"


def row(**changes: Any) -> dict[str, Any]:
    result = {
        "company_name": "Example Company",
        "funding_organization": "Y Combinator",
        "domain": "example.com",
        "company_url": "https://www.example.com/",
        "directory_url": "https://www.ycombinator.com/companies/example-company",
        "founding_year": "2020",
        "founding_year_source": "https://www.example.com/about",
        "verified_at": "2026-09-12",
        "listing_status": "active",
        "enabled": "TRUE",
    }
    return result | changes


def directory(rows: list[dict[str, Any]]) -> AffiliationDirectory:
    documents = build_snapshot(rows, source_sheet_url=SHEET, now=NOW)
    return AffiliationDirectory(lambda key: copy.deepcopy(documents.get(key)))


def test_exact_verified_domain_returns_sourced_affiliation() -> None:
    result = directory([row()]).lookup("Person@EXAMPLE.COM", email_verified=True, now=NOW)
    assert len(result) == 1
    assert result[0]["domain"] == "example.com"
    assert result[0]["funding_organization"] == "Y Combinator"
    assert result[0]["founding_year"] == 2020
    assert result[0]["match_method"] == "verified_email_domain"
    assert result[0]["source_url"].startswith("https://www.ycombinator.com/")


@pytest.mark.parametrize("verified", [False, None, "true", "false", 1])
def test_unverified_emails_do_not_even_read_directory(verified: Any) -> None:
    def forbidden_read(key: str) -> None:
        pytest.fail("Unverified identities must not query affiliation data")

    assert AffiliationDirectory(forbidden_read).lookup(
        "person@example.com", email_verified=verified, now=NOW
    ) == []


@pytest.mark.parametrize("email", [
    "person@sub.example.com", "person@example.com.attacker.net",
    "person@notexample.com", "person@example.co", "person@examp1e.com",
    "person@gmail.com", "person@example.com@attacker.net", "example.com",
    "person@example.com.", "person@www.example.com", "person@éxample.com",
])
def test_no_suffix_fuzzy_subdomain_or_invalid_email_matches(email: str) -> None:
    assert directory([row()]).lookup(email, email_verified=True, now=NOW) == []


@pytest.mark.parametrize("domain", [
    "gmail.com", "outlook.com", "yahoo.com", "github.io", "co.uk", "localhost",
    "127.0.0.1", "example.com/path", "*.example.com", "user@example.com",
    "https://example.com", "example.com:443", "-bad.example", "example.com.",
])
def test_invalid_or_shared_domains_are_rejected(domain: str) -> None:
    with pytest.raises(ValueError):
        normalize_company_domain(domain)


def test_multiple_organizations_are_preserved_and_duplicates_removed() -> None:
    records = [row(), row(), row(
        funding_organization="Accel", directory_url="https://www.accel.com/companies/example"
    )]
    result = directory(records).lookup("x@example.com", email_verified=True, now=NOW)
    assert {item["funding_organization"] for item in result} == {"Y Combinator", "Accel"}
    assert len(result) == 2


def test_missing_founding_year_remains_null() -> None:
    result = directory([row(founding_year="", founding_year_source="")]).lookup(
        "x@example.com", email_verified=True, now=NOW
    )
    assert result[0]["founding_year"] is None


def test_enabled_rows_require_source_evidence_and_real_founding_year() -> None:
    for changes in [
        {"directory_url": ""}, {"company_url": "https://other.example/"},
        {"founding_year": "W2020"}, {"founding_year": "2030"},
        {"founding_year_source": ""}, {"verified_at": ""},
        {"directory_url": "javascript:alert(1)"},
    ]:
        with pytest.raises(ValueError):
            build_snapshot([row(**changes)], source_sheet_url=SHEET, now=NOW)


def test_unreviewed_disabled_and_retired_rows_cannot_make_claims() -> None:
    for changes in [
        {"enabled": "FALSE"}, {"enabled": ""}, {"listing_status": "inactive"},
        {"listing_status": "defunct"}, {"listing_status": "acquired"},
    ]:
        assert directory([row(**changes)]).lookup(
            "x@example.com", email_verified=True, now=NOW
        ) == []


def test_conflicting_domain_ownership_rejects_snapshot() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        build_snapshot([row(), row(company_name="Unrelated Company")], source_sheet_url=SHEET, now=NOW)


def test_stale_rows_and_expired_snapshot_do_not_make_claims() -> None:
    assert directory([row(verified_at="2025-01-01")]).lookup(
        "x@example.com", email_verified=True, now=NOW
    ) == []
    assert directory([row()]).lookup(
        "x@example.com", email_verified=True, now=NOW + dt.timedelta(days=31)
    ) == []


def test_snapshot_is_deterministic_and_bounded() -> None:
    a, b = row(), row(domain="other.example", company_url="https://other.example", company_name="Other")
    assert build_snapshot([a, b], source_sheet_url=SHEET, now=NOW) == build_snapshot(
        [b, a], source_sheet_url=SHEET, now=NOW
    )
    docs = build_snapshot([a, b], source_sheet_url=SHEET, now=NOW)
    assert len(docs) <= 257
    assert docs["current"]["domain_count"] == 2


def test_cache_is_local_and_cannot_be_mutated_by_a_caller() -> None:
    docs = build_snapshot([row()], source_sheet_url=SHEET, now=NOW)
    reads: list[str] = []

    def read(key: str) -> Any:
        reads.append(key)
        return copy.deepcopy(docs.get(key))

    lookup = AffiliationDirectory(read)
    first = lookup.lookup("first@example.com", email_verified=True, now=NOW)
    first[0]["funding_organization"] = "Forged"
    second = lookup.lookup("second@example.com", email_verified=True, now=NOW)
    assert second[0]["funding_organization"] == "Y Combinator"
    assert len(reads) == 2
    assert all("@" not in key for key in reads)


def test_missing_or_broken_catalog_never_breaks_login_or_claims_a_match() -> None:
    assert AffiliationDirectory(lambda _: None).lookup("x@example.com", email_verified=True, now=NOW) == []

    def unavailable(_: str) -> Any:
        raise ConnectionError("backend down")

    assert AffiliationDirectory(unavailable).lookup("x@example.com", email_verified=True, now=NOW) == []


def test_modified_or_incomplete_snapshot_cannot_publish() -> None:
    docs = build_snapshot([row()], source_sheet_url=SHEET, now=NOW)
    bucket = next(key for key in docs if key != "current")
    for change in ("record", "count", "missing"):
        modified = copy.deepcopy(docs)
        if change == "record":
            modified[bucket]["domains"]["example.com"][0]["founding_year"] = 1900
        elif change == "count":
            modified["current"]["domain_count"] = 999
        else:
            del modified[bucket]
        with pytest.raises(ValueError):
            validate_documents(modified)
