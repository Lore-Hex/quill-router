from __future__ import annotations

import html
import json
import re
import shutil
import subprocess

import pytest

from trusted_router.content.blog import BLOG_POSTS_BY_SLUG

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


def _example() -> str:
    body = BLOG_POSTS_BY_SLUG["how-confidential-computing-protects-ai-prompts"].body_html
    match = re.search(r"<pre><code>(.*?)</code></pre>", body, re.DOTALL)
    assert match is not None
    return html.unescape(match.group(1))


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"image_digest": NEW, "accepted_image_digests": [OLD, NEW]}, f"{OLD},{NEW}"),
        ({"image_digest": NEW, "accepted_image_digests": [NEW]}, NEW),
        ({"image_digest": NEW}, None),
        ({"accepted_image_digests": []}, None),
        ({"accepted_image_digests": None}, None),
        ({"accepted_image_digests": NEW}, None),
        ({"accepted_image_digests": [OLD, None]}, None),
        ({"accepted_image_digests": [OLD, "not-a-digest"]}, None),
    ],
)
def test_documented_digest_filter_fails_closed(record: dict, expected: str | None) -> None:
    match = re.search(r"digests=\"\$\(jq -er '(.*?)' \"\$release\"\)\"", _example(), re.DOTALL)
    assert match is not None, "the example must validate the accepted set, not just the target"
    jq = shutil.which("jq")
    assert jq is not None, "jq is required to exercise the published example"
    result = subprocess.run(  # noqa: S603 - fixed executable, filter from tracked blog source
        [jq, "-er", match.group(1)],
        input=json.dumps(record), text=True, capture_output=True, check=False, timeout=5,
    )
    if expected is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


def test_example_authenticates_release_before_using_its_fields() -> None:
    example = _example()
    assert example.startswith("set -eu\n")
    assert example.index("cosign verify-blob") < example.index("commit=")
    assert "publish-trust-gcp.yml@refs/heads/main" in example
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in example
    assert '--expect-digest "$digests"' in example
    assert "--api-host api-us-central1.quillrouter.com" in example
    assert "--no-require-exporter-binding" not in example
