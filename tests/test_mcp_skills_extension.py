"""Skills Extension (SEP-2640) on the TrustedRouter MCP server.

The point of serving the advisor skill over MCP is that an agent already
connected to TrustedRouter picks up skill edits with no client change. These
tests pin the two things that property depends on: a refresh really re-reads
the canonical source, and a canonical source that is unreachable degrades to
STALE rather than to an error.
"""

from __future__ import annotations

import pytest

from trusted_router.mcp_skills import (
    CACHE_TTL_SECONDS,
    MAX_SKILL_BYTES,
    SKILL_NAME,
    SKILL_URI,
    SKILLS_EXTENSION_KEY,
    SkillNotFound,
    SkillsRegistry,
    SkillTooLarge,
    parse_frontmatter,
)

V1 = "---\nname: trustedrouter-model-advisor\ndescription: First.\n---\n\n# One\n"
V2 = "---\nname: trustedrouter-model-advisor\ndescription: Second.\n---\n\n# Two\n"


class _Source:
    """Canonical source whose content and availability the test controls."""

    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls = 0

    def __call__(self, _url: str) -> str:
        self.calls += 1
        if self.text is None:
            raise RuntimeError("canonical source unreachable")
        return self.text


class TestContinuousUpgrade:
    def test_get_always_refetches_so_an_edit_reaches_the_agent(self) -> None:
        src = _Source(V1)
        reg = SkillsRegistry(client_factory=src)
        assert reg.get_result(name=SKILL_NAME)["skill"]["description"] == "First."
        src.text = V2  # the skill is edited upstream
        assert reg.get_result(name=SKILL_NAME)["skill"]["description"] == "Second."
        assert src.calls == 2, "skills/get must re-read; a cached answer defeats the feature"

    def test_list_serves_cache_within_ttl_then_refreshes(self) -> None:
        src = _Source(V1)
        reg = SkillsRegistry(client_factory=src)
        reg.current(now=0.0)
        reg.current(now=CACHE_TTL_SECONDS - 1)
        assert src.calls == 1, "inside the TTL a list must not hit the network"
        src.text = V2
        reg.current(now=CACHE_TTL_SECONDS + 1)
        assert src.calls == 2
        assert reg.entry()["description"] == "Second."

    def test_digest_changes_with_content(self) -> None:
        src = _Source(V1)
        reg = SkillsRegistry(client_factory=src)
        first = reg.entry()["resources"][0]["digest"]
        src.text = V2
        second = reg.entry(force=True)["resources"][0]["digest"]
        assert first != second and first.startswith("sha256:")


class TestAvailabilityIsNotHostage:
    def test_unreachable_source_serves_last_known_good_not_an_error(self) -> None:
        src = _Source(V1)
        reg = SkillsRegistry(client_factory=src)
        reg.current(now=0.0)
        src.text = None  # canonical source goes down
        entry = reg.entry(force=True)
        assert entry["description"] == "First.", "a failed refresh must serve the cached skill"
        assert entry["_meta"]["stale"] is False

    def test_never_fetched_and_unreachable_degrades_to_a_pointer(self) -> None:
        reg = SkillsRegistry(client_factory=_Source(None))
        entry = reg.entry()
        assert entry["name"] == SKILL_NAME
        assert entry["_meta"]["stale"] is True
        body = reg.read_resource(SKILL_URI)["contents"][0]["text"]
        assert "LLM-advisor" in body, "offline fallback must still name the canonical source"


class TestSpecConformance:
    def test_list_shape_matches_sep_2640(self) -> None:
        reg = SkillsRegistry(client_factory=_Source(V1))
        result = reg.list_result()
        assert set(result) == {"skills", "ttlMs", "cacheScope"}
        entry = result["skills"][0]
        assert {"name", "description", "resources"} <= set(entry)
        res = entry["resources"][0]
        assert set(res) == {"uri", "digest", "size"}
        assert res["uri"] == SKILL_URI and res["uri"].startswith("skill://")

    def test_unknown_name_or_uri_is_rejected(self) -> None:
        reg = SkillsRegistry(client_factory=_Source(V1))
        with pytest.raises(SkillNotFound):
            reg.get_result(name="not-a-skill")
        with pytest.raises(SkillNotFound):
            reg.read_resource("skill://nope/SKILL.md")

    def test_oversized_skill_is_refused_not_truncated(self) -> None:
        big = "---\nname: x\ndescription: y\n---\n" + ("a" * (MAX_SKILL_BYTES + 1))
        reg = SkillsRegistry(client_factory=_Source(big))
        with pytest.raises(SkillTooLarge):
            reg.current()

    def test_frontmatter_parsing_is_verbatim_and_total(self) -> None:
        fields, body = parse_frontmatter(V1)
        assert fields == {"name": SKILL_NAME, "description": "First."}
        assert body.strip() == "# One"
        assert parse_frontmatter("no frontmatter") == ({}, "no frontmatter")
        assert parse_frontmatter("---\nunterminated: yes\n")[0] == {}


class TestServerAdvertisesTheExtension:
    def test_initialize_declares_the_capability(self, client, inference_headers) -> None:
        response = client.post(
            "/mcp",
            headers={**inference_headers, "content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert response.status_code == 200
        caps = response.json()["result"]["capabilities"]
        assert SKILLS_EXTENSION_KEY in caps.get("extensions", {})
        assert "resources" in caps

    def test_skills_list_is_reachable_over_jsonrpc(
        self, client, inference_headers, monkeypatch
    ) -> None:
        # Pin the canonical source so this test asserts protocol wiring, not
        # GitHub's availability.
        import trusted_router.mcp_skills as skills

        monkeypatch.setattr(
            skills.SkillsRegistry,
            "_fetch",
            lambda self: "---\nname: trustedrouter-model-advisor\ndescription: d\n---\n",
        )
        response = client.post(
            "/mcp",
            headers={**inference_headers, "content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 2, "method": "skills/list", "params": {}},
        )
        assert response.status_code == 200
        body = response.json()
        assert "result" in body, body
        names = [s["name"] for s in body["result"]["skills"]]
        assert SKILL_NAME in names
