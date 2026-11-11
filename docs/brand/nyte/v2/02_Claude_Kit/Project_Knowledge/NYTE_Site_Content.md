# NYTE · Approved site content (nytokenexchange.com, read Nov 3, 2026)

Source of truth for copy, figures and claims. Anything not here is a placeholder: put it in [brackets] until a person confirms it. Re-check the live site before publishing figures.

## Navigation
Buy: Confidential API (/confidential) · Trusted Cowork (/trusted-cowork) · Reserved tokens (/buy). Sell: Suppliers (/suppliers) · Providers (/providers) · Neocloud accelerator (/neocloud-accelerator). Exchange: Token Index (/index) · Buyer deck (/#deck). CTA: Start building. Footer: Media kit, Privacy, Terms, Exchange overview, Email us (help@trustedrouter.com); regional exchange links (Global, Chicago, SF, Texas, US, Europe, London, Dubai, Riyadh, Hong Kong, Shanghai, Tokyo).

## Home
- H1: Frontier AI your security, legal and finance teams will all agree on.
- Sub: Every major model from competing providers, through one API. Set your privacy and price rules once, and check the proof yourself.
- CTAs: Reserved tokens · Get the buyer deck · Watch the 90-second walkthrough.
- Press quotes (publication names to confirm): "A storefront for businesses wanting to tap any AI model." / "I was able to use a DeepSeek model in under 30 seconds." / "Neither TrustedRouter nor the data center running the model can read a customer's prompts or outputs."
- Case study: 170,974 privileged legal documents processed. In production in three weeks. "Trust us, we won't peek" was never an acceptable answer to give a client. — Robot, Robot & Human.
- Three layers of protection, each checkable: Confidential compute (prompt runs inside verified hardware the provider can't see into) · End-to-end encryption (request stays encrypted until it reaches that hardware) · Zero data retention (prompts and outputs discarded once the response is sent). CTA: See the evidence.
- Routing demo: nyte/auto · "Summarize the attached credit memo" · Rules: CONFIDENTIAL, ZDR, US, ≤ $5 / 1M · ✕ Claude Opus 5 (Anthropic) no confidential compute · ✕ GPT-5 mini (OpenAI) no confidential compute · ✓ DeepSeek V4.1 Flash (Tinfoil) $1.53 / 1M, Attested, build sha256:3d126321…72e2e3f0.
- Stats: 675 models through one API, powered by TrustedRouter · 100 providers competing on price · 5.5% fee, wholesale pricing as low as 0.1% at scale · Zero: provably zero data retained on Confidential routes, plus E2EE options.
- Provider names (type only, never logos): OpenAI, Anthropic, Mistral, Google Vertex AI, DeepSeek, xAI.
- Built for teams that answer to a security review: Legal "Flag privileged passages in 2,400 discovery documents." (CONFIDENTIAL ZDR US) · Investment research "Compare the covenants in these 40 loan agreements." (CONFIDENTIAL ZDR) · Financial platforms "Explain this transaction to the account holder." (ZDR US ≤ $2 / 1M).
- Close: Start with one workload. A 30-day pilot on your own tasks. Start building · Get the buyer deck.

## Token Index (/index)
- Eyebrow: TOKEN PRICE INDEX · $NYTE. H1: NYTE Token Index. Sub: AI inference prices, in US dollars per billion tokens.
- Headline: $380 per billion tokens · −3.0% over 7 days · as of October 26, 2026.
- Grades, "Priced by capability": AAA Frontier $1,794 (−0.3% 7D) — frontier research, hardest reasoning, strategic synthesis · A Advanced $985 (−5.7%) — coding agents, complex analysis · B Professional $219 (+3.2%) — production assistants, structured analysis, code review · C Efficient $27 (−6.3%) — extraction, summarization, classification, routing. Each grade: "Reserve [grade] tokens".
- Footnotes: same published index, grades and chart data as TrustedRouter's Token Index. Provisional. Not for trading. Index levels are a reference, not executable provider quotes or a guarantee of available capacity.

## Confidential API (/confidential)
- H1: Frontier AI for sensitive work. Sub: For prompts that carry client material, regulated records or your own source code. Every request runs on verified confidential hardware, encrypted end to end, and is never stored.
- Layers: Confidential compute · End-to-end encryption · Zero data retention · Refused, never downgraded (if no route meets all three, the request stops; nothing reaches a provider).
- Attestation: published build digest sha256:c43f1d7c…508639 · published source a4950bb6 · Location: not proven by attestation.
- Uptime 24h (GCP): Canonical API 100.00% · US East Regional API 100.00% · Model Inference 99.99%.
- Models on confidential routes: DeepSeek V4.1 Flash 1017 (Tinfoil, Confidential + E2EE) $0.68575 in / $1.52975 out per 1M · GPT OSS 120B (Tinfoil, Confidential + E2EE) $0.15825 / $0.633.
- Code: base_url="https://api.confidential.nytokenexchange.com/v1", model="nyte/auto", extra_body={"provider": {"min_privacy": "confidential"}}. "Keep your SDK. Confidential inference."
- FAQ: no qualifying route → refused with an error · New York is the market, not where requests run; restrict to US-hosted providers in one setting · TrustedRouter account, balance and key limits carry over · pilot: one workload, 30 days · "Is this a securities or crypto exchange? No. NYTE sells AI inference, billed by the token."

## Suppliers (/suppliers)
H1: Sell your inference to enterprise buyers. Sub: List your models and prices once. When a buyer's rules match your route and your price wins, the request is yours. Three steps: Show your service · Make it verifiable (public conformance suite) · Serve real workloads. Apache-2.0 open-source serving stack available. Listing figures on the site are illustrative.

## Providers (/providers)
H1: More demand. Same GPUs. Higher utilization and revenue are target benefits, not guarantees; capacity futures and derivatives are planned offerings, not live products.

## Reserved tokens (/buy)
H1: Reserve your next billion tokens. Reserve index-grade tokens by card for 2027 delivery, or shape a reservation request. Units: 1 billion tokens · 300 tokens per second · Not sure yet. Card checkout purchases a reservation for 2027 delivery; capacity futures are planned, not live.

## Trusted Cowork (/trusted-cowork)
A desktop agent for sensitive work. Frontier models on local files in an open-source harness. macOS 13+. Default route nyte/confidential · data collection deny · no eligible route: fails closed.

## Neocloud accelerator (/neocloud-accelerator)
Pre-sell tokens. Build your neocloud. Two planned facilities (targets, not live): Solar Powered Datacenter — 20 kW, targeted Feb 2027, 1,000 lots · Underwater Datacenter — 1 MW, targeted Aug 2027, 50,000 lots. A lot = 1B tokens over the facility's first 12 months.

## Naming and legal (media kit)
Name: NY Token Exchange; short name NYTE; category Enterprise AI inference; powered by TrustedRouter; operator Lore Hex Corp (Delaware); Founder & CEO Joseph Perla; press help@trustedrouter.com. First mention "NY Token Exchange", then NYTE; never "NY Token Exchange (NYTE)". Logos are not certifications. Badge name to confirm: "Verified Secure" (brand system) vs "Privacy Secured seal" (media kit).

## Claims discipline
The index is provisional, not for trading · capacity futures and derivatives are planned, not live · utilization and revenue benefits are targets, not guarantees · supplier listing figures are illustrative · location is not proven by attestation · provider logos are not licensed (name them in type).
