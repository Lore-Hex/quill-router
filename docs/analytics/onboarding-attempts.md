# Welcome Test And Activation Metrics

The welcome-page live test and API activation are separate measurements.

* `acquisition.first_successful_api_call` is a server-side, once-per-workspace
  successful settlement milestone. SDKs, agents and the welcome test can produce
  it. It is not proof that the welcome page displayed a visible answer.
* `acquisition.onboarding_call_started`, `onboarding_call_succeeded` and
  `onboarding_call_failed` are browser-reported welcome-test events. Every click
  gets a UUID `attempt_id`. The same ID is sent as the proxy request ID, with a
  distinct `welcome-` idempotency-key prefix. Manual retries get new IDs.
* Legacy `first_call_started` and `first_call_failed` measured welcome clicks
  only, with no success event or attempt ID. They cannot be used as the
  denominator for all API activations or as a reliable historical failure rate.

The old eight-token output cap could be consumed entirely by reasoning. HTTP
200 then produced an activation settlement but an empty visible answer and a
browser failure. The welcome test now allows 512 output tokens, including any
reasoning, and stops waiting after 75 seconds. It never retries a paid request
automatically. A browser timeout does not prove the upstream request was free
or never completed.

## Dashboard Interpretation

The Axiom Growth Funnel has a separate matched-attempt panel. Its denominator
contains attempts with both a start and exactly one outcome type. Visible text
must have been rendered for browser success. Missing, pending, orphaned and
conflicting outcomes are shown separately; none are silently counted as a
failure. Five minutes without an outcome is a telemetry gap, not proof of an
inference failure. Window boundaries and blocked browser telemetry can leave
unmatched events. Browser events are observational, not trusted billing data.

Failure metadata is restricted to HTTP status, elapsed milliseconds, a fixed
failure category and a fixed finish-reason category. It contains no API key,
prompt, output, reasoning text or provider error body. Classified failures
distinguish exhausted output budget, empty output, invalid JSON, HTTP error,
network error, timeout, missing key and client rendering error.

## Rollout And Verification

1. Run the application regression, browser, lint, type and full Python gates.
2. Deploy the control plane through the protected deployment workflow.
3. Update the exact-event Cloud Logging sink via
   `python -m scripts.axiom_growth.provision source-filter`.
4. Rebuild and update the scheduled growth-sync worker with the new event
   projection. Retain its checkpoint, credentials, service account and schedule.
5. Validate and publish dashboards with
   `python -m scripts.axiom_growth.dashboards --publish`.
6. Verify one live welcome request, matching start/outcome structured logs,
   subsequent Axiom delivery, and the separate matched-attempt panel. Export
   typically follows within five minutes. Keep diagnostic traffic tagged as
   internal testing when checking customer funnels.

Historical records are preserved as observed. Do not fabricate missing starts,
infer visible answers from HTTP 200 alone, or relabel old browser failures as
provider outages.
