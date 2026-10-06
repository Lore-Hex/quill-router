#!/usr/bin/env bash
# Model-check every TLA+ spec in this directory.
#
# Each <Spec>.tla must have a matching <Spec>.cfg naming its INVARIANT and any
# PROPERTY to check. A spec without a .cfg is a hard error rather than a skip:
# silently not checking something that looks checked is the failure mode this
# whole directory exists to avoid.
#
# -deadlock disables TLC's deadlock detection. These are reactive models where
# "no action is enabled" is a legitimate terminal state (every lease closed,
# clock exhausted), not a bug. Safety invariants and temporal properties are
# still fully checked.
#
# -workers auto because this script used to run TLC single-threaded, which was
# not a deliberate choice — it is just TLC's default. RegionalQuotaLease grew a
# succession action and about a quarter more states (4,634,802 -> 5,844,105),
# and at the default one worker it had not finished after fifteen minutes on
# the machine this was measured on. With workers it completes, in a few
# minutes rather than the "under three" this comment used to promise: the
# four -workers auto runs behind this commit (JDK 21) took between 1m26s and
# 4m06s, and one -workers 2 run (JDK 17) took 3m47s, all on an 8-core Apple
# M2, all 5,844,105 states / 1,292,173 distinct. The spread is machine load
# rather than the search. Nothing about the
# RESULT changes with the worker count — an exhaustive breadth-first search
# visits the same states either way, and the counts above are identical.
#
# One thing does change: a parallel search reports the first counterexample
# any worker hands back, which need not be a shortest one. Re-run a failing
# mutant at -workers 1 before quoting a trace length anywhere.
#
# The model checker is a pinned input. tla2tools.jar is in the repository and
# its sha256 is in tla2tools.jar.sha256, which this script checks before it
# runs anything. It is TLC2 Version 2026.10.04.025638 (rev: 1813307): the
# tlaplus project's v1.8.0 pre-release as built on 2026-10-04. That release's
# asset is rebuilt upstream, so its URL returns different bytes on different
# days, and CI used to run whichever build it had cached first.
#
# To move to another build:
#   1. Try it: `TLA_TOOLS_JAR=/path/to/new/tla2tools.jar ./proofs/check.sh`
#      runs every spec with it, without the digest check. It must pass.
#   2. Replace the jar and its pin, from the repository root:
#        cp /path/to/new/tla2tools.jar proofs/tla2tools.jar
#        (cd proofs && shasum -a 256 tla2tools.jar > tla2tools.jar.sha256)
#      and write its version above: `java -cp proofs/tla2tools.jar tlc2.TLC`
#      prints it on its first line.
#   3. Run `./proofs/check.sh` again, now with the check. CI runs it on the
#      pull request that changes the jar, as it does on every pull request.
set -euo pipefail

cd "$(dirname "$0")"
JAR="${TLA_TOOLS_JAR:-tla2tools.jar}"

if [[ ! -f "$JAR" ]]; then
  echo "error: $JAR not found. proofs/tla2tools.jar is part of the repository; TLA_TOOLS_JAR names another." >&2
  exit 1
fi
digest="$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$JAR")"
if [[ -z "${TLA_TOOLS_JAR:-}" ]]; then
  pinned="$(cut -d' ' -f1 tla2tools.jar.sha256)"
  if [[ "$digest" != "$pinned" ]]; then
    echo "error: tla2tools.jar has sha256 $digest; tla2tools.jar.sha256 pins $pinned." >&2
    echo "       To move to another build, see the top of this script." >&2
    exit 1
  fi
  echo "tla2tools.jar: sha256 $digest (pinned)"
else
  echo "$JAR: sha256 $digest (TLA_TOOLS_JAR, not the pinned jar)"
fi

shopt -s nullglob
specs=(*.tla)
if (( ${#specs[@]} == 0 )); then
  echo "error: no .tla specs found; this script should not be silently vacuous." >&2
  exit 1
fi

failed=0
for spec in "${specs[@]}"; do
  name="${spec%.tla}"
  cfg="${name}.cfg"
  if [[ ! -f "$cfg" ]]; then
    echo "error: $spec has no $cfg — every spec must declare what is checked." >&2
    failed=1
    continue
  fi

  # A .cfg naming only a SPECIFICATION checks nothing while looking checked —
  # TLC would explore the state space and report success having verified no
  # claim at all. Require at least one INVARIANT or PROPERTY.
  if ! grep -qE '^[[:space:]]*(INVARIANT|INVARIANTS|PROPERTY|PROPERTIES)\b' "$cfg"; then
    echo "error: $cfg declares no INVARIANT or PROPERTY — it would pass vacuously." >&2
    failed=1
    continue
  fi

  echo "=== TLC: $name ==="
  log="$(mktemp)"
  # Write the log first and grep the FILE. Piping straight into `grep -q` makes
  # grep exit on first match, which SIGPIPEs tee/java and — under `pipefail` —
  # reports a passing check as a failure.
  java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC \
      -deadlock -workers auto -config "$cfg" "$spec" >"$log" 2>&1 || true
  cat "$log"
  if grep -qE '^Model checking completed\. No error has been found\.' "$log"; then
    grep -E 'states generated' "$log" | tail -1
    echo "    OK"
  else
    echo "    FAILED — counterexample or error above" >&2
    failed=1
  fi
done

exit "$failed"
