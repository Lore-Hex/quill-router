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
<p><strong>We tried to push TrustedRouter Fusion up to Mythos and Fable-class DRACO performance, and it isn't there.</strong> The target panel right now is seven models: GPT-5.5, Claude Opus 4.8, Kimi K2.7 Code, GLM 5.1, MiniMax M3, Gemini 3 Flash, and Gemini 3.1 Pro. Opus 4.8 synthesizes the final answer and Gemini 3.1 Pro judges it against DRACO criteria.</p>
<p>Can we publish that run? No. One model breaks it. GPT-5.5 on DRACO prompts will spend its whole completion budget on reasoning and hand back nothing usable. So the seven-model panel produces no score at all. And GLM 5.2 isn't enabled on the current Z.AI account, so the reproducible run substitutes GLM 5.1 until a direct GLM 5.2 smoke passes. One model goes silent, the other is a stand-in.</p>
<table class="data-table">
  <thead><tr><th>Run</th><th>Task slice</th><th>Result</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Current 7-model target</td><td>Non-financial DRACO pilot</td><td>No score</td><td>Waiting on GPT-5.5 long-reasoning handling</td></tr>
    <tr><td>Available 6-model fallback</td><td>First completed non-financial DRACO task</td><td>19.85</td><td>Completed, far below target</td></tr>
  </tbody>
</table>
<p>The six-model fallback dropped GPT-5.5 and ran Opus 4.8, Kimi K2.7 Code, GLM 5.1, MiniMax M3, Gemini 3 Flash, and Gemini 3.1 Pro. It finished exactly one non-financial DRACO task before we stopped the pilot for speed and reliability, and it scored 19.85. That is nowhere near the target, and I'm not dressing it up as one.</p>
<p>The harness changes are real, even if the score isn't yet. GPT-5.5 eval calls now drop <span class="mono">temperature</span> and use <span class="mono">max_completion_tokens</span>. Panel and final synthesis calls stream, so a long answer gets parsed as it arrives instead of blocking on full completion. Analysis and judge calls stay non-streaming, because they need reliable structured JSON and streaming fights that. The live runner carries explicit six-model and seven-model frontier Fusion configs, each behind a hard budget. And the slice I'd actually run for this is <span class="mono">--task-filter non-financial</span>.</p>
<p>Two gates stand between here and any headline. First, make GPT-5.5 long-reasoning responses produce useful content through the attested gateway instead of burning the budget on thinking. Second, finish a 10-task non-financial DRACO pilot with no task-level hangs. GLM 5.2 swaps in for GLM 5.1 later, whenever Z.AI flips it on for the account.</p>
<p>That gap is the whole reason to do this in the open. If TrustedRouter ever clears a Mythos or Fable-class target, the number should fall straight out of the code, the model ids, the task filters, the budget limits, and the artifacts, with nothing to take on faith. It hasn't yet. Not there yet.</p>
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
<p>We're reproducing OpenRouter's Fusion DRACO eval in the open. Same class of routing experiment, but with public code, explicit model lists, and cost/quality tradeoffs you can actually measure, instead of a hidden benchmark harness you have to take on faith.</p>
<p>Do we have comparable full-run numbers yet? No. The one full run we did finish used a holistic judge, and that doesn't match OpenRouter's DRACO scoring, so it's out. Showing it next to their numbers would be comparing two different things.</p>
<table class="data-table">
  <thead><tr><th>Run</th><th>OpenRouter score</th><th>TrustedRouter score</th><th>Status</th></tr></thead>
  <tbody>
    <tr><td>Solo Gemini 3 Flash</td><td>43.1</td><td>29.35 on 10-task smoke</td><td>Investigating</td></tr>
    <tr><td>Solo Kimi K2.6</td><td>53.7</td><td>Not enough completed rows</td><td>Investigating</td></tr>
    <tr><td>Solo DeepSeek V4 Pro</td><td>60.3</td><td>Not run with exact scorer yet</td><td>Pending</td></tr>
    <tr><td>Fusion budget panel</td><td>64.7</td><td>Not run with exact scorer yet</td><td>Pending</td></tr>
  </tbody>
</table>
<p>The rules keep us cheap and keep us comparable. We run in <span class="mono">micro-hybrid</span> mode, which means the small public smoke runs first before we spend on any full pass. The judge is <span class="mono">google/gemini-3.1-pro-preview</span>. Scoring is DRACO criterion-level grading, three independent passes, normalized 0-100. Search is Exa with the DRACO and rubric hostnames excluded and result-leakage checks turned on, so the judge can't just look up the answer. And the headline rule: the raw solo baselines have to replicate before we publish a single Fusion number. Fusion looking good means nothing if we can't first reproduce Gemini 3 Flash scoring 43.1 on its own.</p>
<p>The exact scorer and the leakage guard both live in the open-source harness, so none of this is a claim you have to trust. When the raw baselines replicate, those numbers replace this table.</p>
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
<p>Developers reach for the OpenRouter shape because it kills switching cost. One base URL, many models, fallback when a provider dies, one ledger for usage. What it doesn't give you is any way to verify trust, and that's the part that matters once a real prompt is on the wire.</p>
<p>TrustedRouter splits the dashboard and billing surface off from the attested API gateway. The hosted prompt path is built so you can check the running code, the image digest, and the attestation evidence yourself, instead of taking someone's word for it. That's the whole difference: verifiable, not promised.</p>
<p>For a developer the change is small. Keep the OpenAI SDK and point the base URL somewhere new. From there you route to hundreds of models across many providers. Use <span class="mono">trustedrouter/zdr</span> when you need zero-retention providers, and <span class="mono">trustedrouter/e2e</span> for confidential provider routes where they exist. Verify the hosted gateway at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>.</p>
<p>This does not turn every upstream model provider confidential by magic. It can't. The router's job is to be plain about where the guarantee starts, where it ends, and which provider route actually got picked, so you know exactly what you're trusting and what you aren't.</p>
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
<p>Policy is not enough for high-value prompts. A policy tells you what a router promises to do with your prompt. It does not let you check that the promise is being kept. For prompts that actually matter, you want to verify what code is receiving the request and whether that code matches the open source release.</p>
<p>So TrustedRouter makes attestation part of the product, not a footnote. You can pull up the trust page, compare the source commits against the release digests, and decide for yourself whether a route clears your workload's privacy bar before you send anything through it.</p>
<p>The design splits cleanly along who needs what. The control plane handles accounts, keys, billing, docs, and status. The API plane carries prompt traffic through the attested gateway, and nothing else runs there. Provider pages show upstream retention and confidential-compute posture on their own, separate from our claims, because that posture is theirs and not ours to launder. Legal and procurement pages say plainly what is ready now and what still needs a signed agreement.</p>
<p>The payoff is that each person can verify the part they care about, in their own terms. A lawyer reads the DPA and the subprocessor list. An engineer reads the code. An agent checks attestation before it routes sensitive work. Nobody has to take the others' word for it.</p>
""",
    ),
)

BLOG_POSTS_BY_SLUG: dict[str, BlogPost] = {post.slug: post for post in BLOG_POSTS}
