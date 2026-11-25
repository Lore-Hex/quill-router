"""Starter-session claims: the same contract on every registered store backend."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from trusted_router.store_protocol import Store

NOW = 1_800_000_000
WINDOW = 7 * 86_400


def test_first_claimer_holds_the_session_within_the_window(store: Store, unique: str):
    key = f"session-{unique}"
    first, second = f"ws-a-{unique}", f"ws-b-{unique}"
    assert store.claim_starter_session(key, first, now=NOW, window_seconds=WINDOW) is None
    # The holder keeps using it.
    assert store.claim_starter_session(key, first, now=NOW + 60, window_seconds=WINDOW) is None
    # Another workspace is refused and does not take the claim over.
    assert store.claim_starter_session(key, second, now=NOW + 120, window_seconds=WINDOW) == first
    assert store.claim_starter_session(key, second, now=NOW + 180, window_seconds=WINDOW) == first
    assert store.claim_starter_session(key, first, now=NOW + 240, window_seconds=WINDOW) is None


def test_claim_expires_after_the_window_and_moves(store: Store, unique: str):
    key = f"session-{unique}"
    first, second = f"ws-a-{unique}", f"ws-b-{unique}"
    assert store.claim_starter_session(key, first, now=NOW, window_seconds=WINDOW) is None
    later = NOW + WINDOW + 1
    assert store.claim_starter_session(key, second, now=later, window_seconds=WINDOW) is None
    assert store.claim_starter_session(key, first, now=later + 1, window_seconds=WINDOW) == second


def test_an_active_holder_renews_and_keeps_the_session(store: Store, unique: str):
    key = f"session-{unique}"
    first, second = f"ws-a-{unique}", f"ws-b-{unique}"
    assert store.claim_starter_session(key, first, now=NOW, window_seconds=WINDOW) is None
    # Used again past half the window: the holder's claim is renewed.
    renewed = NOW + WINDOW // 2 + 1
    assert store.claim_starter_session(key, first, now=renewed, window_seconds=WINDOW) is None
    # Past the original window but inside the renewed one, another workspace is
    # still refused, and the holder keeps working.
    assert store.claim_starter_session(key, second, now=NOW + WINDOW + 1, window_seconds=WINDOW) == first
    assert store.claim_starter_session(key, first, now=NOW + WINDOW + 2, window_seconds=WINDOW) is None


def test_sessions_are_independent(store: Store, unique: str):
    first, second = f"ws-a-{unique}", f"ws-b-{unique}"
    assert store.claim_starter_session(f"one-{unique}", first, now=NOW, window_seconds=WINDOW) is None
    assert store.claim_starter_session(f"two-{unique}", second, now=NOW, window_seconds=WINDOW) is None


def test_concurrent_first_claims_leave_exactly_one_holder(store: Store, unique: str):
    key = f"session-{unique}"
    workspaces = [f"ws-{i}-{unique}" for i in range(6)]
    barrier = Barrier(len(workspaces))

    def claim(workspace_id: str) -> str | None:
        barrier.wait()
        return store.claim_starter_session(key, workspace_id, now=NOW, window_seconds=WINDOW)

    with ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
        results = list(pool.map(claim, workspaces))
    winners = [ws for ws, held_by in zip(workspaces, results, strict=True) if held_by is None]
    assert len(winners) == 1
    assert all(held_by == winners[0] for held_by in results if held_by is not None)
