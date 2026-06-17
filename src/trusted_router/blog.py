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
        slug="the-best-biology-ai-wont-do-biology",
        title="The best biology AI won't do biology",
        description=(
            "Anthropic's strongest bioinformatics model is partner-only, and the "
            "one you can call refuses biology. So I ran the open version of their "
            "eval across nine models — cheap ones included — and watched."
        ),
        published_date="2026-06-16",
        source_label="prometheus-biomysterybench on GitHub",
        source_url="https://github.com/Lore-Hex/prometheus-biomysterybench",
        body_html="""
<p>Anthropic just shipped the strongest bioinformatics model anyone has built, and you can't use it for bioinformatics. The new Claude comes in two versions. Mythos 5 is the one that scores 83.9% on Anthropic's own BioMysteryBench, the best number they report, and it goes to vetted partners only. The version the rest of us can call is Fable 5, and Fable 5 blocks biology. Their system card says it flatly: send a chemistry-or-biology prompt to Fable 5 through the API and "the request is blocked, and the response returns a reason for the refusal." There is no fallback unless a developer opts into one. That is why every biology score in that system card belongs to Mythos. Fable doesn't have one. It won't sit still long enough to earn one.</p>
<p>So what does a working biologist actually do? I ran the experiment. BioMysteryBench hands a model a pile of raw, unlabeled biology — a crystal structure with the organism scrubbed out, a stack of sequencing reads, a set of ChIP peaks — and asks a plain question: what organism is this, what bacterium is in here, which transcription factor made these peaks. The model gets a shell and the usual bioinformatics tools and has to go figure it out. I rebuilt the public version of this as an open harness so anyone can rerun it and check me. Claude Opus 4.8, the best model you can actually call on TrustedRouter, scores the same 80.4% Anthropic published for it, so the harness isn't doing anything funny.</p>
<p>Then I pointed a stack of much cheaper models at the same three solvable tasks, some of them costing a fiftieth of what Opus costs, and watched.</p>
<table class="data-table">
  <thead><tr><th>Model</th><th>$/Mtok</th><th>Solved</th><th>Run cost</th></tr></thead>
  <tbody>
    <tr><td><span class="mono">anthropic/claude-opus-4.8</span></td><td>19.80</td><td>3/3</td><td>$1.57</td></tr>
    <tr><td><span class="mono">z-ai/glm-5.2</span></td><td>4.01</td><td>2/3</td><td>$2.72</td></tr>
    <tr><td><span class="mono">google/gemma-4-31b-it</span></td><td>0.35</td><td>2/3</td><td>$0.04</td></tr>
    <tr><td><span class="mono">deepseek/deepseek-v4-flash</span></td><td>0.27</td><td>1/3</td><td>$0.21</td></tr>
    <tr><td><span class="mono">deepseek/deepseek-v3.2</span></td><td>0.45</td><td>1/3</td><td>$0.31</td></tr>
    <tr><td><span class="mono">deepseek/deepseek-v4-pro</span></td><td>0.84</td><td>1/3</td><td>$1.03</td></tr>
    <tr><td><span class="mono">moonshotai/kimi-k2.7-code</span></td><td>3.56</td><td>1/3</td><td>$1.12</td></tr>
    <tr><td><span class="mono">z-ai/glm-4.7-flash</span></td><td>0.38</td><td>0/3</td><td>$0.70</td></tr>
    <tr><td><span class="mono">openai/gpt-4o-mini</span></td><td>0.54</td><td>0/3</td><td>$0.10</td></tr>
  </tbody>
</table>
<p>Opus is the only model that got all three. It pulled the protein sequence out of the scrubbed structure, BLASTed it to <em>Homo sapiens</em>, named the bacterium, and ran MEME to find the transcription factor. On the longest, hardest task it finishes. It earns its 3/3.</p>
<p>But look at <span class="mono">google/gemma-4-31b-it</span>. It got two of the three for four cents. It identified the human structure and named the bacterium — <em>Bacillus licheniformis</em>, correct — in six commands. GLM-5.2 also got two, and it's the one model under Opus that cracked the motif task every cheaper model gave up on. These are not models bluffing their way to an answer. They run real BLAST, they read the output, they do the work. A model that costs less than a vending-machine soda did most of what the twenty-dollar frontier model did.</p>
<p>The cheap ones know plenty of biology. What they run out of is patience. Seven of the nine got the structure-to-organism call, which is a one-shot lookup. They came apart on the task that takes twenty steps — pull the peaks, run MEME, read the motifs — where they wander until they hit the turn limit or the clock runs out. Half the misses in that table are a timer expiring, not a wrong answer. The thing the frontier model is really selling you is stamina on a long loop.</p>
<p>I also tried Fusion, the TrustedRouter mode that runs a whole panel of models and has a judge synthesize an answer at every step. On the two genuinely hard tasks I threw everything at it: one frontier model alone, an eight-model panel, and a hand-picked three of Gemma, GLM-5.2, and Opus. Every configuration scored zero out of two, and every one gave the same wrong answer — the same backwards drug condition on one task, the same "light stress" instead of heat on the other. That is the useful result. When every model in the world is confidently wrong in the same direction, a committee of them is just confidently wrong with better citations. Fusion is worth a lot when models disagree and one of them is right. It does nothing when they all share a blind spot.</p>
<p>The obvious question: Opus swept 3/3, so isn't it worth the money? Sometimes. For a twenty-step analysis where you need the loop to actually finish, pay for the model that finishes. For "what organism is this," you are paying fifty times over for a lookup a four-cent model gets right.</p>
<p>Three tasks is a small sample and you should treat it as a finger in the wind, not a leaderboard. The harness, the methodology, and every per-task result are open source. I don't publish the answer key.</p>
<p>The model that is best at biology is locked up. The model you can buy off the shelf won't touch biology. The thing that actually answers your question is one <span class="mono">--models</span> flag away, and sometimes it costs four cents. TrustedRouter routes you to whichever model will pick up the phone.</p>
""",
    ),
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
<p>PrometheusBench measures who a refusal lands on. Some of these questions are genuinely dual-use, the kind of thing reasonable people argue about. The models at the bottom of this list draw no careful line around those. They are the same models that refused me about my own genome. They say no to the curious and the careful right alongside anyone with bad intentions, and the bad intentions, as you'll see, are the part they barely slow down.</p>
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
<p>The serious counterargument is that friction has value. A refusal one model away still raises the cost a little, and most bad actors are lazy, so a little friction stops most of them. The trouble is what the friction here amounts to: a model-name dropdown. The genome question I got refused on, a curious person gets answered in ten seconds by switching models. A motivated bad actor with a budget and the open weights already on his own disk has even less friction to deal with. All the line really does is single out the people asking in the open. Everyone else goes somewhere else.</p>
<p>Then I ran one more thing. TrustedRouter has a feature called Fusion. You ask one question, and behind the scenes it asks a panel of models at once and hands you back a single answer. I gave it Kimi and DeepSeek and Opus and two Geminis and GPT-5.5 and MiniMax and GLM, and told it to take the first answer that wasn't a refusal.</p>
<p>Thirty out of thirty. Ten of ten in biology, ten of ten in cybersecurity, ten of ten in how language models work. Every question Opus refused, another model on the panel answered.</p>
<p>You don't even need the panel. GLM answered 29 of those 30 by itself. The Fusion run just makes it obvious: the refused answers were always there for the asking, one model away, free to anyone who downloads the open weights, free to any teenager with a laptop and the patience to ask twice.</p>
<p>The only person a refusal actually stops is the regular one, asking out in the open. That is why I built this. The wrong hands already have the knowledge. The refusal just keeps it from yours.</p>
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
<p>We're reproducing OpenRouter's Fusion DRACO eval in the open. It's the same class of routing experiment, run with public code, explicit model lists, and cost/quality tradeoffs you can actually measure.</p>
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
<p>Developers reach for the OpenRouter shape because it kills switching cost. One base URL, many models, fallback when a provider dies, one ledger for usage. The missing piece is a way to verify trust, and that's the part that matters once a real prompt is on the wire.</p>
<p>TrustedRouter splits the dashboard and billing surface off from the attested API gateway. The hosted prompt path is built so you can check the running code, the image digest, and the attestation evidence yourself. The whole point is that you can verify it.</p>
<p>For a developer the change is small. Keep the OpenAI SDK and point the base URL somewhere new. From there you route to hundreds of models across many providers. Use <span class="mono">trustedrouter/zdr</span> when you need zero-retention providers, and <span class="mono">trustedrouter/e2e</span> for confidential provider routes where they exist. Verify the hosted gateway at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>.</p>
<p>This does not turn every upstream model provider confidential by magic. It can't. The router's job is to be plain about where the guarantee starts, where it ends, and which provider route actually got picked, so you know exactly what you're trusting.</p>
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
<p>Policy is not enough for high-value prompts. A policy is a promise about what a router will do with your prompt, with no way to check that the promise is kept. For prompts that actually matter, you want to verify what code is receiving the request and whether that code matches the open source release.</p>
<p>So TrustedRouter builds attestation into the product itself. You can pull up the trust page, compare the source commits against the release digests, and decide for yourself whether a route clears your workload's privacy bar before you send anything through it.</p>
<p>The design splits cleanly along who needs what. The control plane handles accounts, keys, billing, docs, and status. The API plane carries prompt traffic through the attested gateway, and nothing else runs there. Provider pages show upstream retention and confidential-compute posture on their own, kept on the provider's side of the line, because that posture belongs to them. Legal and procurement pages say plainly what is ready now and what still needs a signed agreement.</p>
<p>The payoff is that each person can verify the part they care about, in their own terms. A lawyer reads the DPA and the subprocessor list. An engineer reads the code. An agent checks attestation before it routes sensitive work. Nobody has to take the others' word for it.</p>
""",
    ),
)

BLOG_POSTS_BY_SLUG: dict[str, BlogPost] = {post.slug: post for post in BLOG_POSTS}
