"""Daily, at-most-once product notices for effective-dated route retirements.

Like activation reminders, claims commit before SES is called. A crash or send
failure after claiming can lose a notice; it cannot duplicate it. Preview uses
the existing durable event replay guard for one operator report per UTC day,
and never consumes customer claims.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    ModelEndpoint,
)
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.config import Settings
from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.provider_lifecycle import (
    _Retirement,
    provider_model_retired,
    provider_retirements,
)
from trusted_router.services.email import EmailMessage, EmailService, get_email_service
from trusted_router.storage import STORE

log = logging.getLogger(__name__)


_REMAINING_SHOWN = 4
_FAILOVER = (
    "Requests fail over automatically to remaining eligible routes when your "
    "provider and credential settings allow them."
)


@dataclass(frozen=True)
class RouteImpact:
    remaining: tuple[str, ...] = ()
    privacy_losses: tuple[str, ...] = ()
    replacements: tuple[str, ...] = ()

    @property
    def stops_working(self) -> bool:
        return not self.remaining

    def remaining_summary(self) -> str:
        shown = ", ".join(self.remaining[:_REMAINING_SHOWN])
        hidden = len(self.remaining) - _REMAINING_SHOWN
        return shown + (f" (+{hidden} more)" if hidden > 0 else "")

    def lines(self) -> list[str]:
        """Short, independent facts; the failover rule is stated once per email."""
        lines = (
            ["No routes remain for this model. Requests for this model will stop working."]
            if self.stops_working
            else [f"Still served by: {self.remaining_summary()}."]
        )
        lines += [
            f"This removes the model's last {label} route. Requests requiring {label} will "
            "fail after this cutover; TrustedRouter will not lower their privacy requirement."
            for label in self.privacy_losses
        ]
        if self.replacements:
            lines.append(
                "Provider-suggested replacement: " + ", ".join(self.replacements)
                + ". Changing models requires updating your request; it is not an automatic substitution."
            )
        return lines

    @property
    def needs_action(self) -> bool:
        return self.stops_working or bool(self.privacy_losses)

    @property
    def provider_count(self) -> int:
        return len({route.split(" (", 1)[0] for route in self.remaining})

    def text(self, provider: str) -> str:
        parts = [] if self.stops_working else [
            f"{_FAILOVER} Requests restricted to {provider} will fail."
        ]
        return " ".join([*self.lines(), *parts])


@dataclass(frozen=True)
class RouteNotice:
    retirement_id: str
    provider: str
    model: str
    cutover: datetime
    impact: RouteImpact

    @property
    def provider_name(self) -> str:
        provider = PROVIDERS.get(self.provider)
        return f"{provider.name} ({self.provider})" if provider else self.provider

    @property
    def when(self) -> str:
        return self.cutover.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def short_date(self) -> str:
        cutover = self.cutover.astimezone(UTC)
        day = f"{cutover:%b} {cutover.day}, {cutover.year}"
        return day if (cutover.hour, cutover.minute) == (0, 0) else f"{day}, {cutover:%H:%M} UTC"

    @property
    def provider_label(self) -> str:
        provider = PROVIDERS.get(self.provider)
        return provider.name if provider else self.provider

    def headline(self) -> str:
        return f"{self.model} — {self.provider_name} retires at {self.when}."

    def consequence(self) -> str:
        """One short sentence: what happens, and what to do if anything."""
        if self.impact.stops_working:
            switch = (
                "Switch to " + " or ".join(self.impact.replacements) + "."
                if self.impact.replacements else "Choose another model."
            )
            return f"It stops working. {switch}"
        sentence = (
            f"{self.impact.provider_count} other providers still serve it; "
            "requests switch automatically."
        )
        for label in self.impact.privacy_losses:
            short = label.split(" (", 1)[0]
            sentence += (
                f" This was its last {short} route: requests requiring {label} will fail; "
                "TrustedRouter will not lower their privacy requirement."
            )
        return sentence

    def text(self) -> str:
        return f"{self.headline()} {self.consequence()}"

    def html(self) -> str:
        model = f"<b>{html.escape(self.model)}</b>"
        date = f"<b>{html.escape(self.short_date)}</b>"
        if self.impact.stops_working:
            switch = (
                "Switch to " + " or ".join(f"<b>{html.escape(r)}</b>" for r in self.impact.replacements) + "."
                if self.impact.replacements else "Choose another model."
            )
            return f"<li>{model} stops working on {date}. {switch}</li>"
        body = (
            f"<li>{model} — {html.escape(self.provider_label)} stops serving it on {date}. "
            f"{self.impact.provider_count} other providers still serve it."
        )
        for label in self.impact.privacy_losses:
            short = html.escape(label.split(" (", 1)[0])
            body += (
                f" <b>Requests that require {short} will fail</b>: this was its last {short} route."
            )
        return body + "</li>"


@dataclass(frozen=True)
class RetirementNoticePassResult:
    workspaces: int = 0
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    preview_sent: int = 0


def route_impact(retirement: _Retirement, model: str, *, now: datetime) -> RouteImpact:
    """Project known catalog routes at the cutover, including other retirements.

    Apply every announced retirement to today's available routes. Manifest
    freshness deadlines renew hourly; they are not scheduled retirements and
    cannot predict whether a route will exist 45 days from now.
    """
    endpoints = [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.model_id == model]
    remaining = [
        endpoint for endpoint in endpoints
        if endpoint.catalog_is_current(at=now)
        and not provider_model_retired(
            endpoint.provider, model, endpoint.upstream_id, at=retirement.effective_at,
        )
    ]
    # Only the routes this retirement removes: current now, retired at the cutover.
    retiring = [
        endpoint for endpoint in endpoints
        if endpoint.provider == retirement.provider
        and endpoint.catalog_is_current(at=now)
        and provider_model_retired(
            endpoint.provider, model, endpoint.upstream_id, at=retirement.effective_at,
        )
    ]
    privacy_losses = tuple(
        label
        for label, tier in (
            ('confidential (provider.min_privacy="confidential")', PRIVACY_TIER_CONFIDENTIAL),
            ('ZDR (provider.min_privacy="zdr")', PRIVACY_TIER_ZERO_RETENTION),
        )
        if remaining
        and any(_meets(endpoint, tier) for endpoint in retiring)
        and not any(_meets(endpoint, tier) for endpoint in remaining)
    )
    return RouteImpact(
        remaining=tuple(sorted({
            f"{endpoint.provider} ({'BYOK; requires your provider key' if endpoint.is_byok else 'Credits'})"
            for endpoint in remaining
        })),
        privacy_losses=privacy_losses,
        replacements=tuple(retirement.replacement_model_ids),
    )


def retirement_impact(retirement: _Retirement, model: str, *, now: datetime) -> str:
    return route_impact(retirement, model, now=now).text(retirement.provider)


def _meets(endpoint: ModelEndpoint, tier: int) -> bool:
    return endpoint.provider in PROVIDERS and endpoint_meets_privacy_requirement(endpoint, tier)


def _recipients(workspace_id: str, service: EmailService) -> list[str]:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None or workspace.deleted or workspace.federated_home:
        return []
    user_ids = {workspace.owner_user_id} | {
        member.user_id for member in STORE.list_members(workspace_id)
        if member.role in {"owner", "admin"}
    }
    emails = set()
    for user_id in sorted(user_ids):
        user = STORE.get_user(user_id)
        if user and user.email and user.email_verified and not user.disabled and not user.suspended:
            email = user.email.strip().lower()
            if email:
                emails.add(email)
    return [email for email in sorted(emails) if service.can_receive_product_email(email)]


_FONT = (
    "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
    "font-size:14px;color:#1f2328;line-height:1.5"
)
_ACTION_BOX = "border-left:4px solid #c62828;background:#fdecea;padding:10px 14px;margin:12px 0"
_INFO_BOX = "border-left:4px solid #8c959f;background:#f6f8fa;padding:10px 14px;margin:12px 0"


def _sorted(notices: list[RouteNotice]) -> list[RouteNotice]:
    return sorted(notices, key=lambda item: (item.cutover, item.model, item.provider))


def _section(title: str, notices: list[RouteNotice], style: str) -> str:
    if not notices:
        return ""
    items = "".join(notice.html() for notice in _sorted(notices))
    return (
        f'<div style="{style}"><b>{html.escape(title)}</b>'
        f'<ul style="margin:6px 0 0;padding-left:20px">{items}</ul></div>'
    )


def build_retirement_notice_email(
    settings: Settings, *, recipient: str, workspace_id: str, notices: list[RouteNotice],
) -> EmailMessage:
    url = f"https://{settings.trusted_domain}/models"
    footer = (
        "You received this email because you are an owner or admin of this workspace "
        "and it used these routes in the last 30 days."
    )
    action = [notice for notice in notices if notice.impact.needs_action]
    info = [notice for notice in notices if not notice.impact.needs_action]
    count = len({notice.model for notice in action})
    subject = (
        f"Action needed: {count} model{'s' if count != 1 else ''} you use will stop working"
        if action else "Upcoming TrustedRouter route changes (no action needed)"
    )

    text = [f"Upcoming model changes for your TrustedRouter workspace {workspace_id}."]
    if action:
        text.append("ACTION NEEDED")
        text.extend(notice.text() for notice in _sorted(action))
    if info:
        text.append("NO ACTION NEEDED")
        text.extend(notice.text() for notice in _sorted(info))
        text.append(_FAILOVER)
    text += [f"Models and routes: {url}", footer]

    return EmailMessage(
        to=recipient,
        subject=subject,
        text_body="\n\n".join(text),
        html_body=(
            f'<div style="{_FONT};max-width:600px">'
            '<h2 style="margin:0 0 4px;font-size:20px">Upcoming model changes</h2>'
            f'<p style="margin:0;color:#57606a">Workspace <code>{html.escape(workspace_id)}</code></p>'
            + _section("Action needed", action, _ACTION_BOX)
            + _section("No action needed — requests switch providers automatically", info, _INFO_BOX)
            + f'<p><a href="{html.escape(url, quote=True)}">See models and routes</a></p>'
            + f'<p style="color:#57606a;font-size:12px">{html.escape(footer)}</p></div>'
        ),
        reply_to=settings.support_email,
        mail_class="retirement_notice",
        sender_profile="notices",
    )


def run_retirement_notice_pass(
    settings: Settings, *, now: datetime | None = None,
    analytics: OperationalAnalyticsClient | None = None, email_service: EmailService | None = None,
) -> RetirementNoticePassResult:
    if settings.retirement_notices_mode == "off":
        return RetirementNoticePassResult()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    due = [
        retirement for retirement in provider_retirements()
        if timedelta(hours=1) <= retirement.effective_at - current <= timedelta(days=45)
    ]
    if not due:
        if settings.retirement_notices_mode == "preview":
            return _preview(settings, current, {}, {}, email_service or get_email_service(settings))
        return RetirementNoticePassResult()
    reader = analytics or OperationalAnalyticsClient(
        base_url=settings.operational_analytics_clickhouse_url,
        user=settings.operational_analytics_clickhouse_user,
        password=settings.operational_analytics_clickhouse_password,
        database=settings.operational_analytics_clickhouse_database,
    )
    service = email_service or get_email_service(settings)
    by_workspace: dict[str, list[RouteNotice]] = {}
    # Complete discovery before claiming anything. An analytics outage can be
    # retried without consuming notices or delivering a partial summary.
    for retirement in sorted(due, key=lambda item: (item.effective_at, item.provider)):
        models = retirement.model_ids | {
            endpoint.model_id for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.provider == retirement.provider and endpoint.upstream_id in retirement.upstream_ids
        }
        for model in sorted(models):
            workspace_ids = reader.route_workspaces(
                provider=retirement.provider, model=model,
                start_at=(current - timedelta(days=30)).isoformat(), end_at=current.isoformat(),
            )
            notice = RouteNotice(
                retirement.notice_id, retirement.provider, model, retirement.effective_at,
                route_impact(retirement, model, now=current),
            )
            for workspace_id in workspace_ids:
                notices = by_workspace.setdefault(workspace_id, [])
                if notice not in notices:
                    notices.append(notice)

    recipients = {workspace: _recipients(workspace, service) for workspace in sorted(by_workspace)}
    if settings.retirement_notices_mode == "preview":
        return _preview(settings, current, by_workspace, recipients, service)

    claimed_count = sent = failed = 0
    for workspace_id, addresses in recipients.items():
        if not addresses:
            continue
        notices = by_workspace[workspace_id]
        claimed = STORE.claim_retirement_notices(
            workspace_id, [notice.retirement_id for notice in notices], occurred_at=current.isoformat(),
        )
        claimed_count += len(claimed)
        notices = [notice for notice in notices if notice.retirement_id in claimed]
        if not notices:
            continue
        for address in addresses:
            message = build_retirement_notice_email(
                settings, recipient=address, workspace_id=workspace_id, notices=notices,
            )
            try:
                if service.send(message):
                    sent += 1
                else:
                    failed += 1
            except Exception:  # best effort after the durable at-most-once claim
                failed += 1
                log.exception("retirement_notice.send_failed workspace_id=%s", workspace_id)
    result = RetirementNoticePassResult(len(by_workspace), claimed_count, sent, failed)
    log.info("retirement_notice.pass_completed %s", result)
    return result


def _preview(
    settings: Settings, current: datetime, by_workspace: dict[str, list[RouteNotice]],
    recipients: dict[str, list[str]], service: EmailService,
) -> RetirementNoticePassResult:
    if not by_workspace:
        # Nothing to review: log it instead of emailing the operator every day.
        log.info("retirement_notice.preview no affected workspaces")
        return RetirementNoticePassResult()
    routes: dict[tuple[datetime, str, str], tuple[RouteNotice, set[str]]] = {}
    for workspace_id, notices in by_workspace.items():
        for notice in notices:
            key = (notice.cutover, notice.model, notice.provider)
            routes.setdefault(key, (notice, set()))[1].add(workspace_id)
    ordered = [routes[key] for key in sorted(routes)]
    action = [(notice, ws) for notice, ws in ordered if notice.impact.needs_action]
    info = [(notice, ws) for notice, ws in ordered if not notice.impact.needs_action]
    total_recipients = sum(len(addresses) for addresses in recipients.values())
    title = f"Retirement notice preview for {current.date().isoformat()} (UTC)"
    summary = (
        f"{len(action)} need customer action · {len(info)} fail over automatically · "
        f"{len(by_workspace)} workspaces · {total_recipients} eligible recipients"
    )

    lines = [title, summary]
    for heading, group in (("== Action needed ==", action), ("== No action needed ==", info)):
        if group:
            lines.append(heading)
            lines.extend(f"{notice.text()} [{len(ws)} workspaces]" for notice, ws in group)
    lines += [_FAILOVER, "== Workspaces =="]
    lines.extend(
        f"Workspace {workspace_id}: {len(recipients[workspace_id])} eligible recipients — "
        f"{len(notices)} models"
        for workspace_id, notices in sorted(by_workspace.items())
    )
    body = "\n\n".join(lines)
    if not STORE.record_webhook_event_once("retirement_notice_preview", current.date().isoformat()):
        return RetirementNoticePassResult(workspaces=len(by_workspace))
    operator = settings.retirement_notices_preview_email
    if not operator:
        log.info("retirement_notice.preview\n%s", body)
        return RetirementNoticePassResult(workspaces=len(by_workspace))

    cell = 'style="padding:6px 10px;border-bottom:1px solid #d0d7de;vertical-align:top"'

    def table(group: list[tuple[RouteNotice, set[str]]], outcome: str) -> str:
        head = (
            f'<tr><th align="left" {cell}>Date</th><th align="left" {cell}>Model</th>'
            f'<th align="left" {cell}>Leaving</th><th align="left" {cell}>{html.escape(outcome)}</th>'
            f'<th align="right" {cell}>Workspaces</th></tr>'
        )
        rows = "".join(
            f"<tr><td {cell}><b>{html.escape(notice.short_date)}</b></td>"
            f"<td {cell}><b>{html.escape(notice.model)}</b></td>"
            f"<td {cell}>{html.escape(notice.provider_label)}</td>"
            f"<td {cell}>{html.escape(notice.consequence())}</td>"
            f'<td align="right" {cell}>{len(ws)}</td></tr>'
            for notice, ws in group
        )
        return f'<table style="border-collapse:collapse;width:100%">{head}{rows}</table>'

    blocks = ""
    if action:
        blocks += f'<div style="{_ACTION_BOX}"><b>Action needed ({len(action)})</b></div>' + table(action, "What happens")
    if info:
        blocks += f'<div style="{_INFO_BOX}"><b>No action needed ({len(info)})</b></div>' + table(info, "Fallback")
    workspace_rows = "".join(
        f"<li><code>{html.escape(workspace_id[:8])}</code> — {len(recipients[workspace_id])} eligible recipients, "
        f"{len(notices)} models</li>"
        for workspace_id, notices in sorted(by_workspace.items())
    )
    accepted = service.send(EmailMessage(
        to=operator,
        subject=f"Retirement preview: {len(action)} need action, {len(info)} automatic",
        text_body=body,
        html_body=(
            f'<div style="{_FONT};max-width:760px">'
            f'<h2 style="margin:0 0 4px;font-size:20px">{html.escape(title)}</h2>'
            f'<p style="margin:0 0 8px"><b>{html.escape(summary)}</b></p>'
            + blocks
            + f'<p style="color:#57606a;font-size:12px">{html.escape(_FAILOVER)}</p>'
            + '<details><summary><b>Workspaces</b></summary>'
            + f'<ul style="font-size:13px">{workspace_rows}</ul></details></div>'
        ),
        mail_class="retirement_notice_preview",
        sender_profile="notices",
    ))
    return RetirementNoticePassResult(
        workspaces=len(by_workspace), preview_sent=int(accepted), failed=int(not accepted),
    )
