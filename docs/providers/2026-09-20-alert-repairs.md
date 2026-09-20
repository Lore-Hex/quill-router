# Provider and payment alert repairs, September 20, 2026

## Confirmed causes

* Kimi discovery accepted a partial priced catalog, but its manifest writer
  required every static expected family. The missing Kimi K3 price aborted
  publication for all providers. The writer now checks the validated result
  against live discovery instead of inventing a price or requiring a retired
  discovery hint. Existing price-change and mass-prune guards stay in place.
* Inceptron used the retired MiniMax M2.5 route as its account-wide canary.
  It now uses the shared independently canaried catalog implementation.
* Morph's old Qwen 27B, Qwen 397B and two MiniMax routes are not served by
  the configured endpoint. Nextbit no longer serves its MythoMax alias.
  These exact provider/model pairs are excluded; other providers are unaffected.
* Phala discovery published GLM 5.3 and GLM 5.3 Flash without entries in the
  enclave's reviewed dispatch map. Production fails before contacting them.
  Both routes have explicit operator holds until a reviewed adapter release.
  The production confidential credential works; the legacy local Phala key
  does not. Do not replace the confidential credential with the legacy key.
* NVIDIA's Kimi K3 returned a first SSE event after 36.7 seconds in a bounded
  direct check. The probe classified it as a generic 20-second model even
  though its token budget already classified it as reasoning. It now uses
  the existing 45-second reasoning deadline. Measured latency is unchanged;
  exceeding the budget still alerts. No global alert threshold changed.
  Follow-up inspection found the rotation probe also bounded the entire
  stream with that first-token deadline. It now switches once on the first
  content/reasoning token to the existing absolute completion deadline.
  Role/heartbeat frames cannot extend either deadline. Deterministic tests
  reproduced the premature timeout before the fix, and cover late errors,
  never-ending streams and cancellation. Live NVIDIA recovery remains
  unverified and its issue remains open.
* After the Kimi writer repair, price refresh reached catalog validation.
  Three exact Spanner-operation tests failed because removal of a GMI BYOK
  catalog endpoint removed one eligibility lookup. The tests now use a
  fixed prepaid/BYOK catalog while retaining exact operation-count gates;
  no billing transaction or pricing safety guard was changed.
* Together retired its shared E5 embedding endpoint on September 14. The
  remaining BGE catalog entry returns HTTP 400 requiring a dedicated endpoint.
  The daily probe now reports that retirement rather than calling a model
  deliberately excluded by our lifecycle rules. Other embedding probes stay.
* PayPal PENDING_REVIEW correctly issued no credits, but the console treated
  it as a failed capture. Pending captures now return an explicit pending
  result and show a notice. Only COMPLETED enters the existing idempotent
  credit transaction. Repeated pending/completed deliveries and workspace
  ownership are regression tested. No refunds or dispute actions were taken.
* Deployment 35481392420 could not finish uploading the public snapshot
  worker within 300 seconds. SSH authentication succeeded; project SSH
  metadata contained only two keys. The temporary archive reached 14,100,480
  bytes but failed a read-only gzip integrity check with unexpected EOF.
  The old worker remained healthy, publishing all five snapshots every minute.
  Public snapshot bundles now exclude website static assets and use the
  existing committed-source/valid-JSON builder. Equivalent committed bundles
  shrink from 14,484,681 to 2,581,099 bytes, about 82 percent. An isolated
  import of the reduced bundle loaded the snapshot worker and all 625 models.
  The 300-second bound, short-lived SSH access, rollback, and publication
  checks are unchanged. Production upload still requires verification.
  Follow-up run 35483932283 also timed out: only 2,350,080 of 2,580,921 bytes
  reached the node. CPU, memory and disk checks found no resource exhaustion.
  Reducing the archive was therefore not a sufficient repair. CI now installs
  the pinned NumPy IAP accelerator into an isolated Python used by gcloud, with
  site packages enabled only for this step, as recommended in
  [Google's IAP documentation](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding#increasing_the_tcp_upload_bandwidth).
  It also reports the raw failed step outcome explicitly; `continue-on-error`
  can otherwise present a green job despite a failed worker update. This
  mitigation still requires a successful real upload and publication check.

## Open operational work

* NEAR GLM 5.3 Flash fails with `NEAR AI deployment history is outside the
  reviewed policy`. Ordinary direct inference succeeds, which does not prove
  attestation. Keep the alert open and review the new deployment evidence;
  do not remove history verification or switch to an unverified endpoint.
* Confidential API certificate renewal is denied Cloud DNS changes for the
  `quill-workload` identity. Main API certificates remain valid. A proposed
  zone-level conditional grant was blocked by the permission safety review;
  no IAM change was applied. Repair requires separately approved least-
  privilege access to the exact ACME TXT records, not project-wide DNS admin.
* The existing RedPill provider split is a separate reviewed release. These
  repairs do not bypass its secret/launch-policy approval gate or label
  ordinary RedPill inference as confidential.

Resolve Sentry issues only after the corresponding release is live and its
behavior has been checked. Route holds are containment, not evidence that
the excluded upstream integration is repaired.
