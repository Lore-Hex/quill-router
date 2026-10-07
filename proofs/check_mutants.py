#!/usr/bin/env python3
"""Check proofs/manifest.toml: every spec's mutants and what implements it.

A spec that passes TLC says nothing about a guard until removing that guard
makes TLC fail, and fail for the stated reason. Each mutant in the manifest is
one textual replacement in its spec, with the one invariant (or temporal
property) it must violate. The mutated spec is checked against that invariant
alone, and the mutant passes only when TLC reports it violated. Anything else
fails this script: no error (the guard holds nothing up in the model), a parse
or evaluation error, a timeout, or a replacement whose text does not occur
exactly once.

A survivor is the opposite claim, also checked: a guard whose removal the spec
says breaks nothing. It runs against the spec's whole configuration and must
find no error.

A spec's main configuration is <Spec>.cfg. It may have variants, each named
<Spec>.<variant>.cfg, for a hazard the main instance's constants cannot
reach. A mutant or survivor names a variant with `cfg = "<variant>"`. A
variant must check something, must have a mutant of its own, and may check
only claims that the main configuration checks or that one of its own mutants
breaks.

A spec also has a guard table, <Spec>.guards.toml, written by
proofs/guard_sweep.py: every guard of every action, and what breaks when it
alone is removed. This script checks that the table lists exactly the spec's
guards, was swept against the spec and configurations as they are now, and
gives a reason for each guard whose removal breaks no claim, or none but
TypeOK. It does not repeat the sweep; guard_sweep.py --verify does. An entry
may opt out with `unswept = "<why>"`, which is debt in plain sight.

Each spec also has a state, checked against the files it names:

  planned      its code is not written: none of its `code` paths may exist
  implemented  every `code` and `tests` path must exist
  orphaned     its code was deleted: the entry names the spec that replaces it,
               and fails once that spec is here

That check is existence only. It cannot tell whether a test follows its spec;
it does catch the code or the test being deleted, or written, while the spec
goes on running unchanged.

Run: proofs/check_mutants.py [--self-test] [--only SPEC]
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

PROOFS = Path(__file__).resolve().parent
ROOT = PROOFS.parent
KILLED, SURVIVED, ERROR = "killed", "survived", "error"
NO_ERROR = "Model checking completed. No error has been found."
# A runaway guard for one TLC run, not a target: a mutant can make a model
# unbounded, and an unbounded search must fail rather than hang the job.
TLC_TIMEOUT_SECONDS = 1200
# Every keyword that starts a section of a TLC configuration. A section runs
# to the next keyword, wherever the line breaks fall. The list is the jar's
# own (tlc2.tool.impl.ModelConfig); the self-test compares the two, so a new
# jar with another keyword fails here rather than being misread.
_KEYWORDS = frozenset({
    "SPECIFICATION", "INIT", "NEXT", "INVARIANT", "INVARIANTS", "PROPERTY", "PROPERTIES",
    "CONSTANT", "CONSTANTS", "CONSTRAINT", "CONSTRAINTS", "ACTION_CONSTRAINT",
    "ACTION_CONSTRAINTS", "SYMMETRY", "VIEW", "ALIAS", "POSTCONDITION", "POSTCONDITIONS",
    "CHECK_DEADLOCK", "_PERIODIC", "_RL_REWARD", "_POSSIBLE",
})
# What the guard table may say a removed guard breaks, besides a claim.
BREAKS_NOTHING, BREAKS_EVALUATION = "nothing", "evaluation"
# What a guard that breaks nothing says besides: whether the spec reaches a
# state without it that it does not reach with it.
NO_NEW_STATE, NEW_STATES = "no new state", "new states"
# The claim that states only a model's types and bounds, in every spec here.
# A guard that breaks nothing else keeps the model inside its own shape, which
# is a reason of a different kind from holding up a property, so its row says
# which bound.
TYPE_INVARIANT = "TypeOK"
_CLAIM_KEYWORDS = {"INVARIANT": "invariant", "INVARIANTS": "invariant",
                   "PROPERTY": "property", "PROPERTIES": "property"}
_OPENERS, _CLOSERS = "{[(", "}])"


def jar() -> Path:
    # Resolved now: TLC runs in a temporary directory, where a relative path
    # (which check.sh accepts) would point nowhere.
    return Path(os.environ.get("TLA_TOOLS_JAR", PROOFS / "tla2tools.jar")).resolve()


def jar_identity() -> str:
    """Which jar this is. CI fetches a release that upstream rebuilds, so a log names the jar it ran."""

    return f"tla2tools.jar: sha256 {hashlib.sha256(jar().read_bytes()).hexdigest()}"


def jar_keywords() -> frozenset[str]:
    """The configuration keywords the jar's parser knows, from its class file."""

    with zipfile.ZipFile(jar()) as archive:
        data = archive.read("tlc2/tool/impl/ModelConfig.class")
    # The constant pool: each UTF-8 entry is tag 1, a two-byte length, the bytes.
    count, position, index = int.from_bytes(data[8:10], "big"), 10, 1
    strings = []
    while index < count:
        tag = data[position]
        if tag == 1:
            length = int.from_bytes(data[position + 1: position + 3], "big")
            strings.append(data[position + 3: position + 3 + length].decode("utf-8", "replace"))
            position += 3 + length
        elif tag in (5, 6):
            position, index = position + 9, index + 1
        else:
            position += {3: 5, 4: 5, 7: 3, 8: 3, 16: 3, 19: 3, 20: 3, 9: 5, 10: 5, 11: 5,
                         12: 5, 17: 5, 18: 5, 15: 4}[tag]
        index += 1
    words = {text for text in strings if re.fullmatch(r"_?[A-Z][A-Z_]+", text)}
    # Two values and the name of the field that holds the list.
    return frozenset(words - {"TRUE", "FALSE", "ALL_KEYWORDS"})


def _lex(cfg_text: str) -> tuple[str, list[tuple[str, int, int]]]:
    """The configuration with its comments blanked, and its tokens with their spans.

    A token is a word, a string, or one punctuation mark. Comments are `\\*` to
    the end of the line and `(* ... *)`, which nests. A string runs to its
    closing quote, and a backslash in it escapes the next character.
    """

    clean = list(cfg_text)
    tokens: list[tuple[str, int, int]] = []
    i, n = 0, len(cfg_text)

    def blank(begin: int, finish: int) -> None:
        for k in range(begin, finish):
            if clean[k] != "\n":
                clean[k] = " "

    while i < n:
        char = cfg_text[i]
        if char.isspace():
            i += 1
        elif cfg_text.startswith("\\*", i):
            finish = cfg_text.find("\n", i)
            finish = n if finish < 0 else finish
            blank(i, finish)
            i = finish
        elif cfg_text.startswith("(*", i):
            begin, depth, i = i, 1, i + 2
            while i < n and depth:
                if cfg_text.startswith("(*", i):
                    depth, i = depth + 1, i + 2
                elif cfg_text.startswith("*)", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise SystemExit("error: a configuration has a comment that never closes")
            blank(begin, i)
        elif char == '"':
            finish = i + 1
            while finish < n and cfg_text[finish] != '"':
                finish += 2 if cfg_text[finish] == "\\" else 1
            if finish >= n:
                raise SystemExit("error: a configuration has a string that never closes")
            tokens.append((cfg_text[i: finish + 1], i, finish + 1))
            i = finish + 1
        elif char.isalnum() or char == "_":
            finish = i
            while finish < n and (cfg_text[finish].isalnum() or cfg_text[finish] == "_"):
                finish += 1
            tokens.append((cfg_text[i:finish], i, finish))
            i = finish
        elif cfg_text.startswith("<-", i):
            tokens.append(("<-", i, i + 2))
            i += 2
        else:
            tokens.append((char, i, i + 1))
            i += 1
    return "".join(clean), tokens


def _sections(cfg_text: str) -> list[tuple[str, str, list[str]]]:
    """Each section: its keyword, its text as written, and its tokens.

    A keyword starts a section only where TLC reads it as one: outside any
    bracket, and not as the value after `=` or `<-`, where it is a model value
    that happens to share the name.
    """

    clean, tokens = _lex(cfg_text)
    heads: list[int] = []
    depth = 0
    for index, (text, _, _) in enumerate(tokens):
        if text in _OPENERS:
            depth += 1
        elif text in _CLOSERS:
            depth -= 1
        elif text in _KEYWORDS and depth == 0 and (index == 0 or tokens[index - 1][0] not in ("=", "<-")):
            heads.append(index)
    if tokens and (not heads or heads[0] != 0):
        raise SystemExit("error: a configuration has text before its first keyword")
    sections = []
    for head, following in zip(heads, [*heads[1:], len(tokens)], strict=True):
        body = tokens[head + 1: following]
        written = clean[tokens[head][1]: body[-1][2] if body else tokens[head][2]]
        sections.append((tokens[head][0], written, [text for text, _, _ in body]))
    return sections


def claims_in_order(cfg_text: str) -> dict[str, list[str]]:
    """The invariants and the properties a configuration names, in the order it names them."""

    claims: dict[str, list[str]] = {"invariant": [], "property": []}
    for keyword, _, body in _sections(cfg_text):
        if keyword in _CLAIM_KEYWORDS:
            claims[_CLAIM_KEYWORDS[keyword]] += [name for name in body if name not in claims[_CLAIM_KEYWORDS[keyword]]]
    return claims


def split_config(cfg_text: str) -> tuple[str, dict[str, set[str]]]:
    """(every section that is not a claim, the invariants and the properties named)."""

    kept: list[str] = []
    claims: dict[str, set[str]] = {"invariant": set(), "property": set()}
    for keyword, written, body in _sections(cfg_text):
        if keyword in _CLAIM_KEYWORDS:
            claims[_CLAIM_KEYWORDS[keyword]].update(body)
        else:
            kept.append(written)
    return "\n".join(kept) + "\n", claims


def config_for(cfg_text: str, *, invariant: str | None, prop: str | None) -> str:
    """The spec's configuration with its claims replaced by exactly one.

    Every other section is kept as written: dropping a constraint, a symmetry
    set or a constant would check a different model from the spec's own. The
    result is read back before it is used, and anything but the same other
    sections and the one claim is an error, not a guess.
    """

    kept, _ = split_config(cfg_text)
    if invariant is not None:
        rewritten = kept + f"INVARIANT {invariant}\n"
    else:
        rewritten = kept + f"PROPERTY {prop}\n"
    before = [(keyword, body) for keyword, _, body in _sections(cfg_text) if keyword not in _CLAIM_KEYWORDS]
    after = _sections(rewritten)
    claim = [(keyword, body) for keyword, _, body in after if keyword in _CLAIM_KEYWORDS]
    others = [(keyword, body) for keyword, _, body in after if keyword not in _CLAIM_KEYWORDS]
    if others != before or len(claim) != 1 or claim[0][1] != [invariant or prop]:
        raise SystemExit("error: a configuration could not be rewritten to check one claim")
    return rewritten


def run_tlc(spec_text: str, cfg_text: str, name: str, workers: str | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix="tla-mutant-") as directory:
        work = Path(directory)
        (work / f"{name}.tla").write_text(spec_text)
        (work / f"{name}.cfg").write_text(cfg_text)
        command = [
            shutil.which("java") or "java", "-XX:+UseParallelGC", "-cp", str(jar()),
            "tlc2.TLC", "-deadlock", "-workers", workers or os.environ.get("TLC_WORKERS", "auto"),
            "-metadir", str(work / "states"), "-config", f"{name}.cfg", f"{name}.tla",
        ]
        try:
            result = subprocess.run(  # noqa: S603 - a fixed java command on a temporary copy
                command, cwd=work, capture_output=True, text=True, check=False,
                timeout=TLC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"Error: TLC did not finish in {TLC_TIMEOUT_SECONDS} seconds.\n"
        return result.stdout + result.stderr


def verdict(
    output: str, *, invariant: str | None, prop: str | None, spec_text: str = ""
) -> tuple[str, str]:
    """(KILLED | SURVIVED | ERROR, the line that decided it)."""

    errors = [line for line in output.splitlines() if line.startswith("Error:")]
    if not errors:
        if searched_every_state(output):
            return SURVIVED, "no error found"
        if NO_ERROR in output.splitlines():
            return ERROR, _STOPPED_EARLY
        return ERROR, "TLC printed neither a result nor an error"
    first = errors[0]
    if invariant is not None:
        forms = [rf"Invariant {re.escape(invariant)} is violated( by the initial state)?[.:]"]
    else:
        name = re.escape(prop or "")
        forms = [
            rf"Temporal property {name} was violated\.",
            # TLC's older wording names no property. The configuration holds
            # exactly one, so it can mean no other.
            r"Temporal properties were violated\.",
            rf"Action property {name} is violated\.",
            rf"Property {name} is violated by the initial state:",
        ]
        # A property defined as []Inv is checked as the invariant Inv and
        # reported under that name. Accept that name only when the spec
        # defines the property so.
        inner = re.search(rf"^{name}\s*==\s*\[\]\s*([A-Za-z_]\w*)\s*$", spec_text, re.MULTILINE)
        if inner:
            forms.append(rf"Invariant {re.escape(inner.group(1))} is violated( by the initial state)?[.:]")
    if any(re.fullmatch("Error: " + form, first) for form in forms):
        return KILLED, first
    return ERROR, first


# What TLC says when an expression has no value: the head of an empty
# sequence, an index outside a domain, a field that is not there, a CHOOSE
# with no witness, a value of the wrong kind, an assertion that failed.
# Each is a whole line of TLC's message. A state that TLC prints may hold any
# string, so the words are not looked for inside a line.
_NO_VALUE = tuple(re.compile(line) for line in (
    r"(Error: |: )?Attempted to apply \w+ to the empty sequence\.",
    r"(Error: |: )?Attempted to access index -?\d+ of tuple",
    r"which is out of bounds\.",
    r"which is not in (its|the) domain( of the function)?\.",
    r"to argument .*, which is not in the domain of the function\.",
    r"(Error: |: )?Attempted to select nonexistent field \S+ from the record",
    r"(Error: |: )?Attempted to select field \S+ from a non-record value.*",
    r"CHOOSE x \\in S: P, but no element of S satisfied P\.",
    r"(Error: |: )?Attempted to check equality of .*",
    r"Cannot cast \S+ to \S+",
    r"Error: The first argument of Assert evaluated to FALSE; the second argument was:",
))


def _first_error(output: str) -> str:
    """TLC's first error, with the lines that belong to it."""

    lines = output.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("Error:")]
    if not starts:
        return ""
    return "\n".join(lines[starts[0]: starts[1] if len(starts) > 1 else len(lines)])


def failed_to_evaluate(output: str) -> bool:
    """Whether TLC stopped because an expression of the spec had no value.

    That is read from what its first error says, against a list of the ways
    it says so. An error that is not on the list is not this. That leaves
    out TLC's own limits (a set too large to build, an integer overflow, a
    set it cannot enumerate), which it reports in the same frame as an
    expression with no value, and which a larger limit would make go away.
    """

    return any(form.fullmatch(line) for line in _first_error(output).splitlines() for form in _NO_VALUE)


_EVERY_STATE = re.compile(r"\d+ states generated, \d+ distinct states found, 0 states left on queue\.")
_STOPPED_EARLY = "TLC stopped with states still to visit"


def searched_every_state(output: str) -> bool:
    """TLC found no error, and left no state it had found unvisited.

    A spec can stop a search itself: `TLCSet("exit", TRUE)` does. TLC then
    still says that checking completed and no error was found. Its count of
    the states left on its queue says what that is worth.
    """

    lines = output.splitlines()
    return NO_ERROR in lines and any(_EVERY_STATE.fullmatch(line) for line in lines)


def inconclusive(output: str) -> str | None:
    """Why a TLC run decided nothing, if it decided nothing.

    A run decides something when it finds no error, names a claim as
    violated, or stops on an expression that has no value. Anything else is
    no evidence about the spec: a timeout, a spec that does not parse, a JVM
    out of memory, a set larger than TLC will build, an error this does not
    know, or output with neither a result nor an error.
    """

    lines = output.splitlines()
    errors = [line for line in lines if line.startswith("Error:")]
    if not errors:
        if searched_every_state(output):
            return None
        return _STOPPED_EARLY if NO_ERROR in lines else "TLC printed neither a result nor an error"
    if violated_claim(output) is not None or failed_to_evaluate(output):
        return None
    if re.fullmatch("Error: " + _SEVERAL_VIOLATED, errors[0]):
        return None
    return errors[0]


_VIOLATION_FORMS = (
    r"Invariant (\S+) is violated( by the initial state)?[.:]",
    r"Temporal property (\S+) was violated\.",
    r"Action property (\S+) is violated\.",
    r"Property (\S+) is violated by the initial state:",
)
# One counterexample that violates several properties names them all: "P and
# Q", or "P, Q, and R". TLC's older wording names none. Either way the run
# found a violation, and which property is for a run of each alone to say.
_SEVERAL_VIOLATED = r"Temporal properties( \S+(, \S+)*,? and \S+)? were violated\."


def violated_claim(output: str) -> str | None:
    """The claim TLC's first error names as violated, if it names one."""

    errors = [line for line in output.splitlines() if line.startswith("Error:")]
    for form in _VIOLATION_FORMS if errors else ():
        match = re.fullmatch("Error: " + form, errors[0])
        if match:
            return match.group(1)
    return None


def mutate(spec_text: str, change: dict, label: str) -> str:
    old, new = change["old"], change["new"]
    count = spec_text.count(old)
    if count != 1:
        raise SystemExit(f"error: {label}: the text to replace occurs {count} times, not once")
    if old == new:
        raise SystemExit(f"error: {label}: the replacement changes nothing")
    return spec_text.replace(old, new, 1)


def check_mutant(name: str, spec_text: str, cfg_text: str, mutant: dict) -> bool:
    label = f"{name}/{mutant['name']}"
    invariant, prop = mutant.get("violates"), mutant.get("violates_property")
    if (invariant is None) == (prop is None):
        raise SystemExit(f"error: {label}: name exactly one of violates / violates_property")
    # The claim must be one the spec's own configuration checks, so that the
    # unmutated spec is known to satisfy it. Any other definition, such as a
    # guard that is false half the time, would be "violated" by every mutant.
    _, claims = split_config(cfg_text)
    if invariant is not None and invariant not in claims["invariant"]:
        raise SystemExit(f"error: {label}: {invariant} is not an invariant in {name}.cfg")
    if prop is not None and prop not in claims["property"]:
        raise SystemExit(f"error: {label}: {prop} is not a property in {name}.cfg")
    mutated = mutate(spec_text, mutant, label)
    # One worker: a parallel search reports whichever error a worker meets
    # first, so a mutant that also left an expression undefined would be
    # KILLED on one run and an ERROR on the next. One worker's breadth-first
    # search meets the same error first every time, and it is a shortest one.
    output = run_tlc(mutated, config_for(cfg_text, invariant=invariant, prop=prop), name, workers="1")
    result, why = verdict(output, invariant=invariant, prop=prop, spec_text=mutated)
    wanted = invariant or prop
    if result == KILLED:
        print(f"    KILLED    {label}: {wanted}")
        return True
    if result == SURVIVED:
        print(f"    SURVIVED  {label}: removing this guard does not violate {wanted}", file=sys.stderr)
    else:
        print(f"    ERROR     {label}: wanted {wanted}, got: {why}", file=sys.stderr)
    return False


def check_survivor(name: str, spec_text: str, cfg_text: str, survivor: dict) -> bool:
    label = f"{name}/{survivor['name']}"
    mutated = mutate(spec_text, survivor, label)
    result, why = verdict(run_tlc(mutated, cfg_text, name), invariant=None, prop=None)
    if result == SURVIVED:
        print(f"    SURVIVES  {label}: no error, as the spec says")
        return True
    print(f"    ERROR     {label}: said to break nothing, but: {why}", file=sys.stderr)
    return False


def claim_problems(entry: dict, cfg_text: str) -> list[str]:
    """A .cfg that claims nothing, and each claim in it that no mutant breaks."""

    _, by_kind = split_config(cfg_text)
    claims = by_kind["invariant"] | by_kind["property"]
    if not claims:
        return ["its .cfg names no invariant or property, so it checks nothing"]
    broken = {
        mutant.get("violates") or mutant.get("violates_property")
        for mutant in entry.get("mutants", [])
    }
    return [f"no mutant breaks {claim}" for claim in sorted(claims - broken)]


def config_file(name: str, variant: str) -> str:
    """The file a configuration is read from: <Spec>.cfg, or <Spec>.<variant>.cfg."""

    return f"{name}.{variant}.cfg" if variant else f"{name}.cfg"


def load_configs(name: str, root: Path = PROOFS) -> tuple[dict[str, str], list[str]]:
    """A spec's configurations by variant name ("" is the main one), and what is wrong."""

    configs = {"": (root / f"{name}.cfg").read_text()}
    problems = []
    for path in sorted(root.glob(f"{name}.*.cfg")):
        variant = path.name[len(name) + 1: -len(".cfg")]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", variant):
            # "Spec..cfg" would otherwise stand in for the main configuration.
            problems.append(f"{path.name} is not named <Spec>.<variant>.cfg")
            continue
        configs[variant] = path.read_text()
    return configs, problems


def _claims(cfg_text: str) -> set[str]:
    _, by_kind = split_config(cfg_text)
    return by_kind["invariant"] | by_kind["property"]


def variant_problems(entry: dict, configs: dict[str, str]) -> list[str]:
    """What is wrong with a spec's variant configurations.

    A variant must check something and must have a mutant of its own: a
    survivor alone shows only that it passes. It may check a claim only if
    the main configuration checks it too, where some mutant breaks it, or if
    one of the variant's own mutants breaks it.
    """

    mutants, survivors = entry.get("mutants", []), entry.get("survivors", [])
    by_mutants = {mutant["cfg"] for mutant in mutants if "cfg" in mutant}
    named = by_mutants | {survivor["cfg"] for survivor in survivors if "cfg" in survivor}
    problems = []
    for variant in sorted(configs.keys() - {""}):
        claims = _claims(configs[variant])
        if not claims:
            problems.append(f"the variant {variant} names no invariant or property, so it checks nothing")
        if variant not in by_mutants:
            problems.append(f"no mutant runs against the variant {variant}")
        own = {
            mutant.get("violates") or mutant.get("violates_property")
            for mutant in mutants if mutant.get("cfg") == variant
        }
        problems += [
            f"the variant {variant} checks {claim}, which no mutant shows can fail"
            for claim in sorted(claims - _claims(configs[""]) - own)
        ]
    problems += [f"the variant {variant} has no .cfg" for variant in sorted(named - configs.keys())]
    return problems


@dataclasses.dataclass(frozen=True)
class Guard:
    action: str
    text: str   # the condition on one line, without its comments, after "/\\ "
    first: int  # its first line in the spec, from 0
    last: int   # the line after its last
    start: int  # where it starts on its first line, from 0
    end: int    # the column after its last character, on its last line


def _code_lines(spec_text: str) -> list[str]:
    """The spec's lines with every comment blanked, each character in its place.

    Comments are `\\*` to the end of the line and `(* ... *)`, which nests and
    may run over lines. Neither starts inside a string. A guard's text is
    read from these lines, at the columns SANY gives.
    """

    code = list(spec_text)
    i, n = 0, len(spec_text)
    while i < n:
        if spec_text.startswith("\\*", i):
            finish = spec_text.find("\n", i)
            finish = n if finish < 0 else finish
        elif spec_text.startswith("(*", i):
            depth, finish = 1, i + 2
            while finish < n and depth:
                if spec_text.startswith("(*", finish):
                    depth, finish = depth + 1, finish + 2
                elif spec_text.startswith("*)", finish):
                    depth, finish = depth - 1, finish + 2
                else:
                    finish += 1
        elif spec_text[i] == '"':
            finish = i + 1
            while finish < n and spec_text[finish] not in '"\n':
                finish += 2 if spec_text[finish] == "\\" else 1
            i = min(finish + 1, n)
            continue
        else:
            i += 1
            continue
        for k in range(i, finish):
            if spec_text[k] != "\n":
                code[k] = " "
        i = finish
    return "".join(code).split("\n")


def _squeezed(code: str) -> str:
    """The code with each run of spaces outside a string made one space.

    A string's own spaces are its value, so `x # "a  b"` is not `x # "a b"`.
    A string does not run past its line, so neither does it past a guard's.
    """

    out, i, n = [], 0, len(code)
    while i < n:
        if code[i] == '"':
            finish = i + 1
            while finish < n and code[finish] != '"':
                finish += 2 if code[finish] == "\\" else 1
            out.append(code[i:finish + 1])
            i = finish + 1
        elif code[i].isspace():
            while i < n and code[i].isspace():
                i += 1
            out.append(" ")
        else:
            out.append(code[i])
            i += 1
    return "".join(out).strip()


_WORD = re.compile(r"[A-Za-z_]\w*")
# SANY's kind for a declared VARIABLE, as its export writes it. A CONSTANT is 2.
_VARIABLE_KIND = "3"


@functools.lru_cache(maxsize=16)
def _sany(spec_text: str) -> tuple[str, ET.Element]:
    """The module's name, and SANY's parse of it as its XML exporter writes it.

    SANY is TLC's own front end, so what it calls a definition, an action or
    a condition is what TLC runs. Each node carries its level (constant,
    state, action, temporal) and where it is in the text.
    """

    header = re.search(r"-{4,}\s*MODULE\s+([A-Za-z_]\w*)", spec_text)
    if header is None:
        raise SystemExit("error: the spec has no MODULE line")
    name = header.group(1)
    with tempfile.TemporaryDirectory(prefix="tla-parse-") as directory:
        (Path(directory) / f"{name}.tla").write_text(spec_text, encoding="utf-8")
        try:
            result = subprocess.run(  # noqa: S603 - a fixed java command on a temporary copy
                [shutil.which("java") or "java", "-Dfile.encoding=UTF-8", "-cp", str(jar()),
                 "tla2sany.xml.XMLExporter", "-o", f"{name}.tla"],
                cwd=directory, capture_output=True, text=True, check=False, timeout=TLC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"error: SANY did not finish parsing {name}") from None
    begins = result.stdout.find("<?xml")
    if result.returncode != 0 or begins < 0:
        said = (result.stdout + result.stderr).strip().splitlines()
        raise SystemExit(f"error: SANY could not parse {name}: {said[-1] if said else 'it printed nothing'}")
    return name, ET.fromstring(result.stdout[begins:])  # noqa: S314 - SANY's own output, for a spec of this repository


class _Parse:
    """One spec as SANY read it, and what the guard table needs from that."""

    def __init__(self, spec_text: str) -> None:
        self.name, root = _sany(spec_text)
        self.lines = _code_lines(spec_text)
        # A guard is cut out of the text at the columns SANY gives. A tab,
        # in a comment as much as in code, or a character Java stores as two,
        # would make its count differ from this script's.
        if "\t" in spec_text or any(ord(character) > 0xFFFF for character in spec_text):
            raise SystemExit(f"error: {self.name} has a tab, or a character outside the basic plane: "
                             "SANY's columns and this script's would differ")
        context = root.find("context")
        self.entries = {str(entry.findtext("UID")): entry[1] for entry in (context if context is not None else [])}
        # The parameters of every definition. Inside its definition one is
        # constant level, whatever a caller gives it.
        self.parameters = {str(reference.findtext("UID")) for node in self.entries.values()
                           if node.tag == "UserDefinedOpKind" for reference in node.iterfind("params//FormalParamNodeRef")}
        self.found: list[Guard] = []
        self.nodes: dict[Guard, ET.Element] = {}
        # The variables each action definition may give a value, once read,
        # and the actions being read.
        self.assigns: dict[str, frozenset[str]] = {}
        self.reading: set[str] = set()
        # The calls to actions the walk followed.
        self.followed: set[ET.Element] = set()

    # --- reading a node

    def _defined_here(self, node: ET.Element) -> bool:
        return node.findtext("location/filename") == self.name

    def steady_names(self) -> set[str]:
        """The names of the definitions, of this module or one it extends, that read no state."""

        levels: dict[str, int] = {}
        for node in self.entries.values():
            if node.tag == "UserDefinedOpKind":
                name = str(node.findtext("uniquename"))
                levels[name] = max(levels.get(name, 0), self._level(node))
        return {name for name, level in levels.items() if level == 0}

    def own_names(self) -> set[str]:
        """Every name the module itself defines, at the top or in a LET."""

        return {str(node.findtext("uniquename")) for node in self.entries.values()
                if node.tag == "UserDefinedOpKind" and self._defined_here(node)}

    def _operator(self, node: ET.Element) -> ET.Element:
        reference = node.find("operator")
        uid = reference[0].findtext("UID") if reference is not None and len(reference) else None
        if uid is None or uid not in self.entries:
            raise SystemExit(f"error: {self.name}: an operator that SANY's export does not define, at {self._where(node)}")
        return self.entries[uid]

    @staticmethod
    def _level(node: ET.Element) -> int:
        return int(node.findtext("level") or 0)

    @staticmethod
    def _operands(node: ET.Element) -> list[ET.Element]:
        operands = node.find("operands")
        return list(operands) if operands is not None else []

    def _span(self, node: ET.Element) -> tuple[int, int, int, int]:
        """(first line, first column, line after the last, column after the last), each from 0."""

        try:
            return (int(node.findtext("location/line/begin") or "") - 1,
                    int(node.findtext("location/column/begin") or "") - 1,
                    int(node.findtext("location/line/end") or ""),
                    int(node.findtext("location/column/end") or ""))
        except ValueError:
            raise SystemExit(f"error: {self.name}: SANY gave no place in the text for a {node.tag}") from None

    def _text(self, node: ET.Element) -> str:
        first, start, last, end = self._span(node)
        rows = self.lines[first:last]
        rows[-1] = rows[-1][:end]
        rows[0] = rows[0][start:]
        return _squeezed(" ".join(rows))

    def _where(self, node: ET.Element) -> str:
        return f"line {self._span(node)[0] + 1}"

    def _parameters_under(self, node: ET.Element, seen: set[str]) -> set[str]:
        """The definition parameters an expression depends on, through every definition it names.

        SANY gives `T` in `Step(S) == LET T == S IN \\E k \\in T : ...` the
        level of a constant: the parameter is in T's body, which is not
        under the node that names T. So each definition named is read too.
        Its own parameters are not counted for it: what a use of the
        definition gives them is in the expression, and is counted there.
        """

        found = {str(reference.findtext("UID")) for reference in node.iter("FormalParamNodeRef")} & self.parameters
        for reference in node.iter("UserDefinedOpKindRef"):
            uid = str(reference.findtext("UID"))
            if uid in seen:
                continue
            seen.add(uid)
            definition = self.entries[uid]
            own = {str(parameter.findtext("UID")) for parameter in definition.iterfind("params//FormalParamNodeRef")}
            body = definition.find("body")
            if body is not None:
                found |= self._parameters_under(body, seen) - own
        return found

    def _is_a_variable(self, node: ET.Element) -> bool:
        if node.tag != "OpApplNode":
            return False
        declared = self._operator(node)
        return declared.tag == "OpDeclNode" and declared.findtext("kind") == _VARIABLE_KIND

    def _variables_named(self, node: ET.Element, seen: set[str]) -> frozenset[str] | None:
        """The variables an expression is made of, if it is a variable, a tuple of these, or a definition that is one of these."""

        if self._is_a_variable(node):
            return frozenset({str(self._operator(node).findtext("uniquename"))})
        if node.tag != "OpApplNode":
            return None
        operator, operands = self._operator(node), self._operands(node)
        if operator.findtext("uniquename") == "$Tuple":
            named: frozenset[str] = frozenset()
            for operand in operands:
                more = self._variables_named(operand, seen)
                if more is None:
                    return None
                named |= more
            return named
        uid = str(node.find("operator")[0].findtext("UID"))  # type: ignore[index]
        if operator.tag != "UserDefinedOpKind" or uid in seen or operator.find("body") is None:
            return None
        return self._variables_named(operator.find("body")[0], seen | {uid})  # type: ignore[index]

    # --- what SANY read, without where it stands

    _UNSHAPED = frozenset({"location", "level", "UID", "pre-comments", "leibniz",
                           "originalOperator", "originallyDefinedInModule"})

    def shape(self, marked: ET.Element | None) -> tuple[object, ...]:
        """What SANY read of the module, without where anything stands in the text.

        It is every declaration and definition the module makes at its top,
        in order, each with its parameters and its body, and under them
        every definition a LET makes and every LAMBDA. A name that some
        module defines at its top stands for itself. So does `marked`, if it
        is given: it is the one node in which two parses may differ.
        """

        top = {str(reference.findtext("UID")) for node in self.entries.values() if node.tag == "ModuleNode"
               for reference in node if reference.tag.endswith("Ref")}

        def of(element: ET.Element, inside: tuple[str, ...]) -> object:
            if element is marked:
                return ("the guard",)
            uid = element.findtext("UID") if element.tag.endswith("Ref") else None
            if uid is not None:
                target = self.entries[uid]
                name = str(target.findtext("uniquename"))
                if target.tag != "UserDefinedOpKind" or uid in top:
                    return (target.tag, name)
                if uid in inside:
                    return ("itself", name)
                return ("in a LET", of(target, (*inside, uid)))
            # A leaf's text is its value, a string's spaces included. An
            # element with children has only the export's indentation there.
            text = (element.text or "").strip() if len(element) else element.text or ""
            return (element.tag, text,
                    tuple(of(child, inside) for child in element if child.tag not in self._UNSHAPED))

        module = next(node for node in self.entries.values()
                      if node.tag == "ModuleNode" and node.findtext("uniquename") == self.name)
        return tuple(of(self.entries[str(reference.findtext("UID"))], ())
                     for reference in module if reference.tag.endswith("Ref"))

    def true_at(self, line: int, column: int) -> list[ET.Element]:
        """Each TRUE that stands at that place in the module's text, line and column from 0."""

        return [node for entry in self.entries.values() for node in entry.iter("OpApplNode")
                if self._defined_here(node) and self._span(node)[:2] == (line, column)
                and self._operator(node).findtext("uniquename") == "TRUE"]

    # --- the relation TLC runs
    # --- the relation TLC runs

    def relation(self, formula: str) -> ET.Element:
        """The A of the one `[][A]_v` in the formula the configurations check.

        The formula is followed as TLC reads it: through its conjunction, a
        LET's body, and any definition it is built from. So a box that is
        written and not used is not found, and one that a definition brings
        in is. Beside its box it may hold its initial condition and fairness,
        and nothing else: another always, an eventually or a leads-to would
        restrict the behaviors from outside the actions.
        """

        top = [node for node in self.entries.values()
               if node.tag == "UserDefinedOpKind" and node.findtext("uniquename") == formula
               and self._defined_here(node) and node.find("body") is not None]
        if len(top) != 1:
            raise SystemExit(f"error: the configurations check {formula}, which the spec does not define once")
        boxes = self._boxes(top[0].find("body")[0], formula, set())  # type: ignore[index]
        if len(boxes) != 1:
            raise SystemExit(f"error: {formula} holds {len(boxes)} next-state relations as [][Next]_vars, not one")
        return self._operands(boxes[0])[0]

    def _boxes(self, node: ET.Element, formula: str, seen: set[str]) -> list[ET.Element]:
        if node.tag == "LetInNode":
            return self._boxes(node.find("body")[0], formula, seen)  # type: ignore[index]
        if self._level(node) < 2:
            return []
        if node.tag != "OpApplNode":
            raise SystemExit(f"error: {formula} has a {node.tag} at {self._where(node)} that this cannot read")
        operator, operands = self._operator(node), self._operands(node)
        kind = str(operator.findtext("uniquename"))
        if operator.tag == "UserDefinedOpKind" and self._level(node) == 3:
            if operands:
                # Its body would be read without what it is given: in
                # `Wrap(A) == Init /\\ [][A]_v` the relation is the argument.
                raise SystemExit(f"error: {formula} is built from {kind}, which takes arguments, at "
                                 f"{self._where(node)}: write the [][Next]_vars where the formula is")
            uid = str(node.find("operator")[0].findtext("UID"))  # type: ignore[index]
            if uid in seen or operator.find("body") is None:
                return []
            return self._boxes(operator.find("body")[0], formula, seen | {uid})  # type: ignore[index]
        if kind in ("$ConjList", "\\land"):
            return [box for operand in operands for box in self._boxes(operand, formula, seen)]
        if kind == "[]" and self._operator(operands[0]).findtext("uniquename") == "$SquareAct":
            return [operands[0]]
        if kind in ("$WF", "$SF"):
            return []
        if kind == "$BoundedForall":
            return [box for operand in operands for box in self._boxes(operand, formula, seen)]
        raise SystemExit(f"error: {formula} has `{self._text(node)}` beside its [][Next]_vars, at "
                         f"{self._where(node)}: only an initial condition and fairness may stand there")

    def used_otherwise(self, formula: str) -> str | None:
        """An action the walk read that the specification also uses other than as a step, if there is one.

        Read after the walk. A use is a reference anywhere in the formula the
        configurations check, or in a definition it names: the initial
        condition as much as the relation. Fairness is not read: it changes
        which behaviors count, not which states are reached. Nor is the place
        a LET defines an action.
        """

        followed = {node.find("operator")[0] for node in self.followed}  # type: ignore[index]
        top = [node for node in self.entries.values()
               if node.tag == "UserDefinedOpKind" and node.findtext("uniquename") == formula
               and self._defined_here(node) and node.find("body") is not None]
        seen: set[str] = set()
        bodies = [top[0].find("body")] if len(top) == 1 else []
        while bodies:
            for reference in self._references(bodies.pop()):  # type: ignore[arg-type]
                uid = str(reference.findtext("UID"))
                if uid in self.assigns and reference not in followed:
                    return str(self.entries[uid].findtext("uniquename"))
                if uid not in seen:
                    seen.add(uid)
                    # A definition's expression is its body. A named theorem's
                    # or assumption's is the node itself, which a guard can
                    # name as it names a definition.
                    entry = self.entries[uid]
                    definition = entry.find("body") if entry.tag == "UserDefinedOpKind" else entry
                    if definition is not None:
                        bodies.append(definition)
        return None

    # What an expression can name that has an expression of its own.
    _NAMED = ("UserDefinedOpKindRef", "TheoremDefRef", "AssumeDefRef")

    def _references(self, node: ET.Element) -> list[ET.Element]:
        """The definitions, theorems and assumptions an expression names, outside fairness and a LET's definition sites."""

        found, stack = [], [node]
        while stack:
            current = stack.pop()
            if current.tag == "OpApplNode" and self._operator(current).findtext("uniquename") in ("$WF", "$SF"):
                continue
            if current.tag in self._NAMED:
                found.append(current)
                continue
            stack += [child for child in current if not (current.tag == "LetInNode" and child.tag == "opDefs")]
        return found

    # --- the guards

    def _conditions(self, node: ET.Element, action: str) -> None:
        """A conjunct that reads no next state is a guard, or several if it is a conjunction itself.

        `/\\ a /\\ b` on one line is two conditions, as it is on two.
        """

        if node.tag == "OpApplNode" and self._operator(node).findtext("uniquename") in ("$ConjList", "\\land"):
            for operand in self._operands(node):
                self._conditions(operand, action)
            return
        first, start, last, end = self._span(node)
        guard = Guard(action, "/\\ " + self._text(node), first, last, start, end)
        self.found.append(guard)
        self.nodes[guard] = node

    def walk(self, node: ET.Element, action: str) -> frozenset[str]:
        """List the conditions of an action, following every action it is built from.

        An action is a conjunction, a disjunction, an existential quantifier
        over a set that does not depend on the state, an IF, a LET, a call
        to another action of this module, or an effect. An effect is
        `x' = e` or `x' \\in S` for a variable x, or UNCHANGED of variables.
        In a conjunction, a conjunct that reads no next state is a
        condition: SANY gives it the level of a state predicate or a
        constant. Anything else is an error.

        What comes back is the variables the action may give a value. A
        conjunction gives a variable a value once: TLC takes a second
        `x' = e` or `x' \\in S` for a condition on the value the first one
        gave, and that condition would have no row.
        """

        if node.tag == "LetInNode":
            return self.walk(node.find("body")[0], action)  # type: ignore[index]
        if node.tag != "OpApplNode":
            raise SystemExit(f"error: {action} has a {node.tag} at {self._where(node)} where an action was expected")
        operator, operands = self._operator(node), self._operands(node)
        kind = str(operator.findtext("uniquename"))
        given: frozenset[str] = frozenset()
        if operator.tag == "UserDefinedOpKind" and self._level(operator) >= 2 and self._defined_here(operator):
            if any(self._level(operand) >= 2 for operand in operands):
                raise SystemExit(f"error: {action} hands something primed to the action {kind}, at {self._where(node)}")
            self.followed.add(node)
            uid = str(node.find("operator")[0].findtext("UID"))  # type: ignore[index]
            if uid in self.reading:
                # What it gives a value is not known until it has been read,
                # so a value beside the call could not be told from a second one.
                raise SystemExit(f"error: {action} calls {kind}, which calls itself, at {self._where(node)}: "
                                 "an action that calls itself is not read here")
            if uid not in self.assigns:
                self.reading.add(uid)
                self.assigns[uid] = self.walk(operator.find("body")[0], kind)  # type: ignore[index]
                self.reading.discard(uid)
            return self.assigns[uid]
        if operator.tag == "UserDefinedOpKind" and self._defined_here(operator):
            # A definition that is no action by itself and is one here because
            # of what it is given: it could put a condition beside its argument.
            raise SystemExit(f"error: {action} gives an action, or something primed, to {kind}, at "
                             f"{self._where(node)}: a condition inside {kind} would have no row")
        if kind in ("$DisjList", "\\lor"):
            for operand in operands:
                if self._level(operand) < 2:
                    raise SystemExit(f"error: {action} has a disjunct that is no action: `{self._text(operand)}`")
                given |= self.walk(operand, action)
            return given
        if kind == "$BoundedExists":
            bounds = node.find("boundSymbols")
            for bound in (bounds if bounds is not None else []):
                for domain in bound:
                    if domain.tag in ("FormalParamNodeRef", "tuple"):
                        continue
                    if self._level(domain) > 0 or self._parameters_under(domain, set()):
                        # `Step(S) == \E k \in S : ...` is the same thing one
                        # call away: S is what `Step({j \in 1..3 : j > x})` gives.
                        raise SystemExit(
                            f"error: {action} quantifies over `{self._text(domain)}`, which depends on the state or "
                            "on a parameter: that is a condition with no row. Quantify over a constant set and "
                            "write the condition as a conjunct")
            for operand in operands:
                given |= self.walk(operand, action)
            return given
        if kind in ("$ConjList", "\\land"):
            for operand in operands:
                if self._level(operand) < 2:
                    self._conditions(operand, action)
                    continue
                more = self.walk(operand, action)
                if given & more:
                    raise SystemExit(
                        f"error: {action} gives {sorted(given & more)[0]} a value twice in one step, at "
                        f"{self._where(operand)}: TLC takes the second for a condition on the first, and it would "
                        "have no row. Write it as a condition")
                given |= more
            return given
        if kind == "$IfThenElse":
            for branch in operands[1:]:
                if self._level(branch) < 2:
                    # `IF c THEN x' = e ELSE FALSE` is the guard c, written
                    # where it would have no row.
                    raise SystemExit(f"error: {action} has an IF at {self._where(node)} with a branch that is no "
                                     f"action, `{self._text(branch)}`: write its condition as a conjunct")
                given |= self.walk(branch, action)
            return given
        if kind in ("=", "\\in") and operands[0].tag == "OpApplNode" \
                and self._operator(operands[0]).findtext("uniquename") == "'" \
                and self._is_a_variable(self._operands(operands[0])[0]):
            # `x' = e` or `x' \in S`. What e or S holds is a value's business.
            return frozenset({str(self._operator(self._operands(operands[0])[0]).findtext("uniquename"))})
        unchanged = self._variables_named(operands[0], set()) if kind == "UNCHANGED" else None
        if unchanged is None:
            # Anything else could hold a condition where this would not look:
            # `(g /\\ x' = e) = TRUE`, `~(~g \\/ x' # e)`, `Print("", g /\\ x' = e)`,
            # `TRUE' = g`, which assigns nothing and is the condition g, and
            # `\A k \in S : x' = k`, which gives x a value for each k.
            raise SystemExit(f"error: {action} has `{self._text(node)}` at {self._where(node)}: in an action this "
                             "reads `x' = e` and `x' \\in S` for a variable x, and UNCHANGED of variables, "
                             f"and not {kind}")
        return unchanged


def specification_of(configs: dict[str, str]) -> str:
    """The formula every configuration of a spec checks: the one its SPECIFICATION names."""

    named = set()
    for variant in sorted(configs):
        formulas = [body for keyword, _, body in _sections(configs[variant]) if keyword == "SPECIFICATION"]
        if len(formulas) != 1 or len(formulas[0]) != 1:
            raise SystemExit("error: a configuration does not name one formula as its SPECIFICATION")
        named.add(formulas[0][0])
    if len(named) != 1:
        raise SystemExit(f"error: the configurations name {len(named)} formulas as their SPECIFICATION, not one")
    return named.pop()


def guards(spec_text: str, formula: str) -> list[Guard]:
    """Every condition of every action that the next-state relation is built from.

    This is read from SANY's parse, not from the text. The relation is the
    one in the formula the configurations check (_Parse.relation). From it,
    every action is followed (_Parse.walk), and each conjunct of an action
    that reads no next state is a guard: a condition, with the place in the
    text it can be removed from. That includes one in a list nested under
    another, under a quantifier, in a LET's body or in a branch of an IF, and
    one that shares its line with an effect.

    What has no row: the condition of an IF between two actions, and a
    condition inside the value an effect assigns, which includes the set in
    `x' \\in S`. What stops the listing: anything SANY cannot parse, a
    quantifier over a set that depends on the state, an IF with a branch
    that is no action, a definition given an action or something primed,
    and anything in an action that is not one of the shapes _Parse.walk
    names.
    """

    found = sorted(_walked(spec_text, formula).found, key=lambda guard: (guard.first, guard.start))
    keys = [(guard.action, guard.text) for guard in found]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise SystemExit(f"error: the same guard twice in one action: {repeated[0]}")
    return found


def _walked(spec_text: str, formula: str) -> _Parse:
    """The spec as SANY read it, with the actions of its relation followed."""

    parse = _Parse(spec_text)
    relation = parse.relation(formula)
    if parse._level(relation) < 2:
        raise SystemExit(f"error: the relation in {formula} primes nothing: it is no action")
    parse.walk(relation, formula)
    return parse


def action_used_otherwise(spec_text: str, formula: str) -> str | None:
    """An action of the next-state relation that the specification also uses other than as a step, if there is one.

    The walk follows an action only where weakening it weakens the whole: a
    conjunction, a disjunction, a quantifier, an IF's branches, a LET's body
    and a call. So removing a guard adds steps and takes none away, and every
    state the spec reached is reached without it, unless the specification
    also uses an action some other way: under ENABLED, in an IF's condition,
    inside a value, or in the initial condition. `~ENABLED A` loses a step,
    or an initial state, when a guard of A goes.
    """

    return _walked(spec_text, formula).used_otherwise(formula)


def without_guard(spec_text: str, guard: Guard, formula: str) -> str:
    """The spec with that one guard replaced by TRUE, and nothing else touched.

    In TLA+ the column a `/\\` or `\\/` stands in says which list it belongs
    to. So what follows the guard on its line stays in its column: TRUE is
    padded to the guard's width. Where it cannot be, because the guard is
    narrower than TRUE, this is an error and not a guess.

    The result is then read again (_is_the_spec_without), and no model is
    run on a result that is anything but the spec with that guard gone.
    """

    lines = spec_text.split("\n")
    before, after = lines[guard.first][: guard.start], lines[guard.last - 1][guard.end:]
    width = guard.end - guard.start
    if not _code_lines(spec_text)[guard.last - 1][guard.end:].strip():
        replaced = before + "TRUE" + after
    elif width < len("TRUE"):
        raise SystemExit(f"error: {guard.action}'s guard `{guard.text}` is narrower than TRUE and has more after it "
                         "on its line: removing it would move that. Give the guard a line of its own")
    else:
        replaced = before + "TRUE".ljust(width) + after
    removed = "\n".join([*lines[: guard.first], replaced, *lines[guard.last:]])
    if not _is_the_spec_without(spec_text, removed, guard, formula):
        raise SystemExit(f"error: replacing {guard.action}'s guard `{guard.text}` by TRUE did not give the same "
                         "spec with that one guard gone: its place in the text was misread")
    return removed


def _is_the_spec_without(spec_text: str, removed: str, guard: Guard, formula: str) -> bool:
    """Whether `removed` reads as the spec with that guard replaced by TRUE, and nothing else changed.

    The two are compared as SANY read them, whole (_Parse.shape): every
    definition, the initial condition and the fairness with the rest. They
    may differ in one node, which is the guard in the one, as the listing
    gives it, and a TRUE at the guard's place in the other. A guard the
    spec does not list marks nothing in the spec, so they differ there. If
    the guard's place in the text was misread, TRUE landed somewhere else,
    or took something with it, or left something SANY cannot parse.
    """

    before = _walked(spec_text, formula)
    try:
        after = _Parse(removed)
    except SystemExit:
        return False
    there = after.true_at(guard.first, guard.start)
    return len(there) == 1 and after.shape(there[0]) == before.shape(before.nodes.get(guard))


def inputs_digest(spec_text: str, configs: dict[str, str]) -> str:
    """One digest of everything a sweep's result depends on."""

    digest = hashlib.sha256()
    for part in [spec_text, *(f"{variant}\0{configs[variant]}" for variant in sorted(configs))]:
        digest.update(part.encode())
        digest.update(b"\0\0")
    return digest.hexdigest()


_SWEPT_SECTIONS = frozenset({"SPECIFICATION", "CONSTANT", "CONSTANTS", *_CLAIM_KEYWORDS})
_VALUE_WORD = re.compile(r"\w+|\".*\"", re.DOTALL)


def _constant_entries(body: list[str]) -> list[tuple[str, str]] | None:
    """Each entry of a CONSTANT section: the name it sets, and the definition put in its place if it is a `<-`.

    None if an entry is not read.

    An entry is `Name = value` or `Name <- Other`. A value is a word, a
    string, or a set of values. TLC reads more: `Op(1) = FALSE` replaces a
    definition for those arguments, and `Name <- [Module] Other` replaces
    one inside a module. A configuration with a guard table does not use
    them, and is refused if it does, since here the name on the left would
    not be the token before the `=`.
    """

    def value(at: int) -> int | None:
        """The index after the value that starts at `at`, or None if none does."""

        if at >= len(body):
            return None
        if body[at] == "{":
            at += 1
            if at < len(body) and body[at] == "}":
                return at + 1
            while True:
                after = value(at)
                if after is None or after >= len(body) or body[after] not in (",", "}"):
                    return None
                if body[after] == "}":
                    return after + 1
                at = after + 1
        if body[at] == "-":
            return at + 2 if at + 1 < len(body) and body[at + 1].isdigit() else None
        return at + 1 if _VALUE_WORD.fullmatch(body[at]) else None

    entries, at = [], 0
    while at < len(body):
        if at + 1 >= len(body) or not _WORD.fullmatch(body[at]) or body[at + 1] not in ("=", "<-"):
            return None
        if body[at + 1] == "=":
            after = value(at + 2)
        else:
            after = at + 3 if at + 2 < len(body) and _WORD.fullmatch(body[at + 2]) else None
        if after is None:
            return None
        entries.append((body[at], body[at + 2] if body[at + 1] == "<-" else ""))
        at = after
    return entries


def guard_problems(
    name: str, entry: dict, spec_text: str, configs: dict[str, str], table_text: str | None
) -> list[str]:
    """What is wrong with a spec's guard table, short of running the sweep again."""

    sweep = f"run proofs/guard_sweep.py {name}"
    if "unswept" in entry:
        if table_text is not None:
            return ["it has a guard table and says it is unswept: remove one"]
        return [] if str(entry["unswept"]).strip() else ["`unswept` gives no reason"]
    if table_text is None:
        return [f"it has no {name}.guards.toml: {sweep}, or say why it is `unswept`"]
    table = tomllib.loads(table_text)
    if table.get("inputs_sha256") != inputs_digest(spec_text, configs):
        return [f"its guard table was swept against another spec or configuration: {sweep}"]
    states = table.get("states")
    if not isinstance(states, dict) or sorted(states) != sorted(config_file(name, variant) for variant in configs) \
            or any(type(count) is not int or count < 1 for count in states.values()):
        return [f"its guard table does not give the distinct states each configuration reaches (`[states]`): {sweep}"]
    # The table is of the spec's own text. A configuration that names its own
    # relation, restricts the behaviors checked, or replaces a definition
    # would have TLC check something the table does not follow.
    parse = _Parse(spec_text)
    own, steady = parse.own_names(), parse.steady_names()
    for variant in sorted(configs):
        for keyword, _, body in _sections(configs[variant]):
            if keyword not in _SWEPT_SECTIONS:
                return [f"a configuration has a {keyword} section: a spec with a guard table is checked "
                        "as its text says, with constants and claims and nothing else"]
            if keyword not in ("CONSTANT", "CONSTANTS"):
                continue
            # TLC takes `Next <- Other` and `Next = FALSE` alike for a name the
            # module defines, and runs what the configuration says.
            entries = _constant_entries(body)
            if entries is None:
                return [f"a configuration's {keyword} section has an entry that is not `Name = value` or "
                        "`Name <- Other`: with a guard table, a configuration gives constants their values "
                        "that way and no other"]
            replaced = sorted({name for name, _ in entries} & own)
            if replaced:
                return [f"a configuration replaces {replaced[0]}, one of the "
                        "spec's own definitions: the guard table is of the definitions as written"]
            # In the text the name is a constant, whatever module defines it.
            # A definition that reads the state, or is an action, would make
            # of each use of it a condition or an effect with no row.
            moving = sorted(other for _, other in entries if other and other not in steady)
            if moving:
                return [f"a configuration puts {moving[0]} in place of a name, and {moving[0]} is not a "
                        "definition that reads no state: the guard table is of the text, where that name "
                        "is a constant"]
    rows = table.get("guard", [])
    listed = [(row.get("action"), row.get("text")) for row in rows]
    wanted = [(guard.action, guard.text) for guard in guards(spec_text, specification_of(configs))]
    if listed != wanted:
        return [f"its guard table does not list the spec's guards in order: {sweep}"]
    problems = []
    for row in rows:
        label = f"{row['action']}: {row['text']}"
        breaks, variant = row.get("breaks"), row.get("cfg", "")
        if breaks in (BREAKS_NOTHING, BREAKS_EVALUATION, TYPE_INVARIANT):
            if len(str(row.get("why", "")).split()) < 4:
                problems.append(f"guard {label}: breaks {breaks} and gives no reason (`why`)")
        if breaks == BREAKS_NOTHING and row.get("reaches") not in (NO_NEW_STATE, NEW_STATES):
            problems.append(f"guard {label}: breaks nothing and does not say whether it reaches a new state (`reaches`)")
        if breaks != BREAKS_NOTHING and "reaches" in row:
            problems.append(f"guard {label}: `reaches` is only for a guard that breaks nothing")
        # A row names the variant that shows what its guard breaks when the
        # main configuration does not: a claim, or an expression left with no
        # value. A guard that breaks nothing names none.
        if breaks == BREAKS_NOTHING:
            if "cfg" in row:
                problems.append(f"guard {label}: `cfg` is not for a guard that breaks nothing")
        elif variant not in configs:
            problems.append(f"guard {label}: the variant {variant} has no .cfg")
        elif breaks != BREAKS_EVALUATION and breaks not in _claims(configs[variant]):
            problems.append(f"guard {label}: {breaks} is not a claim its configuration checks")
    return problems


def state_problems(entry: dict, specs_on_disk: set[str], root: Path = ROOT) -> list[str]:
    """What is wrong with the entry's state, given the files that exist."""

    state = entry.get("state")
    code = [root / path for path in entry.get("code", [])]
    tests = [root / path for path in entry.get("tests", [])]
    problems: list[str] = []
    if state == "planned":
        if not code:
            problems.append("a planned spec names where its code will go (code)")
        problems += [
            f"{path.relative_to(root)} exists: name the code and its tests, and mark the spec implemented"
            for path in code if path.exists()
        ]
    elif state == "implemented":
        if not code or not tests:
            problems.append("an implemented spec names its code and its tests")
        problems += [f"{path.relative_to(root)} is missing" for path in code + tests if not path.exists()]
    elif state == "orphaned":
        replacement = entry.get("retire_with")
        if not replacement:
            problems.append("an orphaned spec names the spec that replaces it (retire_with)")
        elif replacement in specs_on_disk:
            problems.append(f"{replacement} is here: delete this spec")
        if not code:
            problems.append("an orphaned spec names the code that was deleted (code)")
        problems += [f"{path.relative_to(root)} exists: the spec is not orphaned" for path in code if path.exists()]
    else:
        problems.append(f"unknown state {state!r}")
    return problems


_SELF_TEST_SPEC = r"""---- MODULE SelfTest ----
EXTENDS Naturals
VARIABLE x
Init == x = 0
Next == /\ x < 2
        /\ x' = x + 1
Spec == Init /\ [][Next]_x /\ WF_x(Next)
Small == x <= 2
NonNegative == x >= 0
Reaches == <>(x = 2)
StartsAtZero == x = 0
OneStep == [][x' = x + 1]_x
AlwaysSmall == []Small
Limit == x' <= 5
====
"""
_SELF_TEST_CFG = (
    "SPECIFICATION Spec\n"
    "INVARIANTS\n    Small\n    NonNegative\n"
    "PROPERTIES Reaches StartsAtZero OneStep AlwaysSmall\n"
    "ACTION_CONSTRAINT Limit\n"
)
# Act has three guards and Other one. A conjunct that primes a variable is
# no guard, whether it does so itself, inside a LET, or through a definition,
# here two definitions down (Through, then Step). Next reaches Act and Other
# through Either, and Step through Act. Unused is not reached. Range is named
# in Next and is not an action. The comment before the first conjunct hides
# nothing. A LOCAL definition is a definition, and fairness under a
# quantifier is fairness.
_GUARD_SPEC = r"""---- MODULE Tiny ----
EXTENDS Naturals
VARIABLES x, z
Range == {
    1, 2
}
Allowed(a) == a > 0
Same(a) == a
LOCAL Step(a) ==
    /\ z' = a
Through(a) == Step(a)
Act(a) ==
    \* the action's guards follow this line
    /\ a \in Range          \* its first guard
    /\ Allowed(a)
    /\ \/ x = 1
       \/ x = 2
    /\ Through(Same(a))
    /\ LET y == 1 IN x' = y
Other ==
    /\ x > 0
    /\ x' = 0
    /\ UNCHANGED z
Unused ==
    /\ x = 5
    /\ x' = 6
    /\ UNCHANGED z
Either(a) == Act(a) \/ Other
Next == \E a \in Range : Either(a)
Spec == x = 0 /\ z = 0 /\ [][Next]_<< x, z >> /\ \A a \in Range : WF_<< x, z >>(Act(a))
Live == {x}
====
"""
# What is in a comment or a string is not code: a conjunct inside a block
# comment is not a guard, an apostrophe in a string or a comment is not a
# prime, and a comment cut out of the middle of a guard leaves the guard.
_COMMENT_SPEC = r"""---- MODULE Tiny ----
VARIABLE x
Act ==
    (* a block comment, with a line that looks like a guard:
    /\ x = 0
    and (* a nested one *) before it ends *)
    /\ "don't" (* it is *) # "do"     \* it's a guard
    /\ x \* = 9' and not this
       = 1
    /\ x' = 2
Next == Act
Spec == x = 0 /\ [][Next]_x
====
"""
# A wrapper that primes only through the action it calls still has a guard of
# its own. The relation is the one in the formula the configurations check:
# Upward's box is a property's.
_WRAPPER_SPEC = r"""---- MODULE Tiny ----
EXTENDS Naturals
VARIABLE x
Step ==
    /\ x' = x + 1
Bounded ==
    /\ x < 2
    /\ Step
Next == Bounded
Spec == x = 0 /\ [][Next]_x /\ WF_x(Next)
Upward == [][Step]_x
====
"""
# A condition has a row wherever in an action it stands: in a list under a
# quantifier, in a branch of an IF, in a LET's body, and on a line it shares
# with effects, where two conditions are two rows. The condition of an IF has
# none, and neither has anything inside the value an effect assigns. The
# guards are listed in the order of the text, whatever order the relation
# names its actions in.
_NESTED_SPEC = r"""---- MODULE Tiny ----
EXTENDS Naturals
VARIABLES x, y, z, w
ASSUME Positive == 1 > 0
Act ==
    /\ x' = IF x > 0 /\ y > 0 THEN 1 ELSE 0
    /\ \E n \in {1, 2} :
           /\ n > x
           /\ y' = n
    /\ IF x = 0
           THEN /\ y < 5
                /\ z' = 1
           ELSE z' = 2
    /\ LET m == x + 1 IN m < 9 /\ w' = m
Other == /\ y > 2 /\ w = 0 /\ z > 0 /\ x' = 0 /\ UNCHANGED << y, z, w >>
Next == Other \/ Act
Spec == x = 0 /\ [][Next]_<< x, y, z, w >>
====
"""
_OTHER = "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n"
_THROUGH = "    /\\ Through(Same(a))\n"
_EFFECT = "    /\\ LET y == 1 IN x' = y\n"
_EITHER = "Either(a) == Act(a) \\/ Other\n"
_NEXT = "Next == \\E a \\in Range : Either(a)\n"
_FORMULA = "Spec == x = 0 /\\ z = 0 /\\ [][Next]_<< x, z >> /\\ \\A a \\in Range : WF_<< x, z >>(Act(a))\n"
_FOUR = [("Act", "/\\ a \\in Range"), ("Act", "/\\ Allowed(a)"), ("Act", "/\\ \\/ x = 1 \\/ x = 2"), ("Other", "/\\ x > 0")]
# The same model written other ways, and the guards each way has: (what, the
# text replaced, by what, the guards). SANY reads them all, so none of these
# shapes hides a guard or needs to be refused.
_OTHER_WAYS = [
    ("an action written on the line of its ==", _OTHER, "Other == x > 0 /\\ x' = 0 /\\ UNCHANGED z\n", _FOUR),
    ("a conjunction spelled \\land", _OTHER, "Other == x > 0 \\land x' = 0 \\land UNCHANGED z\n", _FOUR),
    ("a wrapper that chooses between actions with an IF", _EITHER,
     "Either(a) == IF a = 1 THEN Act(a) ELSE Other\n", _FOUR),
    ("a condition on the line of an effect", _EFFECT, "    /\\ x < 3 /\\ x' = 1\n",
     [*_FOUR[:3], ("Act", "/\\ x < 3"), _FOUR[3]]),
    ("a guard that reads whether an action is enabled", _OTHER,
     "Other ==\n    /\\ ENABLED Act(1)\n    /\\ x' = 0\n    /\\ UNCHANGED z\n",
     [*_FOUR[:3], ("Other", "/\\ ENABLED Act(1)")]),
    ("a definition indented under another", "Allowed(a) == a > 0\n",
     "Allowed(a) == a > 0\n  Hidden ==\n    /\\ x < 1\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", _FOUR),
    ("a quantifier over a constant set that a LET names", _NEXT,
     "Next == LET R == Range IN \\E a \\in R : Either(a)\n", _FOUR),
    ("a quantifier over a set that a definition builds from the constants it is given", _NEXT,
     "Span(i, j) == i..j\nNext == \\E a \\in Span(1, 2) : Either(a)\n", _FOUR),
    ("a quantifier over a set that a definition builds by calling itself", _NEXT,
     "RECURSIVE Up(_)\nUp(n) == IF n = 0 THEN {} ELSE {n} \\cup Up(n - 1)\nNext == \\E a \\in Up(2) : Either(a)\n", _FOUR),
    ("an UNCHANGED that names its variables through a definition", _OTHER,
     "both == << x, << z >> >>\nOther ==\n    /\\ x > 0\n    /\\ UNCHANGED both\n", _FOUR),
    ("a conjunct that is TRUE, which is a guard like another", _OTHER,
     "Other ==\n    /\\ TRUE\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n",
     [*_FOUR[:3], ("Other", "/\\ TRUE"), _FOUR[3]]),
    ("a guard whose strings differ only in their spaces", _OTHER,
     "Other ==\n    /\\ \"a  b\" # \"a b\"\n    /\\ x' = 0\n    /\\ UNCHANGED z\n",
     [*_FOUR[:3], ("Other", '/\\ "a  b" # "a b"')]),
    ("an action called from two places", _EITHER,
     "Either(a) == Act(a) \\/ Other \\/ (x > 5 /\\ Other)\n", [*_FOUR, ("Either", "/\\ x > 5")]),
    ("a formula whose own box is unused, with the one TLC runs in a definition it names", _FORMULA,
     "Actual == x = 0 /\\ z = 0 /\\ [][Other]_<< x, z >>\nSpec == LET Ignored == [][Next]_<< x, z >> IN Actual\n",
     [_FOUR[3]]),
]
# An action used other than as a step, which a count of states has to know
# of: (what, the text replaced, by what, the action named).
_USES = [
    ("a guard that reads whether an action is enabled", _OTHER,
     "Other ==\n    /\\ ENABLED Act(1)\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", "Act"),
    ("a guard that reads, through a definition, whether an action is enabled", _OTHER,
     "Ready == ENABLED Through(1)\nOther ==\n    /\\ Ready\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", "Through"),
    ("an action in an IF's condition", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ IF Act(1) THEN x' = 0 ELSE x' = 1\n    /\\ UNCHANGED z\n", "Act"),
    ("an action inside the value an effect gives", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = IF Act(1) THEN 0 ELSE 1\n    /\\ UNCHANGED z\n", "Act"),
    ("an initial condition that reads whether an action is enabled", _FORMULA,
     "Spec == x = 0 /\\ z = 0 /\\ ~ENABLED Act(1) /\\ [][Next]_<< x, z >> /\\ \\A a \\in Range : WF_<< x, z >>(Act(a))\n",
     "Act"),
    ("a guard that names a theorem that reads whether an action is enabled", _OTHER,
     "THEOREM NotReady == ~ENABLED Act(1)\nOther ==\n    /\\ NotReady\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", "Act"),
    ("an action a LET defines and the relation takes as a step", _NEXT,
     "Next == LET Hop == x = 3 /\\ x' = 0 /\\ UNCHANGED z IN \\E a \\in Range : Either(a) \\/ Hop\n", None),
]
# What stops the listing: (what, the text replaced, by what, how the refusal
# starts).
_UNLISTABLE = [
    ("a spec SANY cannot parse", _OTHER,
     "Other ==\n    /\\ x >\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", "error: SANY could not parse Tiny"),
    ("a spec that extends a module that is not TLA+'s own", "EXTENDS Naturals\n",
     "EXTENDS Naturals, Actions\n", "error: SANY could not parse Tiny"),
    ("an action indented with a tab", _OTHER,
     "Other ==\n\t/\\ x > 0\n\t/\\ x' = 0\n\t/\\ UNCHANGED z\n", "error: Tiny has a tab"),
    ("a tab inside a comment, before a guard on its line", _OTHER,
     "Other ==\n    /\\ (*\t\t\t*) x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n", "error: Tiny has a tab"),
    ("a character that Java stores as two, before a guard on its line", _OTHER,
     "Other ==\n    /\\ \"\U0001F600\" # \"\" /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n",
     "error: Tiny has a tab, or a character outside the basic plane"),
    ("a guard written as an IF with FALSE in its other branch", _EFFECT,
     "    /\\ IF x < 3 THEN x' = 1 ELSE FALSE\n", "error: Act has an IF at line 19 with a branch that is no action, `FALSE`"),
    ("an action that is a CASE", _OTHER,
     "Other == CASE x > 0 -> x' = 0 /\\ UNCHANGED z [] OTHER -> UNCHANGED << x, z >>\n",
     "error: Other has `CASE"),
    ("a formula built from a definition that is given the relation", _FORMULA,
     "Wrap(A) == x = 0 /\\ z = 0 /\\ [][A]_<< x, z >>\nSpec == Wrap(Other)\n",
     "error: Spec is built from Wrap, which takes arguments"),
    ("a relation that quantifies over a set that depends on the state", _NEXT,
     "Next == \\E a \\in 1..x : Either(a)\n", "error: Next quantifies over `1..x`"),
    ("a relation that quantifies over a definition that reads the state, two definitions down", _NEXT,
     "Open == {x}\nWide == Open \\cup {3}\nNext == \\E a \\in Wide : Either(a)\n", "error: Next quantifies over `Wide`"),
    ("a call under a quantifier over a set that depends on the state", _THROUGH,
     "    /\\ \\E k \\in 1..x : Through(k)\n", "error: Act quantifies over `1..x`"),
    ("a quantifier whose second set depends on the state", _NEXT,
     "Next == \\E a \\in Range, b \\in 1..x : Either(a)\n", "error: Next quantifies over `1..x`"),
    ("a quantifier over a set that a caller passes in", _NEXT,
     "Over(S) == \\E a \\in S : Either(a)\nNext == Over({j \\in Range : j > x})\n",
     "error: Over quantifies over `S`, which depends on the state or on a parameter"),
    ("a quantifier over a set built from a number that a caller passes in", _NEXT,
     "Upto(n) == \\E a \\in 1..n : Either(a)\nNext == Upto(x)\n",
     "error: Upto quantifies over `1..n`, which depends on the state or on a parameter"),
    ("a quantifier over a set that a caller passes in, under a LET's name", _NEXT,
     "Over(S) == LET T == S IN \\E a \\in T : Either(a)\nNext == Over({j \\in Range : j > x})\n",
     "error: Over quantifies over `T`, which depends on the state or on a parameter"),
    ("a quantifier over a set that a LET builds from a number a caller passes in", _NEXT,
     "Upto(n) == LET D(i) == i..n IN \\E a \\in D(1) : Either(a)\nNext == Upto(x)\n",
     "error: Upto quantifies over `D(1)`, which depends on the state or on a parameter"),
    ("a condition written as an effect on TRUE, which assigns nothing", _EFFECT,
     "    /\\ TRUE' = (x < 3)\n    /\\ x' = 1\n", "error: Act has `TRUE' = (x < 3)` at line 19: in an action this reads"),
    ("an effect on a parameter, which is no variable", _EFFECT,
     "    /\\ a' \\in {x}\n    /\\ x' = 1\n", "error: Act has `a' \\in {x}` at line 19: in an action this reads"),
    ("an UNCHANGED of something that is no variable", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED (z > 0)\n",
     "error: Other has `UNCHANGED (z > 0)` at line 23: in an action this reads"),
    ("an UNCHANGED of a tuple that holds something that is no variable", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED << z, z > 0 >>\n",
     "error: Other has `UNCHANGED << z, z > 0 >>` at line 23: in an action this reads"),
    ("an UNCHANGED of a definition that is no variable", _OTHER,
     "shown == z > 0\nOther ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED shown\n",
     "error: Other has `UNCHANGED shown` at line 24: in an action this reads"),
    ("an UNCHANGED of a definition that names itself", _OTHER,
     "RECURSIVE both\nboth == << z, both >>\nOther ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED both\n",
     "error: Other has `UNCHANGED both` at line 25: in an action this reads"),
    ("a quantifier over a set that names two definitions, the second of which a caller's set reaches", _NEXT,
     "Over(S) == LET A == {}\n               B == S\n           IN \\E a \\in A \\cup B : Either(a)\n"
     "Next == Over({j \\in Range : j > x})\n",
     "error: Over quantifies over `A \\cup B`, which depends on the state or on a parameter"),
    ("a variable given a value twice in one step", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ x' \\in {0, 1}\n    /\\ UNCHANGED z\n",
     "error: Other gives x a value twice in one step, at line 23"),
    ("a variable given a value, and again in one disjunct of a disjunction", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ (x' = 1 \\/ z' = 1)\n    /\\ UNCHANGED z\n",
     "error: Other gives x a value twice in one step, at line 23"),
    ("a variable given a value, and again under an existential quantifier", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ \\E k \\in Range : x' = k\n    /\\ UNCHANGED z\n",
     "error: Other gives x a value twice in one step, at line 23"),
    ("a variable given a value, and again in one branch of an IF", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ IF z = 0 THEN x' = 1 ELSE z' = 1\n    /\\ UNCHANGED z\n",
     "error: Other gives x a value twice in one step, at line 23"),
    ("an action that calls itself", _EITHER,
     "RECURSIVE Go(_)\nGo(k) == IF k = 0 THEN Other ELSE Go(k - 1)\nEither(a) == Act(a) \\/ Go(1)\n",
     "error: Go calls Go, which calls itself"),
    ("an UNCHANGED of a variable the step has given a value", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED << x, z >>\n",
     "error: Other gives x a value twice in one step, at line 23"),
    ("a variable given a value by an action that is called, and again beside the call", _THROUGH,
     "    /\\ Through(Same(a))\n    /\\ z' = 1\n",
     "error: Act gives z a value twice in one step, at line 19"),
    ("an effect under a universal quantifier, which gives its variable a value for each member", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ \\A k \\in Range : x' = k\n    /\\ UNCHANGED z\n",
     "error: Other has `\\A k \\in Range : x' = k` at line 22: in an action this reads"),
    ("an effect handed to a definition", _EFFECT,
     "    /\\ Allowed(x' = 1)\n", "error: Act gives an action, or something primed, to Allowed"),
    ("an action handed to an operator", _EITHER,
     "Apply(Op(_), a) == Op(a)\nEither(a) == Apply(Act, a) \\/ Other\n",
     "error: Either gives an action, or something primed, to Apply"),
    ("something primed handed to an action", _THROUGH,
     "    /\\ Through(x')\n", "error: Act hands something primed to the action Through"),
    ("a disjunct that is no action", _NEXT,
     "Next == (\\E a \\in Range : Either(a)) \\/ x = 5\n", "error: Next has a disjunct that is no action: `x = 5`"),
    ("an effect behind an implication", _EFFECT,
     "    /\\ x < 3 => x' = 1\n", "error: Act has `x < 3 => x' = 1` at line 19: in an action this reads"),
    ("a condition inside an equality with TRUE", _EFFECT,
     "    /\\ (x < 3 /\\ x' = 1) = TRUE\n", "error: Act has `(x < 3 /\\ x' = 1) = TRUE` at line 19: in an action this reads"),
    ("a condition under a negation", _EFFECT,
     "    /\\ ~(x >= 3 \\/ x' # 1)\n", "error: Act has `~(x >= 3 \\/ x' # 1)` at line 19: in an action this reads"),
    ("a condition handed to an operator of one of TLA+'s own modules", _EFFECT,
     "    /\\ (x < 3 /\\ x' = 1) > 0\n", "error: Act has `(x < 3 /\\ x' = 1) > 0` at line 19: in an action this reads"),
    ("the same guard twice in one action", _OTHER,
     "Other ==\n    /\\ x > 0\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n",
     "error: the same guard twice in one action"),
    ("a relation that primes nothing", _NEXT, "Next == x = 5\n", "error: the relation in Spec primes nothing"),
    ("a formula with no relation", _FORMULA, "Spec == x = 0 /\\ z = 0\n", "error: Spec holds 0 next-state relations"),
    ("a formula with two relations", _FORMULA,
     "Spec == x = 0 /\\ [][Next]_<< x, z >> /\\ [][Other]_<< x, z >>\n", "error: Spec holds 2 next-state relations"),
    ("a formula with an always beside its box", _FORMULA,
     "Spec == x = 0 /\\ [][Next]_<< x, z >> /\\ [](x < 5)\n", "error: Spec has `[](x < 5)` beside its [][Next]_vars"),
    ("a formula with an eventually beside its box", _FORMULA,
     "Spec == x = 0 /\\ [][Next]_<< x, z >> /\\ <>(x = 2)\n", "error: Spec has `<>(x = 2)` beside its [][Next]_vars"),
]
_STOPPING_SPEC = r"""---- MODULE Stops ----
EXTENDS Naturals, TLC
VARIABLE x
Init == x = 0
Next == /\ x < 5
        /\ x' = x + 1
        /\ IF x = 1 THEN TLCSet("exit", TRUE) ELSE TRUE
Spec == Init /\ [][Next]_x
Small == x <= 9
====
"""
_SELF_TEST_CLAIMS = {
    "Small", "NonNegative", "Reaches", "StartsAtZero", "OneStep", "AlwaysSmall",
}


def self_test() -> bool:
    """The runner must tell a killed mutant from one that survives or errors."""

    ok = True
    # (label, (old, new), invariant, property, the verdict it must get)
    tlc_cases = [
        ("a violated invariant is KILLED", ("x < 2", "x < 3"), "Small", None, KILLED),
        ("a violated temporal property is KILLED", ("x < 2", "x < 1"), None, "Reaches", KILLED),
        ("a property violated by the initial state is KILLED",
         ("Init == x = 0", "Init == x = 1"), None, "StartsAtZero", KILLED),
        ("a violated action property is KILLED", ("/\\ x' = x + 1", "/\\ x' = x + 2"), None, "OneStep", KILLED),
        ("a violated property of the form []P is KILLED", ("x < 2", "x < 3"), None, "AlwaysSmall", KILLED),
        ("naming the wrong invariant SURVIVES", ("x < 2", "x < 3"), "NonNegative", None, SURVIVED),
        ("a change that breaks nothing SURVIVES", ("x < 2", "x < 1"), "Small", None, SURVIVED),
        # Here Next can take x to 3, and only the spec's own constraint stops
        # the search before it. (TLC checks invariants on a state a constraint
        # rejects, so the constraint must stop a step short.) A runner that
        # dropped the constraint from the mutant's configuration reports a kill.
        ("the configuration's other sections still apply to a mutant",
         ("Limit == x' <= 5", "Limit == x' <= 1 /\\ x < 9"), "Small", None, SURVIVED),
        ("a syntax error is an ERROR, not a kill", ("x < 2", "x <"), "Small", None, ERROR),
        ("an evaluation error is an ERROR, not a kill", ("/\\ x' = x + 1", "/\\ x' = x + \"a\""), "Small", None, ERROR),
    ]
    for label, (old, new), invariant, prop, wanted in tlc_cases:
        mutated = mutate(_SELF_TEST_SPEC, {"old": old, "new": new}, label)
        if "other sections" in label:
            mutated = mutate(mutated, {"old": "/\\ x < 2\n", "new": "/\\ x < 3\n"}, label)
        output = run_tlc(mutated, config_for(_SELF_TEST_CFG, invariant=invariant, prop=prop), "SelfTest")
        got, why = verdict(output, invariant=invariant, prop=prop, spec_text=mutated)
        ok = _report(label, got == wanted, f"{got}: {why}") and ok

    kept, by_kind = split_config(_SELF_TEST_CFG)
    claims = by_kind["invariant"] | by_kind["property"]
    ok = _report(
        "a configuration's claims are read", claims == _SELF_TEST_CLAIMS, ", ".join(sorted(claims))
    ) and ok
    ok = _report(
        "invariants and properties are told apart",
        by_kind["invariant"] == {"Small", "NonNegative"}, ", ".join(sorted(by_kind["invariant"])),
    ) and ok
    ok = _report(
        "a mutant's configuration keeps every section that is not a claim",
        kept.split() == ["SPECIFICATION", "Spec", "ACTION_CONSTRAINT", "Limit"], " ".join(kept.split()),
    ) and ok
    kept, by_kind = split_config(
        "\\* INVARIANT InALineComment\r\n"
        "(* INVARIANT InABlock (* nested *) PROPERTY Comment *)\n"
        "SPECIFICATION Spec INVARIANT One Two  \\* INVARIANT AfterCode\n"
        "CONSTANTS\n    N = 2\n    Name = \"INVARIANT InAString\"\n"
        "    Slash = \"\\\\\"\n    Quote = \"a\\\"b INVARIANT C\"\n"
        "    Value = INVARIANT\n    Set = {PROPERTY, x}\n    Swap <- NEXT\n"
        "INVARIANTS\n    Three\n    \\* Four\n"
        "CONSTRAINT Bound PROPERTY Five\n"
        "CHECK_DEADLOCK FALSE\n"
    )
    claims = by_kind["invariant"] | by_kind["property"]
    ok = _report(
        "comments, strings and sections that share a line are read as TLC reads them",
        claims == {"One", "Two", "Three", "Five"}, ", ".join(sorted(claims)),
    ) and ok
    ok = _report(
        "and every other section survives the rewrite",
        kept.split() == [
            "SPECIFICATION", "Spec", "CONSTANTS", "N", "=", "2", "Name", "=", '"INVARIANT',
            'InAString"', "Slash", "=", '"\\\\"', "Quote", "=", '"a\\"b', "INVARIANT", 'C"',
            "Value", "=", "INVARIANT", "Set", "=", "{PROPERTY,", "x}", "Swap", "<-", "NEXT",
            "CONSTRAINT", "Bound", "CHECK_DEADLOCK", "FALSE",
        ],
        " ".join(kept.split()),
    ) and ok
    # A claim the rewrite failed to remove would be checked beside the named
    # one, and its violation read as the named one's. Here Tight sits after a
    # string that ends in a backslash, which a careless reader takes for an
    # unclosed string; the mutant breaks Tight and keeps Target.
    hidden_spec = _SELF_TEST_SPEC.replace("VARIABLE x\n", "CONSTANTS K, J\nVARIABLE x\n").replace(
        "NonNegative == x >= 0\n", "NonNegative == x >= 0\nTight == x <= 2\nTarget == []NonNegative\n"
    )
    hidden_cfg = (
        'SPECIFICATION Spec\nCONSTANT K = "\\\\"\nINVARIANT Tight\n'
        'CONSTANT J = "x"\nPROPERTY Target\n'
    )
    hidden = mutate(hidden_spec, {"old": "x < 2", "new": "x < 3"}, "hidden claim")
    output = run_tlc(hidden, config_for(hidden_cfg, invariant=None, prop="Target"), "SelfTest")
    got, why = verdict(output, invariant=None, prop="Target", spec_text=hidden)
    ok = _report(
        "a claim behind a string that ends in a backslash does not leak into a mutant's run",
        got == SURVIVED, f"{got}: {why}",
    ) and ok
    for label, bad in [
        ("a .cfg whose comment never closes is refused", "SPECIFICATION Spec (* INVARIANT X"),
        ("a .cfg whose string never closes is refused", 'SPECIFICATION Spec CONSTANT K = "x'),
        ("a .cfg that does not start with a keyword is refused", "Spec INVARIANT X"),
    ]:
        try:
            split_config(bad)
            refused = ""
        except SystemExit as exit_:
            refused = str(exit_.code)
        ok = _report(label, refused.startswith("error:"), refused or "accepted") and ok

    for label, fake in [
        ("a mutant naming a definition that is not a claim is refused",
         {"name": "m", "violates": "Limit", "old": "x < 2", "new": "x < 3"}),
        ("a mutant naming a property as an invariant is refused",
         {"name": "m", "violates": "Reaches", "old": "x < 2", "new": "x < 3"}),
    ]:
        try:
            check_mutant("SelfTest", _SELF_TEST_SPEC, _SELF_TEST_CFG, fake)
            refused = ""
        except SystemExit as exit_:
            refused = str(exit_.code)
        ok = _report(label, "is not an invariant" in refused, refused or "accepted") and ok

    every = [{"violates": "Small"}, {"violates": "NonNegative"}] + [
        {"violates_property": name} for name in ("Reaches", "StartsAtZero", "OneStep", "AlwaysSmall")
    ]
    claim_cases = [
        ("a claim no mutant breaks is reported", every[1:], _SELF_TEST_CFG, ["no mutant breaks Small"]),
        ("a spec whose every claim has a mutant passes", every, _SELF_TEST_CFG, []),
        ("a .cfg that names no claim is refused, whatever its mutants",
         every, "SPECIFICATION Spec\nINVARIANTS\n", ["its .cfg names no invariant or property, so it checks nothing"]),
    ]
    for label, mutants, cfg, wanted_problems in claim_cases:
        problems = claim_problems({"mutants": mutants}, cfg)
        ok = _report(label, problems == wanted_problems, "; ".join(problems) or "no problem") and ok

    main_cfg = "SPECIFICATION Spec\nINVARIANTS Small TypeOK\n"
    more_cfg = "SPECIFICATION Spec\nINVARIANTS Small NonNegative\n"
    wide = {"violates": "Small", "cfg": "wide"}
    variant_cases: list[tuple[str, dict, dict[str, str], str]] = [
        ("a variant that no mutant runs against is refused",
         {"mutants": [{"violates": "Small"}]}, {"": main_cfg, "wide": main_cfg},
         "no mutant runs against the variant wide"),
        ("a variant that only a survivor runs against is refused",
         {"mutants": [{"violates": "Small"}], "survivors": [{"cfg": "wide"}]},
         {"": main_cfg, "wide": main_cfg}, "no mutant runs against the variant wide"),
        ("a variant with a mutant is accepted", {"mutants": [wide]}, {"": main_cfg, "wide": main_cfg}, ""),
        ("a variant that names no claim is refused",
         {"mutants": [wide]}, {"": main_cfg, "wide": "SPECIFICATION Spec\nINVARIANTS\n"},
         "the variant wide names no invariant or property, so it checks nothing"),
        ("a variant with a claim that no mutant shows can fail is refused",
         {"mutants": [wide]}, {"": main_cfg, "wide": more_cfg},
         "the variant wide checks NonNegative, which no mutant shows can fail"),
        ("a variant's own claim with its own mutant is accepted",
         {"mutants": [wide, {"violates": "NonNegative", "cfg": "wide"}]},
         {"": main_cfg, "wide": more_cfg}, ""),
        ("a mutant naming a variant that has no .cfg is refused",
         {"mutants": [wide]}, {"": main_cfg}, "the variant wide has no .cfg"),
    ]
    for label, entry, configs, wanted_problem in variant_cases:
        problems = variant_problems(entry, configs)
        ok = _report(label, problems == ([wanted_problem] if wanted_problem else []),
                     "; ".join(problems) or "no problem") and ok

    ok = _report("the keyword list is the jar's own", _KEYWORDS == jar_keywords(),
                 ", ".join(sorted(_KEYWORDS ^ jar_keywords())) or "the same") and ok

    with tempfile.TemporaryDirectory(prefix="tla-configs-") as directory:
        root = Path(directory)
        for file_name, text in [("Tiny.cfg", "main"), ("Tiny..cfg", "empty"), ("Tiny.a.b.cfg", "dotted"),
                                ("Tiny.wide.cfg", "wide")]:
            (root / file_name).write_text(text)
        configs, problems = load_configs("Tiny", root)
        ok = _report(
            "a configuration with an empty or dotted variant name is refused, and replaces nothing",
            configs == {"": "main", "wide": "wide"} and len(problems) == 2, "; ".join(problems),
        ) and ok

    found = guards(_GUARD_SPEC, "Spec")
    ok = _report(
        "an action's guards are its conjuncts that read no next state",
        [(guard.action, guard.text) for guard in found] == _FOUR,
        "; ".join(f"{guard.action}: {guard.text}" for guard in found),
    ) and ok
    found_in_comments = [guard.text for guard in guards(_COMMENT_SPEC, "Spec")]
    ok = _report(
        "comments and strings are not read as code",
        found_in_comments == ['/\\ "don\'t" # "do"', "/\\ x = 1"], "; ".join(found_in_comments),
    ) and ok
    renamed = [guard.text for guard in guards(_COMMENT_SPEC.replace("Next", "Step"), "Spec")]
    ok = _report("a next-state relation by another name is found through the temporal formula",
                 renamed == found_in_comments, "; ".join(renamed)) and ok
    wrapped = [(guard.action, guard.text) for guard in guards(_WRAPPER_SPEC, "Spec")]
    ok = _report("a wrapper's own guard is listed, and a property's box is not the relation",
                 wrapped == [("Bounded", "/\\ x < 2")],
                 "; ".join(f"{action}: {text}" for action, text in wrapped)) and ok
    nested_guards = guards(_NESTED_SPEC, "Spec")
    inside = [(guard.action, guard.text) for guard in nested_guards]
    ok = _report(
        "a condition has a row under a quantifier, in an IF's branch, in a LET's body and beside an effect",
        inside == [("Act", "/\\ n > x"), ("Act", "/\\ y < 5"), ("Act", "/\\ m < 9"), ("Other", "/\\ y > 2"),
                   ("Other", "/\\ w = 0"), ("Other", "/\\ z > 0")],
        "; ".join(f"{action}: {text}" for action, text in inside),
    ) and ok
    beside = without_guard(_NESTED_SPEC, nested_guards[3], "Spec") if len(nested_guards) == 6 else ""
    ok = _report(
        "removing a guard that shares its line with others leaves them, each in its column",
        beside == _NESTED_SPEC.replace("Other == /\\ y > 2 /\\ w = 0", "Other == /\\ TRUE  /\\ w = 0"),
        "as wanted" if "/\\ TRUE  /\\ w = 0 /\\ z > 0 /\\ x' = 0" in beside else "the line is not as wanted",
    ) and ok
    # A guard whose place is misread: TRUE would land in the effect on the
    # next line, and the guard would still be there.
    misread = dataclasses.replace(found[3], first=found[3].first + 1, last=found[3].last + 1,
                                  start=found[3].start + 5, end=found[3].end + 5)
    try:
        without_guard(_GUARD_SPEC, misread, "Spec")
        refused = ""
    except SystemExit as exit_:
        refused = str(exit_.code)
    ok = _report("a guard whose place in the text is misread is not removed",
                 refused.startswith("error: replacing Other's guard `/\\ x > 0` by TRUE did not give"),
                 refused or "removed") and ok
    # A guard that is none of the spec's: its TRUE goes into a definition that
    # is no action, and every guard and effect reads as before.
    line = _GUARD_SPEC.split("\n").index("Allowed(a) == a > 0")
    elsewhere = Guard("Act", "/\\ a > 0", line, line + 1, len("Allowed(a) == "), len("Allowed(a) == a > 0"))
    try:
        without_guard(_GUARD_SPEC, elsewhere, "Spec")
        refused = ""
    except SystemExit as exit_:
        refused = str(exit_.code)
    ok = _report("a guard that is not one of the spec's own is not removed",
                 refused.startswith("error: replacing Act's guard `/\\ a > 0` by TRUE did not give"),
                 refused or "removed") and ok
    other_line = "Other == /\\ y > 2 /\\ w = 0 /\\ z > 0 /\\ x' = 0 /\\ UNCHANGED << y, z, w >>"
    for label, becomes, same in [
        ("the spec with one guard replaced by TRUE reads as that",
         "Other == /\\ y > 2 /\\ w = 0 /\\ TRUE  /\\ x' = 0 /\\ UNCHANGED << y, z, w >>", True),
        ("a removal that took the effect beside the guard does not",
         "Other == /\\ y > 2 /\\ w = 0 /\\ TRUE              /\\ UNCHANGED << y, z, w >>", False),
        ("a removal that put TRUE in the effect and left the guard does not",
         "Other == /\\ y > 2 /\\ w = 0 /\\ z > 0 /\\ x' = TRUE /\\ UNCHANGED << y, z, w >>", False),
        ("a removal that changed nothing does not", other_line, False),
        ("a removal that left something SANY cannot parse does not",
         "Other == /\\ y > 2 /\\ w = 0 /\\ TRUE 0 /\\ x' = 0 /\\ UNCHANGED << y, z, w >>", False),
    ]:
        reads = len(nested_guards) == 6 and _is_the_spec_without(
            _NESTED_SPEC, _NESTED_SPEC.replace(other_line, becomes), nested_guards[5], "Spec")
        ok = _report(label, reads == same and other_line in _NESTED_SPEC, "it does" if reads else "it does not") and ok
    # The guard is gone as it should be, and one more thing is changed that
    # is neither a guard nor an effect's text.
    gone_line = "Other == /\\ y > 2 /\\ w = 0 /\\ TRUE  /\\ x' = 0 /\\ UNCHANGED << y, z, w >>"
    for what, old, new in [
        ("a number in an effect's value", "THEN 1 ELSE 0", "THEN 2 ELSE 0"),
        ("the set a quantifier ranges over", "\\E n \\in {1, 2} :", "\\E n \\in {1, 3} :"),
        ("the condition of an IF between two actions", "    /\\ IF x = 0\n", "    /\\ IF x = 1\n"),
        ("a definition that a LET makes", "LET m == x + 1 IN", "LET m == x + 2 IN"),
        ("which action the relation names", "Next == Other \\/ Act\n", "Next == Other \\/ Other\n"),
        ("the initial condition", "Spec == x = 0 /\\ [][Next]", "Spec == x = 1 /\\ [][Next]"),
        ("an assumption", "ASSUME Positive == 1 > 0\n", "ASSUME Positive == 2 > 0\n"),
        ("the variables", "VARIABLES x, y, z, w\n", "VARIABLES x, y, z, w, v\n"),
    ]:
        more = _NESTED_SPEC.replace(other_line, gone_line).replace(old, new)
        reads = len(nested_guards) == 6 and _is_the_spec_without(_NESTED_SPEC, more, nested_guards[5], "Spec")
        ok = _report(f"a removal that also changed {what} does not read as the spec without its guard",
                     not reads and old in _NESTED_SPEC and gone_line in more, "it does" if reads else "it does not") and ok
    # A string's spaces are part of its value.
    commented = guards(_COMMENT_SPEC, "Spec")
    gone = without_guard(_COMMENT_SPEC, commented[1], "Spec") if len(commented) == 2 else ""
    spaced = gone.replace('# "do"', '# "do "')
    ok = _report("a removal that also changed the spaces in a string does not read as the spec without its guard",
                 _is_the_spec_without(_COMMENT_SPEC, gone, commented[1], "Spec") and spaced != gone
                 and not _is_the_spec_without(_COMMENT_SPEC, spaced, commented[1], "Spec"),
                 "it does not" if spaced != gone else "the string is not there") and ok
    with_calls = _GUARD_SPEC.replace("    /\\ x > 0\n    /\\ x' = 0\n", "    /\\ TRUE\n    /\\ x' = 0\n")
    for what, old, new in [
        ("what an action is called with", "    /\\ Through(Same(a))\n", "    /\\ Through(a + 1)\n"),
        ("a definition that a guard names", "Allowed(a) == a > 0\n", "Allowed(a) == a > 1\n"),
        ("fairness", " /\\ \\A a \\in Range : WF_<< x, z >>(Act(a))\n", "\n"),
        ("nothing", "Live == {x}\n", "Live == {x}\n"),
    ]:
        reads = _is_the_spec_without(_GUARD_SPEC, with_calls.replace(old, new), found[3], "Spec")
        ok = _report(f"a removal that also changed {what} "
                     + ("reads" if what == "nothing" else "does not read") + " as the spec without its guard",
                     reads == (what == "nothing") and old in with_calls, "it does" if reads else "it does not") and ok
    beside_true = mutate(_GUARD_SPEC, {"old": _OTHER, "new": "Other ==\n    /\\ TRUE\n    /\\ x > 0\n    /\\ x' = 0\n    /\\ UNCHANGED z\n"},
                         "a TRUE beside a guard")
    try:
        gone = without_guard(beside_true, guards(beside_true, "Spec")[4], "Spec")
    except SystemExit as exit_:
        gone = str(exit_.code)
    ok = _report("a guard beside a conjunct that is TRUE is removed like another",
                 gone == beside_true.replace("    /\\ x > 0\n", "    /\\ TRUE\n"),
                 "as wanted" if gone.count("    /\\ TRUE\n") == 2 else gone[:120]) and ok
    with_constant = mutate(_GUARD_SPEC, {"old": "VARIABLES x, z\n", "new": "CONSTANT Limit\nVARIABLES x, z\n"}, "a constant")
    try:
        guards(mutate(with_constant, {"old": _EFFECT, "new": "    /\\ Limit' = x\n    /\\ x' = 1\n"}, "a primed constant"), "Spec")
        refused = ""
    except SystemExit as exit_:
        refused = str(exit_.code)
    ok = _report("an effect on a declared constant, which is no variable, is refused",
                 refused.startswith("error: Act has `Limit' = x` at line 20: in an action this reads"),
                 refused or "listed") and ok
    narrow = _NESTED_SPEC.replace("/\\ w = 0 /\\ z > 0", "/\\ w  /\\ z > 0")
    try:
        without_guard(narrow, guards(narrow, "Spec")[4], "Spec")
        refused = ""
    except SystemExit as exit_:
        refused = str(exit_.code)
    ok = _report("a guard narrower than TRUE with more after it on its line is not removed by guessing",
                 refused.startswith("error: Other's guard `/\\ w` is narrower than TRUE"), refused or "removed") and ok
    used = action_used_otherwise(_GUARD_SPEC, "Spec")
    ok = _report("the relation uses each action only as a step", used is None, str(used)) and ok
    for label, old, new, wanted_use in _USES:
        used = action_used_otherwise(mutate(_GUARD_SPEC, {"old": old, "new": new}, label), "Spec")
        ok = _report(f"{label}: " + (f"{wanted_use} is used other than as a step" if wanted_use
                                     else "each action is used only as a step"),
                     used == wanted_use, str(used)) and ok
    for label, old, new, wanted_guards in _OTHER_WAYS:
        other_way = [(guard.action, guard.text) for guard in guards(mutate(_GUARD_SPEC, {"old": old, "new": new}, label), "Spec")]
        ok = _report(f"{label} is read", other_way == wanted_guards,
                     "; ".join(f"{action}: {text}" for action, text in other_way)) and ok
    for label, call, wanted_error in [
        ("a formula the spec does not define is refused",
         lambda: guards(_WRAPPER_SPEC, "Missing"), "error: the configurations check Missing"),
        ("configurations that check different formulas are refused",
         lambda: specification_of({"": "SPECIFICATION Spec\n", "wide": "SPECIFICATION Other\n"}),
         "error: the configurations name 2 formulas"),
        ("a configuration that names no formula is refused",
         lambda: specification_of({"": "INIT Start\nNEXT Next\n"}), "error: a configuration does not name one formula"),
    ]:
        try:
            call()
            refused = ""
        except SystemExit as exit_:
            refused = str(exit_.code)
        ok = _report(label, refused.startswith(wanted_error), refused or "accepted") and ok
    ok = _report("the formula is the one every configuration names",
                 specification_of({"": "SPECIFICATION Spec\n", "wide": "SPECIFICATION Spec\n"}) == "Spec", "Spec") and ok
    for label, old, shape, wanted_error in _UNLISTABLE:
        try:
            guards(mutate(_GUARD_SPEC, {"old": old, "new": shape}, label), "Spec")
            refused = ""
        except SystemExit as exit_:
            refused = str(exit_.code)
        ok = _report(f"{label} is refused", refused.startswith(wanted_error), refused or "accepted") and ok
    nested = "Error: The error occurred when TLC was evaluating the nested\nexpressions at the following positions:\n"
    unexpected = ("Error: TLC threw an unexpected exception.\nThis was probably caused by an error in the spec or model.\n"
                  "The exception was a java.lang.RuntimeException\n: ")
    for label, output, decided in [
        ("a run that found no error decided something",
         NO_ERROR + "\n9 states generated, 4 distinct states found, 0 states left on queue.\n", True),
        ("a run that TLC stopped with states still to visit decided nothing",
         NO_ERROR + "\n3 states generated, 3 distinct states found, 1 states left on queue.\n", False),
        ("a run that says only that it completed decided nothing", NO_ERROR + "\n", False),
        ("a run that named a violated claim decided something", "Error: Invariant Small is violated.\n", True),
        ("a run that could not evaluate an expression decided something",
         "Error: Attempted to apply Head to the empty sequence.\n" + nested, True),
        ("a run that indexed a tuple out of bounds decided something",
         unexpected + "Attempted to access index 3 of tuple\n<<5, 6>>\nwhich is out of bounds.\n"
         "Error: TLC was unable to fingerprint.\n", True),
        ("a run that applied a function with a listed domain outside it decided something",
         unexpected + "Attempted to apply function:\n(1 :> 1 @@ 2 :> 2)\n"
         "to argument 3, which is not in the domain of the function.\n" + nested, True),
        ("a run that compared a set TLC cannot enumerate decided nothing",
         unexpected + "Attempted to compare overridden value Nat with non-set:\n{0}\n" + nested, False),
        ("a run that applied a function outside its domain decided something",
         unexpected + "In applying the function\n<<1, 2>>,\nthe first argument is:\n5\nwhich is not in its domain.\n"
         + nested, True),
        ("a run that built a set larger than TLC allows decided nothing",
         unexpected + "Attempted to apply the operator overridden by the Java method\n"
         "public static tlc2.value.impl.IntValue tlc2.module.FiniteSets.Cardinality(tlc2.value.impl.Value),\n"
         "but it produced the following error:\nAttempted to construct a set with too many elements (>1000000).\n"
         + nested, False),
        ("a run whose arithmetic overflowed decided nothing",
         "Error: Overflow when computing 2147483647+1\n" + nested, False),
        ("a run that could not enumerate a set decided nothing",
         unexpected + "Attempted to compute the value of an expression of\n"
         "form CHOOSE x \\in S: P, but S was not enumerable.\n" + nested, False),
        ("a run that stopped on an error this does not know decided nothing",
         "Error: Something TLC has not said before.\n" + nested, False),
        ("a run whose printed state quotes TLC's own words decided nothing",
         "Error: current state is not a legal state\nWhile working on the initial state:\n"
         "/\\ x = \"Cannot cast\"\n/\\ y = null\n", False),
        ("a run whose printed state is one of TLC's lines, in quotes, decided nothing",
         "Error: current state is not a legal state\nWhile working on the initial state:\n"
         "x = \"which is out of bounds.\"\n", False),
        ("a run that ran out of memory, and then could not evaluate, decided nothing",
         "Error: Java ran out of memory.\nError: Attempted to apply Head to the empty sequence.\n", False),
        ("a run that named two violated properties decided something",
         "Error: Temporal properties Settles and Stays were violated.\n", True),
        ("a run that named three violated properties decided something",
         "Error: Temporal properties Settles, Stays, and Ends were violated.\n", True),
        ("a run that named no violated property decided something",
         "Error: Temporal properties were violated.\n", True),
        ("a run that overflowed the stack decided nothing",
         "Error: This was a Java StackOverflowError. It was probably the result\n", False),
        ("a run whose assumption is false decided nothing",
         "Error: Assumption line 4, col 8 to line 4, col 12 of module Tiny is false.\n", False),
        ("a run that timed out decided nothing", "Error: TLC did not finish in 1200 seconds.\n", False),
        ("a run that could not parse decided nothing", "Error: Parsing or semantic analysis failed.\n", False),
        ("a run that ran out of memory decided nothing", "Error: Java ran out of memory.\n", False),
        ("a run that printed nothing decided nothing", "", False),
    ]:
        ok = _report(label, (inconclusive(output) is None) == decided, inconclusive(output) or "decided") and ok
    # A spec can stop TLC's search itself, and TLC then says what it says of a
    # search that finished. This is TLC's own output for one that did.
    stopped = run_tlc(_STOPPING_SPEC, "SPECIFICATION Spec\nINVARIANT Small\n", "Stops", workers="1")
    ok = _report("a search that the spec stopped is not one that found no error",
                 NO_ERROR in stopped.splitlines() and not searched_every_state(stopped)
                 and inconclusive(stopped) == _STOPPED_EARLY
                 and verdict(stopped, invariant="Small", prop=None) == (ERROR, _STOPPED_EARLY),
                 inconclusive(stopped) or "decided") and ok
    finished = run_tlc(_STOPPING_SPEC.replace('TLCSet("exit", TRUE)', "TRUE"),
                       "SPECIFICATION Spec\nINVARIANT Small\n", "Stops", workers="1")
    ok = _report("the same search, not stopped, is one that found no error",
                 searched_every_state(finished) and verdict(finished, invariant="Small", prop=None)[0] == SURVIVED,
                 inconclusive(finished) or "decided") and ok
    removed = without_guard(_GUARD_SPEC, found[2], "Spec")
    ok = _report(
        "removing a guard replaces its lines, and only those, by TRUE",
        removed == _GUARD_SPEC.replace("    /\\ \\/ x = 1\n       \\/ x = 2\n", "    /\\ TRUE\n"),
        "as wanted" if "/\\ TRUE" in removed else "TRUE is not there",
    ) and ok

    guard_cfgs = {"": main_cfg, "wide": more_cfg}
    digest = inputs_digest(_GUARD_SPEC, guard_cfgs)

    every_state = '[states]\n"Tiny.cfg" = 1\n"Tiny.wide.cfg" = 1\n'

    def table(rows: list[str], head: str = digest, states: str = every_state) -> str:
        return f'inputs_sha256 = "{head}"\n' + states + "".join(
            f"[[guard]]\naction = \"{guard.action}\"\ntext = \'\'\'{guard.text}\'\'\'\n{row}\n"
            for guard, row in zip(found, rows, strict=False)
        )

    kills = 'breaks = "Small"'
    explained = 'breaks = "nothing"\nreaches = "no new state"\nwhy = "it only enables the action"'
    table_cases: list[tuple[str, dict, str | None, str]] = [
        ("a spec with no guard table is refused", {}, None, "it has no Tiny.guards.toml"),
        ("a spec that says why it is unswept is accepted", {"unswept": "predates the table"}, None, ""),
        ("`unswept` with no reason is refused", {"unswept": " "}, None, "`unswept` gives no reason"),
        ("a table beside `unswept` is refused",
         {"unswept": "predates the table"}, table([kills] * 4), "it has a guard table and says it is unswept"),
        ("a table swept against another spec is refused",
         {}, table([kills] * 4, "0" * 64), "its guard table was swept against another spec"),
        ("a table that misses a guard is refused",
         {}, table([kills] * 3), "its guard table does not list the spec's guards"),
        ("a table that does not give the states each configuration reaches is refused",
         {}, table([kills] * 4, states=""), "does not give the distinct states each configuration reaches"),
        ("a table that gives the states of one configuration and not the other is refused",
         {}, table([kills] * 4, states='[states]\n"Tiny.cfg" = 1\n'),
         "does not give the distinct states each configuration reaches"),
        ("a table that gives a configuration no states is refused",
         {}, table([kills] * 4, states='[states]\n"Tiny.cfg" = 0\n"Tiny.wide.cfg" = 1\n'),
         "does not give the distinct states each configuration reaches"),
        ("a guard that breaks nothing and says no why is refused",
         {}, table([kills, 'breaks = "nothing"\nreaches = "new states"', kills, kills]), "breaks nothing and gives no reason"),
        ("a guard that breaks nothing and does not say what it reaches is refused",
         {}, table([kills, 'breaks = "nothing"\nwhy = "it only enables the action"', kills, kills]),
         "does not say whether it reaches a new state"),
        ("a guard that breaks nothing and says something else of what it reaches is refused",
         {}, table([kills, 'breaks = "nothing"\nreaches = "some"\nwhy = "it only enables the action"', kills, kills]),
         "does not say whether it reaches a new state"),
        ("a guard that breaks a claim and says what it reaches is refused",
         {}, table([kills, 'breaks = "Small"\nreaches = "new states"', kills, kills]),
         "`reaches` is only for a guard that breaks nothing"),
        ("a guard said to break something that is not a claim is refused",
         {}, table([kills, 'breaks = "Limit"', kills, kills]), "Limit is not a claim its configuration checks"),
        ("a guard that breaks a claim only a variant checks must name the variant",
         {}, table([kills, 'breaks = "NonNegative"', kills, kills]), "NonNegative is not a claim"),
        ("a guard naming a variant that has no .cfg is refused",
         {}, table([kills, 'breaks = "Small"\ncfg = "narrow"', kills, kills]), "the variant narrow has no .cfg"),
        ("a guard that breaks only TypeOK and says no why is refused",
         {}, table([kills, 'breaks = "TypeOK"', kills, kills]), "breaks TypeOK and gives no reason"),
        ("a guard that breaks nothing and names a variant is refused",
         {}, table([kills, explained + '\ncfg = "wide"', kills, kills]),
         "`cfg` is not for a guard that breaks nothing"),
        ("a guard that leaves an expression undefined only in a variant names it",
         {}, table([kills, explained, 'breaks = "NonNegative"\ncfg = "wide"',
                    'breaks = "evaluation"\ncfg = "wide"\nwhy = "it keeps Head defined"']), ""),
        ("a guard that leaves an expression undefined in a variant that has no .cfg is refused",
         {}, table([kills, explained, 'breaks = "NonNegative"\ncfg = "wide"',
                    'breaks = "evaluation"\ncfg = "narrow"\nwhy = "it keeps Head defined"']),
         "the variant narrow has no .cfg"),
        ("a complete table is accepted",
         {}, table([kills, explained, 'breaks = "NonNegative"\ncfg = "wide"',
                    'breaks = "evaluation"\nwhy = "it keeps Head defined"']), ""),
    ]
    for label, entry, table_text, wanted_problem in table_cases:
        problems = guard_problems("Tiny", entry, _GUARD_SPEC, guard_cfgs, table_text)
        passed = (not problems) if not wanted_problem else any(wanted_problem in each for each in problems)
        ok = _report(label, passed, "; ".join(problems) or "no problem") and ok
    # A configuration can have TLC check something other than the spec's text:
    # another relation, fewer behaviors, or another definition under the same
    # name. A table of the text says nothing about such a run.
    for label, extra, wanted_problem in [
        ("a configuration that names its own next-state relation is refused",
         "INIT Start\nNEXT Other\n", "has a INIT section"),
        ("a configuration with a constraint is refused", "CONSTRAINT Allowed\n", "has a CONSTRAINT section"),
        ("a configuration that replaces the relation is refused", "CONSTANT Next <- Other\n", "replaces Next"),
        ("a configuration that replaces a definition a guard reads is refused",
         "CONSTANTS\n    Never = 5\n    Allowed <- Same\n", "replaces Allowed"),
        ("a configuration that sets a name the spec defines is refused", "CONSTANT Next = FALSE\n", "replaces Next"),
        ("a configuration that sets a definition for some arguments is refused",
         "CONSTANT Allowed(1) = FALSE\n", "has an entry that is not `Name = value`"),
        ("a configuration that replaces a definition inside a module is refused",
         "CONSTANT Nat <- [Naturals] Range\n", "has an entry that is not `Name = value`"),
        ("a configuration that gives a constant a set with nothing between two commas is refused",
         "CONSTANT Limit = {a,, b}\n", "has an entry that is not `Name = value`"),
        ("a configuration that gives a value to something that is no name is refused",
         "CONSTANT 3 = 4\n", "has an entry that is not `Name = value`"),
        ("a configuration with a constant and neither `=` nor `<-` after it is refused",
         "CONSTANT Limit Depth Range\n", "has an entry that is not `Name = value`"),
        ("a configuration that replaces a constant by something that is no name is refused",
         "CONSTANT Limit <- 3\n", "has an entry that is not `Name = value`"),
        ("a configuration whose set has values with no comma between them is refused",
         "CONSTANT Limit = {a b c}\n", "has an entry that is not `Name = value`"),
        ("a configuration that gives a constant no value is refused",
         "CONSTANT Limit =\n", "has an entry that is not `Name = value`"),
        ("a configuration that replaces a constant by nothing is refused",
         "CONSTANT Limit <-\n", "has an entry that is not `Name = value`"),
        ("a configuration whose set of values never closes is refused",
         "CONSTANT Limit = {a, b\n", "has an entry that is not `Name = value`"),
        ("a configuration that replaces a definition after a set of sets, strings and a negative number is refused",
         'CONSTANTS\n    Limit = {a, {b, {}}, "s t", -1}\n    Depth = 2\n    Allowed <- Same\n', "replaces Allowed"),
        ("a configuration that gives constants such values is accepted",
         'CONSTANTS\n    Limit = {a, {b, {}}, "s t", -1}\n    Depth = 2\n    Wide <- Range\n', ""),
        ("a configuration that replaces a name the spec does not define is accepted", "CONSTANT Nat <- Range\n", ""),
        ("a configuration that puts a definition that reads the state in a name's place is refused",
         "CONSTANT Nat <- Live\n", "puts Live in place of a name"),
        ("a configuration that puts an action in a name's place is refused",
         "CONSTANT Nat <- Other\n", "puts Other in place of a name"),
        ("a configuration that puts something nothing defines in a name's place is refused",
         "CONSTANT Nat <- Missing\n", "puts Missing in place of a name"),
        ("a configuration that sets a constant is accepted", "CONSTANT Limit = 3\n", ""),
    ]:
        other_cfgs = {"": main_cfg + extra, "wide": more_cfg}
        if "accepted" not in label:
            # The same in a variant alone, which is a configuration like any other.
            in_variant = {"": main_cfg, "wide": more_cfg + extra}
            whole = table([kills, explained, 'breaks = "NonNegative"\ncfg = "wide"',
                           'breaks = "evaluation"\nwhy = "it keeps Head defined"'],
                          inputs_digest(_GUARD_SPEC, in_variant))
            problems = guard_problems("Tiny", {}, _GUARD_SPEC, in_variant, whole)
            ok = _report(f"{label}, in a variant too", any(wanted_problem in each for each in problems),
                         "; ".join(problems) or "no problem") and ok
        complete = table([kills, explained, 'breaks = "NonNegative"\ncfg = "wide"',
                          'breaks = "evaluation"\nwhy = "it keeps Head defined"'],
                         inputs_digest(_GUARD_SPEC, other_cfgs))
        problems = guard_problems("Tiny", {}, _GUARD_SPEC, other_cfgs, complete)
        passed = (not problems) if not wanted_problem else any(wanted_problem in each for each in problems)
        ok = _report(label, passed, "; ".join(problems) or "no problem") and ok

    for label, output, wanted_claim in [
        ("a violated invariant is named", "Error: Invariant Small is violated.\n", "Small"),
        ("a violated temporal property is named", "Error: Temporal property Reaches was violated.\n", "Reaches"),
        ("an evaluation error names no claim", "Error: TLC threw an unexpected exception.\n", None),
    ]:
        ok = _report(label, violated_claim(output) == wanted_claim, str(violated_claim(output))) and ok

    with tempfile.TemporaryDirectory(prefix="tla-manifest-") as directory:
        root = Path(directory)
        (root / "code.py").write_text("")
        state_cases: list[tuple[str, dict, bool]] = [
            ("a planned spec whose code exists is refused", {"state": "planned", "code": ["code.py"]}, True),
            ("a planned spec whose code is absent is accepted", {"state": "planned", "code": ["absent.py"]}, False),
            ("an implemented spec with a missing test is refused",
             {"state": "implemented", "code": ["code.py"], "tests": ["absent.py"]}, True),
            ("an implemented spec whose files exist is accepted",
             {"state": "implemented", "code": ["code.py"], "tests": ["code.py"]}, False),
            ("an orphaned spec whose replacement is here is refused",
             {"state": "orphaned", "retire_with": "Next", "code": ["absent.py"]}, True),
            ("an orphaned spec awaiting its replacement is accepted",
             {"state": "orphaned", "retire_with": "Later", "code": ["absent.py"]}, False),
            ("an orphaned spec whose code still exists is refused",
             {"state": "orphaned", "retire_with": "Later", "code": ["code.py"]}, True),
            ("an orphaned spec that names no deleted code is refused",
             {"state": "orphaned", "retire_with": "Later"}, True),
        ]
        for case, state_entry, state_refused in state_cases:
            problems = state_problems(state_entry, {"Next"}, root)
            ok = _report(case, bool(problems) == state_refused, "; ".join(problems) or "no problem") and ok
    return ok


def _report(label: str, passed: bool, detail: str) -> bool:
    mark = "ok  " if passed else "FAIL"
    print(f"    {mark}      self-test: {label} ({detail})", file=sys.stdout if passed else sys.stderr)
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--self-test", action="store_true", help="only test the runner itself")
    parser.add_argument("--only", help="check one spec's entry")
    args = parser.parse_args(argv)
    # Failures go to stderr, the rest to stdout: keep the two in order in a log.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    if not jar().is_file():
        print(f"error: {jar()} not found", file=sys.stderr)
        return 1
    print(jar_identity())
    print("=== mutant runner self-test ===")
    if not self_test():
        return 1
    if args.self_test:
        return 0

    manifest = tomllib.loads((PROOFS / "manifest.toml").read_text())
    on_disk = {path.stem for path in PROOFS.glob("*.tla")}
    stray = sorted(
        path.name
        for pattern in ("*.cfg", "*.guards.toml")
        for path in PROOFS.glob(pattern)
        if path.name.split(".")[0] not in on_disk
    )
    if stray:
        print(f"error: files with no spec: {', '.join(stray)}", file=sys.stderr)
        return 1
    if args.only and args.only not in manifest.keys() & on_disk:
        print(f"error: --only {args.only}: no such spec in proofs/ and its manifest", file=sys.stderr)
        return 1
    ok = True
    for name in sorted(on_disk - manifest.keys()):
        print(f"error: {name}.tla has no entry in proofs/manifest.toml", file=sys.stderr)
        ok = False
    for name in sorted(manifest.keys() - on_disk):
        print(f"error: proofs/manifest.toml names {name}, which has no .tla", file=sys.stderr)
        ok = False
    for name in sorted(manifest.keys() & on_disk):
        if args.only and name != args.only:
            continue
        entry = manifest[name]
        print(f"=== manifest: {name} ({entry.get('state')}) ===")
        spec_text = (PROOFS / f"{name}.tla").read_text()
        configs, problems = load_configs(name)
        problems += state_problems(entry, on_disk)
        if entry.get("state") != "orphaned":
            problems += claim_problems(entry, configs[""])
            problems += variant_problems(entry, configs)
            table = PROOFS / f"{name}.guards.toml"
            problems += guard_problems(
                name, entry, spec_text, configs, table.read_text() if table.exists() else None
            )
        for problem in problems:
            print(f"    STATE     {name}: {problem}", file=sys.stderr)
            ok = False
        for mutant in entry.get("mutants", []):
            cfg_text = configs.get(mutant.get("cfg", ""))
            ok = cfg_text is not None and check_mutant(name, spec_text, cfg_text, mutant) and ok
        for survivor in entry.get("survivors", []):
            cfg_text = configs.get(survivor.get("cfg", ""))
            ok = cfg_text is not None and check_survivor(name, spec_text, cfg_text, survivor) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
