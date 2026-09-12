"""Publish a reviewed Google Sheet CSV export; directory data stays outside git."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from trusted_router.company_affiliations import build_snapshot, validate_documents
from trusted_router.config import Settings
from trusted_router.storage import create_store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="Local export of the Companies tab")
    parser.add_argument("--sheet-url", help="Private source Google Sheet URL")
    parser.add_argument("--status", action="store_true", help="Read current snapshot metadata only")
    parser.add_argument("--apply", action="store_true", help="Publish atomically in the configured store")
    parser.add_argument("--expected-revision", help="Current revision from --status; 'none' for first import")
    args = parser.parse_args()
    if args.status:
        if args.apply or args.csv or args.sheet_url:
            parser.error("--status cannot be combined with an import")
        store = create_store(Settings(), initialize_schema=False)
        print(json.dumps(store.get_company_affiliation_document("current"), sort_keys=True))
        return
    if args.csv is None or not args.sheet_url:
        parser.error("--csv and --sheet-url are required")
    if args.csv.stat().st_size > 50_000_000:
        parser.error("CSV exceeds 50 MB")
    with args.csv.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"company_name", "funding_organization", "domain", "company_url", "directory_url", "founding_year", "founding_year_source", "verified_at", "listing_status", "enabled"}
        if not required.issubset(reader.fieldnames or []):
            parser.error("CSV must include all documented company affiliation columns")
        documents = build_snapshot(reader, source_sheet_url=args.sheet_url)
    validate_documents(documents)
    if args.apply:
        if args.expected_revision is None:
            parser.error("--apply requires --expected-revision from a fresh --status")
        settings = Settings()
        if settings.storage_backend == "memory":
            parser.error("Cannot publish to an ephemeral memory store")
        store = create_store(settings, initialize_schema=False)
        expected = None if args.expected_revision == "none" else args.expected_revision
        store.publish_company_affiliation_documents(documents, expected_revision=expected)
        if store.get_company_affiliation_document("current") != documents["current"]:
            raise RuntimeError("Snapshot read-back mismatch")
    print(json.dumps({"applied": args.apply, **documents["current"]}, sort_keys=True))


if __name__ == "__main__":
    main()
