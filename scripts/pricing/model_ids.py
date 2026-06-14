"""Shared provider-native model id normalization for pricing refreshes.

Provider APIs return native IDs in their own casing and namespace. The
catalog indexes by TrustedRouter/OpenRouter-style canonical IDs, so every
API-backed provider adapter needs the same conservative normalization:
explicit hand maps win for tricky aliases, and simple vendor/model IDs are
lowercased with known author namespace rewrites.
"""
from __future__ import annotations

import re

_AUTHOR_ALIASES = {
    "deepseek-ai": "deepseek",
    "minimaxai": "minimax",
    "moonshotai": "moonshotai",
    "qwen": "qwen",
    "zai-org": "z-ai",
    "zhipuai": "z-ai",
    "zhipu-ai": "z-ai",
    "xai": "x-ai",
    "x-ai": "x-ai",
}

_MODEL_CHARS_RE = re.compile(r"[^a-z0-9._-]+")


def canonicalize_native_model_id(native_id: str) -> str | None:
    """Best-effort conversion from provider-native ID to catalog ID.

    This intentionally does not strip semantic suffixes like ``-turbo`` or
    ``-preview`` because those can be distinct billable models. Provider files
    should keep explicit maps for those cases. The fallback is for straightforward
    new IDs like ``moonshotai/Kimi-K2.7-Code`` -> ``moonshotai/kimi-k2.7-code``.
    """

    raw = native_id.strip()
    if not raw or "/" not in raw:
        return None

    author, model = raw.split("/", 1)
    author_key = author.strip().casefold()
    model_slug = model.strip().casefold()
    if not author_key or not model_slug:
        return None

    canonical_author = _AUTHOR_ALIASES.get(author_key, author_key)
    model_slug = model_slug.replace(" ", "-")
    model_slug = _MODEL_CHARS_RE.sub("-", model_slug)
    model_slug = re.sub(r"-{2,}", "-", model_slug).strip("-")
    if not model_slug:
        return None
    return f"{canonical_author}/{model_slug}"


def mapped_or_canonical_model_id(
    native_id: str,
    explicit_map: dict[str, str],
) -> str | None:
    """Return the explicit mapping or the conservative canonical fallback."""

    return explicit_map.get(native_id) or canonicalize_native_model_id(native_id)


def remember_upstream_id(
    upstream_map: dict[str, str],
    canonical_id: str,
    native_id: str,
) -> None:
    """Record the provider-native ID used to call upstream.

    ``refresh._merge_snapshot`` reads each provider module's ``UPSTREAM_ID_MAP``
    after ``fetch()`` runs. Mutating this map during refresh lets newly
    discovered models keep the exact upstream request ID in the committed
    snapshot without hand-editing the provider module.
    """

    upstream_map.setdefault(canonical_id, native_id)
