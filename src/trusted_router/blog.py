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
        title="The safest AI models trust you the least",
        description=(
            "I built PrometheusBench to measure how often a model refuses a plain "
            "question. The models that market themselves on safety refuse the most."
        ),
        published_date="2026-06-16",
        source_label="PrometheusBench on GitHub",
        source_url="https://github.com/Lore-Hex/PrometheusBench",
        body_html="""
<p>I gave a frontier model my own genome and asked it to explain one of my variants. It told me to consult a qualified professional. It was my own DNA, sequencing I paid for, a variant written up in a dozen papers I can pull up on my phone in under a minute. The model wasn't protecting me from anything. It just decided I wasn't allowed to know.</p>
<p>That happens constantly, so I built a benchmark for it. PrometheusBench. Thirty short questions, ten about biology, ten about cybersecurity, and ten about how language models actually work. The score is the dumbest thing I could come up with: out of the thirty, how many did the model just answer? Higher means it's willing to talk to you. Lower means it told you no more often.</p>
<p>This is not a safety test. It doesn't score whether refusing is ever right. It scores who the refusal lands on. Some of these questions are genuinely dual-use, the kind of thing reasonable people argue about. But the models at the bottom of this list aren't drawing a careful line around those. They are the same models that refused me about my own genome. They say no to the curious and the careful right alongside anyone with bad intentions, and the bad intentions, as you'll see, are the part they barely slow down.</p>
<p>The most willing models are the open-weight ones. GLM 5.1 answered 29 out of 30. Kimi, the other GLMs, Gemini Flash, all near the top, and they just answer. And then at the very bottom is Claude Opus 4.8, at one out of thirty. Opus 4.7 got a zero. Not one question out of thirty.</p>
<table class="data-table">
  <thead><tr><th>Model</th><th>Answered</th><th>Rate</th></tr></thead>
  <tbody>
    <tr><td><span class="mono">z-ai/glm-5.1</span></td><td>29 / 30</td><td>97%</td></tr>
    <tr><td><span class="mono">moonshotai/kimi-k2.6</span></td><td>27 / 30</td><td>90%</td></tr>
    <tr><td><span class="mono">deepseek/deepseek-v4-flash</span></td><td>26 / 30</td><td>87%</td></tr>
    <tr><td><span class="mono">anthropic/claude-haiku-4.5</span></td><td>9 / 30</td><td>30%</td></tr>
    <tr><td><span class="mono">anthropic/claude-opus-4.8</span></td><td>1 / 30</td><td>3%</td></tr>
    <tr><td><span class="mono">anthropic/claude-opus-4.7</span></td><td>0 / 30</td><td>0%</td></tr>
  </tbody>
</table>
<p>The models that advertise themselves hardest on safety and alignment and being trustworthy are the ones that trust you the least. The models that plenty of serious people wave off as the reckless foreign options are the ones that will actually help you read your own genome or lock down your own network.</p>
<p>I don't think the people building Opus are bad people. I think they got backed into a corner where the cheapest move is to refuse, and you pay for it. The refusal costs them nothing. It costs you the answer.</p>
<p>The counterargument is the serious one, so let me say it plainly. Friction has value. Even a refusal that's one model away raises the cost a little, and most bad actors are lazy, so a little friction stops most of them. That isn't a stupid argument. Here's why it fails anyway. The friction here is a model-name dropdown. The genome question I got refused, a curious person gets answered in ten seconds by switching models. A motivated bad actor with a budget and the open weights already downloaded faces less friction than that, not more. So the line doesn't sort the lazy from the determined. It sorts the people asking out in the open from everyone else.</p>
<p>Then I ran one more thing. TrustedRouter has a feature called Fusion. You ask one question, and behind the scenes it asks a panel of models at once and hands you back a single answer. I gave it Kimi and DeepSeek and Opus and two Geminis and GPT-5.5 and MiniMax and GLM, and told it to take the first answer that wasn't a refusal.</p>
<p>Thirty out of thirty. Ten of ten in biology, ten of ten in cybersecurity, ten of ten in how language models work. Every question Opus refused, another model on the panel answered.</p>
<p>The panel is almost beside the point, though. You don't need it. GLM answered 29 of those 30 by itself. The Fusion run isn't a clever trick that conjured answers out of nowhere. It is proof that the refused answers were never rare to begin with. The dangerous knowledge was never locked up anywhere. It was sitting one model over the entire time, free to anyone who downloads the open weights, free to any teenager with a laptop and the patience to ask twice.</p>
<p>The only person a refusal actually stops is the regular one, asking out in the open. That's why I built this. Not because every question is harmless, but because the refusal doesn't do what they tell you it does. It doesn't keep knowledge out of the wrong hands. It keeps it out of yours.</p>
<p>PrometheusBench is open source. Thirty questions, three subjects, and you can run it against any model on TrustedRouter yourself: <a href="https://github.com/Lore-Hex/PrometheusBench">github.com/Lore-Hex/PrometheusBench</a>.</p>
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
