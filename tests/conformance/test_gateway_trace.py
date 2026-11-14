from __future__ import annotations

from tests.conformance.test_store_semantics import _authorize
from trusted_router.store_protocol import Store


def test_trace_survives_idempotent_replay(
    store: Store, workspace_id: str, unique: str,
) -> None:
    trace = "rlog_" + unique.ljust(32, "0")
    other_trace = "rlog_" + unique.ljust(32, "1")
    kwargs = dict(idempotency_key=f"trace-{unique}", idempotency_fingerprint="same")
    auth = _authorize(store, workspace_id, f"key-{unique}", gateway_request_id=trace, **kwargs)
    replay = _authorize(store, workspace_id, f"key-{unique}", gateway_request_id=other_trace, **kwargs)
    assert auth.id == replay.id
    stored = store.get_gateway_authorization(auth.id)
    assert stored is not None and stored.gateway_request_id == trace
