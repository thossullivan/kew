"""What a remediation or dispatch brief tells a sandboxed agent to run (#3201).

The brief scopes a round's tests to the files its findings name, names the cap
kew really enforces, and names the merge candidate and bandit.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from kew.config import DispatchConfig
from kew.prompt import build_prompt
from kew.review import ReviewComment, build_review_prompt, round_test_targets
from kew.subprocess_utils import TOOL_PATHS
from kew.ui_check_modules import touched_modules
from kew.worktree import ConflictInfo

MODULES = 100

Render = Callable[[DispatchConfig], str]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def hundred_module_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose PR adds 100 modules, each with its own test module."""
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    root = tmp_path / "checkout"
    (root / "src" / "kew").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "kew" / "__init__.py").write_text("")
    _git("init", "-q", "-b", "main", cwd=root)
    _git("-c", "user.name=t", "-c", "user.email=t@t.t", "add", "-A", cwd=root)
    _git(
        "-c", "user.name=t", "-c", "user.email=t@t.t", "commit", "-qm", "base", cwd=root
    )
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=root)
    _git("checkout", "-qb", "agent/issue-1", cwd=root)
    # One helper, imported by one module only: the selector's import graph is
    # what ties a finding in it to that module's tests.
    (root / "src" / "kew" / "helpers.py").write_text(
        "def double(x):\n    return 2 * x\n"
    )
    for n in range(MODULES):
        imports = "from kew.helpers import double\n\n" if n == 3 else ""
        (root / "src" / "kew" / f"mod_{n}.py").write_text(f"{imports}VALUE = {n}\n")
        (root / "tests" / f"test_mod_{n}.py").write_text(
            f"from kew import mod_{n}\n\n\ndef test_value():\n"
            f"    assert mod_{n}.VALUE == {n}\n"
        )
    _git("-c", "user.name=t", "-c", "user.email=t@t.t", "add", "-A", cwd=root)
    _git("-c", "user.name=t", "-c", "user.email=t@t.t", "commit", "-qm", "pr", cwd=root)
    return root


def _comment(path: str | None) -> ReviewComment:
    return ReviewComment(
        id=1,
        author="kew",
        body="[high] VALUE is wrong",
        path=path,
        line=1 if path else None,
        state="COMMENTED",
        in_reply_to=None,
        resolved=False,
    )


def _brief(config: DispatchConfig, root: Path, comment: ReviewComment) -> str:
    targets, scoped = round_test_targets(root, "main", comments=[comment])
    return build_review_prompt(
        pr_number=7,
        issue_number=1,
        branch_name="agent/issue-1",
        comments=[comment],
        config=config,
        governed=True,
        sandboxed=True,
        targeted_tests=targets,
        targets_scoped=scoped,
    )


def test_a_one_file_finding_on_a_hundred_module_pr_names_only_that_files_tests(
    hundred_module_pr: Path,
) -> None:
    brief = _brief(DispatchConfig(), hundred_module_pr, _comment("src/kew/mod_7.py"))

    named = {
        f"tests/test_mod_{n}.py"
        for n in range(MODULES)
        if f"`tests/test_mod_{n}.py`" in brief
    }
    assert named == {"tests/test_mod_7.py"}
    assert "cover the files this round's findings name" in brief


def test_a_finding_in_a_helper_names_the_tests_of_what_imports_it(
    hundred_module_pr: Path,
) -> None:
    """No test module is named after ``helpers.py``; the import graph is what
    reaches ``test_mod_3`` through ``mod_3``."""
    brief = _brief(DispatchConfig(), hundred_module_pr, _comment("src/kew/helpers.py"))

    named = {
        f"tests/test_mod_{n}.py"
        for n in range(MODULES)
        if f"`tests/test_mod_{n}.py`" in brief
    }
    assert named == {"tests/test_mod_3.py"}


def test_a_finding_that_names_no_file_keeps_the_whole_pr_rule(
    hundred_module_pr: Path,
) -> None:
    brief = _brief(DispatchConfig(), hundred_module_pr, _comment(None))

    named = [n for n in range(MODULES) if f"`tests/test_mod_{n}.py`" in brief]
    assert len(named) == MODULES
    assert "named after the source modules this PR changes" in brief


def _dispatch_brief(config: DispatchConfig) -> str:
    return build_prompt(
        issue_num=1,
        issue_title="t",
        issue_body="b",
        complexity="medium",
        config=config,
        has_coderabbit=False,
        sandboxed=True,
    )


def _review_brief(config: DispatchConfig) -> str:
    return build_review_prompt(
        pr_number=7,
        issue_number=1,
        branch_name="agent/issue-1",
        comments=[],
        config=config,
        governed=True,
        sandboxed=True,
    )


@pytest.mark.parametrize("render", [_dispatch_brief, _review_brief])
def test_a_lowered_memory_cap_is_the_cap_the_brief_names(render: Render) -> None:
    config = DispatchConfig()
    config.sandbox.max_memory = "1g"

    brief = render(config)

    assert "1 GB memory cap" in brief
    assert "8 GB" not in brief


def test_a_host_brief_reads_no_sandbox_setting() -> None:
    """A host run has no sandbox: its brief names no cap, and a [sandbox] value
    it could not parse (or one a caller never set) must not stop the round."""
    config = DispatchConfig()
    config.sandbox = config.sandbox.model_copy(update={"max_memory": object()})

    review = build_review_prompt(
        pr_number=7,
        issue_number=1,
        branch_name="agent/issue-1",
        comments=[],
        config=config,
    )
    dispatch = build_prompt(
        issue_num=1,
        issue_title="t",
        issue_body="b",
        complexity="medium",
        config=config,
        has_coderabbit=False,
        sandboxed=False,
    )

    for brief in (review, dispatch):
        assert "memory cap" not in brief


@pytest.mark.parametrize("render", [_dispatch_brief, _review_brief])
def test_an_openshell_sandbox_names_no_cap_kew_does_not_enforce(render: Render) -> None:
    config = DispatchConfig()
    config.sandbox.runtime = "openshell"
    config.sandbox.max_memory = "1g"

    brief = render(config)

    assert "kew enforces no memory cap on this runtime" in brief
    assert "GB memory cap" not in brief


@pytest.mark.parametrize("render", [_dispatch_brief, _review_brief])
def test_the_brief_names_bandit_and_the_merge_candidate(render: Render) -> None:
    config = DispatchConfig()
    config.commands.static = "bash scripts/static-gates.sh"
    config.commands.ui_check = "cd frontend && npm run ui-check -- {modules}"

    brief = render(config)

    assert "bandit" in brief
    assert "`git worktree add --detach /tmp/kew-merge-candidate HEAD`" in brief
    assert "`git -C /tmp/kew-merge-candidate merge --no-edit origin/main`" in brief
    assert "`git worktree remove --force /tmp/kew-merge-candidate`" in brief
    assert "`bash scripts/static-gates.sh`" in brief
    assert "merged with the current `origin/main`" in brief


@pytest.mark.parametrize("render", [_dispatch_brief, _review_brief])
def test_the_merge_candidate_check_never_merges_into_the_agents_own_tree(
    render: Render,
) -> None:
    """A merge-and-abort in the agent's own tree discards uncommitted work, and
    in a round resolving a base merge it throws away the staged resolution."""
    brief = render(DispatchConfig())

    assert "git merge --abort" not in brief
    assert "merge --no-commit" not in brief
    assert "`git rev-parse -q --verify MERGE_HEAD` succeeds" in brief


def test_a_round_resolving_a_base_merge_is_told_to_keep_its_merge() -> None:
    brief = build_review_prompt(
        pr_number=7,
        issue_number=1,
        branch_name="agent/issue-1",
        comments=[],
        config=DispatchConfig(),
        governed=True,
        sandboxed=True,
        conflict_info=ConflictInfo(
            strategy_used="merge",
            conflicted_files=["frontend/src/routes/work/+page.svelte"],
            base_branch="main",
        ),
    )

    assert "git merge --abort" not in brief
    assert "Skip this step while a merge is in progress" in brief
    assert "complete and commit it first" in brief


def test_a_changed_surface_selects_the_module_that_owns_it() -> None:
    sources = {
        "issue-table": (
            "/** Rows. */\n// ui-check-owns: src/lib/components/IssueList.svelte\n"
        ),
        "spend-meter": "/**\n * ui-check-owns: src/lib/budget/**\n */\n",
        "unrelated": "// no ownership line\n",
    }

    assert touched_modules(
        ["frontend/src/lib/components/IssueList.svelte"], sources=sources
    ) == ["issue-table"]
    assert touched_modules(
        [
            "frontend/src/lib/budget/deep/Meter.svelte",
            "frontend/scripts/ui-checks/unrelated.mjs",
            "kew/src/kew/review.py",
        ],
        sources=sources,
    ) == ["spend-meter", "unrelated"]
