"""W1.1 node 4c: the in-container producer, driven end to end (D3).

The producer is a shell script because the kew-agent image has git, jq and
coreutils but no python3. It is exercised here against real git repositories on
the host — the script has no container dependency, and running it for real is
the only way to know the `-z` parsing and the JSON escaping are right.

Each test drives the WHOLE chain: produce the artifact, read it back with
``read_artifact``, and put it through ``filter_artifact``. A producer test that
only inspected the JSON would not notice the two halves disagreeing about the
format, which is the failure the chain exists to prevent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kew.publication_artifact import (
    MANIFEST_NAME,
    WORKSPACE_SCOPE,
    Artifact,
    ArtifactRejected,
    read_artifact,
)
from kew.publication_filter import filter_artifact

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "extract-artifact.sh"

# sha256sum is the one that bites: macOS ships `shasum`, not `sha256sum`, so
# without this the suite FAILS rather than skips on a developer machine that
# never installed coreutils.
_PRODUCER_TOOLS = ("bash", "git", "jq", "sha256sum")

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in _PRODUCER_TOOLS),
    reason=f"the producer needs {', '.join(_PRODUCER_TOOLS)}, as it does in the image",
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A repository with *files* committed, and the base commit id."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _produce(
    repo: Path,
    base: str,
    out: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), base, str(out)],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )


def _content(artifact: Artifact, path: str) -> bytes:
    entry = next(e for e in artifact.entries if e.path == path)
    return artifact.content(entry)


def _paths(artifact: Artifact) -> list[str]:
    return sorted(e.path for e in artifact.entries)


def _commit(repo: Path, message: str = "work") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


class TestTheWholeChain:
    def test_edits_additions_and_removals_survive_the_round_trip(self, tmp_path: Path):
        repo, base = _repo(
            tmp_path, {"a.txt": "one\n", "old.txt": "gone\n", "d/b.txt": "x\n"}
        )
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        (repo / "old.txt").unlink()
        (repo / "c.txt").write_text("new\n", encoding="utf-8")
        _commit(repo)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert sorted(e.path for e in artifact.entries) == ["a.txt", "c.txt"]
        assert artifact.deletions == ("old.txt",)
        assert artifact.base_sha == base
        # The content the host gets back is the content the agent committed.
        content = {e.path: artifact.content(e) for e in artifact.entries}
        assert content["a.txt"] == b"two\n"

        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])
        assert not verdict.refused

    def test_an_unusual_filename_survives_both_the_parser_and_the_json(
        self, tmp_path: Path
    ):
        """`-z` and `jq` between them are why this script does not quote paths.

        A space, a quote and a non-ASCII name each break a different naive
        implementation — git's own quoting on one side, hand-built JSON on the
        other.
        """
        repo, base = _repo(tmp_path, {"keep.txt": "k\n"})
        names = ["with space.txt", 'quote".txt', "café.txt", "d/nested one.txt"]
        for name in names:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
        _commit(repo)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert sorted(e.path for e in artifact.entries) == sorted(names)
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])
        assert not verdict.refused

    def test_a_newline_in_a_name_is_carried_faithfully_and_refused_by_the_host(
        self, tmp_path: Path
    ):
        """The name `-z` exists for.

        A newline is what makes a line-oriented parser WRONG rather than merely
        awkward, so the producer must carry it intact — and the host must then
        refuse it as a control character. Both halves matter: a producer that
        mangled it would hide the refusal, and a host that accepted it would
        take a path no filesystem should be asked to hold.
        """
        repo, base = _repo(tmp_path, {"keep.txt": "k\n"})
        (repo / "line\nbreak.txt").write_text("x\n", encoding="utf-8")
        _commit(repo)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert [e.path for e in artifact.entries] == ["line\nbreak.txt"]
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])
        assert {r.rule for r in verdict.rejections} == {"control_characters"}

    def test_a_pathspec_magic_name_resolves_to_its_own_content(self, tmp_path: Path):
        """`--` separates options from paths; it does NOT disable pathspec magic.

        Without `:(literal)`, `ls-tree HEAD -- ':(top)'` matches from the
        repository root and returns EVERY entry — and this script's parse takes
        the first line, so the artifact would claim this path holds another
        file's content. Asserting the digest is what catches that; asserting
        only that the entry exists would not.
        """
        repo, base = _repo(tmp_path, {"aaa-sorts-first.txt": "other\n"})
        (repo / ":(top)").write_text("its own content\n", encoding="utf-8")
        _commit(repo)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        entry = next(e for e in artifact.entries if e.path == ":(top)")
        assert artifact.content(entry) == b"its own content\n"
        # And the host refuses the name anyway, for the colon.
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])
        assert {r.rule for r in verdict.rejections} == {"colon"}

    def test_a_rename_arrives_as_a_delete_plus_an_add(self, tmp_path: Path):
        """`--no-renames`, verified rather than asserted in a comment.

        A rename record would ask the host to trust a pairing it cannot check.
        As two independent operations they go through the same rules as
        anything else — and the content has to survive the move.
        """
        repo, base = _repo(tmp_path, {"before.txt": "moved content\n"})
        _git(repo, "mv", "before.txt", "after.txt")
        _commit(repo)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert [e.path for e in artifact.entries] == ["after.txt"]
        assert artifact.deletions == ("before.txt",)
        assert artifact.content(artifact.entries[0]) == b"moved content\n"

        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])
        assert not verdict.refused

    def test_two_paths_with_identical_content_share_one_blob(self, tmp_path: Path):
        """The point of addressing by digest rather than by path."""
        repo, base = _repo(tmp_path, {"keep.txt": "k\n"})
        (repo / "one.txt").write_text("same\n", encoding="utf-8")
        (repo / "two.txt").write_text("same\n", encoding="utf-8")
        _commit(repo)

        _produce(repo, base, tmp_path / "artifact")
        artifact = read_artifact(tmp_path / "artifact")

        assert len(artifact.entries) == 2
        assert len({e.sha256 for e in artifact.entries}) == 1
        assert len(list((tmp_path / "artifact" / "blobs").iterdir())) == 1

    def test_an_empty_change_produces_an_empty_artifact(self, tmp_path: Path):
        """A run that changed nothing must still read as a valid artifact.

        And it must SAY so. An empty package is the one artifact whose meaning
        cannot be read off its own bytes — the host accepts it only because the
        producer declares it looked at the whole workspace.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert artifact.entries == ()
        assert artifact.deletions == ()
        assert artifact.scope == WORKSPACE_SCOPE


class TestTheWholeWorkspaceIsTheWorkPackage:
    """Committed, staged, unstaged and untracked work are one package.

    Extracting ``base..HEAD`` published only what the agent remembered to
    commit, and an agent that edited files and never ran ``git commit`` looked
    exactly like one that did nothing at all. Every case below is work that a
    commit-only producer silently threw away.
    """

    def test_staged_work_that_was_never_committed_is_carried(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        (repo / "added.txt").write_text("fresh\n", encoding="utf-8")
        _git(repo, "add", "-A")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["a.txt", "added.txt"]
        assert _content(artifact, "a.txt") == b"two\n"
        assert not filter_artifact(
            artifact, existing_modes={}, destination_paths=[]
        ).refused

    def test_unstaged_worktree_edits_are_carried(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("edited but never added\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _content(artifact, "a.txt") == b"edited but never added\n"

    def test_untracked_files_are_carried(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "brand-new.txt").write_text("never added\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["brand-new.txt"]
        assert _content(artifact, "brand-new.txt") == b"never added\n"

    def test_an_untracked_directory_is_carried_file_by_file(self, tmp_path: Path):
        """A directory is not a manifest entry; the files under it are."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "pkg" / "deep").mkdir(parents=True)
        (repo / "pkg" / "one.txt").write_text("1\n", encoding="utf-8")
        (repo / "pkg" / "deep" / "two.txt").write_text("2\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["pkg/deep/two.txt", "pkg/one.txt"]

    def test_an_untracked_executable_keeps_its_mode(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        script = repo / "tool.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert [(e.path, e.mode) for e in artifact.entries] == [("tool.sh", "100755")]

    def test_an_unstaged_deletion_is_carried(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n", "gone.txt": "doomed\n"})
        (repo / "gone.txt").unlink()

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert artifact.deletions == ("gone.txt",)
        assert artifact.entries == ()

    def test_a_staged_deletion_is_carried(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n", "gone.txt": "doomed\n"})
        _git(repo, "rm", "-q", "gone.txt")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert artifact.deletions == ("gone.txt",)

    def test_the_working_tree_wins_over_the_index_and_the_commit(self, tmp_path: Path):
        """Three versions of one file exist; the package carries the last one.

        Carrying the committed or the staged bytes would publish a state the
        agent had already moved past — a package that is complete in count and
        wrong in content.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("committed\n", encoding="utf-8")
        _commit(repo)
        (repo / "a.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        (repo / "a.txt").write_text("on disk\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _content(artifact, "a.txt") == b"on disk\n"

    def test_an_uncommitted_rename_arrives_as_a_delete_plus_an_add(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"before.txt": "moved content\n"})
        _git(repo, "mv", "before.txt", "after.txt")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["after.txt"]
        assert artifact.deletions == ("before.txt",)
        assert _content(artifact, "after.txt") == b"moved content\n"

    def test_a_path_dropped_from_the_index_but_still_on_disk_is_an_entry(
        self, tmp_path: Path
    ):
        """The one shape that reports as a deletion AND an addition.

        ``git rm --cached`` leaves the file exactly where it was, so the
        workspace still holds it. A package that carried the deletion would
        remove a file the agent never removed.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "rm", "-q", "--cached", "a.txt")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["a.txt"]
        assert artifact.deletions == ()
        assert _content(artifact, "a.txt") == b"one\n"

    def test_committed_staged_unstaged_and_untracked_arrive_together(
        self, tmp_path: Path
    ):
        repo, base = _repo(
            tmp_path,
            {
                "committed.txt": "before\n",
                "staged.txt": "before\n",
                "unstaged.txt": "before\n",
                "removed.txt": "doomed\n",
            },
        )
        (repo / "committed.txt").write_text("after\n", encoding="utf-8")
        _commit(repo)
        (repo / "staged.txt").write_text("after\n", encoding="utf-8")
        _git(repo, "add", "staged.txt")
        (repo / "unstaged.txt").write_text("after\n", encoding="utf-8")
        (repo / "removed.txt").unlink()
        (repo / "untracked.txt").write_text("after\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == [
            "committed.txt",
            "staged.txt",
            "unstaged.txt",
            "untracked.txt",
        ]
        assert artifact.deletions == ("removed.txt",)
        assert all(_content(artifact, path) == b"after\n" for path in _paths(artifact))
        assert not filter_artifact(
            artifact, existing_modes={}, destination_paths=[]
        ).refused

    def test_ignored_files_are_the_one_deliberate_omission(self, tmp_path: Path):
        """What the agent itself declared is not work.

        Build output and local secrets are not carried, and no ordinary git
        publication would carry them either — so this omission is a rule, not
        an accident, and it is asserted rather than assumed.
        """
        repo, base = _repo(tmp_path, {".gitignore": "build/\n.env\n"})
        (repo / "build").mkdir()
        (repo / "build" / "out.o").write_text("junk\n", encoding="utf-8")
        (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (repo / "real.txt").write_text("work\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["real.txt"]


class TestWhatTheProducerRefusesToCarry:
    def test_an_added_relative_symlink_is_refused(self, tmp_path: Path):
        """One uncarryable entry refuses the whole change; none may disappear."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to("a.txt")
        (repo / "real.txt").write_text("r\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_added_absolute_symlink_outside_the_repo_is_never_followed(
        self, tmp_path: Path
    ):
        """Refusal examines the tree entry, never the filesystem target."""
        outside = tmp_path / "outside.txt"
        outside.write_text(
            "content that must never enter the artifact\n", encoding="utf-8"
        )
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to(outside)
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()
        assert all(
            blob.read_bytes() != outside.read_bytes()
            for blob in (out / "blobs").iterdir()
        )

    def test_a_nested_symlink_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "d").mkdir()
        (repo / "d" / "link").symlink_to("../a.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "d/link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_regular_file_replaced_by_a_symlink_is_refused(self, tmp_path: Path):
        """A typechange is no safer than adding a symlink at a new path."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n", "link": "regular\n"})
        (repo / "link").unlink()
        (repo / "link").symlink_to("a.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_changed_symlink_is_refused(self, tmp_path: Path):
        """The base must contain the symlink for retargeting to report M."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "b.txt").write_text("two\n", encoding="utf-8")
        (repo / "link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "link").unlink()
        (repo / "link").symlink_to("b.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_deleted_symlink_is_refused(self, tmp_path: Path):
        """A deletion must be checked against the base tree before recording it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "link").unlink()
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_gitlink_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},sub")
        _git(repo, "commit", "-qm", "gitlink")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "sub" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_symlink_replaced_by_a_regular_file_is_refused(self, tmp_path: Path):
        """The base side of a typechange is judged too.

        Deleting a symlink refuses; replacing it with a regular file must not
        be the one-file bypass of that rule.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "link").unlink()
        (repo / "link").write_text("now a file\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_gitlink_replaced_by_a_regular_file_is_refused(self, tmp_path: Path):
        repo, base_seed = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base_seed},sub")
        _git(repo, "commit", "-qm", "gitlink base")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "rm", "-q", "--cached", "sub")
        (repo / "sub").write_text("now a file\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "sub" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_deleted_gitlink_is_refused(self, tmp_path: Path):
        repo, base_seed = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base_seed},sub")
        _git(repo, "commit", "-qm", "gitlink base")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "rm", "-q", "--cached", "sub")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "sub" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_quote_in_a_symlink_name_still_refuses(self, tmp_path: Path):
        """The refusal path must survive the names `-z` exists for."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / 'qu"ote').symlink_to("a.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert 'qu"ote' in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_executable_demotion_is_carried_not_refused(self, tmp_path: Path):
        """Both sides regular means both mode gates pass; the host judges."""
        repo, base = _repo(tmp_path, {"tool.sh": "#!/bin/sh\n"})
        (repo / "tool.sh").chmod(0o755)
        _commit(repo, "make executable")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "tool.sh").chmod(0o644)
        _commit(repo, "demote")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 0
        artifact = read_artifact(out)
        assert [(e.path, e.mode) for e in artifact.entries] == [("tool.sh", "100644")]

    def test_a_file_replaced_by_a_directory_round_trips(self, tmp_path: Path):
        """Git reports this as a delete plus adds; both arms must agree."""
        repo, base = _repo(tmp_path, {"thing": "was a file\n"})
        (repo / "thing").unlink()
        (repo / "thing").mkdir()
        (repo / "thing" / "inner.txt").write_text("now inside\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 0
        artifact = read_artifact(out)
        assert [e.path for e in artifact.entries] == ["thing/inner.txt"]
        assert artifact.deletions == ("thing",)

    def test_a_directory_replaced_by_a_file_round_trips(self, tmp_path: Path):
        """The base entry under the added file is a 040000 tree — a legitimate
        shape change, not a symlink. The base-side check must not refuse it."""
        repo, base = _repo(tmp_path, {"shape/inner.txt": "was inside\n"})
        (repo / "shape" / "inner.txt").unlink()
        (repo / "shape").rmdir()
        (repo / "shape").write_text("now a file\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 0
        artifact = read_artifact(out)
        assert [e.path for e in artifact.entries] == ["shape"]
        assert artifact.deletions == ("shape/inner.txt",)

    def test_a_symlink_replaced_by_a_directory_is_refused(self, tmp_path: Path):
        """The link's removal arrives as a deletion; the D arm judges it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "spot").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "spot").unlink()
        (repo / "spot").mkdir()
        (repo / "spot" / "inner.txt").write_text("now inside\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "spot" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_late_refusal_still_leaves_no_manifest(self, tmp_path: Path):
        """A refusal after a regular entry was already processed must still
        terminate the whole extraction — blobs may exist, a manifest may not."""
        repo, base = _repo(tmp_path, {"a-regular.txt": "one\n"})
        (repo / "a-regular.txt").write_text("changed\n", encoding="utf-8")
        (repo / "z-link").symlink_to("a-regular.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "z-link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_untouched_base_symlink_does_not_refuse(self, tmp_path: Path):
        """Only the CHANGE is judged. A symlink that predates the work and is
        not in the diff must not poison unrelated regular edits."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "old-link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 0
        artifact = read_artifact(out)
        assert [e.path for e in artifact.entries] == ["a.txt"]

    def test_a_symlink_only_change_cannot_masquerade_as_no_change(self, tmp_path: Path):
        """An empty valid artifact must mean no change, not omitted work."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to("a.txt")
        _commit(repo)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_it_refuses_a_populated_output_directory(self, tmp_path: Path):
        """Re-running into a used directory leaves blobs nothing references.

        The host refuses unreferenced content, so the second run would produce
        an artifact that cannot be read — failing far from the cause. Refusing
        here names it.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        _commit(repo)
        assert _produce(repo, base, tmp_path / "artifact").returncode == 0

        again = _produce(repo, base, tmp_path / "artifact")

        assert again.returncode == 7
        assert "not empty" in again.stderr

    def test_a_base_that_is_not_a_commit_is_refused(self, tmp_path: Path):
        repo, _base = _repo(tmp_path, {"a.txt": "one\n"})

        result = _produce(repo, "not-a-commit", tmp_path / "artifact")

        assert result.returncode == 4
        assert "not a commit" in result.stderr

    def test_it_refuses_rather_than_writing_half_an_artifact(self, tmp_path: Path):
        """A refused run must not leave a manifest the host would then read."""
        repo, _base = _repo(tmp_path, {"a.txt": "one\n"})

        _produce(repo, "not-a-commit", tmp_path / "artifact")

        assert not (tmp_path / "artifact" / MANIFEST_NAME).exists()


class TestWhatTheWiderWorkspaceRefusesToCarry:
    """The refusal matrix, extended to work that was never committed.

    Every case here was previously invisible: a commit-only producer never saw
    it, so it could neither carry it nor refuse it. Widening what is captured
    widens what must be refused by exactly the same amount, or the widening is
    itself the hole.
    """

    def test_an_untracked_symlink_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to("a.txt")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_untracked_absolute_symlink_is_never_followed(self, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("content that must never enter the artifact\n")
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to(outside)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert not (out / MANIFEST_NAME).exists()
        assert all(
            blob.read_bytes() != outside.read_bytes()
            for blob in (out / "blobs").iterdir()
        )

    def test_an_untracked_file_under_a_symlinked_directory_is_never_followed(
        self, tmp_path: Path
    ):
        """The component walk, not just the leaf.

        ``d/secret.txt`` looks like an ordinary nested path. If ``d`` is a link
        to somewhere outside the checkout, reading it is how content the agent
        never had enters the package.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("host content\n", encoding="utf-8")
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "d").symlink_to(outside, target_is_directory=True)
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert not (out / MANIFEST_NAME).exists()
        assert all(
            b"host content" not in blob.read_bytes()
            for blob in (out / "blobs").iterdir()
        )

    def test_a_staged_symlink_that_was_never_committed_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_unstaged_symlink_replacing_a_regular_file_is_refused(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"a.txt": "one\n", "link": "regular\n"})
        (repo / "link").unlink()
        (repo / "link").symlink_to("a.txt")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_unstaged_deletion_of_a_symlink_is_refused(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        (repo / "link").symlink_to("a.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "link").unlink()
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "link" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_gitlink_with_no_worktree_directory_is_refused(self, tmp_path: Path):
        """The hole a working-tree diff cannot see.

        Git reports NOTHING for a gitlink whose directory is absent, so the
        submodule would have left the package in silence. The index still
        remembers it, which is why the index is judged separately.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},sub")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 8
        assert "sub" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_a_tracked_file_replaced_by_a_fifo_is_refused_rather_than_read(
        self, tmp_path: Path
    ):
        """A FIFO is not a symlink, and opening one for reading BLOCKS.

        Git calls this a plain modification — it reports mode ``100644`` and no
        object id, because the content is only in the working tree. Reading it
        would hang the extraction until the timeout instead of saying what it
        found, so the file TYPE is proven before any read.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").unlink()
        os.mkfifo(repo / "a.txt")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 10
        assert "a.txt" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_untracked_fifo_is_omitted_exactly_as_git_omits_it(self, tmp_path: Path):
        """git's own scan skips it, so there is no work here to lose.

        Asserted rather than assumed: if git ever started listing them, this
        producer would read one — and the refusal above is what would catch it.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        os.mkfifo(repo / "pipe")
        (repo / "real.txt").write_text("work\n", encoding="utf-8")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert _paths(artifact) == ["real.txt"]

    def test_an_unmerged_path_is_refused(self, tmp_path: Path):
        """A conflicted path has no single state, so carrying one is a guess."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        trunk = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        _git(repo, "checkout", "-qb", "side")
        (repo / "a.txt").write_text("side\n", encoding="utf-8")
        _commit(repo, "side")
        _git(repo, "checkout", "-q", trunk)
        (repo / "a.txt").write_text("trunk\n", encoding="utf-8")
        _commit(repo, "trunk")
        subprocess.run(
            ["git", "-C", str(repo), "merge", "side"], capture_output=True, text=True
        )
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 6
        assert "unmerged" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    @pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
    def test_an_index_entry_git_refuses_to_look_at_is_refused(
        self, tmp_path: Path, flag: str
    ):
        """The quietest omission there is.

        With either bit set, an edit shows in NO enumeration: the working-tree
        diff is empty, the index blob is unchanged, and the file is not
        untracked. The package would be a verified no-change over work sitting
        on disk. There is no honest way to carry it, so the run refuses.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        _git(repo, "update-index", flag, "a.txt")
        (repo / "a.txt").write_text("invisible edit\n", encoding="utf-8")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out)

        assert result.returncode == 9
        assert "a.txt" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_enumeration_that_fails_refuses_rather_than_shortening(
        self, tmp_path: Path
    ):
        """`done < <(git ...)` throws git's exit status away.

        A git that dies halfway then yields a SHORT list, and the manifest
        calls everything it never reached a no-change. Enumeration is written
        to a file and its status checked before a single record is parsed.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "untracked.txt").write_text("work\n", encoding="utf-8")
        # An index git cannot read. Everything the base check needs still
        # works, so the run gets as far as enumerating and then cannot.
        broken = tmp_path / "broken.index"
        broken.write_bytes(b"not an index")
        out = tmp_path / "artifact"

        result = _produce(repo, base, out, env={"GIT_INDEX_FILE": str(broken)})

        assert result.returncode == 9
        assert "could not enumerate" in result.stderr
        assert not (out / MANIFEST_NAME).exists()

    def test_an_output_directory_inside_the_checkout_is_refused(self, tmp_path: Path):
        """Otherwise the artifact enumerates itself.

        Every blob written lands in the worktree as untracked content, which
        the loop still running would then carry — an artifact eating its own
        tail, and unbounded.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})

        result = _produce(repo, base, repo / "artifact")

        assert result.returncode == 11
        assert "inside the checkout" in result.stderr


class TestTheChainEndToEnd:
    """Producer to reader to filter, over a workspace with every shape in it."""

    @staticmethod
    def _mixed(tmp_path: Path) -> tuple[Path, str]:
        repo, base = _repo(
            tmp_path,
            {"committed.txt": "before\n", "staged.txt": "before\n", "gone.txt": "x\n"},
        )
        (repo / "committed.txt").write_text("after\n", encoding="utf-8")
        _commit(repo)
        (repo / "staged.txt").write_text("after\n", encoding="utf-8")
        _git(repo, "add", "staged.txt")
        (repo / "gone.txt").unlink()
        (repo / "pkg").mkdir()
        (repo / "pkg" / "untracked.txt").write_text("after\n", encoding="utf-8")
        return repo, base

    def test_the_same_workspace_produces_the_same_manifest_twice(self, tmp_path: Path):
        """Byte-for-byte, or the package cannot be lineage evidence.

        git's enumeration order is not part of anyone's contract, and untracked
        files are appended after tracked ones — so a package that only compared
        equal by luck would make two identical workspaces look like two
        different pieces of work.
        """
        repo, base = self._mixed(tmp_path)

        assert _produce(repo, base, tmp_path / "first").returncode == 0
        assert _produce(repo, base, tmp_path / "second").returncode == 0

        first = (tmp_path / "first" / MANIFEST_NAME).read_bytes()
        second = (tmp_path / "second" / MANIFEST_NAME).read_bytes()
        assert first == second
        assert sorted(p.name for p in (tmp_path / "first" / "blobs").iterdir()) == (
            sorted(p.name for p in (tmp_path / "second" / "blobs").iterdir())
        )

    def test_the_manifest_is_sorted_rather_than_however_git_listed_it(
        self, tmp_path: Path
    ):
        repo, base = self._mixed(tmp_path)

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        document = json.loads(
            (tmp_path / "artifact" / MANIFEST_NAME).read_text(encoding="utf-8")
        )

        paths = [entry["path"] for entry in document["entries"]]
        assert paths == sorted(paths)
        assert document["deletions"] == sorted(document["deletions"])
        assert document["scope"] == WORKSPACE_SCOPE

    def test_the_package_reads_and_filters_after_the_checkout_is_gone(
        self, tmp_path: Path
    ):
        """No host step needs the agent's Git metadata — proved by removing it.

        The host must never point git at a directory an agent can write, so
        every host-side step has to work from the opaque package alone. Nothing
        demonstrates that like deleting the checkout, `.git` and all, before the
        host reads a single byte.
        """
        repo, base = self._mixed(tmp_path)
        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        shutil.rmtree(repo)

        artifact = read_artifact(tmp_path / "artifact")
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])

        assert _paths(artifact) == [
            "committed.txt",
            "pkg/untracked.txt",
            "staged.txt",
        ]
        assert artifact.deletions == ("gone.txt",)
        assert not verdict.refused
        assert sorted(e.path for e in verdict.approved()) == _paths(artifact)
        assert tuple(verdict.deletions()) == ("gone.txt",)
        assert all(_content(artifact, path) == b"after\n" for path in _paths(artifact))

    def test_producing_does_not_touch_the_checkout(self, tmp_path: Path):
        """The extraction container mounts it read-only; this says why that is
        enough. A producer that staged blobs through `git hash-object` would
        need a writable object store and would fail there instead of here."""
        repo, base = self._mixed(tmp_path)
        before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
        head = _git(repo, "rev-parse", "HEAD")

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0

        assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before
        assert _git(repo, "rev-parse", "HEAD") == head

    def test_an_untouched_workspace_is_the_only_empty_package(self, tmp_path: Path):
        """Empty is accepted, and only ever means what it says.

        The refusal matrix above is the other half: every shape the producer
        cannot carry stops the run, so nothing reaches the host as an empty
        package by having been quietly dropped.
        """
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})

        assert _produce(repo, base, tmp_path / "artifact").returncode == 0
        artifact = read_artifact(tmp_path / "artifact")

        assert (artifact.entries, artifact.deletions) == ((), ())
        assert artifact.scope == WORKSPACE_SCOPE
        assert not filter_artifact(
            artifact, existing_modes={}, destination_paths=[]
        ).refused


class TestTheHostStillDecides:
    """The producer is not trusted; it just does not volunteer nonsense."""

    def test_a_mode_escalation_is_carried_honestly_and_refused_by_the_host(
        self, tmp_path: Path
    ):
        """The producer reports what was committed; the filter judges it."""
        repo, base = _repo(tmp_path, {"build.sh": "#!/bin/sh\n"})
        (repo / "build.sh").chmod(0o755)
        _commit(repo)

        _produce(repo, base, tmp_path / "artifact")
        artifact = read_artifact(tmp_path / "artifact")

        assert artifact.entries[0].mode == "100755"
        verdict = filter_artifact(
            artifact,
            existing_modes={"build.sh": "100644"},
            destination_paths=["build.sh"],
        )
        assert {r.rule for r in verdict.rejections} == {"mode_escalation"}

    def test_a_tampered_manifest_is_caught_by_the_host_not_the_producer(
        self, tmp_path: Path
    ):
        """Anything in the container can rewrite the artifact after this script
        runs, so the host's re-hash is the guarantee — not the producer."""
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        _commit(repo)
        _produce(repo, base, tmp_path / "artifact")

        manifest = tmp_path / "artifact" / MANIFEST_NAME
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["entries"][0]["path"] = ".git/config"
        manifest.write_text(json.dumps(document), encoding="utf-8")

        artifact = read_artifact(tmp_path / "artifact")
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])

        assert {r.rule for r in verdict.rejections} == {"dot_git"}

    def test_swapped_content_is_caught_by_the_hosts_re_hash(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": "one\n"})
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        _commit(repo)
        _produce(repo, base, tmp_path / "artifact")

        blob = next((tmp_path / "artifact" / "blobs").iterdir())
        blob.write_bytes(b"substituted after extraction")

        with pytest.raises(ArtifactRejected, match="hashes to"):
            read_artifact(tmp_path / "artifact")
