from __future__ import annotations

import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import pytest

from trusted_router import social_rewards_monitor as monitor

NOW = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)


def receipt(user="private-user", action="github", amount="30000", age=0):
    return [f"social-rewards-v1:{user}:{action}", "private-workspace", "grant", amount,
            (NOW - dt.timedelta(minutes=age)).isoformat()]


def report(rows, **kwargs):
    return monitor.summarize(rows, now=NOW, window_minutes=15, alert_claims=2, **kwargs)


def test_read_uses_only_covering_index_prefix_and_bounded_low_priority_rpc():
    calls = []

    class Client:
        def read(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(rows=[receipt()])

    assert monitor.read_receipts(Client(), "session", 10) == [receipt()]
    call = calls[0]
    request = call["request"]
    assert request["index"] == "tr_trust_event_by_reward_event"
    assert request["key_set"] == {"ranges": [{"start_closed": ["social-rewards-v1:"],
                                             "end_open": ["social-rewards-v1;"]}]}
    assert request["limit"] == 11
    assert request["columns"] == monitor.COLUMNS
    assert request["request_options"]["priority"] == "PRIORITY_LOW"
    assert request["transaction"] == {"single_use": {"read_only": {"strong": True}}}
    assert call["timeout"] == 10 and call["retry"] is None


@pytest.mark.parametrize("limit", [0, -1, 10001])
def test_unsafe_limits_rejected_before_read(limit):
    with pytest.raises(ValueError):
        monitor.read_receipts(None, "session", limit)


def test_truncation_is_failure_not_partial_total():
    client = SimpleNamespace(read=lambda **kwargs: SimpleNamespace(rows=[receipt(), receipt("other")]))
    with pytest.raises(ValueError, match="incomplete"):
        monitor.read_receipts(client, "session", 1)


def test_durable_receipts_need_no_marketing_and_replays_do_not_inflate():
    result = report([receipt(), receipt()])
    assert result["claims"] == 1
    assert result["issued_microdollars"] == 30000
    assert result["alerts"] == []
    assert "private" not in json.dumps(result)


def test_each_cycle_reconciles_late_receipts_without_event_time_cursor():
    responses = iter([[receipt()], [receipt(), receipt("late", age=60)]])
    requests = []

    def read(**kwargs):
        requests.append(kwargs["request"])
        return SimpleNamespace(rows=next(responses))

    client = SimpleNamespace(read=read)
    first = report(monitor.read_receipts(client, "session", 10))
    second = report(monitor.read_receipts(client, "session", 10))
    assert first["claims"] == 1 and second["claims"] == 2
    assert second["recent_claims"] == 1
    assert requests[0] == requests[1]


def test_qa_excluded_only_from_customer_counts_and_alert_still_fires():
    qa = hashlib.sha256(b"tr-account:private-user").hexdigest()
    result = report([receipt(), receipt(action="youtube")], qa_hashes=frozenset({qa}))
    assert result["claims"] == 2 and result["customer_claims"] == 0
    assert result["qa_claims"] == 2 and result["issued_microdollars"] == 60000
    assert result["alerts"] == ["redemption_velocity"]


def test_amount_anomaly_alerts_without_hiding_actual_issuance():
    result = report([receipt(amount="60000")])
    assert result["issued_microdollars"] == 60000
    assert result["alerts"] == ["unexpected_reward_amount"]


def test_duplicate_receipt_in_different_workspace_is_not_silently_deduped():
    duplicate = receipt()
    duplicate[1] = "other-workspace"
    with pytest.raises(ValueError, match="conflicting"):
        report([receipt(), duplicate])


@pytest.mark.parametrize("row", [receipt(action="invalid"), receipt(age=-10),
                                ["other:person:github", "ws", "grant", "30000", NOW.isoformat()]])
def test_invalid_receipts_fail_closed(row):
    with pytest.raises(ValueError):
        report([row])


def test_qa_list_rejects_raw_pii(tmp_path):
    path = tmp_path / "qa.json"
    path.write_text('["user@example.com"]')
    with pytest.raises(ValueError):
        monitor.load_qa_hashes(str(path))
