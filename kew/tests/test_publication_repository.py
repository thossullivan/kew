"""I09A: the canonical repository's commit surface.

I07 gave the host one repository it is allowed to trust and one way to turn a
preserved package into a tree in it. I09A adds the step after that: naming the
commit. The tests here are about the *git call*, not the lifecycle — that the
commit is exactly the intent it was given, that it is reproducible, and that
nothing an agent could have written into an environment or a config file can
move it.

The last property is the one that needs asserting rather than assuming. A
``GIT_DIR`` in the ambient environment answers questions about a repository
this module never named; a ``GIT_AUTHOR_DATE`` or a global ``user.name``
silently rewrites the commit; ``commit.gpgsign`` in a system config turns a
deterministic object into one that changes on every call. Each of those is
poisoned metadata under a different name, and each gets a test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.publication_job import CommitIdentity, CommitIntent
from kew.publication_repository import (
    VERIFICATION_EXACT_COMMIT,
    CanonicalRepository,
    CanonicalRepositoryError,
    commit_tree,
    read_commit,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="the canonical repository is git"
)

_WHEN = "@1787054400 +0000"
_IDENTITY = CommitIdentity(name="kew host", email="host@example.invalid")


@pytest.mark.parametrize("scheme", ["", "socks5://"])
def test_repository_failure_redacts_proxy_before_truncation(
    tmp_path, monkeypatch, scheme
):
    from unittest.mock import MagicMock

    from kew import publication_repository

    secret = f"{scheme}user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    failure = MagicMock(returncode=1, stderr="x" * 275 + " " + secret)
    monkeypatch.setattr(publication_repository, "run", MagicMock(return_value=failure))
    repo = MagicMock(path=tmp_path)
    with pytest.raises(CanonicalRepositoryError) as error:
        CanonicalRepository._git(repo, ["status"], "inspect")
    assert "s3cret" not in str(error.value)
    assert "<HTTP_PROXY redacted>" in str(error.value)


def test_verification_fetch_auth_uses_source_without_ambient_git(tmp_path, monkeypatch):
    from kew import publication, publication_repository

    preparation = getattr(publication_repository, "HostGitPreparation", None)
    assert preparation is not None, "verification needs an isolated Git boundary"
    monkeypatch.setenv(
        publication.DETACHED_PUBLICATION_TOKEN_ENV, "test-publication-credential"
    )
    monkeypatch.setattr(
        publication,
        "repo_host",
        lambda *a, **k: pytest.fail("verification auth must not run ambient Git"),
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess_utils, "run", run)
    publication_repository.provision(
        tmp_path / "repo",
        source="https://forge.example/repo.git",
        base_sha="a" * 40,
        git=preparation(tmp_path),
    )
    assert len(calls) == 3
    fetch_env = calls[1][1]
    assert fetch_env["GIT_CONFIG_KEY_0"] == "http.https://forge.example/.extraHeader"
    assert fetch_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GIT_CONFIG_COUNT" not in calls[0][1]
    assert "GIT_CONFIG_COUNT" not in calls[2][1]


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch):
    """Every git call goes through the subprocess chokepoint, which resolves
    executables from an allowlist that preflight normally fills."""
    resolved = shutil.which("git")
    assert resolved is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def canonical(tmp_path: Path) -> CanonicalRepository:
    """A bare host-owned repository holding one base commit."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", ".")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "keep.txt").write_text("keep\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    base = _git(seed, "rev-parse", "HEAD")

    dest = tmp_path / "canonical.git"
    dest.mkdir(mode=0o700)
    _git(dest.parent, "init", "-q", "--bare", str(dest))
    _git(seed, "push", "-q", str(dest), f"{base}:refs/heads/main")
    return CanonicalRepository(path=dest, base_sha=base)


def _intent(canonical: CanonicalRepository, **overrides: object) -> CommitIntent:
    fields: dict[str, object] = {
        "tree_sha": _git(canonical.path, "rev-parse", f"{canonical.base_sha}^{{tree}}"),
        "parent_sha": canonical.base_sha,
        "message": "feat(#7): the change the agent made",
        "author": _IDENTITY,
        "committer": _IDENTITY,
        "when": _WHEN,
    }
    fields.update(overrides)
    return CommitIntent(**fields)  # type: ignore[arg-type]


class TestTheCommitIsExactlyTheIntent:
    def test_it_names_the_tree_parent_and_message_it_was_given(
        self, canonical: CanonicalRepository
    ) -> None:
        intent = _intent(canonical)

        sha = commit_tree(canonical, intent)

        facts = read_commit(canonical, sha)
        assert facts.tree_sha == intent.tree_sha
        assert facts.parents == (canonical.base_sha,)
        assert _git(canonical.path, "log", "-1", "--format=%s", sha) == intent.message

    def test_the_author_and_committer_are_the_ones_the_intent_named(
        self, canonical: CanonicalRepository
    ) -> None:
        sha = commit_tree(canonical, _intent(canonical))

        who = _git(canonical.path, "log", "-1", "--format=%an|%ae|%cn|%ce|%at", sha)
        assert who == f"{_IDENTITY.name}|{_IDENTITY.email}|" + (
            f"{_IDENTITY.name}|{_IDENTITY.email}|1787054400"
        )

    def test_a_parentless_intent_creates_a_root_commit(
        self, canonical: CanonicalRepository
    ) -> None:
        """The first commit on an orphan branch has no parent to name."""
        sha = commit_tree(canonical, _intent(canonical, parent_sha=None))

        assert read_commit(canonical, sha).parents == ()

    def test_an_extra_parent_creates_a_merge_commit(
        self, canonical: CanonicalRepository
    ) -> None:
        extra = commit_tree(canonical, _intent(canonical, parent_sha=None))

        sha = commit_tree(
            canonical,
            _intent(canonical, extra_parents=(extra,)),
        )

        assert read_commit(canonical, sha).parents == (canonical.base_sha, extra)

    def test_the_commit_is_reachable_in_this_repository_and_no_other(
        self, canonical: CanonicalRepository
    ) -> None:
        sha = commit_tree(canonical, _intent(canonical))

        assert canonical.holds_commit(sha)
        assert not canonical.holds_commit("d" * 40)


class TestTheSameIntentNamesTheSameCommit:
    def test_committing_twice_produces_one_sha(
        self, canonical: CanonicalRepository
    ) -> None:
        """A crash after object creation retries into the same object."""
        intent = _intent(canonical)

        assert commit_tree(canonical, intent) == commit_tree(canonical, intent)

    def test_the_verification_this_repeatability_earns_is_named(self) -> None:
        assert VERIFICATION_EXACT_COMMIT == "exact_commit"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("message", "a different message"),
            ("when", "@1787054401 +0000"),
            ("committer", CommitIdentity(name="other", email="o@example.invalid")),
        ],
    )
    def test_any_changed_field_names_a_different_commit(
        self, canonical: CanonicalRepository, field: str, value: object
    ) -> None:
        first = commit_tree(canonical, _intent(canonical))

        assert commit_tree(canonical, _intent(canonical, **{field: value})) != first


class TestPoisonedGitMetadataCannotMoveTheCommit:
    def test_ambient_git_variables_do_not_reach_the_commit(
        self, canonical: CanonicalRepository, tmp_path: Path, monkeypatch
    ) -> None:
        """The agent's environment is the attacker-controlled one. ``GIT_DIR``
        would answer for another repository; ``GIT_AUTHOR_DATE`` would move the
        commit; ``GIT_CONFIG_*`` is how kew carries the publication token."""
        intent = _intent(canonical)
        clean = commit_tree(canonical, intent)

        for name, value in (
            ("GIT_DIR", str(tmp_path / "elsewhere.git")),
            ("GIT_AUTHOR_NAME", "agent"),
            ("GIT_AUTHOR_EMAIL", "agent@example.invalid"),
            ("GIT_AUTHOR_DATE", "@1 +0000"),
            ("GIT_COMMITTER_NAME", "agent"),
            ("GIT_COMMITTER_DATE", "@1 +0000"),
            ("GIT_INDEX_FILE", str(tmp_path / "agent.index")),
            ("GIT_CONFIG_COUNT", "1"),
            ("GIT_CONFIG_KEY_0", "user.name"),
            ("GIT_CONFIG_VALUE_0", "agent"),
        ):
            monkeypatch.setenv(name, value)

        assert commit_tree(canonical, intent) == clean

    def test_a_global_git_config_does_not_reach_the_commit(
        self, canonical: CanonicalRepository, tmp_path: Path, monkeypatch
    ) -> None:
        """A host with ``commit.gpgsign`` set globally would otherwise produce a
        signed — and therefore unrepeatable — commit."""
        clean = commit_tree(canonical, _intent(canonical))

        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = poisoned\n\temail = poisoned@example.invalid\n"
            "[commit]\n\tgpgsign = true\n"
        )
        monkeypatch.setenv("HOME", str(home))

        assert commit_tree(canonical, _intent(canonical)) == clean


class TestACommitThatCannotBeNamedIsRefused:
    def test_a_tree_this_repository_does_not_hold_is_refused(
        self, canonical: CanonicalRepository
    ) -> None:
        with pytest.raises(CanonicalRepositoryError, match="commit-tree"):
            commit_tree(canonical, _intent(canonical, tree_sha="c" * 40))

    def test_a_parent_this_repository_does_not_hold_is_refused(
        self, canonical: CanonicalRepository
    ) -> None:
        with pytest.raises(CanonicalRepositoryError, match="commit-tree"):
            commit_tree(canonical, _intent(canonical, parent_sha="d" * 40))

    @pytest.mark.parametrize("tree", ["HEAD", "--help", "", "c" * 39])
    def test_a_tree_that_is_not_an_object_id_never_reaches_git(
        self, canonical: CanonicalRepository, tree: str
    ) -> None:
        with pytest.raises(CanonicalRepositoryError, match="not a tree id"):
            commit_tree(canonical, _intent(canonical, tree_sha=tree))

    @pytest.mark.parametrize("parent", ["HEAD~1", "--upload-pack=evil", "d" * 39])
    def test_a_parent_that_is_not_an_object_id_never_reaches_git(
        self, canonical: CanonicalRepository, parent: str
    ) -> None:
        with pytest.raises(CanonicalRepositoryError, match="not a commit id"):
            commit_tree(canonical, _intent(canonical, parent_sha=parent))

    @pytest.mark.parametrize("name", ["HEAD", "main", "--help", ""])
    def test_reading_a_name_that_is_not_an_object_id_never_reaches_git(
        self, canonical: CanonicalRepository, name: str
    ) -> None:
        """A revision expression would let a caller ask about whatever the
        repository currently points at, not about the commit it just made."""
        with pytest.raises(CanonicalRepositoryError, match="not a commit id"):
            read_commit(canonical, name)

    def test_reading_a_commit_that_is_not_there_is_refused(
        self, canonical: CanonicalRepository
    ) -> None:
        with pytest.raises(CanonicalRepositoryError, match="cat-file"):
            read_commit(canonical, "d" * 40)

    def test_reading_a_tree_as_a_commit_is_refused(
        self, canonical: CanonicalRepository
    ) -> None:
        tree = _git(canonical.path, "rev-parse", f"{canonical.base_sha}^{{tree}}")

        with pytest.raises(CanonicalRepositoryError, match="not a commit"):
            read_commit(canonical, tree)
