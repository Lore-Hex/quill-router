"""Spanner-backed SES bounce/complaint suppression list.

Sibling of InMemoryEmailBlocks (storage_email_blocks.py). Stores per-email
suppression entries plus an SNS message-id replay guard."""

from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_codec import normalize_email
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import EmailSendBlock, iso_now


class SpannerEmailBlocks:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def block(
        self,
        *,
        email: str,
        reason: str,
        bounce_type: str | None = None,
        feedback_id: str | None = None,
    ) -> EmailSendBlock:
        normalized = normalize_email(email)
        block = EmailSendBlock(
            email=normalized,
            reason=reason,
            bounce_type=bounce_type,
            feedback_id=feedback_id,
        )
        self._io.write_entity("email_block", normalized, block)
        return block

    def is_blocked(self, email: str) -> bool:
        return self._io.read_entity("email_block", normalize_email(email), EmailSendBlock) is not None

    def get(self, email: str) -> EmailSendBlock | None:
        return self._io.read_entity("email_block", normalize_email(email), EmailSendBlock)

    def record_message_once(self, message_id: str) -> bool:
        def txn(transaction: Any) -> bool:
            if self._io.read_entity_tx(transaction, "sns_message", message_id, dict) is not None:
                return False
            self._io.write_entity_tx(transaction, "sns_message", message_id, {"created_at": iso_now()})
            return True

        return self._io.database.run_in_transaction(txn)
