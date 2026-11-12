"""A no-change is narrowed to the files its item names only when it names them all.

The judged paths decide which moves of main leave a no-change confirmable, so a
judgment narrowed to less than the item is about is a false done waiting to
happen: #3199 named a docs page and a dozen short module paths, and only the
docs page used to count. The narrowing is now all or nothing — every
path-shaped name must resolve to exactly one tracked file, or nothing is
recorded and the judgment stays bound to its exact base.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kew.dispatch_core as dispatch_core
from kew.audit import Actor
from kew.config import DispatchConfig
from kew.evidence import EvidenceKind, Producer, ProducerKind, record_evidence
from kew.no_change import PENDING_CONFIRMATION, judged_paths_from
from kew.outcomes import tracked_files
from kew.run_store import RunRecord, SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS
from kew.tracking import DispatchResult

#: The tracked files the reliability issues below name, where kew keeps them.
KEW_FILES = (
    "docs/cli/no-change.md",
    "docs/cli/publication.md",
    "kew/src/kew/waits.py",
    "kew/src/kew/outcomes.py",
    "kew/src/kew/attention_journal.py",
    "kew/src/kew/exit_signal.py",
    "kew/src/kew/dispatch_core.py",
    "kew/src/kew/orchestrator.py",
    "kew/src/kew/dispatch_sandbox.py",
    "kew/src/kew/publication_credentials.py",
    "kew/src/kew/publication_attest.py",
    "kew/src/kew/review.py",
    "kew/src/kew/runners/codex.py",
    "kew/src/kew/graph/driver/attention.py",
    "kew/src/kew/graph/driver/tick.py",
    "kew/src/kew/graph/driver/review_stage.py",
    "kew/src/kew/graph/driver/overlay.py",
    "kew/src/kew/graph/driver/facts.py",
    "kew/src/kew/service/verbs.py",
    "kew/tests/test_waits.py",
)

#: The shapes of #3199, #3193 and #3191: every one cites its spec under the
#: gitignored ``.kew/docs/plans``, which no ``git ls-files`` lists.
ISSUE_3199 = (
    "Stuck nodes name their cause and the command that moves them\n"
    "Spec: .kew/docs/plans/2026-10-25-reliability.md, node R9\n"
    "**Files.** `waits.py:230-235,369-375,520`; `graph/driver/attention.py:154-165`;"
    " `attention_journal.py:603-613`; `graph/driver/tick.py:7129-7133`;"
    " `outcomes.py:1311-1320`; `service/verbs.py:1693-1695`; the no-change CLI"
    " verbs; `docs/cli/no-change.md`.\n"
    "- [ ] A no-change judged on main~4 with untouched paths confirms."
)
ISSUE_3193 = (
    "Spec: .kew/docs/plans/2026-10-25-reliability.md, node R7\n"
    "**Files.** `graph/driver/review_stage.py:2204-2269,2472-2484`;"
    " `graph/driver/overlay.py:49-54`; `publication_credentials.py:44,1251-1318`;"
    " `publication_attest.py:420-427`; `review.py:653,868-906`;"
    " `service/verbs.py:201-275`; `graph/driver/facts.py:426-510`;"
    " `docs/cli/publication.md`."
)
ISSUE_3191 = (
    "Spec: .kew/docs/plans/2026-10-25-reliability.md, node R4\n"
    "**Files.** `exit_signal.py`; `dispatch_core.py:1784-1830` (`_classify_attempt`);"
    " `orchestrator.py:2088-2100`; `runners/codex.py:596,606`;"
    " `dispatch_sandbox.py:3484-3537,3970-3990`; the graph driver's retry decision"
    " for `resource`."
)


@pytest.mark.parametrize(
    "text",
    [ISSUE_3199, ISSUE_3193, ISSUE_3191],
    ids=["3199", "3193", "3191"],
)
def test_a_real_issue_citing_an_untracked_spec_records_no_judged_paths(
    text: str,
) -> None:
    assert judged_paths_from(text, KEW_FILES) == ()


def test_short_module_names_resolve_to_the_one_tracked_file_they_end() -> None:
    """Without the untracked spec line, #3199 names the code, not only a page."""
    text = ISSUE_3199.replace(
        "Spec: .kew/docs/plans/2026-10-25-reliability.md, node R9\n", ""
    )

    assert judged_paths_from(text, KEW_FILES) == (
        "kew/src/kew/waits.py",
        "kew/src/kew/graph/driver/attention.py",
        "kew/src/kew/attention_journal.py",
        "kew/src/kew/graph/driver/tick.py",
        "kew/src/kew/outcomes.py",
        "kew/src/kew/service/verbs.py",
        "docs/cli/no-change.md",
    )


def test_a_full_path_and_a_dot_slash_path_resolve_to_themselves() -> None:
    tracked = ("src/app.py", "README.md")

    assert judged_paths_from(
        "Fix `src/app.py:12-30` and ./README.md, e.g. the intro.", tracked
    ) == ("src/app.py", "README.md")


@pytest.mark.parametrize(
    "named",
    [
        "../outside.txt",  # resolves to no tracked file
        "tick.py",  # two tracked files end in it
        "src/",  # a directory
        "src/kew",  # a directory, spelled without the slash
        "src/*.py",  # a glob
        "kew.toml",  # an untracked or ignored file
        "config.branches.base",  # a dotted code name, not a file
    ],
)
def test_one_name_that_is_not_exactly_one_tracked_file_records_none(
    named: str,
) -> None:
    tracked = ("src/app.py", "src/kew/a/tick.py", "src/kew/b/tick.py")

    assert judged_paths_from(f"Fix src/app.py; see also {named}", tracked) == ()


@pytest.mark.parametrize(
    ("text", "tracked", "named"),
    [
        (
            "The `Makefile` test target is broken; docs/install.md describes it.",
            ("Makefile", "docs/install.md", "src/app.py"),
            "Makefile",
        ),
        (
            "Dockerfile pins an old base image; docs/install.md describes it.",
            ("docker/Dockerfile", "docs/install.md", "src/app.py"),
            "docker/Dockerfile",
        ),
    ],
    ids=["Makefile", "Dockerfile"],
)
def test_a_tracked_file_named_without_an_extension_is_a_judged_path(
    text: str, tracked: tuple[str, ...], named: str
) -> None:
    """Narrowing to the docs page alone would let main rewrite the build."""
    assert judged_paths_from(text, tracked) == (named, "docs/install.md")


def test_an_extensionless_name_two_tracked_files_share_records_none() -> None:
    text = "The `Makefile` test target is broken; docs/install.md describes it."
    tracked = ("Makefile", "kew/Makefile", "docs/install.md")

    assert judged_paths_from(text, tracked) == ()


def test_a_plus_or_at_sign_is_part_of_the_file_name() -> None:
    tracked = ("frontend/src/lib/page.svelte", "frontend/src/routes/+page.svelte")

    assert judged_paths_from("The +page.svelte route crashes", tracked) == (
        "frontend/src/routes/+page.svelte",
    )
    assert judged_paths_from("The (`+page.svelte`) route crashes", tracked) == (
        "frontend/src/routes/+page.svelte",
    )


def test_a_bare_name_a_subdirectory_also_has_records_none() -> None:
    """The root ``README.md`` does not win over ``kew/README.md``."""
    tracked = ("README.md", "kew/README.md")

    assert judged_paths_from("Fix the steps in README.md", tracked) == ()
    assert judged_paths_from("Fix the steps in kew/README.md", tracked) == (
        "kew/README.md",
    )


def test_an_item_that_names_no_path_records_none() -> None:
    assert judged_paths_from("Make the console faster", ("src/app.py",)) == ()


def test_a_url_is_not_a_path_the_item_names() -> None:
    text = "Fix src/app.py (https://github.com/o/r/pull/12 has the trace)"

    assert judged_paths_from(text, ("src/app.py",)) == ("src/app.py",)


def test_files_that_could_not_be_listed_record_none() -> None:
    assert judged_paths_from("Fix src/app.py", None) == ()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [TOOL_PATHS["git"], "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The host-owned project repository; the judged base is its HEAD."""
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")
    repo = tmp_path / "project"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "app.py").write_text("print('hello')\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "base")
    monkeypatch.setenv("KEW_PROJECT", str(repo))
    return repo


def test_tracked_files_are_read_at_the_base_from_the_project_repository(
    project: Path,
) -> None:
    base = _git(project, "rev-parse", "HEAD")

    assert tracked_files(base) == ("src/app.py",)
    assert tracked_files("f" * 40) is None


def _context(store: SqliteRunStore, base: str, body: str) -> object:
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    return dispatch_core._DispatchContext(
        config=config,
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=None,
        provider="github",
        issue_num=42,
        issue_title="Nothing to do",
        issue_body=body,
        complexity="simple",
        item_id="42",
        branch="agent/issue-42",
        publication_base_sha=base,
        start_monotonic=0.0,
        refuse_rollback=lambda: None,
        terminal_failure=MagicMock(),
        run_kind="dispatch",
    )


def test_a_dispatch_records_the_paths_its_issue_text_names(
    project: Path, tmp_path: Path
) -> None:
    """The wire from the issue text to the attested evidence, end to end."""
    base = _git(project, "rev-parse", "HEAD")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="run-1",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
            )
        )
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=Actor("policy", "host-dispatch"),
            producer=Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="t"),
            run_id="run-1",
            facts={"fact": "outcome", "value": "no_changes"},
        )
        result = DispatchResult(
            exit_code=0, issue_num=42, log_file=tmp_path / "run.log", run_id="run-1"
        )
        ctx = _context(store, base, "The bug is in `src/app.py:1`.")
        # The agent's own checkout, cut from the project's main.
        checkout = tmp_path / "agent-checkout"
        _git(tmp_path, "clone", "-q", str(project), str(checkout))
        with patch("kew.labels.run"):
            dispatch_core._record_no_change(ctx, result, checkout)  # type: ignore[arg-type]

        [judgment] = store.list_no_change_judgments()

    assert judgment.state == PENDING_CONFIRMATION
    assert judgment.evidence is not None
    assert judgment.evidence.judged_paths == ("src/app.py",)
