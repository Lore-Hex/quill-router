"""Assert that every public repository still accepts a vulnerability report.

Two org-wide security properties were established by a one-time sweep and then
maintained by nothing:

* **Private vulnerability reporting (PVR)** — the intake channel our published
  ``SECURITY.md`` files point researchers at.
* **A ``SECURITY.md``** telling a researcher where to go at all.

Both drifted. On 2026-08-27 an internal audit (IA-2026-01, findings E-9/E-10)
found ``trpi`` publishing a ``SECURITY.md`` that directed researchers to an
advisories URL whose backing setting read ``{"enabled": false}`` — an advertised
inbound channel that could not accept a report — and
``trusted-router-sdk-conformance`` with neither. Both repositories were created
*after* the sweep that set the property everywhere else. Nothing enrolled them,
so nothing noticed.

That is the shape this script exists to break: coverage asserted continuously
over the live set of repositories, rather than asserted once over the set that
happened to exist that day.

**Branch protection.** The same script also asserts that `main` on the production
repositories still carries required status checks and refuses force-pushes and
deletions. This was added on 2026-08-27 after that configuration changed twice
inside 24 hours with nothing noticing either time — once when `gate-on-ci` was
added to the required set (which silently blocked every pull request, since that
job lives in `deploy.yml` and never runs on a PR), and again when it was removed.
Neither change produced a signal. A setting nothing watches is a setting that
drifts, and branch protection is the control the change-management narrative
rests on.

Note what is asserted and what is not. Required *contexts* are compared as a
set-membership test against a required minimum, not pinned to an exact list —
pinning would fail the run every time a legitimate CI job is added or renamed,
and a check that cries wolf on ordinary work gets muted. What must hold is that
protection exists, that force-push and deletion stay off, and that the named
minimum checks are present.

**On reading failures.** PVR state is only visible to a token with repository
administration rights. When the token cannot read it, this script reports
``unreadable`` and exits non-zero — it never reports such a repository as
covered, and never as uncovered either. Collapsing "I could not look" into
either answer is the specific defect that produced six false drift reports
against this organisation's cloud baseline on 2026-08-26; a detector that says
"missing" whenever it cannot read is saturated, and a real regression becomes
indistinguishable from a broken query.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
COVERED = "covered"
UNCOVERED = "uncovered"
UNREADABLE = "unreadable"


def _get(path: str, token: str, *, attempts: int = 4) -> tuple[int, object]:
    """GET an API path. Returns (status, parsed-body-or-None). Never raises for HTTP.

    Retries 429 and 5xx with backoff. A rate limit is not evidence about the
    repository — during development this check hit a 429 on one repository and,
    without this retry, would have reported an existing SECURITY.md as absent.
    """
    for attempt in range(attempts):
        status, body = _get_once(path, token)
        if status not in (0, 429) and status < 500:
            return status, body
        if attempt == attempts - 1:
            return status, body
        delay = _retry_after(body) or 2**attempt
        print(f"  retrying {path} in {delay}s (HTTP {status})", file=sys.stderr)
        time.sleep(delay)
    return 0, None


def _retry_after(body: object) -> int | None:
    if isinstance(body, dict):
        for key in ("retry_after", "retryAfter"):
            value = body.get(key)
            if isinstance(value, int) and 0 < value <= 60:
                return value
    return None


def _get_once(path: str, token: str) -> tuple[int, object]:
    req = urllib.request.Request(  # noqa: S310 - fixed https API host
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "trustedrouter-repo-security-coverage",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"null")
        except ValueError:
            body = None
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  transport error on {path}: {exc}", file=sys.stderr)
        return 0, None


def list_public_repos(org: str, token: str) -> list[str]:
    names: list[str] = []
    page = 1
    while True:
        status, body = _get(f"/orgs/{org}/repos?type=public&per_page=100&page={page}", token)
        if status != 200 or not isinstance(body, list):
            raise SystemExit(f"could not list repositories for {org}: HTTP {status}")
        if not body:
            break
        names += [r["name"] for r in body if not r.get("archived")]
        if len(body) < 100:
            break
        page += 1
    return sorted(names)


def pvr_state(org: str, repo: str, token: str) -> str:
    """Covered / uncovered / unreadable — three states, deliberately."""
    status, body = _get(f"/repos/{org}/{repo}/private-vulnerability-reporting", token)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("enabled"), bool):
        return COVERED if body["enabled"] else UNCOVERED
    # 403 = token lacks administration rights; 404 = repo gone or invisible to this token.
    # Neither means "no channel exists", so neither is reported as one.
    return UNREADABLE


def security_md_state(org: str, repo: str, token: str) -> str:
    for path in ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md"):
        status, _ = _get(f"/repos/{org}/{repo}/contents/{path}", token)
        if status == 200:
            return COVERED
        if status not in (403, 404):
            return UNREADABLE
    return UNCOVERED


# main is protected by these, and by nothing else, on these repositories.
PROTECTED_REPOS = {
    "quill-router": {"lint", "test (1)", "test (2)", "test (3)"},
    "quill-cloud-proxy": {"parent", "trust-page-validation"},
}


def check_branch_protection(org: str, token: str) -> list[str]:
    """Return a list of problems. Empty means the protection still holds."""
    problems: list[str] = []
    for repo, required in sorted(PROTECTED_REPOS.items()):
        status, body = _get(f"/repos/{org}/{repo}/branches/main/protection", token)
        if status == 404:
            problems.append(f"{repo}: main is NOT PROTECTED")
            continue
        if status != 200 or not isinstance(body, dict):
            # Same rule as everywhere else here: could-not-read is its own state.
            problems.append(f"{repo}: could not read protection (HTTP {status}) — NOT treating as unprotected")
            continue

        checks = body.get("required_status_checks") or {}
        contexts = set(checks.get("contexts") or [])
        missing = required - contexts
        if missing:
            problems.append(f"{repo}: required status checks missing {sorted(missing)} (have {sorted(contexts)})")

        if (body.get("allow_force_pushes") or {}).get("enabled"):
            problems.append(f"{repo}: force pushes to main are ALLOWED")
        if (body.get("allow_deletions") or {}).get("enabled"):
            problems.append(f"{repo}: deletion of main is ALLOWED")

        # Not a failure — recorded so the report states the real boundary of the
        # control rather than implying a stronger one. A sole founder cannot
        # approve their own pull request, so review is structurally unavailable.
        admins = (body.get("enforce_admins") or {}).get("enabled")
        reviews = (body.get("required_pull_request_reviews") or {}).get(
            "required_approving_review_count"
        )
        print(f"  {repo}: contexts={sorted(contexts)} enforce_admins={admins} required_reviews={reviews}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", default="Lore-Hex")
    ap.add_argument(
        "--allow-unreadable",
        action="store_true",
        help="Exit 0 when the only problem is that PVR state could not be read. "
        "For runs whose token has no administration rights; the SECURITY.md "
        "assertion still holds and still fails the run.",
    )
    args = ap.parse_args()

    token = os.environ.get("REPO_SECURITY_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit("REPO_SECURITY_TOKEN or GITHUB_TOKEN must be set")

    print("branch protection on production repositories:")
    protection_problems = check_branch_protection(args.org, token)
    if protection_problems:
        print("\n  BRANCH PROTECTION PROBLEMS:")
        for line in protection_problems:
            print(f"    - {line}")
    else:
        print("  ok — protection holds on all production repositories")
    print()

    repos = list_public_repos(args.org, token)
    print(f"public, non-archived repositories in {args.org}: {len(repos)}\n")

    uncovered: list[str] = []
    unreadable: list[str] = []
    for repo in repos:
        pvr = pvr_state(args.org, repo, token)
        sec = security_md_state(args.org, repo, token)
        flags = []
        if pvr == UNCOVERED:
            flags.append("PVR DISABLED")
        if pvr == UNREADABLE:
            flags.append("PVR UNREADABLE")
        if sec == UNCOVERED:
            flags.append("NO SECURITY.md")
        if sec == UNREADABLE:
            flags.append("SECURITY.md UNREADABLE")

        if not flags:
            print(f"  ok         {repo}")
            continue
        label = "UNREADABLE" if all("UNREADABLE" in f for f in flags) else "GAP"
        print(f"  {label:10} {repo}  ({', '.join(flags)})")
        if label == "GAP":
            uncovered.append(f"{repo}: {', '.join(flags)}")
        else:
            unreadable.append(f"{repo}: {', '.join(flags)}")

    print()
    print(f"covered: {len(repos) - len(uncovered) - len(unreadable)}/{len(repos)}")

    if uncovered:
        print("\nGAPS — a researcher cannot report a vulnerability here:")
        for line in uncovered:
            print(f"  - {line}")
    if unreadable:
        print("\nUNREADABLE — state not determined; NOT counted as covered:")
        for line in unreadable:
            print(f"  - {line}")
        print(
            "\n  A fine-grained token with Administration: read over the organisation\n"
            "  is required to read private-vulnerability-reporting. Set it as the\n"
            "  REPO_SECURITY_TOKEN secret."
        )

    if uncovered or protection_problems:
        return 1
    if unreadable:
        return 0 if args.allow_unreadable else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
