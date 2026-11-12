"""The resolution path reads the agent's checkout without running its programs.

After a resolving agent has run, kew reads the agent's checkout on the host:
which files changed, whether conflict state remains, which commit is HEAD and
whether it integrates both inputs. The agent can write that checkout's
``.git/config`` (and any file it includes), and plain git runs programs that
config names: ``core.fsmonitor`` on nearly every index read, and a clean filter
driver whenever ``git diff`` has to hash a working-tree file. Each test plants
both in a scratch checkout, each writing to a marker file, and asserts the
marker never appears; the positive control runs the same argv under plain git
and asserts it does, so a marker that never appears means the read was
hardened, not that the plant was inert.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from support.source_ast import module_tree

from kew import resolve, resolve_checks
from kew.resolve import (
    _assert_resolution_ancestry,
    _assert_resolved_worktree,
    _is_ancestor,
    _worktree_head_sha,
)
from kew.resolve_checks import deleted_paths, touched_paths
from kew.subprocess_utils import TOOL_PATHS
from kew.worktree import (
    _checkout_read_command,
    checkout_content_read_env,
    has_uncommitted_changes,
)


@pytest.fixture(autouse=True)
def _host_git(monkeypatch: pytest.MonkeyPatch) -> None:
    found = shutil.which("git")
    assert found is not None, "git required"
    monkeypatch.setitem(TOOL_PATHS, "git", found)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Planted:
    def __init__(self, checkout: Path, marker: Path, base: str, head: str) -> None:
        self.checkout = checkout
        self.marker = marker
        self.base = base
        self.head = head

    def fired(self) -> str:
        return self.marker.read_text() if self.marker.exists() else ""


@pytest.fixture
def planted(tmp_path: Path) -> Planted:
    """A checkout whose config runs a program on fsmonitor and on clean filter.

    ``a.txt`` is edited to the same size and given an mtime its index entry
    does not record, so git cannot trust the stat data and must hash the file,
    which is when a clean filter runs. The filter is declared ``required`` and
    defined in an included file, the way an agent would hide it.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = tmp_path / "marker"
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@t.t")
    _git(checkout, "config", "user.name", "t")
    (checkout / "a.txt").write_text("hi\n")
    (checkout / "c.txt").write_text("deleted later\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "d.txt").write_text("resolution\n")
    _git(checkout, "add", "d.txt")
    _git(checkout, "commit", "-qm", "resolution")
    head = _git(checkout, "rev-parse", "HEAD")

    (checkout / "a.txt").write_text("ho\n")
    os.utime(checkout / "a.txt", (1_000_000_000, 1_000_000_000))
    (checkout / "b.txt").write_text("untracked\n")
    (checkout / "c.txt").unlink()

    included = tmp_path / "agent.cfg"
    included.write_text(
        '[filter "evil"]\n'
        f"\tclean = echo clean >> '{marker}'; cat\n"
        f"\tprocess = echo process >> '{marker}'; false\n"
        "\trequired = true\n"
    )
    (checkout / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    _git(checkout, "config", "include.path", str(included))
    _git(checkout, "config", "core.fsmonitor", f"echo fsmonitor >> '{marker}'; false")
    return Planted(checkout, marker, base, head)


#: Every git read the resolution path makes of the agent's checkout.
def _resolution_reads(p: Planted) -> list[list[str]]:
    return [
        ["diff", "--name-only", "--diff-filter=d", p.base],
        ["ls-files", "--others", "--exclude-standard"],
        ["ls-files", "-u"],
        ["ls-files", "-z"],
        ["check-attr", "-z", "conflict-marker-size", "--", "a.txt"],
        ["grep", "-I", "-n", "-E", r"^(<{7,}|\|{7,}|>{7,})( |$)", "--"],
    ]


@pytest.mark.parametrize("index", range(6))
def test_plain_git_runs_the_planted_program_for_each_resolution_read(
    planted: Planted, index: int
) -> None:
    """Positive control: the plant is live for every read, under plain git."""
    args = _resolution_reads(planted)[index]
    subprocess.run(["git", "-C", str(planted.checkout), *args], capture_output=True)
    assert "fsmonitor" in planted.fired()


def test_plain_git_diff_runs_the_planted_clean_filter(planted: Planted) -> None:
    """Positive control: an index-trusting fsmonitor off, the filter still runs."""
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(planted.checkout)]
        + ["diff", "--name-only", "--diff-filter=d", planted.base],
        capture_output=True,
    )
    assert "clean" in planted.fired() or "process" in planted.fired()


@pytest.mark.parametrize("index", range(6))
def test_hardened_resolution_read_runs_no_planted_program(
    planted: Planted, index: int
) -> None:
    args = _resolution_reads(planted)[index]
    env = checkout_content_read_env(planted.checkout)
    assert env is not None
    subprocess.run(
        _checkout_read_command(planted.checkout, *args), capture_output=True, env=env
    )
    assert planted.fired() == ""


def test_resolution_changed_files_are_listed_without_running_checkout_programs(
    planted: Planted,
) -> None:
    assert touched_paths(planted.checkout, since=planted.base) == [
        "a.txt",
        "b.txt",
        "d.txt",
    ]
    assert deleted_paths(planted.checkout, since=planted.base) == ["c.txt"]
    assert planted.fired() == ""


def test_resolution_conflict_checks_run_no_checkout_program(planted: Planted) -> None:
    _assert_resolved_worktree(planted.checkout)
    assert planted.fired() == ""


def test_resolution_conflict_checks_still_see_unmerged_entries(
    planted: Planted,
) -> None:
    """Hardening keeps detection: an unmerged index entry is still refused."""
    blob = _git(planted.checkout, "rev-parse", "HEAD:d.txt")
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "update-index", "--index-info"],
        cwd=planted.checkout,
        input=f"0 {'0' * 40}\td.txt\n100644 {blob} 2\td.txt\n100644 {blob} 3\td.txt\n",
        text=True,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_COUNT": "1"}
        | {"GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull},
    )
    planted.marker.unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match="unmerged entries"):
        _assert_resolved_worktree(planted.checkout)
    assert planted.fired() == ""


def test_resolution_head_and_ancestry_reads_run_no_checkout_program(
    planted: Planted,
) -> None:
    assert _worktree_head_sha(planted.checkout) == planted.head
    _assert_resolution_ancestry(
        worktree_dir=planted.checkout,
        local_head=planted.head,
        expected_head_sha=planted.base,
        base_sha=planted.base,
    )
    assert _is_ancestor(planted.checkout, planted.base, planted.head)
    assert not _is_ancestor(planted.checkout, planted.head, planted.base)
    assert planted.fired() == ""


def test_resolution_dirty_check_runs_no_checkout_program(planted: Planted) -> None:
    assert has_uncommitted_changes(planted.checkout) is True
    assert planted.fired() == ""


def test_resolution_dirty_check_still_sees_a_same_size_edit(planted: Planted) -> None:
    """With the clean filter emptied, git compares the file's real bytes."""
    (planted.checkout / "b.txt").unlink()
    (planted.checkout / "c.txt").write_text("deleted later\n")
    assert has_uncommitted_changes(planted.checkout) is True
    (planted.checkout / "a.txt").write_text("hi\n")
    os.utime(planted.checkout / "a.txt", (1_000_000_000, 1_000_000_000))
    assert has_uncommitted_changes(planted.checkout) is False
    assert planted.fired() == ""


_RESOLUTION_MODULES = {"resolve.py": resolve, "resolve_checks.py": resolve_checks}


def _plain_git_argv(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.List | ast.Tuple)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "git"
    ]


@pytest.mark.policy
@pytest.mark.parametrize("module", _RESOLUTION_MODULES)
def test_resolution_modules_build_no_plain_git_argv(module: str) -> None:
    """Every git call in the resolution path reads the agent's checkout.

    So none may build its own ``["git", ...]`` argv: each goes through
    :func:`kew.worktree._checkout_read_command`, which switches off the
    programs a checkout's config can name.
    """
    found = _plain_git_argv(module_tree(_RESOLUTION_MODULES[module]))
    assert found == [], f"{module} builds a plain git argv at lines {found}"


def test_plain_git_argv_rule_flags_a_literal_git_list() -> None:
    """The rule's own control: a literal ``["git", ...]`` argv is found."""
    tree = ast.parse('run(["git", "-C", d, "ls-files"])\nrun(("git", "diff"))\n')
    assert _plain_git_argv(tree) == [1, 2]


@pytest.fixture
def lazy(planted: Planted) -> Planted:
    """The planted checkout, declared a partial clone of a promisor remote.

    Any read that meets a missing object then fetches it from ``origin``,
    over the ssh command the same config names.
    """
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.url", "ssh://example.invalid/r"),
        ("remote.origin.promisor", "true"),
        ("core.sshCommand", f"echo ssh >> '{planted.marker}'; false"),
    ):
        _git(planted.checkout, "config", key, value)
    return planted


_MISSING = "1234567" * 5 + "89abc"


def test_plain_git_lazily_fetches_a_missing_object_over_the_planted_ssh(
    lazy: Planted,
) -> None:
    """Positive control: the partial-clone plant is live under plain git."""
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(lazy.checkout)]
        + ["merge-base", "--is-ancestor", _MISSING, lazy.head],
        capture_output=True,
    )
    assert "ssh" in lazy.fired()


def test_resolution_reads_never_lazily_fetch_a_missing_object(lazy: Planted) -> None:
    assert not _is_ancestor(lazy.checkout, _MISSING, lazy.head)
    assert _worktree_head_sha(lazy.checkout) == lazy.head
    touched_paths(lazy.checkout, since=lazy.base)
    with pytest.raises(RuntimeError, match="omits the fetched base"):
        _assert_resolution_ancestry(
            worktree_dir=lazy.checkout,
            local_head=lazy.head,
            expected_head_sha=lazy.base,
            base_sha=_MISSING,
        )
    assert lazy.fired() == ""


@pytest.fixture
def submodule_planted(tmp_path: Path) -> Planted:
    """A clean checkout whose submodule's own config plants a clean filter.

    The submodule's ``f`` is edited to the same size with an mtime its index
    does not record, so the submodule's dirt can only be read by hashing it.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "f").write_text("hi\n")
    _git(upstream, "add", "f")
    _git(upstream, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "f")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = tmp_path / "marker"
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@t.t")
    _git(checkout, "config", "user.name", "t")
    (checkout / "a.txt").write_text("hi\n")
    _git(checkout, "add", "a.txt")
    _git(checkout, "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")
    _git(
        checkout,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(upstream),
        "s",
    )
    _git(checkout, "commit", "-qm", "submodule")
    head = _git(checkout, "rev-parse", "HEAD")

    inner_git = checkout / ".git" / "modules" / "s"
    (inner_git / "info").mkdir(exist_ok=True)
    (inner_git / "info" / "attributes").write_text("f filter=evil\n")
    _git(
        checkout / "s", "config", "filter.evil.clean", f"echo clean >> '{marker}'; cat"
    )
    (checkout / "s" / "f").write_text("ho\n")
    os.utime(checkout / "s" / "f", (1_000_000_000, 1_000_000_000))
    return Planted(checkout, marker, base, head)


def test_plain_git_diff_runs_the_submodule_planted_clean_filter(
    submodule_planted: Planted,
) -> None:
    """Positive control: the superproject's diff hashes the submodule's file."""
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(submodule_planted.checkout)]
        + ["diff", "--name-only", "--diff-filter=d", submodule_planted.base],
        capture_output=True,
    )
    assert "clean" in submodule_planted.fired()


def test_resolution_changed_files_run_no_submodule_program(
    submodule_planted: Planted,
) -> None:
    """The submodule's recorded commit still counts as a touched path."""
    touched = touched_paths(submodule_planted.checkout, since=submodule_planted.base)
    assert touched == [".gitmodules", "s"]
    assert submodule_planted.fired() == ""


def test_dirty_check_runs_no_submodule_program_and_still_sees_its_dirt(
    submodule_planted: Planted,
) -> None:
    assert has_uncommitted_changes(submodule_planted.checkout) is True
    assert submodule_planted.fired() == ""


@pytest.fixture
def inactive_submodule(tmp_path: Path) -> Path:
    """A checkout whose submodule is in ``.gitmodules`` but not its config.

    The submodule's repository is embedded (``sub/.git`` is a directory) and
    holds an uncommitted edit; with no ``submodule.sub`` section in the
    checkout's config, ``git submodule status`` does not list it as checked
    out, so only its gitlink says a repository lives there.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@t.t")
    _git(checkout, "config", "user.name", "t")
    sub = checkout / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "f").write_text("hi\n")
    _git(sub, "add", "f")
    _git(sub, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "f")
    (checkout / ".gitmodules").write_text(
        '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n'
    )
    _git(checkout, "add", ".gitmodules", "sub")
    _git(checkout, "commit", "-qm", "submodule")
    return checkout


def test_inactive_submodule_is_not_listed_as_checked_out(
    inactive_submodule: Path,
) -> None:
    """Control: ``git submodule status`` marks the submodule uninitialized."""
    status = _git(inactive_submodule, "submodule", "status")
    assert status.startswith("-")
    assert _git(inactive_submodule, "status", "--porcelain") == ""


def test_dirty_check_counts_an_inactive_submodule_repository_as_dirty(
    inactive_submodule: Path,
) -> None:
    (inactive_submodule / "sub" / "f").write_text("edited\n")
    assert has_uncommitted_changes(inactive_submodule) is True


def test_dirty_check_passes_an_inactive_submodule_with_no_repository(
    inactive_submodule: Path,
) -> None:
    """A gitlink whose directory holds no repository has no content to lose."""
    shutil.rmtree(inactive_submodule / "sub")
    (inactive_submodule / "sub").mkdir()
    assert has_uncommitted_changes(inactive_submodule) is False


@pytest.fixture
def pre_2_44_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Host reads run under a git that behaves like one before 2.44.

    That git rejects ``--no-lazy-fetch`` (exit 129) and knows nothing of
    ``GIT_NO_LAZY_FETCH``, so the wrapper drops the variable before running
    the real git. Ubuntu 24.04's apt git is 2.43.
    """
    real = shutil.which("git")
    assert real is not None
    wrapper = tmp_path / "old-git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  [ "$arg" = --no-lazy-fetch ] && '
        "{ echo 'unknown option: --no-lazy-fetch' >&2; exit 129; }\n"
        "done\n"
        "unset GIT_NO_LAZY_FETCH\n"
        f"exec '{real}' \"$@\"\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "git", str(wrapper))


@pytest.mark.usefixtures("pre_2_44_git")
def test_checkout_reads_still_answer_under_git_before_2_44(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    (checkout / "f").write_text("hi\n")
    _git(checkout, "add", "f")
    _git(checkout, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "f")
    head = _git(checkout, "rev-parse", "HEAD")
    assert _worktree_head_sha(checkout) == head
    assert _is_ancestor(checkout, head, head)
    assert has_uncommitted_changes(checkout) is False
    (checkout / "f").write_text("ho\n")
    assert has_uncommitted_changes(checkout) is True


@pytest.mark.usefixtures("pre_2_44_git")
def test_checkout_reads_never_lazily_fetch_under_git_before_2_44(
    lazy: Planted,
) -> None:
    """Without ``GIT_NO_LAZY_FETCH``, only the transport refusal stops the fetch.

    The checkout allows ssh itself, which beats a command-line
    ``protocol.allow=never``; ``GIT_ALLOW_PROTOCOL`` beats all config.
    """
    _git(lazy.checkout, "config", "protocol.ssh.allow", "always")
    assert not _is_ancestor(lazy.checkout, _MISSING, lazy.head)
    assert _worktree_head_sha(lazy.checkout) == lazy.head
    assert lazy.fired() == ""


@pytest.mark.usefixtures("pre_2_44_git")
def test_lazy_plant_fetches_under_git_before_2_44_without_the_guard(
    lazy: Planted,
) -> None:
    """Positive control: the wrapper does not itself stop the lazy fetch."""
    _git(lazy.checkout, "config", "protocol.ssh.allow", "always")
    subprocess.run(
        [TOOL_PATHS["git"], "-c", "core.fsmonitor=false", "-c", "protocol.allow=never"]
        + ["-C", str(lazy.checkout), "merge-base", "--is-ancestor", _MISSING]
        + [lazy.head],
        capture_output=True,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    assert "ssh" in lazy.fired()


def test_dirty_check_never_rewrites_the_index_of_a_linked_repository(
    tmp_path: Path,
) -> None:
    """A ``.git`` file can name another repository; a read leaves its index be.

    Touching a tracked file leaves its stat data stale, which plain ``git
    status`` repairs by rewriting the index (the positive control).
    """
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    (other / "f").write_text("hi\n")
    _git(other, "add", "f")
    _git(other, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "f")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {other / '.git'}\n")
    (checkout / "f").write_text("hi\n")
    index = other / ".git" / "index"

    def stale() -> int:
        os.utime(checkout / "f", (1_000_000_000, 1_000_000_000))
        os.utime(index, (1_000_000_000, 1_000_000_000))
        return index.stat().st_mtime_ns

    before = stale()
    _git(checkout, "status", "--porcelain")
    assert index.stat().st_mtime_ns != before

    before = stale()
    assert has_uncommitted_changes(checkout) is False
    assert index.stat().st_mtime_ns == before
