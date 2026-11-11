"""Shared committed-event assertions for absent key counters."""
import logging


def assert_unbooked(caplog, key_hash, shard, actual_micro, book_to_byok):
    records = [record for record in caplog.records
               if record.name == "trusted_router.storage_gcp_authorize"
               and record.levelno >= logging.WARNING]
    assert len(records) == 1
    event = records[0]
    assert event.levelno == logging.WARNING
    assert event.getMessage().startswith("metric=key_usage_unbooked_total value=1 ")
    assert event.metric == "key_usage_unbooked_total"
    assert event.value == 1
    expected = dict(key_hash=key_hash, shard=shard, actual_micro=actual_micro,
                    book_to_byok=book_to_byok, hold_micro=0, usage_unbooked=True)
    for name, value in expected.items():
        assert getattr(event, name) == value
    for name in ("key_hash", "shard", "actual_micro", "book_to_byok"):
        assert f"{name}={expected[name]}" in event.getMessage()
