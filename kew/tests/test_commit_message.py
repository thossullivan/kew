"""The host-built publication commit describes the change, not the ceremony (#2062)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kew.commit_message import (
    MIN_DESCRIPTION,
    RUN_TRAILER,
    SUBJECT_LIMIT,
    build_commit_message,
    report_summary_line,
    sanitize_commit_subject,
    strip_closing_references,
)
from kew.config import DispatchConfig
from kew.dispatch_core import _broker_commit_message
from kew.pr_text import break_closing_references
from kew.tracking import DispatchResult
from kew.transcript_metrics import DerivedRunEvidence, write_derived_evidence

PREFIX = "feat(#123): "


def _subject(message: str) -> str:
    return message.splitlines()[0]


def _body(message: str) -> str:
    return "\n".join(message.splitlines()[1:]).strip()


def _report(what_was_done: str) -> str:
    return (
        "## Dispatch Report\n\n"
        "**Issue:** #123 — something\n\n"
        "### What was done\n"
        f"{what_was_done}\n\n"
        "### Decisions made\n"
        "- none\n"
    )


def test_subject_is_the_prefixed_issue_title() -> None:
    message = build_commit_message(item_id="123", prefix=PREFIX, title="X")

    assert _subject(message) == "feat(#123): X"


def test_body_names_the_run_and_keeps_the_attribution() -> None:
    message = build_commit_message(
        item_id="123", prefix=PREFIX, title="Add a widget", run_id="run-7"
    )

    assert _subject(message) == "feat(#123): Add a widget"
    assert _body(message) == (
        "complete work for #123 (committed by kew)\n\nKew-Run-Id: run-7"
    )


def test_subject_is_one_line_however_the_title_was_written() -> None:
    message = build_commit_message(
        item_id="123",
        prefix=PREFIX,
        title="Add\na widget\twith  spaces\r\n",
    )

    assert _subject(message) == "feat(#123): Add a widget with spaces"
    assert _body(message) == "complete work for #123 (committed by kew)"


def test_a_long_title_is_capped_on_a_word_boundary() -> None:
    title = "Rebuild the publication commit subject from the issue title " + (
        "and then keep going well past any sane subject width"
    )

    message = build_commit_message(item_id="123", prefix=PREFIX, title=title)

    subject = _subject(message)
    assert len(subject) <= SUBJECT_LIMIT
    assert subject.startswith("feat(#123): Rebuild the publication commit")
    assert subject.endswith("…")
    assert not subject.removesuffix("…").endswith(" ")


def test_a_long_prefix_never_starves_the_description() -> None:
    prefix = "a very long configured commit prefix that eats the whole width: "

    message = build_commit_message(item_id="123", prefix=prefix, title="D" * 200)

    assert _subject(message) == f"{prefix}{'D' * (MIN_DESCRIPTION - 1)}…"


def test_the_agents_own_summary_line_beats_the_issue_title() -> None:
    message = build_commit_message(
        item_id="123",
        prefix=PREFIX,
        title="Host-built publication commit has a generic title",
        report=_report("- Rewrote the host commit subject builder"),
        run_id="run-7",
    )

    assert _subject(message) == "feat(#123): Rewrote the host commit subject builder"
    assert f"{RUN_TRAILER}: run-7" in _body(message)


@pytest.mark.parametrize(
    "what_was_done",
    ["- [List each task/change implemented]", "", "1) ", "-"],
    ids=["unfilled-placeholder", "empty-section", "empty-numbered-item", "bare-marker"],
)
def test_an_unusable_report_section_falls_back_to_the_title(
    what_was_done: str,
) -> None:
    message = build_commit_message(
        item_id="123", prefix=PREFIX, title="X", report=_report(what_was_done)
    )

    assert _subject(message) == "feat(#123): X"


def test_a_report_without_the_section_falls_back_to_the_title() -> None:
    message = build_commit_message(
        item_id="123",
        prefix=PREFIX,
        title="X",
        report="## Dispatch Report\n\n### Testing\n- ran pytest\n",
    )

    assert _subject(message) == "feat(#123): X"


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("- ", "Rewrote it"),
        ("* ", "Rewrote it"),
        ("+ ", "Rewrote it"),
        ("1. ", "Rewrote it"),
        ("2) ", "Rewrote it"),
        ("", "Rewrote it"),
    ],
    ids=["dash", "star", "plus", "ordered-dot", "ordered-paren", "no-marker"],
)
def test_every_list_marker_yields_the_bare_summary(marker: str, expected: str) -> None:
    message = build_commit_message(
        item_id="123", prefix=PREFIX, report=_report(f"{marker}Rewrote it")
    )

    assert _subject(message) == f"feat(#123): {expected}"


def test_nothing_to_describe_keeps_the_historical_attribution_subject() -> None:
    assert (
        build_commit_message(item_id="123", prefix=PREFIX)
        == "feat(#123): complete work for #123 (committed by kew)"
    )


def test_nothing_to_describe_still_names_the_run() -> None:
    message = build_commit_message(item_id="123", prefix=PREFIX, run_id="run-7")

    assert _subject(message) == "feat(#123): complete work for #123 (committed by kew)"
    assert _body(message) == f"{RUN_TRAILER}: run-7"


def test_sanitize_drops_control_characters() -> None:
    assert sanitize_commit_subject("a\x00b\x1fc\x7fd") == "a b c d"


def test_report_summary_line_of_nothing_is_none() -> None:
    assert report_summary_line(None) is None
    assert report_summary_line("") is None


def test_broker_commit_message_renders_the_configured_prefix() -> None:
    config = DispatchConfig()
    config.commands.commit_prefix = "change(#{issue}): "

    message = _broker_commit_message("123", config, title="X", run_id="run-7")

    assert _subject(message) == "change(#123): X"
    assert f"{RUN_TRAILER}: run-7" in message


def test_broker_commit_message_reads_the_runs_report_sidecar(tmp_path: Path) -> None:
    log_file = tmp_path / "run.jsonl"
    log_file.write_text("", encoding="utf-8")
    write_derived_evidence(
        log_file, DerivedRunEvidence(report=_report("- Rewrote the subject builder"))
    )

    message = _broker_commit_message(
        "123", DispatchConfig(), title="X", run_id="run-7", log_file=log_file
    )

    assert _subject(message) == "feat(#123): Rewrote the subject builder"


def test_broker_commit_message_survives_an_unreadable_sidecar(tmp_path: Path) -> None:
    message = _broker_commit_message(
        "123",
        DispatchConfig(),
        title="X",
        log_file=tmp_path / "never-written.jsonl",
    )

    assert _subject(message) == "feat(#123): X"


def _checkout_config() -> DispatchConfig:
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    return config


def _dirty_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git worktree with one uncommitted file, ready for the ceremony."""
    from kew.subprocess_utils import TOOL_PATHS

    git_path = shutil.which("git")
    assert git_path is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git_path)

    work = tmp_path / "wt"
    work.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "kew@example.invalid"],
        ["git", "config", "user.name", "kew"],
    ):
        subprocess.run(argv, cwd=work, check=True)
    (work / "widget.py").write_text("print('hi')\n", encoding="utf-8")
    return work


def _committed_message(work: Path) -> tuple[str, str]:
    """(subject, body) of the commit that landed in *work*."""
    shown = subprocess.run(
        ["git", "log", "-1", "--format=%s%n--%n%b"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subject, body = shown.split("\n--\n", 1)
    return subject, body


def _run_log(tmp_path: Path, what_was_done: str) -> Path:
    """A run log whose evidence sidecar carries a Dispatch Report."""
    log_file = tmp_path / "run.jsonl"
    log_file.write_text("", encoding="utf-8")
    write_derived_evidence(log_file, DerivedRunEvidence(report=_report(what_was_done)))
    return log_file


def test_the_commit_kew_writes_carries_the_title_and_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the git object itself, not just the rendered string."""
    from kew.dispatch_core import _commit_all_if_dirty

    work = _dirty_worktree(tmp_path, monkeypatch)

    assert _commit_all_if_dirty(
        work, "123", _checkout_config(), title="Add a widget", run_id="run-7"
    )

    subject, body = _committed_message(work)
    assert subject == "feat(#123): Add a widget"
    assert "complete work for #123 (committed by kew)" in body
    assert f"{RUN_TRAILER}: run-7" in body


# --- the production plumbing: every path that publishes kew's own commit -----
#
# Each edge below is asserted on the *message the ceremony carries*, not on the
# keyword it was passed: drop ``title``, ``run_id`` or ``log_file`` anywhere
# between a caller and ``_broker_commit_message`` and one of these fails (review
# round: both plumbing edges were untested, so a refactor could revert #2062).

_PR_URL = "https://github.invalid/o/r/pull/12"


class _CapturedPush:
    """The publication push, recording the commit message it was handed."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, *_args: object, **kwargs: object) -> MagicMock:
        message = kwargs["message"]
        assert isinstance(message, str)
        self.messages.append(message)
        return MagicMock()

    @property
    def subject(self) -> str:
        assert len(self.messages) == 1, self.messages
        return _subject(self.messages[0])


@contextmanager
def _ceremony(mode: str = "checkout") -> Iterator[_CapturedPush]:
    """The host git ceremony with only the remote-touching steps stubbed."""
    from kew import dispatch_broker, dispatch_core

    push = _CapturedPush()
    with (
        patch.object(dispatch_core, "_git_push_branch", push),
        patch.object(dispatch_core, "select_publication_mode", return_value=mode),
        patch.object(dispatch_broker, "select_publication_mode", return_value=mode),
        patch.object(dispatch_core, "_require_publication_repo", return_value="o/r"),
        patch.object(dispatch_core, "_create_pull_request", return_value=(_PR_URL, 12)),
        patch.object(dispatch_core, "_apply_created_pr_labels"),
        patch.object(dispatch_core, "_persist_pr_url"),
    ):
        yield push


def test_the_host_broker_publishes_the_agents_summary_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#518 broker, checkout mode: the report reaches both commit and push."""
    from kew.dispatch_broker import _broker_agent_work

    work = _dirty_worktree(tmp_path, monkeypatch)
    log_file = _run_log(tmp_path, "- Rewrote the host commit subject builder")

    with _ceremony() as push:
        assert _broker_agent_work(
            work,
            "agent/issue-123",
            "123",
            "Issue title nobody reads",
            _checkout_config(),
            run_id="run-7",
            log_file=log_file,
        )

    expected = "feat(#123): Rewrote the host commit subject builder"
    subject, body = _committed_message(work)
    assert subject == expected
    assert f"{RUN_TRAILER}: run-7" in body
    assert push.subject == expected


def test_the_host_broker_falls_back_to_the_issue_title_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No report: the broker still carries the title and run it was given."""
    from kew.dispatch_broker import _broker_agent_work

    work = _dirty_worktree(tmp_path, monkeypatch)

    with _ceremony() as push:
        assert _broker_agent_work(
            work,
            "agent/issue-123",
            "123",
            "Add a widget",
            _checkout_config(),
            run_id="run-7",
            log_file=tmp_path / "never-written.jsonl",
        )

    subject, body = _committed_message(work)
    assert subject == "feat(#123): Add a widget"
    assert f"{RUN_TRAILER}: run-7" in body
    assert push.subject == "feat(#123): Add a widget"


def test_the_canonical_broker_path_publishes_the_agents_summary_line(
    tmp_path: Path,
) -> None:
    """Canonical mode commits inside the push, so the report must reach it."""
    from kew.dispatch_broker import _broker_agent_work

    log_file = _run_log(tmp_path, "- Rewrote the host commit subject builder")

    with _ceremony("canonical") as push:
        assert _broker_agent_work(
            tmp_path / "checkout",
            "agent/issue-123",
            "123",
            "Issue title nobody reads",
            DispatchConfig(),
            run_id="run-7",
            log_file=log_file,
        )

    assert push.subject == "feat(#123): Rewrote the host commit subject builder"


def test_the_rescue_path_publishes_the_agents_summary_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incomplete-run rescue draft (#518) describes the change too."""
    from kew import dispatch_core

    work = _dirty_worktree(tmp_path, monkeypatch)
    log_file = _run_log(tmp_path, "- Got the parser most of the way there")

    with (
        _ceremony() as push,
        patch.object(dispatch_core, "check_pr_exists", return_value=0),
        patch.object(dispatch_core, "mark_needs_review_incomplete"),
    ):
        dispatch_core._rescue_incomplete_run(
            work,
            "agent/issue-123",
            123,
            "Issue title nobody reads",
            _checkout_config(),
            item_id="123",
            run_id="run-7",
            log_file=log_file,
        )

    expected = "feat(#123): Got the parser most of the way there"
    subject, body = _committed_message(work)
    assert subject == expected
    assert f"{RUN_TRAILER}: run-7" in body
    assert push.subject == expected


def _dispatch_context(tmp_path: Path) -> object:
    """The per-dispatch state the host publication call sites read from."""
    from kew import dispatch_core

    config = _checkout_config()
    config.logging.dir = str(tmp_path / "logs")
    return dispatch_core._DispatchContext(
        config=config,
        store=None,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=None,
        provider="github",
        issue_num=123,
        issue_title="Issue title nobody reads",
        issue_body="",
        complexity="simple",
        item_id="123",
        branch="agent/issue-123",
        publication_base_sha="c" * 40,
        start_monotonic=0.0,
        refuse_rollback=lambda _reason: None,
        terminal_failure=None,
        run_kind="dispatch",
    )


def test_the_host_broker_call_site_hands_over_the_runs_log(tmp_path: Path) -> None:
    """The broker can only read the report if the run's log reaches it."""
    from kew import dispatch_core

    log_file = _run_log(tmp_path, "- Rewrote the host commit subject builder")
    result = DispatchResult(
        exit_code=0, issue_num=123, log_file=log_file, run_id="run-7"
    )

    with (
        patch.object(dispatch_core, "_broker_agent_work", return_value=True) as broker,
        patch.object(dispatch_core, "_finalize_brokered_pr"),
    ):
        dispatch_core._broker_or_complete(
            _dispatch_context(tmp_path), result, tmp_path, False
        )

    assert broker.call_args.kwargs["log_file"] == log_file


def test_the_rescue_call_site_hands_over_the_runs_log(tmp_path: Path) -> None:
    """Same for the incomplete-run rescue draft."""
    from kew import dispatch_core

    log_file = _run_log(tmp_path, "- Got the parser most of the way there")
    result = DispatchResult(
        exit_code=1, issue_num=123, log_file=log_file, run_id="run-7"
    )

    with (
        patch.object(dispatch_core, "_qualifies_for_rescue", return_value=True),
        patch.object(dispatch_core, "_rescue_incomplete_run") as rescue,
    ):
        assert dispatch_core._try_rescue_incomplete(
            _dispatch_context(tmp_path), result, tmp_path
        )

    assert rescue.call_args.kwargs["log_file"] == log_file


def test_the_sandbox_path_publishes_the_agents_summary_line(tmp_path: Path) -> None:
    """The contained path's publication is the one #2062 was written for."""
    from kew import dispatch_core, dispatch_sandbox

    log_file = _run_log(tmp_path, "- Rewrote the host commit subject builder")
    finalized: list[int] = []

    with (
        _ceremony("canonical") as push,
        patch.object(dispatch_core, "_post_agent_report"),
    ):
        result = dispatch_sandbox._publish_completed_sandbox(
            SimpleNamespace(error_category="", subtype="", published_pr_url=""),
            finalize=lambda exit_code, *_a, **_k: finalized.append(exit_code),
            checkout_dir=tmp_path / "checkout",
            branch="agent/issue-123",
            item_id="123",
            title="Issue title nobody reads",
            config=DispatchConfig(),
            store=None,
            run_id="run-7",
            publication_base_sha="a" * 40,
            image_id="img",
            log_file=log_file,
            issue_num=123,
        )

    assert result.published_pr_url == _PR_URL
    assert finalized == [0]
    assert push.subject == "feat(#123): Rewrote the host commit subject builder"


@pytest.mark.parametrize(
    "reference",
    [
        "Fixes #999",
        "closes GH-12",
        "resolves owner/repo#3",
        "Resolved: https://github.com/owner/repo/issues/77",
    ],
)
def test_a_closing_reference_in_the_summary_never_reaches_the_subject(
    reference: str,
) -> None:
    report = f"## Dispatch Report\n\n### What was done\n- {reference} tidy the parser"

    message = build_commit_message(
        item_id="7", prefix="fix: ", title="Parser", run_id="r1", report=report
    )

    subject = message.splitlines()[0]
    assert subject == "fix: tidy the parser"


def test_a_closing_reference_in_the_title_never_reaches_the_subject() -> None:
    message = build_commit_message(item_id="7", prefix="fix: ", title="fix #12 parser")

    assert message.splitlines()[0] == "fix: parser"


@pytest.mark.parametrize(
    "nested",
    ["Fixes Fixes #1 #999", "closes fixes resolves #1 #2 #3", "Fixes: Fixes #1 GH-9"],
)
def test_a_closing_reference_that_reforms_after_removal_never_reaches_the_subject(
    nested: str,
) -> None:
    report = f"## Dispatch Report\n\n### What was done\n- {nested} tidy"

    message = build_commit_message(
        item_id="7", prefix="fix: ", title="Parser", run_id="r1", report=report
    )

    assert message.splitlines()[0] == "fix: tidy"


def test_a_summary_that_is_only_a_closing_reference_falls_back_to_attribution() -> None:
    report = "## Dispatch Report\n\n### What was done\n- Closes #999"

    message = build_commit_message(item_id="7", prefix="fix: ", report=report)

    assert message.splitlines()[0] == "fix: complete work for #7 (committed by kew)"


@pytest.mark.parametrize(
    "reference",
    [
        "Fixes #999",
        "fix#12",
        "closes GH-12",
        "resolves owner/repo#3",
        "Resolved: https://github.com/owner/repo/issues/77",
        "fixes https://github.com/owner/repo/pull/77",
    ],
)
def test_the_subject_strips_every_closing_reference_a_pr_title_breaks(
    reference: str,
) -> None:
    assert break_closing_references(reference) != reference
    assert strip_closing_references(f"{reference} tidy") == "tidy"
