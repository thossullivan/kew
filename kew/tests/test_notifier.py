"""Tests for the notifier module."""

import subprocess
from unittest.mock import patch

from kew.config import DispatchConfig
from kew.notifier import (
    CompositeNotifier,
    GitHubNotifier,
    NotificationEvent,
    Notifier,
    WebhookNotifier,
    build_notifier,
    deliver_report,
    notify_approval_hold,
)

# ── NotificationEvent ──────────────────────────────────────────────────


class TestNotificationEvent:
    def test_minimal_creation(self):
        event = NotificationEvent(
            event_type="dispatch_started",
            severity="info",
            message="Run started",
        )
        assert event.event_type == "dispatch_started"
        assert event.severity == "info"
        assert event.message == "Run started"
        assert event.issue_number is None
        assert event.pr_number is None
        assert event.run_id is None
        assert event.metadata is None

    def test_full_creation(self):
        event = NotificationEvent(
            event_type="dispatch_failed",
            severity="critical",
            message="CI failed",
            issue_number=42,
            pr_number=99,
            run_id="abc-123",
            metadata={"attempt": 2},
        )
        assert event.event_type == "dispatch_failed"
        assert event.severity == "critical"
        assert event.message == "CI failed"
        assert event.issue_number == 42
        assert event.pr_number == 99
        assert event.run_id == "abc-123"
        assert event.metadata == {"attempt": 2}


# ── Notifier protocol ─────────────────────────────────────────────────


class TestNotifierProtocol:
    def test_github_notifier_satisfies_protocol(self):
        assert isinstance(GitHubNotifier(), Notifier)

    def test_protocol_is_runtime_checkable(self):
        class FakeNotifier:
            def notify(self, event: NotificationEvent) -> None:
                pass

        assert isinstance(FakeNotifier(), Notifier)


# ── GitHubNotifier ─────────────────────────────────────────────────────


class TestGitHubNotifier:
    def test_posts_pr_comment(self):
        event = NotificationEvent(
            event_type="dispatch_failed",
            severity="critical",
            message="CI failed after 3 attempts",
            pr_number=42,
        )
        with patch("kew.notifier.subprocess_utils") as mock_sub:
            mock_sub.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0
            )
            GitHubNotifier().notify(event)
            mock_sub.run.assert_called_once_with(
                [
                    "gh",
                    "pr",
                    "comment",
                    "42",
                    "--body",
                    "**[critical]** CI failed after 3 attempts",
                ],
                check=False,
                capture=True,
            )

    def test_posts_issue_comment(self):
        event = NotificationEvent(
            event_type="dispatch_started",
            severity="info",
            message="Dispatch started",
            issue_number=10,
        )
        with patch("kew.notifier.subprocess_utils") as mock_sub:
            mock_sub.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0
            )
            GitHubNotifier().notify(event)
            mock_sub.run.assert_called_once_with(
                [
                    "gh",
                    "issue",
                    "comment",
                    "10",
                    "--body",
                    "**[info]** Dispatch started",
                ],
                check=False,
                capture=True,
            )

    def test_prefers_pr_over_issue(self):
        event = NotificationEvent(
            event_type="ci_fix_triggered",
            severity="warning",
            message="Retrying CI",
            issue_number=10,
            pr_number=42,
        )
        with patch("kew.notifier.subprocess_utils") as mock_sub:
            mock_sub.run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0
            )
            GitHubNotifier().notify(event)
            mock_sub.run.assert_called_once_with(
                ["gh", "pr", "comment", "42", "--body", "**[warning]** Retrying CI"],
                check=False,
                capture=True,
            )

    def test_skips_silently_when_no_target(self):
        event = NotificationEvent(
            event_type="dispatch_started",
            severity="info",
            message="No target",
        )
        with patch("kew.notifier.subprocess_utils") as mock_sub:
            GitHubNotifier().notify(event)
            mock_sub.run.assert_not_called()


# ── build_notifier ─────────────────────────────────────────────────────


def _make_config(repo: str = "", webhook_url: str = "") -> DispatchConfig:
    """Create a DispatchConfig with optional repo and webhook_url."""
    raw: dict = {}
    if repo:
        raw["project"] = {"repo": repo}
    if webhook_url:
        raw["alerting"] = {"webhook_url": webhook_url}
    return DispatchConfig.model_validate(raw)


class TestBuildNotifier:
    """Tests for the build_notifier factory function."""

    def test_returns_composite_notifier(self) -> None:
        config = _make_config()
        result = build_notifier(config)
        assert isinstance(result, CompositeNotifier)

    def test_includes_github_notifier_when_repo_set(self) -> None:
        config = _make_config(repo="owner/repo")
        result = build_notifier(config)
        github_notifiers = [
            n for n in result.notifiers if isinstance(n, GitHubNotifier)
        ]
        assert len(github_notifiers) == 1

    def test_includes_webhook_notifier_when_url_set(self) -> None:
        config = _make_config(
            repo="owner/repo", webhook_url="https://hooks.example.com/dispatch"
        )
        result = build_notifier(config)
        webhook_notifiers = [
            n for n in result.notifiers if isinstance(n, WebhookNotifier)
        ]
        assert len(webhook_notifiers) == 1
        assert webhook_notifiers[0].url == "https://hooks.example.com/dispatch"

    def test_excludes_webhook_notifier_when_url_empty(self) -> None:
        config = _make_config(repo="owner/repo", webhook_url="")
        result = build_notifier(config)
        webhook_notifiers = [
            n for n in result.notifiers if isinstance(n, WebhookNotifier)
        ]
        assert len(webhook_notifiers) == 0

    def test_excludes_github_notifier_when_repo_empty(self) -> None:
        config = _make_config(repo="")
        result = build_notifier(config)
        assert len(result.notifiers) == 0

    def test_both_notifiers_when_repo_and_webhook_set(self) -> None:
        config = _make_config(
            repo="owner/repo", webhook_url="https://hooks.example.com/dispatch"
        )
        result = build_notifier(config)
        assert len(result.notifiers) == 2
        types = {type(n) for n in result.notifiers}
        assert types == {GitHubNotifier, WebhookNotifier}

    def test_no_notifiers_when_nothing_configured(self) -> None:
        config = _make_config()
        result = build_notifier(config)
        assert len(result.notifiers) == 0


# ── CompositeNotifier ──────────────────────────────────────────────────


class TestCompositeNotifier:
    """Tests for CompositeNotifier dispatch behavior."""

    def test_dispatches_to_all_notifiers(self) -> None:
        calls: list[NotificationEvent] = []

        class FakeNotifier:
            def notify(self, event: NotificationEvent) -> None:
                calls.append(event)

        composite = CompositeNotifier([FakeNotifier(), FakeNotifier()])
        event = NotificationEvent(
            event_type="test",
            severity="info",
            message="Test event",
        )
        composite.notify(event)
        assert len(calls) == 2

    def test_empty_composite_does_not_error(self) -> None:
        composite = CompositeNotifier([])
        event = NotificationEvent(
            event_type="test",
            severity="info",
            message="Test event",
        )
        composite.notify(event)  # Should not raise

    def test_error_in_first_notifier_does_not_block_second(self) -> None:
        """If the first notifier raises, the second must still be called."""
        second_calls: list[NotificationEvent] = []

        class RaisingNotifier:
            def notify(self, event: NotificationEvent) -> None:
                raise RuntimeError("first notifier exploded")

        class RecordingNotifier:
            def notify(self, event: NotificationEvent) -> None:
                second_calls.append(event)

        composite = CompositeNotifier([RaisingNotifier(), RecordingNotifier()])
        event = NotificationEvent(
            event_type="test",
            severity="info",
            message="Test event",
        )
        composite.notify(event)  # Should not raise
        assert len(second_calls) == 1, (
            "Second notifier should have been called despite first raising"
        )


# ── WebhookNotifier text key ───────────────────────────────────────────


def test_webhook_payload_includes_text_key():
    captured = {}

    def fake_run(cmd, **kw):
        captured["payload"] = cmd[cmd.index("-d") + 1]

        class R:  # minimal CompletedProcess stand-in
            returncode = 0

        return R()

    with patch("kew.notifier.subprocess_utils.run", side_effect=fake_run):
        WebhookNotifier("https://hook").notify(
            NotificationEvent(event_type="x", severity="info", message="hello")
        )
    import json

    payload = json.loads(captured["payload"])
    assert payload["text"] == "hello"  # additive Slack-compatible key
    assert payload["message"] == "hello"  # existing key preserved


# ── deliver_report ─────────────────────────────────────────────────────


def test_deliver_report_no_webhook_returns_false():
    config = DispatchConfig()
    config.alerting.webhook_url = ""
    assert deliver_report("# report", config) is False


def test_deliver_report_posts_when_configured():
    config = DispatchConfig()
    config.alerting.webhook_url = "https://hook"
    with patch("kew.notifier.subprocess_utils.run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0})()
        assert deliver_report("# report body", config) is True
    sent = mock_run.call_args[0][0]
    assert "https://hook" in sent


def test_deliver_report_raises_on_transport_failure():
    # A configured webhook whose POST fails (curl non-zero) must raise, not
    # silently report success.
    import pytest

    from kew.notifier import ReportDeliveryError

    config = DispatchConfig()
    config.alerting.webhook_url = "https://hook"
    with patch("kew.notifier.subprocess_utils.run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 22})()  # curl -f HTTP>=400
        with pytest.raises(ReportDeliveryError):
            deliver_report("# report body", config)


# ── notify_approval_hold ───────────────────────────────────────────────


def test_notify_approval_hold_fires_event_with_instructions():
    config = DispatchConfig()
    sent = []

    class FakeNotifier:
        def notify(self, event):
            sent.append(event)

    notify_approval_hold(
        config,
        issue_num=42,
        estimate_usd=6.10,
        basis="median of 8",
        threshold_usd=5.0,
        notifier=FakeNotifier(),
    )
    assert len(sent) == 1
    assert sent[0].event_type == "approval_hold"
    assert sent[0].issue_number == 42
    assert "/approve" in sent[0].message and "/deny" in sent[0].message


def test_notify_approval_hold_suppressed_when_disabled():
    config = DispatchConfig()
    config.alerting.notify_on_hold = False
    sent = []

    class FakeNotifier:
        def notify(self, event):
            sent.append(event)

    notify_approval_hold(
        config,
        issue_num=42,
        estimate_usd=6.10,
        basis="b",
        threshold_usd=5.0,
        notifier=FakeNotifier(),
    )
    assert sent == []


def test_approval_hold_notice_names_kew_approve_and_what_slash_approve_needs():
    """`kew setup` installs no approval workflow, so the notice leads with the
    host command, which needs no workflow, and says what the comment path needs."""
    config = DispatchConfig()
    sent = []

    class FakeNotifier:
        def notify(self, event):
            sent.append(event)

    notify_approval_hold(
        config,
        issue_num=42,
        estimate_usd=6.10,
        basis="median of 8",
        threshold_usd=5.0,
        notifier=FakeNotifier(),
    )
    message = sent[0].message
    assert "`kew approve 42`" in message and "`kew deny 42`" in message
    assert "`/approve" in message
    assert "`.github/workflows/approval.yml`" in message
    assert message.index("kew approve 42") < message.index("/approve")
