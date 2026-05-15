from __future__ import annotations

import logging

from trusted_router.axiom_config import _SafeAxiomHandler


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
