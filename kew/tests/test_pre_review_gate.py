"""The merge gate's static checks and ui-checks run before any review (#3201).

Real git (a bare origin, a working clone, a pushed agent branch), a real run
store and the real review stage. Only the container runtime is faked, at the
process boundary (``run_container_with_cpu_cap_retry``): the fake plays the gate
tools by reading the checkout kew mounted into it, so what it answers depends on
the tree kew actually built — the head alone, the base, or the head merged with
main.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support.container_runtime import runtime_present
from support.driver_fakes import REPO, FakeFactsReader, FakeSink, FakeSource, _node
from support.gh_fakes import install_placeholder_forge
from support.review_stream import stream_verdict
from support.verify_lease_holder import lease_held
from typer.testing import CliRunner

from kew import sandbox
from kew.cli import app
from kew.cli.epic import _print_tick_categories
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig, load_config
from kew.epic_holds import REVIEW_CYCLE_RESET_HOLD_REASONS
from kew.graph.build import build_graph
from kew.graph.driver import pre_review_gate, review_stage
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.pre_review_gate import (
    HELD_OUT_HALF,
    MAX_GATE_ROUNDS,
    STATIC_HALF,
    UI_HALF,
    settled_halves,
    settled_key,
)
from kew.graph.driver.result import TerminalSignal, TickResult
from kew.graph.driver.review_detach import DetachedReviewSpawner
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.graph.driver.tick import advance, summarize
from kew.loops import verification
from kew.review_contract import CollectedSidecar
from kew.review_delta import REVIEW_MODE_PRE_REVIEW_GATE
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import WORK_MOUNT_PATH
from kew.subprocess_utils import TOOL_PATHS, BoundedOutput, OutputBudgetExceeded

EPIC = "epic:x"
ITEM = "42"
BRANCH = "agent/issue-42"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
BANDIT = "bandit -r src -q"
UI_CHECK = "npm run ui-check -- {modules}"
PLANT = "plant"
RED_EVERYWHERE = "red-everywhere"
MISSING_TOOL = "semgrep --config pinned.yml"
FLOOD = "mypy src"
#: What the fake mypy prints per error; thousands of them overflow the budget.
FLOOD_LINE = "src/app/run.py:1: error: Incompatible types  [assignment]\n"
IMAGE_ID = "sha256:" + "d" * 64

#: What the fake bandit prints for a shell=True call, as bandit 1.8 prints it.
BANDIT_REPORT = (
    ">> Issue: [B602:subprocess_popen_with_shell_equals_true] subprocess call "
    "with shell=True identified, security issue.\n"
    "   Severity: High   Confidence: High\n"
    "   Location: {path}:{line}:4\n"
)

#: Git programs a checkout can name for itself, which the host would run.
_HOOKS = (
    "reference-transaction",
    "post-checkout",
    "post-merge",
    "pre-merge-commit",
    "post-commit",
    "pre-auto-gc",
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(work: Path, message: str) -> str:
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(tmp_path: Path, branch_files: dict[str, str]) -> tuple[Path, Path, str]:
    """A bare origin with ``main`` and the agent branch; ``(origin, work, head)``."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _write(work, "src/app/__init__.py", "")
    _write(work, "frontend/src/routes/work/+page.svelte", "<h1>Work queue</h1>\n")
    _write(work, "frontend/scripts/ui-checks/work-rows.mjs", "// expect-text: Work\n")
    _commit(work, "base")
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-qb", BRANCH, cwd=work)
    for rel, text in branch_files.items():
        _write(work, rel, text)
    head = _commit(work, "agent work")
    _git("push", "-q", "origin", BRANCH, cwd=work)
    _git("checkout", "-q", "main", cwd=work)
    return origin, work, head


def _config(tmp_path: Path, *, static: str = "", ui_check: str = "") -> DispatchConfig:
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    config.commands.static = static
    config.commands.ui_check = ui_check
    config.sandbox.images = {"claude-code": "approved:tag"}
    return config


def _facts(head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


class _Container:
    """The container runtime, playing the gate tools.

    It answers from the checkout kew mounted into it, so a gate's result is a
    fact about the tree kew built. Every launch is kept for assertions: its
    argv, the mounted directory, and the tree it held.
    """

    def __init__(self) -> None:
        self.launches: list[list[str]] = []
        self.mounts: list[Path] = []
        self.trees: list[dict[str, str]] = []
        #: What the ``plant`` command writes into the mounted ``.git``.
        self.marker: Path | None = None
        self.replace: tuple[str, str] | None = None
        self.base_floods = False

    def __call__(
        self, argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.launches.append(argv)
        mount = next(
            arg.split(":")[0]
            for arg in argv
            if arg.endswith(f":{WORK_MOUNT_PATH}:rw")
            or arg.endswith(f":{WORK_MOUNT_PATH}:ro")
        )
        root = Path(mount)
        self.mounts.append(root)
        self.trees.append(
            {
                str(p.relative_to(root)): p.read_text()
                for p in root.rglob("*")
                if p.is_file() and ".git" not in p.parts
            }
        )
        command = argv[-1]
        if command.startswith("bandit"):
            return self._bandit(argv, root)
        if command.startswith("npm run ui-check"):
            return self._ui_check(argv, root, command.split("-- ", 1)[1].split())
        if command == PLANT:
            self._plant(root)
        if command == RED_EVERYWHERE:
            return subprocess.CompletedProcess(
                argv, 1, "E999 the image is broken\n", ""
            )
        if command == FLOOD:
            return self._flood(argv, root)
        if command == MISSING_TOOL:
            return subprocess.CompletedProcess(
                argv, 127, "", "sh: semgrep: not found\n"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _plant(self, root: Path) -> None:
        """What candidate code can do to a writable clone: name programs the
        next host git in it would run, and swap the objects it would read."""
        assert self.marker is not None
        script = root / ".git" / "kew-test-marker.sh"
        script.write_text(f"#!/bin/sh\necho ran >> {self.marker}\nexit 0\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        if self.replace is not None:
            _git("replace", "-f", *self.replace, cwd=root)
        _git("config", "core.fsmonitor", str(script), cwd=root)
        hooks = root / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in _HOOKS:
            shutil.copy(script, hooks / name)
            (hooks / name).chmod(0o755)

    def _flood(self, argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        """A type checker with thousands of errors in the head: more output
        than the runtime keeps. The base floods too when :attr:`base_floods`."""
        if "src/app/run.py" not in self.trees[-1] and not self.base_floods:
            return subprocess.CompletedProcess(argv, 0, "Success\n", "")
        stdout, stderr = BoundedOutput(4096), BoundedOutput(4096)
        stdout.append(FLOOD_LINE.encode() * 5000)
        raise OutputBudgetExceeded("stdout", {"stdout": stdout, "stderr": stderr})

    @staticmethod
    def _bandit(argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        for path in sorted((root / "src").rglob("*.py")):
            for line, text in enumerate(path.read_text().splitlines(), start=1):
                if "shell=True" in text:
                    rel = path.relative_to(root)
                    report = BANDIT_REPORT.format(path=rel, line=line)
                    return subprocess.CompletedProcess(argv, 1, report, "")
        return subprocess.CompletedProcess(argv, 0, "No issues identified.\n", "")

    @staticmethod
    def _ui_check(
        argv: list[str], root: Path, modules: list[str]
    ) -> subprocess.CompletedProcess[str]:
        page = (root / "frontend/src/routes/work/+page.svelte").read_text()
        for name in modules:
            module = root / f"frontend/scripts/ui-checks/{name}.mjs"
            for line in module.read_text().splitlines():
                expected = line.removeprefix("// expect-text: ")
                if expected != line and expected not in page:
                    out = f"ui-check: {name}: FAIL page has no text {expected!r}\n"
                    return subprocess.CompletedProcess(argv, 1, out, "")
        return subprocess.CompletedProcess(argv, 0, "ui-check: all passed\n", "")


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> _Container:
    """An approved image the runtime resolves, and the faked runtime itself."""
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    # The shell and `timeout` too, so a gate that wrongly ran on the host
    # would really run there.
    for tool in ("sh", "timeout", "gtimeout"):
        found = shutil.which(tool)
        if found:
            monkeypatch.setitem(TOOL_PATHS, tool, found)
    runtime_present(monkeypatch)
    monkeypatch.setattr(verification, "require_image_id", lambda *a, **k: IMAGE_ID)
    fake = _Container()
    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", fake)
    return fake


class _Reviewer:
    """A reviewer launcher that records each launch and answers READY."""

    def __init__(self) -> None:
        self.launched: list[str] = []

    def __call__(self, ctx: ReviewerLaunchContext) -> CollectedSidecar:
        self.launched.append(ctx.head_sha)
        evidence = {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
        return stream_verdict(
            ctx, {"verdict": "READY", "findings": [], "acceptance_evidence": [evidence]}
        )


def _drive(
    store: SqliteRunStore,
    config: DispatchConfig,
    work: Path,
    head: str,
    reviewer: _Reviewer | None,
    remediations: list[RemediationContext],
    *,
    worker_cap: int = 0,
) -> review_stage.ReviewStageOutcome:
    return drive_review_stage(
        {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=work,
        base="main",
        reviewer_launcher=reviewer,
        remediator=remediations.append,
        worker_cap=worker_cap,
        now=NOW,
    )


def _review_due_again(store: SqliteRunStore) -> None:
    """A READY on another head: this head is due a reviewer once more."""
    store.record_review_round(
        EPIC, ITEM, head_sha="0" * 40, verdict="READY", review_mode="full"
    )


def _sidecar(path: str | None) -> dict[str, Any]:
    assert path is not None
    loaded: dict[str, Any] = json.loads(Path(path).read_text())
    return loaded


def _shell_true_head(tmp_path: Path) -> tuple[Path, Path, str]:
    return _repo(
        tmp_path,
        {"src/app/run.py": "import subprocess\nsubprocess.call('x', shell=True)\n"},
    )


def test_a_head_that_fails_bandit_is_remediated_with_the_output_and_never_reviewed(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(
        tmp_path,
        {
            "src/app/run.py": (
                "import subprocess\n\n\ndef go(cmd):\n"
                "    return subprocess.call(cmd, shell=True)\n"
            )
        },
    )
    config = _config(tmp_path, static=BANDIT)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _drive(store, config, work, head, reviewer, remediations)

        assert reviewer.launched == []
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.head_sha, latest.review_mode) == (
            "NEEDS_CHANGES",
            head,
            REVIEW_MODE_PRE_REVIEW_GATE,
        )
    # The remediation round is dispatched on this tick, and its findings carry
    # bandit's own words and the file and line bandit named.
    assert [ctx.head_sha for ctx in remediations] == [head]
    assert outcome.remediating == [ITEM]
    findings = _sidecar(remediations[0].sidecar_path)["findings"]
    assert [f["location"] for f in findings] == ["src/app/run.py:5"]
    assert "B602:subprocess_popen_with_shell_equals_true" in findings[0]["summary"]
    assert f"`{BANDIT}`" in findings[0]["summary"]
    # The gate ran in the container over the agent's head, with no network,
    # then once over the base, which passes: so the failure is the head's.
    head_run, base_run = container.launches
    assert head_run[:2] == ["docker", "run"]
    assert head_run[head_run.index("--network") + 1] == "none"
    assert head_run[-2:] == ["-c", BANDIT] == base_run[-2:]
    assert "src/app/run.py" in container.trees[0]
    assert "src/app/run.py" not in container.trees[1]
    assert container.mounts[0] != container.mounts[1]


def test_a_clean_head_goes_to_review_and_the_gate_is_not_rerun_on_it(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    config = _config(tmp_path, static=BANDIT)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, remediations)
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.review_mode) == ("READY", "full")
        # A second review of the same head (a re-review a moved verdict asks
        # for) does not pay for the gate twice.
        _review_due_again(store)
        _drive(store, config, work, head, reviewer, remediations)

    assert reviewer.launched == [head, head]
    assert remediations == []
    assert len(container.launches) == 1


def test_candidate_code_in_a_gate_clone_never_runs_on_the_host_or_fakes_the_merge(
    tmp_path: Path, container: _Container
) -> None:
    """The static command runs the head's code with the clone writable. What it
    plants there (hooks, an fsmonitor, a replace ref) must never run as the
    operator, and must not change the merge candidate the ui-checks judge."""
    _, work, head = _repo(
        tmp_path,
        {"frontend/scripts/ui-checks/queue-title.mjs": "// expect-text: Work queue\n"},
    )
    _write(work, "docs/main-only.md", "main moved\n")
    base = _commit(work, "main moves on")
    _git("push", "-q", "origin", "main", cwd=work)
    marker = tmp_path / "ran-on-host"
    container.marker = marker
    # A replace ref swapping the head for the base: a host merge that honoured
    # it would build the base alone and drop the branch's new module.
    container.replace = (head, base)
    config = _config(tmp_path, static=PLANT, ui_check=UI_CHECK)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, [])

    assert not marker.exists()
    plant_run, ui_run = container.launches
    assert plant_run[-1] == PLANT
    assert ui_run[-1] == "npm run ui-check -- queue-title"
    assert container.mounts[0] != container.mounts[1]
    # The tree the ui-check judged is the real merge of main into the head.
    expected = tmp_path / "expected"
    subprocess.run(["git", "clone", "-q", str(work), str(expected)], check=True)
    _git("config", "user.email", "t@t.t", cwd=expected)
    _git("config", "user.name", "t", cwd=expected)
    _git("checkout", "-q", "--detach", head, cwd=expected)
    _git("merge", "-q", "--no-edit", base, cwd=expected)
    want = {
        str(p.relative_to(expected)): p.read_text()
        for p in expected.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    assert container.trees[1] == want
    assert reviewer.launched == [head]


def _ui_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """The branch adds a ui-check module; then main breaks what it checks."""
    origin, work, head = _repo(
        tmp_path,
        {"frontend/scripts/ui-checks/queue-title.mjs": "// expect-text: Work queue\n"},
    )
    _write(work, "frontend/src/routes/work/+page.svelte", "<h1>Work</h1>\n")
    _commit(work, "main renames the queue heading")
    _git("push", "-q", "origin", "main", cwd=work)
    return origin, work, head


def _refs(origin: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=origin)


def test_main_breaking_the_branchs_new_ui_check_fails_the_step_and_pushes_nothing(
    tmp_path: Path, container: _Container
) -> None:
    origin, work, head = _ui_repo(tmp_path)
    before = _refs(origin)
    config = _config(tmp_path, ui_check=UI_CHECK)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, remediations)
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.review_mode) == (
            "NEEDS_CHANGES",
            REVIEW_MODE_PRE_REVIEW_GATE,
        )

    assert reviewer.launched == []
    # Only the module the head added ran, on main merged into the head; then
    # the base's own module ran on the base, which passed.
    candidate, control = container.launches
    assert candidate[-1] == "npm run ui-check -- queue-title"
    assert control[-1] == "npm run ui-check -- work-rows"
    assert container.trees[0]["frontend/src/routes/work/+page.svelte"] == (
        "<h1>Work</h1>\n"
    )
    assert "frontend/scripts/ui-checks/queue-title.mjs" not in container.trees[1]
    findings = _sidecar(remediations[0].sidecar_path)["findings"]
    assert [f["location"] for f in findings] == [
        "frontend/scripts/ui-checks/queue-title.mjs:1"
    ]
    assert "page has no text 'Work queue'" in findings[0]["summary"]
    # No merge commit went anywhere: every ref on the origin is where it was.
    assert _refs(origin) == before


def test_the_same_branch_on_an_unchanged_main_passes_the_ui_check(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(
        tmp_path,
        {"frontend/scripts/ui-checks/queue-title.mjs": "// expect-text: Work queue\n"},
    )
    config = _config(tmp_path, ui_check=UI_CHECK)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, [])

    assert reviewer.launched == [head]
    assert [argv[-1] for argv in container.launches] == [
        "npm run ui-check -- queue-title"
    ]


def test_a_conflict_with_main_is_a_remediation_round_naming_the_files(
    tmp_path: Path, container: _Container
) -> None:
    origin, work, head = _repo(
        tmp_path,
        {
            "frontend/src/routes/work/+page.svelte": "<h1>Work items</h1>\n",
            "frontend/scripts/ui-checks/queue-title.mjs": "// expect-text: Work\n",
        },
    )
    _write(work, "frontend/src/routes/work/+page.svelte", "<h1>Queue</h1>\n")
    _commit(work, "main edits the same line")
    _git("push", "-q", "origin", "main", cwd=work)
    before = _refs(origin)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            store,
            _config(tmp_path, ui_check=UI_CHECK),
            work,
            head,
            reviewer,
            remediations,
        )

    assert reviewer.launched == []
    assert container.launches == []
    findings = _sidecar(remediations[0].sidecar_path)["findings"]
    assert [f["location"] for f in findings] == [
        "frontend/src/routes/work/+page.svelte:1"
    ]
    assert "does not merge with main" in findings[0]["summary"]
    assert _refs(origin) == before


def test_a_check_that_fails_on_the_base_too_is_skipped_and_not_rerun(
    tmp_path: Path, container: _Container, caplog: pytest.LogCaptureFixture
) -> None:
    """An image without the project's dependencies fails every tree alike. That
    is no finding against the head: no round, no remediation, a review, and
    the skip stands for this head like a pass."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    config = _config(tmp_path, static=RED_EVERYWHERE)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with caplog.at_level(logging.WARNING):
            _drive(store, config, work, head, reviewer, remediations)
        _review_due_again(store)
        _drive(store, config, work, head, reviewer, remediations)
        modes = [r.review_mode for r in store.review_rounds_in_cycle(ITEM)]

    assert remediations == []
    assert REVIEW_MODE_PRE_REVIEW_GATE not in modes
    assert reviewer.launched == [head, head]
    assert len(container.launches) == 2
    assert "src/app/run.py" in container.trees[0]
    assert "src/app/run.py" not in container.trees[1]
    assert any(
        "fail on the base" in r.getMessage() and "E999" in r.getMessage()
        for r in caplog.records
    )


def test_a_head_whose_check_overflows_the_output_budget_fails_the_gate(
    tmp_path: Path, container: _Container
) -> None:
    """Thousands of type errors are a failure, not a gate that could not run:
    the kept output is the evidence, the base passes, so the head is sent
    back rather than reviewed."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE: int = 'x'\n"})
    config = _config(tmp_path, static=FLOOD)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, remediations)
        settled = settled_halves(store, ITEM, head)

    assert reviewer.launched == []
    assert [ctx.head_sha for ctx in remediations] == [head]
    assert len(container.launches) == 2  # the head, then the base
    assert settled == set()
    findings = _sidecar(remediations[0].sidecar_path)["findings"]
    assert [f["location"] for f in findings] == ["src/app/run.py:1"]
    assert "output-budget refusal" in findings[0]["summary"]
    assert "Incompatible types" in findings[0]["summary"]


def test_a_check_that_overflows_on_the_base_too_is_skipped(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE: int = 'x'\n"})
    container.base_floods = True
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            store, _config(tmp_path, static=FLOOD), work, head, reviewer, remediations
        )
        settled = settled_halves(store, ITEM, head)

    assert remediations == []
    assert reviewer.launched == [head]
    assert len(container.launches) == 2
    assert settled == {STATIC_HALF}


def _symlink_head(tmp_path: Path, link: str, target: Path) -> tuple[Path, str]:
    """A head that commits *link* as a symlink to *target* on the host and
    changes a console page, so the ui-check half has something to select."""
    _, work, _ = _repo(tmp_path, {"docs/notes.md": "first commit\n"})
    _git("checkout", "-q", BRANCH, cwd=work)
    if (work / link).exists():
        _git("rm", "-rq", link, cwd=work)
    (work / link).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, work / link)
    _write(work, "frontend/src/routes/work/+page.svelte", "<h1>Work queue!</h1>\n")
    head = _commit(work, "agent work")
    _git("push", "-qf", "origin", BRANCH, cwd=work)
    _git("checkout", "-q", "main", cwd=work)
    return work, head


def test_a_committed_symlink_to_a_host_module_directory_is_never_followed(
    tmp_path: Path, container: _Container
) -> None:
    host = tmp_path / "host-secrets"
    host.mkdir()
    (host / "host-only-name.mjs").write_text("// ui-check-owns: src/**\n")
    work, head = _symlink_head(tmp_path, "frontend/scripts/ui-checks", host)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            store,
            _config(tmp_path, ui_check=UI_CHECK),
            work,
            head,
            reviewer,
            remediations,
        )

    assert all("host-only-name" not in " ".join(argv) for argv in container.launches)
    assert remediations == []
    assert reviewer.launched == [head]


def test_a_committed_symlink_module_is_not_read(
    tmp_path: Path, container: _Container
) -> None:
    host_file = tmp_path / "host-file.mjs"
    host_file.write_text("// ui-check-owns: src/**\n")
    work, head = _symlink_head(
        tmp_path, "frontend/scripts/ui-checks/linked.mjs", host_file
    )
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, _config(tmp_path, ui_check=UI_CHECK), work, head, reviewer, [])

    assert all("linked" not in " ".join(argv) for argv in container.launches)
    assert reviewer.launched == [head]


def test_a_tool_the_image_lacks_is_skipped_without_a_base_run(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    config = _config(tmp_path, static=MISSING_TOOL)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, remediations)
        _review_due_again(store)
        _drive(store, config, work, head, reviewer, remediations)
        settled = settled_halves(store, ITEM, head)

    assert remediations == []
    assert reviewer.launched == [head, head]
    assert len(container.launches) == 1
    assert settled == {STATIC_HALF}


def test_an_unconfigured_gate_reviews_the_head_and_touches_nothing(
    tmp_path: Path, container: _Container
) -> None:
    """No [commands] static or ui_check: the review goes ahead exactly as
    before the step existed — no container, no clone, no remembered result."""
    _, work, head = _shell_true_head(tmp_path)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, _config(tmp_path), work, head, reviewer, remediations)
        remembered = [
            store.get_meta(settled_key(ITEM, half)) for half in (STATIC_HALF, UI_HALF)
        ]
        modes = [r.review_mode for r in store.review_rounds_in_cycle(ITEM)]

    assert reviewer.launched == [head]
    assert container.launches == []
    assert remediations == []
    assert remembered == [None, None]
    assert modes == ["full"]


def test_without_a_container_the_gates_never_run_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _Container
) -> None:
    """Owner decision: mypy loads the candidate's plugins, so the static gates
    run contained or not at all — even when the operator lets kew skip
    containment elsewhere. The head goes to review as it did before."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    marker = tmp_path / "ran-on-host"
    config = _config(tmp_path, static=f"touch {marker}")
    config.sandbox.dangerously_skip_containment = True

    def unresolved(*_args: Any, **_kwargs: Any) -> str:
        raise sandbox.ContainedImageUnresolvedError("approved:tag is not local")

    monkeypatch.setattr(verification, "require_image_id", unresolved)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, [])

    assert not marker.exists()
    assert container.launches == []
    assert reviewer.launched == [head]


def test_a_container_that_could_not_launch_is_not_a_defect_in_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})

    def refused(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("docker: Cannot connect to the Docker daemon")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", refused)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            store, _config(tmp_path, static=BANDIT), work, head, reviewer, remediations
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert latest.review_mode == "full"

    assert remediations == []
    assert reviewer.launched == [head]


def test_a_gate_that_could_not_build_its_clone_says_so_and_sends_the_head_on(
    tmp_path: Path, container: _Container, caplog: pytest.LogCaptureFixture
) -> None:
    """A clone that fails proves nothing about the head: it goes on to the
    reviewer as it did before the step existed (whose own launch then fails on
    the same missing head), and a warning with the traceback says the gate
    never ran on it, so the review is not mistaken for a gated one."""
    _, work, _ = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    missing = "f" * 40  # a head the checkout never fetched
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            store,
            _config(tmp_path, static=BANDIT),
            work,
            missing,
            reviewer,
            remediations,
        )

    assert container.launches == []
    assert remediations == []
    assert any(
        f"reviewer dispatch failed for {ITEM}" in r.getMessage() for r in caplog.records
    )
    warned = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and f"pre-review gate could not run on {ITEM} at {missing[:12]}"
        in r.getMessage()
    ]
    assert len(warned) == 1 and warned[0].exc_info is not None


def test_a_merge_that_fails_with_no_conflict_loses_only_the_held_out_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _Container
) -> None:
    """git refuses the throwaway merge without a conflict to name: the static
    half, which runs on the head alone, still runs and stands; the held-out
    half sits this try out and is not remembered for the head."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    config = _config(tmp_path, static=BANDIT)
    config.verify.held_out_before_review = True
    config.verify.host = "gcp"
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _r, _n: "Closes #42\n")
    real_run = pre_review_gate.run

    def git(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "merge" in argv and "--no-ff" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: refused\n")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(pre_review_gate, "run", git)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, [])
        settled = settled_halves(store, ITEM, head)

    assert len(container.launches) == 1
    assert STATIC_HALF in settled and HELD_OUT_HALF not in settled
    assert reviewer.launched == [head]


def _seed_gate_rounds(store: SqliteRunStore) -> None:
    for n in range(MAX_GATE_ROUNDS):
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=f"{n}" * 40,
            verdict="NEEDS_CHANGES",
            review_mode=REVIEW_MODE_PRE_REVIEW_GATE,
        )


def test_a_head_that_keeps_failing_the_gate_is_held_under_the_gates_own_reason(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _shell_true_head(tmp_path)
    config = _config(tmp_path, static=BANDIT)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_gate_rounds(store)
        outcome = _drive(store, config, work, head, reviewer, remediations)
        holds = store.list_epic_holds(EPIC)

    assert outcome.held == [ITEM]
    (hold,) = holds
    assert hold.reason.startswith(
        "pre_review_gate_exhausted: static checks still fail after 3 rounds: "
        ">> Issue: [B602:subprocess_popen_with_shell_equals_true]"
    )
    assert "review_rounds_exhausted" not in hold.reason
    assert remediations == []
    assert reviewer.launched == []


def test_releasing_the_gates_hold_restarts_the_cycle_so_it_is_remediated_again(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _shell_true_head(tmp_path)
    config = _config(tmp_path, static=BANDIT)
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_gate_rounds(store)
        _drive(store, config, work, head, _Reviewer(), remediations)
        released = store.remove_epic_hold(
            EPIC, ITEM, fresh_review_cycle_reasons=REVIEW_CYCLE_RESET_HOLD_REASONS
        )
        assert released is not None
        outcome = _drive(store, config, work, head, _Reviewer(), remediations)

    assert outcome.held == []
    assert [ctx.head_sha for ctx in remediations] == [head]


def test_the_review_after_a_remediated_gate_round_is_a_full_round(
    tmp_path: Path, container: _Container
) -> None:
    """No reviewer read the head the gate failed, so the next reviewer reads
    the whole PR, and its READY stands without a second, full round."""
    _, work, head = _shell_true_head(tmp_path)
    config = _config(tmp_path, static=BANDIT)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, reviewer, remediations)
        # The remediation lands a fix on the branch.
        _git("checkout", "-q", BRANCH, cwd=work)
        _write(work, "src/app/run.py", "import subprocess\nsubprocess.call(['x'])\n")
        fixed = _commit(work, "fix: no shell")
        _git("push", "-q", "origin", BRANCH, cwd=work)
        _git("checkout", "-q", "main", cwd=work)
        _drive(store, config, work, fixed, reviewer, remediations)
        latest = store.latest_review_round(ITEM)
        assert latest is not None

    assert [ctx.head_sha for ctx in remediations] == [head]
    assert reviewer.launched == [fixed]
    assert (latest.head_sha, latest.verdict, latest.review_mode) == (
        fixed,
        "READY",
        "full",
    )


def test_a_running_local_merge_verify_starts_no_gate_container(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _shell_true_head(tmp_path)
    config = _config(tmp_path, static=BANDIT)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with lease_held():
            outcome = _drive(store, config, work, head, reviewer, remediations)
        rounds = store.review_rounds_in_cycle(ITEM)

    assert container.launches == []
    assert outcome.refused[ITEM].startswith("deferred: verify running")
    assert rounds == [] and remediations == [] and reviewer.launched == []


def test_no_gate_container_starts_when_no_reviewer_could_launch(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _shell_true_head(tmp_path)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _drive(store, _config(tmp_path, static=BANDIT), work, head, None, [])

    assert container.launches == []
    assert outcome.unwired == [ITEM]


def test_a_full_fleet_defers_the_gate_with_the_reviewer(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _shell_true_head(tmp_path)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(
            RunRecord(
                run_id="busy",
                issue_num=99,
                item_id="99",
                branch="agent/issue-99",
                status="pending",
                exit_code=0,
                started_at=datetime.now(UTC),
                owner_pid=os.getpid(),
            )
        )
        _drive(
            store,
            _config(tmp_path, static=BANDIT),
            work,
            head,
            reviewer,
            [],
            worker_cap=1,
        )
        rounds = store.review_rounds_in_cycle(ITEM)

    assert container.launches == []
    assert reviewer.launched == [] and rounds == []


def test_a_changed_surface_no_module_owns_is_uncovered_not_passed(
    tmp_path: Path, container: _Container, caplog: pytest.LogCaptureFixture
) -> None:
    _, work, head = _repo(
        tmp_path,
        {
            "frontend/src/lib/Orphan.svelte": "<p>unchecked</p>\n",
            "frontend/src/lib/Orphan.test.ts": "// a unit test renders nothing\n",
        },
    )
    config = _config(tmp_path, static=BANDIT, ui_check=UI_CHECK)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with caplog.at_level(logging.WARNING):
            _drive(store, config, work, head, reviewer, [])
        settled = settled_halves(store, ITEM, head)

    assert UI_HALF not in settled
    assert STATIC_HALF in settled
    assert reviewer.launched == [head]
    warned = [
        r.getMessage()
        for r in caplog.records
        if "no ui-check module covers" in r.getMessage()
    ]
    assert len(warned) == 1
    assert "frontend/src/lib/Orphan.svelte" in warned[0]
    assert "Orphan.test.ts" not in warned[0]


def test_advisory_gate_exhaustion_holds_instead_of_proceeding(
    tmp_path: Path, container: _Container
) -> None:
    """Advisory review used to attest DEGRADED here and let a head no reviewer
    read move on; it is held under the gate's own reason, as in blocking mode."""
    _, work, head = _shell_true_head(tmp_path)
    config = _config(tmp_path, static=BANDIT)
    config.review.blocking = False
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_gate_rounds(store)
        outcome = _drive(store, config, work, head, reviewer, remediations)
        holds = store.list_epic_holds(EPIC)
        verdicts = [r.verdict for r in store.review_rounds_in_cycle(ITEM)]
        attested = store.list_audit_events(item_id=ITEM)

    assert outcome.held == [ITEM]
    (hold,) = holds
    assert hold.reason.startswith(
        "pre_review_gate_exhausted: static checks still fail after 3 rounds: "
    )
    assert "DEGRADED" not in verdicts
    assert not any((e.payload or {}).get("verdict") == "DEGRADED" for e in attested)
    assert remediations == []
    assert reviewer.launched == []


def _refused_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("docker: Cannot connect to the Docker daemon")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", refused)


@pytest.mark.parametrize(
    ("command", "refuse", "expected"),
    [
        (RED_EVERYWHERE, False, "E999 the image is broken"),
        (BANDIT, True, "Cannot connect to the Docker daemon"),
    ],
    ids=["red-base", "refused-launch"],
)
def test_a_skipped_gate_is_on_the_tick_outcome_with_its_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    container: _Container,
    command: str,
    refuse: bool,
    expected: str,
) -> None:
    """A gate that did not check the head says so where the verdict is
    recorded: on the stage's outcome, which the tick line prints. A deferred
    review of the same head still names it; a head the gate passed does not."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    if refuse:
        _refused_launch(monkeypatch)
    config = _config(tmp_path, static=command)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _drive(store, config, work, head, reviewer, [])
        _review_due_again(store)
        again = _drive(store, config, work, head, reviewer, [])

    assert reviewer.launched == [head, head]
    assert expected in outcome.gate_skipped[ITEM]
    assert expected in again.gate_skipped[ITEM]

    result = TickResult(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.PROGRESSING,
        gate_skipped=tuple(outcome.gate_skipped.items()),
    )
    capsys.readouterr()
    _print_tick_categories(result)
    (line,) = capsys.readouterr().err.splitlines()
    assert "pre-review gate skipped" in line and ITEM in line and expected in line


def test_a_gate_that_passed_puts_nothing_on_the_tick_outcome(
    tmp_path: Path, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _drive(
            store, _config(tmp_path, static=BANDIT), work, head, reviewer, []
        )
        skip = pre_review_gate.gate_skip_reason(store, ITEM, head)

    assert reviewer.launched == [head]
    assert outcome.gate_skipped == {}
    assert skip is None


def test_a_skipped_gate_is_named_in_epic_status(
    tmp_path: Path, container: _Container
) -> None:
    """The skip is kept per item and per head, so the lines ``kew epic status``
    prints name it after the tick that recorded it is gone; an item the gate
    never skipped gets no line."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    config = _config(tmp_path, static=RED_EVERYWHERE)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, _Reviewer(), [])
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        lines = pre_review_gate.gate_skip_lines(store, [ITEM, "43"], statuses={})

    (line,) = lines
    assert "pre-review gate skipped" in line
    assert ITEM in line and head[:12] in line and "E999 the image is broken" in line


class _Children:
    """The detached stage's process boundary: every ``kew epic _review-node``
    the spawner starts is recorded and stays running, never run."""

    def __init__(self) -> None:
        self.started: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> Any:
        self.started.append(cmd)
        return self

    def poll(self) -> None:
        return None


def _skip_lines(result: TickResult, capsys: pytest.CaptureFixture[str]) -> list[str]:
    """The tick's printed lines that name a pre-review gate skip."""
    capsys.readouterr()
    _print_tick_categories(result)
    return [
        ln
        for ln in capsys.readouterr().err.splitlines()
        if "pre-review gate skipped" in ln
    ]


def test_the_drive_tick_names_the_skip_its_detached_review_child_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    container: _Container,
) -> None:
    """The drive's review stage is detached: the gate runs in the
    ``kew epic _review-node`` child, whose outcome never reaches the tick; only
    the run store does. The next tick reads the skip the child's gate recorded
    there, so its line names it (#3344)."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    monkeypatch.chdir(work)
    install_placeholder_forge(monkeypatch)
    config = _config(tmp_path, static=RED_EVERYWHERE)
    config.project.repo = REPO
    config.review.publish_status = False
    config.review.publish_comment = False
    reader = FakeFactsReader(
        issue_facts_by_num={
            int(ITEM): replace(_facts(head), merge_state_status="CLEAN")
        },
        pr_facts_by_num={7: {"number": 7, "state": "OPEN", "headRefOid": head}},
    )
    children = _Children()
    saved_tools = TOOL_PATHS.copy()

    def tick(store: SqliteRunStore) -> TickResult:
        return advance(
            EPIC,
            config=config,
            store=store,
            deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
            facts=reader,
            dispatch_sink=FakeSink(),
            max_parallel=3,
            now=NOW,
            lock_path=tmp_path / "epic.lock",
            approvers=["human"],
            repo=REPO,
            review_detacher=DetachedReviewSpawner(config, base="main", popen=children),
        )

    try:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            launched = tick(store)
            # The child's gate, recording into its own ledger handle exactly
            # what the review stage records for the head it runs on.
            child_store = store.open_sibling()
            try:
                gate = pre_review_gate.run_pre_review_gate(
                    config, repo_root=work, base="main", head=head, store=child_store
                )
                pre_review_gate.record_skips(child_store, ITEM, head, gate.skipped)
            finally:
                child_store.close()
            after = tick(store)
    finally:
        TOOL_PATHS.clear()
        TOOL_PATHS.update(saved_tools)

    assert len(children.started) == 1
    assert children.started[0][:3] == ["kew", "epic", "_review-node"]
    assert gate.kind == "skipped"
    assert _skip_lines(launched, capsys) == []
    (line,) = _skip_lines(after, capsys)
    assert f"#{ITEM}" in line and "E999 the image is broken" in line


def test_the_tick_names_no_skip_for_a_head_the_merge_main_step_just_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A skip recorded on the head the tick read stops being news once the
    tick's merge-main step moves the branch: the head that will be reviewed
    next is a new one the gate has not seen, so the tick line leaves it out."""
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    monkeypatch.chdir(work)
    install_placeholder_forge(monkeypatch)
    config = _config(tmp_path)
    config.project.repo = REPO
    config.review.publish_status = False
    config.review.publish_comment = False
    dirty = replace(_facts(head), merge_state_status="DIRTY")
    reader = FakeFactsReader(
        issue_facts_by_num={int(ITEM): dirty},
        pr_facts_by_num={
            7: {
                "number": 7,
                "state": "OPEN",
                "headRefOid": head,
                "mergeStateStatus": "DIRTY",
            }
        },
    )
    steps: list[BaseIntegrationContext] = []

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        steps.append(ctx)
        return BaseIntegrationOutcome("integrated")

    saved_tools = TOOL_PATHS.copy()
    try:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            pre_review_gate.record_skips(
                store, ITEM, head, [(STATIC_HALF, "static checks: E999 broken")]
            )
            result = advance(
                EPIC,
                config=config,
                store=store,
                deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
                facts=reader,
                dispatch_sink=FakeSink(),
                max_parallel=3,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=["human"],
                repo=REPO,
                reviewer_launcher=_Reviewer(),
                base_integrator=integrator,
            )
            still_recorded = pre_review_gate.gate_skip_reason(store, ITEM, head)
    finally:
        TOOL_PATHS.clear()
        TOOL_PATHS.update(saved_tools)

    assert [ctx.head_sha for ctx in steps] == [head]
    assert still_recorded == "static checks: E999 broken"
    assert result.gate_skipped == ()
    assert _skip_lines(result, capsys) == []


_cli = CliRunner()


def _epic_status_output(tmp_path: Path, head: str) -> str:
    """``kew epic status`` over a store where the gate skipped *head* on #42
    (in review) and on #43 (its PR merged, the node done)."""
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.branches.worktree_base = str(tmp_path / "wt")
    deps = FakeSource(nodes=[_node(ITEM), _node("43")], edges=[])
    reader = FakeFactsReader(
        issue_facts_by_num={
            int(ITEM): _facts(head),
            43: replace(
                _facts(head), issue_num=43, state="CLOSED", state_reason="COMPLETED"
            ),
        },
        pr_facts_by_num={7: {"number": 7, "state": "OPEN", "headRefOid": head}},
    )
    graph = build_graph(deps, EPIC)
    db = tmp_path / "logs" / "dispatch.db"
    db.parent.mkdir(parents=True)
    with SqliteRunStore(str(db)) as store:
        for item in (ITEM, "43"):
            pre_review_gate.record_skips(
                store, item, head, [(STATIC_HALF, "static checks: E999 broken")]
            )
        summary = summarize(EPIC, graph, config=config, store=store, facts=reader)
    assert summary.statuses["43"] == DriverStatus.DONE.value
    assert summary.statuses[ITEM] != DriverStatus.DONE.value
    with (
        patch("kew.cli.project_root", return_value=tmp_path),
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli._load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
    ):
        result = _cli.invoke(app, ["epic", "status", EPIC])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_epic_status_prints_the_skip_line_naming_the_item_head_and_reason(
    tmp_path: Path,
) -> None:
    head = "a" * 40

    out = _epic_status_output(tmp_path, head)

    assert (
        f"  pre-review gate skipped: #{ITEM} at {head[:12]}: static checks: E999 broken"
    ) in out.splitlines()


def test_epic_status_prints_no_skip_line_for_a_finished_node(tmp_path: Path) -> None:
    """A node whose PR merged is done with its head: the skip on it is no
    longer a line to act on."""
    out = _epic_status_output(tmp_path, "a" * 40)

    assert "#43" not in out


def test_an_unreadable_skip_record_is_warned_not_read_as_no_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt skip record cannot say whether the gate skipped a head; it is
    logged as unreadable instead of passing silently for "nothing skipped"."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.set_meta(pre_review_gate._SKIPPED_META_PREFIX + ITEM, "{not json")
        with caplog.at_level(logging.WARNING, logger=pre_review_gate.__name__):
            skip = pre_review_gate.gate_skip(store, ITEM)

    assert skip is None
    assert any(
        "unreadable" in r.getMessage() and ITEM in r.getMessage()
        for r in caplog.records
    )


def test_the_review_after_a_skipped_gate_says_so_on_the_verdict_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _Container
) -> None:
    _, work, head = _repo(tmp_path, {"src/app/run.py": "VALUE = 1\n"})
    gh = str(tmp_path / "gh")
    argvs: list[list[str]] = []
    real_run = subprocess.run

    def run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv[0] != gh:
            return real_run(argv, *args, **kwargs)
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setitem(TOOL_PATHS, "gh", gh)
    monkeypatch.setattr(subprocess, "run", run)
    config = _config(tmp_path, static=RED_EVERYWHERE)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, config, work, head, _Reviewer(), [])

    (body,) = [
        a.removeprefix("body=") for v in argvs for a in v if a.startswith("body=")
    ]
    (line,) = [ln for ln in body.splitlines() if "pre-review gate" in ln]
    assert "did not check this head" in line and "E999 the image is broken" in line


def test_kews_own_config_runs_the_gate_and_resolve_checks_in_the_check_image() -> None:
    config = load_config(Path(__file__).resolve().parents[2] / "kew.toml")

    assert config.commands.static.strip()
    assert "pytest -m policy" in config.commands.static
    assert config.resolve.python_checks.strip()
    assert config.resolve.frontend_checks.strip()
    assert config.sandbox.check_image.startswith("kew-check:")
