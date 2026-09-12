from __future__ import annotations

import csv
import datetime as dt
import json

import pytest

from scripts import import_company_affiliations as importer
from tests.test_company_affiliations import SHEET, row


def test_dry_run_validates_export_without_connecting_to_storage(tmp_path, monkeypatch, capsys) -> None:
    source = tmp_path / "companies.csv"
    record = row(verified_at=dt.datetime.now(dt.UTC).date().isoformat())
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(record))
        writer.writeheader()
        writer.writerow(record)
    monkeypatch.setattr("sys.argv", ["import", "--csv", str(source), "--sheet-url", SHEET])
    monkeypatch.setattr(importer, "create_store", lambda *args, **kwargs: pytest.fail("Dry run opened storage"))
    importer.main()
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False
    assert result["domain_count"] == 1


def test_bad_export_schema_does_not_connect_or_publish(tmp_path, monkeypatch) -> None:
    source = tmp_path / "companies.csv"
    source.write_text("company_name,domain\nFake,example.com\n")
    monkeypatch.setattr("sys.argv", ["import", "--csv", str(source), "--sheet-url", SHEET, "--apply"])
    monkeypatch.setattr(importer, "create_store", lambda *args, **kwargs: pytest.fail("Invalid export opened storage"))
    with pytest.raises(SystemExit, match="2"):
        importer.main()
