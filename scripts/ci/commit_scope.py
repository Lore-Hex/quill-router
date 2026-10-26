"""Path-aware job selection for ci.yml.

Two subcommands, both stdlib only so the runner's python3 can execute them
before `uv sync`:

  classify   reads `git diff --no-renames --raw BASE HEAD` on stdin and prints
             `true` when every record adds, modifies or deletes a regular file
             that is published catalog data, and `false` otherwise. An empty
             diff is `false`: nothing changed is not the same as only data
             changed, and the complete matrix is the safe default for a
             comparison that produced no records. A rename or copy record, a
             symlink, submodule or executable mode, or a line that is not a
             raw record is `false`.
  summarize  reads the `needs` context of the summary job (JSON in NEEDS)
             and the scope output (DATA_ONLY) and exits non-zero unless every
             job succeeded, allowing a skipped job only when it is one of the
             jobs ci.yml skips for a data-only commit AND the commit was
             data-only. A heavy job skipped on a code commit, a failed scope
             job, or a job in any other state fails the run.

The hourly price refresh publishes commits that touch only DATA_ONLY_PATHS
and gates publication on the ci.yml run of that commit. Every job in
SKIPPED_FOR_DATA_ONLY reads none of those files (Go store tests, TLA+ model
checks, the frozen-main settle oracle with its fixture catalog, the BYOK
write-format diff, the frontend build, the Spanner conformance suites and
the coverage floor over the Python suite). The jobs that do read them
(lint's catalog history check, the production import, both Python suite
clocks and the browser smoke) run on every commit. tests/test_ci_workflow.py
holds the workflow text to these two lists, and holds DATA_ONLY_PATHS to the
files refresh-prices.yml stages.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys

# Every file the refresh commits except the two that are code or Go test
# input: scripts/pricing/parsers/ (self-healed parser modules) and
# fastpath/testdata/model_alias_vectors_v1.json (read by the fastpath job's Go
# tests). A refresh commit that touches either runs the complete matrix. The
# social-card directory admits only what scripts/generate_provider_og.py
# writes: one PNG per provider and its manifest.
DATA_ONLY_PATHS: tuple[str, ...] = (
    "src/trusted_router/data/openrouter_snapshot.json",
    "src/trusted_router/data/speech_pricing.json",
    "src/trusted_router/data/transcription_pricing.json",
    "src/trusted_router/data/provider_routing_evidence.json",
    "src/trusted_router/data/provider_models/*.json",
    "src/trusted_router/data/model_changes.jsonl",
    "src/trusted_router/data/model_changes_state.json",
    "src/trusted_router/static/og/providers/*.png",
    "src/trusted_router/static/og/providers/manifest.json",
)

# Jobs whose `if` in ci.yml is DATA_ONLY_CONDITION. Keep in step with the
# workflow; test_ci_workflow derives the list from the workflow text and
# compares.
SKIPPED_FOR_DATA_ONLY: frozenset[str] = frozenset({
    "frontend",
    "spanner-emulator",
    "coverage",
    "proof-oracle",
    "byok-format-ordering",
    "fastpath",
    "fastpath-spanner",
    "fastpath-walks",
    "proofs",
})

DATA_ONLY_CONDITION = "needs.scope.outputs.data_only != 'true'"

# One record of `git diff --raw` without -z: modes, abbreviated or full object
# ids, a status letter (a rename or copy carries a score, R100, which this
# does not match and so classifies false) and the path. Git quotes paths with
# unusual bytes in double quotes; a quoted path matches no data pattern.
RAW_RECORD = re.compile(
    r"^:(?P<old>[0-7]{6}) (?P<new>[0-7]{6}) [0-9a-f]+\.* [0-9a-f]+\.* (?P<status>[A-Z])\t(?P<path>[^\t]+)$"
)
ABSENT = "000000"
REGULAR_FILE = "100644"
DATA_STATUSES = frozenset({"A", "M", "D"})


def is_data_path(path: str) -> bool:
    path = path.strip()
    if not path or path.startswith("/") or ".." in path.split("/"):
        return False
    return any(
        fnmatch.fnmatchcase(path, pattern) and path.count("/") == pattern.count("/")
        for pattern in DATA_ONLY_PATHS
    )


def is_data_change(record: str) -> bool:
    """True for a raw record that adds, modifies or deletes a regular data file."""
    match = RAW_RECORD.match(record.rstrip("\n"))
    if match is None:
        return False
    modes = {match["old"], match["new"]}
    return (
        match["status"] in DATA_STATUSES
        and modes <= {ABSENT, REGULAR_FILE}
        and is_data_path(match["path"])
    )


def classify(records: list[str]) -> bool:
    """True only for a non-empty diff whose every record is a data change."""
    records = [record for record in records if record.strip()]
    return bool(records) and all(is_data_change(record) for record in records)


def summarize(needs: dict[str, dict[str, object]], data_only: str) -> list[str]:
    """Return the reasons the run must fail; an empty list means success."""
    failures: list[str] = []
    scope = needs.get("scope", {}).get("result")
    if scope != "success":
        failures.append(f"scope: {scope}")
    for job, info in sorted(needs.items()):
        if job == "scope":
            continue
        result = info.get("result")
        if result == "success":
            continue
        if result == "skipped" and job in SKIPPED_FOR_DATA_ONLY and data_only == "true":
            continue
        failures.append(f"{job}: {result}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("classify", help="stdin: git diff --no-renames --raw; stdout: true|false")
    sub.add_parser("summarize", help="env NEEDS (JSON), DATA_ONLY; exit 1 on any failure")
    args = parser.parse_args(argv)
    if args.command == "classify":
        print("true" if classify(sys.stdin.read().splitlines()) else "false")
        return 0
    needs = json.loads(os.environ["NEEDS"])
    data_only = os.environ.get("DATA_ONLY", "")
    for job, info in sorted(needs.items()):
        print(f"{job}: {info.get('result')}")
    print(f"data_only: {data_only or '<unset>'}")
    failures = summarize(needs, data_only)
    for failure in failures:
        print(f"::error title=CI job did not succeed::{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
