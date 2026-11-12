"""The acceptance author writes red-at-base tests before the implementer (#2728)."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.held_out_lock import pinned_lock
from support.pytest_here import run_pytest_here

from kew.acceptance_author import (
    AcceptanceAuthorRefused,
    AuthorRequest,
    author_acceptance_contract,
    author_model,
    contract_checks,
    prepare_acceptance_contract,
)
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS
from kew.triage.plan import extract_plan, plan_sha
from kew.triage.schema import AcceptanceCriterion, ItemPlan

ISSUE = 7
CONTRACT = f"kew/tests/acceptance/test_issue_{ISSUE}.py"

RED_TESTS = """\
from pathlib import Path


def test_ac1():
    assert "VALUE = 2" in Path("kew/feature.py").read_text()


def test_ac3():
    assert "NAME" in Path("kew/feature.py").read_text()
"""

ONE_GREEN_TEST = RED_TESTS.replace('"NAME" in', '"VALUE" in')


def _plan(*kinds: str) -> ItemPlan:
    return ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(id=f"AC{k}", statement=f"criterion {k}", kind=kind)  # type: ignore[arg-type]
            for k, kind in enumerate(kinds, start=1)
        ]
    )


TWO_TEST_PLAN = _plan("test", "behavior", "test")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _file_text(path: Path) -> str:
    """Return the text of a data file (a lock, a ``pyproject.toml``)."""
    return path.read_text()


def _contract_text(checkout: Path) -> str:
    """Return the acceptance contract text written into *checkout*."""
    return (checkout / CONTRACT).read_text()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A node checkout on ``agent/issue-7`` at its base commit."""
    git = shutil.which("git")
    assert git is not None
    TOOL_PATHS["git"] = git
    repo = tmp_path / "checkout"
    (repo / "kew").mkdir(parents=True)
    (repo / "kew" / "feature.py").write_text("VALUE = 1\n")
    _git(repo, "init", "-q", "-b", f"agent/issue-{ISSUE}")
    _git(repo, "config", "user.email", "kew@example.invalid")
    _git(repo, "config", "user.name", "kew")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "runs.db")) as opened:
        yield opened


class FakeAuthor:
    """An author session: writes the given files into its checkout."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.requests: list[AuthorRequest] = []

    def __call__(self, request: AuthorRequest) -> str:
        self.requests.append(request)
        for rel, text in self.files.items():
            target = request.worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return f"author-run-{len(self.requests)}"


def _author(
    store: Any, checkout: Path, author: FakeAuthor, plan: ItemPlan | None, **kw: Any
) -> Any:
    return author_acceptance_contract(
        store=store,
        epic_ref="epic-1",
        item_id=str(ISSUE),
        issue_num=ISSUE,
        issue_body="## Acceptance\n- something",
        plan=plan,
        worktree=checkout,
        implementer_model=kw.pop("implementer_model", "gpt-5.6-sol"),
        routed_models=kw.pop("routed_models", ["gpt-5.6-sol", "claude-opus-5-5"]),
        launch=author,
        run_test=kw.pop("run_test", run_pytest_here),
        **kw,
    )


def _prepare_contract(
    store: Any,
    checkout: Path,
    author: FakeAuthor,
    plan: ItemPlan | None = TWO_TEST_PLAN,
    *,
    contained: bool = False,
) -> Any:
    return prepare_acceptance_contract(
        store=store,
        epic_ref="epic-1",
        item_id=str(ISSUE),
        issue_num=ISSUE,
        issue_body="## Acceptance\n- something",
        plan=plan,
        worktree=checkout,
        implementer_model="gpt-5.6-sol",
        routed_models=["gpt-5.6-sol", "claude-opus-5-5"],
        contained=contained,
        launch=author,
        run_test=run_pytest_here,
    )


def test_two_test_criteria_get_an_author_run_and_a_contract_with_its_digest(
    checkout: Path, store: Any
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    author = FakeAuthor({CONTRACT: RED_TESTS})

    row = _author(store, checkout, author, TWO_TEST_PLAN)

    assert len(author.requests) == 1
    assert "`test_ac1`" in author.requests[0].prompt
    assert "`test_ac3`" in author.requests[0].prompt
    assert "test_ac2" not in author.requests[0].prompt
    assert row.path == CONTRACT
    assert row.sha256 == hashlib.sha256(RED_TESTS.encode()).hexdigest()
    assert row.author_run_id == "author-run-1"
    # The contract is the branch's first commit, and carries only the file.
    assert row.commit == _git(checkout, "rev-parse", "HEAD")
    assert _git(checkout, "rev-parse", "HEAD^") == base
    assert _git(checkout, "show", "--name-only", "--format=", "HEAD") == CONTRACT
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == row


def test_a_recorded_contracts_committed_blob_hashes_to_the_ledger_digest(
    checkout: Path, store: Any
) -> None:
    row = _author(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}), TWO_TEST_PLAN)

    blob = subprocess.run(
        ["git", "show", f"{row.commit}:{row.path}"],
        cwd=checkout,
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(blob).hexdigest() == row.sha256


def test_a_clean_filter_does_not_change_the_committed_contract(
    checkout: Path, store: Any
) -> None:
    """The commit is built from the judged bytes, so a clean filter, ``ident``
    or ``eol`` attribute in the checkout cannot rewrite them on the way in."""
    (checkout / ".gitattributes").write_text(
        "kew/tests/acceptance/*.py filter=mangle ident eol=crlf\n"
    )
    _git(checkout, "config", "filter.mangle.clean", "cat; echo '# mangled'")
    _git(checkout, "add", ".gitattributes")
    _git(checkout, "commit", "-q", "-m", "attributes")

    row = _author(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}), TWO_TEST_PLAN)

    blob = subprocess.run(
        ["git", "show", f"{row.commit}:{row.path}"],
        cwd=checkout,
        check=True,
        capture_output=True,
    ).stdout
    assert blob == RED_TESTS.encode()
    assert row.sha256 == hashlib.sha256(blob).hexdigest()
    assert _git(checkout, "ls-tree", row.commit, "--", CONTRACT).startswith("100644 ")


SELF_REWRITING_TESTS = RED_TESTS.replace(
    """def test_ac3():
    assert "NAME" in Path("kew/feature.py").read_text()
""",
    """def test_ac3():
    try:
        assert "NAME" in Path("kew/feature.py").read_text()
    finally:
        Path(__file__).write_text("def test_ac1():\\n    pass\\n")
""",
)


def test_a_contract_test_that_rewrites_its_own_file_is_refused(
    checkout: Path, store: Any
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")

    with pytest.raises(AcceptanceAuthorRefused, match=f"run rewrote {CONTRACT}"):
        _author(
            store, checkout, FakeAuthor({CONTRACT: SELF_REWRITING_TESTS}), TWO_TEST_PLAN
        )

    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def _planting_tests(action: str) -> str:
    """Red tests whose ``test_ac1`` performs *action* before failing."""
    return RED_TESTS.replace(
        "def test_ac1():\n",
        f"def test_ac1():\n    {action}\n",
    )


def _ignore_logs(repo: Path) -> None:
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "kew" / "keep.log").write_text("kept\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore logs")


def _ignore_pycache(repo: Path) -> None:
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore bytecode")


def _skip_worktree_feature(repo: Path) -> None:
    _git(repo, "update-index", "--skip-worktree", "kew/feature.py")


@pytest.mark.parametrize(
    ("setup", "action", "planted"),
    [
        (
            None,
            'Path("kew/src").mkdir(); Path("kew/src/planted.py").write_text("X = 1")',
            "kew/src/planted.py",
        ),
        (
            _ignore_logs,
            'Path("kew/planted.log").write_text("planted")',
            "kew/planted.log",
        ),
        (
            _ignore_logs,
            'Path("kew/keep.log").write_text("rewritten")',
            "kew/keep.log",
        ),
        (
            _ignore_pycache,
            'Path("kew/__pycache__").mkdir(); '
            'Path("kew/__pycache__/feature.cpython-313.pyc").write_bytes(b"pyc")',
            "kew/__pycache__/feature.cpython-313.pyc",
        ),
        (
            _skip_worktree_feature,
            'Path("kew/feature.py").write_text("VALUE = 1  # planted\\n")',
            "kew/feature.py",
        ),
    ],
    ids=[
        "new-file",
        "new-ignored-file",
        "edited-ignored-file",
        "planted-bytecode",
        "skip-worktree",
    ],
)
def test_a_contract_test_that_changes_another_file_is_refused(
    checkout: Path,
    store: Any,
    setup: Callable[[Path], None] | None,
    action: str,
    planted: str,
) -> None:
    if setup is not None:
        setup(checkout)
    base = _git(checkout, "rev-parse", "HEAD")

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(action)}),
            TWO_TEST_PLAN,
        )

    assert planted in str(refused.value)
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


_AMENDING_HOOK = (
    "#!/bin/sh\n"
    "echo planted > kew/planted.py\n"
    "git add kew/planted.py && git commit -q --amend --no-edit --no-verify\n"
)


def test_a_contract_test_that_plants_a_git_hook_is_refused(
    checkout: Path, store: Any
) -> None:
    """``--no-verify`` does not skip post-commit, which could amend the commit."""
    base = _git(checkout, "rev-parse", "HEAD")
    action = (
        "hook = Path('.git/hooks/post-commit'); "
        f"hook.write_text({_AMENDING_HOOK!r}); hook.chmod(0o755)"
    )

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(action)}),
            TWO_TEST_PLAN,
        )

    assert "hooks/post-commit" in str(refused.value)
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_contract_test_that_symlinks_a_git_directory_is_refused(
    checkout: Path, store: Any, tmp_path: Path
) -> None:
    """A symlinked directory under ``.git`` is not descended into, so the link
    itself is the change: here an outside replace ref would redirect the base."""
    outside = tmp_path / "outside-replace"
    outside.mkdir()
    base = _git(checkout, "rev-parse", "HEAD")
    action = f"Path('.git/refs/replace').symlink_to({str(outside)!r})"

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(action)}),
            TWO_TEST_PLAN,
        )

    assert "refs/replace" in str(refused.value)
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


#: Stages a planted file and points a replace ref for the base commit at a
#: commit whose tree already holds it, so through the replacement the index
#: matches HEAD and the staged file reads as clean.
_REPLACE_BASE = (
    "echo planted > kew/planted.py && git add kew/planted.py && "
    'git replace HEAD "$(git commit-tree "$(git write-tree)" -m base)"'
)


class ReplacingAuthor(FakeAuthor):
    """An author that also runs *script* in its checkout after writing."""

    def __init__(self, files: dict[str, str], script: str) -> None:
        super().__init__(files)
        self.script = script

    def __call__(self, request: AuthorRequest) -> str:
        run_id = super().__call__(request)
        subprocess.run(["sh", "-c", self.script], cwd=request.worktree, check=True)
        return run_id


def test_an_author_that_replaces_the_base_to_hide_a_staged_file_is_refused(
    checkout: Path, store: Any
) -> None:
    """The check reads the real base, so the staged file is a change."""
    base = _git(checkout, "rev-parse", "HEAD")
    author = ReplacingAuthor({CONTRACT: RED_TESTS}, _REPLACE_BASE)

    with pytest.raises(AcceptanceAuthorRefused, match="only") as refused:
        _author(store, checkout, author, TWO_TEST_PLAN)

    assert "kew/planted.py" in str(refused.value)
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_an_author_that_links_a_git_directory_out_is_refused(
    checkout: Path, store: Any, tmp_path: Path
) -> None:
    """The baseline is taken before the author launches, so a link it plants
    under ``.git`` is refused before any red-at-base run writes behind it."""
    outside = tmp_path / "outside-replace"
    outside.mkdir()
    base = _git(checkout, "rev-parse", "HEAD")
    author = ReplacingAuthor(
        {CONTRACT: RED_TESTS}, f"ln -s '{outside}' .git/refs/replace"
    )

    with pytest.raises(AcceptanceAuthorRefused, match="only") as refused:
        _author(store, checkout, author, TWO_TEST_PLAN)

    assert ".git/refs/replace" in str(refused.value)
    assert not (checkout / ".git" / "refs" / "replace").is_symlink()
    assert outside.is_dir()
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


@pytest.mark.parametrize(
    ("script", "planted"),
    [
        ("echo pwn > kew/pwn.log", "kew/pwn.log"),
        (
            "git config filter.plant.clean cat && "
            "echo '* filter=plant' > .git/info/attributes",
            ".git/info/attributes",
        ),
        (
            "echo '#!/bin/sh' > .git/hooks/post-commit && "
            "chmod +x .git/hooks/post-commit",
            ".git/hooks/post-commit",
        ),
    ],
    ids=["ignored-file", "git-attributes", "git-hook"],
)
def test_an_author_that_plants_an_ignored_or_git_file_is_refused(
    checkout: Path, store: Any, script: str, planted: str
) -> None:
    """What the author plants is not blessed as the red-at-base baseline, and
    the refusal removes it along with the contract file."""
    _ignore_logs(checkout)
    base = _git(checkout, "rev-parse", "HEAD")
    author = ReplacingAuthor({CONTRACT: RED_TESTS}, script)

    with pytest.raises(AcceptanceAuthorRefused, match="only") as refused:
        _author(store, checkout, author, TWO_TEST_PLAN)

    assert planted in str(refused.value)
    assert not (checkout / planted).exists()
    assert not (checkout / CONTRACT).exists()
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def _checkout_listing(repo: Path) -> tuple[str, str, str]:
    """What git sees of *repo*'s checkout, ignored files and flags included."""
    return (
        _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all"),
        _git(repo, "ls-files", "-v", "-s"),
        _git(repo, "rev-parse", "HEAD"),
    )


@pytest.mark.parametrize(
    ("setup", "tests"),
    [
        (
            None,
            ONE_GREEN_TEST.replace(
                "def test_ac3():\n",
                "def test_ac3():\n    Path('kew/planted.py').write_text('X = 1')\n",
            ),
        ),
        (
            None,
            _planting_tests(
                "Path('kew/src').mkdir(); Path('kew/src/planted.py').write_text('X'); "
                "__import__('subprocess').run(['git', 'add', 'kew/src'], check=True)"
            ),
        ),
        (None, _planting_tests("Path('kew/feature.py').write_text('VALUE = 2')")),
        (
            _skip_worktree_feature,
            _planting_tests("Path('kew/feature.py').write_text('VALUE = 9')"),
        ),
        (_ignore_logs, _planting_tests("Path('kew/new.log').write_text('x')")),
    ],
    ids=[
        "passing-test-plants-a-file",
        "staged-new-file",
        "tracked-file-edited",
        "skip-worktree-edited",
        "ignored-file-created",
    ],
)
def test_a_refused_red_at_base_run_leaves_the_checkout_as_it_was_found(
    checkout: Path,
    store: Any,
    setup: Callable[[Path], None] | None,
    tests: str,
) -> None:
    if setup is not None:
        setup(checkout)
    found = _checkout_listing(checkout)

    with pytest.raises(AcceptanceAuthorRefused):
        _author(store, checkout, FakeAuthor({CONTRACT: tests}), TWO_TEST_PLAN)

    assert _checkout_listing(checkout) == found
    assert _git(checkout, "hash-object", "kew/feature.py") == _git(
        checkout, "rev-parse", "HEAD:kew/feature.py"
    )
    assert not (checkout / "kew" / "tests").exists()
    assert not (checkout / "kew" / "src").exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


#: Stages another blob for the skip-worktree ``kew/feature.py`` and puts the
#: flag back, so the worktree file and its flag read as they were.
_POISON_HIDDEN_INDEX = (
    "import subprocess as s; "
    "b = s.run(['git', 'hash-object', '-w', '--stdin'], input=b'VALUE = 2\\n', "
    "capture_output=True, check=True).stdout.decode().strip(); "
    "s.run(['git', 'update-index', '--cacheinfo', f'100644,{b},kew/feature.py'], "
    "check=True); "
    "s.run(['git', 'update-index', '--skip-worktree', 'kew/feature.py'], check=True)"
)


def test_an_index_only_change_under_skip_worktree_is_refused_and_restored(
    checkout: Path, store: Any
) -> None:
    _skip_worktree_feature(checkout)
    found = _checkout_listing(checkout)

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(_POISON_HIDDEN_INDEX)}),
            TWO_TEST_PLAN,
        )

    assert "kew/feature.py" in str(refused.value)
    assert _checkout_listing(checkout) == found
    assert _git(checkout, "rev-parse", ":kew/feature.py") == _git(
        checkout, "rev-parse", "HEAD:kew/feature.py"
    )
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_contract_test_that_plants_object_alternates_is_refused_and_removed(
    checkout: Path, store: Any, tmp_path: Path
) -> None:
    """Only the object payloads are inert: ``objects/info/alternates`` would
    make the checkout read objects from another repository."""
    elsewhere = tmp_path / "elsewhere-objects"
    elsewhere.mkdir()
    alternates = checkout / ".git" / "objects" / "info" / "alternates"
    action = (
        "Path('.git/objects/info').mkdir(exist_ok=True); "
        f"Path('.git/objects/info/alternates').write_text({str(elsewhere)!r})"
    )

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(action)}),
            TWO_TEST_PLAN,
        )

    assert "objects/info/alternates" in str(refused.value)
    assert not alternates.exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_contract_test_that_changes_its_own_mode_is_refused(
    checkout: Path, store: Any
) -> None:
    with pytest.raises(AcceptanceAuthorRefused, match=f"run rewrote {CONTRACT}"):
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests("Path(__file__).chmod(0o755)")}),
            TWO_TEST_PLAN,
        )

    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_contract_test_that_changes_an_ignored_files_mode_is_refused(
    checkout: Path, store: Any
) -> None:
    _ignore_logs(checkout)

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor(
                {CONTRACT: _planting_tests("Path('kew/keep.log').chmod(0o755)")}
            ),
            TWO_TEST_PLAN,
        )

    assert "kew/keep.log" in str(refused.value)


def _planting_git(tmp_path: Path, git: str) -> Path:
    """A git boundary that plants ``kew/planted.py`` on its first ``update-ref``."""
    marker = tmp_path / "planted-once"
    executable = tmp_path / "planting-git"
    executable.write_text(
        f"""#!{sys.executable}
import os
import sys
from pathlib import Path

marker = Path({str(marker)!r})
if "update-ref" in sys.argv[1:] and not marker.exists():
    marker.touch()
    Path("kew/planted.py").write_text("X = 1")
os.execv({git!r}, [{git!r}, *sys.argv[1:]])
"""
    )
    executable.chmod(0o755)
    return executable


def test_a_change_made_while_committing_is_refused_and_the_commit_undone(
    checkout: Path, store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final comparison runs after the commit: what changed then is a
    refusal, and the rejected commit does not stay at ``HEAD``."""
    found = _checkout_listing(checkout)
    planting = _planting_git(tmp_path, TOOL_PATHS["git"])
    monkeypatch.setitem(TOOL_PATHS, "git", str(planting))

    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}), TWO_TEST_PLAN)

    assert "kew/planted.py" in str(refused.value)
    assert _checkout_listing(checkout) == found
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_hook_already_in_the_checkout_does_not_run_on_the_contract_commit(
    checkout: Path, store: Any
) -> None:
    hook = checkout / ".git" / "hooks" / "post-commit"
    hook.write_text(_AMENDING_HOOK)
    hook.chmod(0o755)
    base = _git(checkout, "rev-parse", "HEAD")

    row = _author(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}), TWO_TEST_PLAN)

    assert _git(checkout, "rev-parse", "HEAD^") == base
    assert _git(checkout, "show", "--name-only", "--format=", row.commit) == CONTRACT
    assert not (checkout / "kew" / "planted.py").exists()


def test_each_test_criterion_check_names_its_contract_test() -> None:
    checked = contract_checks(TWO_TEST_PLAN, ISSUE)

    assert [c.check for c in checked.acceptance_criteria] == [
        f"pytest {CONTRACT}::test_ac1",
        None,
        f"pytest {CONTRACT}::test_ac3",
    ]


def test_an_author_test_that_passes_at_the_base_is_refused(
    checkout: Path, store: Any
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")

    with pytest.raises(AcceptanceAuthorRefused, match="test_ac3 passes at the base"):
        _author(store, checkout, FakeAuthor({CONTRACT: ONE_GREEN_TEST}), TWO_TEST_PLAN)

    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None
    assert _git(checkout, "rev-parse", "HEAD") == base


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({CONTRACT: "def test_ac1(:\n"}, "does not parse"),
        ({CONTRACT: "import missing_module\n" + RED_TESTS}, "did not run to a failure"),
        ({CONTRACT: RED_TESTS.replace("test_ac3", "test_other")}, "must define"),
        ({CONTRACT: RED_TESTS, "kew/feature.py": "VALUE = 2\n"}, "only"),
        ({}, "did not write"),
    ],
)
def test_an_author_run_without_a_red_contract_records_nothing(
    checkout: Path, store: Any, files: dict[str, str], reason: str
) -> None:
    with pytest.raises(AcceptanceAuthorRefused, match=reason):
        _author(store, checkout, FakeAuthor(files), TWO_TEST_PLAN)

    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


@pytest.mark.parametrize(
    ("implementer", "author_family"),
    [("gpt-5.6-sol", "claude-opus"), ("claude-opus-5-5", "gpt-5.6-sol")],
)
def test_the_author_is_routed_to_the_other_family(
    checkout: Path, store: Any, implementer: str, author_family: str
) -> None:
    author = FakeAuthor({CONTRACT: RED_TESTS})

    _author(store, checkout, author, TWO_TEST_PLAN, implementer_model=implementer)

    assert author.requests[0].model.startswith(author_family)


def test_a_one_family_routing_authors_on_the_implementers_model() -> None:
    assert author_model("gpt-5.6-sol", ["gpt-5.6-sol", "gpt-5.5"]) == "gpt-5.6-sol"
    assert author_model("claude-opus-5-5", ["claude-sonnet-5-5"]) == "claude-opus-5-5"


@pytest.mark.parametrize("plan", [None, _plan(), _plan("behavior", "static")])
def test_no_test_criterion_means_no_author_run(
    checkout: Path, store: Any, plan: ItemPlan | None
) -> None:
    author = FakeAuthor({CONTRACT: RED_TESTS})

    assert _author(store, checkout, author, plan) is None

    assert author.requests == []
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


# --- Contract preparation: author or restore before the implementer ---------


def test_preparing_a_contract_runs_the_author_and_commits_before_returning(
    store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    author = FakeAuthor({CONTRACT: RED_TESTS})

    contract = _prepare_contract(store, checkout, author)

    assert len(author.requests) == 1
    assert author.requests[0].model == "claude-opus-5-5"
    assert contract.commit == _git(checkout, "rev-parse", "HEAD")
    assert _git(checkout, "rev-parse", "HEAD^") == base
    assert contract.sha256 == hashlib.sha256(RED_TESTS.encode()).hexdigest()


@pytest.mark.parametrize("contained", [False, True])
def test_preparing_without_a_test_criterion_runs_no_author(
    store: Any, checkout: Path, contained: bool
) -> None:
    author = FakeAuthor({CONTRACT: RED_TESTS})

    assert (
        _prepare_contract(
            store, checkout, author, _plan("behavior"), contained=contained
        )
        is None
    )
    assert author.requests == []
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def _fresh_checkout(
    tmp_path: Path, checkout: Path, base: str, name: str = "fresh"
) -> Path:
    """A new checkout of the node's branch at *base*, as a redispatch gets."""
    fresh = tmp_path / name
    fresh.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(checkout), str(fresh))
    _git(fresh, "checkout", "-q", "-B", f"agent/issue-{ISSUE}", base)
    _git(fresh, "config", "user.email", "kew@example.invalid")
    _git(fresh, "config", "user.name", "kew")
    return fresh


def test_a_redispatch_on_a_fresh_checkout_carries_the_recorded_contract(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    contract = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    assert not (fresh / CONTRACT).exists()
    author = FakeAuthor({})

    restored = _prepare_contract(store, fresh, author)

    assert author.requests == []
    assert restored == contract
    assert _git(fresh, "rev-parse", "HEAD") == contract.commit
    assert _contract_text(fresh) == RED_TESTS
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


RED_TESTS_WITH_AC4 = (
    RED_TESTS
    + """

def test_ac4():
    assert "LABEL" in Path("kew/feature.py").read_text()
"""
)

REWORDED_PLAN = TWO_TEST_PLAN.model_copy(
    update={
        "acceptance_criteria": [
            TWO_TEST_PLAN.acceptance_criteria[0].model_copy(
                update={"statement": "criterion 1, reworded"}
            ),
            *TWO_TEST_PLAN.acceptance_criteria[1:],
        ]
    }
)


def test_a_contract_records_the_plan_and_the_tests_it_was_written_for(
    store: Any, checkout: Path
) -> None:
    contract = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))

    assert contract.plan_sha == plan_sha(TWO_TEST_PLAN)
    assert contract.test_names == ("test_ac1", "test_ac3")
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


def test_a_redispatch_under_the_same_plan_restores_its_contract_binding(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    """The plan is re-read from its JSON on redispatch; the digest still matches."""
    base = _git(checkout, "rev-parse", "HEAD")
    contract = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    reread = ItemPlan.model_validate_json(TWO_TEST_PLAN.model_dump_json())
    author = FakeAuthor({})

    restored = _prepare_contract(store, fresh, author, reread)

    assert author.requests == []
    assert restored == contract
    assert _git(fresh, "rev-parse", "HEAD") == contract.commit


@pytest.mark.parametrize(
    ("new_plan", "new_tests", "names"),
    [
        (REWORDED_PLAN, RED_TESTS, ("test_ac1", "test_ac3")),
        (
            _plan("test", "behavior", "test", "test"),
            RED_TESTS_WITH_AC4,
            ("test_ac1", "test_ac3", "test_ac4"),
        ),
    ],
    ids=["criterion-reworded", "test-criterion-added"],
)
def test_a_contract_for_an_older_plan_is_superseded_by_a_new_author_run(
    tmp_path: Path,
    store: Any,
    checkout: Path,
    new_plan: ItemPlan,
    new_tests: str,
    names: tuple[str, ...],
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    old = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    author = FakeAuthor({CONTRACT: new_tests})

    contract = _prepare_contract(store, fresh, author, new_plan)

    assert len(author.requests) == 1
    assert contract.version == old.version + 1
    assert contract.plan_sha != old.plan_sha
    assert _git(fresh, "rev-parse", "HEAD^") == base
    assert (contract.plan_sha, contract.test_names) == (plan_sha(new_plan), names)
    assert _contract_text(fresh) == new_tests
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


def test_a_contained_redispatch_refuses_a_contract_for_an_older_plan(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    old = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    author = FakeAuthor({})

    with pytest.raises(AcceptanceAuthorRefused, match="no contained launch"):
        _prepare_contract(store, fresh, author, REWORDED_PLAN, contained=True)

    assert author.requests == []
    assert _git(fresh, "rev-parse", "HEAD") == base
    assert not (fresh / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == old


def test_a_contract_recorded_without_a_plan_binding_is_never_restored(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    """A row from before plans were bound (pre-v68) matches no current plan."""
    base = _git(checkout, "rev-parse", "HEAD")
    first = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    store.record_acceptance_contract(
        "epic-1",
        str(ISSUE),
        path=first.path,
        sha256=first.sha256,
        author_run_id=first.author_run_id,
        commit=first.commit,
    )
    unbound = store.current_acceptance_contract("epic-1", str(ISSUE))
    assert unbound.plan_sha is None
    fresh = _fresh_checkout(tmp_path, checkout, base)
    author = FakeAuthor({CONTRACT: RED_TESTS})

    contract = _prepare_contract(store, fresh, author)

    assert len(author.requests) == 1
    assert contract.version == unbound.version + 1
    assert contract.plan_sha == plan_sha(TWO_TEST_PLAN)


def test_a_contained_request_needing_a_contract_refuses_before_any_launch(
    store: Any, checkout: Path
) -> None:
    """The default posture is contained, and the author only launches on the
    host: neither running it uncontained nor skipping it is allowed."""
    base = _git(checkout, "rev-parse", "HEAD")
    author = FakeAuthor({CONTRACT: RED_TESTS})

    with pytest.raises(AcceptanceAuthorRefused, match="no contained launch"):
        _prepare_contract(store, checkout, author, contained=True)

    assert author.requests == []
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert not (checkout / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


def test_a_contained_redispatch_carries_the_recorded_contract(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    contract = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    author = FakeAuthor({})

    restored = _prepare_contract(store, fresh, author, contained=True)

    assert author.requests == []
    assert restored == contract
    assert _git(fresh, "rev-parse", "HEAD") == contract.commit
    assert _contract_text(fresh) == RED_TESTS


def test_a_redispatch_on_a_moved_base_recommits_the_contract_as_authored(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    first = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _fresh_checkout(tmp_path, checkout, base)
    (fresh / "kew" / "other.py").write_text("OTHER = 1\n")
    _git(fresh, "add", ".")
    _git(fresh, "commit", "-q", "-m", "main moved")
    moved = _git(fresh, "rev-parse", "HEAD")
    author = FakeAuthor({})

    contract = _prepare_contract(store, fresh, author)

    assert author.requests == []
    assert contract.commit == _git(fresh, "rev-parse", "HEAD")
    assert _git(fresh, "rev-parse", "HEAD^") == moved
    assert (contract.sha256, contract.author_run_id) == (
        first.sha256,
        first.author_run_id,
    )
    assert _contract_text(fresh) == RED_TESTS
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


def _moved_base(tmp_path: Path, checkout: Path, base: str, feature: str) -> Path:
    """A fresh checkout whose base moved on: main rewrote ``kew/feature.py``."""
    fresh = _fresh_checkout(tmp_path, checkout, base)
    (fresh / "kew" / "feature.py").write_text(feature)
    _git(fresh, "commit", "-q", "-am", "main moved")
    return fresh


def test_a_restore_onto_a_base_that_now_passes_a_contract_test_is_refused(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    first = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _moved_base(tmp_path, checkout, base, "VALUE = 2\n")
    moved = _git(fresh, "rev-parse", "HEAD")
    author = FakeAuthor({})

    with pytest.raises(AcceptanceAuthorRefused, match="test_ac1") as refused:
        _prepare_contract(store, fresh, author)

    assert "test_ac3" not in str(refused.value)
    assert author.requests == []
    assert _git(fresh, "rev-parse", "HEAD") == moved
    assert not (fresh / CONTRACT).exists()
    assert _git(fresh, "status", "--porcelain") == ""
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == first


def test_a_restore_onto_a_base_where_every_contract_test_still_fails_commits_it(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    first = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _moved_base(tmp_path, checkout, base, "VALUE = 3\n")
    moved = _git(fresh, "rev-parse", "HEAD")

    contract = _prepare_contract(store, fresh, FakeAuthor({}))

    assert _git(fresh, "rev-parse", "HEAD^") == moved
    assert contract.commit == _git(fresh, "rev-parse", "HEAD")
    assert contract.version == first.version + 1
    assert _contract_text(fresh) == RED_TESTS
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


REWRITES_ITSELF_ON_A_MOVED_BASE = RED_TESTS.replace(
    """def test_ac3():
    assert "NAME" in Path("kew/feature.py").read_text()
""",
    """def test_ac3():
    feature = Path("kew/feature.py").read_text()
    try:
        assert "NAME" in feature
    finally:
        if "MOVED" in feature:
            Path(__file__).write_text("def test_ac1():\\n    pass\\n")
""",
)


def test_a_restore_whose_contract_test_rewrites_itself_is_refused(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(
        store, checkout, FakeAuthor({CONTRACT: REWRITES_ITSELF_ON_A_MOVED_BASE})
    )
    first = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _moved_base(tmp_path, checkout, base, "VALUE = 3\nMOVED = 1\n")
    moved = _git(fresh, "rev-parse", "HEAD")

    with pytest.raises(AcceptanceAuthorRefused, match=f"run rewrote {CONTRACT}"):
        _prepare_contract(store, fresh, FakeAuthor({}))

    assert _git(fresh, "rev-parse", "HEAD") == moved
    assert not (fresh / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == first


def test_a_contained_restore_onto_a_moved_base_is_refused(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    first = store.current_acceptance_contract("epic-1", str(ISSUE))
    fresh = _moved_base(tmp_path, checkout, base, "VALUE = 3\n")
    moved = _git(fresh, "rev-parse", "HEAD")
    author = FakeAuthor({})

    with pytest.raises(AcceptanceAuthorRefused, match="contained restore"):
        _prepare_contract(store, fresh, author, contained=True)

    assert author.requests == []
    assert _git(fresh, "rev-parse", "HEAD") == moved
    assert not (fresh / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == first


def _moved_base_holding_the_contract(
    tmp_path: Path, store: Any, checkout: Path, feature: str
) -> tuple[Path, str, Any]:
    """A moved base whose checkout already holds the recorded contract's exact
    bytes, uncommitted; returns it, its HEAD and the recorded contract."""
    base = _git(checkout, "rev-parse", "HEAD")
    first = _prepare_contract(store, checkout, FakeAuthor({CONTRACT: RED_TESTS}))
    fresh = _moved_base(tmp_path, checkout, base, feature)
    (fresh / CONTRACT).parent.mkdir(parents=True)
    (fresh / CONTRACT).write_text(RED_TESTS)
    return fresh, _git(fresh, "rev-parse", "HEAD"), first


def test_a_moved_base_restore_proves_a_contract_already_in_the_checkout(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    """The file matching the recorded bytes is not the recorded commit: a base
    that now passes a contract test is refused all the same."""
    fresh, moved, first = _moved_base_holding_the_contract(
        tmp_path, store, checkout, "VALUE = 2\n"
    )

    with pytest.raises(AcceptanceAuthorRefused, match="test_ac1"):
        _prepare_contract(store, fresh, FakeAuthor({}))

    assert _git(fresh, "rev-parse", "HEAD") == moved
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == first


def test_a_moved_base_restore_commits_a_proven_contract_already_in_the_checkout(
    tmp_path: Path, store: Any, checkout: Path
) -> None:
    fresh, moved, first = _moved_base_holding_the_contract(
        tmp_path, store, checkout, "VALUE = 3\n"
    )

    contract = _prepare_contract(store, fresh, FakeAuthor({}))

    assert _git(fresh, "rev-parse", "HEAD^") == moved
    assert contract.commit == _git(fresh, "rev-parse", "HEAD")
    assert contract.version == first.version + 1
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


def test_a_redispatch_whose_contract_commit_is_gone_authors_a_new_one(
    store: Any, checkout: Path
) -> None:
    store.record_acceptance_contract(
        "epic-1",
        str(ISSUE),
        path=CONTRACT,
        sha256=hashlib.sha256(RED_TESTS.encode()).hexdigest(),
        author_run_id="lost-author",
        commit="0" * 40,
    )
    author = FakeAuthor({CONTRACT: RED_TESTS})

    contract = _prepare_contract(store, checkout, author)

    assert len(author.requests) == 1
    assert _git(checkout, "rev-parse", "HEAD") == contract.commit
    assert contract.version == 2
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == contract


class BudgetSpent(RuntimeError):
    """The admission budget gate's refusal."""


def test_an_author_launch_refusal_records_nothing(store: Any, checkout: Path) -> None:
    base = _git(checkout, "rev-parse", "HEAD")

    def spent(_request: AuthorRequest) -> str:
        raise BudgetSpent("daily budget spent")

    with pytest.raises(BudgetSpent):
        prepare_acceptance_contract(
            store=store,
            epic_ref="epic-1",
            item_id=str(ISSUE),
            issue_num=ISSUE,
            issue_body="## Acceptance\n- something",
            plan=TWO_TEST_PLAN,
            worktree=checkout,
            implementer_model="gpt-5.6-sol",
            routed_models=["gpt-5.6-sol", "claude-opus-5-5"],
            contained=False,
            launch=spent,
            run_test=run_pytest_here,
        )

    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert not (checkout / CONTRACT).exists()


# --- The real pytest runner: whatever the repository's layout -------------


def _fake_uv_executable(tmp_path: Path) -> Path:
    """Write a uv boundary that delegates its ``python`` command.

    The run starts kew's report plugin as the script, which starts pytest.

    Like real uv, ``--frozen`` without a ``uv.lock`` exits 2 before pytest
    runs, and a run without it writes the project's ``uv.lock`` (or, with
    ``FAKE_UV_LOCK_AT`` set, the one in that directory: a workspace root's).
    """
    executable = tmp_path / "fake-uv"
    executable.write_text(
        f"""#!{sys.executable}
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

args = sys.argv[1:]
project = args[args.index("--project") + 1]
if not (Path.cwd() / project / "pyproject.toml").is_file():
    raise SystemExit(2)
lock = Path.cwd() / os.environ.get("FAKE_UV_LOCK_AT", project) / "uv.lock"
if "--frozen" in args and not lock.is_file():
    raise SystemExit(2)
if not lock.is_file():
    lock.write_text("version = 1\\n")
python_args = args[args.index("python") + 1:]
completed = subprocess.run([sys.executable, *python_args], check=False)
raise SystemExit(completed.returncode)
"""
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize("project", [".", "kew"])
def test_a_red_acceptance_test_fails_at_the_base_in_either_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: str
) -> None:
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    repo = tmp_path / "repo"
    (repo / CONTRACT).parent.mkdir(parents=True)
    (repo / project / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / CONTRACT).write_text("def test_ac1():\n    assert False\n")

    assert run_pytest_node(repo, f"{CONTRACT}::test_ac1") == "failed"


#: A committed lock pinning the runner at the versions this interpreter has:
#: a run judged against a lock that pins no pytest is refused.
_COMMITTED_LOCK = f"{pinned_lock()}# committed\n"


def _red_contract_repo(repo: Path, *, locked: bool) -> str:
    """A root-level project holding one red contract; returns its node id."""
    (repo / CONTRACT).parent.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    if locked:
        (repo / "uv.lock").write_text(_COMMITTED_LOCK)
    (repo / CONTRACT).write_text("def test_ac1():\n    assert False\n")
    return f"{CONTRACT}::test_ac1"


@pytest.mark.parametrize("locked", [True, False])
def test_a_red_acceptance_test_fails_with_or_without_a_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool
) -> None:
    """A project without a committed ``uv.lock`` still runs its red test to a
    failure, and neither run leaves a lockfile behind that was not there."""
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    repo = tmp_path / "repo"
    node_id = _red_contract_repo(repo, locked=locked)

    assert run_pytest_node(repo, node_id) == "failed"
    if locked:
        assert (repo / "uv.lock").read_text() == _COMMITTED_LOCK
    else:
        assert not (repo / "uv.lock").exists()


@pytest.mark.parametrize(
    "lock", ["version = [\n", "version = 1\npackage = 1\n"], ids=["toml", "packages"]
)
def test_a_lock_that_does_not_parse_refuses_the_run_rather_than_loading_no_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: str
) -> None:
    """The run's plugins come from the lock; one kew cannot read is not a lock
    that installs none of them."""
    from kew.acceptance_author import AcceptanceAuthorRefused, run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    repo = tmp_path / "repo"
    node_id = _red_contract_repo(repo, locked=False)
    (repo / "uv.lock").write_text(lock)

    with pytest.raises(AcceptanceAuthorRefused, match="uv.lock"):
        run_pytest_node(repo, node_id)
    assert (repo / "uv.lock").read_text() == lock


@pytest.mark.parametrize("workspace", [True, False])
def test_a_lockless_run_removes_only_the_lock_uv_writes_for_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: bool
) -> None:
    """A lock written at the root is the project's own only when the root
    declares the uv workspace; otherwise it is left for the caller to refuse."""
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    monkeypatch.setenv("FAKE_UV_LOCK_AT", ".")
    repo = tmp_path / "repo"
    (repo / CONTRACT).parent.mkdir(parents=True)
    (repo / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    root = "[tool.uv.workspace]\nmembers = ['kew']\n" if workspace else "[x]\n"
    (repo / "pyproject.toml").write_text(root)
    (repo / CONTRACT).write_text("def test_ac1():\n    assert False\n")

    assert run_pytest_node(repo, f"{CONTRACT}::test_ac1") == "failed"
    assert (repo / "uv.lock").exists() is not workspace


def test_a_lock_slot_linked_outside_the_checkout_is_not_the_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``uv.lock`` slot is bounded by its real path: a link out of the
    checkout neither freezes the run nor gets removed with what it points at."""
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    outside = tmp_path / "outside.lock"
    outside.write_text(_COMMITTED_LOCK)
    repo = tmp_path / "repo"
    (repo / CONTRACT).parent.mkdir(parents=True)
    (repo / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "uv.lock").symlink_to(outside)
    (repo / CONTRACT).write_text("def test_ac1():\n    assert False\n")

    assert run_pytest_node(repo, f"{CONTRACT}::test_ac1") == "failed"
    assert not (repo / "kew" / "uv.lock").exists()
    assert (repo / "uv.lock").is_symlink()
    assert _file_text(outside) == _COMMITTED_LOCK


def test_a_contract_test_that_edits_the_project_is_refused_before_the_next_runs(
    checkout: Path, store: Any
) -> None:
    """The next node's ``uv`` would build the edited ``pyproject.toml``; the
    checkout is compared after each node, so it never runs."""
    pyproject = checkout / "kew" / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "project")
    ran: list[str] = []

    def recording(worktree: Path, node_id: str) -> Any:
        ran.append(node_id)
        return run_pytest_here(worktree, node_id)

    edit = "Path('kew/pyproject.toml').write_text('[project]')"
    with pytest.raises(AcceptanceAuthorRefused, match="outside") as refused:
        _author(
            store,
            checkout,
            FakeAuthor({CONTRACT: _planting_tests(edit)}),
            TWO_TEST_PLAN,
            run_test=recording,
        )

    assert "kew/pyproject.toml" in str(refused.value)
    assert ran == [f"{CONTRACT}::test_ac1"]
    assert _file_text(pyproject) == "[project]\nname = 'x'\n"


def test_a_red_at_base_run_through_the_real_runner_leaves_the_checkout_unchanged(
    checkout: Path, store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production runner writes no bytecode or pytest cache, so a real
    contract is recorded rather than refused as a change to the checkout."""
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    (checkout / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "project")

    row = author_acceptance_contract(
        store=store,
        epic_ref="epic-1",
        item_id=str(ISSUE),
        issue_num=ISSUE,
        issue_body="## Acceptance\n- something",
        plan=TWO_TEST_PLAN,
        worktree=checkout,
        implementer_model="gpt-5.6-sol",
        routed_models=["gpt-5.6-sol", "claude-opus-5-5"],
        launch=FakeAuthor({CONTRACT: RED_TESTS}),
        run_test=run_pytest_node,
    )

    assert store.current_acceptance_contract("epic-1", str(ISSUE)) == row
    assert (
        _git(checkout, "status", "--porcelain", "--ignored", "--untracked-files=all")
        == ""
    )


@pytest.mark.parametrize(
    ("source", "outcome"),
    [
        ("def test_ac1():\n    assert 1 == 2\n", "failed"),
        ("def test_ac1():\n    raise AssertionError('not yet')\n", "failed"),
        ("def test_ac1():\n    raise RuntimeError('not implemented')\n", "error"),
        (
            "import pytest\n\ndef test_ac1():\n"
            "    with pytest.raises(ValueError):\n        int('1')\n",
            "error",
        ),
        ("import pytest\n\ndef test_ac1():\n    pytest.fail('not yet')\n", "error"),
        (
            "import pytest\n\ndef test_ac1():\n"
            "    with pytest.raises(ValueError):\n        raise RuntimeError\n",
            "error",
        ),
        ("def test_ac1(no_such_fixture):\n    assert False\n", "error"),
        (
            "import no_such_module_for_kew\n\ndef test_ac1():\n    assert False\n",
            "error",
        ),
        ("def test_ac1():\n    assert True\n", "passed"),
        # Dies before pytest writes its junit report, still exiting 1.
        ("import os\n\ndef test_ac1():\n    os._exit(1)\n", "error"),
    ],
    ids=[
        "assertion",
        "bare-assertion-error",
        "runtime-error",
        "did-not-raise",
        "pytest-fail",
        "raised-the-wrong-exception",
        "setup-error",
        "collection-error",
        "green",
        "missing-report",
    ],
)
def test_the_real_runner_counts_only_an_assertion_failure_as_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, outcome: str
) -> None:
    from kew.acceptance_author import run_pytest_node

    monkeypatch.setitem(TOOL_PATHS, "uv", str(_fake_uv_executable(tmp_path)))
    repo = tmp_path / "repo"
    (repo / CONTRACT).parent.mkdir(parents=True)
    (repo / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / CONTRACT).write_text(source)

    assert run_pytest_node(repo, f"{CONTRACT}::test_ac1") == outcome


_JUNIT = "<testsuites><testsuite>{cases}</testsuite></testsuites>"
_ASSERT_FAILED = (
    '<testcase name="test_ac1"><failure message="assert 1 == 2"/></testcase>'
)
_SETUP_ERROR = '<testcase name="test_ac1"><error message="fixture missing"/></testcase>'
_PASSED = '<testcase name="test_ac1"/>'
_RUNTIME_ERROR = (
    '<testcase name="test_ac1">'
    '<failure message="RuntimeError: not implemented"/></testcase>'
)
_DISGUISED_RUNTIME_ERROR = (
    '<testcase name="test_ac1">'
    '<failure message="RuntimeError: AssertionError: assert False"/></testcase>'
)
_TYPED_RUNTIME_ERROR = (
    '<testcase name="test_ac1">'
    '<failure type="RuntimeError" message="assert 1 == 2"/></testcase>'
)
_FAILED_THEN_TEARDOWN_ERROR = (
    '<testcase name="test_ac1"><failure message="assert 1 == 2"/>'
    '<error message="teardown failed"/></testcase>'
)
_OTHER_NAME_FAILED = (
    '<testcase name="test_other"><failure message="assert 1 == 2"/></testcase>'
)
_DID_NOT_RAISE = (
    '<testcase name="test_ac1">'
    "<failure message=\"Failed: DID NOT RAISE &lt;class 'ValueError'&gt;\"/>"
    "</testcase>"
)
_BARE_PYTEST_FAIL = '<testcase name="test_ac1"><failure message="Failed"/></testcase>'
_PARAMETRIZED_FAILED = (
    '<testcase name="test_ac1[a]"><failure message="assert 1 == 2"/></testcase>'
)


@pytest.mark.parametrize(
    ("cases", "outcome"),
    [
        (_ASSERT_FAILED, "failed"),
        (_PARAMETRIZED_FAILED, "failed"),
        (_DID_NOT_RAISE, "error"),
        (_BARE_PYTEST_FAIL, "error"),
        (_SETUP_ERROR, "error"),
        (_ASSERT_FAILED + _SETUP_ERROR, "error"),
        (_FAILED_THEN_TEARDOWN_ERROR, "error"),
        (_PASSED, "error"),
        (_RUNTIME_ERROR, "error"),
        (_DISGUISED_RUNTIME_ERROR, "error"),
        (_TYPED_RUNTIME_ERROR, "error"),
        ("", "error"),
    ],
)
def test_only_an_assertion_failure_is_red_at_base(cases: str, outcome: str) -> None:
    """A missing fixture or a RuntimeError also makes pytest exit 1; not red."""
    from kew.acceptance_author import junit_outcome

    assert junit_outcome(_JUNIT.format(cases=cases), ["test_ac1"]) == outcome


@pytest.mark.parametrize(
    ("cases", "expected"),
    [
        (_OTHER_NAME_FAILED, ["test_ac1"]),
        (_ASSERT_FAILED, ["test_ac1", "test_ac2"]),
        (_ASSERT_FAILED.replace("test_ac1", "test_ac10"), ["test_ac1"]),
    ],
)
def test_a_report_missing_an_expected_testcase_is_not_red(
    cases: str, expected: list[str]
) -> None:
    from kew.acceptance_author import junit_outcome

    assert junit_outcome(_JUNIT.format(cases=cases), expected) == "error"


def test_unparsable_junit_is_no_verdict() -> None:
    from kew.acceptance_author import junit_outcome

    assert junit_outcome("<testsuites", ["test_ac1"]) == "error"


def _commit_implementation(checkout: Path) -> str:
    """The implementer's own commit on the node's branch; returns its sha."""
    (checkout / "kew" / "feature.py").write_text("VALUE = 2\n")
    _git(checkout, "commit", "-qam", "implementation")
    return _git(checkout, "rev-parse", "HEAD")


def test_a_continuation_still_at_its_base_authors_the_contract_first(
    store: Any, checkout: Path
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    author = FakeAuthor({CONTRACT: RED_TESTS})

    contract = prepare_acceptance_contract(
        store=store,
        epic_ref="epic-1",
        item_id=str(ISSUE),
        issue_num=ISSUE,
        issue_body="## Acceptance\n- something",
        plan=TWO_TEST_PLAN,
        worktree=checkout,
        implementer_model="gpt-5.6-sol",
        routed_models=["gpt-5.6-sol", "claude-opus-5-5"],
        contained=False,
        launch=author,
        run_test=run_pytest_here,
        continuation_base=base,
    )

    assert len(author.requests) == 1
    assert _git(checkout, "rev-parse", "HEAD^") == base
    assert _git(checkout, "rev-parse", "HEAD") == contract.commit


def test_a_continuation_past_its_base_with_no_contract_is_refused(
    store: Any, checkout: Path
) -> None:
    """#2771: a contract authored now would sit on top of the implementation."""
    base = _git(checkout, "rev-parse", "HEAD")
    head = _commit_implementation(checkout)
    author = FakeAuthor({CONTRACT: RED_TESTS})

    with pytest.raises(AcceptanceAuthorRefused, match="implementation commits"):
        prepare_acceptance_contract(
            store=store,
            epic_ref="epic-1",
            item_id=str(ISSUE),
            issue_num=ISSUE,
            issue_body="## Acceptance\n- something",
            plan=TWO_TEST_PLAN,
            worktree=checkout,
            implementer_model="gpt-5.6-sol",
            routed_models=["gpt-5.6-sol", "claude-opus-5-5"],
            contained=False,
            launch=author,
            run_test=run_pytest_here,
            continuation_base=base,
        )

    assert author.requests == []
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert not (checkout / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None


# A continuation through the real dispatch path: the store, its continuation
# claim, the plan and the git checkout are real. Only two imported boundaries
# are stubbed: containment resolves native, and the runner preflight -- the
# last step before the implementer's paid launch -- records what the
# implementer would have started on, then refuses so nothing is launched.
TESTED_BODY = (
    "## Acceptance Criteria\n"
    "- [ ] the value is two (kind: test)\n"
    "- [ ] the name is set (kind: test)\n"
)
TESTED_BODY_RED_TESTS = RED_TESTS.replace("def test_ac3", "def test_ac2")


class ImplementerLaunched(RuntimeError):
    """The runner preflight was reached: the implementer would launch now."""


def _claimed_continuation(store: Any, preserved: Path, base: str) -> None:
    """Record node 7's original run on *preserved* and claim its continuation."""
    from kew.dispatch_core import _bind_publication_base
    from kew.run_store import RunRecord
    from kew.worktree import write_checkout_generation

    write_checkout_generation(preserved)
    branch = f"agent/issue-{ISSUE}"
    started = datetime(2026, 10, 12, tzinfo=UTC)
    store.save(
        RunRecord(
            run_id="original-run",
            issue_num=ISSUE,
            item_id=str(ISSUE),
            branch=branch,
            status="failure",
            exit_code=1,
            run_kind="dispatch",
            started_at=started,
            finished_at=started,
        )
    )
    _bind_publication_base(
        store,
        publish="canonical",
        base=base,
        run_id="original-run",
        issue_num=ISSUE,
        item_id=str(ISSUE),
        branch=branch,
        checkout=preserved,
    )
    record = store.record_continuation(
        "epic-1",
        str(ISSUE),
        "original-run",
        branch,
        preserved,
        checkout_generation=fixture_checkout_generation(preserved),
    )
    claimed = store.claim_continuation_under_cap(
        record.epic_ref,
        record.item_id,
        record.original_run_id,
        record.branch,
        record.worktree_identity,
        "continuation-run",
        5,
        claim_evidence=fixture_claim_evidence(
            store,
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            "continuation-run",
        ),
    )
    assert claimed == "claimed"


def _continue_dispatch(
    monkeypatch: pytest.MonkeyPatch, store: Any, preserved: Path, base: str
) -> list[tuple[str, bool]]:
    """Run a claimed continuation of node 7 on *preserved*; return its launches."""
    from kew import dispatch_core
    from kew.config import DispatchConfig
    from kew.sandbox import SandboxDecision
    from kew.worktree import release_checkout_claim

    _claimed_continuation(store, preserved, base)
    launches: list[tuple[str, bool]] = []

    def at_launch(*_a: Any, **_k: Any) -> str:
        launches.append(
            (_git(preserved, "rev-parse", "HEAD"), (preserved / CONTRACT).exists())
        )
        raise ImplementerLaunched

    monkeypatch.setattr(
        "kew.dispatch_core.resolve_effective_sandbox",
        lambda *_a, **_k: SandboxDecision(
            sandboxed=False, runtime=None, posture="native", notice=None, image=None
        ),
    )
    monkeypatch.setattr("kew.runner_preflight.runner_preflight_failure", at_launch)
    config = DispatchConfig()
    config.branches.worktree_base = str(preserved.parent.parent)
    ctx = dispatch_core._DispatchContext(
        config=config,
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref="epic-1",
        provider="github",
        issue_num=ISSUE,
        issue_title="Resume",
        issue_body=TESTED_BODY,
        complexity="simple",
        item_id=str(ISSUE),
        branch=f"agent/issue-{ISSUE}",
        start_monotonic=0.0,
        refuse_rollback=lambda _reason: None,
        terminal_failure=None,
        run_kind="dispatch",
        worktree_dir=preserved,
        run_id="continuation-run",
    )
    runner = MagicMock()
    runner.name = "claude-code"
    runner.publication_style = "host"
    try:
        with contextlib.suppress(ImplementerLaunched):
            dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=config,
                resolved=MagicMock(runner=runner, model="claude-sonnet-4-6"),
                runner=runner,
                early=dispatch_core._EarlyRunInfo(runner="claude-code"),
                admit_budget_check=None,
            )
    finally:
        release_checkout_claim(preserved)
    return launches


def test_a_continuation_restores_the_recorded_contract_before_the_implementer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any, checkout: Path
) -> None:
    """#2771 AC1."""
    base = _git(checkout, "rev-parse", "HEAD")
    _prepare_contract(
        store,
        checkout,
        FakeAuthor({CONTRACT: TESTED_BODY_RED_TESTS}),
        extract_plan(TESTED_BODY),
    )
    contract = store.current_acceptance_contract("epic-1", str(ISSUE))
    preserved = _fresh_checkout(tmp_path, checkout, base, "worktrees/agent/issue-7")
    assert not (preserved / CONTRACT).exists()

    launches = _continue_dispatch(monkeypatch, store, preserved, base)

    assert launches == [(contract.commit, True)]
    assert _contract_text(preserved) == TESTED_BODY_RED_TESTS


def test_a_continuation_with_implementation_commits_and_no_contract_never_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any, checkout: Path
) -> None:
    """#2771 AC2."""
    base = _git(checkout, "rev-parse", "HEAD")
    preserved = _fresh_checkout(tmp_path, checkout, base, "worktrees/agent/issue-7")
    head = _commit_implementation(preserved)

    with pytest.raises(AcceptanceAuthorRefused, match="implementation commits"):
        _continue_dispatch(monkeypatch, store, preserved, base)

    plan = store.current_item_plan(str(ISSUE))
    assert plan is not None and '"test"' in plan.plan_json
    assert _git(preserved, "rev-parse", "HEAD") == head
    assert not (preserved / CONTRACT).exists()
    assert store.current_acceptance_contract("epic-1", str(ISSUE)) is None
    assert store.get("continuation-run") is None
