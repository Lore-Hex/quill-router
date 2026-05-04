# Positioning Interview

Date: 2026-05-04

## Raw Transcript

What is TrustedRouter in one sentence?

TrustedRouter is one API, all the LLMs, provably private. TrustedRouter lets you route to any LLM very easily. Switch between all your LLMs, have higher uptime, have backups, and try out much cheaper models, saving you money without needing to trust TrustedRouter servers as an intermediary any more than you need to. This is because it uses a secure enclave to prove that it's working. Most importantly it's all open source software. Every part of the backend infrastructure, configuration, bring up, and UI is entirely open source. Why is OpenRouter compatible but verifiable? The wedge: we make it super easy to one-line replace your call to any direct LLM (OpenAI or ChatGPT or any other one) and immediately have it be a router that can handle all of it transparently. By being open source and verified, OpenRouter also lets you switch between different sources but you have no idea really what it's doing in terms of your prompt and how safely it's carrying it out. You can't really know whereas for our code you know end to end what's happening with open source and you know that it's running.

Who is this for first? This is for anybody that wants this feature because many engineers are using OpenRouter but only for low-security non-sensitive data because they know it's not so safe to put sensitive data in this third-party router. If you know end to end the software that's running and that it's verified secure then you can put sensitive data because it is running well and it has FIPS which means that it's certified secure and you can attest to it every time you connect and continuously while you're connecting.

It's also for people who've never tried OpenRouter but who want to but again didn't have a great trust of their security story and should maybe call it the mistrusted router, insecure router, an unverifiable router.

Why should people stop trusting opaque routers? We should really demand that all routers be totally open source. That's really the only way to be sure that it is running things that are safe and secure. Many eyes catch all bugs and by having the whole community be able to verify all pieces of the infrastructure then we can know it's secure. On top of that we had provable security by having secure enclave attested security around all of the code that's running. It's a very minimal amount of code in the attested router.

What should we never claim? Not sure. Let's think about it. We can never claim that we're perfectly secure but we can claim that we're perfectly transparent and being open source and open about everything that we're doing. If there are any issues that come up, we take them very seriously, both security issues or feature requests or bugs.

Why should someone care enough to switch this week? Well we're meeting a lot of people who are using OpenRouter or something like it but only for a fraction of their use cases. They would love to send other traffic, other use cases to something like it but they can't really trust its security so they don't. Now that we have an API equivalent drop-in replacement that looks and feels even better and is entirely open source, then they can easily switch to it. They can take the open source project and run it themselves on their hardware. They can run it themselves internally. They can run it hosted. Someone else can take this and run it.

What we want is for proliferation of these routers to exist because it's important that people have the safety, security, uptime, and cost savings from this. That's what I really want everyone to have. I got into this because I'm trying to democratize everybody in the world's access to large language models and AI. It's something I've been working on for 20 years, both when I was working on the DARPA Grand Challenge self-driving car project at Princeton and while I was working on my PhD in AI. Especially today when we're seeing increasing amounts of centralization, that's very concerning for the future and a lot of people are feeling a lot of anxiety about AI because of this. By creating these tools and open sourcing them then we can create a way for people to have alternatives, whether that's free year models, cheaper models, and just things with more uptime as well. We make it super easy. I think it's just really, really interesting and fun to be using secure enclaves to really prove in confidential computing the security of what we're doing, the verifiability of what we're doing.

## Publication Notes

- Do not publish the FIPS/certification sentence until separately documented.
- Avoid publishing the "mistrusted router" line in comparison pages.
