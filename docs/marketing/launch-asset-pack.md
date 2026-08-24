# TrustedRouter launch asset pack

Everything here is **draft, ready to post**. Nothing has been posted. The
publish actions (HN, X, Reddit, LinkedIn, PH) are yours to fire — these are
copy-paste ready and timed.

## Market context (why now, who we're up against)

**The wedge — ride this wave:** OpenRouter raised a $113M Series B on
2026-05-30. The HN thread (457 pts, 253 comments,
news.ycombinator.com/item?id=48338660) is full of the exact complaints TR
answers: *"inputs and outputs are going into someone's training database,"*
*"ZDR isn't the default,"* *"middleman tax,"* *"no moat."* The category is
top-of-mind and the privacy gap is the loudest objection. Launch into it.

**The comp to study:** Tinfoil (YC X25) ran "Launch HN: Verifiable Privacy
for Cloud AI" — 146 pts, 100+ comments (news.ycombinator.com/item?id=43996555).
Nearly the same pitch. We'll face the same skeptics; their objections are
pre-empted in the HN first-comment prep below. Our edge over Tinfoil: TR is a
*verifiable router across every provider*, not single-cloud confidential
inference.

**Reality check:** generic "OpenAI-compatible LLM gateway" Show HNs die at
single-digit points (the category is crowded). The launch only works if we
lead with **(1) attestation + the no-logging proof, (2) the founder story
(ex-DARPA Grand Challenge at Princeton, AI PhD, 20 years), (3) the
OpenRouter-privacy wedge** — NOT "another gateway."

**The one-line contrast to own everywhere:**
> Everyone else asks you to trust their promise not to log. TrustedRouter lets
> you cryptographically verify the prompt path — and proves it logs nothing.

OpenRouter = convenience, trust-us. Portkey / Cloudflare = log-everything for
control. LiteLLM = self-host but unverified. **TR is the only one selling proof.**

Canonical links used throughout:
- Product: https://trustedrouter.com
- Playground: https://trustedrouter.com/chat
- Verify / security: https://trustedrouter.com/security
- Essay: https://jperla.com/blog/attestation-is-all-you-need
- Repo: (fill in the public GitHub URL)

---

## 1. Show HN  ← the single biggest lever

**Title** (HN rules: factual, ≤80 chars, no hype, no trailing period):

```
Show HN: TrustedRouter – open-source LLM router you can cryptographically verify
```

Alt titles if the first feels long:
- `Show HN: An OpenRouter alternative with hardware attestation`
- `Show HN: LLM router that proves it isn't logging your prompts`

**Body** (HN text field — first-person, technical, honest, no marketing voice):

```
I'm Joseph. I built TrustedRouter because every LLM provider tells you
they don't log your prompts and none of them lets you check.

It's an OpenAI-compatible router (same shape as OpenRouter — change one
base_url, keep your model IDs) that runs its gateway inside GCP
Confidential Space. The CPU signs a measurement of the running binary.
You fetch /attestation?nonce=<yours>, get back a JWT signed by the
hardware root key, and compare the image_digest to the hash published
for the open-source build. If they match, the code handling your
prompts is the code you can read on GitHub.

The whole thing is open source — control plane, gateway, config, UI.
Prompt and output bodies are never written to disk; you can verify that
in the source rather than trust a policy. If attestation can't verify,
the gateway fails closed.

You can try it without signing up at https://trustedrouter.com/chat
(compare up to 4 models side by side; nothing fires until you sign in).
The verification walkthrough is at https://trustedrouter.com/security
and I wrote up the thinking here:
https://jperla.com/blog/attestation-is-all-you-need

Honest limits: attestation proves the running binary is the published
binary, not that the binary is bug-free. It doesn't defeat a nation
state with physical access to the cloud host. Upstream providers handle
prompts per their own policies — I publish each one's posture on the
model pages so you can route deliberately.

Happy to go deep on the enclave setup, the attestation chain, or why I
think this should be table stakes for inference. Ask
away.
```

**Launch timing:** Tuesday–Thursday, 8:00–9:15am Pacific. Avoid Mon/Fri and weekends.

**First-comment prep — the skeptic gauntlet.** These are the 8 objections the
Tinfoil launch actually got, plus the OpenRouter-specific ones. Have answers
typed and ready to paste the second a question lands. Speed + honesty here is
the whole ballgame.

- *"How is this different from OpenRouter?"* → OpenRouter is a closed hosted
  router; you trust their policy. TR is open-source + the gateway is
  hardware-attested, so you verify the code path instead of trusting it. Same
  API, same models. (Re: the Series B thread — the "prompts go to a training
  DB" worry is exactly what verification removes.)
- *"Why not just use GCP confidential computing directly?"* → You can, for
  one provider, if you build all the routing/billing/fallback/attestation
  plumbing yourself. TR is that plumbing, open-source, verifiable, across 30+
  providers — so you're not locked to one cloud's models.
- *"How do I actually trust the attestation / what stops you faking it?"* →
  The JWT is signed by Google's hardware-backed attestation service, not by us. We can't forge
  that signature. The nonce you supply prevents replay. Exact commands: /security.
- *"You still trust the chip vendor."* → True, and I say so. Attestation moves
  trust from "the operator's policy" to "the silicon vendor's root key." That's
  a much smaller, better-studied surface.
- *"Confidential computing has existed for years with ~zero adoption."* → Right
  — because nobody put inference behind it and made it one-line easy. The
  overhead is now single-digit milliseconds. That's the bet: it should be table
  stakes, and it wasn't because it was hard, not because it was useless.
- *"Nation-state with physical host access can still coerce the hardware."* →
  Correct. In scope: operator can't see your prompts + code is provably the
  published code. Out of scope: a state actor with physical access. I'm explicit
  about this on /security.
- *"What about the upstream provider — they still see the prompt."* → Yes. TR
  proves *our* hop doesn't log and runs published code. For the upstream hop,
  route to providers with their own ZDR/confidential posture — we publish each
  one's status on the model pages so you choose deliberately.
- *"No SOC 2 / HIPAA / FedRAMP?"* → [state the honest current status +
  roadmap]. The attestation + open source is a *stronger* technical guarantee
  than a SOC 2 attestation of process; the compliance paperwork is in progress
  for buyers who need the checkbox too.
- *"Open source — can I self-host?"* → Yes. Build the image yourself; the hash
  you build is the hash your enclave reports, so attestation still works for
  your own deployment.

---

## 2. X / Twitter thread (post from @jperla, time it within ~30 min of HN going live)

**Tweet 1 (hook):**
```
Every LLM provider tells you they don't log your prompts.

You're supposed to believe them.

I built the version you can actually verify. Open source, hardware-attested, OpenAI-compatible.

Show HN today 👇
```

**Tweet 2:**
```
A privacy policy is a thing you can sue over after it's broken.

Attestation is a thing you can check before you hit send.

The CPU signs the running binary. You compare the hash to the open-source build. Match = you know what code touched your prompt.
```

**Tweet 3:**
```
How it works at TrustedRouter:

→ GCP Confidential Space
→ fetch /attestation?nonce=<yours>
→ get a JWT signed by the hardware root key
→ image_digest matches the published artifact

The router can't lie about what it's running.
```

**Tweet 4:**
```
It's a drop-in OpenRouter replacement.

Change one base_url. Keep your OpenAI SDK, your model IDs, your code.

30+ providers — Claude, GPT, Gemini, DeepSeek, Kimi, Llama, Mistral. One key.
```

**Tweet 5:**
```
Prompt + output bodies are never written to disk. Not "we promise" — it's in the source, and the gateway fails closed if attestation can't verify.

Try it, no signup, compare 4 models side by side:
https://trustedrouter.com/chat
```

**Tweet 6 (honest limits — this earns trust on X too):**
```
What it does NOT do: defeat a nation-state with physical host access, or prove the open-source code is bug-free.

It proves the running binary is the published binary, on hardware you can challenge with a nonce. That's the bar nobody else is clearing.
```

**Tweet 7 (CTA):**
```
Full essay — "Attestation is All You Need":
https://jperla.com/blog/attestation-is-all-you-need

Verify it yourself:
https://trustedrouter.com/security

HN thread (would love your hardest questions): [paste HN link here]
```

**Reply-tweet after the thread:** quote-tweet the HN link once it's up.

---

## 3. Reddit (one sub at a time, space them a day apart — never blast)

Confirm each sub's self-promo rules before posting; research agent is
checking norms. Default-safe targets: r/LocalLLaMA, r/selfhosted.

**r/LocalLLaMA** (most aligned — privacy-literate, skeptical, technical):

Title:
```
I built an open-source LLM router you can cryptographically verify isn't logging your prompts
```
Body:
```
Most hosted routers ask you to trust a privacy policy. I wanted one where
you can check.

TrustedRouter runs its gateway inside GCP Confidential Space. You hit
/attestation?nonce=<yours> and get a JWT signed by the hardware-backed
attestation service; the image_digest matches the hash of the open-source build. So you
can verify the exact code that's handling your prompts, instead of trusting
a policy.

OpenAI-compatible (change one base_url), 30+ providers, prompt/output bodies
never persisted (verifiable in source), fails closed if attestation breaks.

Open source, and you can self-host — your built image hash is what your
enclave reports, so attestation works for your own deployment too.

Try it without signup (compare 4 models side by side): trustedrouter.com/chat
Verify walkthrough: trustedrouter.com/security
Why I built it: jperla.com/blog/attestation-is-all-you-need

Happy to answer anything about the enclave setup or the attestation chain.
```

**r/selfhosted** (lead with the self-host + no-logging angle, soften the hosted bits):

Title:
```
Open-source LLM router with hardware attestation — self-hostable, no prompt logging
```
Body: same core, but open with "you can run the whole thing yourself" and
emphasize the build-your-own-image → attestation-still-works property.

**Skip unless research says otherwise:** r/MachineLearning (research-only,
hostile to product posts), r/OpenAI (low signal).

---

## 4. LinkedIn (Joseph's profile — B2B / regulated-industry reach)

```
Every LLM provider says they don't log your prompts. None of them lets you check.

I built TrustedRouter to change that. It's an open-source, OpenAI-compatible
LLM router whose gateway runs inside GCP Confidential Space. You can
cryptographically verify — with a nonce challenge
against the hardware root key — that the code processing your prompts is the
open-source code we published, and that it never writes your prompts to disk.

For teams in healthcare, legal, and finance that can't put sensitive data
through an opaque router: this is privacy you can audit, not privacy you have
to take on faith.

Same API as the router you're already using. 30+ models. One key. Self-hostable.

Try it: trustedrouter.com/chat
The thinking behind it: jperla.com/blog/attestation-is-all-you-need
```

---

## 5. Product Hunt (queue for a separate day from HN — don't split attention)

- **Name:** TrustedRouter
- **Tagline (≤60 chars):** `The LLM router you can cryptographically verify`
- **Alt tagline:** `OpenRouter, but you can prove it isn't logging you`
- **First comment (maker):**
```
Hey PH 👋 I'm Joseph. Every LLM API promises not to log your prompts;
TrustedRouter lets you verify it. The gateway runs in hardware enclaves
and signs the exact binary it's running — you challenge it with a nonce
and check the hash against our open source. Same API as OpenRouter,
30+ models, self-hostable. Try it free (no signup) at the chat
playground. Would love your feedback on the verification flow.
```
- **Topics:** Artificial Intelligence, Developer Tools, Privacy, Open Source

---

## 6. Dev.to / Hashnode cross-post (SEO + dev audience, canonical → jperla.com)

Re-publish "Attestation is All You Need" with a `canonical_url` pointing at
jperla.com so it doesn't compete for ranking. Tags: ai, security,
opensource, privacy. Adds backlinks + a second discovery surface.

---

## 7. Evergreen discovery (do these once; they pay off for months)

These are not launch-day — they're the long tail. Highest leverage first.

**GitHub awesome-list PRs** (free, evergreen, high-authority backlinks):
- `tensorchord/Awesome-LLMOps` — canonical LLMOps list, has a gateway/proxy
  section (LiteLLM, AI Gateway, TensorZero live there). TR belongs here.
- `bpradipt/awesome-confidential-computing` — perfect topical fit (TEEs,
  attestation). Add TR to the attestation/frameworks section.
- `InftyAI/Awesome-LLMOps`, `jihoo-kim/awesome-production-llm` — secondary.
- Each PR: one line + link, framed as "open-source attested LLM gateway."

**Comparison-roundup outreach** (these already rank for "best LLM gateway 2026"):
- helicone.ai/blog/top-llm-gateways-comparison
- braintrust.dev/articles/best-llm-gateways-2026
- inworld.ai/resources/best-llm-gateways
- Email each: "please add TrustedRouter — we're the only entry that can check
  the 'hardware attestation / no prompt logging' box." That column is our moat
  in a table full of look-alikes.

**Confidential Computing Consortium** (credibility + the exact niche room):
- confidentialcomputing.io — get TR listed.
- CCC Attestation SIG (`github.com/CCC-Attestation`) holds public fortnightly
  meetings. Present there — it's literally the room where confidential-computing
  attestation people are. Best credibility-per-hour available.

**AI directories** (low effort, modest traffic): theresanaiforthat.com,
futurepedia.io. Submit, don't over-invest.

**Newsletters** (time the pitch to the Show HN): Ben's Bites, TLDR AI,
Latent Space. They cover launches — give them the HN link + the founder angle.

---

## Launch-day playbook (one page)

1. **T-1 day:** confirm GitHub repo is public + README is strong (first thing
   HN clicks). Confirm /chat + /security + the blog post all load fast.
2. **T-0, 8:05am PT:** post Show HN. Do NOT ask anyone to upvote (HN bans for
   it). Just post.
3. **T+10 min:** post the X thread, last tweet links the HN thread.
4. **T+15 min:** be in the HN comments. Answer every question fast, honestly,
   technically. This is where the launch is won or lost.
5. **T+2 hrs:** post r/LocalLLaMA.
6. **T+1 day:** post r/selfhosted, LinkedIn.
7. **T+3 days:** Product Hunt (its own day), Dev.to cross-post.
8. **Throughout:** when someone verifies the attestation and says "huh, it
   actually works" — screenshot it, that's your best social proof.

The whole campaign rides on one thing being true: a skeptical engineer can
verify the claim in 60 seconds. Everything points back to that.
```
