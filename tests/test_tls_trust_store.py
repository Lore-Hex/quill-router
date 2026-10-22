"""Offline regression checks for the October 2026 eMudhra CA compromise."""

import json
from datetime import timedelta
from pathlib import Path

import certifi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

FIXTURES = Path(__file__).parent / "fixtures" / "emudhra"
BLOCKED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
}


def test_certifi_excludes_compromised_trust_paths():
    roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    fingerprints = {cert.fingerprint(hashes.SHA256()).hex() for cert in roots}
    assert not fingerprints & BLOCKED_ROOTS
    names = {cert.subject.rfc4514_string() for cert in roots}
    assert not any("CN=emSign ECC Root CA - G3" in name for name in names)
    assert not any("CN=AAA Certificate Services" in name for name in names)
    assert any("CN=ISRG Root X1" in name for name in names)
    assert any("CN=GTS Root R1" in name for name in names)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.pem")), ids=lambda p: p.stem)
def test_compromised_public_chains_are_rejected_offline(path):
    chain = x509.load_pem_x509_certificates(path.read_bytes())
    when = chain[0].not_valid_before_utc + timedelta(hours=1)
    for cert in chain:
        assert cert.not_valid_before_utc <= when < cert.not_valid_after_utc
    for child, issuer in zip(chain, chain[1:], strict=False):
        child.verify_directly_issued_by(issuer)
    store = Store(x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes()))
    verifier = PolicyBuilder().store(store).time(when).build_server_verifier(x509.DNSName(path.stem))
    with pytest.raises(VerificationError, match="candidates exhausted"):
        verifier.verify(chain[0], chain[1:])

    # A negative test must not pass because its fixture is malformed. Restoring
    # only the old roots must make this exact chain and hostname verify.
    old_roots = [
        x509.load_pem_x509_certificate(root["pem"].encode())
        for root in json.loads((FIXTURES / "roots.json").read_text())
    ]
    old_verifier = (
        PolicyBuilder().store(Store(old_roots)).time(when)
        .build_server_verifier(x509.DNSName(path.stem))
    )
    old_verifier.verify(chain[0], chain[1:])


def test_all_compromised_tls_paths_are_covered():
    assert len(list(FIXTURES.glob("*.pem"))) == 12
