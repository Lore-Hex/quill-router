# LightningRouter logo

The founder supplied the orange lightning-bolt robot logo on September 15, 2026.
The compact mascot is used in the shared header and browser favicon. The adjacent
LightningRouter text names the home link; the image has empty alternative text
to avoid announcing the brand twice.

Asset: `web/lightningrouter-mark.webp`. It has real alpha transparency and fixed
40 by 40 CSS dimensions in both themes. WebP encoding keeps the shared asset
below 100 KB without a new image-processing dependency at runtime or build time.

The built-in image generation tool extracted the mascot with this prompt:

> Create a faithful transparent PNG cutout of ONLY the orange/red/golden
> lightning-bolt robot mascot, with its black face, glowing yellow smiling eyes,
> and circular ear pieces. Remove the white background and remove the LIGHTNING
> ROUTER wordmark below it. Preserve the existing mascot's exact design,
> silhouette, angled orientation, colors, shading, face, and proportions; do not
> redesign, add elements, or simplify it. Center the complete uncropped mascot
> on a square canvas with a small transparent safe margin. Actual alpha
> transparency, no white rectangle, no checkerboard texture, no text, no new
> shadow. Intended use: the same supplied logo as a website header mark and favicon.

The result was encoded as WebP using `cwebp -q 85 -m 6`. The public-source exporter
explicitly allows this one reviewed binary path and validates its file envelope;
all other source still requires UTF-8. Browser tests decode the image and check
both transparent corners and a near-opaque center, all page headers, favicon links,
mobile layout, and light/dark themes.
