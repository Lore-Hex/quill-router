"""Provider-scoped pseudonyms, never a raw account identifier or API key."""

import hashlib
import re


def owner_digest(key_owner_id: str | None, workspace_id: str) -> str:
    subject = f"user\0{key_owner_id}" if key_owner_id else f"workspace\0{workspace_id}"
    return hashlib.sha256(f"trustedrouter/openai-safety/v1\0{subject}".encode()).hexdigest()


def valid_digest(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) else ""
