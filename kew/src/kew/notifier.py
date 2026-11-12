"""Notification system for kew.

Provides a protocol-based notifier interface with GitHub and webhook
implementations, plus a composite notifier and factory function.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kew import subprocess_utils

if TYPE_CHECKING:
    from kew.config import DispatchConfig

logger = logging.getLogger(__name__)


@dataclass
class NotificationEvent:
    """A notification about a dispatch lifecycle event."""

    event_type: str
    severity: str
    message: str
    issue_number: int | None = None
    pr_number: int | None = None
    run_id: str | None = None
    metadata: dict[str, Any] | None = None


@runtime_checkable
class Notifier(Protocol):
    """Protocol for notification backends."""

    def notify(self, event: NotificationEvent) -> None: ...


class GitHubNotifier:
    """Posts comments on GitHub issues/PRs via the gh CLI."""

    def notify(self, event: NotificationEvent) -> None:
        body = f"**[{event.severity}]** {event.message}"

        if event.pr_number is not None:
            cmd = ["gh", "pr", "comment", str(event.pr_number), "--body", body]
        elif event.issue_number is not None:
            cmd = ["gh", "issue", "comment", str(event.issue_number), "--body", body]
        else:
            return

        subprocess_utils.run(cmd, check=False, capture=True)


class ReportDeliveryError(Exception):
    """A configured webhook POST failed (transport or HTTP >= 400)."""


class WebhookNotifier:
    """POSTs JSON payloads to a configured webhook URL."""

    def __init__(self, url: str) -> None:
        self.url = url

    def deliver(self, event: NotificationEvent) -> bool:
        """POST the event; return True iff the request succeeded.

        ``curl -sf`` exits non-zero on transport failure AND on HTTP >= 400, so
        the return value distinguishes a real delivery failure from success.
        """
        payload = json.dumps(
            {
                "text": event.message,  # additive: Slack/Mattermost render this
                "event_type": event.event_type,
                "severity": event.severity,
                "message": event.message,
                "issue_number": event.issue_number,
                "pr_number": event.pr_number,
            }
        )
        result = subprocess_utils.run(
            [
                "curl",
                "-sf",  # -f: fail (non-zero exit) on HTTP >= 400
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
                self.url,
            ],
            check=False,
        )
        return int(getattr(result, "returncode", 1)) == 0

    def notify(self, event: NotificationEvent) -> None:
        """Best-effort fire-and-forget (lifecycle pings); failures ignored."""
        self.deliver(event)


class CompositeNotifier:
    """Dispatches notifications to multiple backends."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = list(notifiers)

    def notify(self, event: NotificationEvent) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(event)
            except Exception:
                logger.warning(
                    "Notifier %s raised an exception; continuing to next notifier",
                    type(notifier).__name__,
                    exc_info=True,
                )


def build_notifier(config: DispatchConfig) -> CompositeNotifier:
    """Build a CompositeNotifier from dispatch configuration.

    Args:
        config: The dispatch configuration.

    Returns:
        A CompositeNotifier with the appropriate backends.
    """
    notifiers: list[Notifier] = []

    if config.project.repo:
        notifiers.append(GitHubNotifier())

    if config.alerting.webhook_url:
        notifiers.append(WebhookNotifier(url=config.alerting.webhook_url))

    return CompositeNotifier(notifiers)


def deliver_report(report_md: str, config: DispatchConfig) -> bool:
    """POST a rendered report to the configured webhook.

    Returns ``False`` when no webhook is configured (a caller-facing no-op).
    Raises ``ReportDeliveryError`` when a configured webhook POST fails, so a
    scheduled `kew report --notify` surfaces a real delivery failure instead of
    reporting success — distinct from the no-config case.
    """
    if not config.alerting.webhook_url:
        return False
    ok = WebhookNotifier(config.alerting.webhook_url).deliver(
        NotificationEvent(event_type="report", severity="info", message=report_md)
    )
    if not ok:
        # Don't echo the URL — webhook URLs often embed a secret token.
        raise ReportDeliveryError("webhook POST failed (check [alerting] webhook_url)")
    return True


def notify_approval_hold(
    config: DispatchConfig,
    *,
    issue_num: int,
    estimate_usd: float,
    basis: str,
    threshold_usd: float,
    notifier: Notifier | None = None,
) -> None:
    """Tell the governor a hold exists and how to release it (gated, best-effort)."""
    if not config.alerting.notify_on_hold:
        return
    message = (
        f"⏸ Issue #{issue_num} is held for approval: estimated "
        f"${estimate_usd:.2f} ({basis}) ≥ the ${threshold_usd:.2f} gate. "
        f"Release it with `kew approve {issue_num}` or `kew deny {issue_num}` "
        f"on the kew host. Commenting `/approve [reason]` or `/deny [reason]` "
        f"on this issue works only when the repo has the "
        f"`.github/workflows/approval.yml` workflow installed; neither "
        f"`kew setup` nor `create-kew` installs it."
    )
    event = NotificationEvent(
        event_type="approval_hold",
        severity="warning",
        message=message,
        issue_number=issue_num,
        metadata={"estimate_usd": estimate_usd, "threshold_usd": threshold_usd},
    )
    (notifier or build_notifier(config)).notify(event)
