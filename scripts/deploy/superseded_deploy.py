"""May a superseded control-plane deploy still ship?

deploy.yml runs on pushes to main that touch DEPLOY_PATHS, and a run whose
commit is no longer main's head normally skips its production steps, leaving
the deploy to the newer push's run. A newer commit that touches none of
DEPLOY_PATHS starts no run of its own, though, so that skip would leave main
undeployed until someone dispatches by hand.

    superseded_deploy.py --repo OWNER/NAME --base DEPLOYED_SHA --head MAIN_SHA

Exit 0 when HEAD is a fast-forward of BASE and no file changed between them
matches DEPLOY_PATHS. DEPLOY_PATHS covers every input of the image and of the
deploy itself (tests pin it to the Dockerfile and to the trigger), so deploying
BASE then ships exactly what main would. Exit 1 otherwise, including whenever
the answer is uncertain (a diverged history, a truncated file list, or a failed
API call), so the newer run deploys instead.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable

# Must equal `on.push.paths` in .github/workflows/deploy.yml (a test pins it).
DEPLOY_PATHS = (
    "src/**",
    "scripts/deploy/**",
    "scripts/entrypoint.sh",
    "frontend/src/**",
    "Dockerfile",
    ".gcloudignore",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/deploy.yml",
)

# GitHub's compare API returns at most 300 files.
_COMPARE_FILE_LIMIT = 300


def touches_deploy_paths(files: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for name in files for pattern in DEPLOY_PATHS)


def changed_files(compare: dict) -> list[str] | None:
    """Every path a compare touched, or None when the answer is incomplete."""

    if compare.get("status") != "ahead":
        return None
    entries = compare.get("files")
    if not isinstance(entries, list) or len(entries) >= _COMPARE_FILE_LIMIT:
        return None
    names: list[str] = []
    for entry in entries:
        names.append(str(entry["filename"]))
        if entry.get("previous_filename"):
            names.append(str(entry["previous_filename"]))
    return names


def may_deploy(compare: dict) -> bool:
    files = changed_files(compare)
    return files is not None and not touches_deploy_paths(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    result = subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are commit SHAs
        ["gh", "api", f"repos/{args.repo}/compare/{args.base}...{args.head}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        print(f"superseded_deploy: compare failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    try:
        compare = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("superseded_deploy: compare answered with something other than JSON", file=sys.stderr)
        return 1
    files = changed_files(compare)
    if files is None:
        print(
            f"superseded_deploy: cannot list the changes ({compare.get('status')}); not deploying",
            file=sys.stderr,
        )
        return 1
    deploy_relevant = sorted(name for name in files if touches_deploy_paths([name]))
    if deploy_relevant:
        print(f"superseded_deploy: newer commits change deploy paths: {', '.join(deploy_relevant[:10])}")
        return 1
    print(f"superseded_deploy: {len(files)} newer file(s), none on a deploy path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
