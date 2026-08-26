"""Skills Extension for the TrustedRouter MCP server (SEP-2640).

WHY. The model-advisor skill's canonical copy lives in its own repository
(Lore-Hex/LLM-advisor) so Codex, Claude Code, Hermes and others can share one
playbook. Today every agent has to know that URL and fetch it by hand, and the
copy vendored here is a POINTER file that goes stale the moment the canonical
one changes. Serving the skill over MCP inverts that: a client that has already
connected to TrustedRouter discovers the skill through the protocol and gets
the CURRENT text on every refresh, with no client-side change when the skill
is edited. That is the "upgrade continuously" property.

PROTOCOL. This implements the Skills Extension as specified in SEP-2640
(Extensions Track, Resources-based):

    capability   "extensions": {"io.modelcontextprotocol/skills": {}}
    methods      skills/list  (paginated via nextCursor), skills/get
    URIs         skill://<skill-path>/SKILL.md
    entry        {name, description, resources: [{uri, digest, size}] | "dynamic",
                  plus SKILL.md frontmatter verbatim}
    limits       512 resources and 16 MiB per skill
    freshness    ttlMs / cacheScope hints, matching tools/list semantics

SEP-2640 is IN REVIEW, not ratified. It is an extension precisely so servers
can ship it before ratification: a client that does not understand the
capability simply never calls the methods, and nothing else in this server
changes. If the SEP moves, this module is the only thing that moves with it.

FRESHNESS vs AVAILABILITY. The canonical text is fetched over the network, so
this module never lets that fetch become a hard dependency of the MCP server:
content is cached for ``CACHE_TTL_SECONDS``, a failed refresh serves the last
known-good copy rather than an error, and the very first fetch failing degrades
to the vendored pointer text. A skill server that 500s because GitHub is slow
would be worse than a slightly stale skill.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Extension capability key from SEP-2640.
SKILLS_EXTENSION_KEY = "io.modelcontextprotocol/skills"

#: The one skill this server publishes today.
SKILL_NAME = "trustedrouter-model-advisor"
SKILL_URI = f"skill://{SKILL_NAME}/SKILL.md"
CANONICAL_URL = "https://raw.githubusercontent.com/Lore-Hex/LLM-advisor/main/SKILL.md"
CANONICAL_REPO = "https://github.com/Lore-Hex/LLM-advisor"

#: Cache lifetime for the canonical text, and the ttlMs hint handed to clients.
#: Short enough that an edit to the skill reaches agents the same session,
#: long enough that a busy client does not turn every skills/list into an
#: egress request.
CACHE_TTL_SECONDS = 900
FETCH_TIMEOUT_SECONDS = 5.0

#: SEP-2640 per-skill ceilings. Enforced here rather than assumed: a canonical
#: file that grows past the limit must fail loudly at the server, not produce a
#: response clients are entitled to reject.
MAX_RESOURCES_PER_SKILL = 512
MAX_SKILL_BYTES = 16 * 1024 * 1024

#: Served when the canonical fetch has never succeeded. Mirrors the vendored
#: pointer file so an offline server still tells the agent where to look.
_FALLBACK_TEXT = f"""---
name: {SKILL_NAME}
description: Pointer to the canonical TrustedRouter model advisor skill.
---

# TrustedRouter Model Advisor

The canonical copy could not be fetched from {CANONICAL_REPO}. Read it directly:

- Raw `SKILL.md`: {CANONICAL_URL}
"""


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split leading YAML-ish frontmatter from a SKILL.md body.

    Deliberately not a YAML parser: SEP-2640 asks for the frontmatter
    "verbatim", skill frontmatter is flat ``key: value`` by convention, and
    taking a YAML dependency here would let a malformed skill file execute
    parser edge cases inside the MCP request path.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}, text
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields, "\n".join(lines[closing + 1 :]).lstrip("\n")


@dataclass
class _Cached:
    text: str
    digest: str
    size: int
    fetched_at: float
    is_fallback: bool = True


@dataclass
class SkillsRegistry:
    """Serves the advisor skill, refreshing it from the canonical repository.

    One instance per server process. ``_cached`` is the last known-good copy
    and is only ever replaced by a SUCCESSFUL fetch, which is what makes a
    GitHub outage a staleness problem instead of an availability one.
    """

    client_factory: Any = None
    _cached: _Cached | None = field(default=None, repr=False)

    def _fetch(self) -> str | None:
        try:
            if self.client_factory is not None:
                return str(self.client_factory(CANONICAL_URL))
            response = httpx.get(
                CANONICAL_URL, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
            )
            if response.status_code != 200:
                return None
            return response.text
        except Exception:
            # Any failure -> serve what we already have. Never propagate.
            return None

    def current(self, *, now: float | None = None, force: bool = False) -> _Cached:
        moment = time.monotonic() if now is None else now
        cached = self._cached
        fresh = (
            cached is not None
            and not cached.is_fallback
            and (moment - cached.fetched_at) < CACHE_TTL_SECONDS
        )
        if fresh and not force:
            assert cached is not None
            return cached
        text = self._fetch()
        if text is None:
            if cached is not None:
                return cached
            return _Cached(
                text=_FALLBACK_TEXT,
                digest=_sha256(_FALLBACK_TEXT),
                size=len(_FALLBACK_TEXT.encode("utf-8")),
                fetched_at=moment,
                is_fallback=True,
            )
        size = len(text.encode("utf-8"))
        if size > MAX_SKILL_BYTES:
            raise SkillTooLarge(
                f"{SKILL_NAME}: canonical SKILL.md is {size} bytes, over the "
                f"SEP-2640 ceiling of {MAX_SKILL_BYTES}"
            )
        self._cached = _Cached(
            text=text,
            digest=_sha256(text),
            size=size,
            fetched_at=moment,
            is_fallback=False,
        )
        return self._cached

    def entry(self, *, force: bool = False) -> dict[str, Any]:
        """One SEP-2640 skill entry."""
        cached = self.current(force=force)
        fields, _body = parse_frontmatter(cached.text)
        resources = [{"uri": SKILL_URI, "digest": f"sha256:{cached.digest}", "size": cached.size}]
        if len(resources) > MAX_RESOURCES_PER_SKILL:  # pragma: no cover - single resource today
            raise SkillTooLarge(f"{SKILL_NAME}: over {MAX_RESOURCES_PER_SKILL} resources")
        return {
            "name": fields.get("name", SKILL_NAME),
            "description": fields.get("description", ""),
            "resources": resources,
            "frontmatter": fields,
            "_meta": {
                "source": CANONICAL_URL,
                "stale": cached.is_fallback,
            },
        }

    def list_result(self, *, force: bool = False) -> dict[str, Any]:
        """`skills/list`. No nextCursor: one skill fits in one page, and an
        absent cursor is how the spec spells "that was everything"."""
        return {
            "skills": [self.entry(force=force)],
            "ttlMs": CACHE_TTL_SECONDS * 1000,
            "cacheScope": "session",
        }

    def get_result(self, *, name: str | None = None, uri: str | None = None) -> dict[str, Any]:
        """`skills/get`. Always forces a refresh -- a client calling get is
        asking whether the skill CHANGED, so answering from cache would defeat
        the point of serving it over the protocol at all."""
        if name and name != SKILL_NAME:
            raise SkillNotFound(f"Unknown skill: {name}")
        if uri and uri != SKILL_URI:
            raise SkillNotFound(f"Unknown skill uri: {uri}")
        return {"skill": self.entry(force=True)}

    def list_resources(self) -> list[dict[str, Any]]:
        """`resources/list`. The skill's files as ordinary MCP resources, so a
        client that understands Resources but not the Skills Extension can
        still see -- and read -- the advisor text."""
        cached = self.current()
        return [
            {
                "uri": SKILL_URI,
                "name": f"{SKILL_NAME}/SKILL.md",
                "description": "TrustedRouter model advisor skill (canonical, refreshed).",
                "mimeType": "text/markdown",
                "size": cached.size,
            }
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        """`resources/read` for a skill:// URI.

        SEP-2640 is explicit that reading this URI grants no approval on its
        own -- a skill is loaded through the host's skill-loading path. This
        returns the bytes; it does not activate anything.
        """
        if uri != SKILL_URI:
            raise SkillNotFound(f"Unknown skill uri: {uri}")
        cached = self.current()
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": cached.text}]}


class SkillNotFound(LookupError):
    pass


class SkillTooLarge(ValueError):
    pass


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
