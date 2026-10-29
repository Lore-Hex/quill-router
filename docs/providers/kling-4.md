# Kling 4.0

Canonical model: `kling/v4`. Initial qualified host: Runway, native ID
`kling_4`, on the existing Runway account. Standard privacy, not ZDR or
confidential. This route does not use Venice or fal.

## Contract

- `/v1/videos`: text or first/last-frame images, 3-30 whole seconds.
- 720p or 1080p; text aspects 16:9, 9:16, 1:1, 21:9.
- Image output derives its aspect from the first frame.
- Native audio is always enabled. Explicit audio-off is rejected.
- No seed, negative prompt, reference assets, video editing, 4K or Flash
  variant is advertised until separately qualified.
- Prompt limit: 26,000 characters, subject to upstream language validation.

Runway standard rates are USD 0.157/second at 720p and 0.235/second at
1080p. Like the existing fixed-cost video routes, TR uses standard list
rates rather than a temporary launch promotion. Round the job's upstream
cost up to whole USD 0.01 Runway credits, then apply the existing 20% video
fee. A three-second 720p job quotes USD 0.576; 1080p quotes USD 0.852.
The fixed quote is reserved before content is forwarded and remains fixed
through polling, settlement and replay. Do not apply chat token markup.

## Evidence (2026-10-29)

- [Runway API and model contract](https://dev.runway.com/learn/kling-4-0-api)
- [Runway price and whole-credit rounding contract](https://docs.dev.runwayml.com/guides/pricing/)
- Authenticated Runway organization metadata lists `kling_4` with nonzero
  quota; authenticated submission succeeded.
- Text canary `354b4b81-a2d7-4994-a4b0-5c64fe5cd2ad`: SUCCEEDED;
  inspected MP4 is 1280x720, H.264 + AAC, 3.042 seconds, and matches the
  synthetic blue-teapot prompt.
- First/last-frame 1080p canary `9807d696-d478-4f85-acdc-25b65c49c16a`:
  SUCCEEDED; inspected MP4 is 1920x1080, H.264 + AAC, 3.042 seconds.
- fal's native catalog advertises an active route, but its
  [launch page](https://fal.ai/kling-4) says API access is coming soon.
  Do not enable fal from that catalog signal alone.

## Release Order

Deploy the matching `quill-cloud-proxy` adapter to all relevant serving
gateways before merging this control-plane catalog activation. Use the
normal shared coordinator and keep a healthy cloud holdback. Preserve the
documented staged trust publication for the new measured binaries. No secret
rotation, permission expansion, or bypass of release checks is needed.
After activation, verify authenticated TR create/poll/content/replay,
provider-only routing, exact quoted charge, public model page and health.
