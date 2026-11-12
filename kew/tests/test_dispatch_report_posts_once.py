"""A dispatch leaves one Dispatch Report on its PR (#1761).

Every test runs the real report code against a fake ``gh`` registered as the
allowlisted executable. The fake logs every invocation and serves ``gh pr view``
comment bodies from the comments it accepted, so "how many reports are on the
PR" is read off what the forge would hold.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.agent_report import quoted
from support.fake_gcloud import launchable

import kew.dispatch_core as dispatch_core
from kew import dispatch_sandbox, subprocess_utils, worktree
from kew.config import DispatchConfig
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

_PR_URL = "https://github.com/octo/widgets/pull/9"
_REPORT = (
    "## Dispatch Report\n\n**Issue:** #7 — fix it\n**Complexity:** medium\n\n"
    "### What was done\n- fixed it"
)
_MARKER = "<!-- kew-dispatch-report run=run-abc -->"
_FAKE_GH = """#!{python}
import json, sys
from pathlib import Path

state = Path(__file__).parent
args = sys.argv[1:]
with (state / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
pr = args[2].rstrip("/").rsplit("/", 1)[-1] if len(args) > 2 else ""
comments = state / f"comments-{{pr}}.jsonl"
if args[:2] == ["pr", "comment"]:
    fail = state / "fail_next_comment"
    if fail.exists():
        fail.unlink()
        sys.stderr.write("HTTP 502")
        sys.exit(1)
    with comments.open("a") as out:
        out.write(json.dumps(args[args.index("--body") + 1]) + "\\n")
elif args[:2] == ["pr", "view"] and comments.exists():
    # kew asks for ``.comments[].body | @json``: one JSON string per comment.
    print(comments.read_text(), end="")
elif args[:3] == ["api", "--method", "POST"] and args[3].endswith("/pulls"):
    (state / "pr_number").write_text("9")
    print(json.dumps({{"html_url": "{pr_url}", "number": 9}}))
elif args[:1] == ["api"] and args[3].endswith("/pulls"):
    number = state / "pr_number"
    print(number.read_text() if number.exists() else "")
"""


class _FakeGh:
    def __init__(self, state: Path) -> None:
        self.state = state

    def comments(self) -> list[str]:
        return [
            json.loads(line)
            for path in sorted(self.state.glob("comments-*.jsonl"))
            for line in path.read_text().splitlines()
        ]

    def reports(self) -> list[str]:
        return [body for body in self.comments() if "## Dispatch Report" in body]

    def calls(self) -> list[list[str]]:
        return [
            json.loads(line)
            for line in (self.state / "calls.jsonl").read_text().splitlines()
        ]

    def fail_next_comment(self) -> None:
        (self.state / "fail_next_comment").touch()

    def has_comment(self, pr: int, body: str) -> None:
        """A comment already on PR *pr*, posted by someone other than this run."""
        with (self.state / f"comments-{pr}.jsonl").open("a") as out:
            out.write(json.dumps(body) + "\n")

    def has_pr(self, number: int) -> None:
        """The branch lookup finds PR *number*."""
        (self.state / "pr_number").write_text(str(number))


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeGh]:
    # A noexec pytest tmpfs cannot launch the shim; HOME can (see fake_gcloud).
    fallback = None if launchable(tmp_path) else tempfile.mkdtemp(dir=Path.home())
    state = Path(fallback or tmp_path) / "gh"
    state.mkdir()
    script = state / "gh"
    script.write_text(_FAKE_GH.format(python=sys.executable, pr_url=_PR_URL))
    script.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", str(script))
    try:
        yield _FakeGh(state)
    finally:
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


@pytest.fixture
def contained_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """A local bare origin, and an agent checkout holding one edit on its base.

    Publication runs real git against the bare origin; only ``gh`` is faked.
    """
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "widget.txt").write_text("before\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "base")
    _git(checkout, "push", "-q", "origin", "main")
    base = _git(checkout, "rev-parse", "HEAD")
    worktree.write_checkout_generation(checkout)
    # The agent commits in the container; the host publishes the branch.
    _git(checkout, "checkout", "-qb", "agent/issue-7")
    (checkout / "widget.txt").write_text("after\n")
    _git(checkout, "commit", "-qam", "fix it")
    return origin, checkout, base


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _publish_contained(
    result: DispatchResult,
    config: DispatchConfig,
    contained_checkout: tuple[Path, Path, str],
) -> None:
    """The sandbox's publication step for a finished contained run.

    Checkout publication pushes the agent's branch to the local origin; the
    canonical path would extract the tree through a container runtime first.
    """
    origin, checkout, base = contained_checkout
    config.project.repo = str(origin)
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    dispatch_sandbox._publish_completed_sandbox(
        result,
        finalize=lambda *_a, **_k: None,
        checkout_dir=checkout,
        branch="agent/issue-7",
        item_id="7",
        title="fix it",
        config=config,
        store=None,
        run_id=result.run_id,
        publication_base_sha=base,
        image_id="img",
        log_file=result.log_file,
        issue_num=7,
    )
    assert result.published_pr_url == _PR_URL


def _config() -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "octo/widgets"
    return config


def _result(tmp_path: Path, *, report: str | None = _REPORT) -> DispatchResult:
    log = tmp_path / "dispatch.log"
    derivation = StreamDerivation()
    final = report if report is not None else "all done"
    derivation.observe(json.dumps({"type": "result", "result": final}))
    write_derived_evidence(log, derivation.evidence())
    return DispatchResult(exit_code=0, issue_num=7, log_file=log, run_id="run-abc")


def _ctx(
    config: DispatchConfig, *, publication_style: str = "agent"
) -> SimpleNamespace:
    return SimpleNamespace(
        config=config,
        issue_num=7,
        item_id="7",
        complexity="medium",
        store=None,
        branch="agent/issue-7",
        worktree_dir=None,
        publication_style=publication_style,
    )


def test_contained_success_posts_the_agent_report_once(
    tmp_path: Path, gh: _FakeGh
) -> None:
    config = _config()
    result = _result(tmp_path)

    # The sandbox publishes the agent's report once its PR exists, then the
    # success path runs; between them the PR carries one report.
    assert dispatch_core._post_agent_report(
        _PR_URL, result.log_file, config, result=result
    )
    dispatch_core._mark_success_pr(_ctx(config), result, 9, False)

    [report] = gh.reports()
    assert report.startswith("**Agent-authored report** from kew run `run-abc`")
    assert f"\n\n{quoted(_REPORT)}\n\n" in report
    assert result.dispatch_report_posted is True


def test_a_report_post_that_raises_warns_and_leaves_the_fallback_to_run(
    tmp_path: Path, gh: _FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _result(tmp_path)
    missing = gh.state / "missing-gh"

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(subprocess_utils.TOOL_PATHS, "gh", str(missing))
        posted = dispatch_core._post_agent_report(
            _PR_URL, result.log_file, _config(), result=result
        )

    assert posted is False
    assert result.dispatch_report_posted is False
    captured = capsys.readouterr()
    assert "Could not post agent Dispatch Report" in captured.out + captured.err


def test_a_second_step_without_the_flag_finds_the_run_marker(
    tmp_path: Path, gh: _FakeGh
) -> None:
    config = _config()
    first = _result(tmp_path)
    again = _result(tmp_path)

    assert dispatch_core._post_agent_report(
        _PR_URL, first.log_file, config, result=first
    )
    assert dispatch_core._post_agent_report(
        _PR_URL, again.log_file, config, result=again
    )

    assert len(gh.reports()) == 1
    assert again.dispatch_report_posted is True


def test_fallback_is_skipped_after_the_agent_report_posted(
    tmp_path: Path, gh: _FakeGh
) -> None:
    config = _config()
    result = _result(tmp_path)

    dispatch_core._mark_success_pr(_ctx(config), result, 9, False)

    [report] = gh.reports()
    assert report.startswith("**Agent-authored report** from kew run `run-abc`")
    assert f"\n\n{quoted(_REPORT)}\n\n" in report
    comment_at = next(
        i for i, call in enumerate(gh.calls()) if call[:2] == ["pr", "comment"]
    )
    # The fallback trusts the recorded post; it never re-reads the PR.
    assert not any(call[:2] == ["pr", "view"] for call in gh.calls()[comment_at:])


def test_fallback_trusts_the_recorded_post_without_reading_github(
    tmp_path: Path, gh: _FakeGh
) -> None:
    result = _result(tmp_path)
    result.dispatch_report_posted = True

    dispatch_core._post_dispatch_report_fallback(9, "7", "medium", result, _config())

    assert gh.comments() == []


def test_fallback_posts_once_when_the_agent_report_post_fails(
    tmp_path: Path, gh: _FakeGh
) -> None:
    config = _config()
    result = _result(tmp_path)
    gh.fail_next_comment()

    dispatch_core._mark_success_pr(_ctx(config), result, 9, False)
    dispatch_core._post_dispatch_report_fallback(9, "7", "medium", result, config)

    [report] = gh.reports()
    assert "kew generated this fallback" in report
    assert result.dispatch_report_posted is True


def test_report_carries_the_run_id_marker(tmp_path: Path, gh: _FakeGh) -> None:
    config = _config()
    dispatch_core._mark_success_pr(_ctx(config), _result(tmp_path), 9, False)
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    dispatch_core._mark_success_pr(
        _ctx(config), _result(fallback_dir, report=None), 10, False
    )

    [agent_report] = [
        body for body in gh.reports() if f"\n\n{quoted(_REPORT)}\n\n" in body
    ]
    [fallback_report] = [
        body for body in gh.reports() if "kew generated this fallback" in body
    ]
    assert _MARKER in agent_report
    assert _MARKER in fallback_report


def test_a_contained_run_posts_one_report_across_publication_and_success(
    tmp_path: Path, gh: _FakeGh, contained_checkout: tuple[Path, Path, str]
) -> None:
    config = _config()
    result = _result(tmp_path)

    # The sandbox publishes the branch, PR and report; the success path then
    # finds the PR by branch and finalizes it on the same result.
    _publish_contained(result, config, contained_checkout)
    dispatch_core._handle_success_pr(_ctx(config), result, tmp_path, True)

    [report] = gh.reports()
    assert report.startswith("**Agent-authored report** from kew run `run-abc`")
    assert f"\n\n{quoted(_REPORT)}\n\n" in report
    assert _MARKER in report


def test_an_agent_published_run_keeps_only_the_report_the_agent_posted(
    tmp_path: Path, gh: _FakeGh
) -> None:
    config = _config()
    config.publication.publish = "checkout"
    result = _result(tmp_path)
    gh.has_pr(9)
    # The agent created the PR and ran ``gh pr comment`` itself, as its
    # prompt tells it to; its comment carries no run marker.
    gh.has_comment(9, _REPORT)

    dispatch_core._handle_success_pr(_ctx(config), result, tmp_path, False)

    assert gh.reports() == [_REPORT]


def test_an_older_runs_report_does_not_stand_in_for_this_runs_fallback(
    tmp_path: Path, gh: _FakeGh
) -> None:
    older = _REPORT + "\n\n<!-- kew-dispatch-report run=run-older -->\n"
    gh.has_comment(9, older)
    result = _result(tmp_path, report=None)

    dispatch_core._mark_success_pr(_ctx(_config()), result, 9, False)

    [kept, fallback] = gh.reports()
    assert kept == older
    assert "kew generated this fallback" in fallback
    assert _MARKER in fallback
    assert result.dispatch_report_posted is True


def test_a_contained_run_retries_its_own_report_when_the_first_post_failed(
    tmp_path: Path, gh: _FakeGh, contained_checkout: tuple[Path, Path, str]
) -> None:
    config = _config()
    result = _result(tmp_path)
    gh.fail_next_comment()

    _publish_contained(result, config, contained_checkout)
    assert gh.reports() == []
    dispatch_core._handle_success_pr(_ctx(config), result, tmp_path, True)

    # kew opened this PR, so the agent could not comment; kew posts its report.
    [report] = gh.reports()
    assert report.startswith("**Agent-authored report** from kew run `run-abc`")
    assert f"\n\n{quoted(_REPORT)}\n\n" in report
