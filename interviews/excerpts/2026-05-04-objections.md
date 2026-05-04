# Objections Excerpts

Source: `interviews/transcripts/2026-05-04-objections.md`

## OpenRouter

The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts.

They have a lot more other prompts that they would love to use something like OpenRouter for, but they're not able to have that trust with OpenRouter. So then they are looking for an alternative solution in TrustedRouter.

## LiteLLM And Self-Hosting

LiteLLM has been around for a while, and you can also just take our open source code and use that as well. It's also a battle-tested solution.

A lot of teams love to self-host and if you're that type of team, then you should totally do that.

You can also take our code and create another version of TrustedRouter that you run as a service.

We're very happy with that because we want to let so many other companies have this service and be open, fully Apache licensed open source.

## Vercel AI Gateway

Vercel AI Gateway is also great, but again, not open source. You don't know what's running.

Even if you had the source, again, they don't use an attested gateway.

It's that trust that really gives you confidence that it's going to keep working.

## Latency

The LLMs often have multi-second time to first token latency on their own.

Our latency is in tens of milliseconds and we have regions around the world or in data centers around the world.

We do full streaming chat so that we're not buffering anything. You're immediately getting it as soon as we get it.

## Model Coverage

We cover the top models that people are using and we're adding more every day.

We're open source as well, so we accept people submitting issues and suggestions for other providers to add, other models to add.

## Price

We offer a price that's competitive with OpenRouter.

## Not The Right Fit

The main reason would be if you aren't looking for a hosted solution. If you need to have something that you host yourself, then please use our open source software and host it yourself.

If you're looking to just own your flow and make something that's very custom then that's something that you want to own.

I know a lot of engineers that always want to roll their own things and not trust someone else to manage it and keep it up to date. They like doing that themselves.
