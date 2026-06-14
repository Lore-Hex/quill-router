from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BlogPost:
    slug: str
    title: str
    description: str
    published_date: str
    source_label: str | None
    source_url: str | None
    body_html: str

    @property
    def href(self) -> str:
        return f"/blog/{self.slug}"


BLOG_POSTS: tuple[BlogPost, ...] = (
    BlogPost(
        slug="fusion-evals-open-source",
        title="Fusion eval results",
        description=(
            "TrustedRouter is reproducing Fusion-style DRACO evals with exact "
            "criterion scoring before publishing a headline comparison."
        ),
        published_date="2026-06-14",
        source_label="OpenRouter Fusion announcement",
        source_url="https://openrouter.ai/blog/announcements/fusion-beats-frontier/",
        body_html="""
<p><strong>Reproducing Fusion in the open.</strong> TrustedRouter is running the same class of routing experiment with public code, explicit model lists, and measurable cost/quality tradeoffs instead of a hidden benchmark harness.</p>
<p>Comparable full-run results are not published yet. The prior holistic-judge run is excluded from this post because it does not match OpenRouter's DRACO scoring method.</p>
<h2>Reference Results</h2>
<table class="data-table">
  <thead><tr><th>Run</th><th>OpenRouter score</th><th>TrustedRouter score</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Solo Gemini 3 Flash</td><td>43.1</td><td>29.35 on 10-task smoke</td><td>Investigating</td></tr>
    <tr><td>Solo Kimi K2.6</td><td>53.7</td><td>Not enough completed rows</td><td>Investigating</td></tr>
    <tr><td>Solo DeepSeek V4 Pro</td><td>60.3</td><td>Not run with exact scorer yet</td><td>Pending</td></tr>
    <tr><td>Fusion budget panel</td><td>64.7</td><td>Not run with exact scorer yet</td><td>Pending</td></tr>
  </tbody>
</table>
<h2>Replication Rules</h2>
<ul class="plain-list">
  <li>Mode: <span class="mono">micro-hybrid</span> runs the small public smoke before any expensive full pass.</li>
  <li>Judge model: <span class="mono">google/gemini-3.1-pro-preview</span>.</li>
  <li>Scoring: DRACO criterion-level grading, three independent passes, normalized 0-100.</li>
  <li>Search: Exa with DRACO/rubric hostnames excluded and result leakage checks enabled.</li>
  <li>Publication rule: raw solo baselines must be close before any Fusion headline is published.</li>
</ul>
<p>The exact scorer and leakage guard are implemented in the open-source harness. Full comparable results will replace this table when the raw baselines replicate.</p>
""",
    ),
    BlogPost(
        slug="one-api-all-llms-provably-private",
        title="One API, all the LLMs, with a prompt path you can verify",
        description=(
            "TrustedRouter gives developers OpenAI-compatible model routing while "
            "keeping the prompt path separate from the control plane."
        ),
        published_date="2026-06-14",
        source_label="Joseph Perla original",
        source_url="https://www.jperla.com/blog/trustedrouter-one-api-all-llms-provably-private",
        body_html="""
<p>Developers want the OpenRouter shape because it removes switching cost. One base URL, many models, fallback when a provider fails, and one ledger for usage. The missing piece is verifiable trust.</p>
<p>TrustedRouter keeps the dashboard and billing surface separate from the attested API gateway. The hosted prompt path is designed so the running code, image digest, and attestation evidence can be checked instead of taken on faith.</p>
<h2>What that gives a developer</h2>
<ul class="plain-list">
  <li>Keep the OpenAI SDK and change the base URL.</li>
  <li>Route to hundreds of models across many providers.</li>
  <li>Use <span class="mono">trustedrouter/zdr</span> when zero-retention providers matter.</li>
  <li>Use <span class="mono">trustedrouter/e2e</span> for confidential provider routes where available.</li>
  <li>Verify the hosted gateway on <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>.</li>
</ul>
<p>The point is not that every upstream model provider becomes confidential by magic. The point is that the router should be honest about where the guarantee starts, where it ends, and which provider route was selected.</p>
""",
    ),
    BlogPost(
        slug="attestation-is-all-you-need",
        title="Attestation is all you need",
        description=(
            "For AI routing, trust should be something an agent can verify, not "
            "only a policy page a human reads after the fact."
        ),
        published_date="2026-06-14",
        source_label="Joseph Perla original",
        source_url="https://www.jperla.com/blog/attestation-is-all-you-need",
        body_html="""
<p>Policy matters, but policy alone is not enough for high-value prompts. A router should make it possible to verify what code is receiving the request and whether that code matches the open source release.</p>
<p>That is why TrustedRouter treats attestation as part of the product surface. A user or agent can check the trust page, compare source commits and release digests, and then decide whether a route meets the workload's privacy bar.</p>
<h2>The practical split</h2>
<ul class="plain-list">
  <li>The control plane manages accounts, keys, billing, docs, and status.</li>
  <li>The API plane handles prompt traffic through the attested gateway.</li>
  <li>Provider pages show upstream retention and confidential-compute posture separately.</li>
  <li>Legal and procurement pages state what is ready now and what still requires a signed agreement.</li>
</ul>
<p>This makes the system legible. A lawyer can read the DPA and subprocessor list. An engineer can inspect the code. An agent can verify attestation before routing sensitive work.</p>
""",
    ),
)

BLOG_POSTS_BY_SLUG: dict[str, BlogPost] = {post.slug: post for post in BLOG_POSTS}
