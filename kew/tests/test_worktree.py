"""Tests for worktree management."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.narrow_checkout import (
    assert_narrow_checkout,
    make_narrow_checkout_source,
)

from kew import subprocess_utils, worktree
from kew.config import BranchConfig, DispatchConfig
from kew.worktree import (
    ConflictInfo,
    PublicationHeadMovedError,
    _repo_root,
    checkout_existing_worktree,
    claim_checkout,
    cleanup_worktree,
    detect_base_branch,
    dispatch_checkout_path,
    has_uncommitted_changes,
    is_linked_worktree,
    is_safe_standalone_clone,
    publish_existing_pr_branch,
    read_checkout_generation,
    release_checkout_claim,
    remote_branch_exists,
    reuse_or_restore_worktree,
    revoke_checkout_generation,
    setup_worktree,
    unpushed_commit_count,
    write_checkout_generation,
)


@pytest.fixture(autouse=True)
def _git_allowed():
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(*args, cwd) -> str:
    """Like ``_git`` but returns stdout — the isolation tests read git's answer."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _materialize_mock_clone(mock_run):
    """Let mocked Git create the directory that generation minting must stat."""

    def run(command, *args, **kwargs):
        if command[:2] == ["git", "clone"]:
            (Path(command[-1]) / ".git").mkdir(parents=True, exist_ok=True)
        if command[:4] == ["git", "rev-parse", "--verify", "--quiet"]:
            return MagicMock(returncode=0, stdout="a" * 40 + "\n")
        if command[:3] == ["git", "cat-file", "-e"]:
            return MagicMock(returncode=0)
        return mock_run.return_value

    mock_run.side_effect = run


def _git_argvs(mock_run: MagicMock) -> list[list[str]]:
    """The argv of every mocked Git call, and nothing else.

    Assertions must read argv alone: `str(call)` renders the keyword arguments
    too, so a call passing `env=` drags the whole process environment into the
    haystack. An iTerm `TERM_SESSION_ID` UUID holding "-D" was enough to fail
    "never deletes a branch" with no such call ever made (#2232).
    """
    return [list(call.args[0]) for call in mock_run.call_args_list if call.args]


def _ran_git(mock_run: MagicMock, *words: str) -> bool:
    """True when one mocked Git call's argv carries every one of `words`.

    Whole-element matching, never substrings: a directory path that happens to
    contain "branch" is not a `git branch`.
    """
    return any(all(word in argv for word in words) for argv in _git_argvs(mock_run))


def test_call_assertions_read_argv_not_the_environment() -> None:
    """#2232: the environment must never reach a command assertion.

    `git rev-parse --show-toplevel` runs with an explicit `env`, and a terminal
    session-id UUID contains "-D" about one session in four — which failed
    `TestCheckoutExistingWorktree` deterministically in those sessions. The
    matcher still has to see a real deletion.
    """
    mock_run = MagicMock()
    mock_run(
        ["git", "rev-parse", "--show-toplevel"],
        env={
            "TERM_SESSION_ID": "w0t0p0:ABCD-DEAD-BEEF",
            "PWD": "/tmp/test_never_deletes_a_branch0",
        },
    )
    assert not _ran_git(mock_run, "branch", "-D")

    mock_run(["git", "branch", "-D", "agent/issue-42"], cwd="/repo")
    assert _ran_git(mock_run, "branch", "-D")


def test_agent_visible_origin_strips_credentials() -> None:
    assert callable(getattr(worktree, "_agent_visible_origin", None))
    cases = (
        ("https://user:token@github.com/o/r.git", "https://github.com/o/r.git"),
        # Leading whitespace defeated the scheme match and let the credential
        # through unchanged (#1949 round 2); surrounding whitespace is dropped.
        (" https://user:password@host/o/r.git", "https://host/o/r.git"),
        ("\t https://u:p@host/r.git \n", "https://host/r.git"),
        (" ssh://user:pw@host/o/r.git", "ssh://user@host/o/r.git"),
        # A remote-helper prefix wraps an address sanitized on its own; the
        # inner whitespace that defeated the scheme match is dropped
        # (#1949 round 3).
        ("https:: https://user:password@host/r.git", "https::https://host/r.git"),
        ("https:: file://user:pw@localhost/r.git", "https::file://localhost/r.git"),
        ("helper::https://user:pw@host/r.git", "helper::https://host/r.git"),
        # An encoded password behind a literal "@" is still a password once
        # Git decodes the authority (#1949 round 4).
        ("ssh://user%3Apw@host:2222/r.git", "ssh://user@host:2222/r.git"),
        ("https://user%3Apw@host/r.git", "https://host/r.git"),
        # The rebuilt host keeps naming the destination Git would contact:
        # encoded characters in the host are re-encoded, never decoded away.
        ("https://user:pw@h%2540st/r.git", "https://h%2540st/r.git"),
        ("https://user:pw@[::1]:8443/r.git", "https://[::1]:8443/r.git"),
        ("/abs/path/user@host/repo.git", "/abs/path/user@host/repo.git"),
        # Git's SSH transports end the authority at the first slash: "#" and
        # "?" inside a password are not delimiters, and the git+ssh/ssh+git
        # aliases keep their username like ssh:// (#1949 round 5).
        ("ssh://user:pa#ss@host/r.git", "ssh://user@host/r.git"),
        ("ssh://user:pa?ss@host/r.git", "ssh://user@host/r.git"),
        ("git+ssh://git@host/r.git", "git+ssh://git@host/r.git"),
        ("git+ssh://user:pw@host:2222/r.git", "git+ssh://user@host:2222/r.git"),
        ("ssh+git://user:pw@host/r.git", "ssh+git://user@host/r.git"),
        ("GIT+SSH://u:p@host/r.git", "GIT+SSH://u@host/r.git"),
        # file and git are Git-native transports too: first-slash authority
        # (#1949 round 6).
        ("file://user:pa#ss@localhost/repo.git", "file://localhost/repo.git"),
        ("file://user:pa?ss@localhost/repo.git", "file://localhost/repo.git"),
        ("git://u:p@host/r.git", "git://host/r.git"),
        # file:/// is a local path: "@" there is a path character.
        ("file:///repos/user@host.git", "file:///repos/user@host.git"),
        # Git decodes %2F before splitting: the authority ends there, so this
        # URL has no userinfo and must NOT be rewritten toward newhost.
        (
            "ssh://oldhost:2222%2Fnamespace@newhost/repo.git",
            "ssh://oldhost:2222%2Fnamespace@newhost/repo.git",
        ),
        # An encoded slash after a real authority stays in the path, encoded.
        (
            "ssh://user:pw@host/dir%2Fwith@at/r.git",
            "ssh://user@host/dir%2Fwith@at/r.git",
        ),
        ("~/repos/user@host.git", "~/repos/user@host.git"),
        # Git percent-decodes the authority: encoded separators are the same
        # credential (#1949 round 3).
        ("ssh://user%3Apassword%40host/r.git", "ssh://user@host/r.git"),
        ("https://user%3Apw%40host:8443/r.git", "https://host:8443/r.git"),
        ("https://u%40ser:p%3Ass@host/o/r.git", "https://host/o/r.git"),
        # An encoded delimiter inside the userinfo is only a password
        # character: curl splits the raw authority first and decodes the
        # credential afterwards, so the host is unambiguous (#1949 round 8).
        ("https://user:pa%2Fss@host/o/r.git", "https://host/o/r.git"),
        ("https://user:pa%3Fss@host/o/r.git", "https://host/o/r.git"),
        ("https://user:pa%23ss@host/o/r.git", "https://host/o/r.git"),
        ("https://u%2Fser:pw@host:8443/r.git", "https://host:8443/r.git"),
        # curl never decodes a bracketed IPv6 literal: an encoded "@" inside a
        # zone identifier is host data, so the literal and its port survive
        # exactly as written, with and without a credential (#1949 round 9).
        (
            "https://u:p@[fe80::1%25eth%400]:8443/r.git",
            "https://[fe80::1%25eth%400]:8443/r.git",
        ),
        (
            "https://[fe80::1%25eth%400]:8443/r.git",
            "https://[fe80::1%25eth%400]:8443/r.git",
        ),
        ("https://user:pw@[fe80::1%25eth0]/r.git", "https://[fe80::1%25eth0]/r.git"),
        # curl also accepts a raw zone identifier ("%en0"); it stays as
        # written, with and without a credential (#1949 round 11).
        ("https://u:p@[fe80::1%en0]/repo.git", "https://[fe80::1%en0]/repo.git"),
        ("https://[fe80::1%en0]/repo.git", "https://[fe80::1%en0]/repo.git"),
        ("https://[fe80::1%3]:8443/repo.git", "https://[fe80::1%3]:8443/repo.git"),
        ("https://u%40ser:p%3Ass@[::1]/r.git", "https://[::1]/r.git"),
        # The native transports decode the whole URL first, like Git does.
        (
            "ssh://user:pw@[fe80::1%25eth0]:22/r.git",
            "ssh://user@[fe80::1%25eth0]:22/r.git",
        ),
        ("https://token@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("http://user:token@github.com/o/r.git", "http://github.com/o/r.git"),
        ("http://token@github.com/o/r.git", "http://github.com/o/r.git"),
        ("http://github.com/o/r.git", "http://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("ssh://git:pw@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("https://user:token@host:8443/o/r.git", "https://host:8443/o/r.git"),
        ("ssh://git:pw@host:2222/o/r.git", "ssh://git@host:2222/o/r.git"),
        (
            "HTTPS://u%40ser:p%3Ass@[::1]:8443/o/r.git?ref=@main#head",
            "HTTPS://[::1]:8443/o/r.git?ref=@main#head",
        ),
        ("ssh://git:pw@host/o/r.git?", "ssh://git@host/o/r.git?"),
        ("/local/repo.git", "/local/repo.git"),
        ("./user@host/repo.git", "./user@host/repo.git"),
        ("../user@host/repo.git", "../user@host/repo.git"),
        ("git@[::1]:o/r.git", "git@[::1]:o/r.git"),
        ("git@host:/o/r.git", "git@host:/o/r.git"),
        ("git@host:o/r.git \t\n", "git@host:o/r.git"),
        ("./local/repo.git \t\n", "./local/repo.git"),
        ("https://@host/o/r.git", "https://host/o/r.git"),
        ("https://host/o/@r.git?ref=@main", "https://host/o/@r.git?ref=@main"),
        ("ssh://git:pw@[::1]:2222/o/r.git", "ssh://git@[::1]:2222/o/r.git"),
        ("SSH://u%40ser:p%3Ass@host/r.git", "SSH://u%40ser@host/r.git"),
        ("https://u:p@host/r.git \t\n", "https://host/r.git"),
        ("", ""),
        (
            "https::https://user:password@host/o/r.git",
            "https::https://host/o/r.git",
        ),
        ("ftp://user:password@host/o/r.git", "ftp://host/o/r.git"),
        ("git+custom://u:p@host/r.git", "git+custom://host/r.git"),
        ("helper_1::https://u:p@host/r.git", "helper_1::https://host/r.git"),
        ("helper::https://host/r.git", "helper::https://host/r.git"),
        ("ftp://host/r.git", "ftp://host/r.git"),
        ("file:///local/repo.git", "file:///local/repo.git"),
        ("helper::ssh://git:pw@host/r.git", "helper::ssh://git@host/r.git"),
        (
            "FTP://u%40ser:p%3Ass@[::1]:2121/o/@r.git?q=@main#frag",
            "FTP://[::1]:2121/o/@r.git?q=@main#frag",
        ),
        (
            "helper::https://u:pass@word@host:8443/o/@r.git?q=@main#frag",
            "helper::https://host:8443/o/@r.git?q=@main#frag",
        ),
        ("helper::https://@host/r.git", "helper::https://host/r.git"),
        ("ftp://u:p@host/r.git \t\n", "ftp://host/r.git"),
        ("ftp://u:p@host \t\n", "ftp://host"),
    )
    for url, expected in cases:
        assert worktree._agent_visible_origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "user:password@host/o/r.git",
        "user@host",
        "helper::user:password@host/o/r.git",
        "user:password@host/o/r.git \t\n",
        " user:password@host/o/r.git",
        "\n helper::user:password@host/o/r.git",
        # A helper prefix wrapping a schemeless credential refuses like the
        # bare form (#1949 round 3).
        "https:: user:password@host/r.git",
        # Wrappers that break scheme detection refuse instead of passing the
        # credential through (#1949 round 4).
        '"https://user:password@host/r.git"',
        "helper ::https://user:password@host/r.git",
        "relative/repo@host.git",
        "unparsed/path@data",
        # A percent-escape that is not UTF-8 cannot be rebuilt faithfully.
        "ssh://user:pw@h%FFst/r.git",
        # curl splits before decoding: an encoded delimiter inside an https
        # authority is ambiguous and refuses (#1949 round 6).
        "https://user:pw@host%2Fx/r.git",
        "https://u:p@host%3Fq/r.git",
        # An empty curl authority puts the credential after the third slash
        # (#1949 round 7).
        "https:///user:pw@host/r.git",
        "http:///user:pw@host/r.git",
        "helper::https:///user:pw@host/r.git",
        # An authority that is only userinfo would rebuild as a three-slash
        # URL and move the credential behind it (#1949 round 8).
        "https://u:p@/user:password@host/r.git",
        "https://user:password@/r.git",
        "http://user:password@/user:password@host/r.git",
        "ssh://user:password@/r.git",
        "git://user:password@/r.git",
        "helper::https://u:p@/user:password@host/r.git",
        # Brackets anywhere but as the whole curl host literal refuse
        # (#1949 round 9).
        "https://u[x]:p@host/r.git",
        "https://u:p@[::1/r.git",
        "https://u:p@[::1]x/r.git",
        "https://u:p@x[::1]/r.git",
        "https://u:p@[::1]:8443@host/r.git",
        # Raw userinfo inside the brackets: Git's credential parser reads
        # "[user" as the username and "host]" as the host (#1949 round 10).
        "https://[user:password@host]/r.git",
        "http://[user:password@host]/r.git",
        "https://u:p@[fe80::1%25eth@0]/r.git",
        "https://[::1@host]/r.git",
        "https://[host.example]/r.git",
        "https://[v1.fe80::1]/r.git",
    ],
)
def test_agent_visible_origin_refuses_unparseable_userinfo(url: str) -> None:
    with pytest.raises(RuntimeError) as error:
        worktree._agent_visible_origin(url)

    message = str(error.value)
    assert message.startswith("cannot prepare checkout: unparseable origin userinfo")
    assert "password" not in message  # never echo the credential
    assert url not in str(error.value)


def _bootstrap_git(repo: Path, *args: str) -> str:
    """Run real Git through kew's subprocess boundary."""
    return subprocess_utils.run(["git", *args], cwd=repo).stdout.strip()


@pytest.mark.parametrize(
    "origin",
    [
        "user:password@host/repo.git",
        "https:: user:password@host/repo.git",
        "https:///user:password@host/repo.git",
        "https://u:p@/user:password@host/repo.git",
        "https://[user:password@host]/repo.git",
    ],
    ids=[
        "schemeless",
        "helper-schemeless",
        "three-slash-https",
        "empty-host-https",
        "bracket-userinfo-https",
    ],
)
def test_clone_refuses_unparseable_origin_before_creation(
    tmp_path: Path, origin: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _bootstrap_git(source, "init", "-q")
    _bootstrap_git(source, "remote", "add", "origin", origin)
    checkout = tmp_path / "agent"

    with pytest.raises(RuntimeError, match="unparseable origin userinfo"):
        worktree.clone_local_checkout(source, checkout, "HEAD")

    assert not checkout.exists()


@pytest.mark.parametrize(
    "form",
    ["plain", "leading-space", "percent-encoded", "encoded-password", "hash-password"],
)
def test_clone_local_checkout_strips_origin_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    prefix = " " if form == "leading-space" else ""
    userinfo = {
        "percent-encoded": "user%3Apassword%40",
        "encoded-password": "user%3Apassword@",
        "hash-password": "user:pa#ss@",
    }.get(form, "user:password@")
    source = tmp_path / "source"
    source.mkdir()
    _bootstrap_git(source, "init", "-q", "-b", "main")
    _bootstrap_git(source, "config", "user.name", "Test")
    _bootstrap_git(source, "config", "user.email", "test@example.test")
    tree = _bootstrap_git(source, "mktree")
    head = _bootstrap_git(source, "commit-tree", tree, "-m", "base")
    _bootstrap_git(source, "update-ref", "refs/heads/main", head)
    origin = tmp_path / "origin.git"
    _bootstrap_git(tmp_path, "clone", "--bare", "-q", str(source), str(origin))
    upstream = f"{prefix}file://{userinfo}localhost{origin}"
    _bootstrap_git(source, "remote", "add", "origin", upstream)
    checkout = tmp_path / "agent"
    monkeypatch.chdir(source)
    if form != "plain":
        # Git cannot fetch through a URL with a leading space, so exercise the
        # seam that writes the origin on its own: the round-2 review's bypass
        # was exactly this path (both commits already local, no bootstrap
        # fetch, origin written verbatim with the credential).
        worktree.clone_local_checkout(source, checkout, head)
    else:
        worktree._clone_checkout_on(checkout, "agent/test", start_point="main")

    agent_origin = _bootstrap_git(checkout, "remote", "get-url", "origin")
    assert agent_origin == f"file://localhost{origin}"
    assert "@" not in agent_origin
    if form == "plain":
        # The bootstrap fetch ran through the operator repository.
        assert _bootstrap_git(checkout, "rev-parse", "FETCH_HEAD") == head
    assert _bootstrap_git(checkout, "rev-parse", "HEAD") == head
    # The operator origin is untouched (the helper strips Git's output, so a
    # leading space in the configured value does not show here).
    assert _bootstrap_git(source, "remote", "get-url", "origin") == upstream.strip()


@pytest.mark.parametrize("packed", [False, True], ids=["loose", "bitmap-pack"])
def test_clone_local_checkout_excludes_unrelated_and_unreachable_objects(
    tmp_path: Path, packed: bool
) -> None:
    source = make_narrow_checkout_source(tmp_path, packed=packed)
    checkout = tmp_path / "narrow-checkout"
    worktree.clone_local_checkout(source.repo, checkout, "main")
    worktree.fetch_ref(checkout, "main", repo_root=source.repo)
    assert_narrow_checkout(
        source,
        checkout,
        required_refs=("main",),
        expected_refs={"refs/remotes/origin/main"},
    )


def _origin_repo_with_branch(tmp_path: Path, branch: str) -> Path:
    """A bare 'origin' with `branch` pushed, and a working clone to run from."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "f.txt").write_text("base")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    # the loop's work branch, pushed
    _git("checkout", "-qb", branch, cwd=work)
    (work / "work.txt").write_text("loop work")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "loop work", cwd=work)
    _git("push", "-q", "origin", branch, cwd=work)
    # Detach HEAD so neither 'main' nor the loop branch is locked by the
    # working tree — both must be addable as worktrees in the tests below.
    _git("checkout", "-q", "--detach", cwd=work)
    return work


def _cfg(work: Path, tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.branches.prefix = "agent/issue-"  # the real production default
    cfg.branches.worktree_base = str(tmp_path / "wt")
    return cfg


@pytest.mark.parametrize(
    ("issue_num", "item_id", "provider", "expected"),
    [
        (1, None, "github", "issue-1"),
        (101, None, "github", "issue-101"),
        (1010, None, "github", "issue-1010"),
        (2020, None, "github", "issue-2020"),
        (0, "SFD-2", "linear", "issue-sfd-2"),
    ],
)
def test_default_worktree_paths_are_per_test_and_support_real_issue_ids(
    tmp_path: Path,
    issue_num: int,
    item_id: str | None,
    provider: str,
    expected: str,
) -> None:
    config = DispatchConfig()

    path = dispatch_checkout_path(
        config,
        issue_num=issue_num,
        item_id=item_id,
        provider=provider,
    )

    assert path == tmp_path / "kew-worktrees" / "agent" / expected


def test_claim_release_keeps_one_stable_lock_inode(tmp_path: Path) -> None:
    config = DispatchConfig()
    checkout = dispatch_checkout_path(config, issue_num=104)
    lock = checkout.parent / f"{checkout.name}.lock"
    try:
        claim_checkout(checkout)
        first_inode = lock.stat().st_ino
        release_checkout_claim(checkout)

        claim_checkout(checkout)

        assert lock.stat().st_ino == first_inode
        assert lock.read_text(encoding="utf-8").splitlines()[-2:] == [
            str(os.getpid()),
            str(os.getpid()),
        ]
    finally:
        release_checkout_claim(checkout)


def test_a_lock_the_sweep_unlinks_between_open_and_flock_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-review's TOCTOU attack, made deterministic at the ``flock`` call.

    The work-root sweep removes the free lock file after the claimer opened it
    and before the claimer's ``flock``. The claimer must end up holding the
    file at the lock's path, so a second claimer is refused, not let in on a
    fresh file beside an orphaned inode.
    """
    import fcntl

    from kew import work_dirs

    checkout = tmp_path / "kew-sandbox-5-x"
    lock = checkout.parent / f"{checkout.name}.lock"
    real_flock = fcntl.flock
    swept: list[bool] = []

    def flock_after_a_sweep(fd: int, operation: int) -> None:
        # The first flock is the claimer's: the sweep runs just before it,
        # with the real flock (its own call must not re-enter this one).
        if not swept:
            monkeypatch.setattr(fcntl, "flock", real_flock)
            swept.append(work_dirs._remove_lock(lock))
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock_after_a_sweep)
    try:
        claim_checkout(checkout)
        monkeypatch.setattr(fcntl, "flock", real_flock)
        with open(lock, "a+", encoding="utf-8") as second, pytest.raises(OSError):
            fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        monkeypatch.setattr(fcntl, "flock", real_flock)
        release_checkout_claim(checkout)
    assert swept == [True]


def _make_checkout_shape(path: Path, shape: str) -> None:
    """Materialize one checkout metadata shape for safety tests."""
    path.mkdir(parents=True)
    if shape == "missing":
        return
    if shape == "bare":
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        (path / "objects").mkdir()
        return
    if shape == "file":
        (path / ".git").write_text("gitdir: ../operator.git/worktrees/test\n")
        return
    if shape == "symlink":
        operator_git = path.parent / "operator.git"
        operator_git.mkdir()
        (operator_git / "HEAD").write_text("ref: refs/heads/main\n")
        (operator_git / "objects").mkdir()
        (path / ".git").symlink_to(operator_git, target_is_directory=True)
        return

    dot_git = path / ".git"
    dot_git.mkdir()
    (dot_git / "HEAD").write_text("ref: refs/heads/main\n")
    (dot_git / "objects").mkdir()
    if shape == "commondir":
        (dot_git / "commondir").write_text("../../operator.git\n")
    elif shape == "alternates":
        outside = path.parent / "outside-objects"
        outside.mkdir()
        info = dot_git / "objects" / "info"
        info.mkdir()
        (info / "alternates").write_text(f"{outside}\n")


@pytest.mark.parametrize(
    "shape", ["file", "symlink", "missing", "bare", "alternates", "commondir"]
)
def test_unsafe_checkout_metadata_shapes_are_rejected(
    tmp_path: Path, shape: str
) -> None:
    checkout = tmp_path / "checkout"
    _make_checkout_shape(checkout, shape)

    assert not is_safe_standalone_clone(checkout)
    assert is_linked_worktree(checkout) is (shape == "file")


def test_healthy_standalone_clone_metadata_is_accepted(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _make_checkout_shape(checkout, "healthy")

    assert is_safe_standalone_clone(checkout)
    assert not is_linked_worktree(checkout)


def test_repo_root_ignores_ambient_git_repository_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _origin_repo_with_branch(tmp_path / "operator", "agent/issue-7")
    decoy = _origin_repo_with_branch(tmp_path / "decoy", "agent/issue-7")
    monkeypatch.chdir(source)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    assert _repo_root() == source


@pytest.mark.parametrize(
    "shape", ["symlink", "missing", "bare", "alternates", "commondir"]
)
def test_resume_refuses_unsafe_checkout_before_git_discovery(
    tmp_path: Path, shape: str
) -> None:
    cfg = _cfg(tmp_path, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    checkout = Path(cfg.branches.worktree_base) / "agent" / "issue-7"
    _make_checkout_shape(checkout, shape)
    marker = checkout / "preserve.txt"
    marker.write_text("preserve me")

    with (
        patch("kew.worktree.run") as git_run,
        patch("kew.worktree.hold_checkout_generation"),
        pytest.raises(RuntimeError, match="standalone clone"),
    ):
        reuse_or_restore_worktree(
            issue_num=7,
            config=cfg,
            expected_generation="recorded",
        )

    git_run.assert_not_called()
    assert marker.read_text() == "preserve me"


@pytest.mark.parametrize(
    "shape", ["symlink", "missing", "bare", "alternates", "commondir"]
)
def test_setup_refuses_unsafe_checkout_before_git_discovery(
    tmp_path: Path, shape: str
) -> None:
    cfg = _cfg(tmp_path, tmp_path)
    checkout = Path(cfg.branches.worktree_base) / "agent" / "issue-7"
    _make_checkout_shape(checkout, shape)
    marker = checkout / "preserve.txt"
    marker.write_text("preserve me")

    with (
        patch("kew.worktree.run") as git_run,
        pytest.raises(RuntimeError, match="standalone clone"),
    ):
        setup_worktree(issue_num=7, config=cfg)

    git_run.assert_not_called()
    assert marker.read_text() == "preserve me"


def test_reuse_existing_linked_worktree_is_refused_and_preserved(tmp_path, monkeypatch):
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    wt = Path(cfg.branches.worktree_base) / "agent" / "issue-7"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt), "agent/issue-7"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    ahead = wt / "ahead.txt"
    ahead.write_text("local commit")
    _git("add", "ahead.txt", cwd=wt)
    _git("commit", "-qm", "local only", cwd=wt)
    head_before = _git_out("rev-parse", "HEAD", cwd=wt)
    marker = wt / "dirty.txt"
    marker.write_text("preserve me")

    with (
        patch("kew.worktree.hold_checkout_generation"),
        pytest.raises(RuntimeError) as raised,
    ):
        reuse_or_restore_worktree(
            issue_num=7, config=cfg, expected_generation="recorded"
        )

    message = str(raised.value)
    assert str(wt) in message
    assert "push the branch or copy the directory, then remove it and resume" in message
    assert wt.exists()
    assert (wt / "work.txt").read_text() == "loop work"
    assert _git_out("rev-parse", "HEAD", cwd=wt) == head_before
    assert _git_out("rev-list", "--count", "origin/agent/issue-7..HEAD", cwd=wt) == "1"
    assert marker.read_text() == "preserve me"


def test_reuse_wrong_branch_restores(tmp_path, monkeypatch):
    # Dir exists but is checked out to the WRONG branch (main) -> must NOT be
    # blindly reused (would give the gate a stale tree); remove + restore from
    # origin/agent/issue-7. (Covers adversarial-review Critical: verify branch.)
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    wt = Path(cfg.branches.worktree_base) / "agent" / "issue-7"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "-q", str(work), str(wt)],
        check=True,
        capture_output=True,
    )
    _git("checkout", "-q", "main", cwd=wt)
    out = reuse_or_restore_worktree(
        issue_num=7, config=cfg, expected_generation=write_checkout_generation(wt)
    )
    assert out == wt
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "agent/issue-7"
    assert (wt / "work.txt").read_text() == "loop work"


def test_restore_from_pushed_branch_when_missing(tmp_path, monkeypatch):
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    # No local worktree dir yet -> restore from origin/agent/issue-7.
    out = reuse_or_restore_worktree(
        issue_num=7, config=cfg, expected_generation="previous-generation"
    )
    assert out.exists()
    assert (out / "work.txt").read_text() == "loop work"


def test_governed_round2_legacy_token_sidecar_reads_unbound_and_is_adopted(
    tmp_path,
):
    """A sidecar from a build before complete evidence (one bare token) reads
    as unbound rather than refusing, and only an adopting caller binds it;
    preparing a checkout for a new owner still refuses (governed review
    round 2, 2026-09-21)."""
    import json

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    token = "a" * 32
    sidecar = worktree._generation_sidecar(checkout)
    sidecar.write_text(token + "\n")
    with worktree.checkout_claim_guard(checkout):
        assert read_checkout_generation(checkout) == ""
        assert sidecar.read_text().strip() == token
        # Replacement authority is unchanged: capture still refuses (round 21).
        with pytest.raises(
            worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
        ):
            worktree.capture_checkout_generation(checkout)
        assert sidecar.read_text().strip() == token

        evidence = worktree.adopt_legacy_checkout_generation(checkout)
        binding = json.loads(evidence)
        assert binding["token"] != token
        assert binding["nonce"] == binding["token"]
        assert json.loads(sidecar.read_text()) == binding
        nonce = checkout / ".git" / "kew-generation"
        assert nonce.read_text().strip() == binding["token"]
        assert read_checkout_generation(checkout) == evidence
        assert worktree.capture_checkout_generation(checkout) == evidence
        assert worktree.adopt_legacy_checkout_generation(checkout) == evidence


def test_governed_round3_a_restore_drops_the_pin_on_the_replaced_checkout(
    tmp_path, monkeypatch
):
    """The claim lease pins the admitted directory by descriptor and evidence.

    A restore replaces that directory, so a pin left in place refused every
    later identity boundary on the restored checkout and broke the legacy loop
    resume (governed review round 3, 2026-09-21).
    """
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    checkout = dispatch_checkout_path(cfg, issue_num=7)
    claim_checkout(checkout)

    # An existing checkout on another branch, pinned by this thread's lease.
    out = setup_worktree(issue_num=7, config=cfg)
    _git("checkout", "-b", "agent/other", cwd=out)
    generation = worktree.read_checkout_generation(out)
    worktree.hold_checkout_generation(out, generation)
    assert worktree._checkout_claims[out.absolute()].binding is not None

    restored = reuse_or_restore_worktree(
        issue_num=7, config=cfg, expected_generation=generation
    )

    assert worktree._checkout_claims[restored.absolute()].binding is None
    # Every later identity boundary reads the restored checkout, not the old one.
    fresh = worktree.capture_checkout_generation(restored)
    worktree.require_checkout_generation(restored, fresh)
    assert worktree.read_checkout_generation(restored) == fresh


def test_restore_reports_the_new_checkout_for_durable_rebinding(tmp_path, monkeypatch):
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    restored = []

    out = reuse_or_restore_worktree(
        issue_num=7,
        config=cfg,
        expected_generation="a" * 32,
        on_restore=restored.append,
    )

    assert restored == [out]


def test_resume_refuses_a_replaced_checkout_generation_before_git(
    tmp_path, monkeypatch
):
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=7))
    checkout = reuse_or_restore_worktree(
        issue_num=7, config=cfg, expected_generation="previous-generation"
    )
    recorded_generation = read_checkout_generation(checkout)
    revoke_checkout_generation(checkout)
    replacement_generation = write_checkout_generation(checkout)
    assert replacement_generation != recorded_generation

    with (
        patch("kew.worktree.run") as git_run,
        pytest.raises(RuntimeError, match="generation"),
    ):
        reuse_or_restore_worktree(
            issue_num=7,
            config=cfg,
            expected_generation=recorded_generation,
        )

    git_run.assert_not_called()


def test_fail_closed_when_unrestorable(tmp_path, monkeypatch):
    work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
    monkeypatch.chdir(work)
    cfg = _cfg(work, tmp_path)
    claim_checkout(dispatch_checkout_path(cfg, issue_num=999))
    with pytest.raises(RuntimeError, match="restore"):
        reuse_or_restore_worktree(
            issue_num=999, config=cfg, expected_generation="previous-generation"
        )  # no branch, no worktree


class TestDetectBaseBranch:
    @patch("kew.worktree.run")
    def test_detects_from_origin_head(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="refs/remotes/origin/main\n"
        )
        assert detect_base_branch("") == "main"

    @patch("kew.worktree.run")
    def test_falls_back_to_main(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert detect_base_branch("") == "main"

    def test_config_override(self):
        assert detect_base_branch("develop") == "develop"

    def test_a_named_repository_answers_for_itself_not_the_shell(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A host-side read about the operator repository names it (#2177).

        ``kew publication attest`` decides which merges bind against this base,
        so the answer must come from the operator repository — not from whatever
        repository the operator's shell, or an ambient ``GIT_DIR``, points at.
        """
        operator = _origin_repo_with_branch(tmp_path / "operator", "agent/issue-7")
        _git("push", "-q", "origin", "main:release", cwd=operator)
        _git("fetch", "-q", "origin", cwd=operator)
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/release",
            cwd=operator,
        )
        decoy = _origin_repo_with_branch(tmp_path / "decoy", "agent/issue-7")
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=decoy,
        )
        monkeypatch.chdir(decoy)
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        assert detect_base_branch("", repo_root=operator) == "release"
        # Unpinned detection stays what dispatch, which runs from the
        # repository it dispatches for, already relies on.
        assert detect_base_branch("") == "main"


class TestOperatorBaseBranch:
    """`kew publication attest` reads the base from the operator repo (#2177)."""

    def test_it_detects_in_the_operator_repository_not_the_ambient_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from kew.worktree import operator_base_branch

        operator = _origin_repo_with_branch(tmp_path / "operator", "agent/issue-7")
        _git("push", "-q", "origin", "main:release", cwd=operator)
        _git("fetch", "-q", "origin", cwd=operator)
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/release",
            cwd=operator,
        )
        decoy = _origin_repo_with_branch(tmp_path / "decoy", "agent/issue-7")
        _git(
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=decoy,
        )
        monkeypatch.chdir(operator)
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        assert operator_base_branch("") == "release"

    def test_a_configured_base_answers_without_a_git_call(self, monkeypatch) -> None:
        """The attest path runs before any repository read is needed."""
        from kew.worktree import operator_base_branch

        def refuse(*args, **kwargs):
            raise AssertionError("a configured base needs no git call")

        monkeypatch.setattr("kew.worktree.run", refuse)

        assert operator_base_branch("develop") == "develop"


class TestSetupWorktree:
    @staticmethod
    def _clone_existing_checkout(source: Path, checkout: Path) -> None:
        checkout.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(source), str(checkout)],
            check=True,
            capture_output=True,
        )
        _git("config", "user.email", "t@e", cwd=checkout)
        _git("config", "user.name", "T", cwd=checkout)

    def _checkout_commit_on_attacker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        marker_name: str,
    ) -> tuple[Path, DispatchConfig, Path, Path, str, str]:
        """Create work that only an attacker remote advertises."""
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        marker = checkout / marker_name
        marker.write_text("only the attacker remote has this commit\n")
        _git("add", marker.name, cwd=checkout)
        _git("commit", "-qm", "checkout-only commit", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        operator_origin = _git_out("remote", "get-url", "origin", cwd=source)

        attacker_origin = tmp_path / f"{marker.stem}.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(attacker_origin)], check=True
        )
        _git(
            "push",
            "-q",
            str(attacker_origin),
            "HEAD:refs/heads/agent/issue-42",
            cwd=checkout,
        )
        return (
            source,
            config,
            checkout,
            attacker_origin,
            operator_origin,
            head_before,
        )

    @patch("kew.worktree._repo_root")
    @patch("kew.worktree.run")
    @patch("kew.worktree.detect_base_branch", return_value="main")
    def test_creates_worktree(
        self,
        mock_detect: MagicMock,
        mock_run: MagicMock,
        mock_repo_root: MagicMock,
        tmp_path: Path,
    ):
        mock_repo_root.return_value = tmp_path
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        result = setup_worktree(issue_num=42, config=config)

        # Fetch, then an isolated CLONE — never `git worktree add`.
        assert _ran_git(mock_run, "fetch")
        assert _ran_git(mock_run, "clone")
        assert not _ran_git(mock_run, "worktree", "add")
        assert "issue-42" in str(result)

    @patch("kew.worktree._repo_root")
    @patch("kew.worktree.run")
    @patch("kew.worktree.detect_base_branch", return_value="main")
    def test_item_id_slugs_branch_and_path(
        self,
        mock_detect: MagicMock,
        mock_run: MagicMock,
        mock_repo_root: MagicMock,
        tmp_path: Path,
    ):
        mock_repo_root.return_value = tmp_path
        # A5a: a Linear-style item id produces a slugged (lowercase) branch
        # and worktree dir; issue_num is ignored for path/branch construction.
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        result = setup_worktree(
            issue_num=0, config=config, item_id="SFD-12", provider="linear"
        )
        assert "issue-sfd-12" in str(result)
        assert any("agent/issue-sfd-12" in str(c) for c in mock_run.call_args_list)

    @patch("kew.worktree._repo_root")
    @patch("kew.worktree.run")
    @patch("kew.worktree.detect_base_branch", return_value="main")
    def test_github_item_id_byte_identical(
        self,
        mock_detect: MagicMock,
        mock_run: MagicMock,
        mock_repo_root: MagicMock,
        tmp_path: Path,
    ):
        mock_repo_root.return_value = tmp_path
        # A5a golden: item_id="42" yields the same dir as the legacy path.
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        with_id = setup_worktree(issue_num=42, config=config, item_id="42")
        worktree.remove_checkout_tree(with_id)
        legacy = setup_worktree(issue_num=42, config=config)
        assert with_id == legacy
        assert "issue-42" in str(with_id)

    def test_rejects_uncanonical_item_id(self, tmp_path: Path):
        # A5a path-safety: a non-canonical id (path traversal) is rejected at
        # the boundary, never sanitized into a worktree path.
        from kew.ids import InvalidItemIdError

        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        with pytest.raises(InvalidItemIdError):
            setup_worktree(issue_num=0, config=config, item_id="../../etc")

    def test_refuses_to_replace_dirty_existing_worktree(self, tmp_path: Path):
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        worktree = tmp_path / "agent" / "issue-42"
        worktree.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "init",
                "--initial-branch",
                "agent/issue-42",
            ],
            check=True,
            capture_output=True,
        )
        (worktree / "wip.bin").write_bytes(b"keep this byte\x00\n")

        with pytest.raises(RuntimeError, match="dirty worktree"):
            setup_worktree(issue_num=42, config=config)

        assert (worktree / "wip.bin").read_bytes() == b"keep this byte\x00\n"

    def test_clone_with_commits_and_no_upstream_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "push",
            "-q",
            "origin",
            "main:refs/heads/agent/issue-42",
            cwd=source,
        )
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        committed = checkout / "committed.txt"
        committed.write_text("unique committed work\n")
        _git("add", "committed.txt", cwd=checkout)
        _git("commit", "-qm", "local only", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        upstream = subprocess.run(
            ["git", "rev-parse", "--verify", "@{upstream}"],
            cwd=checkout,
            capture_output=True,
        )
        assert upstream.returncode != 0

        with pytest.raises(RuntimeError, match=r"1 commit"):
            setup_worktree(issue_num=42, config=config)

        assert committed.read_text() == "unique committed work\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_clone_missing_operator_origin_head_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "-qb",
            "agent/issue-42",
            "main",
            cwd=source,
        )
        remote_only = source / "remote-only.txt"
        remote_only.write_text("the existing checkout lacks this object\n")
        _git("add", remote_only.name, cwd=source)
        _git("commit", "-qm", "remote branch moved", cwd=source)
        _git("push", "-q", "origin", "agent/issue-42", cwd=source)
        marker = checkout / ".git" / "preservation-probe"
        marker.write_text("keep this checkout\n")
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert marker.read_text() == "keep this checkout\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_clone_whose_head_is_only_on_another_origin_branch_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        old_metadata = checkout / ".git" / "replacement-probe"
        old_metadata.write_text("old checkout\n")
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert old_metadata.read_text() == "old checkout\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_stale_origin_ref_after_remote_deletion_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        origin_url = _git_out("remote", "get-url", "origin", cwd=source)
        _git("remote", "set-url", "origin", origin_url, cwd=checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/agent/issue-7",
            cwd=checkout,
        )
        committed = checkout / "work.txt"
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        _git("push", "-q", "origin", "agent/issue-42", cwd=checkout)
        _git(
            "fetch",
            "-q",
            "origin",
            "agent/issue-42:refs/remotes/origin/agent/issue-42",
            cwd=checkout,
        )
        _git("push", "-q", "origin", "--delete", "agent/issue-42", cwd=source)
        assert (
            _git_out(
                "rev-parse",
                "--verify",
                "refs/remotes/origin/agent/issue-42",
                cwd=checkout,
            )
            == head_before
        )

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert committed.read_text() == "loop work"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_operator_origin_query_failure_preserves_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        marker = checkout / ".git" / "preservation-probe"
        marker.write_text("keep this checkout\n")
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        _git(
            "remote",
            "set-url",
            "origin",
            str(tmp_path / "missing-origin.git"),
            cwd=source,
        )

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert marker.read_text() == "keep this checkout\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_repointed_checkout_origin_cannot_authorize_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        marker = checkout / "remote-only.txt"
        marker.write_text("only the attacker remote has this commit\n")
        _git("add", marker.name, cwd=checkout)
        _git("commit", "-qm", "checkout-only commit", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)

        attacker_origin = tmp_path / "attacker.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(attacker_origin)], check=True
        )
        _git("remote", "set-url", "origin", str(attacker_origin), cwd=checkout)
        _git("push", "-q", "origin", "HEAD:refs/heads/agent/issue-42", cwd=checkout)

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert marker.read_text() == "only the attacker remote has this commit\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_ambient_origin_override_cannot_authorize_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        marker = checkout / "ambient-origin-only.txt"
        marker.write_text("only the ambient origin has this commit\n")
        _git("add", marker.name, cwd=checkout)
        _git("commit", "-qm", "checkout-only commit", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)

        attacker_origin = tmp_path / "ambient-origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(attacker_origin)], check=True
        )
        _git("remote", "set-url", "origin", str(attacker_origin), cwd=checkout)
        _git("push", "-q", "origin", "HEAD:refs/heads/agent/issue-42", cwd=checkout)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(attacker_origin))

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert marker.read_text() == "only the ambient origin has this commit\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_global_include_if_cannot_rewrite_operator_origin_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (
            source,
            config,
            checkout,
            attacker_origin,
            operator_origin,
            head_before,
        ) = self._checkout_commit_on_attacker(
            tmp_path, monkeypatch, "global-include-only.txt"
        )
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        included = fake_home / "transport-rewrite.conf"
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(included),
                f"url.{attacker_origin}.insteadOf",
                operator_origin,
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(fake_home / ".gitconfig"),
                f"includeIf.gitdir:{source / '.git'}.path",
                str(included),
            ],
            check=True,
        )
        monkeypatch.setenv("HOME", str(fake_home))

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        marker = checkout / "global-include-only.txt"
        assert marker.read_text() == "only the attacker remote has this commit\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_worktree_config_cannot_rewrite_operator_origin_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (
            source,
            config,
            checkout,
            attacker_origin,
            operator_origin,
            head_before,
        ) = self._checkout_commit_on_attacker(
            tmp_path, monkeypatch, "worktree-config-only.txt"
        )
        _git("config", "extensions.worktreeConfig", "true", cwd=source)
        _git(
            "config",
            "--worktree",
            f"url.{attacker_origin}.insteadOf",
            operator_origin,
            cwd=source,
        )

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        marker = checkout / "worktree-config-only.txt"
        assert marker.read_text() == "only the attacker remote has this commit\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_relative_operator_origin_is_anchored_at_the_repository(
        self, tmp_path: Path
    ) -> None:
        """``../origin.git`` must resolve against the repository, not the query dir.

        The isolated query runs from a temporary directory; without anchoring,
        a relative origin never resolves and a clean checkout stays unverifiable.
        """
        from kew.worktree import _operator_remote_branch_head

        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        _git("remote", "set-url", "origin", "../origin.git", cwd=source)
        assert _git_out("config", "--get", "remote.origin.url", cwd=source) == (
            "../origin.git"
        )
        expected = _git_out("rev-parse", "origin/agent/issue-7", cwd=source)

        assert _operator_remote_branch_head(source, "agent/issue-7") == expected

    @pytest.mark.parametrize(
        ("origin", "anchored"),
        [
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
            ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
            ("file:///srv/r.git", "file:///srv/r.git"),
            ("/srv/r.git", "/srv/r.git"),
        ],
    )
    def test_non_relative_origins_pass_through_unchanged(
        self, tmp_path: Path, origin: str, anchored: str
    ) -> None:
        from kew.worktree import _anchor_operator_origin

        assert _anchor_operator_origin(tmp_path, origin) == anchored

    def test_operator_origin_query_carries_only_kew_https_auth(
        self, tmp_path: Path
    ) -> None:
        """The isolated query keeps kew's own auth header and nothing inherited.

        Dropping every ``GIT_*`` also dropped the credential helpers a private
        HTTPS origin needs, so a clean pushed checkout could never be replaced.
        Only the ``http.<host>.extraHeader`` entry kew adds crosses over; an
        inherited entry before it (an attacker's ``remote.origin.url``) does not.
        """
        from kew.worktree import _operator_remote_branch_head

        head = "a" * 40
        listed = subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/agent/issue-7\n", ""
        )
        auth_env = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.origin.url",
            "GIT_CONFIG_VALUE_0": "/attacker/repository.git",
            "GIT_CONFIG_KEY_1": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_1": "Authorization: Basic c2VjcmV0",
            "GIT_TERMINAL_PROMPT": "0",
        }
        with (
            patch(
                "kew.worktree._operator_origin_url",
                return_value="https://github.com/o/r.git",
            ),
            patch("kew.worktree.publication_git_env", return_value=auth_env),
            patch("kew.worktree.run", return_value=listed) as run_mock,
        ):
            assert _operator_remote_branch_head(tmp_path, "agent/issue-7") == head

        kwargs = run_mock.call_args.kwargs
        env = kwargs["env"]
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "http.followRedirects"
        assert env["GIT_CONFIG_VALUE_0"] == "false"
        assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraHeader"
        assert env["GIT_CONFIG_VALUE_1"] == "Authorization: Basic c2VjcmV0"
        assert "GIT_CONFIG_KEY_2" not in env
        assert "/attacker/repository.git" not in env.values()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert kwargs["timeout"] == 60.0

    def test_operator_origin_query_without_a_token_carries_no_config(
        self, tmp_path: Path
    ) -> None:
        from kew.worktree import _operator_remote_branch_head

        head = "b" * 40
        listed = subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/agent/issue-7\n", ""
        )
        with (
            patch(
                "kew.worktree._operator_origin_url",
                return_value="https://github.com/o/r.git",
            ),
            patch("kew.worktree.publication_git_env", return_value=None),
            patch("kew.worktree.run", return_value=listed) as run_mock,
        ):
            assert _operator_remote_branch_head(tmp_path, "agent/issue-7") == head

        env = run_mock.call_args.kwargs["env"]
        # Only the redirect guard: no header, no token.
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.followRedirects"
        assert "GIT_CONFIG_KEY_1" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_operator_origin_query_drops_inherited_askpass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git runs an askpass program before GIT_TERMINAL_PROMPT=0 applies."""
        from kew.worktree import _operator_remote_branch_head

        monkeypatch.setenv("SSH_ASKPASS", "/bin/echo")
        monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
        monkeypatch.setenv("GIT_ASKPASS", "/bin/echo")
        head = "c" * 40
        listed = subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/agent/issue-7\n", ""
        )
        with (
            patch(
                "kew.worktree._operator_origin_url",
                return_value="https://github.com/o/r.git",
            ),
            patch("kew.worktree.publication_git_env", return_value=None),
            patch("kew.worktree.run", return_value=listed) as run_mock,
        ):
            assert _operator_remote_branch_head(tmp_path, "agent/issue-7") == head

        env = run_mock.call_args.kwargs["env"]
        assert "SSH_ASKPASS" not in env
        assert "SSH_ASKPASS_REQUIRE" not in env
        assert "GIT_ASKPASS" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_non_https_origins_never_consult_auth_discovery(
        self, tmp_path: Path
    ) -> None:
        """Auth discovery reaches for the project host; a path origin has none."""
        from kew.worktree import _operator_remote_branch_head

        head = "d" * 40
        listed = subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/agent/issue-7\n", ""
        )
        with (
            patch(
                "kew.worktree._operator_origin_url",
                return_value=str(tmp_path / "origin.git"),
            ),
            patch("kew.worktree.publication_git_env") as auth,
            patch("kew.worktree.run", return_value=listed),
        ):
            assert _operator_remote_branch_head(tmp_path, "agent/issue-7") == head

        auth.assert_not_called()

    @pytest.mark.parametrize(
        ("origin", "wants_auth"),
        [
            ("https://github.com/o/r.git", True),
            ("HTTPS://github.com/o/r.git", True),
            ("http://github.com/o/r.git", False),
            ("https://user:pass@github.com/o/r.git", False),
            ("https://token@github.com/o/r.git", False),
            ("git@github.com:o/r.git", False),
            ("/srv/r.git", False),
            ("../origin.git", False),
        ],
    )
    def test_kew_auth_is_added_only_for_uncredentialed_https(
        self, origin: str, wants_auth: bool
    ) -> None:
        from kew.worktree import _wants_kew_https_auth

        assert _wants_kew_https_auth(origin) is wants_auth

    @pytest.mark.parametrize(
        ("origin", "expected_key"),
        [
            ("https://github.com/o/r.git", "http.https://github.com/.extraHeader"),
            (
                "https://github.com:8443/o/r.git",
                "http.https://github.com:8443/.extraHeader",
            ),
            ("HTTPS://GitHub.com/o/r.git", "http.https://GitHub.com/.extraHeader"),
            ("https://gitlab.example/o/r.git", None),
            ("https://[2001:db8::1]/o/r.git", None),
        ],
    )
    def test_auth_key_follows_the_origin_authority_for_the_project_host_only(
        self, origin: str, expected_key: str | None
    ) -> None:
        from kew.worktree import _auth_key_for_origin

        issued = "http.https://github.com/.extraHeader"
        assert _auth_key_for_origin(issued, origin) == expected_key

    def test_ipv6_project_host_keeps_its_brackets(self) -> None:
        from kew.worktree import _auth_key_for_origin

        issued = "http.https://2001:db8::1/.extraHeader"
        assert (
            _auth_key_for_origin(issued, "https://[2001:db8::1]:8443/o/r.git")
            == "http.https://[2001:db8::1]:8443/.extraHeader"
        )

    @pytest.mark.parametrize(
        "origin",
        ["https://github.com:8443/o/r.git", "https://[2001:db8::1]:8443/o/r.git"],
    )
    def test_the_scoped_key_matches_the_origin_in_real_git(
        self, tmp_path: Path, origin: str
    ) -> None:
        """``git config --get-urlmatch`` must resolve the rebuilt key."""
        from kew.worktree import _auth_key_for_origin

        host = "github.com" if "github" in origin else "2001:db8::1"
        key = _auth_key_for_origin(f"http.https://{host}/.extraHeader", origin)
        assert key is not None
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": key,
            "GIT_CONFIG_VALUE_0": "Authorization: Basic c2VjcmV0",
        }
        matched = subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraHeader", origin],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert matched.returncode == 0, matched.stderr
        assert matched.stdout.strip() == "Authorization: Basic c2VjcmV0"

    def test_a_cross_host_redirect_is_never_followed_by_real_git(
        self, tmp_path: Path
    ) -> None:
        """Two local HTTP servers: the origin redirects, the target must see nothing.

        The query runs under the same ``GIT_CONFIG_*`` list production builds,
        with the header issued for the origin host; git must stop at the
        redirect instead of carrying that header to the other host.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from kew.worktree import _operator_origin_query_env

        target_requests: list[dict[str, str]] = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                target_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                return

        target = HTTPServer(("127.0.0.1", 0), Target)
        target_port = target.server_address[1]

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{target_port}{self.path}"
                )
                self.end_headers()

            def log_message(self, *_args):
                return

        origin = HTTPServer(("127.0.0.1", 0), Origin)
        origin_url = f"http://127.0.0.1:{origin.server_address[1]}/o/r.git"
        for server in (origin, target):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = _operator_origin_query_env(str(tmp_path), origin_url)
            # Attach a header for this (http, local) origin the way the https
            # path would, so the test proves the header stops at the redirect.
            count = int(env["GIT_CONFIG_COUNT"])
            env[f"GIT_CONFIG_KEY_{count}"] = f"http.{origin_url}/.extraHeader"
            env[f"GIT_CONFIG_VALUE_{count}"] = "Authorization: Basic c2VjcmV0"
            env["GIT_CONFIG_COUNT"] = str(count + 1)
            listed = subprocess.run(
                ["git", "ls-remote", "--heads", origin_url, "refs/heads/main"],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        finally:
            origin.shutdown()
            target.shutdown()

        assert listed.returncode != 0
        assert target_requests == [], "the redirect target must never be contacted"

    def test_operator_origin_query_timeout_fails_closed(self, tmp_path: Path) -> None:
        from kew.worktree import _operator_remote_branch_head

        with (
            patch(
                "kew.worktree._operator_origin_url",
                return_value="https://github.com/o/r.git",
            ),
            patch("kew.worktree.publication_git_env", return_value=None),
            patch(
                "kew.worktree.run",
                side_effect=subprocess.TimeoutExpired(["git"], 60.0),
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            _operator_remote_branch_head(tmp_path, "agent/issue-7")

    def test_planted_origin_ref_outside_fetch_refspec_cannot_authorize_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        marker = checkout / "planted-ref-only.txt"
        marker.write_text("the operator origin lacks this commit\n")
        _git("add", marker.name, cwd=checkout)
        _git("commit", "-qm", "checkout-only commit", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        _git("config", "--unset-all", "remote.origin.fetch", cwd=checkout)
        _git(
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main",
            cwd=checkout,
        )
        _git(
            "update-ref",
            "refs/remotes/origin/agent/issue-42",
            head_before,
            cwd=checkout,
        )

        with pytest.raises(RuntimeError, match="could not verify"):
            setup_worktree(issue_num=42, config=config)

        assert marker.read_text() == "the operator origin lacks this commit\n"
        assert _git_out("rev-parse", "HEAD", cwd=checkout) == head_before

    def test_legacy_linked_worktree_is_refused_before_any_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        checkout.parent.mkdir(parents=True)
        _git(
            "worktree",
            "add",
            "-q",
            "-b",
            "agent/issue-42",
            str(checkout),
            "main",
            cwd=source,
        )
        assert is_linked_worktree(checkout)

        with (
            patch("kew.worktree.run", wraps=subprocess_utils.run) as mock_run,
            pytest.raises(RuntimeError, match="linked worktree"),
        ):
            setup_worktree(issue_num=42, config=config)

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert not any("fetch" in command for command in commands)
        assert checkout.exists()

    def test_checkout_fsmonitor_never_runs_during_replacement_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        operator_origin = _git_out("remote", "get-url", "origin", cwd=source)
        _git("remote", "set-url", "origin", operator_origin, cwd=checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        _git("push", "-q", "origin", "agent/issue-42", cwd=checkout)

        sentinel = tmp_path / "fsmonitor-ran"
        fsmonitor = checkout / ".git" / "agent-fsmonitor"
        fsmonitor.write_text(f"#!/bin/sh\n: > '{sentinel}'\n")
        fsmonitor.chmod(0o755)
        _git("config", "core.fsmonitor", str(fsmonitor), cwd=checkout)

        result = setup_worktree(issue_num=42, config=config)

        assert result == checkout
        assert not sentinel.exists()

    def test_operator_origin_branch_authorizes_quarantined_replacement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        operator_origin = _git_out("remote", "get-url", "origin", cwd=source)
        _git("remote", "set-url", "origin", operator_origin, cwd=checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        marker = checkout / "pushed.txt"
        marker.write_text("this commit exists on the operator origin\n")
        _git("add", marker.name, cwd=checkout)
        _git("commit", "-qm", "pushed checkout commit", cwd=checkout)
        head_before = _git_out("rev-parse", "HEAD", cwd=checkout)
        _git("push", "-q", "origin", "agent/issue-42", cwd=checkout)

        result = setup_worktree(issue_num=42, config=config)

        quarantines = list(checkout.parent.glob("issue-42.replaced-*"))
        assert result == checkout
        assert len(quarantines) == 1
        assert _git_out("rev-parse", "HEAD", cwd=quarantines[0]) == head_before
        assert str(quarantines[0]) in capsys.readouterr().err

    def test_commit_after_validation_remains_in_quarantined_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(source)
        config = _cfg(source, tmp_path)
        checkout = Path(config.branches.worktree_base) / "agent" / "issue-42"
        self._clone_existing_checkout(source, checkout)
        origin_url = _git_out("remote", "get-url", "origin", cwd=source)
        _git("remote", "set-url", "origin", origin_url, cwd=checkout)
        _git(
            "checkout",
            "--no-track",
            "-qb",
            "agent/issue-42",
            "origin/main",
            cwd=checkout,
        )
        _git("push", "-q", "origin", "agent/issue-42", cwd=checkout)
        late_heads: list[str] = []

        def validate_then_commit(path: Path, repo_root: Path, branch_name: str) -> int:
            count = unpushed_commit_count(path, repo_root, branch_name)
            late_file = path / "late-commit.txt"
            late_file.write_text("created after validation\n")
            _git("add", late_file.name, cwd=path)
            _git("commit", "-qm", "late commit", cwd=path)
            late_heads.append(_git_out("rev-parse", "HEAD", cwd=path))
            return count

        with patch(
            "kew.worktree.unpushed_commit_count", side_effect=validate_then_commit
        ):
            result = setup_worktree(issue_num=42, config=config)

        quarantines = list(checkout.parent.glob("issue-42.replaced-*"))
        assert result == checkout
        assert len(quarantines) == 1
        quarantine = quarantines[0]
        assert (quarantine / "late-commit.txt").read_text() == (
            "created after validation\n"
        )
        assert _git_out("rev-parse", "HEAD", cwd=quarantine) == late_heads[0]
        assert str(quarantine) in capsys.readouterr().err

    @patch("kew.worktree.run")
    @patch("kew.worktree.detect_base_branch", return_value="main")
    def test_a_reusable_checkout_is_replaced(
        self, mock_detect: MagicMock, mock_run: MagicMock, tmp_path: Path
    ):
        """A clean, fully-pushed checkout is replaced without ceremony.

        The old stale-LOCAL-branch handling is gone: with a clone there is no
        branch in the operator's repository to go stale, and nothing to delete
        there.
        """
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        _make_checkout_shape(tmp_path / "agent" / "issue-42", "healthy")

        with (
            patch("kew.worktree.has_uncommitted_changes", return_value=False),
            patch("kew.worktree._repo_root", return_value=tmp_path),
            patch("kew.worktree.unpushed_commit_count", return_value=0),
        ):
            result = setup_worktree(issue_num=42, config=config)

        assert "issue-42" in str(result)
        assert not _ran_git(mock_run, "branch", "-D"), (
            "a fresh checkout deleted a branch in the operator's repository"
        )

    @patch("kew.worktree.run")
    @patch("kew.worktree.detect_base_branch", return_value="main")
    def test_unpushed_commits_are_refused_not_discarded(
        self, mock_detect: MagicMock, mock_run: MagicMock, tmp_path: Path
    ):
        """The protection `_clean_stale_branch` gave, moved to where the work is.

        That check refused to delete a local branch sitting ahead of origin.
        In a clone the branch lives inside the checkout, so discarding the
        directory would discard those commits — silently, since the dirty check
        only sees UNcommitted changes. The refusal has to move with the work.
        """
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        _make_checkout_shape(tmp_path / "agent" / "issue-42", "healthy")

        # Clean tree, three commits that never reached origin — the case the
        # dirty check cannot see.
        with (
            patch("kew.worktree.has_uncommitted_changes", return_value=False),
            patch("kew.worktree._repo_root", return_value=tmp_path),
            patch("kew.worktree.unpushed_commit_count", return_value=3),
            pytest.raises(RuntimeError, match=r"3 commit"),
        ):
            setup_worktree(issue_num=42, config=config)


def _operator_repo(tmp_path: Path) -> Path:
    """A real repo with a branch kew would create and one the operator owns."""
    repo = tmp_path / "operator"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    # An operator branch carrying work that is NOT merged into main.
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "precious"], check=True
    )
    (repo / "important.txt").write_text("months of work\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "unmerged"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    return repo


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch],
            capture_output=True,
        ).returncode
        == 0
    )


class TestCleanupWorktree:
    """Real git, not mocks.

    The previous tests asserted ``mock_run.call_count >= 2``, which is true of
    deleting the *wrong* branch as well as the right one — which is how #1445
    and #1487 survived a green suite. What matters is which branch is gone
    afterwards, and that is only observable against a real repository.
    """

    def test_deletes_the_branch_the_caller_named(self, tmp_path: Path, monkeypatch):
        repo = _operator_repo(tmp_path)
        worktree_dir = tmp_path / "agent" / "issue-42"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-q",
                str(worktree_dir),
                "-b",
                "agent/issue-42",
            ],
            check=True,
        )
        monkeypatch.chdir(repo)

        cleanup_worktree(worktree_dir, "agent/issue-42")

        assert not worktree_dir.exists()
        assert not _branch_exists(repo, "agent/issue-42")

    def test_an_agent_cannot_redirect_the_delete_at_an_operator_branch(
        self, tmp_path: Path, monkeypatch
    ):
        """#1445: the branch name came from ``git branch --show-current`` run
        inside the agent's own directory, so an agent that checked out any
        operator branch not currently in use had it force-deleted — unmerged
        work included — while the branch kew created survived orphaned.
        """
        repo = _operator_repo(tmp_path)
        worktree_dir = tmp_path / "agent" / "issue-42"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-q",
                str(worktree_dir),
                "-b",
                "agent/issue-42",
            ],
            check=True,
        )
        # The agent moves its worktree onto the operator's branch. Nothing in
        # git stops this: `precious` is not checked out anywhere else.
        subprocess.run(
            ["git", "-C", str(worktree_dir), "checkout", "-q", "precious"], check=True
        )
        monkeypatch.chdir(repo)

        cleanup_worktree(worktree_dir, "agent/issue-42")

        assert _branch_exists(repo, "precious"), (
            "an agent redirected a destructive host `git branch -D` onto the "
            "operator's branch by checking it out in its own worktree"
        )
        assert not _branch_exists(repo, "agent/issue-42"), (
            "the branch kew created was left behind"
        )

    def test_removal_works_on_a_clone_not_only_a_linked_worktree(
        self, tmp_path: Path, monkeypatch
    ):
        """#1487: ``git worktree remove`` is a no-op on a clone and its failure
        was swallowed, so the directory stayed on disk.
        """
        repo = _operator_repo(tmp_path)
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(repo), str(clone)], check=True
        )
        monkeypatch.chdir(repo)

        cleanup_worktree(clone, "")

        assert not clone.exists()

    def test_an_empty_branch_deletes_nothing(self, tmp_path: Path, monkeypatch):
        repo = _operator_repo(tmp_path)
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(repo), str(clone)], check=True
        )
        monkeypatch.chdir(repo)

        cleanup_worktree(clone, "")

        assert _branch_exists(repo, "precious")
        assert _branch_exists(repo, "main")

    @patch("kew.worktree.run")
    def test_noop_if_dir_missing(self, mock_run: MagicMock, tmp_path: Path):
        cleanup_worktree(tmp_path / "nonexistent", "agent/issue-42")
        mock_run.assert_not_called()


class TestCheckoutExistingWorktree:
    @patch("kew.worktree._repo_root")
    @patch("kew.worktree.run")
    def test_fetches_and_creates_worktree(
        self, mock_run: MagicMock, mock_repo_root: MagicMock, tmp_path: Path
    ):
        mock_repo_root.return_value = tmp_path
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        path, conflict = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )

        # A CLONE, never `git worktree add`: a linked worktree would share
        # the operator's .git (W1.1 / D2).
        assert _ran_git(mock_run, "fetch", "agent/issue-42")
        assert _ran_git(mock_run, "clone")
        assert not _ran_git(mock_run, "worktree", "add")
        assert "issue-42" in str(path)
        assert conflict is None

    @patch("kew.worktree.run")
    def test_item_id_slugs_worktree_dir(self, mock_run: MagicMock, tmp_path: Path):
        # A5a: the worktree dir derives from branch_slug(item_id).
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        # stdout doubles as the clone source `_repo_root` reads.
        mock_run.return_value = MagicMock(returncode=0, stdout=str(tmp_path))
        path, _conflict = checkout_existing_worktree(
            issue_num=0,
            branch_name="agent/issue-sfd-12",
            config=config,
            item_id="SFD-12",
            provider="linear",
        )
        assert "issue-sfd-12" in str(path)

    @patch("kew.worktree.run")
    def test_reuses_existing_worktree(self, mock_run: MagicMock, tmp_path: Path):
        """If the worktree directory already exists on correct branch, reuse it."""
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        worktree_dir = tmp_path / "agent" / "issue-42"
        worktree_dir.mkdir(parents=True)

        # Mock: fetch succeeds, rev-parse returns correct branch, pull succeeds
        mock_run.return_value = MagicMock(returncode=0, stdout="agent/issue-42\n")

        path, conflict = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )

        assert path == worktree_dir
        assert conflict is None
        # Should still fetch to ensure up-to-date, but not create a new worktree
        assert _ran_git(mock_run, "fetch")
        assert _ran_git(mock_run, "rev-parse")
        assert _ran_git(mock_run, "pull", "--ff-only")
        assert not _ran_git(mock_run, "worktree", "add")

    @patch("kew.worktree.run")
    def test_raises_on_pull_failure(self, mock_run: MagicMock, tmp_path: Path):
        """If pull --ff-only fails on an existing correct-branch worktree, raise."""
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        worktree_dir = tmp_path / "agent" / "issue-42"
        worktree_dir.mkdir(parents=True)

        # fetch succeeds, rev-parse returns correct branch, pull fails
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0, stdout="agent/issue-42\n"),  # rev-parse
            MagicMock(returncode=1),  # git pull --ff-only fails
        ]

        import pytest

        with pytest.raises(RuntimeError, match="pull.*failed"):
            checkout_existing_worktree(
                issue_num=42, branch_name="agent/issue-42", config=config
            )

    @patch("kew.worktree.run")
    def test_recreates_worktree_on_wrong_branch(
        self, mock_run: MagicMock, tmp_path: Path
    ):
        """If the worktree exists but on wrong branch, remove and recreate."""
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        worktree_dir = tmp_path / "agent" / "issue-42"
        worktree_dir.mkdir(parents=True)

        # Mock: fetch succeeds, rev-parse returns wrong branch, then worktree add
        mock_run.return_value = MagicMock(returncode=0, stdout="some-other-branch\n")

        path, conflict = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )

        assert path == worktree_dir
        assert conflict is None
        # Removed the old checkout and cloned a fresh isolated one.
        assert _ran_git(mock_run, "worktree", "remove")
        assert _ran_git(mock_run, "clone")

    @patch("kew.worktree.run")
    def test_never_deletes_a_branch_in_the_operators_repository(
        self, mock_run: MagicMock, tmp_path: Path
    ):
        """The stale-branch cleanup is gone, deliberately.

        Creating a fresh checkout used to run `git branch -D <branch_name>`
        against the OPERATOR's repository, with a name derived from the work
        item — a destructive host git command taking agent-influenced input
        (#1445). A clone carries its own branches, so there is nothing stale to
        clear and no reason to touch the operator's refs at all.
        """
        _materialize_mock_clone(mock_run)
        config = DispatchConfig(branches=BranchConfig(worktree_base=str(tmp_path)))
        mock_run.return_value = MagicMock(returncode=0, stdout=str(tmp_path))

        path, conflict = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )

        assert "issue-42" in str(path)
        assert conflict is None
        assert not _ran_git(mock_run, "branch", "-D"), (
            "a fresh checkout deleted a branch in the operator's repository"
        )
        # It still materializes a checkout — as an isolated clone.
        assert _ran_git(mock_run, "clone")


class TestRemoteBranchExists:
    @patch("kew.worktree.run")
    def test_returns_true_when_branch_exists(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234\trefs/heads/agent/issue-42\n",
        )
        assert remote_branch_exists("agent/issue-42") is True

    @patch("kew.worktree.run")
    def test_returns_false_when_branch_missing(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert remote_branch_exists("agent/issue-99") is False

    @patch("kew.worktree.run")
    def test_returns_false_on_git_failure(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        assert remote_branch_exists("agent/issue-42") is False


def _bare_source(path: Path, filename: str) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git("config", "user.email", "t@t.t", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    (path / filename).write_text("one\n")
    _git("add", filename, cwd=path)
    _git("commit", "-qm", "one", cwd=path)
    return path


def _repo_with_submodule(tmp_path: Path, *, unpublished_inner_commit: bool) -> Path:
    """A repository with submodule ``sub`` checked out at ``tmp_path/repo``."""
    sub_origin = _bare_source(tmp_path / "sub-origin", "inner.txt")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(sub_origin),
        "sub",
        cwd=repo,
    )
    _git("commit", "-qm", "add submodule", cwd=repo)
    if unpublished_inner_commit:
        _git("config", "user.email", "t@t.t", cwd=repo / "sub")
        _git("config", "user.name", "t", cwd=repo / "sub")
        (repo / "sub" / "inner.txt").write_text("two, never pushed\n")
        _git("commit", "-qam", "unpublished inside the submodule", cwd=repo / "sub")
        _git("commit", "-qam", "record the new gitlink", cwd=repo)
    return repo


class TestHasUncommittedChanges:
    @patch("kew.worktree.run")
    def test_clean_worktree(self, mock_run: MagicMock, tmp_path: Path):
        """Clean worktree has no uncommitted changes."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert has_uncommitted_changes(tmp_path) is False

    @patch("kew.worktree.run")
    def test_dirty_worktree(self, mock_run: MagicMock, tmp_path: Path):
        """Worktree with modified files has uncommitted changes."""
        mock_run.return_value = MagicMock(returncode=0, stdout=" M src/foo.py\n")
        assert has_uncommitted_changes(tmp_path) is True

    @patch("kew.worktree.run")
    def test_git_status_fails(self, mock_run: MagicMock, tmp_path: Path):
        """If git status fails, assume there are changes (safe default)."""
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        assert has_uncommitted_changes(tmp_path) is True

    def test_untracked_file_hidden_by_repo_config_is_detected(self, tmp_path: Path):
        """``status.showUntrackedFiles=no`` in the checkout must not hide a file.

        An agent controls its checkout's ``.git/config``. A plain
        ``git status --porcelain`` honours that setting and reports clean, and
        cleanup would then delete the unpublished file.
        """
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "status.showUntrackedFiles", "no", cwd=repo)
        (repo / "final-edit.txt").write_text("not published\n")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The config must hide the file from a plain status, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_tracked_edit_hidden_by_fsmonitor_hook_is_detected(self, tmp_path: Path):
        """A ``core.fsmonitor`` hook that reports no changes must not hide an edit.

        With the hook installed, Git trusts its cached index for every path the
        monitor did not name, so a modified tracked file disappears from a plain
        ``git status``. The check disables the monitor for its own query.
        """
        import time

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "f.txt").write_text("one\n")
        _git("add", "f.txt", cwd=repo)
        _git("commit", "-qm", "one", cwd=repo)
        hook = tmp_path / "silent-monitor.sh"
        # Protocol v2: argv is `2 <last-token>`; output is the new token, NUL,
        # then NUL-separated changed paths. No paths means "nothing changed".
        hook.write_text("#!/bin/sh\nprintf 'token\\0'\n")
        hook.chmod(0o755)
        _git("config", "core.fsmonitor", str(hook), cwd=repo)
        # Two status runs prime the index with the monitor's token.
        for _ in range(2):
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                check=True,
                capture_output=True,
            )
        time.sleep(1)  # leave the racy-git window so mtime alone cannot save us
        (repo / "f.txt").write_text("two, and much longer than before\n")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The hook must hide the edit from a plain status, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_tracked_edit_hidden_by_an_index_flag_is_detected(
        self, tmp_path: Path, flag: str
    ):
        """``update-index --assume-unchanged`` / ``--skip-worktree`` hide edits.

        Both flags tell ``git status`` to skip the path, and no command-line
        override brings it back. A checkout carrying either flag is dirty.
        """
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "f.txt").write_text("one\n")
        _git("add", "f.txt", cwd=repo)
        _git("commit", "-qm", "one", cwd=repo)
        assert has_uncommitted_changes(repo) is False

        _git("update-index", flag, "f.txt", cwd=repo)
        (repo / "f.txt").write_text("two, hidden behind the index flag\n")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The flag must hide the edit from status, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_index_flag_inside_a_submodule_is_detected(self, tmp_path: Path):
        """A flag in a submodule's index hides the edit from the superproject."""
        sub_origin = tmp_path / "sub-origin"
        subprocess.run(["git", "init", "-q", str(sub_origin)], check=True)
        _git("config", "user.email", "t@t.t", cwd=sub_origin)
        _git("config", "user.name", "t", cwd=sub_origin)
        (sub_origin / "inner.txt").write_text("one\n")
        _git("add", "inner.txt", cwd=sub_origin)
        _git("commit", "-qm", "one", cwd=sub_origin)

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_origin),
            "sub",
            cwd=repo,
        )
        _git("commit", "-qm", "add submodule", cwd=repo)
        assert has_uncommitted_changes(repo) is False

        _git("update-index", "--assume-unchanged", "inner.txt", cwd=repo / "sub")
        (repo / "sub" / "inner.txt").write_text("two, hidden inside the submodule\n")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The superproject must look clean, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_deinitialized_submodule_with_retained_module_is_detected(
        self, tmp_path: Path
    ):
        """``submodule deinit`` leaves ``.git/modules/<name>`` with its commits.

        The worktree is gone, so neither the superproject status nor
        ``submodule foreach`` can see it; the retained module cannot be proven
        published and must count as unpublished.
        """
        sub_origin = tmp_path / "sub-origin"
        subprocess.run(["git", "init", "-q", str(sub_origin)], check=True)
        _git("config", "user.email", "t@t.t", cwd=sub_origin)
        _git("config", "user.name", "t", cwd=sub_origin)
        (sub_origin / "inner.txt").write_text("one\n")
        _git("add", "inner.txt", cwd=sub_origin)
        _git("commit", "-qm", "one", cwd=sub_origin)

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_origin),
            "sub",
            cwd=repo,
        )
        _git("commit", "-qm", "add submodule", cwd=repo)
        _git("config", "user.email", "t@t.t", cwd=repo / "sub")
        _git("config", "user.name", "t", cwd=repo / "sub")
        (repo / "sub" / "inner.txt").write_text("two, never pushed\n")
        _git("commit", "-qam", "unpublished inside the submodule", cwd=repo / "sub")
        _git("submodule", "deinit", "-f", "sub", cwd=repo)
        assert (repo / ".git" / "modules" / "sub").exists()

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The superproject must look clean, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_committed_submodule_removal_with_retained_module_is_detected(
        self, tmp_path: Path
    ):
        """``git rm sub`` + commit: status is clean, ``.git/modules/sub`` stays."""
        repo = _repo_with_submodule(tmp_path, unpublished_inner_commit=True)
        _git("rm", "-q", "sub", cwd=repo)
        _git("commit", "-qm", "drop the submodule", cwd=repo)
        assert (repo / ".git" / "modules" / "sub").exists()

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_nested_submodule_deinit_is_detected(self, tmp_path: Path):
        """A retained module two levels down lives under the outer module."""
        inner_origin = _bare_source(tmp_path / "inner-origin", "inner.txt")
        outer_origin = tmp_path / "outer-origin"
        subprocess.run(["git", "init", "-q", str(outer_origin)], check=True)
        _git("config", "user.email", "t@t.t", cwd=outer_origin)
        _git("config", "user.name", "t", cwd=outer_origin)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner_origin),
            "inner",
            cwd=outer_origin,
        )
        _git("commit", "-qm", "outer with inner", cwd=outer_origin)

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(outer_origin),
            "outer",
            cwd=repo,
        )
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
            "-q",
            cwd=repo,
        )
        _git("commit", "-qm", "add outer", cwd=repo)
        assert has_uncommitted_changes(repo) is False

        inner = repo / "outer" / "inner"
        _git("config", "user.email", "t@t.t", cwd=inner)
        _git("config", "user.name", "t", cwd=inner)
        (inner / "inner.txt").write_text("unpublished, two levels down\n")
        _git("commit", "-qam", "nested unpublished", cwd=inner)
        _git("submodule", "deinit", "-f", "inner", cwd=repo / "outer")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The nested commit moved outer's gitlink for inner; commit that so
        # the superproject is clean and only the retained module remains.
        if plain.stdout.strip():
            _git("commit", "-qam", "record inner", cwd=repo / "outer")
            _git("commit", "-qam", "record outer", cwd=repo)
        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_unpushed_commit_inside_a_checked_out_submodule_is_detected(
        self, tmp_path: Path
    ):
        """kew publishes the superproject only; a submodule commit no remote
        ref contains would be deleted by cleanup with nothing able to fetch it."""
        repo = _repo_with_submodule(tmp_path, unpublished_inner_commit=True)
        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

        # Publishing the inner commit (to any branch of its remote) clears it.
        _git("push", "-q", "origin", "HEAD:refs/heads/published", cwd=repo / "sub")
        _git("fetch", "-q", "origin", cwd=repo / "sub")
        assert has_uncommitted_changes(repo) is False

    def test_embedded_old_form_submodule_cannot_mask_a_deinitialized_sibling(
        self, tmp_path: Path
    ):
        """One checked-out line with no stored repository, one stored repository
        with no checkout: a count says clean; identity says dirty."""
        import shutil

        repo = _repo_with_submodule(tmp_path, unpublished_inner_commit=False)
        # Add a second, modern submodule and deinitialize it (store retained).
        other_origin = _bare_source(tmp_path / "other-origin", "other.txt")
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(other_origin),
            "other",
            cwd=repo,
        )
        _git("commit", "-qm", "add other", cwd=repo)
        _git("submodule", "deinit", "-f", "other", cwd=repo)
        # Turn `sub` into the old form: its repository embedded at sub/.git.
        modules = repo / ".git" / "modules"
        (repo / "sub" / ".git").unlink()
        shutil.move(str(modules / "sub"), str(repo / "sub" / ".git"))
        config = repo / "sub" / ".git" / "config"
        config.write_text(
            "\n".join(
                line
                for line in config.read_text().splitlines()
                if not line.strip().startswith("worktree =")
            )
            + "\n"
        )
        _git("fetch", "-q", "origin", cwd=repo / "sub")
        assert sorted(p.name for p in modules.iterdir()) == ["other"]

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    @pytest.mark.parametrize("scope", ["local", "gitmodules"])
    def test_submodule_ignore_setting_cannot_hide_uncommitted_work(
        self, tmp_path: Path, scope: str
    ):
        """``submodule.<name>.ignore=all`` blanks the superproject's view."""
        repo = _repo_with_submodule(tmp_path, unpublished_inner_commit=False)
        if scope == "local":
            _git("config", "submodule.sub.ignore", "all", cwd=repo)
        else:
            _git(
                "config",
                "--file",
                ".gitmodules",
                "submodule.sub.ignore",
                "all",
                cwd=repo,
            )
            _git("commit", "-qam", "ignore the submodule", cwd=repo)
        (repo / "sub" / "left-behind.txt").write_text("uncommitted inside sub\n")

        plain = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The setting must hide the file from plain status, or the test proves nothing.
        assert plain.stdout.strip() == ""

        assert has_uncommitted_changes(repo) is True

    def test_module_store_locator_failure_counts_as_dirty(self, tmp_path: Path):
        from kew.worktree import _retained_submodule_state

        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        # Invalid metadata prevents discovery of the repository above the temp root.
        (not_a_repo / ".git").write_text("invalid gitfile\n")
        assert _retained_submodule_state(not_a_repo) is True

    def test_uninitialized_submodule_without_a_module_is_clean(self, tmp_path: Path):
        """A fresh clone's gitlink with nothing under .git/modules is not dirty."""
        sub_origin = tmp_path / "sub-origin"
        subprocess.run(["git", "init", "-q", str(sub_origin)], check=True)
        _git("config", "user.email", "t@t.t", cwd=sub_origin)
        _git("config", "user.name", "t", cwd=sub_origin)
        (sub_origin / "inner.txt").write_text("one\n")
        _git("add", "inner.txt", cwd=sub_origin)
        _git("commit", "-qm", "one", cwd=sub_origin)

        source = tmp_path / "source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        _git("config", "user.email", "t@t.t", cwd=source)
        _git("config", "user.name", "t", cwd=source)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_origin),
            "sub",
            cwd=source,
        )
        _git("commit", "-qm", "add submodule", cwd=source)

        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(source), str(clone)], check=True)
        assert not (clone / ".git" / "modules").exists()

        assert has_uncommitted_changes(clone) is False


class TestConflictInfo:
    def test_dataclass_fields(self):
        info = ConflictInfo(
            strategy_used="merge",
            conflicted_files=["a.py", "b.py"],
            base_branch="main",
        )
        assert info.strategy_used == "merge"
        assert info.conflicted_files == ["a.py", "b.py"]
        assert info.base_branch == "main"


class TestCheckoutExistingWorktreeConflicts:
    """checkout_existing_worktree handles merge conflicts."""

    @patch("kew.worktree.run")
    def test_ff_succeeds_still_integrates_base(self, mock_run: MagicMock, tmp_path):
        """FF works AND base merges cleanly — no ConflictInfo, base integrated.

        Regression for #790: an up-to-date branch must still be tested against
        base, not returned as "clean" without ever merging.
        """
        worktree_dir = tmp_path / ".worktrees" / "agent" / "issue-1"
        worktree_dir.mkdir(parents=True)

        config = DispatchConfig()
        config.branches.worktree_base = str(tmp_path / ".worktrees")

        # Calls: fetch branch, fetch base, rev-parse HEAD, pull --ff-only, merge
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch origin branch
            MagicMock(returncode=0),  # git fetch origin main
            MagicMock(returncode=0, stdout="agent/issue-1\n"),  # rev-parse HEAD
            MagicMock(returncode=0),  # git pull --ff-only
            MagicMock(returncode=0),  # git merge origin/main (clean)
        ]

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert path == worktree_dir
        assert conflict is None
        # Base was actually integrated, not skipped.
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("merge" in c and "origin/main" in c for c in calls)

    @patch("kew.worktree.run")
    def test_ff_fails_merge_strategy_with_conflicts(
        self, mock_run: MagicMock, tmp_path
    ):
        """FF fails, merge strategy produces conflicts — returns ConflictInfo."""
        worktree_dir = tmp_path / ".worktrees" / "agent" / "issue-1"
        worktree_dir.mkdir(parents=True)

        config = DispatchConfig()
        config.branches.worktree_base = str(tmp_path / ".worktrees")
        config.resolve.strategy = "merge"

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch origin branch
            MagicMock(returncode=0),  # git fetch origin main
            MagicMock(returncode=0, stdout="agent/issue-1\n"),  # rev-parse HEAD
            MagicMock(returncode=1),  # git pull --ff-only FAILS
            MagicMock(returncode=1),  # git merge origin/main CONFLICTS
            MagicMock(
                returncode=0, stdout="src/a.py\nsrc/b.py\n"
            ),  # git diff --name-only --diff-filter=U
            MagicMock(returncode=0, stdout="b" * 40 + "\n"),  # base SHA
        ]

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert path == worktree_dir
        assert conflict is not None
        assert conflict.strategy_used == "merge"
        assert conflict.conflicted_files == ["src/a.py", "src/b.py"]
        assert conflict.base_branch == "main"

    @patch("kew.worktree.run")
    def test_ff_fails_merge_strategy_clean_merge(self, mock_run: MagicMock, tmp_path):
        """FF fails but merge completes cleanly — no ConflictInfo."""
        worktree_dir = tmp_path / ".worktrees" / "agent" / "issue-1"
        worktree_dir.mkdir(parents=True)

        config = DispatchConfig()
        config.branches.worktree_base = str(tmp_path / ".worktrees")
        config.resolve.strategy = "merge"

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch origin branch
            MagicMock(returncode=0),  # git fetch origin main
            MagicMock(returncode=0, stdout="agent/issue-1\n"),  # rev-parse HEAD
            MagicMock(returncode=1),  # git pull --ff-only FAILS
            MagicMock(returncode=0),  # git merge origin/main OK (clean)
        ]

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert path == worktree_dir
        assert conflict is None

    @patch("kew.worktree.run")
    def test_rebase_strategy_succeeds_clean(self, mock_run: MagicMock, tmp_path):
        """Rebase succeeds without conflicts — no ConflictInfo."""
        worktree_dir = tmp_path / ".worktrees" / "agent" / "issue-1"
        worktree_dir.mkdir(parents=True)

        config = DispatchConfig()
        config.branches.worktree_base = str(tmp_path / ".worktrees")
        config.resolve.strategy = "rebase"

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch origin branch
            MagicMock(returncode=0),  # git fetch origin main
            MagicMock(returncode=0, stdout="agent/issue-1\n"),  # rev-parse HEAD
            MagicMock(returncode=1),  # git pull --ff-only FAILS
            MagicMock(returncode=0),  # git rebase origin/main OK
        ]

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert path == worktree_dir
        assert conflict is None

    @patch("kew.worktree.run")
    def test_rebase_conflicts_falls_back_to_merge(self, mock_run: MagicMock, tmp_path):
        """Rebase conflicts, aborts, falls back to merge with conflicts."""
        worktree_dir = tmp_path / ".worktrees" / "agent" / "issue-1"
        worktree_dir.mkdir(parents=True)

        config = DispatchConfig()
        config.branches.worktree_base = str(tmp_path / ".worktrees")
        config.resolve.strategy = "rebase"

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch origin branch
            MagicMock(returncode=0),  # git fetch origin main
            MagicMock(returncode=0, stdout="agent/issue-1\n"),  # rev-parse HEAD
            MagicMock(returncode=1),  # git pull --ff-only FAILS
            MagicMock(returncode=1),  # git rebase origin/main CONFLICTS
            MagicMock(returncode=0),  # git rebase --abort
            MagicMock(returncode=1),  # git merge origin/main CONFLICTS
            MagicMock(
                returncode=0, stdout="src/c.py\n"
            ),  # git diff --name-only --diff-filter=U
            MagicMock(returncode=0, stdout="b" * 40 + "\n"),  # base SHA
        ]

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert conflict is not None
        assert conflict.strategy_used == "merge"
        assert conflict.conflicted_files == ["src/c.py"]


def _origin_with_moved_base(tmp_path: Path, branch: str, *, conflict: bool) -> Path:
    """Bare 'origin' where `branch` diverges from an advanced `main`.

    conflict=True: branch and main edit the SAME file differently -> merging
    base conflicts. conflict=False: they edit DIFFERENT files -> base merges
    clean. Returns a detached-HEAD working clone to run checkout from; the
    agent branch is pushed with an upstream so ``pull --ff-only`` succeeds.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "shared.py").write_text("base\n")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    # Agent branch edits shared.py, pushed with upstream tracking.
    _git("checkout", "-qb", branch, cwd=work)
    (work / "shared.py").write_text("agent change\n")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "agent work", cwd=work)
    _git("push", "-qu", "origin", branch, cwd=work)
    # main advances past where the branch forked.
    _git("checkout", "-q", "main", cwd=work)
    if conflict:
        (work / "shared.py").write_text("main change\n")  # same file -> conflict
    else:
        (work / "other.py").write_text("main only\n")  # different file -> clean
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "main advance", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return work


class TestCheckoutIntegratesBaseRealGit:
    """#790: base is integrated on every path, exercised over real git repos."""

    def test_fresh_worktree_conflict_with_base(self, tmp_path, monkeypatch):
        """No preexisting worktree + branch conflicts with base -> ConflictInfo.

        Red before #790: the fresh-worktree path returned (dir, None).
        """
        work = _origin_with_moved_base(tmp_path, "agent/issue-1", conflict=True)
        monkeypatch.chdir(work)
        config = _cfg(work, tmp_path)

        path, conflict = checkout_existing_worktree(
            issue_num=1,
            branch_name="agent/issue-1",
            config=config,
            base_branch="main",
        )
        assert path == Path(config.branches.worktree_base) / "agent" / "issue-1"
        assert conflict is not None
        assert conflict.conflicted_files == ["shared.py"]
        assert conflict.base_branch == "main"
        assert conflict.base_sha == _git_out("rev-parse", "origin/main", cwd=path)

    def test_existing_uptodate_worktree_conflict_with_base(self, tmp_path, monkeypatch):
        """Existing worktree, up-to-date with remote, conflicts with base.

        Red before #790: pull --ff-only succeeds so it returned (dir, None).
        """
        work = _origin_with_moved_base(tmp_path, "agent/issue-2", conflict=True)
        monkeypatch.chdir(work)
        config = _cfg(work, tmp_path)
        wt = Path(config.branches.worktree_base) / "agent" / "issue-2"
        wt.parent.mkdir(parents=True, exist_ok=True)
        # Materialize the worktree at the branch tip -> up to date with origin.
        subprocess.run(
            ["git", "worktree", "add", str(wt), "agent/issue-2"],
            cwd=work,
            check=True,
            capture_output=True,
        )

        path, conflict = checkout_existing_worktree(
            issue_num=2,
            branch_name="agent/issue-2",
            config=config,
            base_branch="main",
        )
        assert path == wt
        assert conflict is not None
        assert "shared.py" in conflict.conflicted_files

    def test_clean_merge_with_moved_base_creates_commit(self, tmp_path, monkeypatch):
        """Branch merges clean with a moved base -> no conflict, commit created."""
        work = _origin_with_moved_base(tmp_path, "agent/issue-3", conflict=False)
        monkeypatch.chdir(work)
        config = _cfg(work, tmp_path)

        path, conflict = checkout_existing_worktree(
            issue_num=3,
            branch_name="agent/issue-3",
            config=config,
            base_branch="main",
        )
        assert conflict is None
        # Base was integrated: HEAD advanced beyond the pushed branch tip.
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "origin/agent/issue-3"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head != remote
        # HEAD is a merge commit carrying BOTH histories, not an unrelated tip
        # or a bare checkout of main.
        for ref in ("origin/main", "origin/agent/issue-3"):
            result = subprocess.run(
                ["git", "-C", str(path), "merge-base", "--is-ancestor", ref, "HEAD"],
                check=False,
            )
            assert result.returncode == 0, f"{ref} is not an ancestor of HEAD"

    def test_rebase_refuses_preserved_checkout_diverged_from_recorded_head(
        self, tmp_path, monkeypatch
    ):
        branch = "agent/issue-4"
        work = _origin_with_moved_base(tmp_path, branch, conflict=False)
        monkeypatch.chdir(work)
        config = _cfg(work, tmp_path)
        config.resolve.strategy = "rebase"
        preserved = Path(config.branches.worktree_base) / "agent" / "issue-4"
        preserved.parent.mkdir(parents=True)
        origin = tmp_path / "origin.git"
        _git("clone", "-q", "-b", branch, str(origin), str(preserved), cwd=work)
        _git("config", "user.email", "t@t.t", cwd=preserved)
        _git("config", "user.name", "t", cwd=preserved)
        (preserved / "local.txt").write_text("preserved work\n")
        _git("add", "local.txt", cwd=preserved)
        _git("commit", "-qm", "preserved resolution", cwd=preserved)
        preserved_head = _git_out("rev-parse", "HEAD", cwd=preserved)

        other = tmp_path / "other"
        _git("clone", "-q", "-b", branch, str(origin), str(other), cwd=work)
        _git("config", "user.email", "t@t.t", cwd=other)
        _git("config", "user.name", "t", cwd=other)
        (other / "concurrent.txt").write_text("new PR head\n")
        _git("add", "concurrent.txt", cwd=other)
        _git("commit", "-qm", "concurrent PR update", cwd=other)
        _git("push", "-q", "origin", branch, cwd=other)
        expected_head = _git_out("rev-parse", "HEAD", cwd=other)

        with pytest.raises(RuntimeError, match="recorded PR head"):
            checkout_existing_worktree(
                issue_num=4,
                branch_name=branch,
                config=config,
                base_branch="main",
                expected_head_sha=expected_head,
            )

        assert _git_out("rev-parse", "HEAD", cwd=preserved) == preserved_head
        assert (
            _git_out("rev-parse", branch, cwd=tmp_path / "origin.git") == expected_head
        )


def _base_moves_ahead(tmp_path: Path) -> str:
    """Push a new ``main`` from a second clone, so the operator repo is stale."""
    other = tmp_path / "other"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--branch",
            "main",
            str(tmp_path / "origin.git"),
            str(other),
        ],
        check=True,
    )
    _git("config", "user.email", "t@t.t", cwd=other)
    _git("config", "user.name", "t", cwd=other)
    (other / "f.txt").write_text("moved")
    _git("commit", "-aqm", "base moves", cwd=other)
    _git("push", "-q", "origin", "main", cwd=other)
    return _git_out("rev-parse", "HEAD", cwd=other)


class TestOperatorCommitParents:
    """Attesting an operator-pushed head reads its parents from the objects (#2124)."""

    def test_the_single_parent_comes_from_the_operator_repository(
        self, tmp_path: Path
    ) -> None:
        from kew.worktree import operator_commit_parents

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        head = _git_out("rev-parse", "origin/agent/issue-7", cwd=work)
        expected = _git_out("rev-parse", "origin/agent/issue-7^", cwd=work)

        assert operator_commit_parents(work, head) == (expected,)

    def test_a_hand_merge_reports_both_parents_in_order(self, tmp_path: Path) -> None:
        """An operator's hand-resolved base merge is read, not refused (#2177)."""
        from kew.worktree import operator_commit_parents

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        _git("checkout", "-q", "main", cwd=work)
        (work / "f.txt").write_text("moved")
        _git("commit", "-aqm", "base moves", cwd=work)
        base_head = _git_out("rev-parse", "HEAD", cwd=work)
        _git("checkout", "-q", "agent/issue-7", cwd=work)
        branch_head = _git_out("rev-parse", "HEAD", cwd=work)
        _git("merge", "--no-ff", "-q", "-m", "merge main", "main", cwd=work)
        merge = _git_out("rev-parse", "HEAD", cwd=work)

        assert operator_commit_parents(work, merge) == (branch_head, base_head)

    def test_a_root_commit_has_no_parent_to_bind_to(self, tmp_path: Path) -> None:
        """No parent is reported as such, never as an unparented clean read."""
        from kew.worktree import operator_commit_parents

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        root = _git_out("rev-list", "--max-parents=0", "origin/main", cwd=work)

        with pytest.raises(RuntimeError, match="no parent to bind to"):
            operator_commit_parents(work, root)

    def test_an_absent_commit_is_reported_not_defaulted(self, tmp_path: Path) -> None:
        from kew.worktree import operator_commit_parents

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")

        with pytest.raises(RuntimeError, match="not in the operator repository"):
            operator_commit_parents(work, "a" * 40)


class TestOperatorCommitOnBase:
    """A merge's second parent is checked against the FETCHED base (#2177)."""

    def test_a_commit_only_upstream_is_reachable_after_the_fetch(
        self, tmp_path: Path
    ) -> None:
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        moved = _base_moves_ahead(tmp_path)
        # The operator repository has never seen this commit; answering from
        # its stale origin/main would call a commit that IS on the base absent.
        absent = subprocess.run(
            ["git", "cat-file", "-e", f"{moved}^{{commit}}"],
            cwd=work,
            capture_output=True,
        )
        assert absent.returncode != 0

        assert operator_commit_on_base(work, moved, "main") is True
        # The probe ref is dropped again; it is not left in the operator repo.
        assert (
            _git_out("for-each-ref", "--format=%(refname)", "refs/kew/", cwd=work) == ""
        )

    def test_a_commit_off_the_base_is_not_reachable(self, tmp_path: Path) -> None:
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        branch_head = _git_out("rev-parse", "origin/agent/issue-7", cwd=work)

        assert operator_commit_on_base(work, branch_head, "main") is False

    def test_a_base_branch_that_cannot_be_fetched_is_reported(
        self, tmp_path: Path
    ) -> None:
        """An unreadable base is a refusal, never a quiet "not on the base"."""
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        head = _git_out("rev-parse", "origin/agent/issue-7", cwd=work)

        with pytest.raises(RuntimeError, match="could not be fetched"):
            operator_commit_on_base(work, head, "no-such-base")

    def test_a_concurrent_git_process_does_not_refuse_a_fetched_base(
        self, tmp_path: Path
    ) -> None:
        """The probe fetch syncs no remote-tracking ref, so its lock cannot fail it.

        A plain fetch would ALSO force-update refs/remotes/origin/main from
        remote.origin.fetch; any concurrent git process in the operator
        repository holds that lock, which failed the whole command and refused a
        legitimate hand-resolved base merge (#2177).
        """
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        moved = _base_moves_ahead(tmp_path)
        stale = _git_out("rev-parse", "refs/remotes/origin/main", cwd=work)
        lock = work / ".git" / "refs" / "remotes" / "origin" / "main.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")

        assert operator_commit_on_base(work, moved, "main") is True
        # The read leaves the operator's own remote-tracking refs alone.
        assert _git_out("rev-parse", "refs/remotes/origin/main", cwd=work) == stale

    def test_a_fetch_that_landed_the_probe_ref_is_not_a_refusal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The gate is the probe ref, not git's verdict on the whole command (#2177)."""
        from kew import worktree

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        moved = _base_moves_ahead(tmp_path)
        fetch_base = worktree._fetch_operator_base

        def noisy_fetch(*args, **kwargs):
            done = fetch_base(*args, **kwargs)
            assert done.returncode == 0
            return subprocess.CompletedProcess(
                done.args, 1, done.stdout, "error: cannot lock ref 'refs/heads/x'"
            )

        monkeypatch.setattr(worktree, "_fetch_operator_base", noisy_fetch)

        assert worktree.operator_commit_on_base(work, moved, "main") is True
        assert (
            _git_out("for-each-ref", "--format=%(refname)", "refs/kew/", cwd=work) == ""
        )

    def test_ambient_git_dir_does_not_redirect_the_probe_fetch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The fetch is pinned to the named repository, like its paired read (#2177)."""
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path / "operator", "agent/issue-7")
        decoy = _origin_repo_with_branch(tmp_path / "decoy", "agent/issue-7")
        moved = _base_moves_ahead(tmp_path / "operator")
        monkeypatch.chdir(decoy)
        # GIT_DIR beats -C, so an inherited one would fetch "the base" from the
        # decoy's origin and probe a ref the operator repository never got.
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        assert operator_commit_on_base(work, moved, "main") is True
        assert (
            _git_out("for-each-ref", "--format=%(refname)", "refs/kew/", cwd=decoy)
            == ""
        )

    def test_ambient_git_config_does_not_redirect_the_probe_fetch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GIT_CONFIG_* is how kew carries its own token; the fetch drops it (#2177)."""
        from kew.worktree import operator_commit_on_base

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        moved = _base_moves_ahead(tmp_path)
        elsewhere = tmp_path / "not-the-origin.git"
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{elsewhere}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "origin.git"))

        assert operator_commit_on_base(work, moved, "main") is True

    def test_the_probe_fetch_is_bounded_and_cannot_prompt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An unreachable origin is a refusal, never a fetch that hangs (#2177)."""
        from kew.worktree import _OPERATOR_ORIGIN_QUERY_TIMEOUT, operator_commit_on_base

        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            if "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd, _OPERATOR_ORIGIN_QUERY_TIMEOUT)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("kew.worktree.run", fake_run)
        monkeypatch.setenv("SSH_ASKPASS", str(tmp_path / "askpass"))
        monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")

        with pytest.raises(RuntimeError, match="did not finish"):
            operator_commit_on_base(tmp_path, "a" * 40, "main")

        fetch = next(call for call in calls if "fetch" in call["cmd"])
        assert fetch["timeout"] == _OPERATOR_ORIGIN_QUERY_TIMEOUT
        assert fetch["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "SSH_ASKPASS" not in fetch["env"]
        assert "SSH_ASKPASS_REQUIRE" not in fetch["env"]


class TestPublicationHeadBinding:
    """publish_existing_pr_branch binds the push to the intended PR head (#1290)."""

    def test_publishes_when_the_head_is_unmoved(self, tmp_path, monkeypatch):
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (wt / "remediation.txt").write_text("addressed the review")

        assert publish_existing_pr_branch(
            wt,
            "agent/issue-7",
            commit_message="fix: address review feedback",
            expected_remote_sha=head,
        )

    def test_checkout_publication_warns_and_records_posture(
        self, tmp_path, monkeypatch, capsys
    ):
        from kew.run_store import SqliteRunStore

        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        (wt / "remediation.txt").write_text("checkout publication")
        store = SqliteRunStore(":memory:")

        assert publish_existing_pr_branch(
            wt,
            "agent/issue-7",
            commit_message="fix: checkout posture",
            repo=str(tmp_path / "origin.git"),
            publish="checkout",
            dangerously_skip_containment=True,
            store=store,
            item_id="7",
        )

        output = capsys.readouterr()
        assert "temporary checkout escape hatch" in output.err
        [event] = store.list_audit_events(event="publication_posture")
        assert event.payload["posture"] == "uncontained-publication"

    def test_refuses_when_the_head_moved(self, tmp_path, monkeypatch):
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        (wt / "remediation.txt").write_text("addressed the review")

        # The PR head the caller intends to update is NOT what origin holds.
        with pytest.raises(RuntimeError, match="PR head moved"):
            publish_existing_pr_branch(
                wt,
                "agent/issue-7",
                commit_message="fix: address review feedback",
                expected_remote_sha="0" * 40,
            )
        # The refusal happens before anything is committed: the tree is
        # untouched, so the preserved worktree still holds the runner's work.
        assert (wt / "remediation.txt").read_text() == "addressed the review"
        assert has_uncommitted_changes(wt)

    def test_final_push_uses_bound_lease_and_reports_concurrent_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        other = tmp_path / "other"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        expected = _git_out("rev-parse", "HEAD", cwd=wt)
        (wt / "remediation.txt").write_text("addressed the review")

        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "-b",
                "agent/issue-7",
                str(tmp_path / "origin.git"),
                str(other),
            ],
            check=True,
        )
        _git("config", "user.email", "t@t.t", cwd=other)
        _git("config", "user.name", "t", cwd=other)
        (other / "concurrent.txt").write_text("concurrent update")
        _git("add", "concurrent.txt", cwd=other)
        _git("commit", "-qm", "concurrent update", cwd=other)

        real_run = subprocess_utils.run
        pushes: list[list[str]] = []

        def race_on_push(argv, **kwargs):
            if argv[0] == "git" and "push" in argv:
                pushes.append(argv)
                _git("push", "-q", "origin", "agent/issue-7", cwd=other)
            return real_run(argv, **kwargs)

        monkeypatch.setattr("kew.publication_canonical.run", race_on_push)

        with pytest.raises(PublicationHeadMovedError, match="PR head moved"):
            publish_existing_pr_branch(
                wt,
                "agent/issue-7",
                commit_message="fix: address review feedback",
                expected_remote_sha=expected,
            )

        assert len(pushes) == 1
        assert f"--force-with-lease=agent/issue-7:{expected}" in pushes[0]
        assert pushes[0][-1].endswith(":refs/heads/agent/issue-7")
        assert wt.is_dir()
        assert (wt / "remediation.txt").read_text() == "addressed the review"

    def test_refuses_a_review_commit_that_the_agent_already_pushed(
        self, tmp_path, monkeypatch
    ):
        """Canonical publication stays bound to the recorded remote head."""
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        original_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (wt / "remediation.txt").write_text("addressed the review")
        _git("add", "remediation.txt", cwd=wt)
        _git("commit", "-qm", "fix: address review feedback", cwd=wt)
        pushed_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _git("push", "-q", "origin", "HEAD:agent/issue-7", cwd=wt)

        with pytest.raises(PublicationHeadMovedError, match="PR head moved"):
            publish_existing_pr_branch(
                wt,
                "agent/issue-7",
                commit_message="unused",
                expected_remote_sha=original_head,
            )
        assert (
            subprocess.run(
                ["git", "-C", str(wt), "rev-parse", "origin/agent/issue-7"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == pushed_head
        )

    def test_refuses_a_fast_forward_beyond_the_recorded_head(
        self, tmp_path, monkeypatch
    ):
        """Canonical publication rejects every change from the recorded head."""
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)
        wt = tmp_path / "review-wt"
        other = tmp_path / "other-wt"
        _git("worktree", "add", "--detach", str(wt), "origin/agent/issue-7", cwd=work)
        _git("config", "user.email", "t@t.t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        original_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (wt / "remediation.txt").write_text("addressed the review")
        _git("add", "remediation.txt", cwd=wt)
        _git("commit", "-qm", "fix: address review feedback", cwd=wt)
        review_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _git("push", "-q", "origin", "HEAD:agent/issue-7", cwd=wt)

        _git("worktree", "add", "--detach", str(other), review_head, cwd=work)
        _git("config", "user.email", "t@t.t", cwd=other)
        _git("config", "user.name", "t", cwd=other)
        (other / "follow-up.txt").write_text("follow-up")
        _git("add", "follow-up.txt", cwd=other)
        _git("commit", "-qm", "chore: follow up", cwd=other)
        _git("push", "-q", "origin", "HEAD:agent/issue-7", cwd=other)

        with pytest.raises(PublicationHeadMovedError, match="PR head moved"):
            publish_existing_pr_branch(
                wt,
                "agent/issue-7",
                commit_message="unused",
                expected_remote_sha=original_head,
            )


class TestResolveAndCiFixCheckoutsAreIsolated:
    """W1.1 / D2, doors 2-4 for the ``checkout_existing_worktree`` flows.

    Resolve and ci-fix both run agents in the checkout this primitive returns.
    As a linked worktree its ``.git`` was the operator's, so an agent could
    plant hooks that fire on the operator's own ``git commit``. Real git here,
    not mocks: the mocked tests above prove which commands run, and this proves
    what those commands produce.
    """

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        return _origin_repo_with_branch(tmp_path, "agent/issue-42")

    def test_the_checkout_does_not_share_the_operators_git(self, tmp_path, monkeypatch):
        work = self._repo(tmp_path)
        monkeypatch.chdir(work)
        config = DispatchConfig(
            branches=BranchConfig(worktree_base=str(tmp_path / "wt"))
        )
        path, _ = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )

        assert (path / ".git").is_dir(), "checkout is a linked worktree"
        common = Path(_git_out("rev-parse", "--git-common-dir", cwd=path))
        common = (common if common.is_absolute() else path / common).resolve()
        assert common == (path / ".git").resolve()

        # Objects must not be shared by inode either — a local clone hardlinks
        # them by default, and the agent can chmod and write through.
        operator_objects = {
            p.stat().st_ino
            for p in (work / ".git" / "objects").rglob("*")
            if p.is_file()
        }
        shared = [
            p
            for p in (path / ".git" / "objects").rglob("*")
            if p.is_file() and p.stat().st_ino in operator_objects
        ]
        assert not shared, f"object files shared with the operator: {shared}"

    def test_agent_config_writes_stay_in_the_checkout(self, tmp_path, monkeypatch):
        work = self._repo(tmp_path)
        monkeypatch.chdir(work)
        config = DispatchConfig(
            branches=BranchConfig(worktree_base=str(tmp_path / "wt"))
        )
        path, _ = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )
        _git("config", "core.hooksPath", "/tmp/agent", cwd=path)
        landed = subprocess.run(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        assert landed.returncode != 0, (
            f"core.hooksPath reached the operator: {landed.stdout.strip()!r}"
        )

    def test_a_legacy_linked_worktree_is_replaced_not_reused(
        self, tmp_path, monkeypatch
    ):
        """The upgrade path. Without this the fix reaches only new checkouts.

        An operator who updates kew keeps the checkouts they already have, and
        those are linked worktrees. Reusing one silently keeps the exposure
        alive for exactly the people who have been running agents longest.
        """
        work = self._repo(tmp_path)
        monkeypatch.chdir(work)
        wt_base = tmp_path / "wt"
        legacy = wt_base / "agent" / "issue-42"
        legacy.parent.mkdir(parents=True)
        _git("worktree", "add", str(legacy), "agent/issue-42", cwd=work)
        assert (legacy / ".git").is_file(), "fixture is not a linked worktree"

        config = DispatchConfig(branches=BranchConfig(worktree_base=str(wt_base)))
        path, _ = checkout_existing_worktree(
            issue_num=42, branch_name="agent/issue-42", config=config
        )
        assert path == legacy
        assert (path / ".git").is_dir(), "legacy linked worktree was reused"

    def test_a_dirty_legacy_worktree_is_refused_not_destroyed(
        self, tmp_path, monkeypatch
    ):
        """The upgrade must not eat work in progress.

        Unlike the wrong-branch path, a legacy checkout on the RIGHT branch
        holds the work the operator is in the middle of. Replacing it silently
        would discard exactly that. Review refuses the same way; the two must
        not differ on whether an upgrade can destroy uncommitted changes.
        """
        work = self._repo(tmp_path)
        monkeypatch.chdir(work)
        wt_base = tmp_path / "wt"
        legacy = wt_base / "agent" / "issue-42"
        legacy.parent.mkdir(parents=True)
        _git("worktree", "add", str(legacy), "agent/issue-42", cwd=work)
        (legacy / "in-flight.txt").write_text("half-written work")

        config = DispatchConfig(branches=BranchConfig(worktree_base=str(wt_base)))
        with pytest.raises(RuntimeError, match="uncommitted changes"):
            checkout_existing_worktree(
                issue_num=42, branch_name="agent/issue-42", config=config
            )
        assert (legacy / "in-flight.txt").read_text() == "half-written work"


class TestEveryAgentCheckoutIsIsolated:
    """One assertion over ALL of doors 2-4, not one per primitive.

    Three primitives create agent checkouts, feeding five flows. Converting
    them one at a time is how a fourth gets added later without anyone
    noticing, so this drives every entry point through the same check: an
    agent must never be handed a checkout whose `.git` is the operator's.
    """

    def test_no_entry_point_hands_out_the_operators_git(self, tmp_path, monkeypatch):
        work = _origin_repo_with_branch(tmp_path, "agent/issue-7")
        monkeypatch.chdir(work)

        def _base(name: str) -> DispatchConfig:
            # A base per entry point: `reuse_or_restore_worktree` derives its
            # branch from the issue number, so all three want issue 7 and would
            # otherwise land on one directory and test each other's output.
            cfg = _cfg(work, tmp_path)
            cfg.branches.worktree_base = str(tmp_path / f"wt-{name}")
            return cfg

        restore_config = _base("restore")
        claim_checkout(dispatch_checkout_path(restore_config, issue_num=7))
        checkouts = {
            "setup_worktree": setup_worktree(issue_num=7, config=_base("setup")),
            "checkout_existing_worktree": checkout_existing_worktree(
                issue_num=7, branch_name="agent/issue-7", config=_base("existing")
            )[0],
            "reuse_or_restore_worktree": reuse_or_restore_worktree(
                issue_num=7,
                config=restore_config,
                expected_generation="previous-generation",
            ),
        }

        operator_git = (work / ".git").resolve()
        operator_objects = {
            p.stat().st_ino
            for p in (work / ".git" / "objects").rglob("*")
            if p.is_file()
        }

        for name, path in checkouts.items():
            assert (path / ".git").is_dir(), f"{name} returned a linked worktree"
            common = Path(_git_out("rev-parse", "--git-common-dir", cwd=path))
            common = (common if common.is_absolute() else path / common).resolve()
            assert common != operator_git, f"{name} resolves to the operator's .git"
            shared = [
                p
                for p in (path / ".git" / "objects").rglob("*")
                if p.is_file() and p.stat().st_ino in operator_objects
            ]
            assert not shared, f"{name} shares object files with the operator"


@pytest.fixture(autouse=True)
def _legacy_checkout_reuse(monkeypatch):
    """These tests document the checkout escape hatch's refusal semantics for an
    existing clone. Under canonical publication the clone is moved aside intact
    instead (review round 10 of #1440), covered in tests/test_publication_round3.py."""
    monkeypatch.setattr(
        "kew.worktree.quarantine_agent_checkout", lambda *_a, **_k: None
    )
    monkeypatch.setattr("kew.worktree._canonical_never_reuses", lambda *_a, **_k: False)


def test_checkout_claim_guard_locks_checkout_publication(tmp_path):
    import fcntl

    from kew.worktree import CheckoutBusy, checkout_claim_guard

    config = DispatchConfig()
    config.publication.publish = "checkout"
    checkout = tmp_path / "preserved"
    lock = tmp_path / "preserved.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(CheckoutBusy, match="claimed by another kew process"),
            checkout_claim_guard(checkout),
        ):
            pytest.fail("guard entered while another descriptor held the checkout")
    with (
        checkout_claim_guard(checkout),
        lock.open("r") as contender,
        pytest.raises(BlockingIOError),
    ):
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock.open("r") as released:
        fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("item_id", ["7", ""])
def test_checkout_publication_records_intent_before_push(
    tmp_path, monkeypatch, item_id
):
    from test_publication_intent_lineage import _record_native_profile

    from kew import dispatch_core, worktree
    from kew.publication import PublicationError
    from kew.run_store import SqliteRunStore

    branch = "feature/checkout-intent"
    checkout = _origin_repo_with_branch(tmp_path, branch)
    parent = _git_out("rev-parse", "HEAD", cwd=checkout)
    (checkout / "work.txt").write_text("repair\n")
    original_push = worktree._push_publication
    with SqliteRunStore(str(tmp_path / "checkout-intent.db")) as store:
        _record_native_profile(
            store, "producer", branch=branch, item_id=item_id, run_kind="ci_fix"
        )

        def push(*args, **kwargs):
            events = store.list_audit_events(event="publication_intent")
            assert len(events) == 1
            event = events[0]
            assert event.payload["scope"] == ("item" if item_id else "branch")
            assert event.item_id == (item_id or branch)
            assert event.payload["commit_sha"] == _git_out(
                "rev-parse", "HEAD", cwd=checkout
            )
            assert event.payload["parent_sha"] == parent
            original_push(*args, **kwargs)

        monkeypatch.setattr(worktree, "_push_publication", push)
        assert publish_existing_pr_branch(
            checkout,
            branch,
            commit_message="repair",
            expected_remote_sha=parent,
            publish="checkout",
            dangerously_skip_containment=True,
            store=store,
            run_id="producer",
            item_id=item_id,
        )
        head = _git_out("rev-parse", f"origin/{branch}", cwd=checkout)
        assert head != parent
        # A dropped intent leaves the remote on the previous published head.
        (checkout / "work.txt").write_text("another repair\n")
        real_emit = dispatch_core.audit.emit

        def drop_intent(
            store,
            event,
            *,
            actor,
            run_id=None,
            issue_num=None,
            item_id=None,
            endpoint=None,
            repo=None,
            payload=None,
            dedup_key=None,
            strict=False,
        ):
            if event == "publication_intent":
                return None
            return real_emit(
                store,
                event,
                actor=actor,
                run_id=run_id,
                issue_num=issue_num,
                item_id=item_id,
                endpoint=endpoint,
                repo=repo,
                payload=payload,
                dedup_key=dedup_key,
                strict=strict,
            )

        monkeypatch.setattr(dispatch_core.audit, "emit", drop_intent)
        with pytest.raises(PublicationError, match="not durably recorded"):
            publish_existing_pr_branch(
                checkout,
                branch,
                commit_message="another repair",
                expected_remote_sha=head,
                publish="checkout",
                dangerously_skip_containment=True,
                store=store,
                run_id="producer",
                item_id=item_id,
            )
        assert _git_out("rev-parse", f"origin/{branch}", cwd=checkout) == head


def test_clone_local_checkout_strips_ssh_hash_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SSH password containing "#" is stripped on the no-fetch clone path
    (round 5 of #1949 showed the generic authority pattern stopped at "#")."""
    source = tmp_path / "source"
    source.mkdir()
    _bootstrap_git(source, "init", "-q", "-b", "main")
    _bootstrap_git(source, "config", "user.name", "Test")
    _bootstrap_git(source, "config", "user.email", "test@example.test")
    tree = _bootstrap_git(source, "mktree")
    head = _bootstrap_git(source, "commit-tree", tree, "-m", "base")
    _bootstrap_git(source, "update-ref", "refs/heads/main", head)
    _bootstrap_git(source, "remote", "add", "origin", "ssh://user:pa#ss@host/r.git")
    checkout = tmp_path / "agent"
    monkeypatch.chdir(source)

    worktree.clone_local_checkout(source, checkout, head)

    agent_origin = _bootstrap_git(checkout, "remote", "get-url", "origin")
    assert agent_origin == "ssh://user@host/r.git"
    assert "pa#ss" not in (checkout / ".git" / "config").read_text()


@pytest.mark.parametrize("observed_token", ["", "a" * 32])
def test_empty_abandonment_token_never_authorizes_quarantine(
    tmp_path, monkeypatch, observed_token
):
    from kew.continuation_recovery import CheckoutAbandonment

    checkout = tmp_path / "issue-42"
    checkout.mkdir()
    (checkout / "edits.txt").write_text("preserved work")
    worktree._generation_sidecar(checkout).write_text(observed_token)
    authority = CheckoutAbandonment(
        evidence_identity="abandonment-event",
        checkout=checkout.resolve(),
        branch="agent/issue-42",
        original_run_id="original",
        generation=0,
        epic_ref="epic:owner",
        item_id="42",
        checkout_id="",
    )
    monkeypatch.setattr(worktree, "is_safe_standalone_clone", lambda _: True)
    with pytest.raises(RuntimeError, match="authority does not match"):
        worktree._quarantine_abandoned_checkout(checkout, authority.branch, authority)
    assert (checkout / "edits.txt").read_text() == "preserved work"
    assert not list(tmp_path.glob("issue-42.replaced-*"))


def _round20_recreate_checkout(checkout, source, branch):
    """Replace only the directory with a distinct dirty clone of the same branch."""
    import shutil

    replacement = checkout.with_name(checkout.name + ".external-clone")
    _git(
        "clone",
        "--no-hardlinks",
        "--branch",
        branch,
        str(source),
        str(replacement),
        cwd=source,
    )
    previous = checkout.stat()
    assert (replacement.stat().st_dev, replacement.stat().st_ino) != (
        previous.st_dev,
        previous.st_ino,
    )
    shutil.rmtree(checkout)
    replacement.rename(checkout)
    (checkout / "external-edits.txt").write_text("external dirty replacement\n")
    assert _git_out("branch", "--show-current", cwd=checkout) == branch
    assert _git_out("status", "--porcelain", cwd=checkout)


@pytest.mark.parametrize("reader", ["read", "capture"])
def test_round20_same_path_stale_sidecar_refuses(tmp_path, reader):
    branch = "agent/issue-42"
    source = _origin_repo_with_branch(tmp_path, branch)
    checkout = tmp_path / "checkout"
    _git("clone", "--branch", branch, str(source), str(checkout), cwd=source)
    token = write_checkout_generation(checkout)
    sidecar = worktree._generation_sidecar(checkout)
    before = sidecar.read_bytes()
    _round20_recreate_checkout(checkout, source, branch)
    assert sidecar.read_bytes() == before
    with worktree.checkout_claim_guard(checkout):
        read = (
            read_checkout_generation
            if reader == "read"
            else worktree.capture_checkout_generation
        )
        with pytest.raises(
            worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
        ) as refused:
            read(checkout)
    assert "revocation helper" in str(refused.value)
    assert "kew epic abandon" in str(refused.value)
    assert token not in str(refused.value)
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("reader", ["read", "capture"])
def test_round20_incomplete_json_sidecar_refuses_with_recovery(tmp_path, reader):
    import json

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    token = "a" * 32
    contents = json.dumps({"token": token})
    worktree._generation_sidecar(checkout).write_text(contents)
    with worktree.checkout_claim_guard(checkout):
        read = (
            read_checkout_generation
            if reader == "read"
            else worktree.capture_checkout_generation
        )
        with pytest.raises(
            worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
        ) as refused:
            read(checkout)
    assert "remove the stale sidecar through kew's revocation helper" in str(
        refused.value
    )
    assert "kew epic abandon" in str(refused.value)
    assert token not in str(refused.value)


@pytest.mark.parametrize(
    "damage", ["device", "inode", "unreadable", "boolean", "missing"]
)
@pytest.mark.parametrize("reader", ["read", "capture"])
def test_round20_invalid_binding_fails_closed(tmp_path, monkeypatch, damage, reader):
    import json

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    token = write_checkout_generation(checkout)
    identity = checkout.stat()
    binding = {"token": token, "device": identity.st_dev, "inode": identity.st_ino}
    sidecar = worktree._generation_sidecar(checkout)
    if damage in {"device", "inode"}:
        binding[damage] += 1
    elif damage == "boolean":
        binding["inode"] = True
    elif damage == "missing":
        del binding["device"]
    else:
        read_text = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == sidecar:
                raise PermissionError("private diagnostic")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    sidecar.write_text(json.dumps(binding))
    with worktree.checkout_claim_guard(checkout):
        read = (
            read_checkout_generation
            if reader == "read"
            else worktree.capture_checkout_generation
        )
        with pytest.raises(
            worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
        ) as refused:
            read(checkout)
    assert "private diagnostic" not in str(refused.value)
    assert "revocation helper" in str(refused.value)
    assert "kew epic abandon" in str(refused.value)


def test_round20_revocation_allows_fresh_bound_mint(tmp_path):
    import json

    branch = "agent/issue-42"
    source = _origin_repo_with_branch(tmp_path, branch)
    checkout = tmp_path / "checkout"
    _git("clone", "--branch", branch, str(source), str(checkout), cwd=source)
    old_token = write_checkout_generation(checkout)
    _round20_recreate_checkout(checkout, source, branch)
    revoke_checkout_generation(checkout)
    assert not worktree._generation_sidecar(checkout).exists()
    with worktree.checkout_claim_guard(checkout):
        token = worktree.capture_checkout_generation(checkout)
    assert token != old_token
    assert read_checkout_generation(checkout) == token
    contents = worktree._generation_sidecar(checkout).read_text()
    assert contents.startswith("{"), "fresh mint must persist the directory binding"
    assert {key: json.loads(contents)[key] for key in ("token", "device", "inode")} == {
        "token": json.loads(token)["token"],
        "device": checkout.stat().st_dev,
        "inode": checkout.stat().st_ino,
    }


class _Round21StatResult:
    def __init__(self, original, values, no_birth):
        self.original = original
        self.values = values
        self.no_birth = no_birth

    def __getattr__(self, name):
        if self.no_birth and name == "st_birthtime":
            raise AttributeError(name)
        if name in self.values:
            return self.values[name]
        return getattr(self.original, name)


def _round21_stat(path, *args, checkout, original, values, no_birth, **kwargs):
    result = original(path, *args, **kwargs)
    if not isinstance(path, int) and os.path.abspath(path) == str(checkout):
        return _Round21StatResult(result, values, no_birth)
    return result


def _round21_patch_identity(monkeypatch, checkout, *, no_birth=False, **values):
    from functools import partial

    monkeypatch.setattr(
        os,
        "stat",
        partial(
            _round21_stat,
            checkout=checkout,
            original=os.stat,
            values=values,
            no_birth=no_birth,
        ),
    )


def _round21_replace_reusing_identity(
    monkeypatch, checkout, *, same_birth=False, source=None, branch=None
):
    """Preserve the sidecar while simulating reuse of the original device and inode."""
    import shutil

    identity = checkout.stat()
    if source is None:
        shutil.rmtree(checkout)
        (checkout / ".git").mkdir(parents=True)
    else:
        _round20_recreate_checkout(checkout, source, branch)
    (checkout / "foreign.txt").write_text("foreign bytes\n")
    birth = getattr(identity, "st_birthtime", min(identity.st_ctime, identity.st_mtime))
    _round21_patch_identity(
        monkeypatch,
        checkout,
        st_dev=identity.st_dev,
        st_ino=identity.st_ino,
        st_birthtime=birth if same_birth else birth + 60,
        st_ctime=identity.st_ctime if same_birth else identity.st_ctime + 60,
        st_mtime=identity.st_mtime if same_birth else identity.st_mtime + 60,
    )
    assert not (checkout / ".git" / "kew-generation").exists()


@pytest.mark.parametrize("reader", ["read", "capture"])
@pytest.mark.parametrize("same_birth", [False, True])
def test_round21_reused_device_inode_refuses(tmp_path, monkeypatch, reader, same_birth):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, st_birthtime=10.0)
    write_checkout_generation(checkout)
    sidecar = worktree._generation_sidecar(checkout)
    before = sidecar.read_bytes()
    _round21_replace_reusing_identity(monkeypatch, checkout, same_birth=same_birth)
    read = (
        read_checkout_generation
        if reader == "read"
        else worktree.capture_checkout_generation
    )
    with (
        worktree.checkout_claim_guard(checkout),
        pytest.raises(
            worktree.CheckoutGenerationRefusal,
            match="nonce" if same_birth else "birth identity",
        ),
    ):
        read(checkout)
    assert sidecar.read_bytes() == before
    assert (checkout / "foreign.txt").read_text() == "foreign bytes\n"


def test_round21_matching_four_factors_read_token(tmp_path, monkeypatch):
    import json

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, st_birthtime=10.0)
    token = write_checkout_generation(checkout)
    assert read_checkout_generation(checkout) == token
    assert (checkout / ".git" / "kew-generation").read_text().strip() == json.loads(
        token
    )["nonce"]
    binding = json.loads(worktree._generation_sidecar(checkout).read_text())
    identity = checkout.stat()
    assert binding["device"] == identity.st_dev
    assert binding["inode"] == identity.st_ino
    assert binding["birth_time"] == getattr(identity, binding["birth_field"])


@pytest.mark.parametrize("field", ["st_ctime", "st_mtime"])
def test_round22_mutable_timestamps_never_bind(tmp_path, monkeypatch, field):
    import json

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    values = {"st_ctime": 20.0, "st_mtime": 20.0, field: 10.0}
    _round21_patch_identity(monkeypatch, checkout, no_birth=True, **values)
    token = write_checkout_generation(checkout)
    assert read_checkout_generation(checkout) == token
    binding = json.loads(worktree._generation_sidecar(checkout).read_text())
    assert binding["birth_field"] == "none"
    assert "birth_time" not in binding
    with monkeypatch.context() as changed:
        _round21_patch_identity(changed, checkout, no_birth=True, **{field: 11.0})
        assert read_checkout_generation(checkout) == token
    (checkout / ".git" / "kew-generation").unlink()
    with pytest.raises(worktree.CheckoutGenerationRefusal, match="nonce"):
        read_checkout_generation(checkout)


@pytest.mark.parametrize(
    "damage", ["missing", "different", "link", "unreadable", "metadata-link"]
)
def test_round21_nonce_is_required(tmp_path, monkeypatch, damage):
    checkout = tmp_path / "checkout"
    metadata = checkout / ".git"
    metadata.mkdir(parents=True)
    token = write_checkout_generation(checkout)
    nonce = metadata / "kew-generation"
    # Seed the second factor on the old implementation to isolate reader validation.
    nonce.write_text(json.loads(token)["nonce"] + "\n")
    if damage == "missing":
        nonce.unlink()
    elif damage == "different":
        nonce.write_text("f" * 32)
    elif damage == "link":
        target = tmp_path / "foreign-nonce"
        target.write_text(json.loads(token)["nonce"])
        nonce.unlink()
        nonce.symlink_to(target)
    elif damage == "metadata-link":
        metadata.rename(tmp_path / "foreign-metadata")
        metadata.symlink_to(tmp_path / "foreign-metadata", target_is_directory=True)
    else:
        nonce.write_bytes(b"\xff")
    with pytest.raises(worktree.CheckoutGenerationRefusal, match="nonce|metadata"):
        read_checkout_generation(checkout)


@pytest.mark.parametrize("unavailable", ["nan", "boolean"])
def test_round21_birth_identity_unavailable_refuses(tmp_path, monkeypatch, unavailable):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, st_birthtime=10.0)
    write_checkout_generation(checkout)
    _round21_patch_identity(
        monkeypatch,
        checkout,
        st_birthtime=float("nan") if unavailable == "nan" else True,
    )
    with pytest.raises(worktree.CheckoutGenerationRefusal, match="birth identity"):
        read_checkout_generation(checkout)


def test_round21_revocation_removes_both_factors(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    token = write_checkout_generation(checkout)
    nonce = checkout / ".git" / "kew-generation"
    nonce.write_text(json.loads(token)["nonce"] + "\n")
    revoke_checkout_generation(checkout)
    assert not worktree._generation_sidecar(checkout).exists()
    assert not nonce.exists()
    with worktree.checkout_claim_guard(checkout):
        fresh = worktree.capture_checkout_generation(checkout)
    assert fresh != token
    assert read_checkout_generation(checkout) == fresh
    assert nonce.read_text().strip() == json.loads(fresh)["nonce"]


def _round22_edit_root(checkout):
    """Create, delete, and rename real root entries without replacing the checkout."""
    (checkout / "delete-me").write_text("old content")
    (checkout / "rename-me").write_text("retained content")
    (checkout / "created").write_text("new content")
    (checkout / "delete-me").unlink()
    (checkout / "rename-me").rename(checkout / "renamed")


def test_round22_root_edits_preserve_generation(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(
        monkeypatch, checkout, no_birth=True, st_ctime=10.0, st_mtime=20.0
    )
    token = write_checkout_generation(checkout)
    sidecar = worktree._generation_sidecar(checkout).read_bytes()
    _round22_edit_root(checkout)
    _round21_patch_identity(
        monkeypatch, checkout, no_birth=True, st_ctime=30.0, st_mtime=40.0
    )
    assert read_checkout_generation(checkout) == token
    assert worktree._generation_sidecar(checkout).read_bytes() == sidecar
    assert (checkout / "created").read_text() == "new content"
    assert not (checkout / "delete-me").exists()
    assert (checkout / "renamed").read_text() == "retained content"


@pytest.mark.parametrize("nonce", ["missing", "different"])
def test_round22_recreated_without_birth_requires_nonce(tmp_path, monkeypatch, nonce):
    import json
    import shutil

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, no_birth=True)
    token = write_checkout_generation(checkout)
    identity = checkout.stat()
    binding = json.loads(worktree._generation_sidecar(checkout).read_text())
    assert binding["birth_field"] == "none"
    assert "birth_time" not in binding
    shutil.rmtree(checkout)
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(
        monkeypatch,
        checkout,
        no_birth=True,
        st_dev=identity.st_dev,
        st_ino=identity.st_ino,
    )
    if nonce == "different":
        (checkout / ".git" / "kew-generation").write_text("f" * 32)
        assert token != "f" * 32
    with pytest.raises(worktree.CheckoutGenerationRefusal, match="nonce"):
        read_checkout_generation(checkout)


def test_round22_darwin_birth_rejects_recreated_directory(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, st_birthtime=10.0)
    token = write_checkout_generation(checkout)
    # A platform without this field must still accept the unchanged checkout.
    with monkeypatch.context() as unavailable:
        _round21_patch_identity(unavailable, checkout, no_birth=True)
        assert read_checkout_generation(checkout) == token
    _round21_replace_reusing_identity(monkeypatch, checkout)
    # Preserve the nonce to isolate the immutable birth identity check.
    (checkout / ".git" / "kew-generation").write_text(json.loads(token)["nonce"])
    with pytest.raises(worktree.CheckoutGenerationRefusal, match="birth identity"):
        read_checkout_generation(checkout)


@pytest.mark.parametrize("damage", ["none", "device", "inode", "nonce"])
def test_round22_unavailable_recorded_birth_uses_remaining_factors(
    tmp_path, monkeypatch, damage
):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _round21_patch_identity(monkeypatch, checkout, st_birthtime=10.0)
    token = write_checkout_generation(checkout)
    sidecar = worktree._generation_sidecar(checkout).read_bytes()
    _round21_patch_identity(monkeypatch, checkout, no_birth=True)
    assert read_checkout_generation(checkout) == token
    assert worktree._generation_sidecar(checkout).read_bytes() == sidecar
    if damage == "none":
        return
    if damage == "nonce":
        (checkout / ".git" / "kew-generation").unlink()
    else:
        field = "st_dev" if damage == "device" else "st_ino"
        _round21_patch_identity(
            monkeypatch,
            checkout,
            no_birth=True,
            **{field: getattr(checkout.stat(), field) + 1},
        )
    with pytest.raises(worktree.CheckoutGenerationRefusal):
        read_checkout_generation(checkout)


def test_origin_fetch_never_reads_or_writes_the_shared_fetch_head(tmp_path: Path):
    # The operator repository is shared by concurrent dispatches and reviewers.
    # A fetch that resolved FETCH_HEAD could read another process's fetch and
    # pin its commit, so the bootstrap fetch lands in a ref of its own.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    _git("config", "user.email", "t@t.t", cwd=upstream)
    _git("config", "user.name", "t", cwd=upstream)
    (upstream / "f.txt").write_text("one\n")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-q", "-m", "one", cwd=upstream)
    operator = tmp_path / "operator"
    _git("clone", "-q", str(upstream), str(operator), cwd=tmp_path)
    (upstream / "f.txt").write_text("two\n")
    _git("commit", "-q", "-am", "two", cwd=upstream)
    expected = _git_out("rev-parse", "HEAD", cwd=upstream)
    sentinel = "0" * 40 + "\t\tbranch 'other' of elsewhere\n"
    (operator / ".git" / "FETCH_HEAD").write_text(sentinel)

    oid = worktree._fetch_commit_from_origin(operator, "main")

    assert oid == expected
    assert (operator / ".git" / "FETCH_HEAD").read_text() == sentinel
    assert _git_out("for-each-ref", "refs/kew", cwd=operator) == ""


def test_named_base_comes_from_origin_not_an_unpushed_local_branch(tmp_path: Path):
    # Audit 2026-10-06 S2, review round 2: the operator's local main can carry
    # a commit that was never pushed (a secret committed by mistake). A named
    # ref resolves on origin whenever there is one, so that commit's objects
    # never reach the agent's checkout.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    _git("config", "user.email", "t@t.t", cwd=upstream)
    _git("config", "user.name", "t", cwd=upstream)
    (upstream / "f.txt").write_text("public\n")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-q", "-m", "public", cwd=upstream)
    pushed = _git_out("rev-parse", "HEAD", cwd=upstream)
    operator = tmp_path / "operator"
    _git("clone", "-q", str(upstream), str(operator), cwd=tmp_path)
    _git("config", "user.email", "t@t.t", cwd=operator)
    _git("config", "user.name", "t", cwd=operator)
    (operator / ".env").write_text("TOKEN=never-pushed\n")
    _git("add", "-A", cwd=operator)
    _git("commit", "-q", "-m", "unpushed secret", cwd=operator)
    secret_blob = _git_out("rev-parse", "HEAD:.env", cwd=operator)
    checkout = tmp_path / "checkout"

    pinned = worktree.clone_local_checkout(operator, checkout, "main")

    assert pinned == (pushed,)
    assert _git_out("rev-parse", "HEAD", cwd=checkout) == pushed
    missing = subprocess.run(
        ["git", "cat-file", "-e", secret_blob], cwd=checkout, capture_output=True
    )
    assert missing.returncode != 0


@pytest.mark.parametrize(
    ("integrated", "remote_head", "expected"),
    [
        ("a" * 40, "b" * 40, "a" * 40),
        (None, "b" * 40, "b" * 40),
        (None, "", None),
    ],
)
def test_checkout_publication_filters_against_the_merged_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    integrated: str | None,
    remote_head: str,
    expected: str | None,
) -> None:
    """Checkout mode exempts the merged base's own bytes as canonical does (#2362)."""
    from kew import worktree

    branch = "feature/checkout-merged-base"
    checkout = _origin_repo_with_branch(tmp_path, branch)
    parent = _git_out("rev-parse", "HEAD", cwd=checkout)
    (checkout / "work.txt").write_text("repair\n")
    seen: list[str | None] = []
    real_remote_sha = worktree.publication_remote_sha

    def remote_sha(worktree_dir, name, *, repo=None):
        if name == "main":
            return remote_head
        return real_remote_sha(worktree_dir, name, repo=repo)

    def record(*_args, merged_base_sha=None, **_kwargs):
        seen.append(merged_base_sha)

    monkeypatch.setattr(worktree, "publication_remote_sha", remote_sha)
    monkeypatch.setattr(worktree, "filter_checkout_range", record)
    assert publish_existing_pr_branch(
        checkout,
        branch,
        commit_message="repair",
        expected_remote_sha=parent,
        publish="checkout",
        dangerously_skip_containment=True,
        merged_base_branch="main",
        integrated_base_sha=integrated,
    )
    assert seen == [expected]
