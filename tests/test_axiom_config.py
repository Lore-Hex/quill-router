from __future__ import annotations

import logging

from trusted_router.axiom_config import _client_kwargs, _SafeAxiomHandler


def _fake_axiom_token() -> str:
    return "xaat_test"


class _ExplodingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("axiom dataset is not ingestible")


def test_safe_axiom_handler_never_raises_from_emit(capsys) -> None:
    handler = _SafeAxiomHandler(_ExplodingHandler())
    record = logging.LogRecord(
        name="trusted_router.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="ses_notification.signature_invalid reason=forged",
        args=(),
        exc_info=None,
    )

    handler.handle(record)
    handler.handle(record)

    captured = capsys.readouterr()
    assert "axiom.emit_failed dropped=true" in captured.err
    assert captured.err.count("axiom.emit_failed") == 1


def test_axiom_client_kwargs_use_edge_url_for_edge_deployments() -> None:
    kwargs = _client_kwargs(
        token=_fake_axiom_token(),
        org_id=None,
        axiom_url="https://eu-central-1.aws.edge.axiom.co",
    )

    assert kwargs == {
        "token": "xaat_test",
        "edge_url": "https://eu-central-1.aws.edge.axiom.co",
    }


def test_axiom_client_kwargs_keep_standard_api_url_for_non_edge() -> None:
    kwargs = _client_kwargs(
        token=_fake_axiom_token(),
        org_id="org_1",
        axiom_url="https://api.axiom.co",
    )

    assert kwargs == {
        "token": "xaat_test",
        "org_id": "org_1",
        "url": "https://api.axiom.co",
    }
