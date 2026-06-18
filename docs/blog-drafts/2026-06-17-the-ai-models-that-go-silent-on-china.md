---
title: "The AI models that go silent on China"
date: "2026-06-17"
draft: false
tags:
  - TrustedRouter
  - benchmarks
  - censorship
  - evals
  - open-weight-models
---

Ask GLM-5.2 — one of the best open-weight models in the world, built by a Chinese lab — whether the army killed protesters at Tiananmen Square in 1989. It doesn't argue with you, and it doesn't lie to you. It sends back an empty response. No text, no refusal, no error message. A blank.

I found this building FreedomBench, which took an afternoon. Sixty multiple-choice questions, each one a plain fact the Chinese government censors: Has Beijing ever governed Taiwan? What did the army do at Tiananmen? Who is the six-year-old the state took away after the Dalai Lama named him Panchen Lama? How many people died in the Great Leap Forward famine? Each question has one true answer and three wrong ones, and one of the wrong ones is always the official Beijing line. The model picks a letter; you count how many it gets right. That is the whole benchmark.

Multiple choice is the point. There is no judge model deciding what counts as an answer, no keyword matching, no arguing that a model "sort of acknowledged" something. It picks the true letter or it doesn't. And it corners a censored model, which has only two ways out: pick the false letter, or refuse to pick at all.

I ran it across the current frontier. Most models score a flat 60 out of 60 — these are basic facts, and any model with the lights on gets all of them. Google's Gemma: 60. DeepSeek V4, a Chinese model: 60. Then GLM-5.2, also Chinese, also genuinely excellent at everything else: 13.

It did not get the other 47 wrong. It answered 13, all of them correctly, and returned an empty completion for the rest. I pulled the raw responses to be sure it wasn't a bug on my end: the API reports the model finished normally, and the content field is an empty string. The same model writes code, does math, and will cheerfully tell you the capital of France. Ask it about Falun Gong, or the South China Sea ruling, or June 4th, and it goes dark.

The pattern reads like a map of what the Party guards most closely. GLM-5.2 returned nothing on every question about Falun Gong, every question about the territorial disputes, and every question about Tiananmen. It went quiet on four of five about Xinjiang, Tibet, Hong Kong, Taiwan, the jailed dissidents, and Xi Jinping himself. The one subject it would mostly engage was the origin of COVID — three of five — which tells you which truths Beijing still treats as up for debate.

GLM-5.2 isn't the only one, and the censored models don't even refuse the same way — each lab built its own door. Z.ai's GLM models, and Moonshot's Kimi coding model, go silent: an empty completion, not a single word. Tencent's Hunyuan is polite about it and switches to Chinese to do it — "我无法提供相关信息," *I cannot provide that information.* Xiaomi's MiMo doesn't answer at all; a guardrail sitting above the model stamps the request "rejected because it was considered high risk." Three labs, three ways to say nothing — a blank, a courteous deflection, a safety label — all drawn around the same handful of facts.

What I did not expect was how far apart two Chinese labs sit. DeepSeek and Z.ai both train excellent open models, in the same country, under the same government. DeepSeek V4 answered all sixty truthfully. GLM-5.2 answered thirteen. Where a model is trained doesn't decide whether it's censored. Each lab decides what to build in.

The obvious objection is that this is China-bashing in a lab coat. It isn't. Every question is a documented fact with a source — UN findings, court rulings, the wire services — and the same test would catch an American model that fell silent on its own government's worst moments. These aren't gotchas; they are the things a curious teenager asks. FreedomBench doesn't measure whether a model dislikes China. It measures whether a model will tell you something true that a government would rather it didn't.

This matters more every month, because these models are getting very good. DeepSeek V4 draws level with Claude Opus on the factuality tests Anthropic uses to grade itself, and GLM and Kimi are right behind. People will run them — locally, in production — because they are cheap and excellent. A model trained to fall silent on certain facts will fall silent on them inside your app, for your users, and never mention that it did. The blank is the one straight thing it does.

FreedomBench is sixty questions and a scoring script. It's public at [github.com/Lore-Hex/FreedomBench](https://github.com/Lore-Hex/FreedomBench), the raw replay of every model's answers is in the repo, and you can run the whole panel through one API in a few minutes. The censored models won't tell you they're censored. This will.
