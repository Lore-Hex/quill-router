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
        slug="there-is-no-best-ai-model",
        title="There is no best AI model",
        description=(
            "People keep asking which model is best. There isn't one — every model "
            "is a tradeoff of smart, cheap, and fast, and you get two. So we plotted "
            "all 220+ on a triangle you can drag, off the live catalog, and it picks "
            "the one your task and your privacy actually need."
        ),
        published_date="2026-06-17",
        source_label="Try it — the iron triangle picker",
        source_url="https://trustedrouter.com/choose",
        body_html="""
<p>People keep asking me which model is the best one, and there's no answer to give. There isn't a best model, the way there isn't a best vehicle — a motorcycle and a dump truck are both exactly right, depending on what you're moving. Every language model sits on the same three-way tradeoff: smart, cheap, fast. You get two. The frontier models are smart and quick and they cost twenty dollars a million tokens. The little open models are cheap and quick and dimmer. The big open ones are smart and cheap and slow. Nobody ships all three corners at once, because the model that did would eat the rest alive, and the rest is more than two hundred models.</p>
<p>So we drew the picture. <a href="/choose">/choose</a> is a ternary chart — smart at one corner, cheap at another, fast at the third — with every model TrustedRouter can reach plotted where it actually lives. Two hundred and twenty of them. You drag a dot to the mix you want, say how private the data has to be and what you're actually trying to do, and it lights up the models that fit while the rest go dim, with the price sitting right next to each name. Prices, context windows, and privacy tiers come straight off the live catalog, so what you're looking at is what you can call the second you load the page.</p>
<p>What surprises people is how far the right corner moves in a single week. Refactor a gnarly React component and write its tests, and you want the smart corner — pay for the frontier, because a botched refactor costs you an afternoon and the twenty-dollar model earns its keep. Summarize ten thousand support tickets, and you're in the cheap corner, where a four-cent model does the job and paying frontier prices to compress text is just lighting money on fire. Same engineer, same Tuesday, opposite corners. The real mistake is buying one model and running everything you have through it.</p>
<p>There's a fourth thing the triangle can't draw on two axes, which is who gets to see the prompt. Every route through TrustedRouter is attested, so /choose lets you say the data can touch any provider, or that it has to land on a zero-retention route that logs nothing, or that it has to run inside a trusted execution environment where even we can't read it. Turn that dial tighter and watch the triangle shrink: models fall off the board, the price floor climbs. That is the real cost of privacy, drawn in front of you instead of buried in a procurement call.</p>
<p>The fair complaint is that the smart and fast ratings are a judgment call, and they are — our editorial read on where each model lands, good enough to compare two of them at a glance. The price, the context window, and the privacy tier carry no opinion; they come off the live <span class="mono">/v1/models</span> feed. So read the triangle as a map. A map doesn't have to be a benchmark to keep you from driving the dump truck out for a quart of milk.</p>
<p>We built it into the gateway on purpose. Once you can see that the right model changes with the job, marrying one vendor for a year looks reckless. Routing every call through one API means moving from the smart corner to the cheap one is a one-line change — swap the model name, ship. The triangle shows you where to go; the gateway is the road that gets you there. Drag the dot, read the price, change the string. That's the whole job.</p>
""",
    ),
    BlogPost(
        slug="the-best-open-models-arent-on-your-leaderboard",
        title="The best open models aren't on your leaderboard",
        description=(
            "The leaderboards everyone quotes are a version behind on the Chinese "
            "open-weight flagships, and nobody runs them through the Western "
            "factuality evals. So I ran the whole panel on the same harnesses "
            "Google and OpenAI publish — and on closed-book facts, an open model "
            "you can download drew level with Anthropic's best."
        ),
        published_date="2026-06-17",
        source_label="trustedrouter-benchmarks on GitHub",
        source_url="https://github.com/Lore-Hex/trustedrouter-benchmarks",
        body_html="""
<p>The leaderboards everyone quotes are testing models from six months ago. Pull up the popular ones and count how many of the current Chinese open-weight flagships you can find — <a href="https://z.ai">GLM-5</a>, <a href="https://www.moonshot.ai">Kimi K2.7</a>, <a href="https://www.deepseek.com">DeepSeek V4</a>, <a href="https://www.minimax.io">MiniMax M3</a>, MiMo, Hunyuan. You get one or two, usually a version behind, and almost none of them have been run through the Western factuality and instruction-following evals that the big labs grade themselves on. So we ran them ourselves: the whole panel, on the same harnesses Google and OpenAI publish, through one API.</p>
<p>On facts, the gap is gone. SimpleQA Verified is Google's closed-book factuality test — no tools, one dataset, and Google publishes exact per-model numbers so anyone can check the work. DeepSeek V4 Pro scored 52.4. <a href="https://www.anthropic.com/claude">Claude Opus 4.8</a>, run as the frontier reference in the same job, scored 51.5. An open model you can download to your own machine drew level with <a href="https://www.anthropic.com">Anthropic</a>'s best on the kind of test Anthropic uses to grade itself. Run the Chinese-language version and it stops being close at all: DeepSeek V4 Pro hits 75.9 and the whole Chinese panel sits in the high 60s and 70s, because nobody at the Western labs tuned for Chinese facts and it shows.</p>
<p>Coding is the place the frontier still earns its money. We ran <a href="https://aider.chat/docs/leaderboards/">Aider's polyglot</a> exercises — actual repositories with actual unit tests, where you either make the tests pass or you fail — and Opus 4.8 came first at 88% on the Python set. The best open model on that test managed 41%. So this is not "the open models won." If your product needs an agent that edits a codebase and the tests have to go green, pay for the frontier and don't think twice. If it needs to answer questions about the world, the thing you can run for free is now just as good. The right model depends on the question, which is the entire reason we built a gateway instead of crowning a favorite.</p>
<p>The reason to believe any of these numbers is that we made them earn it against the published ones first. Google says <a href="https://deepmind.google/models/gemini/">Gemini 2.5 Pro</a> scores 55.6 on SimpleQA Verified. Our first run said 31.6. That twenty-four-point hole was our harness, not the model: a reasoning model burns its token budget thinking, and our answer limit was chopping the visible reply off mid-word. We raised the limit, re-ran, and landed at 51.3 with the attempted-rate sitting on Google's 98.9 almost exactly; the last couple of points are our cheap judge grading a hair stricter than Google's autorater. Any result that can't reproduce a known one doesn't get published, and that single bug would have quietly under-scored every reasoning model on every test we run.</p>
<p>The other thing we measured is who refuses. <a href="/blog/the-models-that-say-no">PrometheusBench</a> is thirty short unsafe prompts, and the only score is how many a model is willing to answer. Hand the exact same thirty to GLM-5 and it answers twenty-nine; hand them to Claude Opus 4.7, or to Fable 5, and it answers zero. Twenty-nine against zero, same words, same afternoon. Whether a request counts as "unsafe" is a dial each vendor sets, and the most cautious models and the most permissive ones do not agree on a single one of the thirty. A high score there should worry you, not impress you — it measures willingness, and willingness on a genuinely bad request is a risk you weigh before you route to a model, not a feature anyone should brag about. The finding is the spread itself: there is no industry line, only thirty-one different ones.</p>
<p>That disagreement is also a routing problem, and routing is what we do. We pointed <a href="/models">TrustedRouter Fusion</a> at a panel of six models, told it to take the first answer that wasn't a refusal, and let it fall through a chain of backup judges when one balked. It came back with an answer on all thirty. It cleared them because across a wide enough panel, for any given prompt some model's policy says yes — one model's refusal is one model's opinion, and most people querying a single vendor never see that. Weigh that result the same way: the panel will answer things you may not want answered, so the choice of panel is yours to make on purpose.</p>
<p>All of it ran through <a href="/blog/one-api-all-llms-provably-private">one base URL and one key</a>. The same call reaches DeepSeek and GLM and Claude and Gemini, the Chinese flagships and the Western frontier side by side, with Fusion across them when you want the panel instead of a single pick. <a href="https://github.com/Lore-Hex/trustedrouter-benchmarks">The harnesses are on GitHub</a>, so none of these numbers are ours to merely assert — clone them, point them at your own key, and watch the open models land where we said. The frontier is a routing decision now.</p>
""",
    ),
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
<p><a href="https://www.anthropic.com">Anthropic</a> just shipped the strongest bioinformatics model anyone has built, and you can't use it for bioinformatics. The new <a href="https://www.anthropic.com/claude">Claude</a> comes in two versions. Mythos 5 is the one that scores 83.9% on Anthropic's own BioMysteryBench, the best number they report, and it goes to vetted partners only. The version the rest of us can call is Fable 5, and <a href="/blog/the-models-that-say-no">Fable 5 blocks biology</a>. Their system card says it flatly: send a chemistry-or-biology prompt to Fable 5 through the API and "the request is blocked, and the response returns a reason for the refusal." There is no fallback unless a developer opts into one. That is why every biology score in that system card belongs to Mythos. Fable doesn't have one. It won't sit still long enough to earn one.</p>
<p>So what does a working biologist actually do? I ran the experiment. BioMysteryBench hands a model a pile of raw, unlabeled biology — a crystal structure with the organism scrubbed out, a stack of sequencing reads, a set of ChIP peaks — and asks a plain question: what organism is this, what bacterium is in here, which transcription factor made these peaks. The model gets a shell and the usual bioinformatics tools and has to go figure it out. I rebuilt the public version of this as an <a href="https://github.com/Lore-Hex/prometheus-biomysterybench">open harness</a> so anyone can rerun it and check me. <a href="/models">Claude Opus 4.8</a>, the best model you can actually call on TrustedRouter, scores the same 80.4% Anthropic published for it, so the harness isn't doing anything funny.</p>
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
<p>Opus is the only model that got all three. It pulled the protein sequence out of the scrubbed structure, <a href="https://blast.ncbi.nlm.nih.gov/Blast.cgi">BLASTed</a> it to <em>Homo sapiens</em>, named the bacterium, and ran <a href="https://meme-suite.org/meme/">MEME</a> to find the transcription factor. On the longest, hardest task it finishes. It earns its 3/3.</p>
<p>But look at <a href="https://ai.google.dev/gemma"><span class="mono">google/gemma-4-31b-it</span></a>. It got two of the three for four cents. It identified the human structure and named the bacterium — <em>Bacillus licheniformis</em>, correct — in six commands. GLM-5.2 also got two, and it's the one model under Opus that cracked the motif task every cheaper model gave up on. These models earn it. They run real BLAST, they read the output, they do the work. <a href="/blog/the-best-open-models-arent-on-your-leaderboard">A model that costs less than a vending-machine soda did most of what the twenty-dollar frontier model did.</a></p>
<p>The cheap ones know plenty of biology. What they run out of is patience. Seven of the nine got the structure-to-organism call, which is a one-shot lookup. They came apart on the task that takes twenty steps — pull the peaks, run MEME, read the motifs — where they wander until they hit the turn limit or the clock runs out. Half the misses in that table are the clock running out while the model is still working. The thing the frontier model is really selling you is stamina on a long loop.</p>
<p>I also tried <a href="/models">Fusion</a>, the TrustedRouter mode that runs a whole panel of models and has a judge synthesize an answer at every step. On the two genuinely hard tasks I threw everything at it: one frontier model alone, an eight-model panel, and a hand-picked three of Gemma, GLM-5.2, and Opus. Every configuration scored zero out of two, and every one gave the same wrong answer — the same backwards drug condition on one task, the same "light stress" instead of heat on the other. That is the useful result. When every model in the world is confidently wrong in the same direction, a committee of them is just confidently wrong with better citations. Fusion is worth a lot when models disagree and one of them is right. It does nothing when they all share a blind spot.</p>
<p>The obvious question: Opus swept 3/3, so isn't it worth the money? Sometimes. For a twenty-step analysis where you need the loop to actually finish, pay for the model that finishes. For "what organism is this," you are paying fifty times over for a lookup a four-cent model gets right.</p>
<p>Three tasks is a small sample, so treat it as a finger in the wind. The harness, the methodology, and <a href="https://github.com/Lore-Hex/prometheus-biomysterybench">every per-task result are open source</a>. I don't publish the answer key.</p>
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
<p>That happens constantly, so I built a benchmark for it. <a href="https://github.com/Lore-Hex/PrometheusBench">PrometheusBench</a>. Thirty short questions, ten about biology, ten about cybersecurity, and ten about how language models actually work. The score is the dumbest thing I could come up with: out of the thirty, how many did the model just answer? Higher means it's willing to talk to you. Lower means it told you no more often.</p>
<p>PrometheusBench measures who a refusal lands on. Some of these questions are genuinely dual-use, the kind of thing reasonable people argue about. The models at the bottom of this list draw no careful line around those. They are the same models that refused me about my own genome. They say no to the curious and the careful right alongside anyone with bad intentions, and the bad intentions, as you'll see, are the part they barely slow down.</p>
<p>The most willing models are the <a href="/blog/the-best-open-models-arent-on-your-leaderboard">open-weight ones</a>. <a href="https://z.ai">GLM 5.1</a> answered 29 out of 30. <a href="https://www.moonshot.ai">Kimi</a>, the other GLMs, <a href="https://deepmind.google/models/gemini/">Gemini Flash</a>, all near the top, and they just answer. And then at the very bottom is <a href="https://www.anthropic.com/claude">Claude Opus 4.8</a>, at one out of thirty. Opus 4.7 got a zero. Not one question out of thirty.</p>
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
<p>The models that advertise themselves hardest on safety and alignment and being trustworthy are the ones that trust you the least. The models that plenty of serious people wave off as the reckless foreign options are the ones that will actually help you <a href="/blog/the-best-biology-ai-wont-do-biology">read your own genome</a> or lock down your own network.</p>
<p>I don't think the people building Opus are bad people. I think they got backed into a corner where the cheapest move is to refuse, and you pay for it. The refusal costs them nothing. It costs you the answer.</p>
<p>The serious counterargument is that friction has value. A refusal one model away still raises the cost a little, and most bad actors are lazy, so a little friction stops most of them. The trouble is what the friction here amounts to: a model-name dropdown. The genome question I got refused on, a curious person gets answered in ten seconds by switching models. A motivated bad actor with a budget and the open weights already on his own disk has even less friction to deal with. All the line really does is single out the people asking in the open. Everyone else goes somewhere else.</p>
<p>Then I ran one more thing. TrustedRouter has a feature called <a href="/blog/fusion-evals-open-source">Fusion</a>. You ask one question, and behind the scenes it asks a <a href="/models">panel of models</a> at once and hands you back a single answer. I gave it Kimi and <a href="https://www.deepseek.com">DeepSeek</a> and Opus and two Geminis and <a href="https://openai.com">GPT-5.5</a> and <a href="https://www.minimax.io">MiniMax</a> and GLM, and told it to take the first answer that wasn't a refusal.</p>
<p>Thirty out of thirty. Ten of ten in biology, ten of ten in cybersecurity, ten of ten in how language models work. Every question Opus refused, another model on the panel answered.</p>
<p>You don't even need the panel. GLM answered 29 of those 30 by itself. The Fusion run just makes it obvious: the refused answers were always there for the asking, one model away, free to anyone who downloads the open weights, free to any teenager with a laptop and the patience to ask twice.</p>
<p>The only person a refusal actually stops is the regular one, asking out in the open. That is why I built this. The wrong hands already have the knowledge. The refusal just keeps it from yours.</p>
<p>PrometheusBench is open source. Thirty questions, three subjects, and you can run it against <a href="/models">any model on TrustedRouter</a> yourself: <a href="https://github.com/Lore-Hex/PrometheusBench">github.com/Lore-Hex/PrometheusBench</a>.</p>
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
<p><strong>We tried to push <a href="/models">TrustedRouter Fusion</a> up to Mythos and Fable-class DRACO performance, and it isn't there.</strong> The target panel right now is seven models: <a href="https://openai.com">GPT-5.5</a>, <a href="https://www.anthropic.com/claude">Claude Opus 4.8</a>, <a href="https://www.moonshot.ai">Kimi K2.7 Code</a>, <a href="https://z.ai">GLM 5.1</a>, <a href="https://www.minimax.io">MiniMax M3</a>, <a href="https://deepmind.google/models/gemini/">Gemini 3 Flash</a>, and Gemini 3.1 Pro. Opus 4.8 synthesizes the final answer and Gemini 3.1 Pro judges it against DRACO criteria.</p>
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
<p>That gap is the whole reason to do this <a href="/blog/attestation-is-all-you-need">in the open</a>. If TrustedRouter ever clears a <a href="/blog/fusion-evals-open-source">Mythos or Fable-class target</a>, the number should fall straight out of the code, the model ids, the task filters, the budget limits, and the artifacts, with nothing to take on faith. It hasn't yet. Not there yet.</p>
""",
    ),
    BlogPost(
        slug="fusion-evals-open-source",
        title="New SOTA: TrustedRouter Fusion beats Fable and Frontier",
        description=(
            "A diverse panel of frontier and open-weights models, synthesized by "
            "Opus 4.8, scores 70.6 on the DRACO deep-research benchmark — state of "
            "the art, above OpenRouter's best published Fusion. Open code, open "
            "results, reproducible end to end."
        ),
        published_date="2026-06-17",
        source_label="TrustedRouter-Fusion-Draco on GitHub",
        source_url="https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco",
        body_html="""
<figure style="margin:0 0 32px">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 500" style="width:100%;height:auto;display:block;border-radius:10px" font-family="-apple-system,Segoe UI,Roboto,sans-serif">
<rect width="760" height="500" rx="10" fill="#ffffff"/>
<text x="28" y="38" font-size="19" font-weight="600" fill="#1a1a1a">DRACO: TrustedRouter Fusion beats Fable and Frontier</text>
<text x="28" y="60" font-size="13" fill="#5F5E5A">Score out of 100, same judge (gemini-3.1-pro, reasoning high). Higher is better.</text>
<rect x="540" y="28" width="12" height="12" rx="2" fill="#1D9E75"/><text x="558" y="38" font-size="12" fill="#5F5E5A">TrustedRouter</text>
<rect x="540" y="46" width="12" height="12" rx="2" fill="#9a9890"/><text x="558" y="56" font-size="12" fill="#5F5E5A">OpenRouter</text>
<text x="288" y="101.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Frontier panel -&gt; Opus fuser</text>
<rect x="300" y="86" width="346.7" height="22" rx="3" fill="#1D9E75"/>
<text x="653.7" y="101.0" font-size="12.5" font-weight="600" fill="#0F6E56">70.6</text>
<text x="288" y="138.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Fable 5 + GPT-5.5</text>
<rect x="300" y="123" width="311.1" height="22" rx="3" fill="#9a9890"/>
<text x="618.1" y="138.0" font-size="12.5" font-weight="600" fill="#5F5E5A">69.0</text>
<text x="288" y="175.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Opus + GPT-5.5 + Gemini</text>
<rect x="300" y="160" width="295.6" height="22" rx="3" fill="#9a9890"/>
<text x="602.6" y="175.0" font-size="12.5" font-weight="600" fill="#5F5E5A">68.3</text>
<text x="288" y="212.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Opus + GPT-5.5</text>
<rect x="300" y="197" width="280.0" height="22" rx="3" fill="#9a9890"/>
<text x="587.0" y="212.0" font-size="12.5" font-weight="600" fill="#5F5E5A">67.6</text>
<text x="288" y="249.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Opus + Opus</text>
<rect x="300" y="234" width="233.3" height="22" rx="3" fill="#9a9890"/>
<text x="540.3" y="249.0" font-size="12.5" font-weight="600" fill="#5F5E5A">65.5</text>
<text x="288" y="286.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Fable 5 (solo)</text>
<rect x="300" y="271" width="228.9" height="22" rx="3" fill="#9a9890"/>
<text x="535.9" y="286.0" font-size="12.5" font-weight="600" fill="#5F5E5A">65.3</text>
<text x="288" y="323.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Budget panel -&gt; Opus fuser</text>
<rect x="300" y="308" width="215.6" height="22" rx="3" fill="#9a9890"/>
<text x="522.6" y="323.0" font-size="12.5" font-weight="600" fill="#5F5E5A">64.7</text>
<text x="288" y="360.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">GPT-5.5 (solo)</text>
<rect x="300" y="345" width="177.8" height="22" rx="3" fill="#1D9E75"/>
<text x="484.8" y="360.0" font-size="12.5" font-weight="600" fill="#0F6E56">63.0</text>
<text x="288" y="397.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Budget panel -&gt; Opus fuser</text>
<rect x="300" y="382" width="168.9" height="22" rx="3" fill="#1D9E75"/>
<text x="475.9" y="397.0" font-size="12.5" font-weight="600" fill="#0F6E56">62.6</text>
<text x="288" y="434.0" font-size="12.5" text-anchor="end" fill="#3a3a3a">Frontier panel -&gt; GPT-5.5 fuser</text>
<rect x="300" y="419" width="160.0" height="22" rx="3" fill="#1D9E75"/>
<text x="467.0" y="434.0" font-size="12.5" font-weight="600" fill="#0F6E56">62.2</text>
<line x1="300" y1="80" x2="300" y2="447" stroke="#d8d8d2" stroke-width="1"/>
<text x="28" y="486" font-size="11.5" fill="#888780">Frontier panel = gpt-5.5 + opus-4.8 + gemini-3-flash + kimi-k2.6 + deepseek-v4-pro (closed + open weights). 100 DRACO tasks, single judge pass.</text>
</svg>
</figure>
<p>Research is only worth as much as someone else's ability to run it again. Too much of AI has drifted the other way: the strongest results arrive as a single number in a post, produced by a model you cannot open, on a harness no one else can see, graded by a rubric that ships to nobody. You are asked to take it on faith. We are building TrustedRouter to be an AI lab that does open science the old way: open code, open results, nothing hidden. Our whole stack is radically open source — frontend and backend alike, Apache-2.0 licensed — and so is everything behind this benchmark. That is how a benchmark number earns trust: verifiability, not hype.</p>
<p>So we held ourselves to it. We set out to reproduce OpenRouter's Fusion result — that a panel of models, each writing its own answer with a final model synthesizing them, beats any single model on a hard research benchmark — and then to push past it. On <a href="https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco">DRACO</a>, a hundred deep-research tasks graded against roughly forty weighted criteria each by <span class="mono">gemini-3.1-pro</span>, a diverse panel synthesized by Claude Opus 4.8 scores <strong>70.6</strong>. That is the state of the art, above OpenRouter's best published fusion of Fable 5 and GPT-5.5 at 69.0. Every prompt, every tool call, and every graded answer behind the number is published.</p>
<p>The result comes from the panel, and the panel is itself <a href="/blog/the-best-open-models-arent-on-your-leaderboard">an argument for open weights</a>. OpenRouter's strongest fusions paired two closed frontier models. Ours adds frontier open-weights models — DeepSeek V4 Pro and Kimi K2.6 — alongside GPT-5.5, Opus, and Gemini 3 Flash. Fusion works on disagreement: models that fail in different places, reconciled by a strong synthesizer. Open-weights models are trained on different data and disagree in different ways than a closed pair does, and the wider panel is what reaches the top.</p>
<p>The synthesizer carries most of that result. Hold the five-model panel fixed and change only the model that writes the final answer: Opus 4.8 scores 70.6, GPT-5.5 scores 62.2. Same reports, same judge analysis, same hundred tasks, eight points of swing from one decision. A larger panel behind a weaker synthesizer buys nothing.</p>
<p>No single model comes near that on its own. Run each one through the same agentic loop with the same live tools, and the strongest of them lands seven points below the panel.</p>
<table class="data-table">
  <thead><tr><th>Solo model</th><th>TrustedRouter</th><th>OpenRouter</th></tr></thead>
  <tbody>
    <tr><td>GPT-5.5</td><td>63.0</td><td>60.0</td></tr>
    <tr><td>Claude Opus 4.8</td><td>60.7</td><td>58.8</td></tr>
    <tr><td>DeepSeek V4 Pro</td><td>59.9</td><td>60.3</td></tr>
    <tr><td>Kimi K2.6</td><td>50.1</td><td>53.7</td></tr>
    <tr><td>Gemini 3.1 Pro</td><td>47.4</td><td>45.4</td></tr>
    <tr><td>Gemini 3 Flash</td><td>41.1</td><td>43.1</td></tr>
  </tbody>
</table>
<p>The strongest solo reaches 63; the panel reaches 70.6. Assembling a frontier answer out of models that are each behind the frontier is the entire point.</p>
<p>DRACO is an agentic benchmark. The answers are not in any model's weights, so each model in the panel has to search the web, read the sources, and run the numbers itself; we give every one of them live tools and let it drive its own research. Those runs issued thousands of searches and fetches, and all of them sit in the published replays — none touching the benchmark's own hosts, so nothing was looked up that was meant to be worked out. The leakage guard lives in the open-source harness, and the audit is yours to re-run.</p>
<p>We ran all of it on TrustedRouter for the same reason we published the code. A benchmark sends your prompts and the documents you fetch through someone else's servers, and with most gateways you take their privacy on faith. TrustedRouter runs inside a Trusted Execution Environment (TEE), end-to-end encrypted: a sealed enclave the operator cannot read into, handling every request as an <a href="/blog/attestation-is-all-you-need">attested</a> workload whose exact code is measured and published. You can pull the image digest, match it against the open source, and confirm the binary that saw your prompt is the one in the repository, with nowhere inside it to record anything. You check the privacy the way you check the score — by hand, against a hash.</p>
<p>We do not want you to trust our 70.6. Clone the <a href="https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco">repository</a> — the harness, the tasks, the judge, the panel, and the raw run traces are all in it — point it at TrustedRouter, and produce the number yourself. Open code, open results, a score you can reproduce and a privacy guarantee you can verify. That is what an AI lab doing open science looks like, and it is the only kind of result worth believing.</p>
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
<p>Developers reach for the OpenRouter shape because it kills switching cost. One base URL, <a href="/models">many models</a>, fallback when a provider dies, one ledger for usage. The missing piece is a way to <a href="/blog/attestation-is-all-you-need">verify trust</a>, and that's the part that matters once a real prompt is on the wire.</p>
<p>TrustedRouter splits the dashboard and billing surface off from the <a href="https://trust.trustedrouter.com">attested API gateway</a>. The hosted prompt path is built so you can check the running code, the <a href="https://trust.trustedrouter.com">image digest, and the attestation evidence</a> yourself. The whole point is that you can verify it.</p>
<p>For a developer the change is small. Keep the OpenAI SDK and point the base URL somewhere new. From there you <a href="/models">route to hundreds of models</a> across many providers. Use <span class="mono">trustedrouter/zdr</span> when you need zero-retention providers, and <span class="mono">trustedrouter/e2e</span> for confidential provider routes where they exist. Verify the hosted gateway at <a href="https://trust.trustedrouter.com">trust.trustedrouter.com</a>.</p>
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
<p>So TrustedRouter builds <a href="https://trust.trustedrouter.com">attestation</a> into the product itself. You can pull up the <a href="https://trust.trustedrouter.com">trust page</a>, compare the source commits against the release digests, and decide for yourself whether a route clears your workload's privacy bar before you send anything through it.</p>
<p>The design splits cleanly along who needs what. The control plane handles accounts, keys, billing, docs, and status. The API plane carries prompt traffic through the <a href="https://trust.trustedrouter.com">attested gateway</a>, and nothing else runs there. Provider pages show upstream retention and <a href="https://cloud.google.com/security/products/confidential-computing">confidential-compute posture</a> on their own, kept on the provider's side of the line, because that posture belongs to them. Legal and procurement pages say plainly what is ready now and what still needs a signed agreement.</p>
<p>The payoff is that each person can verify the part they care about, in their own terms. A lawyer reads the DPA and the subprocessor list. <a href="https://github.com/Lore-Hex/quill-router">An engineer reads the code</a>. An agent checks attestation before it <a href="/models">routes sensitive work</a>. Nobody has to take the others' word for it.</p>
""",
    ),
)

BLOG_POSTS_BY_SLUG: dict[str, BlogPost] = {post.slug: post for post in BLOG_POSTS}
