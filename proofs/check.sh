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
set -euo pipefail

cd "$(dirname "$0")"
JAR="${TLA_TOOLS_JAR:-tla2tools.jar}"

if [[ ! -f "$JAR" ]]; then
  echo "error: $JAR not found. Download tla2tools.jar into proofs/ or set TLA_TOOLS_JAR." >&2
  exit 1
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
      -deadlock -config "$cfg" "$spec" >"$log" 2>&1 || true
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
