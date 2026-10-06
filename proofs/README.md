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
| `LeaseLifecycle` | One lease over time: renewals and their answers, the owner's cutoff, its last record and its draining write, draining and close under clock skew (fast admission §4.2, §4.3, §4.8) | planned |
| `CreditDebt` | Money across leases and credit shards: grants under the trust allowance, a settle above its hold and the shortfall its owner, a front door or the auditor reserves, returns, covering, the debt mark and payments (fast admission §4.2, §4.7) | planned |
| `AuditorCommit` | The auditor's per-lease commit: what a member stores so another can carry on, under redelivery, takeover, a member that stalled, records stored twice or out of order, raises between a load and a commit, reaps, the drain log and the checkpoint audit (fast admission §4.8) | planned |

`docs/design/fast-admission-and-batched-settlement.md` §5.1 has the plan for
the fast-admission specs.

## Running

```bash
./proofs/check.sh                                   # what the `proofs` job runs
python3 proofs/check_mutants.py --only TerminalOrder  # one spec's manifest
python3 proofs/guard_sweep.py TerminalOrder           # sweep its guards again
python3 proofs/guard_sweep.py --verify TerminalOrder  # check its guard table, as the
                                                      # `Proofs guard tables` workflow does
```

`TLC_WORKERS=4` lowers the worker count on a machine that is doing other
work.

It needs a JVM (17 or later) and Python 3.11 or later. The model checker,
`proofs/tla2tools.jar`, is in the repository, pinned by its sha256; the top of
`check.sh` says how to move to another build.

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

`AutoRefillHandoff` and `SurfaceCutover` predate this list. They have no
`TypeOK`, no list of assumptions and no guard table.

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

**Survivors.** A change that the spec's header says breaks nothing is listed
in the manifest as a survivor. It is checked against the whole `.cfg` on
every run and must find no error. That is for a change worth naming, such as
a rule the model shows is not needed. It is not where a guard whose removal
breaks nothing is recorded. Every guard has a row in the guard table (below).
A row that says `nothing` carries its reason there, and
`guard_sweep.py --verify` checks it, not the manifest run.

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

  A row names the variant that shows it (`cfg`) when the main configuration
  does not, for an expression left with no value as for a claim.
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
may answer with. When several guards say `nothing`, remove them together
before calling any of them idle: two guards on one hole each look idle
alone (below). `LeaseLifecycle`'s 27 broke a claim when all were removed at
once, and four groups of two or three explained it, each a second defense
behind the first. For example, `OwnerStop` and `FinalCheckpoint` each ask
that the process hold the lease. Either is enough: without both, a later
process stops and publishes the lease's last record, listing none of its
predecessor's holds. Each member's reason names its group, and one group is
a mutant too. Leaving out `Admit`'s, `OwnerStop`'s and `FinalCheckpoint`'s
`has`, which every group needs one of, the other 24 removed all together
break nothing. `CreditDebt`'s 37 broke a claim the same way, and ten groups
of two explained it. For example, `Store` asks that the lease is not sealed
and `OwnerSettle` that it is open: without both, a settle decided after the
boundary is stored past it (`HoldsCoveredInSpanner`). `Close` asks that
every hold has a booked terminal and `AuditorApplyRow` that the lease is
live: without both, a lease closes with a row left and the auditor then
books it (`ShardIdentity`). Leaving out nine of the groups' members, at
least one of each group, the other 28 removed all together break nothing.
`AuditorCommit`'s 53 broke `DrainingLeaseCloses`, and one pair explained it:
`LoadWinners` and `Reread` each ask that the member has loaded the lease.
Without both, a member that has not loaded can load the winners and re-read,
again and again, and never load the lease. Leaving out one of them, the
other 52 removed all together break nothing. That covers every claim in
`two`, `lying` and `ahead`; in `again` and the main configuration, at 102
and 153 million states, the liveness claim was not checked.

## Ways a check proves nothing

These come from the header of `RegionalQuotaLease`, a spec retired with the
pilot it modeled. Each cost a review round there.

- **An adversary that does nothing.** A stale-writer action written as
  `UNCHANGED vars` is trivially safe. The guard it was meant to test could be
  deleted and TLC would still pass. An adversary has to write something.
- **A guard on an existentially chosen value.** That spec had
  `\E t \in Tokens : Reserve(l, h, t)` with the guard `t = leaseToken[l]`,
  and `Reserve` used `t` for nothing but that comparison. The guard
  restricted nothing: TLC picks the `t` that satisfies it, so deleting it
  found no violation. (Where the rest of an action reads the chosen value,
  such a guard does restrict it.) "Some plane presents some token" is not the
  hazard. A specific plane presenting the token it was handed, after that
  token was superseded, is. Make the value state.
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
- **A measured run copied from another header.** `TerminalOrder.cfg` and
  `LeaseLifecycle.cfg` each said "TLC 2.19 on an 8-core Apple M2", copied
  from the two older specs' headers and true of neither new run. State the
  jar, the machine and the counts of the run you made.
- **One step where the system has two.** `LeaseLifecycle` first made a
  renewal and its answer one step, and the owner's last record and its
  draining write one step. An answer that arrives after the owner has let
  the lease go could then not be written down, and with it the rule the
  code needs most: an answer never brings a lease back. Where an action is
  a message and its reply, or a publish and a write, ask what can happen
  between the two.
- **A bound where the sentence says "does not move".** "A revoked lease's
  expiry is at most a window after its revocation" passed a renewal in the
  moment of the revocation. The design's sentence was about steps: the
  expiry does not move. Write that as a property of steps.
- **No new state is not no new step.** A removal that reaches no new state
  can still break a property of steps, or liveness: the states are the same
  and a step between two of them is new. "Reaches no new state" answers for
  the invariants only.
- **A time where only an age is asked.** Recording when a pause or a
  revocation happened multiplied the states by every moment it could
  happen. The model only ever asked how long ago. An age, counted no
  further than anything compares it with, gave the same behaviors in a
  sixth of the states.
- **A rule only the code can keep.** No condition of Spanner's stops a
  process from using a lease it was not granted. The first version of
  `LeaseLifecycle` made that look checked, because a renewal's epoch
  condition happened to be what stopped it in the model. State such a rule
  as an assumption, with mutants that widen it, and say that the code keeps
  it.
- **A reader of text where there is a parser.** The first guard finder read
  the spec's text. Seven review rounds each found a valid shape it passed
  over (a wrapper, a `LOCAL` definition, a guard on an effect's line), and
  each fix was another rule about how a spec may be written. SANY's parse
  needed none of them, and on its first run found two guards under a
  quantifier that the text reader had no row for.
- **A jar that changed under us.** `v1.8.0` is rebuilt upstream, so the same
  URL gave different bytes on different days, and CI ran whichever build it
  had cached first. The jar is now in the repository, pinned by its sha256,
  which `check.sh` and the guard-table workflow check before anything runs
  (#1541). Both tools print the digest; compare it before concluding that
  two machines disagree about a spec.
