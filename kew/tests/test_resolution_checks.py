"""A resolution kew pushes passes the node's checks first (#3200).

On 2026-10-25 a merge-main resolution on agent/issue-3148 reintroduced
``atRoute`` in ``controller.ts``, a symbol an operator fix had removed, and the
branch stopped compiling. Nothing ran the resolved tree before the push, and a
Python-only gate would have passed it.

Each fixture is a real repository: ``main``, the PR branch, and a real
``git merge`` of ``main`` into it, which is the tree ``kew resolve`` publishes.
The checks are the operator's configured commands, run for real. Only the
edges leave the process: the forge read, the remote ref reads, the push
(``publish_checkout``, which records what it was asked to publish), and
``svelte-check``/``vitest``, which are stand-ins on ``PATH``.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.resolve_forge import Push, resolve_forge

from kew.command_outcome import CommandOutcome
from kew.config import (
    DispatchConfig,
    LoggingConfig,
    ResolveConfig,
    ReviewConfig,
    SandboxConfig,
)
from kew.graph.driver.base_integration import (
    INTEGRATION_CONFLICT_HOLD,
    INTEGRATION_FAILED_HOLD,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_launcher import sandbox_base_integrator
from kew.graph.driver.review_stage import drive_review_stage
from kew.resolve import _finish_conflict_resolution, dispatch_resolve
from kew.resolve_checks import (
    ResolutionChecksFailed,
    deleted_paths,
    readable_output,
    run_resolution_checks,
    touched_paths,
)
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS, run
from kew.tracking import DispatchResult
from kew.worktree import ConflictInfo

BRANCH = "agent/issue-5"
PR = 99

#: ``svelte-check`` as far as these fixtures need it: every named import of a
#: ``.ts`` module under ``frontend/src`` must be one that module exports.
_SVELTE_CHECK = """\
import pathlib, re, sys
IMPORT = re.compile(r"import \\{([^}]*)\\} from '\\./(\\w+)'")
EXPORT = re.compile(r"export (?:const|function) (\\w+)")
errors = []
for path in sorted(pathlib.Path("frontend/src").glob("*.ts")):
    for names, mod in IMPORT.findall(path.read_text()):
        exported = set(EXPORT.findall((path.parent / f"{mod}.ts").read_text()))
        for name in (n.strip() for n in names.split(",")):
            if name not in exported:
                errors.append(f"{path}: './{mod}' has no exported member '{name}'")
print("\\n".join(errors) or "svelte-check found 0 errors")
sys.exit(1 if errors else 0)
"""

#: ``vitest related --run <paths>``: runs the touched test files with node's
#: own test runner.
_VITEST = """\
[ "$1" = related ] && shift
[ "$1" = --run ] && shift
tests=""
for path in "$@"; do
  case "$path" in *.test.mjs) tests="$tests $path" ;; esac
done
[ -z "$tests" ] && exit 0
exec node --test $tests
"""


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for tool in ("git", "sh"):
        found = shutil.which(tool)
        assert found is not None, f"{tool} required"
        monkeypatch.setitem(TOOL_PATHS, tool, found)
    # The check's deadline wrapper, where the host has one (as preflight does).
    for tool in ("timeout", "gtimeout"):
        found = shutil.which(tool)
        if found is not None:
            monkeypatch.setitem(TOOL_PATHS, tool, found)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    svelte_check = bin_dir / "svelte-check"
    svelte_check.write_text(_SVELTE_CHECK)
    (bin_dir / "vitest").write_text(_VITEST)
    # Run through their interpreters: a tmp mount may forbid exec.
    monkeypatch.setenv("STUBS", str(bin_dir))
    monkeypatch.setenv("PYTHON", sys.executable)
    # No container runtime on this host: the operator's
    # dangerously_skip_containment admits the host posture.
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda _config: None)
    return bin_dir


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo, check=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _merged(
    tmp_path: Path,
    *,
    base: dict[str, str],
    branch: dict[str, str],
    main: dict[str, str],
    main_deletes: tuple[str, ...] = (),
) -> tuple[Path, str]:
    """``main`` merged into the PR branch, as ``kew resolve`` integrates it.

    ``main_deletes`` are base files main removes in the same commit. Returns
    ``(worktree, pr_head)``; ``origin/main`` names the merged base.
    """
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, base, "base")
    _git(repo, "checkout", "-q", "-b", BRANCH)
    pr_head = _commit(repo, branch, "operator fix")
    _git(repo, "checkout", "-q", "main")
    for rel in main_deletes:
        (repo / rel).unlink()
    base_sha = _commit(repo, main, "sibling lands")
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)
    _git(repo, "checkout", "-q", BRANCH)
    _git(repo, "merge", "-q", "--no-edit", "main")
    return repo, pr_head


def _at_route(tmp_path: Path) -> tuple[Path, str]:
    """The 3148 shape: the operator removed ``atRoute``; main's new panel uses it."""
    return _merged(
        tmp_path,
        base={
            "frontend/src/routes.ts": "export const atRoute = (p: string) => p;\n",
            "frontend/src/controller.ts": (
                "import { atRoute } from './routes';\nexport const c = atRoute('/');\n"
            ),
        },
        branch={
            "frontend/src/routes.ts": "export const toRoute = (p: string) => p;\n",
            "frontend/src/controller.ts": (
                "import { toRoute } from './routes';\nexport const c = toRoute('/');\n"
            ),
        },
        main={
            "frontend/src/panel.ts": (
                "import { atRoute } from './routes';\nexport const p = atRoute('/p');\n"
            ),
        },
    )


def _broken_frontend_test(tmp_path: Path) -> tuple[Path, str]:
    """The branch changed ``sum``; main's new test still expects the old one."""
    return _merged(
        tmp_path,
        base={"frontend/src/sum.mjs": "export const sum = (a, b) => a + b;\n"},
        branch={"frontend/src/sum.mjs": "export const sum = (a, b) => a + b + 1;\n"},
        main={
            "frontend/src/sum.test.mjs": (
                "import test from 'node:test';\n"
                "import assert from 'node:assert';\n"
                "import { sum } from './sum.mjs';\n"
                "test('sum', () => assert.strictEqual(sum(1, 2), 3));\n"
            ),
        },
    )


def _broken_python(tmp_path: Path) -> tuple[Path, str]:
    """The branch removed ``helper``; main's new module still imports it."""
    return _merged(
        tmp_path,
        base={
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper() -> int:\n    return 1\n",
        },
        branch={"pkg/util.py": "def renamed() -> int:\n    return 1\n"},
        main={"pkg/panel.py": "from pkg.util import helper\n\nVALUE = helper()\n"},
    )


def _green_frontend(tmp_path: Path) -> tuple[Path, str]:
    return _merged(
        tmp_path,
        base={"frontend/src/routes.ts": "export const toRoute = (p: string) => p;\n"},
        branch={"frontend/src/notes.md": "operator notes\n"},
        main={
            "frontend/src/panel.ts": (
                "import { toRoute } from './routes';\nexport const p = toRoute('/p');\n"
            ),
        },
    )


_MYPY = f"{sys.executable} -m mypy --no-incremental --ignore-missing-imports {{paths}}"


def _config(*, python: str = "", frontend: str = "") -> DispatchConfig:
    config = DispatchConfig(
        resolve=ResolveConfig(python_checks=python, frontend_checks=frontend),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.project.repo = "o/r"
    return config


_FRONTEND = (
    '"$PYTHON" "$STUBS/svelte-check" && sh "$STUBS/vitest" related --run {paths}'
)


def _resolve(worktree: Path, pr_head: str, config: DispatchConfig) -> tuple[int, Push]:
    """``kew resolve`` on a PR whose base integrated without conflicts."""
    with _forge(worktree, pr_head) as push:
        outcome = dispatch_resolve(pr_number=PR, config=config)
    return outcome.exit_code, push


def _forge(worktree: Path, pr_head: str) -> AbstractContextManager[Push]:
    """The forge for this module's PR, whose base merged cleanly into
    *worktree*."""
    return resolve_forge(
        worktree,
        branch=BRANCH,
        pr_head=pr_head,
        item=5,
        base_sha=_git(worktree, "rev-parse", "origin/main"),
    )


@pytest.mark.parametrize(
    ("fixture", "config", "red"),
    [
        pytest.param(
            _at_route,
            _config(frontend=_FRONTEND),
            "has no exported member 'atRoute'",
            id="svelte-check",
        ),
        pytest.param(
            _broken_frontend_test,
            _config(frontend=_FRONTEND),
            r"sum\.test\.mjs.*Expected values to be strictly equal",
            id="vitest",
        ),
        pytest.param(
            _broken_python,
            _config(python=_MYPY),
            'Module "pkg.util" has no attribute "helper"',
            id="python-static-gates",
        ),
    ],
)
def test_a_resolution_that_fails_its_checks_pushes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fixture: Any,
    config: DispatchConfig,
    red: str,
) -> None:
    worktree, pr_head = fixture(tmp_path)

    with pytest.raises(ResolutionChecksFailed, match=red):
        run_resolution_checks(worktree, config, since=pr_head)

    exit_code, push = _resolve(worktree, pr_head, config)

    assert exit_code == 1
    assert push.published == []
    assert "fails its" in capsys.readouterr().err


def test_a_resolution_that_passes_its_checks_is_pushed(tmp_path: Path) -> None:
    worktree, pr_head = _green_frontend(tmp_path)

    exit_code, push = _resolve(worktree, pr_head, _config(frontend=_FRONTEND))

    assert exit_code == 0
    assert push.published == [worktree]


def test_a_python_check_is_not_run_on_a_frontend_only_resolution(
    tmp_path: Path,
) -> None:
    """Each check runs only over files of its kind: a Python gate that would
    fail on anything does not block a resolution that touched no Python."""
    worktree, pr_head = _green_frontend(tmp_path)

    run_resolution_checks(worktree, _config(python="false {paths}"), since=pr_head)


def test_a_file_the_base_deleted_is_not_handed_to_a_check(tmp_path: Path) -> None:
    """Main deleted ``pkg/old.py`` while the branch edited ``pkg/keep.py``: the
    merge removes ``old.py``, so a check given its path would fail on a file
    that no longer exists and hold every integration across the deletion."""
    worktree, pr_head = _merged(
        tmp_path,
        base={"pkg/keep.py": "A = 1\n", "pkg/old.py": "B = 2\n"},
        branch={"pkg/keep.py": "A = 3\n"},
        main={"pkg/new.py": "C = 4\n"},
        main_deletes=("pkg/old.py",),
    )

    assert touched_paths(worktree, since=pr_head) == ["pkg/new.py"]
    exit_code, push = _resolve(worktree, pr_head, _config(python="ls {paths}"))

    assert exit_code == 0
    assert push.published == [worktree]


def test_a_check_with_no_admitted_posture_pushes_nothing(tmp_path: Path) -> None:
    """Containment on and no image: the check cannot run, so nothing is pushed."""
    worktree, pr_head = _at_route(tmp_path)
    config = _config(frontend=_FRONTEND)
    config.sandbox.dangerously_skip_containment = False

    exit_code, push = _resolve(worktree, pr_head, config)

    assert exit_code == 1
    assert push.published == []


# --------------------------------------------------------------------------- #
# Whole-tree checks: a semantic conflict breaks a file nobody touched.
# --------------------------------------------------------------------------- #

#: mypy over the whole package, as kew's own ``[resolve] python_checks`` runs
#: it: no ``{paths}``, from the worktree root, writing no cache into the tree.
#: The fixtures import nothing from outside ``pkg``, so a missing module is
#: an error here, as it is in kew's own mypy configuration.
_WHOLE_TREE_MYPY = (
    f"{sys.executable} -m mypy --no-incremental --cache-dir=/dev/null pkg"
)

_HELPER_GONE = 'pkg/caller.py:3: error: Module "pkg.util" has no attribute "helper"'


def _helper_removed_on_main(tmp_path: Path) -> tuple[Path, str]:
    """The branch adds a caller of ``helper()``; main removes ``helper()``.

    Git merges this cleanly and the only file the integration changes is
    ``pkg/util.py``, so a check over the changed files passes the broken tree.
    """
    return _merged(
        tmp_path,
        base={
            "pkg/__init__.py": "",
            "pkg/util.py": (
                "def helper() -> int:\n    return 1\n\n\n"
                "def other() -> int:\n    return 2\n"
            ),
        },
        branch={
            "pkg/caller.py": (
                '"""Calls helper."""\n\nfrom pkg.util import helper\n\n'
                "VALUE: int = helper()\n"
            )
        },
        main={"pkg/util.py": "def other() -> int:\n    return 2\n"},
    )


EPIC = "epic:checks"
ITEM = "5"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _drive(
    store: SqliteRunStore,
    tmp_path: Path,
    pr_head: str,
    config: DispatchConfig,
    *,
    resolve: Callable[..., CommandOutcome] | None = None,
) -> None:
    """One review-stage tick for a node whose PR stopped merging into main."""
    config.review = ReviewConfig(
        adversarial=True,
        blocking=True,
        max_rounds=2,
        publish_status=False,
        publish_comment=False,
    )
    config.logging = LoggingConfig(dir=str(tmp_path / "logs"))
    drive_review_stage(
        {ITEM: DriverStatus.IN_REVIEW},
        {
            ITEM: NodeFacts(
                issue_num=int(ITEM),
                state="OPEN",
                state_reason=None,
                labels=frozenset(),
                pr_number=PR,
                pr_url=None,
                review_decision=None,
                merge_state_status="DIRTY",
                head_oid=pr_head,
                pr_pushed_at=None,
            )
        },
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        base_integrator=sandbox_base_integrator(
            config, store, epic_ref=EPIC, resolve=resolve
        ),
        now=NOW,
    )


def test_a_semantic_conflict_in_an_untouched_file_holds_the_node(
    tmp_path: Path,
) -> None:
    """The merge-main step runs ``kew resolve`` on the clean merge; the
    whole-tree check fails on ``caller.py``, which the integration never
    touched, so nothing is pushed and the node holds naming the check and
    its first failing line."""
    worktree, pr_head = _helper_removed_on_main(tmp_path)
    assert touched_paths(worktree, since=pr_head) == ["pkg/util.py"]
    # A check over the changed files alone passes the broken tree.
    run_resolution_checks(worktree, _config(python=_MYPY), since=pr_head)

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        _forge(worktree, pr_head) as push,
    ):
        _drive(store, tmp_path, pr_head, _config(python=_WHOLE_TREE_MYPY))
        holds = store.list_epic_holds(EPIC)

    assert push.published == []
    [hold] = holds
    assert hold.item_id == ITEM
    assert hold.reason.startswith(INTEGRATION_FAILED_HOLD)
    assert "[resolve] python_checks, exit 1" in hold.reason
    assert _HELPER_GONE in hold.reason


def test_a_file_main_deleted_still_runs_its_kinds_whole_tree_check(
    tmp_path: Path,
) -> None:
    """Main deletes ``pkg/util.py`` while the branch adds a caller of it: no
    surviving Python file changed, but the deletion is a Python change."""
    worktree, pr_head = _util_deleted_on_main(tmp_path)
    assert touched_paths(worktree, since=pr_head) == ["pkg/NOTES.md"]
    assert deleted_paths(worktree, since=pr_head) == ["pkg/util.py"]

    exit_code, push = _resolve(worktree, pr_head, _config(python=_WHOLE_TREE_MYPY))

    assert exit_code == 1
    assert push.published == []


def _util_deleted_on_main(tmp_path: Path) -> tuple[Path, str]:
    """Main deletes ``pkg/util.py``; the branch adds a caller of it."""
    return _merged(
        tmp_path,
        base={
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper() -> int:\n    return 1\n",
        },
        branch={"pkg/caller.py": "from pkg.util import helper\n\nV = helper()\n"},
        main={"pkg/NOTES.md": "util is gone\n"},
        main_deletes=("pkg/util.py",),
    )


def test_a_paths_check_with_only_deletions_of_its_kind_warns_it_was_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``{paths}`` check has no surviving file to name when its kind only
    lost files, so it does not run; the push says so, naming the key and the
    deleted paths, rather than passing the change silently."""
    worktree, pr_head = _util_deleted_on_main(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kew.resolve_checks"):
        exit_code, push = _resolve(worktree, pr_head, _config(python=_MYPY))

    assert exit_code == 0
    assert push.published == [worktree]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert "[resolve] python_checks names {paths}" in message
    assert "pkg/util.py" in message
    assert "pushed unchecked" in message


def test_a_conflict_resolution_its_check_refuses_holds_as_a_conflict_naming_it(
    tmp_path: Path,
) -> None:
    """The agent resolved the conflict, but the resolved tree brings
    ``atRoute`` back: the hold is ``base_integration_conflict``, and its
    reason names the check and the line it failed on."""
    worktree, pr_head = _at_route(tmp_path)
    config = _config(frontend=_FRONTEND)
    conflict = ConflictInfo(
        strategy_used="merge",
        conflicted_files=["frontend/src/controller.ts"],
        base_branch="main",
        base_sha=_git(worktree, "rev-parse", "origin/main"),
    )

    def resolve(
        *, on_conflict: Callable[[ConflictInfo], None], **_kwargs: Any
    ) -> CommandOutcome:
        """``kew resolve``: the agent run finished; kew checks its tree."""
        on_conflict(conflict)
        return _finish_conflict_resolution(
            result=DispatchResult(
                exit_code=0, issue_num=int(ITEM), log_file=tmp_path / "run.log"
            ),
            containment=None,
            host_publication=True,
            publication_mode="canonical",
            worktree_dir=worktree,
            conflict_info=conflict,
            pr_number=PR,
            issue_number=int(ITEM),
            branch_name=BRANCH,
            base_branch="main",
            expected_head_sha=pr_head,
            config=config,
            store=None,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, tmp_path, pr_head, config, resolve=resolve)
        [hold] = store.list_epic_holds(EPIC)

    assert hold.reason.startswith(INTEGRATION_CONFLICT_HOLD)
    assert "frontend/src/controller.ts" in hold.reason
    assert "[resolve] frontend_checks, exit 1" in hold.reason
    assert "has no exported member 'atRoute'" in hold.reason


def test_a_resolution_with_no_check_configured_warns_with_the_key_to_set(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    worktree, pr_head = _broken_python(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kew.resolve_checks"):
        exit_code, push = _resolve(worktree, pr_head, _config())

    assert exit_code == 0
    assert push.published == [worktree]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "pushed unchecked" in warning.getMessage()
    assert "set [resolve] python_checks" in warning.getMessage()


#: ``npm run check`` as it failed on 2026-11-06 (#3495): one ``Error:`` block
#: between screens of ``state_referenced_locally`` warnings, then the summary.
#: The error line carries a credential, which the hold must not.
_NOISY_CHECK = """\
import sys
tree = "/tmp/kew-check/tree/frontend/src"
def warn(line):
    print(f"{tree}/routes/scorecard/+page.svelte:{line}:5")
    print("Warn: This reference only captures the initial value of `data`.")
    print("https://svelte.dev/e/state_referenced_locally (svelte)")
print("Loading svelte-check in workspace: /tmp/kew-check/tree/frontend")
for line in range(1, 41):
    warn(line)
print(f"{tree}/lib/fixtures.test.ts:120:7")
print("Error: Object literal may only specify known properties, and 'needsYou' "
      "does not exist in type 'Fixture'. token=ghp_" + "A" * 36 + " (ts)")
for line in range(41, 81):
    warn(line)
print("svelte-check found 1 error and 80 warnings in 2 files")
sys.exit(1)
"""


def _conflict(worktree: Path) -> ConflictInfo:
    return ConflictInfo(
        strategy_used="merge",
        conflicted_files=["frontend/src/panel.ts"],
        base_branch="main",
        base_sha=_git(worktree, "rev-parse", "origin/main"),
    )


def _hold_after_conflict_resolution(
    tmp_path: Path, worktree: Path, pr_head: str, config: DispatchConfig
) -> str:
    """The hold reason a conflict resolution its check refused leaves."""
    conflict = _conflict(worktree)

    def resolve(
        *, on_conflict: Callable[[ConflictInfo], None], **_kwargs: Any
    ) -> CommandOutcome:
        on_conflict(conflict)
        return _finish_conflict_resolution(
            result=DispatchResult(
                exit_code=0, issue_num=int(ITEM), log_file=tmp_path / "run.log"
            ),
            containment=None,
            host_publication=True,
            publication_mode="canonical",
            worktree_dir=worktree,
            conflict_info=conflict,
            pr_number=PR,
            issue_number=int(ITEM),
            branch_name=BRANCH,
            base_branch="main",
            expected_head_sha=pr_head,
            config=config,
            store=None,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(store, tmp_path, pr_head, config, resolve=resolve)
        [hold] = store.list_epic_holds(EPIC)
    return hold.reason


def test_a_failed_frontend_check_keeps_its_error_lines_in_the_hold(
    tmp_path: Path, _tools: Path
) -> None:
    """The tail of a red ``npm run check`` is warnings and the summary: the
    hold quotes the error block wherever it falls, redacted, and the run's
    resolution evidence keeps the whole output."""
    (_tools / "noisy-check").write_text(_NOISY_CHECK)
    worktree, pr_head = _at_route(tmp_path)
    config = _config(frontend='"$PYTHON" "$STUBS/noisy-check"')

    reason = _hold_after_conflict_resolution(tmp_path, worktree, pr_head, config)

    assert reason.startswith(INTEGRATION_CONFLICT_HOLD)
    assert "[resolve] frontend_checks, exit 1" in reason
    assert (
        "frontend/src/lib/fixtures.test.ts:120:7 Error: Object literal may only "
        "specify known properties, and 'needsYou' does not exist" in reason
    )
    assert "ghp_" not in reason
    assert "state_referenced_locally" not in reason
    evidence = json.loads((tmp_path / "run.resolution.json").read_text())
    assert evidence["since"] == pr_head
    assert evidence["check"]["command"] == config.resolve.frontend_checks
    output = evidence["check"]["output"]
    assert "svelte-check found 1 error and 80 warnings in 2 files" in output
    assert "+page.svelte:80:5" in output
    assert "ghp_" not in output
    assert set(evidence) == {"version", "since", "check"}


#: A red check naming a file beneath the checked root whose name is a token.
_TOKEN_PATH_CHECK = """\
import sys
print("/tmp/kew-check/tree/frontend/ghp_" + "A1b2" * 9 + ".ts:1:1")
print("Error: bad")
sys.exit(1)
"""


def test_a_token_named_as_a_checked_file_reaches_neither_hold_nor_evidence(
    tmp_path: Path, _tools: Path
) -> None:
    """A file beneath the checked root named for a GitHub token is quoted from
    the repository root with the token redacted, in the hold reason and in
    the run's resolution evidence alike."""
    (_tools / "token-path-check").write_text(_TOKEN_PATH_CHECK)
    worktree, pr_head = _at_route(tmp_path)
    config = _config(frontend='"$PYTHON" "$STUBS/token-path-check"')

    reason = _hold_after_conflict_resolution(tmp_path, worktree, pr_head, config)

    assert "frontend/<redacted>.ts:1:1 Error: bad" in reason
    assert "ghp_" not in reason
    evidence = json.loads((tmp_path / "run.resolution.json").read_text())
    output = evidence["check"]["output"]
    assert output.splitlines()[:2] == ["frontend/<redacted>.ts:1:1", "Error: bad"]
    assert "ghp_" not in output


def _conflict_resolved_with_a_leftover(tmp_path: Path) -> tuple[Path, str]:
    """Both sides added an item at the same place; the resolver kept both and
    left its conflicted copy in an ignored ``build/`` directory. The merge is
    not committed yet: host publication commits it."""
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _commit(
        repo,
        {
            "frontend/.gitignore": "build/\n",
            "frontend/src/list.ts": "export const items = [\n  'a',\n];\n",
        },
        "base",
    )
    _git(repo, "checkout", "-q", "-b", BRANCH)
    pr_head = _commit(
        repo,
        {"frontend/src/list.ts": "export const items = [\n  'a',\n  'b',\n];\n"},
        "b",
    )
    _git(repo, "checkout", "-q", "main")
    base_sha = _commit(
        repo,
        {"frontend/src/list.ts": "export const items = [\n  'a',\n  'c',\n];\n"},
        "c",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)
    _git(repo, "checkout", "-q", BRANCH)
    conflicted = run(["git", "merge", "-q", "--no-edit", "main"], cwd=repo, check=False)
    assert conflicted.returncode != 0
    listed = repo / "frontend/src/list.ts"
    leftover = repo / "frontend/build/list.ts"
    leftover.parent.mkdir()
    leftover.write_text(listed.read_text())
    assert "<<<<<<<" in leftover.read_text()
    listed.write_text("export const items = [\n  'a',\n  'b',\n  'c',\n];\n")
    _git(repo, "add", "frontend/src/list.ts")
    return repo, pr_head


def test_the_frontend_check_runs_on_the_resolved_tree(tmp_path: Path) -> None:
    """The check runs in the resolved checkout itself: it sees the resolver's
    edit (both added items, which no commit holds yet), and, as a check the
    operator runs in that checkout would, the ignored leftover beside it,
    which publication drops. kew copies nothing out of the checkout for the
    check and leaves the leftover where the resolver put it; the merge verify
    checks the pushed tree."""
    worktree, pr_head = _conflict_resolved_with_a_leftover(tmp_path)
    ran_in = tmp_path / "ran-in"
    check = (
        "grep -q \"'b',\" frontend/src/list.ts && grep -q \"'c',\" frontend/src/list.ts"
        f" && grep -q '<<<<<<<' frontend/build/list.ts && pwd -P > '{ran_in}'"
    )

    run_resolution_checks(worktree, _config(frontend=check), since=pr_head)

    assert ran_in.read_text().strip() == str(worktree.resolve())
    assert "<<<<<<<" in (worktree / "frontend/build/list.ts").read_text()
    assert list(tmp_path.glob(".kew-resolve-check-*")) == []


def test_a_refused_resolutions_evidence_runs_nothing_the_checkout_configures(
    tmp_path: Path, _tools: Path
) -> None:
    """The checkout is agent-written: its ``.git/config`` can name an external
    diff or a textconv driver. Keeping a refused resolution's evidence runs no
    git in it, so neither runs on the host."""
    (_tools / "noisy-check").write_text(_NOISY_CHECK)
    worktree, pr_head = _at_route(tmp_path)
    marker = tmp_path / "ran-on-host"
    (_tools / "probe").write_text(f'touch "{marker}"\n')
    _git(worktree, "config", "diff.external", f'sh "{_tools / "probe"}"')
    _git(worktree, "config", "diff.probe.textconv", f'sh "{_tools / "probe"}"')
    (worktree / ".git/info/attributes").write_text("* diff=probe\n")
    config = _config(frontend='"$PYTHON" "$STUBS/noisy-check"')

    reason = _hold_after_conflict_resolution(tmp_path, worktree, pr_head, config)

    assert "[resolve] frontend_checks, exit 1" in reason
    assert (tmp_path / "run.resolution.json").exists()
    assert not marker.exists()


def test_a_checked_path_is_named_from_the_repository_root_once_redacted() -> None:
    """The check's root is stripped after redaction and only off a plain
    path: a route group keeps its name, and a traversal out of the root is
    still reduced to its basename rather than read back as a home path."""
    output = "\n".join(
        [
            "/tmp/kew-check/tree/frontend/src/routes/(factory)/work/+page.svelte:3:1",
            "Error: 'x' is not defined token=ghp_" + "A" * 36,
            "/work/../home/operator/.ssh/id_ed25519:1:1",
            "/tmp/kew-check/tree/%2e%2e/home/operator/notes.txt",
            "see /work/frontend/src/lib/api.ts and /home/operator/kew/x.ts",
        ]
    )

    assert readable_output(output, "/tmp/kew-check/tree/", "/work/").splitlines() == [
        "frontend/src/routes/(factory)/work/+page.svelte:3:1",
        "Error: 'x' is not defined token=<redacted>",
        "<path>/id_ed25519:1:1",
        "<path>/notes.txt",
        "see frontend/src/lib/api.ts and <path>/x.ts",
    ]


def test_a_token_in_a_checked_path_is_redacted_once_the_root_is_stripped() -> None:
    """A GitHub token named as a file beneath the checked root is redacted,
    the same as it would be in that path printed without the root."""
    token = "ghp_" + "A1b2" * 9
    output = f"/tmp/kew-check/tree/frontend/{token}.ts:1:1\nError: bad"

    kept = readable_output(output, "/tmp/kew-check/tree/")

    assert token not in kept
    assert kept.splitlines() == ["frontend/<redacted>.ts:1:1", "Error: bad"]


@pytest.mark.parametrize(
    "joined",
    [
        "/work/a.ts:/Users/u/.ssh/k",
        "/work/a.ts,/Users/u/.ssh/k",
        "/work/a.ts(/Users/u/.ssh/k",
        "/work/a.ts:3:1:/Users/u/.ssh/k",
        "/work/x:/home/runner/.config/kew/agent-token.env",
        "/work/frontend/a.ts[/Users/u/.ssh/k",
        "/work/frontend/a.ts]/Users/u/.ssh/k",
        "/work/frontend/a.ts)/Users/u/.ssh/k",
        "/work/frontend/a.ts@/Users/u/.ssh/k",
        "/work/frontend/a.ts+/Users/u/.ssh/k",
        "/work/frontend/a.ts-/Users/u/.ssh/k",
        "/work/frontend/a.ts:3:1/Users/u/.ssh/k",
    ],
)
def test_a_host_path_joined_to_a_checked_path_is_still_sanitized(joined: str) -> None:
    """Any character that joins another absolute path to a tree path, not
    only ``:`` ``,`` or ``(``, ends the kept tree path: the host path after
    it is reduced like any other, never kept as part of a repository-relative
    name."""
    kept = readable_output(joined, "/work/")

    assert "/Users/" not in kept
    assert "/home/" not in kept
    assert ".ssh" not in kept and ".config" not in kept
    assert kept.startswith(("a.ts", "x", "frontend/a.ts"))
    assert kept.endswith("<path>/k") or kept.endswith("agent-token.env")


def test_route_group_directories_stay_part_of_the_kept_path() -> None:
    """Bracketed and parenthesized route directories are tree path segments,
    not joiners: the whole route is kept from the repository root."""
    route = "frontend/src/routes/(factory)/[id]/[[lang]]/[...rest]/+page.svelte"

    kept = readable_output(f"/work/{route}:4:2", "/work/")

    assert kept == f"{route}:4:2"
