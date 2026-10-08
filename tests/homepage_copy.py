"""Homepage copy the production smoke test checks on the live site.

The local landscape-homepage test asserts the same strings, so a copy edit
fails in CI before a deploy can turn the post-deploy smoke red.
"""

# The verification section's multi-cloud proof point
# (templates/homepage/verify.html).
LANDSCAPE_MULTICLOUD_HEADING = "Three clouds, visible fallback"
