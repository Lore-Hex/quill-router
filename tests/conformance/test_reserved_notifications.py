"""Isolated reservation notification leases must never need a credit account."""
from concurrent.futures import ThreadPoolExecutor


def test_reserved_notification_retry_and_ownership(store, unique):
    session = f"cs_{unique}"
    assert store.claim_reserved_notification(session, "one", now=100) == "claimed"
    assert store.claim_reserved_notification(session, "two", now=101) == "busy"
    store.finish_reserved_notification(session, "wrong", sent=True)
    assert store.claim_reserved_notification(session, "two", now=102) == "busy"
    store.finish_reserved_notification(session, "one", sent=False)
    assert store.claim_reserved_notification(session, "two", now=103) == "claimed"
    assert store.claim_reserved_notification(session, "three", now=224) == "claimed"
    store.finish_reserved_notification(session, "two", sent=True)
    assert store.claim_reserved_notification(session, "four", now=225) == "busy"
    store.finish_reserved_notification(session, "three", sent=True)
    assert store.claim_reserved_notification(session, "four", now=9999) == "sent"


def test_reserved_notification_concurrent_delivery(store, unique):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: store.claim_reserved_notification(f"cs_{unique}", str(i), now=100), range(4)))
    assert sorted(results) == ["busy", "busy", "busy", "claimed"]
