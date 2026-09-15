import base64

import pytest

from scripts.lightning.install_maintenance import SERVICE, TIMER
from scripts.lightning.prune_node import prune_page


def row(index: int, **overrides: object) -> dict[str, object]:
    return {"add_index": str(index), "r_hash": base64.b64encode(bytes([index]) * 32).decode(),
            "state": "CANCELED", "settled": False, "memo": "LightningRouter API credit",
            "amt_paid_msat": "0", "settle_index": "0", "creation_date": "1", "expiry": "900", **overrides}


@pytest.mark.parametrize("change", [
    {"state": "SETTLED"}, {"state": "OPEN"}, {"state": "ACCEPTED"}, {"settled": True},
    {"amt_paid_msat": "1"}, {"settle_index": "1"}, {"memo": "manual invoice"}, {"creation_date": "9999999"},
])
def test_node_retention_preserves_money_open_and_unrelated_invoices(change):
    deleted = []
    assert prune_page([row(1, **change)], 0, 10_000_000, deleted.append, apply=True)["deleted"] == 0
    assert not deleted


def test_node_retention_bounded_idempotent_pages_and_dry_run():
    rows = [row(i) for i in range(1, 102)]
    deleted = []
    preview = prune_page(rows, 0, 10_000_000, deleted.append, apply=False)
    assert preview == {"eligible": 10, "deleted": 0, "scanned": 10, "cursor": 10}
    assert not deleted
    result = prune_page(rows, 0, 10_000_000, deleted.append, apply=True)
    assert result["deleted"] == 10 and result["cursor"] == 10
    assert len(set(deleted)) == 10
    assert prune_page([], 10, 10_000_000, deleted.append, apply=True)["cursor"] == 0


def test_node_retention_fails_closed_on_malformed_order_and_hash():
    with pytest.raises(ValueError, match="Nonmonotonic"):
        prune_page([row(1)], 2, 10_000_000, lambda _: None, apply=True)
    with pytest.raises(ValueError, match="hash"):
        prune_page([row(1, r_hash="YQ==")], 0, 10_000_000, lambda _: None, apply=True)


def test_node_maintenance_does_not_restart_lnd_or_get_spend_authority():
    assert "--apply" in SERVICE
    assert "OnUnitInactiveSec=1h" in TIMER
    assert "ReadWritePaths=/srv/lnd/recovery" in SERVICE
    assert "restart" not in SERVICE + TIMER
