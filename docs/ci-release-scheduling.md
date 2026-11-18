# Main CI and deploy scheduling

The `scope`, `coverage` and final `CI` jobs on trusted main pushes and
main dispatches use the GitHub-hosted `tr-ci-short` label. The
`tr-main-short-checks` group is restricted to this repository and
`Lore-Hex/quill-router/.github/workflows/ci.yml@refs/heads/main`.
Its two concurrent 2-core machines use GitHub's Ubuntu 24.04 x64 image.
Pull requests, pricing branches and the heavy jobs use standard Ubuntu 24.04.
If the pool is unavailable, restore the standard label through a reviewed
workflow change; never grant fork workflows access to the group.

On a deploying push, the image builds alongside CI. Once built, the outer
CI gate queues on the main CI workflow's concurrency group for that SHA,
without acquiring a production lease or a runner to poll. Acquiring the
group is **not** proof of green CI: the job checks the latest eligible
`ci.yml` run for that commit. The production workflow checks again before
admission. PR CI, API errors, missing runs, and any state other than
completed success do not qualify. Pricing-branch dispatches can still
supply completed CI on the exact commit subsequently advanced to main.

GitHub does not guarantee which event enters a group first. If CI is
missing, the gate wins a scheduling race, or a rerun is pending, the gate
exits unsuccessfully and releases the group immediately. Wait for that
commit's push or dispatch CI to finish green, then rerun the normal failed
deploy after checking its release eligibility. Do not use a hotfix.

To compare releases, use attempt-specific GitHub Actions job records.
Measure each short job from creation to start, the gate from start to
completion, and the end-to-end release from deploy creation to successful
production finalization. Report retries and superseded/skipped rollouts
separately. Queue time and idle runner-minutes saved are not additive
reductions in rollout time; builds, tests and safety checks overlap.
