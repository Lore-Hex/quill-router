#!/usr/bin/env python3
"""Account for every guard of every action in a spec.

A guard is a conjunct of an action that reads no next state: what has to hold
for the action to happen. check_mutants.guards finds them in SANY's parse of
the spec, wherever in an action they stand. This script removes each one in
turn, runs TLC, and records in <Spec>.guards.toml what removing it breaks:

  breaks = "<a claim>"   the invariant a search with one worker meets first:
                         on the first state that breaks any, the first in
                         the configuration's order. If no invariant breaks,
                         the first property in that order that does, each
                         checked alone. `cfg` names the variant configuration
                         if the main one does not show it. It is TypeOK only
                         when a search of every state of the model's shape,
                         and the first outside it, finishes with no other
                         invariant broken.
  breaks = "nothing"     every configuration of the spec still passes. Then
                         `reaches` says whether the guard was doing anything:
                         "no new state" if every configuration reaches as
                         many distinct states as the table's [states] says
                         it does with every guard in place, "new states" if
                         one reaches more and no claim minds. Counting is
                         enough because removing a guard only adds steps,
                         where the specification uses each action of its
                         relation only as a step, and not under ENABLED, in
                         an IF's condition, in a value or in the initial
                         condition; a spec that does is refused
  breaks = "evaluation"  the spec no longer evaluates, or its other
                         invariants do not on the first state outside
                         TypeOK: the guard kept some expression defined

A guard that breaks no claim, or none but TypeOK, needs a `why`, written by
hand in the table and kept across sweeps: an enabling condition, a bound of
the model, a fact about the environment, an order in time, or a guard the
claims do not rest on, each said plainly.

check_mutants.py checks on every run that the table lists exactly the spec's
guards, in order, and was swept against the spec and configurations as they
are now. It does not repeat the sweep, which takes minutes. `--verify` does:
it runs every row again and fails on any that no longer holds, and checks the
[states] of each table once, in one of its parts. CI runs that when proofs/
changes.

Run: proofs/guard_sweep.py SPEC             sweep, and rewrite the table
     proofs/guard_sweep.py SPEC --action A  sweep one action's guards again
     proofs/guard_sweep.py SPEC --list      only list the guards
     proofs/guard_sweep.py --verify [SPEC ...] [--shard I/N]
     proofs/guard_sweep.py --self-test      test this script on a small model
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import tomllib
from pathlib import Path

import check_mutants as cm

PROOFS = Path(__file__).resolve().parent


# Whether a model has an error does not depend on how many workers search it.
# Which error is met first does: a guard whose removal breaks a claim and also
# leaves an expression undefined is reported either way by a parallel search.
# One worker's breadth-first search meets the same error first every time, so
# every run that decides WHAT a guard breaks uses one worker. It also checks
# the invariants before the properties: TLC checks properties every so often
# by the clock, so which of the two kinds it reports first depends on how fast
# the machine is.
ONE = "1"


class Inconclusive(Exception):
    """A TLC run decided nothing: it timed out, or could not start."""


def run(spec_text: str, cfg_text: str, name: str, workers: str | None = None) -> str:
    """TLC's output, or Inconclusive: a run that decided nothing is no answer at all."""

    output = cm.run_tlc(spec_text, cfg_text, name, workers)
    why = cm.inconclusive(output)
    if why is not None:
        raise Inconclusive(f"{name}: {why}")
    return output


def distinct_states(output: str) -> int:
    """How many distinct states a run that found no error reached."""

    said = re.search(r"^\d+ states generated, (\d+) distinct states found, 0 states left on queue\.$", output, re.MULTILINE)
    if said is None:
        raise Inconclusive("a run that found no error did not say how many states it reached")
    return int(said.group(1))


def reached(states: dict[str, int], base: dict[str, int]) -> str:
    """Whether a spec with a guard removed, which broke nothing, reaches a state the spec does not.

    Removing a guard adds steps and takes none away, where the specification
    uses each action of its relation only as a step (base_states checks that). Every state the
    spec reaches is then still reached, and the same number of distinct
    states in each configuration is the same states.
    """

    return cm.NO_NEW_STATE if states == base else cm.NEW_STATES


def outcome(name: str, spec_text: str, configs: dict[str, str]) -> tuple[str, str, dict[str, int]]:
    """(what the spec breaks, the variant that shows it, the states each configuration reaches if nothing breaks)."""

    states = {}
    for variant in sorted(configs):
        output = run(spec_text, configs[variant], name)
        if cm.verdict(output, invariant=None, prop=None)[0] == cm.SURVIVED:
            states[variant] = distinct_states(output)
            continue
        cfg_text = configs[variant]
        by_kind = cm.claims_in_order(cfg_text)
        safety_cfg = only_claims(cfg_text, by_kind["invariant"], [])
        # A search that finds nothing finds nothing with any number of
        # workers, so one worker is spent only where there is an error to name.
        if cm.verdict(run(spec_text, safety_cfg, name), invariant=None, prop=None)[0] == cm.SURVIVED:
            claim = None
        else:
            safety = run(spec_text, safety_cfg, name, workers=ONE)
            claim = cm.violated_claim(safety)
            if claim is None and not cm.failed_to_evaluate(safety):
                raise Inconclusive(f"{name}: two runs of one model disagreed")
            if claim is None:
                return cm.BREAKS_EVALUATION, variant, {}
        if claim == cm.TYPE_INVARIANT:
            # The type invariant is the first to notice most things. Look past
            # it for another invariant the guard holds up: on states of the
            # model's shape, and on the first state outside it. The type
            # invariant becomes a constraint, so a state that breaks it is
            # still checked and is not searched from. Without that, a guard
            # that is one of the model's bounds would leave an endless search.
            rest = only_claims(cfg_text, [each for each in by_kind["invariant"] if each != claim], [])
            rest += f"CONSTRAINT {claim}\n"
            past = run(spec_text, rest, name, workers=ONE)
            other = cm.violated_claim(past)
            if other is not None:
                return other, variant, {}
            if cm.failed_to_evaluate(past):
                # Nothing was learned about the other invariants, and a row
                # that said TypeOK would say they hold.
                return cm.BREAKS_EVALUATION, variant, {}
        if claim is not None:
            return claim, variant, {}
        # No invariant breaks and nothing fails to evaluate, so what is left
        # is a property. Each is checked alone, in the configuration's order:
        # checked together, TLC names every property that the one
        # counterexample it found violates, and which that is depends on the
        # counterexample.
        for each in by_kind["property"]:
            alone = run(spec_text, only_claims(cfg_text, [], [each]), name)
            result = cm.verdict(alone, invariant=None, prop=each, spec_text=spec_text)[0]
            if result == cm.KILLED:
                return each, variant, {}
            if result != cm.SURVIVED:
                raise Inconclusive(f"{name}: checked alone, {each} gives an error that is not its violation")
        raise Inconclusive(f"{name}: its whole configuration fails, and no run of a part of it says why")
    return cm.BREAKS_NOTHING, "", states


def swept(name: str, spec_text: str, configs: dict[str, str], base: dict[str, int]) -> tuple[str, str, str]:
    """A table row for a spec with a guard removed: (breaks, cfg, reaches)."""

    breaks, variant, states = outcome(name, spec_text, configs)
    return breaks, variant, reached(states, base) if breaks == cm.BREAKS_NOTHING else ""


def base_states(name: str, spec_text: str, configs: dict[str, str]) -> dict[str, int]:
    """The states each configuration reaches with every guard in place, which a guard's removal is measured against."""

    used = cm.action_used_otherwise(spec_text, cm.specification_of(configs))
    if used is not None:
        raise Inconclusive(f"{name}'s specification uses {used}, an action of its next-state relation, other "
                           "than as a step: removing a guard could take a state away as well as add one, and a "
                           "count of states would not say whether it reaches a new one")
    breaks, _, states = outcome(name, spec_text, configs)
    if breaks != cm.BREAKS_NOTHING:
        raise Inconclusive(f"{name} does not pass as it stands")
    return states


def only_claims(cfg_text: str, invariants: list[str], properties: list[str]) -> str:
    """The configuration with those claims and no others, and every other section as written.

    The claims keep the order the configuration gave them. TLC evaluates
    invariants in that order, and a spec lists its type invariant first so
    that the others are evaluated only on states of the right shape.
    """

    kept, _ = cm.split_config(cfg_text)
    for keyword, names in (("INVARIANTS", invariants), ("PROPERTIES", properties)):
        if names:
            kept += f"{keyword} {' '.join(names)}\n"
    return kept


def holds(name: str, spec_text: str, configs: dict[str, str], row: dict, base: dict[str, int]) -> tuple[bool, str]:
    """Whether a table row is still true of the spec, and what was seen if not.

    A row is true when sweeping its guard again gives the same row: the same
    claim, shown by the same configuration, and for a guard that breaks
    nothing the same answer about the states it reaches. Anything less would
    accept a row that names TypeOK for a guard that also holds up a property,
    or one that was never decided because TLC ran out of time.
    """

    try:
        seen = swept(name, spec_text, configs, base)
    except Inconclusive as undecided:
        return False, str(undecided)
    wanted = (row["breaks"], row.get("cfg", ""), row.get("reaches", ""))
    if seen == wanted:
        return True, ""
    return False, f"it breaks {seen[0]}" + (f" in {seen[1]}" if seen[1] else "") + (f" and reaches {seen[2]}" if seen[2] else "")


def stored_states(name: str, configs: dict[str, str], table: dict) -> dict[str, int]:
    """The states a table says each configuration reaches with every guard in place, by variant."""

    return {variant: table["states"][cm.config_file(name, variant)] for variant in configs}


def write_table(path: Path, digest: str, states: dict[str, int], rows: list[dict]) -> None:
    name = path.name[: -len(".guards.toml")]
    lines = [
        "# Every guard of every action, and what breaks when it alone is removed.",
        "# Written by proofs/guard_sweep.py. Each `why` is written by hand and kept.",
        "",
        f'inputs_sha256 = "{digest}"',
        "",
        "# The distinct states each configuration reaches with every guard in place.",
        "[states]",
        *(f'"{cm.config_file(name, variant)}" = {states[variant]}' for variant in sorted(states)),
    ]
    for row in rows:
        lines += ["", "[[guard]]", f'action = "{row["action"]}"', f"text = '''{row['text']}'''",
                  f'breaks = "{row["breaks"]}"']
        if row.get("cfg"):
            lines.append(f'cfg = "{row["cfg"]}"')
        if row.get("reaches"):
            lines.append(f'reaches = "{row["reaches"]}"')
        if row.get("why"):
            lines += ["why = '''", row["why"].strip("\n"), "'''"]
    path.write_text("\n".join(lines) + "\n")


def sweep(name: str, actions: list[str], root: Path = PROOFS) -> int:
    spec_text = (root / f"{name}.tla").read_text()
    configs, problems = cm.load_configs(name, root)
    if problems:
        print(f"error: {name}: {problems[0]}", file=sys.stderr)
        return 1
    try:
        base = base_states(name, spec_text, configs)
    except Inconclusive as undecided:
        print(f"error: {undecided}", file=sys.stderr)
        return 1
    target = root / f"{name}.guards.toml"
    reasons, before = {}, {}
    digest = cm.inputs_digest(spec_text, configs)
    if target.exists():
        table = tomllib.loads(target.read_text())
        for row in table.get("guard", []):
            reasons[(row["action"], row["text"])] = row.get("why", "")
            before[(row["action"], row["text"])] = (row["breaks"], row.get("cfg", ""), row.get("reaches", ""))
        if actions and table.get("inputs_sha256") != digest:
            print(f"error: {name}: its table is of another spec, so every guard is swept again: "
                  "leave --action out", file=sys.stderr)
            return 1
    formula = cm.specification_of(configs)
    found = cm.guards(spec_text, formula)
    unknown = sorted(set(actions) - {guard.action for guard in found})
    if unknown or (actions and len(before) != len(found)):
        print(f"error: {name}: no such action, or no whole table to keep rows from: {unknown}", file=sys.stderr)
        return 1
    print(f"{name}: {len(found)} guards")
    rows = []
    for guard in found:
        if actions and guard.action not in actions:
            # Kept from a sweep of the same spec and configurations.
            breaks, variant, reaches = before[(guard.action, guard.text)]
        else:
            try:
                breaks, variant, reaches = swept(name, cm.without_guard(spec_text, guard, formula), configs, base)
            except Inconclusive as undecided:
                print(f"error: {undecided}", file=sys.stderr)
                return 1
        # A reason is for a guard that breaks no claim. One left on a guard
        # that now breaks a claim would explain something no longer true.
        explained = breaks in (cm.BREAKS_NOTHING, cm.BREAKS_EVALUATION, cm.TYPE_INVARIANT)
        rows.append({"action": guard.action, "text": guard.text, "breaks": breaks, "cfg": variant, "reaches": reaches,
                     "why": reasons.get((guard.action, guard.text), "") if explained else ""})
        where = f" ({variant})" if variant else ""
        print(f"  {guard.action:22.22s} {guard.text:58.58s} {breaks}{where}{', ' + reaches if reaches else ''}", flush=True)
    # Written once, at the end: a sweep that is interrupted leaves the old
    # table, and the reasons in it, as they were.
    write_table(target, digest, base, rows)
    unexplained = [
        row for row in rows
        if row["breaks"] in (cm.BREAKS_NOTHING, cm.BREAKS_EVALUATION, cm.TYPE_INVARIANT) and not row["why"]
    ]
    if unexplained:
        print(f"{len(unexplained)} guards break no claim but {cm.TYPE_INVARIANT} and have no `why`: "
              f"write one for each in {target.name}")
    return 0


def verify(names: list[str], shard: tuple[int, int], root: Path = PROOFS) -> int:
    manifest = tomllib.loads((root / "manifest.toml").read_text())
    specs: list[tuple[str, str, dict[str, str], dict[str, int]]] = []
    work = []
    # Every table there is, not every entry the manifest has: a table for a
    # spec the manifest does not name is an error here too.
    for name in sorted(names or (path.name[: -len(".guards.toml")] for path in root.glob("*.guards.toml"))):
        table = root / f"{name}.guards.toml"
        if not table.exists() or name not in manifest or not (root / f"{name}.tla").exists():
            print(f"error: {name} needs a guard table, a spec and a manifest entry", file=sys.stderr)
            return 1
        spec_text = (root / f"{name}.tla").read_text()
        configs, problems = cm.load_configs(name, root)
        problems += cm.guard_problems(name, manifest[name], spec_text, configs, table.read_text())
        if problems:
            print(f"error: {name}: {problems[0]}", file=sys.stderr)
            return 1
        read = tomllib.loads(table.read_text())
        specs.append((name, spec_text, configs, stored_states(name, configs, read)))
        work += [(name, spec_text, configs, guard, row)
                 for guard, row in zip(cm.guards(spec_text, cm.specification_of(configs)), read.get("guard", []),
                                       strict=True)]
    if not work:
        print("error: no guard table to verify", file=sys.stderr)
        return 1
    index, count = shard
    ok = True
    # Each row is measured against the states its table gives. One part
    # checks those for each spec, so that the parts together check them once.
    bases = {name: stored for name, _, _, stored in specs}
    for number, (name, spec_text, configs, stored) in enumerate(specs):
        if number % count != index:
            continue
        try:
            reached_now = base_states(name, spec_text, configs)
        except Inconclusive as undecided:
            print(f"error: {undecided}", file=sys.stderr)
            return 1
        if reached_now == stored:
            print(f"    HOLDS     {name}: with every guard in place it reaches {stored}", flush=True)
        else:
            print(f"    WRONG     {name}: its table says it reaches {stored}, but it reaches {reached_now}",
                  file=sys.stderr, flush=True)
            ok = False
    for position, (name, spec_text, configs, guard, row) in enumerate(work):
        if position % count != index:
            continue
        true, seen = holds(name, cm.without_guard(spec_text, guard, cm.specification_of(configs)), configs, row,
                           bases[name])
        label = f"{name}/{guard.action}: {guard.text}"
        if true:
            print(f"    HOLDS     {label}: breaks {row['breaks']}"
                  + (f", reaches {row['reaches']}" if row.get("reaches") else ""), flush=True)
        else:
            print(f"    WRONG     {label}: said to break {row['breaks']}, but {seen}",
                  file=sys.stderr, flush=True)
            ok = False
    return 0 if ok else 1


# Step's first guard holds up Small, which TypeOK, listed first, reports
# ahead of it. Its second holds up nothing. Its third holds up Low only where
# Never is small, which the variant is for. Pop's guard keeps Tail defined.
# Flag's guard is a bound: without it y grows for ever, and only TypeOK says
# so. Back's guard holds up only the liveness property: without it x can be
# sent back to 0 for ever, which breaks both properties, and the first in
# the configuration's order is the row. Mark's guard is a bound too, but past
# it Listed cannot be evaluated, so nothing says what the other invariants
# would do. Dip's guard holds up only the second property: without it x can
# fall back to 1 for ever, and never to 0. Bump's guard holds up nothing and
# is not idle: without it u is set while x is still 0, a state the model
# does not otherwise reach. Step's second guard is idle: the model reaches
# the same states without it.
_SELF_TEST_SPEC = r"""---- MODULE Tiny ----
EXTENDS Naturals, Sequences
CONSTANT Never
VARIABLES x, q, y, z, u
Init == x = 0 /\ q = << 1 >> /\ y = 0 /\ z = 0 /\ u = 0
Step ==
    /\ x < 2
    /\ x >= 0
    /\ x # Never
    /\ x' = x + 1
    /\ UNCHANGED << q, y, z, u >>
Pop ==
    /\ q # << >>
    /\ q' = Tail(q)
    /\ UNCHANGED << x, y, z, u >>
Flag ==
    /\ y = 0
    /\ y' = y + 1
    /\ UNCHANGED << x, q, z, u >>
Back ==
    /\ x = 99
    /\ x' = 0
    /\ UNCHANGED << q, y, z, u >>
Mark ==
    /\ z = 0
    /\ z' = z + 1
    /\ UNCHANGED << x, q, y, u >>
Dip ==
    /\ x = 98
    /\ x' = 1
    /\ UNCHANGED << q, y, z, u >>
Bump ==
    /\ x > 0
    /\ u' = 1
    /\ UNCHANGED << x, q, y, z >>
Next == Step \/ Pop \/ Flag \/ Back \/ Mark \/ Dip \/ Bump
Spec == Init /\ [][Next]_<< x, q, y, z, u >> /\ WF_<< x, q, y, z, u >>(Step)
TypeOK == x \in 0..2 /\ y \in 0..1 /\ z \in 0..1 /\ u \in 0..1
Small == x <= 2
Low == x <= Never
Listed == << 5, 6 >>[z + 1] > 0
Settles == <>[](x >= 1)
Stays == <>[](x >= 2)
====
"""
_SELF_TEST_CONFIGS = {
    "": "SPECIFICATION Spec\nCONSTANT Never = 5\nINVARIANTS TypeOK Small Low Listed\nPROPERTIES Settles Stays\n",
    # With Never = 1 the model stops at x = 1, so Stays is not true of it.
    "tight": "SPECIFICATION Spec\nCONSTANT Never = 1\nINVARIANTS TypeOK Small Low Listed\nPROPERTY Settles\n",
}
_SELF_TEST_ROWS = [
    {"breaks": "Small"},
    {"breaks": cm.BREAKS_NOTHING, "reaches": cm.NO_NEW_STATE},
    {"breaks": "Low", "cfg": "tight"},
    {"breaks": cm.BREAKS_EVALUATION},
    {"breaks": cm.TYPE_INVARIANT},
    {"breaks": "Settles"},
    {"breaks": cm.BREAKS_EVALUATION},
    {"breaks": "Stays"},
    {"breaks": cm.BREAKS_NOTHING, "reaches": cm.NEW_STATES},
]


def self_test() -> bool:
    """The sweep must say what each guard breaks, and --verify must refuse a false row."""

    ok = True
    found = cm.guards(_SELF_TEST_SPEC, "Spec")
    ok = cm._report("the small model has nine guards", len(found) == 9, str(len(found))) and ok
    base = base_states("Tiny", _SELF_TEST_SPEC, _SELF_TEST_CONFIGS)
    rest = "Rest ==\n    /\\ ~ENABLED Pop\n    /\\ UNCHANGED << x, q, y, z, u >>\n"
    reads_enabled = _SELF_TEST_SPEC.replace("Next == Step", rest + "Next == Rest \\/ Step")
    try:
        base_states("Tiny", reads_enabled, _SELF_TEST_CONFIGS)
        refused = ""
    except Inconclusive as undecided:
        refused = str(undecided)
    ok = cm._report("a relation that asks whether an action is enabled is not measured by counting states",
                    "uses Pop, an action of its next-state relation, other than as a step" in refused,
                    refused or "measured") and ok
    init_reads = _SELF_TEST_SPEC.replace("Spec == Init /\\ ", "Spec == Init /\\ ~ENABLED Bump /\\ ")
    try:
        base_states("Tiny", init_reads, _SELF_TEST_CONFIGS)
        refused = "" if init_reads != _SELF_TEST_SPEC else "the spec was not changed"
    except Inconclusive as undecided:
        refused = str(undecided)
    ok = cm._report("an initial condition that asks whether an action is enabled is not measured by counting states",
                    "uses Bump, an action of its next-state relation, other than as a step" in refused,
                    refused or "measured") and ok
    for guard, row in zip(found, _SELF_TEST_ROWS, strict=False):
        mutated = cm.without_guard(_SELF_TEST_SPEC, guard, "Spec")
        seen = swept("Tiny", mutated, _SELF_TEST_CONFIGS, base)
        wanted = (row["breaks"], row.get("cfg", ""), row.get("reaches", ""))
        ok = cm._report(f"removing {guard.action}'s `{guard.text}` breaks {row['breaks']}"
                        + (f" and reaches {row['reaches']}" if row.get("reaches") else ""),
                        seen == wanted, " ".join(part for part in seen if part)) and ok
        true, why = holds("Tiny", mutated, _SELF_TEST_CONFIGS, row, base)
        ok = cm._report(f"and --verify accepts that row for {guard.action}'s `{guard.text}`",
                        true, why or "holds") and ok
    false_rows = [
        ("a guard said to break nothing that breaks a claim", 0, {"breaks": cm.BREAKS_NOTHING}),
        ("a guard said to break nothing that breaks a claim in a variant only", 2,
         {"breaks": cm.BREAKS_NOTHING}),
        ("a guard said to break a claim that breaks nothing", 1, {"breaks": "Small"}),
        ("a guard said to break a claim in the main configuration that only a variant shows", 2,
         {"breaks": "Low"}),
        ("a guard said to break the wrong claim", 0, {"breaks": "Low"}),
        ("a guard said to break evaluation that breaks nothing", 1, {"breaks": cm.BREAKS_EVALUATION}),
        ("a guard said to break evaluation that breaks a claim", 0, {"breaks": cm.BREAKS_EVALUATION}),
        ("a guard said to break nothing that breaks evaluation", 3, {"breaks": cm.BREAKS_NOTHING}),
        ("a guard said to break nothing that breaks only the type invariant", 4,
         {"breaks": cm.BREAKS_NOTHING}),
        ("a guard said to break nothing that breaks only a property", 5, {"breaks": cm.BREAKS_NOTHING}),
        ("a guard said to break an invariant that breaks only a property", 5, {"breaks": "Small"}),
        # Small does break in the main configuration, so this is refused only
        # for naming a variant that is not there.
        ("a row naming a variant that does not exist", 0, {"breaks": "Small", "cfg": "wide"}),
        ("a guard said to break only TypeOK that also holds up another invariant", 0,
         {"breaks": cm.TYPE_INVARIANT}),
        ("a row naming the main configuration for what only a variant shows, and the reverse", 0,
         {"breaks": "Small", "cfg": "tight"}),
        ("a guard said to break only TypeOK when the other invariants cannot be evaluated past it", 6,
         {"breaks": cm.TYPE_INVARIANT}),
        ("a guard that breaks two properties, said to break the second", 5, {"breaks": "Stays"}),
        ("a guard that breaks the second property, said to break the first", 7, {"breaks": "Settles"}),
        ("a guard that breaks nothing, said to reach no new state, that reaches some", 8,
         {"breaks": cm.BREAKS_NOTHING, "reaches": cm.NO_NEW_STATE}),
        ("a guard that breaks nothing, said to reach new states, that reaches none", 1,
         {"breaks": cm.BREAKS_NOTHING, "reaches": cm.NEW_STATES}),
        ("a guard that breaks nothing and does not say what it reaches", 1, {"breaks": cm.BREAKS_NOTHING}),
    ]
    for label, index, row in false_rows:
        mutated = cm.without_guard(_SELF_TEST_SPEC, found[index], "Spec")
        true, why = holds("Tiny", mutated, _SELF_TEST_CONFIGS, row, base)
        ok = cm._report(f"--verify refuses {label}", not true, why or "accepted") and ok

    # A run that TLC did not finish decides nothing, whatever the row says.
    limit, cm.TLC_TIMEOUT_SECONDS = cm.TLC_TIMEOUT_SECONDS, 0.01  # type: ignore[assignment]
    try:
        for label, index, row in [
            ("a row said to break evaluation", 3, {"breaks": cm.BREAKS_EVALUATION}),
            ("a row said to break nothing", 1, {"breaks": cm.BREAKS_NOTHING, "reaches": cm.NO_NEW_STATE}),
        ]:
            mutated = cm.without_guard(_SELF_TEST_SPEC, found[index], "Spec")
            true, why = holds("Tiny", mutated, _SELF_TEST_CONFIGS, row, base)
            ok = cm._report(f"--verify refuses {label} when TLC ran out of time",
                            not true and "did not finish" in why, why or "accepted") and ok
    finally:
        cm.TLC_TIMEOUT_SECONDS = limit

    # A model has an error or it has none, however many workers search it. If
    # TLC ever says both, nothing was decided. Its own two answers are played
    # back here: the run that fails, twice, and then the run that passes.
    mutated = cm.without_guard(_SELF_TEST_SPEC, found[0], "Spec")
    answers = iter([
        cm.run_tlc(mutated, _SELF_TEST_CONFIGS[""], "Tiny"),
        cm.run_tlc(mutated, _SELF_TEST_CONFIGS[""], "Tiny"),
        cm.run_tlc(_SELF_TEST_SPEC, _SELF_TEST_CONFIGS[""], "Tiny"),
    ])
    real, cm.run_tlc = cm.run_tlc, lambda *_args, **_kwargs: next(answers)
    try:
        true, why = holds("Tiny", mutated, _SELF_TEST_CONFIGS, {"breaks": cm.BREAKS_EVALUATION}, base)
    finally:
        cm.run_tlc = real
    ok = cm._report("--verify refuses every row when two runs of one model disagree",
                    not true and "disagreed" in why, why or "accepted") and ok

    # A property that gives an error of another kind when it is checked alone
    # decided nothing, and the next property in order must not stand in for
    # it. Played back: the whole run fails, the invariants pass, the first
    # property cannot be evaluated, and the second is violated.
    broken = cm.without_guard(_SELF_TEST_SPEC, found[7], "Spec")
    typed = only_claims(_SELF_TEST_CONFIGS[""], ["TypeOK"], [])
    answers = iter([
        cm.run_tlc(broken, _SELF_TEST_CONFIGS[""], "Tiny"),
        cm.run_tlc(_SELF_TEST_SPEC, typed, "Tiny"),
        cm.run_tlc(cm.without_guard(_SELF_TEST_SPEC, found[3], "Spec"), typed, "Tiny"),
        cm.run_tlc(broken, only_claims(_SELF_TEST_CONFIGS[""], [], ["Stays"]), "Tiny"),
    ])
    real, cm.run_tlc = cm.run_tlc, lambda *_args, **_kwargs: next(answers)
    try:
        true, why = holds("Tiny", broken, {"": _SELF_TEST_CONFIGS[""]}, {"breaks": "Stays"}, base)
    finally:
        cm.run_tlc = real
    ok = cm._report("--verify refuses a row when an earlier property gives an error that is not its violation",
                    not true and "not its violation" in why, why or "accepted") and ok

    # End to end, in a directory of its own: sweep, explain, verify.
    with tempfile.TemporaryDirectory(prefix="tla-sweep-") as directory:
        root = Path(directory)
        (root / "Tiny.tla").write_text(_SELF_TEST_SPEC)
        (root / "Tiny.cfg").write_text(_SELF_TEST_CONFIGS[""])
        (root / "Tiny.tight.cfg").write_text(_SELF_TEST_CONFIGS["tight"])
        (root / "manifest.toml").write_text("[Tiny]\n")
        table = root / "Tiny.guards.toml"
        written = sweep("Tiny", [], root) == 0 and table.exists()
        rows = tomllib.loads(table.read_text())["guard"] if written else []
        ok = cm._report(
            "a sweep writes the table the rows above describe",
            [(row["breaks"], row.get("cfg", ""), row.get("reaches", "")) for row in rows]
            == [(row["breaks"], row.get("cfg", ""), row.get("reaches", "")) for row in _SELF_TEST_ROWS],
            ", ".join(row["breaks"] + (" (" + row["reaches"] + ")" if row.get("reaches") else "") for row in rows),
        ) and ok
        ok = cm._report("a table with a reason missing does not verify",
                        verify([], (0, 1), root) == 1, "refused") and ok
        def rewrite(rows: list[dict], states: dict[str, int] | None = None) -> None:
            read = tomllib.loads(table.read_text())
            write_table(table, read["inputs_sha256"], states or stored_states("Tiny", _SELF_TEST_CONFIGS, read), rows)

        ok = cm._report("and gives the states each configuration reaches with every guard in place",
                        stored_states("Tiny", _SELF_TEST_CONFIGS, tomllib.loads(table.read_text())) == base,
                        str(base)) and ok
        for row in rows:
            row["why"] = "written for the self-test only"
        rewrite(rows)
        ok = cm._report("the same table with its reasons verifies", verify([], (0, 1), root) == 0, "holds") and ok
        ok = cm._report("each part of four verifies, and together they cover every row",
                        all(verify([], (part, 4), root) == 0 for part in range(4)), "holds") and ok
        rows[1]["breaks"], rows[1]["reaches"] = "Small", ""
        rewrite(rows)
        ok = cm._report("a table with one false row does not verify", verify([], (0, 1), root) == 1, "refused") and ok
        ok = cm._report("and the part that holds the false row is the one that fails",
                        [verify([], (part, 4), root) for part in range(4)] == [0, 1, 0, 0], "part 1") and ok
        rows[1]["breaks"], rows[1]["reaches"] = cm.BREAKS_NOTHING, cm.NO_NEW_STATE
        rewrite(rows, {**base, "": base[""] + 1})
        ok = cm._report("a table that gives other states than the spec reaches does not verify",
                        verify([], (0, 1), root) == 1, "refused") and ok
        ok = cm._report("and the part that checks the spec's states fails",
                        verify([], (0, 4), root) == 1, "part 0") and ok
        rewrite(rows, base)
        ok = cm._report("the table as swept verifies again", verify([], (0, 1), root) == 0, "holds") and ok
        (root / "manifest.toml").write_text("")
        ok = cm._report("a table for a spec the manifest does not name does not verify",
                        verify([], (0, 1), root) == 1, "refused") and ok
        (root / "manifest.toml").write_text("[Tiny]\n")
        (root / "Tiny.cfg").write_text(_SELF_TEST_CONFIGS[""] + "\\* changed\n")
        ok = cm._report("a table swept against another configuration does not verify",
                        verify([], (0, 1), root) == 1, "refused") and ok
    # Written with `=`: older argparse reads a bare "-1/2" as an option.
    for shard in ("4/4", "x/2", "1", "-1/2"):
        ok = cm._report(f"--shard {shard} is refused", main(["--verify", f"--shard={shard}"]) == 1,
                        "refused") and ok
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="test this script on a small model")
    parser.add_argument("specs", nargs="*")
    parser.add_argument("--list", action="store_true", help="only list the guards")
    parser.add_argument("--action", action="append", default=[],
                        help="sweep only this action's guards, keeping the table's other rows")
    parser.add_argument("--verify", action="store_true", help="check the table against fresh runs")
    parser.add_argument("--shard", default="0/1", help="with --verify: do the I-th of N parts")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    if not cm.jar().is_file():
        print(f"error: {cm.jar()} not found", file=sys.stderr)
        return 1
    print(cm.jar_identity())
    if args.self_test:
        print("=== guard sweep self-test ===")
        return 0 if self_test() else 1
    if args.verify:
        index, _, count = args.shard.partition("/")
        if not (index.isdecimal() and count.isdecimal() and int(index) < int(count)):
            print(f"error: --shard {args.shard}: want I/N with I below N", file=sys.stderr)
            return 1
        return verify(args.specs, (int(index), int(count)))
    if len(args.specs) != 1:
        print("error: name one spec to sweep", file=sys.stderr)
        return 1
    if args.list:
        configs, problems = cm.load_configs(args.specs[0])
        if problems:
            print(f"error: {args.specs[0]}: {problems[0]}", file=sys.stderr)
            return 1
        found = cm.guards((PROOFS / f"{args.specs[0]}.tla").read_text(), cm.specification_of(configs))
        for guard in found:
            print(f"{guard.action}: {guard.text}")
        print(f"{len(found)} guards")
        return 0
    return sweep(args.specs[0], args.action)


if __name__ == "__main__":
    raise SystemExit(main())
