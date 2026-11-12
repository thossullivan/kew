"""#2200: the governed brief names the test module of each changed module.

A remediation brief tells the agent to run "the tests covering your changes",
and an agent reading only the finding decides for itself what that means.
#2053 edited `src/kew/sandbox.py` across its fix rounds, never ran
`tests/test_sandbox.py`, and broke four asserts that only the pre-merge verify
caught. The host now reads the diff and names those test modules itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kew.config import DispatchConfig
from kew.review import build_review_prompt, governed_test_targets
from kew.review_targets import (
    TargetGroup,
    module_test_targets,
    remediation_test_targets,
)


def _checkout(tmp_path: Path, *files: str) -> Path:
    for rel in files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


class TestModuleTestPaths:
    """The diff -> test module mapping the host computes."""

    def test_a_changed_module_names_its_own_test_module(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path, "src/kew/sandbox.py", "tests/test_sandbox.py")
        assert module_test_targets(["src/kew/sandbox.py"], root=root) == [
            TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))
        ]

    def test_suffixed_test_modules_join_the_set(self, tmp_path: Path) -> None:
        root = _checkout(
            tmp_path,
            "src/kew/sandbox.py",
            "tests/test_sandbox.py",
            "tests/test_sandbox_containment.py",
            "tests/test_sandboxing.py",
        )
        assert module_test_targets(["src/kew/sandbox.py"], root=root) == [
            TargetGroup(
                run_dir="",
                paths=("tests/test_sandbox.py", "tests/test_sandbox_containment.py"),
            )
        ]

    def test_a_module_with_no_test_file_adds_nothing(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path, "src/kew/lonely.py", "tests/test_sandbox.py")
        assert module_test_targets(["src/kew/lonely.py"], root=root) == []

    def test_a_nested_module_finds_the_package_tests_dir(self, tmp_path: Path) -> None:
        root = _checkout(
            tmp_path,
            "src/kew/graph/driver/overlay.py",
            "tests/test_overlay.py",
        )
        assert module_test_targets(["src/kew/graph/driver/overlay.py"], root=root) == [
            TargetGroup(run_dir="", paths=("tests/test_overlay.py",))
        ]

    def test_the_tests_dir_beside_the_package_is_found(self, tmp_path: Path) -> None:
        """A monorepo: the diff is repo-rooted, the tests live under the pkg.

        The path is named relative to the package, not the repository: the
        test command is configured for `kew/`, where `kew/tests/test_sandbox.py`
        does not exist and pytest exits 4 on collection.
        """
        root = _checkout(
            tmp_path, "kew/src/kew/sandbox.py", "kew/tests/test_sandbox.py"
        )
        assert module_test_targets(["kew/src/kew/sandbox.py"], root=root) == [
            TargetGroup(run_dir="kew", paths=("tests/test_sandbox.py",))
        ]

    def test_each_package_carries_its_own_run_directory(self, tmp_path: Path) -> None:
        """Two packages name the same relative path from different dirs."""
        root = _checkout(
            tmp_path,
            "kew/src/kew/sandbox.py",
            "kew/tests/test_sandbox.py",
            "create-kew/src/sandbox.py",
            "create-kew/tests/test_sandbox.py",
        )
        changed = ["kew/src/kew/sandbox.py", "create-kew/src/sandbox.py"]
        assert module_test_targets(changed, root=root) == [
            TargetGroup(run_dir="create-kew", paths=("tests/test_sandbox.py",)),
            TargetGroup(run_dir="kew", paths=("tests/test_sandbox.py",)),
        ]

    def test_changed_test_modules_and_non_python_paths_are_ignored(
        self, tmp_path: Path
    ) -> None:
        root = _checkout(
            tmp_path,
            "tests/test_sandbox.py",
            "tests/test_review.py",
            "docs/sandbox.md",
        )
        changed = ["tests/test_sandbox.py", "docs/sandbox.md", "README.md"]
        assert module_test_targets(changed, root=root) == []

    def test_paths_outside_the_checkout_are_ignored(self, tmp_path: Path) -> None:
        root = _checkout(tmp_path, "tests/test_sandbox.py")
        changed = ["../elsewhere/sandbox.py", "/etc/sandbox.py", ""]
        assert module_test_targets(changed, root=root) == []

    def test_a_directory_named_like_a_test_module_is_not_named(
        self, tmp_path: Path
    ) -> None:
        root = _checkout(tmp_path, "src/kew/sandbox.py")
        (root / "tests" / "test_sandbox.py").mkdir(parents=True)
        assert module_test_targets(["src/kew/sandbox.py"], root=root) == []

    def test_the_set_is_deduplicated_across_changed_modules(
        self, tmp_path: Path
    ) -> None:
        root = _checkout(
            tmp_path,
            "src/kew/sandbox.py",
            "src/kew/review.py",
            "tests/test_sandbox.py",
            "tests/test_review.py",
        )
        changed = ["src/kew/sandbox.py", "src/kew/sandbox.py", "src/kew/review.py"]
        assert module_test_targets(changed, root=root) == [
            TargetGroup(
                run_dir="",
                paths=("tests/test_review.py", "tests/test_sandbox.py"),
            )
        ]


class TestHostComputesTheSetFromTheDiff:
    """The host reads the diff itself; the agent never has to."""

    @pytest.fixture(autouse=True)
    def _real_git(self) -> None:
        """Register the real git; the hermetic allowlist fixture restores it."""
        from kew.subprocess_utils import TOOL_PATHS

        git = shutil.which("git")
        assert git is not None, "git is required for this test"
        TOOL_PATHS["git"] = git

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": os.environ.get("PATH", ""),
        }
        done = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return done.stdout.strip()

    def _repo(self, tmp_path: Path, *, pkg: str = "") -> Path:
        """A checkout whose `origin/main` is the commit the branch forked from.

        *pkg* nests the package under a subdirectory, the layout of this repo.
        """
        root = tmp_path / "repo"
        base = root / pkg if pkg else root
        (base / "src" / "kew").mkdir(parents=True)
        (base / "tests").mkdir()
        (base / "src" / "kew" / "sandbox.py").write_text("x = 1\n", encoding="utf-8")
        (base / "tests" / "test_sandbox.py").write_text("", encoding="utf-8")
        self._git(root, "init", "-b", "main")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-m", "base")
        head = self._git(root, "rev-parse", "HEAD")
        self._git(root, "update-ref", "refs/remotes/origin/main", head)
        return root

    def test_the_targets_come_from_the_branch_diff(self, tmp_path: Path) -> None:
        root = self._repo(tmp_path)
        (root / "src" / "kew" / "sandbox.py").write_text("x = 2\n", encoding="utf-8")
        self._git(root, "commit", "-am", "edit")
        assert governed_test_targets(root, "main") == [
            TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))
        ]

    def test_a_packaged_repo_names_package_relative_paths(self, tmp_path: Path) -> None:
        """The diff is repo-rooted; the named path must not be (#2212)."""
        root = self._repo(tmp_path, pkg="kew")
        edited = root / "kew" / "src" / "kew" / "sandbox.py"
        edited.write_text("x = 2\n", encoding="utf-8")
        self._git(root, "commit", "-am", "edit")
        assert governed_test_targets(root, "main") == [
            TargetGroup(run_dir="kew", paths=("tests/test_sandbox.py",))
        ]

    def test_an_unchanged_branch_names_nothing(self, tmp_path: Path) -> None:
        root = self._repo(tmp_path)
        assert governed_test_targets(root, "main") == []

    def test_a_tree_the_selector_cannot_read_keeps_the_floor_and_says_so(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No base to diff against: the import graph names nothing, the name
        rule's floor stands alone, and a warning says why it is only the floor."""
        root = self._repo(tmp_path)
        self._git(root, "update-ref", "-d", "refs/remotes/origin/main")
        self._git(root, "branch", "-m", "main", "topic")

        targets = remediation_test_targets(
            ["src/kew/sandbox.py"], [], root=root, base_branch="main"
        )

        assert targets == [TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))]
        warned = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
            and "test selector could not read" in r.getMessage()
        ]
        assert len(warned) == 1 and "main" in warned[0]


class TestTheBriefNamesThem:
    """What the agent actually reads."""

    @staticmethod
    def _prompt(*, governed: bool, targets: list[TargetGroup]) -> str:
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        config.commands.test_targeted = "uv run pytest {paths}"
        return build_review_prompt(
            pr_number=10,
            issue_number=5,
            branch_name="agent/issue-5",
            comments=[],
            config=config,
            host_publication=True,
            governed=governed,
            targeted_tests=targets,
        )

    def test_a_diff_touching_sandbox_lists_its_test_module(self) -> None:
        prompt = self._prompt(
            governed=True,
            targets=[
                TargetGroup(
                    run_dir="",
                    paths=(
                        "tests/test_sandbox.py",
                        "tests/test_sandbox_containment.py",
                    ),
                )
            ],
        )
        assert "`tests/test_sandbox.py`" in prompt
        assert "`tests/test_sandbox_containment.py`" in prompt

    def test_a_packaged_path_is_briefed_with_the_dir_to_run_it_from(self) -> None:
        """The brief must never hand a repo-rooted path to the test command.

        `uv run pytest kew/tests/test_sandbox.py` from `kew/` exits 4 on
        collection — the path does not exist there (#2212).
        """
        prompt = self._prompt(
            governed=True,
            targets=[TargetGroup(run_dir="kew", paths=("tests/test_sandbox.py",))],
        )
        assert "run from `kew/`" in prompt
        assert "    - `tests/test_sandbox.py`" in prompt
        assert "kew/tests/test_sandbox.py" not in prompt

    def test_a_root_run_dir_brief_names_no_directory(self) -> None:
        prompt = self._prompt(
            governed=True,
            targets=[TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))],
        )
        assert "run from" not in prompt

    def test_the_brief_says_the_set_may_grow_but_never_shrink(self) -> None:
        prompt = self._prompt(
            governed=True,
            targets=[TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))],
        )
        body = prompt[prompt.index("## Verification") :]
        assert "never drop" in body.lower()

    def test_a_module_with_no_test_file_adds_nothing_to_the_brief(self) -> None:
        prompt = self._prompt(governed=True, targets=[])
        assert "never drop" not in prompt.lower()
        assert "tests/test_" not in prompt

    def test_a_standalone_review_never_lists_them(self) -> None:
        prompt = self._prompt(
            governed=False,
            targets=[TargetGroup(run_dir="", paths=("tests/test_sandbox.py",))],
        )
        assert "tests/test_sandbox.py" not in prompt
