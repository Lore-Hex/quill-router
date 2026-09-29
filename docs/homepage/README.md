# TrustedRouter application homepage

The active implementation is in the application branch `jasmine/trustedrouter-homepage-port`. The static design repository is a reference, not the remote review target or a second implementation to maintain.

## Remote application review

https://23e0-108-6-58-177.ngrok-free.app/

This serves the actual application homepage from local port 3001 through a read-only bridge on 3002. The default view has no comparison toolbar. Catalog prices/privacy come from the application model directory; search uses the lightweight `/v1/models/picker` endpoint. Other destinations link to the existing public site. Local mutations and development sign-in are not exposed. No deployment, push, or merge occurred. The tunnel requires the local computer and processes to remain running.

The old ngrok address beginning `985c` still serves the reference design on port 3000. Do not present that as the application result.

## Source and preservation

- `templates/homepage/`: approved marketing markup split into application includes, with server-rendered catalog rows.
- `static/homepage/`: original landscape PNG, all responsive AVIF/WebP images, original colored logos, shared fonts/licenses, approved styles and adapted interactions.
- `homepage.py`: reuses the existing public catalog and model-directory price/privacy aggregation.
- `preview-asset-manifest.json`: original asset hashes from preview commit `e510502` on `jasmine/landscape-homepage`. Before subsequent mobile fixes, 29 of 30 files matched after asset URL normalization; only the interaction script differed. Non-catalog section copy and the approved content JSON matched exactly.
- The original preview, Exchange pages, chart experiments, and hosting settings were not changed during the application port.

Enable the new homepage using `TR_HOMEPAGE_LANDSCAPE_ENABLED=true`. It defaults off pending release review. `TR_HOMEPAGE_REVIEW_TOOLS_ENABLED=true` optionally restores the four-font comparison toolbar in local/test environments. Local/test pages retain noindex even with that toolbar off.

## Mobile fix, September 29

Remote inspection found all 11 supporting disclosures expanded because JavaScript responses were truncated. The application caches static file metadata; editing an asset while its process runs left an old Content-Length, causing the read-only bridge to return 502. Restarting only the new application process restored complete script delivery. Restart this process after asset edits until a development-only cache strategy is added. Never change production cache behavior to solve local editing.

Verified remotely at 390×844: all 11 disclosures closed, HTML content retained, page height reduced from 13,790px to 9,460px. Request/migration/four verification visuals/five footer groups remain optional on phones and expanded on desktop.

The hero now captures its small viewport height before body paint, and uses that stable value and stable height classes through phone browser-bar/keyboard height changes. Width/orientation changes recalculate. Reduced-motion behavior is retained. Touch hover no longer pauses/restarts the ribbon during swipes; keyboard focus still pauses it. With toolbar removed at 390×844, hero is 766px high. Marquee document Y remained 746.016px across a height-only change to 700px and page scrolling. This is browser viewport simulation, not a physical iPhone certification.

## Validation and remaining work

Four focused application tests passed and full ruff passed before the latest mobile adjustments. Full pytest was attempted but stopped at collection because the temporary environment lacks `jsonschema`; coverage is unverified. Mypy reported an installed Bigtable API mismatch in unchanged `storage_gcp_mirror.py`; an unused ignore in the new helper was removed. Later reruns were interrupted by the user's instruction to prioritize remote design review. Do not claim all release gates pass. Dependency/lock files were not changed. Do not resume dependency installation without a new reason consistent with the user's direction.

Hero and headings matched the original reference exactly at 390×844, 320×568, and 1440×900 before hiding the optional toolbar. Original layout, artwork and foreground lowering are preserved; removal of the 44px review bar gives that space to the hero. Search (including models outside the old fixture), no-results state, Escape focus restoration, mobile menu, request controls, migration keyboard tabs and catalog filters were checked in the app. Contrast token ratios: primary CTA 12.48:1; muted text 9.58:1; blue text 8.80:1; primary text 18.61:1. Motion rules inspected, OS motion settings not changed.

Remaining release work: complete repository gates in its supported environment, broader route/sign-in checks, review inherited marketing claims and fixed pricing examples, connect approved homepage analytics events, and decide whether to replace illustrative request evidence with captured evidence. Status remains a link to the public status page, not an invented green health signal. This is a working application design for review, not a claimed production release.
