# shellcheck shell=bash
# The ordered schema a STANDALONE ClickHouse node needs, derived rather than listed.
#
# Every standalone node -- AWS Paris, AWS Stockholm, Azure -- needs the same set,
# and each script used to carry its own copy of it. Two carried a literal
# `006`+`009` and one globbed `clickhouse/00*.sql`, which stops matching the
# moment a migration crosses a digit boundary. Both were wrong the same way and
# for the same reason: the list was written once and the directory kept moving.
# 010 through 013 landed and no node built by any of those three scripts got
# them.
#
# That failure is silent where it hurts. clickhouse/013_*.sql adds the
# workspace_id column the drain inserts (clickhouse/ingest_operational_outbox.py),
# an un-migrated node REJECTS that insert, and shard failures are contained
# (clickhouse/ingest_operational_outbox_postgres.py) -- so the unit stays active,
# reports healthy, and delivers nothing.
#
# WHICH FILES, AND WHY NOT THE OTHERS
# `*_single_node.sql` is the naming the repo already uses to mark "standalone
# variant of this migration". The rest are excluded because they target a
# different topology or a different dataset:
#   001-002  provider-benchmark schemas, not operational analytics
#   003-005, 007-008, 010, 012  the Keeper / ON CLUSTER replicated topology,
#            which is GCP's cluster; a standalone node has no Keeper to
#            coordinate with and these fail or create the wrong engine there.
# Adding a new standalone migration requires nothing here: name it
# `*_single_node.sql` and every node picks it up.

single_node_migrations() {
  local root="$1"
  local -a found=()
  local path

  while IFS= read -r path; do
    [ -n "$path" ] && found+=("$path")
  done < <(find "$root/clickhouse" -maxdepth 1 -name '*_single_node.sql' -print 2>/dev/null | LC_ALL=C sort)

  if [ "${#found[@]}" -eq 0 ]; then
    echo "no clickhouse/*_single_node.sql found under ${root}" >&2
    echo "a rename would otherwise apply an EMPTY schema and look like success" >&2
    return 1
  fi

  # Every consumer of this set connects to the `default` database: the AWS
  # control plane and drain installer both default CH_DATABASE to `default`, and
  # so does Azure. Only GCP uses `tr`, and GCP runs the ON CLUSTER migrations
  # instead of these. So a database-qualified statement in this set would be
  # applied somewhere nothing reads -- which is worse than skipping it, because
  # the apply SUCCEEDS. 011 and 013 shipped qualified `tr.` for exactly that
  # reason and nobody noticed; this refuses instead.
  local qualified
  qualified="$(
    python3 - "${found[@]}" <<'PY'
import pathlib
import re
import sys

pattern = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\.",
    re.IGNORECASE,
)
for name in sys.argv[1:]:
    text = pathlib.Path(name).read_text(encoding="utf-8")
    for database in pattern.findall(text):
        print(f"{pathlib.Path(name).name}: {database}.")
PY
  )"
  if [ -n "$qualified" ]; then
    echo "database-qualified statements in the single-node schema set:" >&2
    echo "$qualified" >&2
    echo "standalone nodes use the 'default' database; qualifying sends these" >&2
    echo "somewhere no drain reads, and the apply still reports success" >&2
    return 1
  fi

  printf '%s\n' "${found[@]}"
}
