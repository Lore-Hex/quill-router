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

## Social preview

Asset: `web/lightningrouter-og.jpg`, 1732 by 908 pixels, about 305 KB. Shared
server-rendered Open Graph and Twitter metadata reference this image with its
actual dimensions and descriptive alt text. The canonical URL is always
`https://lightningrouter.ai`, including when a visitor uses a domain alias.

Created with the built-in image generation tool, using the existing mascot as
the reference. The generated PNG was encoded as JPEG with `sips` at quality 88;
its original composition and dimensions were preserved. The public exporter
allows only this exact reviewed JPEG digest, not arbitrary binary files.

Final generation prompt:

> Create a polished wide Open Graph social sharing image for LightningRouter,
> landscape 1.91:1 aspect ratio, ideally 1200x630. Use the attached transparent
> lightning-bolt robot as the exact brand mascot reference. Preserve its
> recognizable orange/red/yellow bolt silhouette, black face, smiling yellow
> eyes, and circular ear pieces; no new limbs or accessories. Make a bold,
> clean developer-product poster: crisp white background, nearly black
> typography, the large glossy mascot fully visible on the right with generous
> safe margins, restrained orange accents. On the left place clean, extremely
> legible modern sans-serif typography with exactly these words: top brand
> 'LightningRouter'; large two-line headline 'Pay with Bitcoin.' then 'Start
> building.'; below in smaller type 'No email. No password. No card.'; at the
> bottom 'lightningrouter.ai'. Add a small secondary label near the brand reading
> 'BITCOIN OVER LIGHTNING'. Keep the complete brand name and every word legible,
> no clipping, no other text. Avoid QR codes, coins, provider logos, gradients,
> decorative orbs, dashboards, screenshot frames, watermarks, drop shadows
> behind text, or generic circuit-board backgrounds. The composition should be
> distinctive because of the original mascot and precise typography, not busy
> decoration. This is a finished public marketing asset, not a mockup.
