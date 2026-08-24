#!/usr/bin/env python3
"""Alert when a cloud has been running the same code for too long.

WHY

Each cloud is a standalone TrustedRouter. GCP redeploys on every merge; AWS and
Azure deploy only when somebody runs a script by hand. That asymmetry is fine
-- staggering deploys is deliberate, and one cloud at a time means a bad commit
rarely reaches all three the same day -- but it has a silent failure mode. A
cloud that STOPS being deployed looks exactly like a cloud nobody has needed to
deploy.

That is not hypothetical. Azure's deploy script addressed a container app and a
managed environment that had not existed since the VNet migration, so it could
not deploy Azure at all, and nothing noticed: the plane was healthy, answering
200, running whatever it had been left with. What was missing was not a health
check. It was anybody asking how OLD the healthy thing was.

WHAT IT MEASURES

The age of the COMMIT each plane reports at /trust/control-plane.json, read
from outside. Not the age of the deploy job, and not what a deploy job believed
it shipped: a job can succeed while changing nothing, which is the exact case
here. Reading what is serving is the only version of this question that cannot
be answered by a stale record.

Staleness is measured against the commit's own date in this repository, so
"72 hours old" means "running code committed more than 72 hours ago", which is
the drift that matters. A plane redeployed today from an old commit is still
running old code and is reported as such.

WHAT IT DELIBERATELY DOES NOT DO

It does not enforce an order, gate a deploy, or block anything. Bake time
between clouds is an operator judgement, not a rule this script owns. It
reports, and the report is the point.

    scripts/check_cloud_staleness.py
    scripts/check_cloud_staleness.py --max-age-hours 72 --strict
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

PLANES: tuple[tuple[str, str], ...] = (
    ("gcp", "https://trustedrouter.com"),
    ("aws", "https://aws.trustedrouter.com"),
    ("azure", "https://azure.trustedrouter.com"),
)
TIMEOUT_SECONDS = 20
UNKNOWN_RELEASES = frozenset({"local", "unknown", "eu", "azure", ""})
# Abbreviated or full hex object name, nothing else.
_RELEASE_RE = re.compile(r"[0-9a-f]{7,40}")


@dataclass
class PlaneState:
    cloud: str
    release: str = ""
    committed_at: datetime | None = None
    age_hours: float | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def _fetch(url: str) -> dict:
    if not url.startswith("https://"):
        # Scheme is fixed by PLANES, but assert it anyway so a future caller
        # cannot turn this into a file:// read.
        raise ValueError(f"refusing to fetch non-HTTPS URL {url!r}")
    request = urllib.request.Request(url, headers={"accept": "application/json"})  # noqa: S310 - scheme checked above
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read())


def _commit_date(sha: str) -> datetime | None:
    """Committer date for a sha, or None when this checkout has never seen it.

    A sha nobody here can resolve is not treated as fresh. It means the plane
    is running something built from a commit this repository does not have --
    a deleted branch, a local build, a fork -- and that is its own finding.
    """
    # `sha` arrives from a remote response, so it is validated before reaching
    # git rather than trusted. Not shell injection -- this is a list, not a
    # shell -- but an unvalidated value could still be an option-shaped string
    # or a revision expression like "HEAD", which would resolve to something
    # real and report a completely wrong age.
    if not _RELEASE_RE.fullmatch(sha):
        return None
    result = subprocess.run(  # noqa: S603 - argument list; sha validated above
        ["git", "show", "-s", "--format=%cI", sha],  # noqa: S607 - git from PATH, as everywhere else here
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return datetime.fromisoformat(result.stdout.strip())


def inspect(cloud: str, base_url: str, now: datetime) -> PlaneState:
    state = PlaneState(cloud=cloud)
    try:
        document = _fetch(f"{base_url}/trust/control-plane.json")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            state.problem = (
                "no /trust/control-plane.json -- this plane predates the version "
                "endpoint, so its age cannot be read"
            )
            return state
        state.problem = f"HTTP {exc.code}"
        return state
    except Exception as exc:  # noqa: BLE001 - any failure to read is a failure to know
        state.problem = f"unreachable: {exc}"
        return state

    state.release = str(document.get("release", "")).strip()
    if state.release.lower() in UNKNOWN_RELEASES:
        # The pre-fix constants land here: "eu" and "azure" are not commits and
        # never were, so a plane still reporting one has not been redeployed
        # since that was fixed.
        state.problem = f"release {state.release!r} is not a commit; age unknowable"
        return state

    committed = _commit_date(state.release)
    if committed is None:
        state.problem = f"commit {state.release} is not in this repository"
        return state

    state.committed_at = committed
    state.age_hours = (now - committed).total_seconds() / 3600
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-hours", type=float, default=72.0)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a plane is stale or its age cannot be read",
    )
    args = parser.parse_args()

    now = datetime.now(UTC)
    states = [inspect(cloud, base, now) for cloud, base in PLANES]

    print(f"{'CLOUD':<7} {'RELEASE':<12} {'AGE':<12} STATUS")
    stale, unknown = [], []
    for state in states:
        if not state.ok:
            print(f"{state.cloud:<7} {state.release or '-':<12} {'-':<12} {state.problem}")
            unknown.append(state)
            continue
        assert state.age_hours is not None
        age = f"{state.age_hours:.1f}h"
        if state.age_hours > args.max_age_hours:
            print(f"{state.cloud:<7} {state.release:<12} {age:<12} STALE")
            stale.append(state)
        else:
            print(f"{state.cloud:<7} {state.release:<12} {age:<12} ok")

    print()
    # Same-day observation, reported not enforced. The operating norm is that
    # all three clouds rarely take a deploy on the same day, so that all three
    # are never simultaneously untested. Emergencies are the exception and this
    # line is how one shows up in hindsight.
    days = {s.committed_at.date() for s in states if s.committed_at}
    if len(days) == 1 and len([s for s in states if s.committed_at]) == 3:
        print("NOTE: all three planes are running code committed on the same day.")
        print("      Expected during a security fix; otherwise the clouds are")
        print("      no longer staggered and share one untested commit.")

    if stale:
        print(f"{len(stale)} plane(s) older than {args.max_age_hours:.0f}h:")
        for state in stale:
            assert state.age_hours is not None
            print(f"  {state.cloud}: {state.release} is {state.age_hours / 24:.1f} days old")
        print("Redeploy that cloud, or find out why its deploy path stopped working.")
    if unknown:
        print(
            f"{len(unknown)} plane(s) could not be aged -- treat as stale until proven otherwise."
        )
    if not stale and not unknown:
        print("Every plane is within the freshness window.")

    return 1 if args.strict and (stale or unknown) else 0


if __name__ == "__main__":
    raise SystemExit(main())
