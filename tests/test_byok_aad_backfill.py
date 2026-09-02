from __future__ import annotations

import copy
from typing import Any

from scripts import backfill_byok_aad_v2 as retired_cli
from trusted_router.byok_aad_backfill import BackfillRunner, EntityRow
from trusted_router.byok_crypto import ALGORITHM_V2
from trusted_router.byok_v1_attestations import V1_ALGORITHM_LITERAL


class MemoryEntityStore:
    def __init__(self, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.rows = copy.deepcopy(rows)

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        keys = sorted(key for key in self.rows if after is None or key > after)[:limit]
        return [
            EntityRow(
                kind=kind,
                entity_id=entity_id,
                body=copy.deepcopy(self.rows[(kind, entity_id)]),
            )
            for kind, entity_id in keys
        ]


def _envelope(algorithm: str) -> dict[str, str]:
    return {
        "algorithm": algorithm,
        "key_ref": "fixture-key",
        "encrypted_dek": "ZGVr",
        "dek_nonce": "bm9uY2U=",
        "ciphertext": "Y2lwaGVydGV4dA==",
        "nonce": "bm9uY2U=",
    }


def test_retired_backfill_classifies_v1_without_mutating_it() -> None:
    body = {
        "workspace_id": "workspace-1",
        "provider": "anthropic",
        "encrypted_secret": _envelope(V1_ALGORITHM_LITERAL),
    }
    store = MemoryEntityStore({("byok", "workspace-1#anthropic"): body})

    result = BackfillRunner(store, reporter=lambda _message: None).run()

    assert result.v1_envelopes == 1
    assert result.rows_with_v1 == 1
    assert store.rows[("byok", "workspace-1#anthropic")] == body


def test_v2_and_missing_optional_fields_are_counted() -> None:
    store = MemoryEntityStore(
        {
            ("byok", "a"): {
                "workspace_id": "w",
                "provider": "anthropic",
                "encrypted_secret": _envelope(ALGORITHM_V2),
            },
            ("broadcast_destination", "b"): {"workspace_id": "w"},
        }
    )

    result = BackfillRunner(store, reporter=lambda _message: None).run(batch_size=1)

    assert result.rows_scanned == 2
    assert result.v2_envelopes == 1
    assert result.missing_envelopes == 2


def test_unknown_or_malformed_envelopes_fail_closed_without_logging_payloads() -> None:
    secret_marker = "must-not-appear"  # noqa: S105 - redaction sentinel, not a credential
    store = MemoryEntityStore(
        {
            ("byok", "a"): {
                "workspace_id": "w",
                "provider": "anthropic",
                "encrypted_secret": {"algorithm": "future-v99", "ciphertext": secret_marker},
            },
            ("byok", "b"): {
                "workspace_id": "w",
                "provider": "openai",
                "encrypted_secret": secret_marker,
            },
        }
    )
    logs: list[str] = []

    result = BackfillRunner(store, reporter=logs.append).run()

    assert result.unsupported_algorithms == 1
    assert result.failures == 1
    assert secret_marker not in "\n".join(logs)


def test_resume_cursor_and_small_batches_do_not_repeat_rows() -> None:
    store = MemoryEntityStore(
        {
            ("broadcast_destination", "a"): {"workspace_id": "w"},
            ("byok", "a"): {"workspace_id": "w", "provider": "a"},
            ("byok", "b"): {"workspace_id": "w", "provider": "b"},
        }
    )

    result = BackfillRunner(store, reporter=lambda _message: None).run(
        batch_size=1,
        after=("broadcast_destination", "a"),
    )

    assert result.rows_scanned == 2
    assert result.missing_envelopes == 2


def test_retired_mutating_cli_refuses_and_points_to_the_safe_audit(
    capsys: Any,
) -> None:
    assert retired_cli.main() == 2
    assert "scripts/check_no_v1_envelopes.py" in capsys.readouterr().out
