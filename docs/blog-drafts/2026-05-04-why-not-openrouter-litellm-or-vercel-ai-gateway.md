---
title: "Why not OpenRouter, LiteLLM, or Vercel AI Gateway?"
date: "2026-05-04"
draft: true
tags:
  - TrustedRouter
  - OpenRouter
  - LiteLLM
  - Vercel
---

## Why not just use OpenRouter?

The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts.

They have a lot more prompts that they would love to use something like OpenRouter for, but they're not able to have that trust with OpenRouter. So then they are looking for an alternative solution in TrustedRouter.

## Why not just self-host LiteLLM?

That's a great solution.

LiteLLM has been around for a while, and a lot of teams love to self-host. If you're that type of team, then you should totally do that.

You can also take our open source code and use that as well. You can take our code and create another version of TrustedRouter that you run as a service. We're very happy with that because we want to let many other companies have this service.

We want it to be fully Apache-licensed open source, a ubiquitous solution to reduce centralization on just certain very expensive models.

## Why not just use Vercel AI Gateway?

Vercel AI Gateway is also great, but again, not open source. You don't know what's running.

Even if you had the source, they don't use an attested gateway. It's that trust that really gives you confidence that it's going to keep working.

## What about latency?

Prompts have multi-second latencies on their own. LLMs often have multi-second time to first token latency on their own.

Our latency is in tens of milliseconds, and we have regions around the world. We do load balancing by picking your closest one, and you can also pick your favorite closest data center that we provide.

We do full streaming chat, so we're not buffering anything. You're immediately getting it as soon as we get it.

## What about model coverage?

We cover the top models that people are using, and we're adding more every day.

We're open source as well, so we accept people submitting issues and suggestions for other providers and other models to add.

## What about price?

We're offering a price that's competitive with OpenRouter.

## When is TrustedRouter not the right fit?

The main reason would be if you aren't looking for a hosted solution.

If you need to have something that you host yourself, then please use our open source software and host it yourself. Or if you're looking to own your flow and make something very custom, then that's something that you want to own.

I know a lot of engineers that always want to roll their own things and not trust someone else to manage it and keep it up to date. They like doing that themselves.

TrustedRouter is for teams that want hosted OpenAI-compatible routing, with open source code and verifiable privacy.

Read more at [trustedrouter.com/compare/openrouter](https://trustedrouter.com/compare/openrouter).
