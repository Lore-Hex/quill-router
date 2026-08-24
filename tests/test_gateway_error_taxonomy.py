"""The invalid_api_key wire contract with the enclave's negative auth cache.

enclave-go/internal/authcache caches ONLY definitive invalid-credential
verdicts, discriminated by this exact type string. If the string drifts, the
cache silently never fires -- "configured, healthy, and empty" -- and every
bad key becomes a control-plane round trip again. The enclave side pins the
same literal in authcache/cache_test.go.
"""

from trusted_router.types import ErrorType


def test_invalid_api_key_type_is_the_enclave_cache_contract() -> None:
    assert ErrorType.INVALID_API_KEY == "invalid_api_key"


def test_gateway_bad_key_sites_emit_the_contract_type() -> None:
    from pathlib import Path

    gateway = Path("src/trusted_router/routes/internal/gateway.py").read_text()
    assert 'api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)' in gateway
    assert 'api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)' not in gateway
