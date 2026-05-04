from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_outreach_quote_bank_quotes_are_from_founder_excerpts() -> None:
    with (ROOT / "docs/outreach-quote-bank.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    for row in rows:
        quote = row["quote"].strip()
        source = ROOT / row["source"]
        assert source.is_file()
        assert quote in source.read_text()
        assert row["context_link"].startswith(("/", "https://trustedrouter.com/"))
        assert len(quote) <= 280


def test_outreach_approval_packet_requires_approval_and_opt_out_check() -> None:
    packet = (ROOT / "docs/outreach-approval-packet.md").read_text()

    assert "Do not send until `status=approved`" in packet
    assert "`opt_out` is checked before sending." in packet
    assert "`relevant_quote` is copied from `docs/outreach-quote-bank.csv`." in packet


def test_google_sheet_header_supports_quote_based_approval_flow() -> None:
    header = (ROOT / "docs/revenue-loop-google-sheet.csv").read_text().strip().split(",")

    for column in [
        "relevant_quote",
        "context_link",
        "approved_message",
        "status",
        "sent_at",
        "opt_out",
    ]:
        assert column in header
