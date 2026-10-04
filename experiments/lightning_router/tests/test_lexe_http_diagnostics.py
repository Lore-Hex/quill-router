import httpx
import pytest
from lightning_router.errors import FundingReviewRequired
from lightning_router.lexe import Lexe


class UnreadErrorBody(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("Error bodies must not be read for diagnostics")


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("method,path,operation", [
    ("GET", "/v2/node/client_info", "client_info"),
    ("GET", "/v2/node/node_info", "node_info"),
    ("GET", "/v2/node/payment", "payment"),
    ("GET", "/v2/node/updated_payments", "updated_payments"),
    ("POST", "/v2/node/create_invoice", "create_invoice"),
    ("POST", "/v2/node/cancel_payment", "cancel_payment"),
    ("GET", "/private-unrecognized-path", "unknown"),
    ("PRIVATE", "/v2/node/client_info", "unknown"),
])
def test_http_failure_logs_only_static_operation_and_status(status, method, path, operation, caplog):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"x-private-diagnostic": "private-response-header"},
                              stream=UnreadErrorBody())

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        error = FundingReviewRequired if status == 404 and path == "/v2/node/payment" else RuntimeError
        with pytest.raises(error):
            lexe.request(method, path, params={"index": "private-payment-index"},
                         headers={"Authorization": "Bearer private-token"})

    assert len(calls) == 1
    assert lexe._checked == 0
    messages = [record.getMessage() for record in caplog.records if record.name == "lightning_router"]
    assert messages == [f"lightning.lexe_http_failed operation={operation} http_status={status}"]
    for secret in ("private-response-header", "private-payment-index", "private-token",
                   "private-unrecognized-path", "PRIVATE", "a" * 64):
        assert secret not in caplog.text


def test_successful_http_read_does_not_emit_failure_diagnostics(caplog):
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True}))) as client:
        assert Lexe(client, "a" * 64).request("GET", "/v2/node/client_info") == {"ok": True}
    assert "lightning.lexe_http_failed" not in caplog.text
