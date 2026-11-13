"""An agent PR says what the product now does, and shows its proposals whole.

The title is the run report's ``PR title:`` line, or the first sentence of its
summary, or ``Fix: <issue title>``; never a "What was done" bullet. The body
carries cause, change and proof, and the agent's **Behavior changes** lines
verbatim under **Proposed behavior changes**, which the merge's held-out gate
does not read: an agent's own line declares nothing, so the reviewer is asked
about the edit, and a line becomes a declaration only through the
reviewer-requested path (#2872). An edit to an existing frontend test with no
line naming it is flagged before the reviewer can say READY (#3187).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import GhProcessBoundary

from kew import dispatch_core, pr_text, subprocess_utils
from kew import test_integrity as integrity
from kew.behavior_declarations import (
    DeclarationRequest,
    flagged_tests,
    load_declaration_request,
    record_flagged_tests,
    requested_lines,
    undeclared,
    with_declared_lines,
)
from kew.config import DispatchConfig
from kew.loops.delegate import DelegateDriver, _PublicationBinding
from kew.merge_verify import (
    behavior_change_lines,
    declared_behavior_changes,
    declared_on_line,
)
from kew.pr_text import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    PROPOSED_HEADING,
    ZERO_WIDTH_SPACE,
    PrBodyTooLong,
    agent_pr_body,
    agent_pr_title,
    unescape_text,
)
from kew.publication_canonical import PublicationHeld
from kew.review_contract import unanswered_test_flags
from kew.review_integrity import (
    FLAG_UNDECLARED_TEST_EDITS,
    declared_tests,
    read_integrity_report,
)
from kew.run_store import LoopRecord, SqliteRunStore
from kew.transcript_metrics import DerivedRunEvidence, write_derived_evidence
from kew.worktree import write_checkout_generation

ISSUE_TITLE = "Widget crashes on empty input"
SUMMARY = "An empty input now renders an empty widget"
NODE = "tests/test_widget.py::test_empty_input[blank]"
_CLOSING = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s*(?:#|o/r#|https?://)"
)


def _report(
    *,
    what_was_done: str = "- `src/kew/widget.py` returns an empty widget",
    behavior: str = "None",
    header: str = "",
    summary: str = "",
) -> str:
    top = "## Dispatch Report\n\n**Issue:** #9 — Widget crashes on empty input\n"
    top += f"**Complexity:** simple\n{header}\n"
    if summary:
        top += f"{summary}\n\n"
    return (
        f"{top}### What was done\n{what_was_done}\n\n"
        "### Testing\n- tests/test_widget.py covers the empty input\n\n"
        f"### Behavior changes\n{behavior}\n"
    )


def _long_line() -> str:
    """A 600-character declaration carrying a comment opener, code and dashes."""
    head = (
        f"- {NODE} — the old copy asserted `render('')` raised; "
        "an empty input <!-- now --> renders -- by design @maintainers **bold** "
        "and ``` a fence "
    )
    line = head + "x" * (600 - len(head))
    assert len(line) == 600
    return line


def _proposed(body: str) -> list[str]:
    """The lines inside the body's Proposed behavior changes code fence."""
    lines = body.splitlines()
    start = lines.index(PROPOSED_HEADING)
    fence_at = next(n for n in range(start, len(lines)) if lines[n].startswith("```"))
    fence = lines[fence_at]
    end = lines.index(fence, fence_at + 1)
    return lines[fence_at + 1 : end]


# --- the body: proposals, never declarations --------------------------------


def test_the_agents_lines_are_shown_verbatim_and_declare_nothing() -> None:
    line = _long_line()
    report = _report(behavior=line)

    body = agent_pr_body("9", ISSUE_TITLE, report)

    assert _proposed(body) == [line]
    assert declared_behavior_changes(body) == frozenset()
    assert behavior_change_lines(body) == []
    # A person copying the proposal gets the same id the merge would read.
    assert declared_on_line(_proposed(body)[0]) == {NODE}


@pytest.mark.parametrize(
    "node",
    [
        "tests/test_a.py::test_x[<lambda>]",
        "tests/test_a.py::test_x[a@b&c*d]",
        "tests/test_a.py::test_x,fixes",
    ],
)
def test_a_proposed_node_id_reaches_the_body_exactly_as_pytest_prints_it(
    node: str,
) -> None:
    line = f"- {node} — the old copy asserted the crash"

    body = agent_pr_body("9", ISSUE_TITLE, _report(behavior=line))

    assert _proposed(body) == [line]
    assert declared_on_line(_proposed(body)[0]) == {node}


def test_no_proposed_line_closes_the_fence_early() -> None:
    lines = ["- tests/a.py::test_x — ```` four backticks", f"- {NODE} — after it"]
    body = agent_pr_body("9", ISSUE_TITLE, _report(behavior="\n".join(lines)))

    assert _proposed(body) == lines
    assert "\n`````\n" in body


@pytest.mark.parametrize(
    "hidden",
    [
        "\x00**Behavior changes**",
        "\x1b### Behavior changes",
        "#\x7f# Behaviour changes",
    ],
)
def test_a_heading_hidden_behind_a_control_character_declares_nothing(
    hidden: str,
) -> None:
    lines = [f"- {NODE} — before it", hidden, f"- {NODE} — the old copy was wrong"]

    body = agent_pr_body("9", ISSUE_TITLE, _report(behavior="\n".join(lines)))

    assert behavior_change_lines(body) == []
    assert declared_behavior_changes(body) == frozenset()


def test_a_none_section_proposes_nothing() -> None:
    body = agent_pr_body("9", ISSUE_TITLE, _report(behavior="- None"))

    assert PROPOSED_HEADING not in body
    assert body.startswith("Closes #9\n\n**Cause:** Widget crashes on empty input\n")


def test_the_prose_is_escaped_and_closes_nothing() -> None:
    report = _report(what_was_done="- <!-- hidden --> @someone **now** fixes #77")

    body = agent_pr_body("9", ISSUE_TITLE, report)

    (change,) = [ln for ln in body.splitlines() if ln.startswith("**Change:**")]
    assert "<!--" not in change and "@someone" not in change
    assert _CLOSING.search(change) is None
    assert unescape_text(change.removeprefix("**Change:** ")) == (
        "<!-- hidden --> @someone **now** fixes #77"
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def host_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``git`` through kew's allowlist."""
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


def _edited_repo(
    tmp_path: Path, rel: str, before: str, after: str
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    test = repo / rel
    test.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    test.write_text(before, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    test.write_text(after, encoding="utf-8")
    _git(repo, "commit", "-qam", "edit the test")
    return repo, base, _git(repo, "rev-parse", "HEAD")


_PY_BEFORE = "def test_empty_input():\n    assert render('') is None\n"
_PY_AFTER = "def test_empty_input():\n    assert render('') == Widget()\n"
_PY_NODE = "tests/test_widget.py::test_empty_input"


@pytest.mark.usefixtures("host_git")
def test_an_edit_only_the_agent_proposes_is_still_flagged_for_the_reviewer(
    tmp_path: Path,
) -> None:
    repo, base, head = _edited_repo(
        tmp_path, "tests/test_widget.py", _PY_BEFORE, _PY_AFTER
    )
    proposal = f"- {_PY_NODE} — an empty input now renders an empty widget"
    body = agent_pr_body("9", ISSUE_TITLE, _report(behavior=proposal))

    report = read_integrity_report(repo, base, head, pr_body=body)

    assert report is not None
    assert [e.text.split(" ", 1)[0] for e in report.undeclared_test_edits] == [_PY_NODE]
    ready = {"verdict": "READY", "test_changes": []}
    assert FLAG_UNDECLARED_TEST_EDITS in unanswered_test_flags(ready, report.flags)

    # The reviewer asks for it; the line it earns settles the flag.
    request = DeclarationRequest(node_ids=(_PY_NODE,), reasons=())
    declared = with_declared_lines(
        body, requested_lines(request, _report(behavior=proposal))
    )
    settled = read_integrity_report(repo, base, head, pr_body=declared)
    assert settled is not None
    assert FLAG_UNDECLARED_TEST_EDITS not in settled.flags
    assert declared_behavior_changes(declared) == {_PY_NODE}


# --- the body's size ---------------------------------------------------------


def test_sixty_long_declarations_keep_every_line_and_drop_prose_first() -> None:
    lines = [f"- tests/test_a.py::test_{n} — " + "why " * 230 for n in range(60)]
    report = _report(what_was_done="- " + "change " * 2000, behavior="\n".join(lines))

    body = agent_pr_body("9", ISSUE_TITLE, report)

    assert len(body) <= MAX_BODY_CHARS
    assert _proposed(body) == lines
    assert "**Change:**" not in body
    assert body.startswith("Closes #9\n")


def test_sixty_declarations_too_long_for_github_are_refused_whole() -> None:
    lines = [f"- tests/test_a.py::test_{n} — " + "why " * 260 for n in range(60)]

    with pytest.raises(PrBodyTooLong, match="60 proposed Behavior changes lines"):
        agent_pr_body("9", ISSUE_TITLE, _report(behavior="\n".join(lines)))


def test_a_body_that_cannot_fit_is_held_before_anything_is_pushed(
    published: _Published, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's retained report is bounded (20,000 characters), so its lines
    alone cannot pass the real cap: the cap is lowered under them here."""
    lines = [f"- tests/test_a.py::test_{n} — " + "why " * 20 for n in range(60)]
    published.report(_report(behavior="\n".join(lines)))
    monkeypatch.setattr(pr_text, "MAX_BODY_CHARS", 2_000)

    with pytest.raises(PublicationHeld, match="pr_body_too_long"):
        published.open_pr()

    assert published.forge.created_prs() == []
    assert published.remote_branches() == []


# --- the title ---------------------------------------------------------------


def test_the_title_is_the_reports_pr_title_line_as_plain_text() -> None:
    report = _report(
        header="PR title: **An empty input** now renders an `empty` [widget](x).\n"
    )

    assert agent_pr_title("9", ISSUE_TITLE, report) == SUMMARY


def test_an_unfilled_title_placeholder_falls_back_to_the_summary_sentence() -> None:
    report = _report(
        header="PR title: [required: one plain sentence]\n",
        summary="Empty inputs render. Two caveats follow.",
    )

    assert agent_pr_title("9", ISSUE_TITLE, report) == "Empty inputs render"


def test_a_code_path_bullet_never_titles_the_pr() -> None:
    report = _report(what_was_done=f"- **Item 1:** {SUMMARY} in `src/kew/w.py`")

    assert agent_pr_title("9", ISSUE_TITLE, report) == f"Fix: {ISSUE_TITLE}"


def test_with_no_report_the_title_is_fix_and_the_issue_title() -> None:
    assert agent_pr_title("9", ISSUE_TITLE, None) == f"Fix: {ISSUE_TITLE}"
    assert agent_pr_title("9", "", None) == "Fix #9"


def test_a_long_title_is_cut_on_a_word_boundary() -> None:
    words = "renders " * 60
    title = agent_pr_title("9", ISSUE_TITLE, _report(header=f"PR title: {words}\n"))

    assert len(title) <= MAX_TITLE_CHARS
    assert title.endswith("renders…")


@dataclass(frozen=True)
class _RealReport:
    number: int
    issue_title: str
    head: str
    fallback: str


#: The opening of each real Dispatch Report, verbatim through its first "What
#: was done" bullet (`gh pr view N --comments`); none has a ``PR title:`` line.
_REAL_REPORTS = [
    _RealReport(
        3220,
        "Epic journal: a malformed latest row stops the driver's journal, and "
        "log pruning turns quiet repeats into change marks",
        "## Dispatch Report\n\n**Issue:** #2949 — Epic journal: a malformed latest "
        "row stops the driver's journal, and log pruning turns quiet repeats into "
        "change marks\n**Complexity:** simple\n\n### What was done\n- **Item 1:** a "
        "new helper, `_predecessor_nodes` in `src/kew/graph/driver/journal.py`, "
        "checks the previous snapshot before anything reads it. If `nodes` is not "
        "a list, or any node is not an object (e.g. `nodes: [null]`), it logs a "
        "warning and treats the row as having no predecessor. Both places that "
        "read the previous snapshot now use it: `build_snapshot` and "
        "`_transitions`, which emits the per-node audit events.\n",
        "Fix: Epic journal: a malformed latest row stops the driver's journal, "
        "and log pruning turns quiet repeats into change marks",
    ),
    _RealReport(
        3221,
        "The flaky tests check order, events and pinned clocks instead of wall time",
        "## Dispatch Report\n\n**Issue:** #3184 — The flaky tests check order, "
        "events and pinned clocks instead of wall time\n**Complexity:** complex\n\n"
        "All six rows are implemented, with three caveats. The browser checks for "
        "rows 1 and 2 could not run here. The row 3 control that removes the mask "
        "cannot fail on this Linux kernel. And the new policy test is narrower "
        "than the issue's wording.\n\n### What was done\n- **Row 1, /work first "
        "draw (#2909, #2963):** `work-shell.mjs` no longer has `sleep(4000)` or "
        "the 300 ms budget.\n",
        "All six rows are implemented, with three caveats",
    ),
    _RealReport(
        3210,
        "Review holds say whether the reviewer ran, and each PR keeps one review copy",
        "## Dispatch Report\n\n**Issue:** #3186 — Review holds say whether the "
        "reviewer ran, and each PR keeps one review copy\n**Complexity:** medium\n\n"
        "All five acceptance criteria are covered by tests that pass. In this "
        "container the frontend unit tests and browser checks did not run (no "
        "`node_modules`, no Chromium), and neither did the full suite, semgrep or "
        "`-m policy`.\n\n### What was done\n- **The runtime timeout now holds as "
        '"never started".** The launcher now treats both `container_start_failed` '
        "and `runtime_timeout` as a container that never ran "
        "(`NOT_LAUNCHED_CATEGORIES`). It also reads the category off a raised "
        "`ContainerStartFailed` directly. `runtime_timeout` is now in "
        "`_PRE_LAUNCH_HOLDS`, so the hold is `review_launch_failed: the reviewer "
        "never started — the docker create timed out on every attempt "
        "(runtime_timeout); …`. No review round is recorded.\n",
        "All five acceptance criteria are covered by tests that pass",
    ),
]


@pytest.mark.parametrize("real", _REAL_REPORTS, ids=lambda r: f"pr{r.number}")
def test_a_real_report_without_a_title_line_falls_back_to_its_summary(
    real: _RealReport,
) -> None:
    assert agent_pr_title(str(real.number), real.issue_title, real.head) == (
        real.fallback
    )


@pytest.mark.parametrize("real", _REAL_REPORTS, ids=lambda r: f"pr{r.number}")
def test_a_real_report_with_a_title_line_is_titled_by_it(real: _RealReport) -> None:
    line = "PR title: kew titles agent PRs with what the product now does\n"
    report = real.head.replace("\n\n", f"\n{line}\n", 1)

    assert agent_pr_title(str(real.number), real.issue_title, report) == (
        "kew titles agent PRs with what the product now does"
    )


@pytest.mark.parametrize(
    "said",
    [
        "fixes #77",
        "Closes #77",
        "resolved #77",
        "FIX: #77",
        "close o/r#7",
        "fixed https://github.com/o/r/issues/7",
        "resolves#5",
        "Fixes https://github.com/o/r/issues/1,fixes #2",
        "Fixes https://github.com/o/r/issues/1;closes #2",
        "fixes https://x.example/closes#4 resolves #5",
    ],
)
def test_a_closing_keyword_in_the_title_closes_nothing(said: str) -> None:
    report = _report(header=f"PR title: An empty input now renders, {said}\n")

    title = agent_pr_title("9", ISSUE_TITLE, report)

    assert _CLOSING.search(title) is None
    assert title.replace(ZERO_WIDTH_SPACE, "") == (
        f"An empty input now renders, {said}"
    )


def test_a_keyword_with_no_reference_is_left_alone() -> None:
    assert agent_pr_title("9", "Fix the widget", None) == "Fix: Fix the widget"


# --- the publication, at the forge and process boundary ----------------------


class _Forge:
    """GitHub's REST create-PR and labels endpoints, recording every argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        stdout = "[]"
        if cmd[:5] == ["gh", "api", "--method", "POST", "repos/o/r/pulls"]:
            stdout = json.dumps({"number": 9, "html_url": "https://x.invalid/pull/9"})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def created_prs(self) -> list[dict[str, str]]:
        creates = [c for c in self.calls if c[-1].startswith("draft=")]
        return [
            dict(a.split("=", 1) for a in c if "=" in a and not a.startswith("-"))
            for c in creates
        ]


@dataclass
class _Published:
    """A real checkout, a real bare origin and a fake forge."""

    checkout: Path
    origin: Path
    base: str
    log_file: Path
    forge: _Forge
    store: SqliteRunStore
    generation: str

    def report(self, text: str) -> None:
        write_derived_evidence(self.log_file, DerivedRunEvidence(report=text))

    @staticmethod
    def config() -> DispatchConfig:
        config = DispatchConfig()
        config.project.repo = "o/r"
        return config

    def open_pr(self, **kwargs: Any) -> str | None:
        return dispatch_core._push_and_open_pr(
            self.checkout,
            "agent/issue-9",
            "9",
            ISSUE_TITLE,
            self.config(),
            store=self.store,
            run_id="run-9",
            base_sha=self.base,
            log_file=self.log_file,
            **kwargs,
        )

    def remote_branches(self) -> list[str]:
        refs = _git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads")
        return [r for r in refs.splitlines() if r != "refs/heads/main"]


@pytest.fixture
def published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_git: None
) -> Iterator[_Published]:
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(operator))
    _git(operator, "config", "user.email", "t@example.invalid")
    _git(operator, "config", "user.name", "t")
    (operator / "widget.py").write_text("def render(x):\n    raise ValueError\n")
    _git(operator, "add", ".")
    _git(operator, "commit", "-qm", "base")
    _git(operator, "push", "-q", "origin", "main")
    base = _git(operator, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    generation = write_checkout_generation(checkout)
    (checkout / "widget.py").write_text("def render(x):\n    return None\n")
    # The operator's project: canonical publication pushes to its origin.
    monkeypatch.setenv("KEW_PROJECT", str(operator))
    monkeypatch.delenv("KEW_DETACHED_PUBLICATION_TOKEN", raising=False)
    forge = _Forge()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    log_file = tmp_path / "run.jsonl"
    log_file.write_text("", encoding="utf-8")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        yield _Published(checkout, origin, base, log_file, forge, store, generation)


def test_the_published_pr_is_titled_by_the_report_and_proposes_its_lines(
    published: _Published,
) -> None:
    line = _long_line()
    published.report(_report(header=f"PR title: {SUMMARY}\n", behavior=line))

    assert published.open_pr() == "https://x.invalid/pull/9"

    (created,) = published.forge.created_prs()
    assert created["title"] == SUMMARY
    body = created["body"]
    assert body.startswith("Closes #9\n")
    assert f"**Cause:** {ISSUE_TITLE}" in body
    assert "**Proof:** tests/test_widget.py covers the empty input" in body
    assert _proposed(body) == [line]
    assert declared_behavior_changes(body) == frozenset()
    assert published.remote_branches() == ["refs/heads/agent/issue-9"]


def test_a_control_character_in_a_proposed_line_never_reaches_the_pr(
    published: _Published,
) -> None:
    line = f"- {NODE} — the old\x00 copy\x1b[31m asserted\tthe crash\x7f"
    published.report(_report(behavior=line))

    assert published.open_pr() == "https://x.invalid/pull/9"

    (created,) = published.forge.created_prs()
    body = created["body"]
    assert re.search(r"[\x00-\x08\x0b-\x1f\x7f]", body) is None
    assert _proposed(body) == [f"- {NODE} — the old copy[31m asserted\tthe crash"]


@pytest.mark.parametrize(
    "title",
    ["a goal\x00 with a NUL", "a goal\nover two lines", "a lone \ud800 surrogate"],
)
def test_a_title_github_cannot_take_is_held_before_anything_is_pushed(
    published: _Published, title: str
) -> None:
    with pytest.raises(PublicationHeld, match="pr_text_unsendable") as held:
        published.open_pr(pr_title=title)

    assert "Open the PR by hand" in str(held.value)
    assert published.forge.created_prs() == []
    assert published.remote_branches() == []


def test_with_no_report_the_pr_is_fix_and_the_issue_title(
    published: _Published,
) -> None:
    published.log_file.unlink()

    published.open_pr()

    (created,) = published.forge.created_prs()
    assert created["title"] == f"Fix: {ISSUE_TITLE}"
    assert created["body"] == "Closes #9"


def test_a_loop_pr_keeps_its_goal_line_as_its_title(published: _Published) -> None:
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="Make the widget render empty input\nand say why",
        runner_name="claude-code",
        created_at=datetime(2026, 10, 26, tzinfo=UTC),
        issue_num=9,
        repo="o/r",
        budget_usd=5.0,
        wall_clock_secs=600,
        max_steps=200,
    )
    binding = _PublicationBinding(
        run_id="run-9",
        base_sha=published.base,
        lineage_run_id="run-9",
        checkout_generation=published.generation,
    )
    driver = DelegateDriver(published.store, config=published.config())

    driver._publish_iteration(loop, published.checkout, binding)

    (created,) = published.forge.created_prs()
    assert created["title"] == "Make the widget render empty input"


# --- requested declarations: the id verbatim, only the reason escaped -------


@pytest.mark.parametrize(
    ("node", "reason", "carried"),
    [
        pytest.param(
            "tests/test_a.py::test_x[lam]",
            "the <lambda> case @someone **now** renders",
            "the &#60;lambda&#62; case &#64;someone &#42;&#42;now&#42;&#42; renders",
            id="markup-in-the-reason",
        ),
        pytest.param(
            "tests/test_a.py::test_x[fixes-1]",
            "#2949 broke this",
            "#2949 broke this",
            id="a-reason-that-starts-with-a-reference",
        ),
        pytest.param(
            NODE,
            "the reason mentions frontend/src/lib/a.test.ts in passing",
            "the reason mentions frontend/src/lib/a.test.ts in passing",
            id="a-path-in-the-reason",
        ),
    ],
)
def test_a_requested_line_keeps_its_id_verbatim_and_escapes_only_its_reason(
    node: str, reason: str, carried: str
) -> None:
    request = DeclarationRequest(node_ids=(node,), reasons=())

    lines = requested_lines(request, _report(behavior=f"- `{node}` — {reason}"))

    assert lines == [f"- {node} — {carried}"]
    assert unescape_text(lines[0]) == f"- {node} — {reason}"
    body = with_declared_lines("Closes #9\n", lines)
    assert declared_behavior_changes(body) == {node}
    assert declared_tests(body) == {node}


def test_a_reason_naming_another_node_id_is_refused_whole() -> None:
    request = DeclarationRequest(node_ids=(NODE,), reasons=())
    report = _report(behavior=f"- {NODE} — the same change as tests/b.py::test_y")

    assert requested_lines(request, report) == []


# --- frontend tests ----------------------------------------------------------

_TS_BEFORE = "it('renders', () => expect(1).toBe(1))\n"


@pytest.mark.usefixtures("host_git")
@pytest.mark.parametrize(
    "after",
    [
        pytest.param("it('renders', () => expect(1).toBe(2))\n", id="a-changed-line"),
        pytest.param("", id="a-removed-line"),
        pytest.param("it('renders', () => expect(1).toBe(1))", id="its-newline-cut"),
    ],
)
def test_an_undeclared_frontend_test_edit_is_flagged_before_ready(
    tmp_path: Path, after: str
) -> None:
    repo, base, head = _edited_repo(
        tmp_path, "frontend/src/x.test.ts", _TS_BEFORE, after
    )

    report = read_integrity_report(repo, base, head, pr_body="Closes #1\n")

    assert report is not None
    assert [e.path for e in report.undeclared_test_edits] == ["frontend/src/x.test.ts"]
    ready = {"verdict": "READY", "test_changes": []}
    assert FLAG_UNDECLARED_TEST_EDITS in unanswered_test_flags(ready, report.flags)


_TS_CASES = (
    "describe('widget', () => {\n"
    "  it('renders', () => {\n"
    "    expect(render('')).toBe(null)\n"
    "  })\n"
    "})\n"
)


@pytest.mark.usefixtures("host_git")
@pytest.mark.parametrize(
    "after",
    [
        pytest.param(
            _TS_CASES.replace(
                "  it('renders', () => {\n", "  it('renders', () => {\n    return;\n"
            ),
            id="an-inserted-return",
        ),
        pytest.param(
            _TS_CASES.replace("  it(", "  /*\n  it(").replace("  })\n", "  })\n  */\n"),
            id="a-comment-around-an-it",
        ),
        pytest.param(
            _TS_CASES.replace("    expect(", "    if (false)\n    expect("),
            id="an-if-false",
        ),
        pytest.param(
            _TS_CASES + "it.only('nothing else runs', () => {})\n",
            id="an-appended-only",
        ),
        pytest.param(
            "vi.mock('./widget', () => ({ render: () => null }))\n" + _TS_CASES,
            id="a-prepended-mock",
        ),
    ],
)
def test_a_change_that_only_adds_lines_to_a_frontend_test_is_still_an_edit(
    tmp_path: Path, after: str
) -> None:
    """Each of these keeps every old line in order and still changes what the
    file's existing case checks: any change to an existing frontend test file
    needs a line naming it."""
    repo, base, head = _edited_repo(
        tmp_path, "frontend/src/x.test.ts", _TS_CASES, after
    )

    report = read_integrity_report(repo, base, head, pr_body="Closes #1\n")

    assert report is not None
    assert [e.path for e in report.undeclared_test_edits] == ["frontend/src/x.test.ts"]


def _moved_repo(tmp_path: Path, *, renamed_to: str | None) -> tuple[Path, str, str]:
    """``frontend/src/a.test.ts`` on main; the head renames it to *renamed_to*
    and edits it, or deletes it when *renamed_to* is None."""
    repo = tmp_path / "repo"
    (repo / "frontend/src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "frontend/src/a.test.ts").write_text(_TS_CASES, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    if renamed_to is None:
        _git(repo, "rm", "-q", "frontend/src/a.test.ts")
    else:
        _git(repo, "mv", "frontend/src/a.test.ts", renamed_to)
        moved = repo / renamed_to
        moved.write_text(_TS_CASES.replace("toBe(null)", "toBe(undefined)"))
        _git(repo, "add", renamed_to)
    _git(repo, "commit", "-qm", "move the test")
    return repo, base, _git(repo, "rev-parse", "HEAD")


@pytest.mark.usefixtures("host_git")
@pytest.mark.parametrize(
    "renamed_to",
    [pytest.param("frontend/src/b.test.ts", id="renamed-and-edited"), None],
    ids=lambda v: v or "deleted",
)
def test_a_frontend_test_renamed_away_or_deleted_is_flagged_by_its_base_path(
    tmp_path: Path, renamed_to: str | None
) -> None:
    repo, base, head = _moved_repo(tmp_path, renamed_to=renamed_to)

    report = read_integrity_report(repo, base, head, pr_body="Closes #1\n")

    assert report is not None
    assert [e.path for e in report.undeclared_test_edits] == ["frontend/src/a.test.ts"]


@pytest.mark.usefixtures("host_git")
@pytest.mark.parametrize("renamed_to", ["frontend/src/b.test.ts", None])
def test_a_line_naming_the_base_path_declares_a_moved_frontend_test(
    tmp_path: Path, renamed_to: str | None
) -> None:
    repo, base, head = _moved_repo(tmp_path, renamed_to=renamed_to)
    body = (
        "Closes #1\n\n**Behavior changes**\n\n"
        "- frontend/src/a.test.ts — the widget's cases moved and now expect undefined\n"
    )

    report = read_integrity_report(repo, base, head, pr_body=body)

    assert report is not None
    assert FLAG_UNDECLARED_TEST_EDITS not in report.flags


@pytest.mark.usefixtures("host_git")
def test_a_declared_frontend_test_edit_is_not_flagged(tmp_path: Path) -> None:
    repo, base, head = _edited_repo(
        tmp_path, "frontend/src/x.test.ts", _TS_BEFORE, _TS_BEFORE.replace("1)", "2)")
    )
    body = (
        "Closes #1\n\n**Behavior changes**\n\n"
        "- frontend/src/x.test.ts — the widget now renders two\n"
    )

    report = read_integrity_report(repo, base, head, pr_body=body)

    assert report is not None
    assert FLAG_UNDECLARED_TEST_EDITS not in report.flags


_ROUTE_TEST = "frontend/src/routes/(factory)/runs/[id]/+page.test.ts"


@pytest.mark.usefixtures("host_git")
@pytest.mark.parametrize(
    "line",
    [
        f"- {_ROUTE_TEST} — the run page now renders two",
        f"- `{_ROUTE_TEST}` — the run page now renders two.",
        f"- the run page now renders two ({_ROUTE_TEST}).",
    ],
)
def test_a_declared_route_group_test_edit_is_not_flagged(
    tmp_path: Path, line: str
) -> None:
    """A SvelteKit route path keeps its ``(group)``, ``[param]`` and ``+page``
    segments: the line declares the whole path, not its tail."""
    repo, base, head = _edited_repo(
        tmp_path, _ROUTE_TEST, _TS_BEFORE, _TS_BEFORE.replace("1)", "2)")
    )
    body = f"Closes #1\n\n**Behavior changes**\n\n{line}\n"

    report = read_integrity_report(repo, base, head, pr_body=body)

    assert report is not None
    assert FLAG_UNDECLARED_TEST_EDITS not in report.flags


@pytest.mark.usefixtures("host_git")
def test_a_line_naming_a_longer_path_does_not_declare_a_frontend_test_edit(
    tmp_path: Path,
) -> None:
    repo, base, head = _edited_repo(
        tmp_path, "frontend/src/x.test.ts", _TS_BEFORE, _TS_BEFORE.replace("1)", "2)")
    )
    body = (
        "Closes #1\n\n**Behavior changes**\n\n"
        "- frontend/src/x.test.tsx — the widget now renders two\n"
    )

    report = read_integrity_report(repo, base, head, pr_body=body)

    assert report is not None
    assert [e.path for e in report.undeclared_test_edits] == ["frontend/src/x.test.ts"]


def _declaration_round(tmp_path: Path, report: integrity.TestIntegrityReport) -> Path:
    """A stored round that rejects only undeclared edits, every flagged test
    asked for by a declaration finding, and the host's record of them."""
    ids = flagged_tests(report)[FLAG_UNDECLARED_TEST_EDITS]
    sidecar = tmp_path / "logs" / "round-1.sidecar.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    finding = {
        "severity": "medium",
        "location": f"{ids[0]}:1",
        "summary": "the edit is right and no Behavior changes line names it",
        "evidence": {"command": "npx vitest run", "output_digest": "1 failed"},
        "kind": "declaration",
        "node_ids": ids,
    }
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [finding],
                "test_changes": [
                    {
                        "flag": FLAG_UNDECLARED_TEST_EDITS,
                        "answer": "REJECTED",
                        "reason": "justified, but undeclared",
                    }
                ],
            }
        )
    )
    assert record_flagged_tests(sidecar, report) is None
    return sidecar


@pytest.mark.usefixtures("host_git")
def test_a_declaration_round_settles_a_route_group_frontend_test(
    tmp_path: Path,
) -> None:
    route = "frontend/src/routes/(factory)/budget/reads.test.ts"
    repo, base, head = _edited_repo(
        tmp_path, route, _TS_BEFORE, _TS_BEFORE.replace("1)", "2)")
    )
    flagged = read_integrity_report(repo, base, head, pr_body="Closes #1\n")
    assert flagged is not None

    request = load_declaration_request(str(_declaration_round(tmp_path, flagged)))

    assert request is not None
    assert request.node_ids == (route,)
    report = _report(behavior=f"- `{route}` — the budget page now reads two.")
    lines = requested_lines(request, report)
    body = with_declared_lines("Closes #1\n", lines)
    assert lines == [f"- {route} — the budget page now reads two."]
    assert undeclared(request, "Closes #1\n") == (route,)
    assert undeclared(request, body) == ()
    settled = read_integrity_report(repo, base, head, pr_body=body)
    assert settled is not None
    assert FLAG_UNDECLARED_TEST_EDITS not in settled.flags


@pytest.mark.parametrize(
    "path",
    [
        "frontend/src/a$(id).test.ts",
        "frontend/src/a`id`.test.ts",
        "frontend/src/a;b.test.ts",
        "frontend/src/a|b.test.ts",
        "frontend/src/[x](https:/e.invalid)/a.test.ts",
        "frontend/src/@a.test.ts",
        "frontend/src/<!--a.test.ts",
    ],
)
def test_a_flagged_path_with_a_metacharacter_is_never_requested_by_name(
    tmp_path: Path, path: str
) -> None:
    evidence = [integrity.Evidence(path, None, f"{path} is edited, no line names it")]
    report = integrity.with_undeclared_test_edits(
        integrity.TestIntegrityReport(), evidence
    )

    assert load_declaration_request(str(_declaration_round(tmp_path, report))) is None


def test_a_report_line_naming_another_frontend_test_declares_nothing() -> None:
    path = "frontend/src/x.test.ts"
    request = DeclarationRequest(node_ids=(path,), reasons=())
    longer = _report(behavior="- frontend/src/x.test.tsx — the widget now renders two")
    both = _report(
        behavior=f"- {path} and frontend/src/y.test.ts — the widget renders two"
    )

    assert requested_lines(request, longer) == []
    assert requested_lines(request, both) == []
    assert undeclared(request, longer) == (path,)
