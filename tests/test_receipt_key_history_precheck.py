"""Re-observing a stored history document must not cost a read-write transaction.

The receipt-key collector re-observes every history document on every run.
Once a document is stored that observation changes nothing, and on Spanner it
used to find that out inside a read-write transaction that then committed no
mutation: several hundred empty commits an hour in production.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.receipt_keys import (
    b64url_encode,
    receipt_attestation_sha256,
    receipt_key_commitment,
    receipt_key_entity_id,
)
from trusted_router.storage_gcp import RECEIPT_KEY_KIND, SpannerStore
from trusted_router.storage_models import ReceiptKey


def _jwk() -> dict[str, str]:
    public_key = hashlib.sha256(b"history-precheck").digest()
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public_key)}


def _kid(jwk: dict[str, str]) -> str:
    import base64

    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    return b64url_encode(hashlib.sha256(raw).digest())


def _document(marker: str, *, seen: str = "2026-10-01T00:00:00Z") -> ReceiptKey:
    jwk = _jwk()
    header = b64url_encode(json.dumps({"alg": "RS256"}).encode())
    payload = b64url_encode(
        json.dumps({"eat_nonce": [receipt_key_commitment(jwk).hex()], "marker": marker}).encode()
    )
    return ReceiptKey(
        kid=_kid(jwk),
        jwk=jwk,
        att=f"{header}.{payload}.c2ln",
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen=seen,
        last_seen=seen,
        verified=True,
    )


def _rows(db: Any) -> list[tuple[str, str]]:
    """Every stored receipt-key row: its entity id and its body."""

    return sorted(
        (entity_id, row.body)
        for (kind, entity_id), row in db.rows.items()
        if kind == RECEIPT_KEY_KIND
    )


def test_reobserving_a_stored_history_document_commits_nothing() -> None:
    store, db = make_fake_store()
    history = _document("history")
    assert store.observe_receipt_key(history, refresh_last_seen=False) == "appended"
    stored = _rows(db)
    transactions, commits = len(db.transaction_tags), db.commits
    assert transactions == 1

    for minute in range(50):
        later = dataclasses.replace(history, last_seen=f"2026-10-02T00:{minute:02d}:00Z")
        assert store.observe_receipt_key(later, refresh_last_seen=False) == "unchanged"

    assert len(db.transaction_tags) == transactions
    assert db.commits == commits
    assert _rows(db) == stored


def test_a_document_not_yet_stored_still_takes_the_transaction() -> None:
    store, db = make_fake_store()
    assert store.observe_receipt_key(_document("first"), refresh_last_seen=False) == "appended"
    # Another document of the same key is a new row, not a re-observation.
    assert store.observe_receipt_key(_document("second"), refresh_last_seen=False) == "appended"
    assert len(db.transaction_tags) == 2
    assert len(_rows(db)) == 2


def test_a_refreshing_observation_is_not_short_circuited() -> None:
    store, db = make_fake_store()
    current = _document("current")
    assert store.observe_receipt_key(current) == "appended"
    later = dataclasses.replace(current, last_seen="2026-10-03T00:00:00Z")
    assert store.observe_receipt_key(later) == "refreshed"
    assert len(db.transaction_tags) == 2
    (row,) = store.list_receipt_keys(kid=current.kid)
    assert row.last_seen == "2026-10-03T00:00:00Z"


def _seed(store: Any, state: str) -> None:
    history = _document("history")
    if state == "stored":
        assert store.observe_receipt_key(history, refresh_last_seen=False) == "appended"
    elif state == "another-document":
        assert store.observe_receipt_key(_document("other"), refresh_last_seen=False) == "appended"
    elif state == "legacy-same-document":
        store._write_entity(RECEIPT_KEY_KIND, history.kid, history)
    elif state == "legacy-other-document":
        store._write_entity(RECEIPT_KEY_KIND, history.kid, _document("other"))
    elif state == "stored-beside-a-legacy-row":
        # The document is stored, and an older one of the same key is still in
        # the legacy form. The observation changes nothing in the document,
        # but the transaction must still run to migrate the legacy row.
        assert store.observe_receipt_key(history, refresh_last_seen=False) == "appended"
        store._write_entity(RECEIPT_KEY_KIND, history.kid, _document("other"))
    else:
        assert state == "absent"


@pytest.mark.parametrize(
    "state",
    [
        "absent",
        "stored",
        "another-document",
        "legacy-same-document",
        "legacy-other-document",
        "stored-beside-a-legacy-row",
    ],
)
@pytest.mark.parametrize("refresh_last_seen", [False, True])
def test_the_snapshot_answer_is_the_transactions_answer(
    monkeypatch: pytest.MonkeyPatch, state: str, refresh_last_seen: bool
) -> None:
    """Differential: with the short cut and without it, the same outcome and rows."""

    observed = _document("history", seen="2026-10-02T00:00:00Z")
    results = []
    for short_cut in (True, False):
        store, db = make_fake_store()
        _seed(store, state)
        if not short_cut:
            monkeypatch.setattr(
                SpannerStore, "_stored_history_outcome", lambda *_args, **_kwargs: None
            )
        outcome = store.observe_receipt_key(observed, refresh_last_seen=refresh_last_seen)
        results.append((outcome, _rows(db)))
        monkeypatch.undo()
    assert results[0] == results[1]
    # And the legacy row, where there was one, is gone either way.
    if "legacy" in state:
        legacy_ids = {entity_id for entity_id, _ in results[0][1]}
        assert observed.kid not in legacy_ids
        assert receipt_key_entity_id(
            observed.kid, receipt_attestation_sha256(observed.att, observed.att_kind)
        ) in legacy_ids
