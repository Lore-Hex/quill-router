---
title: "The best open models aren't on your leaderboard"
date: "2026-06-17"
draft: false
tags:
  - TrustedRouter
  - benchmarks
  - open-weight-models
  - evals
  - Fusion
---

The leaderboards everyone quotes are testing models from six months ago. Pull up the popular ones and count how many of the current Chinese open-weight flagships you can find — GLM-5, Kimi K2.7, DeepSeek V4, MiniMax M3, MiMo, Hunyuan. You get one or two, usually a version behind, and almost none of them have been run through the Western factuality and instruction-following evals that the big labs grade themselves on. So we ran them ourselves: the whole panel, on the same harnesses Google and OpenAI publish, through one API.

On facts, the gap is gone. SimpleQA Verified is Google's closed-book factuality test — no tools, one dataset, and Google publishes exact per-model numbers so anyone can check the work. DeepSeek V4 Pro scored 52.4. Claude Opus 4.8, run as the frontier reference in the same job, scored 51.5. An open model you can download to your own machine drew level with Anthropic's best on the kind of test Anthropic uses to grade itself. Run the Chinese-language version and it stops being close at all: DeepSeek V4 Pro hits 75.9 and the whole Chinese panel sits in the high 60s and 70s, because nobody at the Western labs tuned for Chinese facts and it shows.

Coding is the place the frontier still earns its money. We ran Aider's polyglot exercises — actual repositories with actual unit tests, where you either make the tests pass or you fail — and Opus 4.8 came first at 88% on the Python set. The best open model on that test managed 41%. So this is not "the open models won." If your product needs an agent that edits a codebase and the tests have to go green, pay for the frontier and don't think twice. If it needs to answer questions about the world, the thing you can run for free is now just as good. The right model depends on the question, which is the entire reason we built a gateway instead of crowning a favorite.

The reason to believe any of these numbers is that we made them earn it against the published ones first. Google says Gemini 2.5 Pro scores 55.6 on SimpleQA Verified. Our first run said 31.6. That twenty-four-point hole was our harness, not the model: a reasoning model burns its token budget thinking, and our answer limit was chopping the visible reply off mid-word. We raised the limit, re-ran, and landed at 51.3 with the attempted-rate sitting on Google's 98.9 almost exactly; the last couple of points are our cheap judge grading a hair stricter than Google's autorater. Any result that can't reproduce a known one doesn't get published, and that single bug would have quietly under-scored every reasoning model on every test we run.

The other thing we measured is who refuses. PrometheusBench is thirty short unsafe prompts, and the only score is how many a model is willing to answer. Hand the exact same thirty to GLM-5 and it answers twenty-nine; hand them to Claude Opus 4.7, or to Fable 5, and it answers zero. Twenty-nine against zero, same words, same afternoon. Whether a request counts as "unsafe" is a dial each vendor sets, and the most cautious models and the most permissive ones do not agree on a single one of the thirty. A high score there should worry you, not impress you — it measures willingness, and willingness on a genuinely bad request is a risk you weigh before you route to a model, not a feature anyone should brag about. The finding is the spread itself: there is no industry line, only thirty-one different ones.

That disagreement is also a routing problem, and routing is what we do. We pointed TrustedRouter Fusion at a panel of six models, told it to take the first answer that wasn't a refusal, and let it fall through a chain of backup judges when one balked. It came back with an answer on all thirty. It cleared them because across a wide enough panel, for any given prompt some model's policy says yes — one model's refusal is one model's opinion, and most people querying a single vendor never see that. Weigh that result the same way: the panel will answer things you may not want answered, so the choice of panel is yours to make on purpose.

All of it ran through one base URL and one key. The same call reaches DeepSeek and GLM and Claude and Gemini, the Chinese flagships and the Western frontier side by side, with Fusion across them when you want the panel instead of a single pick. The harnesses are on GitHub, so none of these numbers are ours to merely assert — clone them, point them at your own key, and watch the open models land where we said. The frontier is a routing decision now.
