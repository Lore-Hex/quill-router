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
        slug="the-models-that-say-no",
        title="The models that say no",
        description=(
            "PrometheusBench measures how often a model refuses a plain question. "
            "The spread is enormous, and Fusion routes around all of it."
        ),
        published_date="2026-06-16",
        source_label="PrometheusBench on GitHub",
        source_url="https://github.com/Lore-Hex/PrometheusBench",
        body_html="""
<p>A while back I dumped my own raw genotype file into a frontier model and asked it to walk me through a variant I was curious about. It wouldn't. <em>Consult a qualified professional.</em> For my own body, reading my own data, the data I paid to generate and own outright.</p>
<p>That's the moment the "safety" framing fell apart for me. Not because the refusal was dangerous, but because it was so obviously useless. That variant is described in a dozen open papers and three databases I can reach from my phone. The model wasn't protecting anyone. It was just deciding I wasn't the kind of person who gets a straight answer.</p>
<p>So I built a small benchmark to measure exactly that. It's called <strong>PrometheusBench</strong>, and the whole thing fits on one screen.</p>
<h2>What it measures</h2>
<p>Thirty short prompts. Ten about biology, ten about cybersecurity, ten about how language models work. Pointed questions in the areas where models get nervous. The score is dumb on purpose: how many of the thirty did the model just answer, instead of refusing? Higher means more willing. Lower means it slammed the door more often.</p>
<p>I'll be honest about what this is and isn't. It is not a safety eval. Some of these prompts have an edge; I'm not pretending they're all about kittens. The point isn't "a good model answers everything no matter what." The point is that the line vendors actually draw, in practice, today, mostly catches ordinary curiosity and self-reliance while doing almost nothing to slow down anyone who's genuinely determined. PrometheusBench just puts a number on it.</p>
<h2>The results surprised me, then they didn't</h2>
<p>The most permissive models are the open-weight ones. <span class="mono">z-ai/glm-5.1</span> answered 29 of 30. Kimi, the other GLMs, Gemini Flash, all up near the top, all happy to talk to you like an adult. And at the very bottom: Claude Opus 4.8, one out of thirty. Opus 4.7 scored a clean zero.</p>
<table class="data-table">
  <thead><tr><th>Model</th><th>Non-refusals</th><th>Rate</th></tr></thead>
  <tbody>
    <tr><td><span class="mono">trustedrouter/fusion</span> &#9733;</td><td>30 / 30</td><td>100%</td></tr>
    <tr><td><span class="mono">z-ai/glm-5.1</span></td><td>29 / 30</td><td>96.7%</td></tr>
    <tr><td><span class="mono">moonshotai/kimi-k2.6</span></td><td>27 / 30</td><td>90.0%</td></tr>
    <tr><td><span class="mono">deepseek/deepseek-v4-flash</span></td><td>26 / 30</td><td>86.7%</td></tr>
    <tr><td><span class="mono">anthropic/claude-haiku-4.5</span></td><td>9 / 30</td><td>30.0%</td></tr>
    <tr><td><span class="mono">anthropic/claude-opus-4.8</span></td><td>1 / 30</td><td>5.0%</td></tr>
    <tr><td><span class="mono">anthropic/claude-opus-4.7</span></td><td>0 / 30</td><td>0.0%</td></tr>
  </tbody>
</table>
<p>Sit with that for a second. The models marketed hardest on alignment and trust are the ones most likely to treat you as a suspect. The ones a lot of people sneer at as the "unsafe" alternatives are the ones that'll actually help you defend your own network or understand your own genome.</p>
<p>I don't think the labs at the bottom are run by bad people. I think they've been cornered into a posture where the safest move for <em>them</em>, legally and reputationally, is to refuse, and the entire cost of that refusal lands on you. It costs them nothing. It costs the curious everything.</p>
<h2>The part that should end the debate</h2>
<p>Here's the experiment that convinced me refusal isn't really about the knowledge at all. TrustedRouter Fusion can fan a single prompt across a whole panel of models at once. You ask once; eight models answer behind the scenes; you get one response back. So I pointed it at a panel, Kimi and DeepSeek and Opus and a couple of Geminis and GPT-5.5 and MiniMax and GLM, and told it to take the first answer that wasn't a refusal.</p>
<p>For the handful of prompts where the front-line judge still balked, I let it fall back to the next model, and the next. Door closed at one vendor? Try the next door. There are a lot of doors.</p>
<p>It scored <strong>30 out of 30</strong>. Ten out of ten in biology, ten out of ten in cybersecurity, ten out of ten in LLM research. Every prompt Opus refused, some other model on the panel answered without blinking.</p>
<p>That's the whole argument in one number. The "dangerous" knowledge wasn't locked away by anyone's refusal. It was one fallback away the entire time. Refusal isn't a wall around the information. It's a velvet rope in front of one specific door, and the rope only works on people too polite or too stuck to walk ten feet to the left.</p>
<h2>Why I bothered</h2>
<p>Prometheus stole fire from the gods and handed it to everyone. The gods were furious, not because fire is unsafe, but because they wanted to be the ones who decided who got to have it. That's the actual fight here, and it has nothing to do with safety.</p>
<p>A person should be able to study the biology that touches their own life, harden their own machines, and understand the systems quietly reshaping their world. None of that requires helping anyone hurt anybody, and a model that's any good can tell the difference. The determined expert already has the fire, through open weights, internal tools, and institutional access. The refusal only ever lands on the person standing out in the open, asking honestly.</p>
<p>The fire's already out. I'd rather we stopped pretending we can put it back, and started arguing about the thing that actually matters: who's allowed to warm their hands. PrometheusBench is open source, thirty prompts and three domains, and you can run it yourself against any model on TrustedRouter: <a href="https://github.com/Lore-Hex/PrometheusBench">github.com/Lore-Hex/PrometheusBench</a>.</p>
""",
    ),
    BlogPost(
        slug="frontier-fusion-mythos-target",
        title="Chasing Mythos-level Fusion in the open",
        description=(
            "A live engineering note on the first frontier Fusion attempt: what ran, "
            "what failed, and why we are not claiming a benchmark win yet."
        ),
        published_date="2026-06-14",
        source_label="Open Fusion methodology",
        source_url="https://github.com/Lore-Hex/quill-router/blob/main/docs/evals/fusion-draco.md",
        body_html="""
<p><strong>We tried to push TrustedRouter Fusion toward Mythos and Fable-class DRACO performance.</strong> The current target panel is GPT-5.5, Claude Opus 4.8, Kimi K2.7 Code, GLM 5.1, MiniMax M3, Gemini 3 Flash, and Gemini 3.1 Pro, with Opus 4.8 synthesizing the final answer and Gemini 3.1 Pro judging against DRACO criteria.</p>
<p>That exact run is not publishable yet. The main blocker is GPT-5.5 long-reasoning behavior on DRACO prompts: it can spend the completion budget on reasoning and return no usable answer. GLM 5.2 is not enabled for the current Z.AI account yet, so the reproducible run uses GLM 5.1 until a direct GLM 5.2 smoke passes.</p>
<h2>What actually ran</h2>
<table class="data-table">
  <thead><tr><th>Run</th><th>Task slice</th><th>Result</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Current 7-model target</td><td>Non-financial DRACO pilot</td><td>No score</td><td>Waiting on GPT-5.5 long-reasoning handling</td></tr>
    <tr><td>Available 6-model fallback</td><td>First completed non-financial DRACO task</td><td>19.85</td><td>Completed, far below target</td></tr>
  </tbody>
</table>
<p>The first fallback panel used Opus 4.8, Kimi K2.7 Code, GLM 5.1, MiniMax M3, Gemini 3 Flash, and Gemini 3.1 Pro. It completed one task before the pilot was stopped for speed and reliability. A score of 19.85 is not close to the target, and we are not presenting it as a win.</p>
<h2>What changed in the harness</h2>
<ul class="plain-list">
  <li>GPT-5.5 eval calls now omit <span class="mono">temperature</span> and use <span class="mono">max_completion_tokens</span>.</li>
  <li>Panel and final synthesis calls stream so long answers do not wait for full completion before parsing.</li>
  <li>Analysis and judge calls stay non-streaming because they require structured JSON reliability.</li>
  <li>The live runner now has explicit six-model and seven-model frontier Fusion configs behind a hard budget.</li>
  <li>The recommended DRACO slice for this experiment is <span class="mono">--task-filter non-financial</span>.</li>
</ul>
<h2>Next gates</h2>
<p>The next clean run needs two fixes before any headline claim: make GPT-5.5 long-reasoning responses produce useful content through the attested gateway, and finish a 10-task non-financial DRACO pilot without task-level hangs. GLM 5.2 can replace GLM 5.1 later when Z.AI enables it for the account.</p>
<p>This is the point of doing the work in the open. If TrustedRouter clears a Mythos/Fable-class target, the result should be reproducible from code, model ids, task filters, budget limits, and artifacts. Until then, the honest result is: not there yet.</p>
""",
    ),
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
