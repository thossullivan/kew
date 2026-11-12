"""Lesson-channel guard tests (Task 8, spec D8).

The D6/D7 lessons loader and fenced injection were deleted (#1647); the D8
channel separation stays because ``scripts/check_memory_channel.py`` is a CI
gate. Three concerns live here:

* ``classify_memory_channel`` — the pure D8 classifier: a change set is valid
  only when it is *pure* (all lesson files, or all work files); a mixed set is
  rejected. Proving cases per the issue: mixed fails, pure-lesson passes,
  pure-work passes.
* the ``scripts/check_memory_channel.py`` CLI over an explicit path list.
* the no-arg *history mode* actually run in CI (``_resolve_base`` /
  ``_check_history`` / ``_commits_ahead`` / ``_files_in_commit``, plus
  ``MEMORY_GUARD_BASE`` handling), incl. the D8 fail-open regression lock
  (issue #1055): a pinned-but-unresolvable base fails closed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kew.memory import classify_memory_channel

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECK_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_memory_channel.py"
)

# --- pure classifier: the three proving cases from the issue -----------------


def test_memory_guard_mixed_change_set_fails() -> None:
    verdict = classify_memory_channel(
        [".kew/memory/0001-trap-gates.md", "src/kew/dispatch_core.py"]
    )
    assert verdict.ok is False
    assert verdict.memory_paths == (".kew/memory/0001-trap-gates.md",)
    assert verdict.other_paths == ("src/kew/dispatch_core.py",)
    assert "inline-smuggling" in verdict.reason


def test_memory_guard_pure_lesson_change_set_passes() -> None:
    verdict = classify_memory_channel(
        [".kew/memory/0001-trap-gates.md", ".kew/memory/0002-foo.md"]
    )
    assert verdict.ok is True
    assert verdict.other_paths == ()
    assert "lesson-only" in verdict.reason


def test_memory_guard_pure_work_change_set_passes() -> None:
    verdict = classify_memory_channel(
        ["src/kew/dispatch_core.py", "tests/test_dispatch_core.py", "README.md"]
    )
    assert verdict.ok is True
    assert verdict.memory_paths == ()
    assert "work-only" in verdict.reason


def test_memory_guard_empty_change_set_passes() -> None:
    verdict = classify_memory_channel([])
    assert verdict.ok is True


def test_memory_guard_blank_paths_ignored() -> None:
    verdict = classify_memory_channel(["  ", ".kew/memory/0001-trap-gates.md", ""])
    assert verdict.ok is True
    assert verdict.memory_paths == (".kew/memory/0001-trap-gates.md",)


def test_memory_guard_normalizes_backslash_and_leading_dotslash() -> None:
    # A Windows-style or ./-prefixed path under the lessons dir still counts as
    # a lesson path (so it can't sneak into a "pure work" set).
    mixed = classify_memory_channel([".kew\\memory\\0001-x.md", "src/kew/x.py"])
    assert mixed.ok is False
    pure = classify_memory_channel(["./.kew/memory/0001-x.md"])
    assert pure.ok is True and pure.other_paths == ()


def test_memory_guard_lookalike_path_is_not_a_lesson_path() -> None:
    # `.kew/memoryX` / `.kew/memory-notes` must not match the `.kew/memory/` dir.
    verdict = classify_memory_channel([".kew/memory-notes.md", "src/kew/x.py"])
    assert verdict.ok is True
    assert verdict.memory_paths == ()


# --- CLI over an explicit path list ------------------------------------------


def _run_cli(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), *paths],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "kew",
    )


def test_memory_guard_cli_mixed_exits_nonzero() -> None:
    proc = _run_cli(".kew/memory/0001-trap-gates.md", "src/kew/dispatch_core.py")
    assert proc.returncode == 1
    assert "inline-smuggling" in proc.stderr


def test_memory_guard_cli_pure_lesson_exits_zero() -> None:
    proc = _run_cli(".kew/memory/0001-trap-gates.md")
    assert proc.returncode == 0


def test_memory_guard_cli_pure_work_exits_zero() -> None:
    proc = _run_cli("src/kew/dispatch_core.py", "README.md")
    assert proc.returncode == 0


def test_memory_guard_script_imports_shared_classifier() -> None:
    # The git plumbing wrapper must reuse the src/ classifier, not fork the rule.
    spec = importlib.util.spec_from_file_location("_cmc", CHECK_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.classify_memory_channel is classify_memory_channel


def test_memory_guard_bad_configured_base_exits_nonzero() -> None:
    """An explicitly configured MEMORY_GUARD_BASE that doesn't resolve must fail
    loudly (exit 2), never degrade to "nothing to check" — a typo'd base would
    otherwise silently disable the guard (fail-open)."""
    proc = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "kew",
        env={**os.environ, "MEMORY_GUARD_BASE": "refs/heads/does-not-exist-zz9"},
    )
    assert proc.returncode == 2
    assert "does not resolve" in proc.stderr


# --- no-arg history mode: the path actually run in CI (issue #1055) -----------
#
# The `_resolve_base`/`_check_history`/`_commits_ahead`/`_files_in_commit` path
# had zero coverage — only the explicit-path classifier above was tested. These
# drive the real script over a throwaway git repo. `preflight_tools()` requires
# `gh`, which the history path never uses, so we stub it onto PATH to keep the
# tests hermetic (and runnable in gh-less environments).


def _fake_gh_on_path(tmp_path: Path) -> str:
    """A no-op ``gh`` on PATH so ``preflight_tools`` passes; history mode only
    shells out to ``git``, never ``gh``. Symlink a real binary (``true``) rather
    than write a script so it stays executable even on a ``noexec`` tmp mount."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    target = shutil.which("true") or "/bin/true"
    (bindir / "gh").symlink_to(target)
    return str(bindir)


def _init_history_repo(repo: Path) -> tuple[str, object]:
    """Init a throwaway repo with a base commit; return ``(base_sha, git_fn)``."""
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "guard-test@example.com")
    git("config", "user.name", "Guard Test")
    git("config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n")
    git("add", "README.md")
    git("commit", "-q", "-m", "base")
    return git("rev-parse", "HEAD"), git


def _run_history(
    repo: Path, base: str, gh_dir: str
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "MEMORY_GUARD_BASE": base,
        "PATH": gh_dir + os.pathsep + os.environ["PATH"],
    }
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_memory_guard_history_pinned_resolvable_inspects_pure_commit(
    tmp_path: Path,
) -> None:
    """(b) A pinned-and-resolvable base must actually inspect the commits ahead —
    a pure-work commit passes, proving the gate isn't a no-op."""
    repo = tmp_path / "repo"
    base, git = _init_history_repo(repo)
    (repo / "feature.py").write_text("x = 1\n")
    git("add", "feature.py")
    git("commit", "-q", "-m", "pure work")

    proc = _run_history(repo, base, _fake_gh_on_path(tmp_path))

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "1 commit(s) ahead" in proc.stdout
    assert "all pure" in proc.stdout


def test_memory_guard_history_pinned_resolvable_flags_mixed_commit(
    tmp_path: Path,
) -> None:
    """A commit mixing a lesson file with work is caught in history mode, not just
    when a path list is passed explicitly."""
    repo = tmp_path / "repo"
    base, git = _init_history_repo(repo)
    mem = repo / ".kew" / "memory"
    mem.mkdir(parents=True)
    (mem / "0009-note.md").write_text("lesson body\n")
    (repo / "feature.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "mixed lesson+work")

    proc = _run_history(repo, base, _fake_gh_on_path(tmp_path))

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "mixed commit(s) found" in proc.stderr


def test_memory_guard_history_pinned_unresolvable_fails_closed(
    tmp_path: Path,
) -> None:
    """(c) Regression lock for the D8 fail-open: a pinned-but-unresolvable base
    must exit non-zero and never reach the exit-0 'nothing to check' path. A
    mutation restoring ``return None`` here flips the exit code and fails this."""
    repo = tmp_path / "repo"
    _init_history_repo(repo)  # a valid repo; only the pinned ref is bogus

    proc = _run_history(repo, "no-such-ref-zz9", _fake_gh_on_path(tmp_path))

    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "does not resolve" in proc.stderr
    assert "nothing to check" not in proc.stdout


def test_the_memory_channel_check_runs_without_gh(tmp_path: Path) -> None:
    """The guard only shells out to ``git``, so a host with no GitHub CLI —
    a verify host, which holds no GitHub token — runs it and passes a pure
    commit. PATH holds ``git`` and nothing else."""
    repo = tmp_path / "repo"
    base, git = _init_history_repo(repo)
    (repo / "feature.py").write_text("x = 1\n")
    git("add", "feature.py")
    git("commit", "-q", "-m", "pure work")
    bindir = tmp_path / "git-only"
    bindir.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    (bindir / "git").symlink_to(real_git)

    proc = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_GUARD_BASE": base, "PATH": str(bindir)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "1 commit(s) ahead" in proc.stdout
    assert "all pure" in proc.stdout
