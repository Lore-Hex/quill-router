"""Email send-block list — the SES bounce/complaint suppression layer.

Lives outside storage.py so the in-memory store stays small and adding
fields here doesn't churn the main module. SpannerBigtableStore has its
own implementation; both satisfy the Store Protocol.
"""

from __future__ import annotations

import threading

from trusted_router.storage_gcp_codec import normalize_email
from trusted_router.storage_models import EmailSendBlock


class InMemoryEmailBlocks:
    """Per-email suppression list + SNS message-id replay guard.

    The dicts are exposed as public attributes for tests that want to
    inspect state directly (`store.email_blocks._blocks`)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._blocks: dict[str, EmailSendBlock] = {}
        self._processed_messages: set[str] = set()

    def reset(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._processed_messages.clear()

    def block(
        self,
        *,
        email: str,
        reason: str,
        bounce_type: str | None = None,
        feedback_id: str | None = None,
    ) -> EmailSendBlock:
        with self._lock:
            normalized = normalize_email(email)
            block = EmailSendBlock(
                email=normalized,
                reason=reason,
                bounce_type=bounce_type,
                feedback_id=feedback_id,
            )
            self._blocks[normalized] = block
            return block

    def is_blocked(self, email: str) -> bool:
        with self._lock:
            return normalize_email(email) in self._blocks

    def get(self, email: str) -> EmailSendBlock | None:
        with self._lock:
            return self._blocks.get(normalize_email(email))

    def record_message_once(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._processed_messages:
                return False
            self._processed_messages.add(message_id)
            return True
