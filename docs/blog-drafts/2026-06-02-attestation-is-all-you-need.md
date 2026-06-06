---
title: "Attestation is All You Need"
date: "2026-06-02"
draft: true
tags:
  - TrustedRouter
  - confidential-computing
  - attestation
  - security
  - LLMs
---

I've been sending a lot of prompts lately. Code. Drafts. Half-written ideas I would not want to read on a billboard.

Every LLM provider tells me they don't log any of it.

I'm supposed to believe them.

OpenAI publishes a 30-day retention policy and a ZDR program for enterprise. Anthropic publishes a privacy policy. Cohere has SOC 2 Type II. Each of these is **a promise**. None of them is **proof**.

A promise is a thing you can sue over after it's broken. Proof is a thing you can check before you click send.

There is a much better way. We have had it since 2018. The industry just never put inference behind it.

It is called **attestation**.

## What attestation is

A confidential VM runs inside a hardware-backed enclave. AWS Nitro. GCP Confidential VMs. Azure Confidential VMs. Same idea everywhere.

The CPU signs a measurement of the running binary.

The signature chains up to the chip vendor's root key.

You can verify the chain. You can compute the same hash from open-source code. If they match, you know exactly what code is running.

That gives you two things contracts can never give you:

The code that is running **is the code that was published**.

The code **cannot be changed at runtime** without invalidating the attestation.

You stop trusting the operator. You start trusting the silicon. The silicon's threat surface is much smaller than the operator's.

## Why this is "all you need"

Every privacy mechanism the industry sells you reduces to attestation.

**"We do not log your prompts."**
If the binary never opens a write handle on a prompt path, it cannot log. You can grep the source. The promise is now redundant.

**"Zero data retention contracts."**
If the binary never writes prompts to disk, retention is structurally zero. The contract is describing what the code does. The contract is not constraining the operator.

**"BYOK."**
Useful for billing isolation. But if the routing code is attested and non-logging, someone else's key going through the same code gets the same privacy.

**"SOC 2 / ISO 27001."**
These audit the operator's processes. Attestation skips the operator. It audits the artifact.

**"Trust us."**
Same.

Attestation is the primitive. Everything else is a slower, lossier approximation of "show me what code is running."

This is why I picked the title. The original *Attention is All You Need* paper did not say nothing else mattered. It said one primitive subsumed most of what was previously built on top.

Attestation is that primitive for inference privacy.

## How we do it at TrustedRouter

`api.trustedrouter.com` runs inside an attested gateway.

AWS Nitro Enclaves in us-east-1.
GCP Confidential VMs in us-central1 and europe-west4.

Cross-cloud. So a single vendor's compromise does not take the trust surface with it.

Every request can be paired with a live attestation.

You generate a nonce. You fetch `/attestation?nonce=<your-nonce>`. The gateway returns a JWT signed by the hardware root key.

The JWT contains:

- `eat_nonce` — the nonce you supplied, so the response can never be replayed
- `image_digest` — SHA-256 of the running container image
- `pcrs` — the platform measurements at boot

You match `image_digest` against the hash published at trustedrouter.com/security with every commit.

If they match, the code processing your prompts is the code you can read on GitHub.

If the attestation fails — image drift, hardware fault, expired cert, anything — the gateway **fails closed**. No request reaches a provider until attestation is valid again.

The synthetic monitor probes the attestation path continuously. It pages on the first miss.

## What I am not promising

Attestation is not a wand.

A nation-state with hardware access could try side-channel attacks. AWS and GCP have hardened their enclaves against most known classes. Not all.

The hardware vendor's root key is a trust anchor. If AWS's or GCP's signing infrastructure is compromised, the chain breaks. Cross-cloud helps. It does not eliminate the dependency.

Open-source code can have bugs. Attestation proves the running binary is the published binary. It does not prove the published binary is correct. Many eyes still help.

The point is not that attestation is perfect.

The point is that *not having it* requires you to trust the operator on everything. That is much worse.

## Why now

LLM traffic is becoming the new sensitive data path.

Code goes through it.
Drafts go through it.
Private contracts go through it.
Medical notes go through it.
Half-written resignation letters go through it.

The industry's default privacy story is still "trust us."

That worked for SaaS in 2010. It does not work for inference in 2026, when one well-placed prompt log can leak more about a person than their email.

Confidential computing is fast now. The Nitro overhead is single-digit milliseconds. The GCP confidential VM overhead is the same.

There is no longer a performance reason to ship inference without attestation.

There is no longer an honest privacy reason to ship inference without attestation.

That is the bet.

[TrustedRouter](https://trustedrouter.com/) is live. Trust surface, attestation flow, and full open-source code are at [trustedrouter.com/security](https://trustedrouter.com/security).
