"""Opt-in P3 process hygiene; importing this module installs no pytest hooks."""
import sys
import threading
import tracemalloc

import pytest


@pytest.fixture(autouse=True)
def package_hygiene(request):
    trace = sys.gettrace()
    threads = frozenset(t for t in threading.enumerate() if not t.daemon)
    yield
    cancel = getattr(request.config.hook, "pytest_timeout_cancel_timer", None)
    if cancel is not None:
        cancel(item=request.node)
    assert not tracemalloc.is_tracing(), f"{request.node.nodeid} leaked tracemalloc"
    assert sys.gettrace() is trace, f"{request.node.nodeid} replaced the trace function"
    extra = [t for t in threading.enumerate() if not t.daemon and t not in threads]
    assert not extra, f"{request.node.nodeid} leaked non-daemon threads: {extra}"
