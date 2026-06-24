---
title: "Fusion's gain lives in the synthesizer"
date: "2026-06-24"
draft: false
tags:
  - TrustedRouter
  - Fusion
  - evals
  - self-fusion
---

Fusion — run a model several times and have something stitch the answers into one — works, and it works even when every draft comes from the same model. On DRACO, the deep-research benchmark, Claude Sonnet 4.6 run ten times and fused into a single answer climbs eight points over one run: +8.0, with a 95% interval of +4.6 to +11.3. The interesting question is where those eight points come from — the ten drafts, or the model doing the stitching. So we held one fixed and changed the other.

We took the ten Sonnet research reports, left them exactly as they were, and swapped only the fuser — the model that reads the drafts and writes the final answer — from Sonnet to Haiku. Same drafts, same grader, same tasks. The gain fell from +8.0 to +2.2, an interval that sits on top of zero. Identical raw material, and a cheaper fuser threw three-quarters of the lift away. Strong drafts don't rescue a fuser that can't tell which one is right. The lever is the fuser, not the drafts.

But "the fuser" is really two jobs. First a judge reads the drafts and writes a compact analysis: where they agree, where they contradict, what each one caught that the others missed. Then a synthesizer takes that analysis plus the drafts and writes the answer. Those can be two different models, so we ran the full two-by-two — every combination of Sonnet and Haiku in the two seats, on the same Sonnet drafts, graded the same way.

Only one seat matters. With a Sonnet synthesizer the gain is +8.0 behind a Sonnet judge and +9.2 behind a Haiku judge — swapping the expensive judge for the cheap one changed nothing. With a Haiku synthesizer the gain is +4.4 behind a Sonnet judge and +2.2 behind a Haiku judge. Downgrade the synthesizer and you lose four to seven points; downgrade the judge and you lose roughly zero. A cheap Haiku judge feeding a Sonnet synthesizer matches the all-Sonnet fuser. The judge can be cheap. The synthesizer cannot.

It makes sense once you see what each seat does. The judge produces structured notes, and getting the notes a little wrong is recoverable. The synthesizer makes the actual call — out of ten messy reports, which claim survives into the answer — and that one decision is the whole game. Reading a pile of research and walking out holding the single correct fact is the skill that scales with raw model strength, and it lives in the writing step, not the analysis step. This is also why fusion sat as a footnote for years: the recipe is old, but the stitcher was always the weak link, and a model good enough to run the synthesis seat cheaply is recent.

So the cheap version of fusion is real, but only in one place. The judge in front of the synthesizer can be the fast, free model; the model that writes the final answer has to be the good one. That is a routing decision, not a model decision — ten draft calls and a judge call go to whatever is cheap, and the single synthesis call goes to the frontier. A gateway that places each call on the right model, and fans the drafts across every provider serving them at once so one provider's rate limit doesn't sink the run, is what makes the cheap version actually payable. The same fan-out that makes self-fusion work is what makes a router worth pointing it at.

*This ran end to end on Claude Code subagents, graded by Claude Sonnet 4.6 criterion by criterion, over the 23 DRACO tasks the four configurations share. The drafts, every fused answer, the per-task scores, and the bootstrap code are in [TrustedRouter-Fusion-Draco](https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco). It's a pilot — twenty-three tasks, one run-ordering — and the absolute scores are grader-inflated; the gaps between configurations are the finding.*
