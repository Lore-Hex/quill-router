import asyncio
import json
import time
from types import SimpleNamespace

from starlette.background import BackgroundTasks
from starlette.datastructures import Headers

from tests.test_async_settle_shadow import NOW, context, endpoint, signer
from tests.test_async_settle_shadow_accounting import Database
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import COUNTER, day_at
from trusted_router.services.async_settle_shadow import Capture, Runtime
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore


def test_one_primary_rejection_per_old_malformed_attempt(monkeypatch):
    ctx = context()
    now = NOW + 31 * 86400
    monkeypatch.setattr(time, "time", lambda: now)
    db = Database()
    store = EvidenceStore(db)
    monkeypatch.setattr(store, "booking", lambda *args: Booking(2, "settled", True))
    rt = Runtime(
        settings(
            async_settle_enabled=False, release="a" * 40, async_settle_shadow_workspaces="ws-v1"
        ),
        runtime(),
        store,
    )
    from trusted_router.services import async_settle_shadow as service
    real_compare = service.compare
    diagnostics = []
    def record(*args):
        compared = real_compare(*args)
        diagnostics.append(compared)
        return compared
    monkeypatch.setattr(service, "compare", record)
    rt.signer = signer()
    rt.counters.clock = lambda: now
    bg = BackgroundTasks()
    try:
        rt.submit(
            Capture(
                rt,
                ctx.body,
                "settle",
                now,
                time.monotonic(),
                ctx.authorization,
                endpoint(),
                (endpoint(),),
            ),
            SimpleNamespace(headers=Headers({"X-TR-Settlement-Shadow": "!"})),
            {"data": {"already_settled": True, "finalization_outcome": "settled"}},
            bg,
        )
        asyncio.run(bg())
    finally:
        rt.executor.shutdown()
    body = json.loads(db.rows[COUNTER, day_at(now) + "/" + rt.counters.instance])
    print(
        "observations",
        sum(b["observed_attempts"] for b in body["counts"]),
        "rejections",
        body["rejections"],
    )
    assert sum(r["count"] for r in body["rejections"]) == 1, (
        "one attempt must have one primary rejection"
    )

    assert [(r["reason"], r["count"]) for r in body["rejections"]] == [("base64", 1)]
    assert sum(b["observed_attempts"] for b in body["counts"]) == 1
    assert diagnostics[0].reasons == {"base64", "proof_expired"}
