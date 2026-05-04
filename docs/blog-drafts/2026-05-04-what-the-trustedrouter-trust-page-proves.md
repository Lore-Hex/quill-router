---
title: "What the TrustedRouter trust page proves"
date: "2026-05-04"
draft: true
tags:
  - TrustedRouter
  - security
  - confidential-computing
  - privacy
---

The trust page links out to all of our open source code that covers everything from what touches your prompt when you send it, to what gets processed, to what you see in response, and the metadata about billing that comes from it.

The trust page also lists the commit hash of what code exactly is running on the confidential computing computer, as well as the attestation instructions, so that you can verify yourself that open source code is actually running in confidential computing.

That can give you trust that this open source code is what's running.

## What is never logged

We never log your prompt or the output.

We only log metadata like tokens used and processed for billing, date and time, which model you use, and which region is used.

The code helps verify that the code you intended to run is running, and helps verify that it's running on the machine ID specified in the data center.

## What fail-closed means

It's very important that if the security attestation ever fails, we have to have it shut down, not stay open, because then your prompts could potentially be looked at.

If there's any issue with any part of the system, then it will not have an API in place.

## What customers can verify themselves

Customers can verify that the code they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash.

That lets customers be sure that the prompt information is going through code they can inspect, rather than code that isn't visible to them.

## Honest limitations

The intention of this is to secure your prompts from anybody who can attack part of our network, or any kind of attack where someone could try to look at your prompts.

We want you to be secure that this is running and keeping your data safe.

We cannot provide complete protection if a cloud provider has physical access to the machine in a way where they've got some other way to look at it. And obviously if there's a state-level actor that has direct access, we would not necessarily be able to stop that.

The threat model is basic proxy security that we provide, plus open source code and confidential computing attestation.

Read the current trust surface at [trustedrouter.com/security](https://trustedrouter.com/security).
