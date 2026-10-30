"""The control plane's fast path lines, held to the metrics and policies that
watch them (infra/fastpath_control_plane_alerts.tf;
docs/design/fast-admission-streams.md sections 4.4 and 6.4): each filter is
built from the Terraform's own locals and matched against the line the code
writes, rendered by the formatter Cloud Run's textPayload carries."""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any

from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.services import fastpath_heartbeat, fastpath_settle
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "fastpath_control_plane_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbooks" / "fastpath-alerts.md"


def _events() -> dict[str, str]:
    tf = TERRAFORM.read_text(encoding="utf-8")
    block = re.search(r"fastpath_control_plane_lines = \{(.*?)\n  \}", tf, re.S)
    assert block
    return dict(re.findall(r'^\s+(\w+)\s+= "([^"]+)"$', block.group(1), re.M))


def _payload_match(event: str) -> str:
    """The quoted textPayload substring the filter carries for an event, as
    the Terraform's template builds it."""
    tf = TERRAFORM.read_text(encoding="utf-8")
    template = re.search(r'"textPayload:\\"(.*?)\\""', tf)
    assert template
    return template.group(1).replace("${event}", event)


class _Lines(logging.Handler):
    """The fast path logger's lines, as the console handler renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(_ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _capture() -> tuple[_Lines, logging.Logger]:
    handler = _Lines()
    logger = logging.getLogger("trusted_router.fastpath")
    logger.addHandler(handler)
    return handler, logger


def _authorization() -> GatewayAuthorization:
    return GatewayAuthorization(id="gwa-alert", workspace_id="ws", key_hash="k", model_id="m", provider="anthropic",
                                usage_type=UsageType.CREDITS, estimated_microdollars=500, credit_reservation_id="r",
                                fastpath_envelope="sealed", fastpath_end_of_life="2099-01-01T00:00:00Z",
                                stage_d_boot_kid="boot", stage_d_prompt_tokens=1, stage_d_max_output_tokens=1)


class _Client:
    def __init__(self, heartbeat: fd.HeartbeatAnswer | None = None, disposition: fd.Disposition | None = None) -> None:
        self._heartbeat, self._disposition = heartbeat, disposition

    def heartbeat(self, **_kwargs: Any) -> fd.HeartbeatAnswer:
        assert self._heartbeat is not None
        return self._heartbeat

    def disposition(self, **_kwargs: Any) -> fd.Disposition:
        assert self._disposition is not None
        return self._disposition


class _Store:
    def __init__(self, authorization: GatewayAuthorization) -> None:
        self.authorization = authorization
        self.extended: list[tuple[str, dt.datetime]] = []

    def get_gateway_authorization(self, _authorization_id: str) -> GatewayAuthorization:
        return self.authorization

    def extend_reservation_expiry(self, reservation_id: str, until: dt.datetime) -> bool:
        self.extended.append((reservation_id, until))
        return True


def _rendered(kind: str) -> list[str]:
    """The lines the code writes for a kind, rendered."""
    handler, logger = _capture()
    try:
        if kind == "heartbeat_divergence":
            for status in ("rejected", "invalid"):
                fastpath_heartbeat.forward(
                    _Client(heartbeat=fd.HeartbeatAnswer(status=status, deadline=None)),  # type: ignore[arg-type]
                    _authorization(), seq=2, digest=b"\x00" * 32,
                    counts={"input_tokens": 1, "output_tokens": 1}, running=1, selected_endpoint_id="e",
                )
        elif kind == "settled_without_intent":
            fastpath_settle.reconcile_reaped(
                _Client(disposition=fd.Disposition(status="settled", outcome="settled", cost=9, cost_known=True)),  # type: ignore[arg-type]
                _Store(_authorization()), "gwa-alert", now=dt.datetime.now(dt.UTC), grace_seconds=300,
            )
        else:
            raise AssertionError(kind)
    finally:
        logger.removeHandler(handler)
    return handler.lines


def test_each_filter_matches_the_line_the_code_writes_and_no_other() -> None:
    events = _events()
    assert events == {
        "heartbeat_divergence": "fastpath.heartbeat_divergence",
        "settled_without_intent": "fastpath.settled_without_intent",
    }
    for kind, event in events.items():
        match = _payload_match(event)
        lines = _rendered(kind)
        hits = [line for line in lines if match in line]
        assert hits, (kind, match, lines)
        assert all("gwa-alert" in line for line in hits), hits
        # A longer event name, another level or another logger is not it.
        assert match not in f"ERROR trusted_router.fastpath {event}_x authorization_id=a"
        assert match not in f"WARNING trusted_router.fastpath {event} authorization_id=a"
        assert match not in f"ERROR trusted_router.other {event} authorization_id=a"


def test_the_divergence_line_is_written_once_per_divergence_only() -> None:
    handler, logger = _capture()
    try:
        for status in ("accepted", "stale", "decided", "deadline_passed", "retry"):
            answer = fd.HeartbeatAnswer(
                status=status, deadline=dt.datetime.now(dt.UTC) if status == "accepted" else None,
            )
            fastpath_heartbeat.forward(
                _Client(heartbeat=answer), _authorization(), seq=2, digest=b"\x00" * 32,  # type: ignore[arg-type]
                counts={"input_tokens": 1, "output_tokens": 1}, running=1, selected_endpoint_id="e",
            )
    finally:
        logger.removeHandler(handler)
    match = _payload_match("fastpath.heartbeat_divergence")
    assert not [line for line in handler.lines if match in line]


def test_the_metric_and_the_policy_read_one_filter_and_page_as_the_nodes_do() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    assert tf.count("filter      = local.fastpath_control_plane_filters[each.key]") == 1
    assert tf.count("filter = local.fastpath_control_plane_filters[each.key]") == 1
    assert '"resource.type=\\"cloud_run_revision\\""' in tf
    assert tf.count("for_each = local.fastpath_control_plane_lines") == 2
    assert re.search(r'notification_rate_limit \{\n\s+period = "300s"\n\s+\}\n\s+auto_close = "1800s"', tf)
    assert "notification_channels = local.fastpath_alert_channels" in tf
    assert 'metric_kind = "DELTA"' in tf and 'value_type  = "INT64"' in tf


def test_every_kind_has_a_runbook_row() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for kind in _events():
        assert re.search(rf"^\| `{kind}` \| ", runbook, re.M), kind
