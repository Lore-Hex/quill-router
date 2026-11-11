"""A supplier's Verified Secure badge must link to evidence a visitor can check without trusting NYTE."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nyte_v2
from build import VERIFIED_PROVIDERS, build

HERE = Path(__file__).parent


class VerifiedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.output.cleanup)
        cls.root = Path(cls.output.name)
        build(cls.root)
        cls.page = (cls.root / "verified" / "maple").read_text()
        cls.script = (cls.root / "assets" / "verified.js").read_text()

    def test_each_provider_has_a_clean_url_alias_and_sitemap_entry(self):
        sitemap = (self.root / "new-york" / "sitemap.xml").read_text()
        for provider in VERIFIED_PROVIDERS:
            page = (self.root / "verified" / provider).read_text()
            self.assertEqual(page, (self.root / "verified" / f"{provider}.html").read_text())
            url = f"https://nytokenexchange.com/verified/{provider}"
            self.assertIn(f'rel="canonical" href="{url}"', page)
            self.assertIn(f"<loc>{url}</loc>", sitemap)

    def test_check_reads_only_first_party_public_records(self):
        # The enclave itself, the provider's own approval, and NYTE's reproduction.
        for attribute, source in {
            "data-attestation": "https://enclave.trymaple.ai/attestation",
            "data-provider-record": "https://raw.githubusercontent.com/MaplePrivacyLabs/Maple/master/services/opensecret/pcrProd.json",
            "data-nyte-record": "https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/main/enclave-go/maple-transport/verified-release.json",
        }.items():
            self.assertIn(f'{attribute}="{source}"', self.page)
            self.assertIn(source, self.page.split('id="manual"')[1], "the manual check must use the same source")

    def test_gateway_check_reads_the_gateway_and_its_published_release(self):
        # The path through NYTE: the gateway's own attestation, then NYTE's public records.
        for attribute, source in {
            "data-attestation": "https://api.trustedrouter.com/attestation",
            "data-release": "https://trust.trustedrouter.com/trust/gcp-release.json",
            "data-pin": "https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/{commit}/enclave-go/maple-transport/verified-release.json",
        }.items():
            self.assertIn(f'{attribute}="{source}"', self.page.split('id="gateway-check"')[1])
            self.assertIn(source, self.page.split('id="manual"')[1], "the manual check must use the same source")
        self.assertIn('data-audience="quill-cloud"', self.page)
        # Google's keys are the trust anchor, so they are pinned in the script, not the page.
        self.assertIn(
            "https://www.googleapis.com/service_accounts/v1/metadata/jwk/signer@confidentialspace-sign.iam.gserviceaccount.com",
            self.script,
        )
        self.assertIn("https://confidentialcomputing.googleapis.com", self.script)
        self.assertIn("disabled-since-boot", self.script)
        # The source commit is taken only from an image name NYTE published.
        self.assertIn("accepted_image_references.includes(claims.imageReference)", self.script)

    def test_script_pins_the_aws_root_and_checks_freshness(self):
        # https://docs.aws.amazon.com/enclaves/latest/user/verify-root.html
        self.assertIn("641a0321a3e244efe456463195d606317ed7cdcc3c1756e09893f3c68f79bb5b", self.script)
        self.assertIn("crypto.randomUUID()", self.script)
        self.assertIn("nonce mismatch", self.script)
        # Readable by a visitor: served as written, with no NYTE address of its own.
        self.assertEqual(self.script, (HERE / "verified.js").read_text())
        self.assertNotIn("nytokenexchange.com", self.script)
        self.assertNotIn("trustedrouter.com", self.script)

    def test_page_states_its_limits_and_works_without_javascript(self):
        for copy in (
            "needs JavaScript",
            "Check it without this page",
            "It covers the enclave, not everything",
            "not the network address it answered from",
            "one gateway machine per visit",
            "The gateway check proves less",
            "can be up to an hour old",
            "are NYTE’s statements",
            "The badge is a link, not a result",
            "NYTE sends it nothing rather than fall back",
        ):
            self.assertIn(copy, self.page)
        self.assertIn('href="/assets/verified.js"', self.page)

    def test_page_opens_with_a_sentence_for_the_visitors_own_agent(self):
        prompt = re.search(r'<pre class="agent-prompt" id="agent-prompt">(.*?)</pre>', self.page).group(1)
        # One sentence, pointing at the manual steps and away from the page's own verdict.
        self.assertEqual(prompt.count(". "), 0)
        self.assertTrue(prompt.endswith("."))
        self.assertIn("https://nytokenexchange.com/verified/maple#manual", prompt)
        self.assertIn("trust nothing the page itself claims", prompt)
        self.assertIn('id="manual"', self.page)
        # The hero's one action leads to the prompt, the first of the ways to check.
        hero = self.page.split('<section class="hero-maple mv3-hero">')[1].split("</section>")[0]
        self.assertEqual(re.findall(r'<a class="(btn[^"]*)" href="([^"]+)">([^<]+)</a>', hero),
                         [("btn btn-secondary", "#manual", "Have your AI agent check")])
        manual = self.page.split('id="manual"')[1]
        self.assertLess(manual.index('id="agent-prompt"'), manual.index('id="manual-commands"'))
        # Both panels use the shared code block: a labelled header strip with an icon-only copy.
        self.assertIn('<span class="v3-label">Prompt</span><button class="v3-code-copy" type="button" aria-label="Copy prompt">', self.page)
        self.assertIn('<span class="v3-label">Shell</span><button class="v3-code-copy" type="button" aria-label="Copy commands">', self.page)
        self.assertNotIn("data-copy=", self.page)
        # Readable and selectable without JavaScript; only the copy buttons and tab row need it.
        self.assertIn('<div class="mv3-tabs" role="tablist" aria-label="Ways to check" hidden>', self.page)
        for panel in ("agent", "commands", "sources"):
            self.assertRegex(self.page, rf'<div class="mv3-panel[^"]*" id="way-{panel}" role="tabpanel" aria-labelledby="tab-{panel}">')

    def test_recorded_attestation_is_a_public_document_only(self):
        fixture = json.loads((HERE / "verified-fixture.json").read_text())
        self.assertEqual(set(fixture), {"comment", "nonce", "now", "pcr0", "attestation_document"})
        self.assertFalse((self.root / "verified-fixture.json").exists(), "test data is not published")

    def test_names_the_verified_secure_badge(self):
        self.assertNotIn("Privacy Secured", self.page)
        self.assertIn('<p class="v3-label">Evidence for Maple</p>', self.page)
        self.assertIn("<title>Maple attestation evidence | NYTE Verified Secure</title>", self.page)
        self.assertIn('<a class="mv3-badge" href="/badge"><img src="/assets/badges/nyte-verified-secure-black.svg" width="221" height="96" alt="NYTE Verified Secure"></a>', self.page)

    def test_page_renders_in_the_nyte_shell_with_a_one_line_rollback(self):
        self.assertIn('data-nyte-page="maple"', self.page)
        self.assertIn('<meta property="og:title" content="Maple, checked. By you, right now.">', self.page)
        self.assertIn("/assets/nyte-v2/maple-v3.css?v=", self.page)
        self.assertIn("/assets/nyte-v2/maple-v3.js?v=", self.page)
        self.assertIn('<script src="/assets/verified.js?v=', self.page)
        self.assertIn("/assets/analytics.js?v=", self.page)
        self.assertNotIn("{{", self.page)
        self.assertEqual(self.page.count('class="v3-band"'), 1)
        self.assertIn("maple", json.loads((HERE / "nyte-brand-v3.json").read_text())["pages"])
        # Removing "maple" from nyte-brand-v3.json restores the pre-v3 page, with its sheets.
        rollback = nyte_v2.BRAND_V3_PAGES - {"maple"}
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", rollback), \
                tempfile.TemporaryDirectory() as directory:
            self.assertEqual(nyte_v2.render_nyte("maple"), (HERE / "verified-maple.html").read_text())
            build(Path(directory))
            self.assertEqual((Path(directory) / "verified" / "maple").read_text(), (HERE / "verified-maple.html").read_text())
            for name in ("badge.css", "verified.css", "verified.js", "og-new-york.png"):
                self.assertTrue((Path(directory) / "assets" / name).exists(), name)

    def test_seal_page_scopes_supplier_use_to_evidence_pages(self):
        badge = (self.root / "badge").read_text()
        self.assertIn("A supplier may display it only where NYTE publishes an evidence page", badge)
        self.assertIn('href="/verified/maple"', badge)


if __name__ == "__main__":
    unittest.main()
