# NYTE 404 release handoff

Owner: Joseph. Item 41 remains open until production missing URLs are verified.
This change is New York only; do not change other markets or the shared bucket's
website error-page setting.

## Publish

After review and required CI gates, build the merged revision with build.py.
Publish `404`, `404.html` as `text/html` and
`assets/nyte-v2/not-found.css` as `text/css`. Inventory the page's root-relative
asset references and verify their live bytes against the build before publishing;
publish any required NYTE dependencies only after reviewing concurrent live changes.
Use the existing narrow-release procedure: back up target bytes/metadata/generations,
upload with observed generation preconditions, assets before HTML, and
`Cache-Control: public,max-age=300`. Do not run a full-market rsync.
The page includes noindex and is excluded from generated sitemaps.

## Configure New York missing URLs

The HTML alone does not replace GCS NoSuchKey responses. Routing is owned by
`infra/control_lb_url_map.tf`; use the reviewed infrastructure workflow.
Attach a default custom error response policy only to the non-redirecting
`exchange-new-york` path matcher, not the URL-map default or shared backend bucket.
Use `token-exchange-static` as error service, match only response code `404`,
serve `/404.html`, and preserve HTTP 404 (never override it to 200).
Leave alias redirects and every other market matcher unchanged. Upload the error
page and dependencies before enabling the policy. Inspect the Terraform plan for
unrelated changes and retain the existing deployment gates.

## Verify before closing item 41

Request new top-level and nested missing paths, including a trailing-slash path
and query string, through public HTTPS. Each must retain its URL, return HTTP 404
with HTML containing the approved page, and include noindex. Load a nested missing
URL in desktop and mobile browsers and verify assets, header menu, all four tiles,
Go home and footer navigation. Confirm `/sitemap.xml` omits `/404` and `/404.html`.
Check the homepage and real destination pages still return 200, aliases retain
their existing redirects, and other market missing responses are unchanged.
Do not use a successful request to `/404.html` as evidence of missing-URL routing.

## Rollback

Revert only the New York error policy through the same infrastructure workflow.
Restore modified objects from their backups with generation preconditions if
needed. Do not import an old whole URL map or overwrite other market objects.
