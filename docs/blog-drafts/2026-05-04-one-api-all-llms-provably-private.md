---
title: "TrustedRouter: one API, all the LLMs, provably private"
date: "2026-05-04"
draft: true
tags:
  - TrustedRouter
  - AI
  - LLMs
  - privacy
---

TrustedRouter is one API, all the LLMs, provably private.

TrustedRouter lets you route to any LLM very easily. You can switch between all your LLMs, have higher uptime, have backups, and try out much cheaper models, saving you money without needing to trust TrustedRouter servers as an intermediary any more than you need to.

This is because it uses a secure enclave to prove that it's working. Most importantly, it's all open source software. Every part of the backend infrastructure, configuration, bring-up, and UI is entirely open source.

## OpenRouter-compatible, but verifiable

The wedge is that we make it super easy to one-line replace your call to any direct LLM, OpenAI or ChatGPT or any other one, and immediately have it be a router that can handle all of it transparently.

OpenRouter also lets you switch between different sources, but you have no idea really what it's doing in terms of your prompt and how safely it's carrying it out. You can't really know. Whereas for our code, you know end to end what's happening with open source, and you know that it's running.

## Who this is for first

This is for anybody that wants this feature, because many engineers are using OpenRouter only for low-security, non-sensitive data because they know it's not so safe to put sensitive data in this third-party router.

If you know end to end the software that's running, and that it's verified secure, then you can put sensitive data through a router with much more confidence because you can attest to it when you connect and continuously while you're connecting.

It's also for people who've never tried OpenRouter, but who want to and didn't have a great trust of the security story.

## Why opaque routers should not be enough

We should really demand that all routers be totally open source. That's really the only way to be sure that it is running things that are safe and secure.

Many eyes catch bugs, and by having the whole community be able to verify all pieces of the infrastructure, we can know what's running. On top of that, we add provable security by having secure enclave attested security around the code that's running.

## Why switch now

We're meeting a lot of people who are using OpenRouter or something like it, but only for a fraction of their use cases. They would love to send other traffic and other use cases to something like it, but they can't really trust its security, so they don't.

Now that we have an API-equivalent drop-in replacement that looks and feels even better and is entirely open source, they can easily switch to it. They can take the open source project and run it themselves on their hardware. They can run it internally. They can run it hosted. Someone else can take this and run it.

What we want is for proliferation of these routers to exist, because it's important that people have the safety, security, uptime, and cost savings from this. That's what I really want everyone to have.

I got into this because I'm trying to democratize everybody in the world's access to large language models and AI. It's something I've been working on for 20 years, both when I was working on the DARPA Grand Challenge self-driving car project at Princeton and while I was working on my PhD in AI.

Especially today, when we're seeing increasing amounts of centralization, that's very concerning for the future. A lot of people are feeling a lot of anxiety about AI because of this. By creating these tools and open sourcing them, we can create a way for people to have alternatives, whether that's freer models, cheaper models, or things with more uptime as well.

We make it super easy. I think it's really interesting and fun to be using secure enclaves to prove in confidential computing the security of what we're doing, and the verifiability of what we're doing.

[TrustedRouter](https://trustedrouter.com/) is live now.
