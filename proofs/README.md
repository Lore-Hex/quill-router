# proofs/

TLA+ specs, model-checked by TLC in CI (`proofs` job, `./proofs/check.sh`).

A spec here covers a protocol whose failures are interleavings: things a test
cannot reach because the composition does not exist in code yet, or cannot be
driven to the bad schedule. It is written before the code it describes.

| Spec | Protocol | Code |
|---|---|---|
| `AutoRefillHandoff` | Settlement handing auto-refill to a credentialed drain, across a surface split and rollback | implemented |
| `SurfaceCutover` | The routed multi-region Cloud Run rollout, with a crash between any two steps | implemented |
| `TerminalOrder` | One lease's records: which terminal wins, and why the live auditor and a rebuild agree (fast admission §4.5, §4.8) | planned |
| `LeaseLifecycle` | One lease over time: renewals, the owner's cutoff, draining and close under clock skew (fast admission §4.2, §4.8) | planned |

`docs/design/fast-admission-and-batched-settlement.md` §5.1 has the plan for
the fast-admission specs.

## Running

```bash
curl -fsSL -o proofs/tla2tools.jar \
  https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar
./proofs/check.sh                                   # everything CI runs
python3 proofs/check_mutants.py --only TerminalOrder  # one spec's manifest
python3 proofs/guard_sweep.py TerminalOrder           # sweep its guards again
python3 proofs/guard_sweep.py --verify TerminalOrder  # check its guard table
```

`TLC_WORKERS=4` lowers the worker count on a machine that is doing other
work.

It needs a JVM (17 or later) and Python 3.11 or later.

## What every spec has

- **A `.cfg` that names each invariant and property, one by one.** A spec
  with no `.cfg`, or a `.cfg` with no claim, fails.
- **Small bounds, stated, with what each leaves out.** TLC visits every
  reachable state of that instance. That is exhaustive for the instance and
  is not a proof for every size.
- **No `CONSTRAINT`.** The constants are the whole bound. A spec with a guard
  table is checked as its text says: its `.cfg` names a specification,
  constants and claims, and nothing else, and replaces none of the spec's
  own definitions.
- **`TypeOK` first** among the invariants, so that the others are evaluated
  only on states of the right shape.
- **Liveness where the design promises progress,** with the fairness it
  assumes written beside it.
- **Its assumptions listed in its header,** each with a mutant that widens
  it.

## The manifest

`manifest.toml` has an entry for every spec. `check_mutants.py` checks it.

**Mutants.** A passing spec says nothing about a guard until removing the
guard fails, and fails for the stated reason. Each mutant is one replacement
in the spec's text with the one invariant (`violates`) or property
(`violates_property`) it must break. The mutated spec is checked against that
claim alone, and the mutant passes only when TLC reports it violated. A
parse error, an evaluation error, a timeout or no error fails it.

- Every invariant and property in a `.cfg` needs a mutant that breaks it. A
  claim nothing can break is not yet shown to say anything.
- `old` must occur exactly once in the spec.
- Mutants run with one worker, so the same error is met first on every run.
- After adding a mutant, read its shortest counterexample (`-workers 1`) and
  confirm it fails for the reason you named.

**Survivors.** A guard whose removal breaks nothing is either unnecessary or
not modeled. The spec's header says which, and the manifest lists it as a
survivor: it is checked against the whole `.cfg` and must find no error.

**Variants.** `<Spec>.<variant>.cfg` is another instance of the same model,
for a hazard the main instance's constants cannot reach. A mutant or survivor
runs against it with `cfg = "<variant>"`. A variant must check something,
needs a mutant of its own, and may check only claims the main configuration
checks or one of its own mutants breaks.

**State.** Each entry is tied to the files it names:

- `planned`: the code is not written. The entry names where it will go, and
  the check fails once that path exists, so the change that creates the code
  must also name the spec's code and tests.
- `implemented`: every `code` and `tests` path must exist.
- `orphaned`: the code was deleted. The entry names the spec that replaces
  it, and the check fails once that spec is here.

That check is of existence. Whether a test follows its spec is for review.

## The guard table

Mutants cover the guards someone thought to try. `<Spec>.guards.toml` covers
all of them: every condition of every action, and what breaks when it alone
is removed.

- **A guard is a conjunct of an action that reads no next state.** They are
  found in SANY's parse of the spec, which is TLC's own front end: it gives
  every expression its level (constant, state, action or temporal) and its
  place in the text. `check_mutants.guards` follows the relation in the
  formula the `.cfg` checks, through every action it is built from: calls,
  disjunctions, quantifiers, the branches of an `IF` and the body of a `LET`.
  A guard is found wherever it stands and however its line is written, and is
  removed at exactly its place.
- What has no row: the condition of an `IF` between two actions, and
  anything inside the value an effect assigns. Give the ones that matter a
  mutant.
- What stops the listing, with an error that says where:
  - a quantifier over a set that depends on the state. `\E h \in Open :
    End(h)` says that `h` is open, in a place nothing can be removed from.
    Write `\E h \in HoldIds : End(h)` and give `End` the guard
    `holds[h].open`;
  - an `IF` with a branch that is no action: `IF c THEN x' = e ELSE FALSE`
    is the guard `c`. Write it as a conjunct;
  - a definition that is given an action or something primed, an
    implication or a `CASE` in an action, a tab in the code, and a module
    that is not one of TLA+'s own.
- `guard_sweep.py <Spec>` writes the table. A row says what the guard breaks:
  - a claim: the invariant a search with one worker meets first or, if no
    invariant breaks, the first property in the configuration's order that
    does, each checked alone;
  - `TypeOK`, only when a search past it (with `TypeOK` as a constraint)
    finishes and finds no other invariant broken;
  - `nothing`;
  - `evaluation`, when the spec, or its other invariants past `TypeOK`,
    can no longer be evaluated.
- A run decides something only if TLC found no error, named a violated
  claim, or said, in one of the ways it has, that an expression has no
  value. A timeout, a parse error, out of memory, a stack overflow, one of
  TLC's own limits or an error not on that list stops a sweep and fails a
  verification.
- A guard that breaks no claim, or none but `TypeOK`, carries a `why`,
  written by hand and kept across sweeps: an enabling condition, a bound of
  the model, a fact about the environment, an order in time, or a guard the
  claims do not rest on.
- `check_mutants.py` checks on every run that the table lists exactly the
  spec's guards and was swept against the spec and configurations as they
  are. It does not repeat the sweep.
- `guard_sweep.py --verify` runs every row again. The `Proofs guard tables`
  workflow does that when `proofs/` changes.
- An entry with no table says why: `unswept = "..."`.

When a row says `nothing`, first ask whether a claim is missing. Three of
`TerminalOrder`'s did break something once two claims said what the design
meant: the cutoff on reaps, releases and adoptions was held up by nothing
until the claim about acknowledged settles covered every terminal the owner
may answer with. When two guards of one action both say `nothing`, remove
them together before calling either idle: `LeaseLifecycle`'s final
checkpoint asked for the process to hold the lease and for the lease's
epoch, either of which keeps a later process from closing a lease over
another's holds.

## Ways a check proves nothing

These come from the header of `RegionalQuotaLease`, a spec retired with the
pilot it modeled. Each cost a review round there.

- **An adversary that does nothing.** A stale-writer action written as
  `UNCHANGED vars` is trivially safe. The guard it was meant to test could be
  deleted and TLC would still pass. An adversary has to write something.
- **A guard on an existentially chosen value.** With
  `\E t \in Tokens : Reserve(l, h, t)`, the guard `t = leaseToken[l]`
  restricts nothing: TLC picks the `t` that satisfies it. Deleting such a
  guard can never produce a violation, in any spec. "Some plane presents some
  token" is not the hazard. A specific plane presenting the token it was
  handed, after that token was superseded, is. Make the value state.
- **Two guards on one hole.** Removing either alone found nothing, and that
  was read as "both are redundant". One of them was a pair, and removing half
  of the pair with the other guard gone was a double charge in six states.
  When a deletion finds nothing, delete it together with each of the others.
- **Equal state counts.** For a guard deletion, a mutant that reaches exactly
  as many distinct states as the original reaches the same states: dropping a
  conjunct only widens a guard, and two nested finite sets of equal size are
  equal. For any other edit, such as deleting an assignment, the sets are not
  nested and equal counts say nothing.
- **A table that looks exhaustive.** A list of deletion experiments covers
  the guards it lists. Say which guards have none.
- **A trace length from a parallel search.** `-workers auto` reports the
  first counterexample any worker returns, not a shortest one. Quote lengths
  from `-workers 1`.
- **"Covered by the property tests."** The header said so of overflow. The
  one property test capped amounts twelve orders of magnitude below the
  boundary. Check what a test covers before citing it.
- **Editing the claim instead of the spec.** A mutation is an edit to the
  spec. An experiment that edits an invariant or the `.cfg` tests something
  else.

And from the fast-admission specs:

- **Which error a parallel search reports first.** A mutant that breaks an
  invariant and also leaves an expression undefined is a kill on one run and
  an error on the next. Decide what broke with one worker.
- **A property reported ahead of an invariant.** TLC checks properties every
  so often by the clock, so on a slow machine a liveness violation can be
  reported before a safety one. Check the invariants alone first.
- **`TypeOK` as the reason.** It is listed first and its bounds are shallow,
  so it notices most things. "Breaks `TypeOK`" often means only that the
  model left its declared shape. Look past it.
- **A sentence nobody ran.** A design sentence that two reviewers accepted
  ("a hold is open until a terminal for it is applied") failed in seven
  steps when it was written as an invariant. Write the invariant from the
  sentence, verbatim, before the sentence is reviewed.
- **A proof with a premise the design does not state.** A reviewer's proof
  held "for every prefix of the owner's records the log can hold". Nothing
  said the log holds a prefix. Ask where each premise is written.
- **An error that is not a verdict.** "Java ran out of memory" was read as an
  expression left undefined, and "Temporal properties P and Q were violated"
  as an undecided run. List what counts as a decided run, and treat every
  other output as no answer.
- **A rewrite that keeps a mutant's name and loses its hazard.**
  `LeaseLifecycle`'s hand-off was rewritten to list its holds outright. The
  mutant that let a partial hand-off count kept its name and began to
  survive, because a partial list had become a complete one. Run every
  mutant after every change to the model, not only the ones that look
  affected.
- **A measured run copied from another header.** Two `.cfg` files said "TLC
  2.19 on an 8-core Apple M2", which was true of neither run. State the jar,
  the machine and the counts of the run you made.
- **A reader of text where there is a parser.** The first guard finder read
  the spec's text. Seven review rounds each found a valid shape it passed
  over (a wrapper, a `LOCAL` definition, a guard on an effect's line), and
  each fix was another rule about how a spec may be written. SANY's parse
  needed none of them, and on its first run found two guards under a
  quantifier that the text reader had no row for.
- **A jar that changes under you.** `v1.8.0` is rebuilt upstream, so the same
  URL gives different bytes on different days, and CI keeps whichever it
  cached first. Both tools print the jar's sha256; compare it before
  concluding that two machines disagree about a spec.
