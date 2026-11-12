"""W1.1 node 5a: the artifact becomes a tree, and the tree is EXACTLY right.

The load-bearing test in this file is the round trip. Applying the diff base→
HEAD onto base must reproduce HEAD's tree bit for bit, so a single sha compare
against the tree the legacy path publishes is a total test of whether the broker
drops anything. Every file class this chain could mishandle — executable bits,
non-UTF-8 bytes, deletions, renames, a directory that empties out — is checked
by that same equality rather than by an assertion about what we think happened.

The applier is driven through the REAL producer wherever the input has to be
believable, because a hand-written manifest tests agreement with itself.

The destination repo here is **bare**. Nothing in this module writes a working
tree, and a bare repo is how that stops being a claim: there is no worktree to
write into, so a test would fail rather than quietly pass if a path ever reached
the filesystem.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.artifact_receipt import RECEIPT_SEALED_EVENT, seal_package_receipt
from kew.artifact_store import PACKAGE_ARTIFACT_KIND, PackageStore, PackageTampered
from kew.publication_apply import ApplyFailed, _git_env, apply_to_tree
from kew.publication_artifact import read_artifact
from kew.publication_filter import ManifestEntry, filter_artifact
from kew.publication_repository import (
    VERIFICATION_EXACT_TREE,
    CanonicalRepositoryError,
    _fetch_env,
    _is_remote_url,
    promote,
    provision,
)
from kew.run_store import SqliteRunStore

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "extract-artifact.sh"

_PRODUCER_TOOLS = ("bash", "git", "jq", "sha256sum")

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in _PRODUCER_TOOLS),
    reason=f"the producer needs {', '.join(_PRODUCER_TOOLS)}, as it does in the image",
)


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch):
    """The applier goes through the subprocess chokepoint, which resolves
    executables from an allowlist that preflight normally fills. ``setitem``
    rather than ``preflight_tools()`` so the global is restored afterwards."""
    resolved = shutil.which("git")
    assert resolved is not None  # the module-level skipif already guarantees it
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(
    tmp_path: Path, files: dict[str, bytes], object_format: str = "sha1"
) -> tuple[Path, str]:
    repo = tmp_path / "agent"
    repo.mkdir()
    _git(repo, "init", "-q", f"--object-format={object_format}", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _commit(repo: Path, message: str = "work") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _produce(repo: Path, base: str, out: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), base, str(out)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _clean_clone(
    tmp_path: Path, source: Path, base: str, name: str = "clean.git"
) -> Path:
    """A destination holding the BASE COMMIT AND NOTHING ELSE.

    Bare, so it has no worktree: nothing in the applier writes files, and a repo
    with nowhere to write them is how that stops being a claim.

    It is built by pushing *base* into an empty repo rather than by cloning the
    fixture, because cloning would carry the agent's objects along with it — and
    a destination that already has the agent's blobs cannot notice an applier
    that never wrote them. In production the destination is the canonical
    repository, which has the base and none of the work. The fixture has to have
    the same shape or it tests something easier than reality.
    """
    dest = tmp_path / name
    # The destination's object format must match the source's, or the push
    # cannot transfer anything — which is also true in production.
    object_format = _git(source, "rev-parse", "--show-object-format=storage")
    subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            "--bare",
            f"--object-format={object_format}",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    _git(source, "push", "--quiet", str(dest), f"{base}:refs/heads/base")
    return dest


def _apply(
    tmp_path: Path,
    repo: Path,
    base: str,
    out_name: str = "artifact",
    dest: Path | None = None,
) -> str:
    """Produce → read → filter → apply, returning the resulting tree sha.

    *dest* is accepted so a test can inspect the repository the applier actually
    wrote into. Building a second clone to look at would answer a question about
    a different repository — which is how the missing-``-w`` mutation first
    escaped.
    """
    artifact_dir = tmp_path / out_name
    _produce(repo, base, artifact_dir)
    artifact = read_artifact(artifact_dir)
    verdict = filter_artifact(artifact, existing_modes={}, destination_paths=())
    if dest is None:
        dest = _clean_clone(tmp_path, repo, base)
    return apply_to_tree(
        dest,
        base_sha=artifact.base_sha,
        approved=verdict.approved(),
        deletions=verdict.deletions(),
        content=artifact.content,
        index_file=tmp_path / f"{out_name}.index",
    )


class TestTheTreeIsExact:
    """One sha, no allowances. This is what shadow mode compares."""

    def test_add_modify_delete_reproduces_head_exactly(self, tmp_path: Path):
        repo, base = _repo(
            tmp_path,
            {
                "keep.txt": b"unchanged\n",
                "edit.txt": b"before\n",
                "gone.txt": b"doomed\n",
                "deep/nested/file.txt": b"deep\n",
            },
        )
        _write(repo, {"edit.txt": b"after\n", "new/dir/added.txt": b"added\n"})
        (repo / "gone.txt").unlink()
        _commit(repo)

        assert _apply(tmp_path, repo, base) == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_the_executable_bit_survives(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"script.sh": b"#!/bin/sh\n"})
        (repo / "script.sh").chmod(0o755)
        _commit(repo)

        # Stated separately from the tree compare: if the mode were dropped the
        # trees would differ, but the failure would not say why.
        assert "100755" in _git(repo, "ls-tree", "HEAD", "script.sh")
        assert _apply(tmp_path, repo, base) == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_content_that_is_not_utf8_survives(self, tmp_path: Path):
        # A blob decoded to str and re-encoded would not round-trip these bytes,
        # and the tree sha is what notices.
        repo, base = _repo(tmp_path, {"seed.txt": b"seed\n"})
        _write(repo, {"binary.bin": bytes(range(256)) + b"\xff\xfe\x00\x80"})
        _commit(repo)

        assert _apply(tmp_path, repo, base) == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_a_rename_reproduces_head_exactly(self, tmp_path: Path):
        # The producer emits a rename as a delete plus an add. The tree is the
        # proof that the pair reassembles into the same thing git had.
        repo, base = _repo(tmp_path, {"old/name.txt": b"content\n"})
        _git(repo, "mv", "old/name.txt", "new-name.txt")
        _commit(repo)

        assert _apply(tmp_path, repo, base) == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_a_directory_that_empties_out_disappears(self, tmp_path: Path):
        # git trees have no empty directories. An applier that removed the file
        # but left the directory in the index would build a different tree.
        repo, base = _repo(tmp_path, {"keep.txt": b"k\n", "doomed/only.txt": b"only\n"})
        (repo / "doomed" / "only.txt").unlink()
        _commit(repo)

        tree = _apply(tmp_path, repo, base)
        assert tree == _git(repo, "rev-parse", "HEAD^{tree}")
        assert "doomed" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD")

    def test_a_no_op_branch_reproduces_the_base_tree(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"only.txt": b"only\n"})

        base_tree = _git(repo, "rev-parse", f"{base}^{{tree}}")
        assert _apply(tmp_path, repo, base) == base_tree


class TestTheObjectFormatIsTheRepositorySAndNotOURS:
    def test_a_sha256_repository_round_trips_including_a_deletion(self, tmp_path: Path):
        """A removal record's placeholder id must be the repository's width.

        ``update-index --index-info`` takes "mode SP id TAB path", and mode 0
        removes. The id is ignored for a removal but still has to PARSE, so a
        40-zero placeholder in a SHA-256 repository is ``fatal: malformed index
        info`` — and because every record travels in one call, a single deletion
        fails the whole apply. Hard-coding 40 makes the module quietly
        SHA-1-only.
        """
        repo, base = _repo(
            tmp_path,
            {"keep.txt": b"k\n", "gone.txt": b"g\n"},
            object_format="sha256",
        )
        _write(repo, {"added.txt": b"a\n"})
        (repo / "gone.txt").unlink()
        _commit(repo)

        assert _git(repo, "rev-parse", "--show-object-format=storage") == "sha256"
        assert _apply(tmp_path, repo, base) == _git(repo, "rev-parse", "HEAD^{tree}")


class TestAmbientGitEnvironmentCannotSteerIt:
    def test_an_inherited_git_dir_does_not_redirect_the_applier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``GIT_DIR`` beats ``-C``. Verified, not assumed.

        ``GIT_DIR=<decoy> git -C <named> rev-parse --git-dir`` answers
        ``<decoy>``: the repository this module is handed is not the repository
        it would operate on. Objects would be written, and the tree built, in
        one the caller never named — from nothing more than an exported
        variable, a git hook, or the ``GIT_CONFIG_*`` kew itself uses to carry
        the publication token.
        """
        repo, base = _repo(tmp_path, {"seed.txt": b"seed\n"})
        _write(repo, {"added.txt": b"added\n"})
        _commit(repo)
        expected = _git(repo, "rev-parse", "HEAD^{tree}")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        _git(decoy, "init", "-q", ".")
        before = sorted(p.name for p in (decoy / ".git" / "objects").rglob("*"))

        # Everything the harness needs happens BEFORE the variables are set:
        # they poison the producer and the fixture's own git calls just as
        # readily, which is the point being made.
        artifact_dir = tmp_path / "artifact"
        _produce(repo, base, artifact_dir)
        artifact = read_artifact(artifact_dir)
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=())
        dest = _clean_clone(tmp_path, repo, base)

        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))

        tree = apply_to_tree(
            dest,
            base_sha=artifact.base_sha,
            approved=verdict.approved(),
            deletions=verdict.deletions(),
            content=artifact.content,
            index_file=tmp_path / "scratch.index",
        )

        assert tree == expected
        assert sorted(p.name for p in (decoy / ".git" / "objects").rglob("*")) == before

    def test_the_environment_carries_nothing_git_did_not_ask_for(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The behavioural test above proves it matters; this states the rule, so
        # a newly-inherited GIT_* variable fails here rather than in whichever
        # flow happens to set it.
        monkeypatch.setenv("GIT_DIR", "/nowhere/.git")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/nowhere/objects")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = _git_env(tmp_path / "scratch.index")

        assert {key for key in env if key.startswith("GIT_")} == {
            "GIT_INDEX_FILE",
            "GIT_NO_REPLACE_OBJECTS",
            "GIT_NO_LAZY_FETCH",
            # The inert settings this module sets itself (#2569 review): no
            # system, global or attributes file, and only its own command-scope
            # config — the inherited GIT_CONFIG_KEY_0 above is overwritten.
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_ATTR_NOSYSTEM",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_CONFIG_KEY_1",
            "GIT_CONFIG_VALUE_1",
            "GIT_CONFIG_KEY_2",
            "GIT_CONFIG_VALUE_2",
        }
        # The exact values, not just the names: a key set to the wrong value is
        # exactly as live as one left out (#2591).
        assert [
            (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(3)
        ] == [
            ("core.hooksPath", os.devnull),
            ("core.fsmonitor", ""),
            ("core.attributesFile", os.devnull),
        ]
        assert env["GIT_INDEX_FILE"] == str(tmp_path / "scratch.index")
        # Scrubbing GIT_*, not the environment: git still needs to run.
        assert env["PATH"] == "/usr/bin:/bin"


class TestTheTreeIsAlsoREACHABLE:
    def test_every_blob_the_tree_names_is_in_the_object_database(self, tmp_path: Path):
        """A matching sha is not the same as a usable tree.

        ``hash-object`` without ``-w`` still prints the id it computed, because
        the id comes from the content — so the tree assembles, and its sha
        compares EQUAL to the real one, while none of the blobs were ever
        written. Shadow mode would report a clean match on a repository that
        cannot be committed or pushed. The sha check cannot catch this by
        construction; only reading the content back can.
        """
        repo, base = _repo(tmp_path, {"seed.txt": b"seed\n"})
        _write(repo, {"added.txt": b"added\n", "deep/one.txt": b"deep\n"})
        _commit(repo)

        dest = _clean_clone(tmp_path, repo, base, name="reachable.git")
        tree = _apply(tmp_path, repo, base, dest=dest)
        listing = _git(dest, "ls-tree", "-r", tree)

        assert listing, "the tree should name blobs"
        for line in listing.splitlines():
            oid = line.split()[2]
            # cat-file, not cat-file -e: reading the content is what a commit
            # and a push actually do.
            assert _git(dest, "cat-file", "blob", oid) is not None
            assert _git(dest, "cat-file", "-t", oid) == "blob"


class TestThePathIsTakenLITERALLY:
    def test_a_name_that_looks_c_quoted_is_not_unquoted(self, tmp_path: Path):
        """``--index-info`` without ``-z`` reads C-quoted paths.

        A file named ``"quoted".txt`` is then written to the index as
        ``quoted`` — a different path, silently, with no error anywhere. That is
        a path-confusion primitive handed to an attacker who controls filenames,
        so the format is NUL-terminated and the name is data.
        """
        repo, base = _repo(tmp_path, {"seed.txt": b"seed\n"})
        _write(repo, {'"quoted".txt': b"quoted\n"})
        _commit(repo)

        dest = _clean_clone(tmp_path, repo, base, name="listing.git")
        tree = _apply(tmp_path, repo, base, dest=dest)
        assert tree == _git(repo, "rev-parse", "HEAD^{tree}")
        # Named explicitly: the tree compare above catches this, but only this
        # says what went wrong when it does.
        # -z on the way out as well: ls-tree C-quotes unusual names in its own
        # output, so a listing read as text would disagree with the tree it is
        # describing for exactly the names this test is about.
        raw = _git(dest, "ls-tree", "-r", "-z", "--name-only", tree)
        names = [name for name in raw.split("\0") if name]
        assert names == ['"quoted".txt', "seed.txt"]


class TestNothingReachesAFilesystem:
    def test_the_destination_needs_no_working_tree(self, tmp_path: Path):
        # Already true of every test above by construction — the clone is bare.
        # Named here so the property is asserted rather than merely arranged.
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        _write(repo, {"b.txt": b"b\n"})
        _commit(repo)
        dest = _clean_clone(tmp_path, repo, base, name="inspected.git")

        assert _git(dest, "rev-parse", "--is-bare-repository") == "true"
        tree = _apply(tmp_path, repo, base, dest=dest)
        assert tree == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_the_index_is_the_caller_s_and_nothing_else_is_touched(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        _write(repo, {"b.txt": b"b\n"})
        _commit(repo)
        artifact_dir = tmp_path / "artifact"
        _produce(repo, base, artifact_dir)
        artifact = read_artifact(artifact_dir)
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=())
        dest = _clean_clone(tmp_path, repo, base)
        index_file = tmp_path / "scratch.index"

        apply_to_tree(
            dest,
            base_sha=artifact.base_sha,
            approved=verdict.approved(),
            deletions=verdict.deletions(),
            content=artifact.content,
            index_file=index_file,
        )

        assert index_file.exists()
        # A bare repo has no default index; if the applier had ignored
        # GIT_INDEX_FILE it would have created one here instead.
        assert not (dest / "index").exists()


class TestDeletionsAreNotDropped:
    def test_a_deletion_alone_changes_the_tree(self, tmp_path: Path):
        # Deletions travel in the same update-index call as the writes. Dropping
        # them is the failure that reads as success everywhere else: the branch
        # still has all the right content, just also the content it removed.
        repo, base = _repo(tmp_path, {"keep.txt": b"k\n", "gone.txt": b"g\n"})
        (repo / "gone.txt").unlink()
        _commit(repo)

        tree = _apply(tmp_path, repo, base)
        assert tree == _git(repo, "rev-parse", "HEAD^{tree}")
        assert tree != _git(repo, "rev-parse", f"{base}^{{tree}}")


class TestItRefusesRatherThanGuesses:
    def test_a_base_the_destination_does_not_have(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        dest = _clean_clone(tmp_path, repo, base)

        with pytest.raises(ApplyFailed, match="read-tree"):
            apply_to_tree(
                dest,
                base_sha="0" * 40,
                approved=(),
                deletions=(),
                content=lambda entry: b"",
                index_file=tmp_path / "scratch.index",
            )

    def test_content_that_cannot_be_produced_propagates(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        dest = _clean_clone(tmp_path, repo, base)

        def explode(entry: ManifestEntry) -> bytes:
            raise OSError("blob unreadable")

        with pytest.raises(OSError, match="blob unreadable"):
            apply_to_tree(
                dest,
                base_sha=base,
                approved=(ManifestEntry(path="x.txt", mode="100644", sha256="0" * 64),),
                deletions=(),
                content=explode,
                index_file=tmp_path / "scratch.index",
            )


class TestTheCanonicalRepositoryIsTheOnlyOneTheHostReads:
    """I07: provisioning refuses a source whose git metadata an agent owns.

    ``git fetch <path>`` runs ``upload-pack`` inside that path, reading that
    path's configuration. The agent's checkout is therefore not merely an unsafe
    place to write — it is an unsafe place to READ, and the refusal has to land
    before git is started rather than after.
    """

    def test_it_holds_the_trusted_base_and_none_of_the_agents_work(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"keep.txt": b"unchanged\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        _write(repo, {"keep.txt": b"agent edited\n"})
        _commit(repo)
        agent_head = _git(repo, "rev-parse", "HEAD")

        canonical = provision(
            tmp_path / "canonical.git", source=str(mirror), base_sha=base
        )

        assert canonical.base_sha == base
        present = subprocess.run(
            ["git", "-C", str(canonical.path), "cat-file", "-e", agent_head],
            capture_output=True,
        )
        assert present.returncode != 0
        assert canonical.base_state() == {"keep.txt": "100644"}

    def test_an_agent_writable_source_is_refused_before_git_reads_it(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        mirror.chmod(0o777)

        with pytest.raises(CanonicalRepositoryError, match="writable"):
            provision(tmp_path / "canonical.git", source=str(mirror), base_sha=base)
        assert not (tmp_path / "canonical.git").exists()

    def test_a_symlinked_source_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        (tmp_path / "link.git").symlink_to(mirror)

        with pytest.raises(CanonicalRepositoryError, match="symlink"):
            provision(
                tmp_path / "canonical.git",
                source=str(tmp_path / "link.git"),
                base_sha=base,
            )

    @pytest.mark.parametrize("base", ["HEAD", "", "../../etc/passwd", "z" * 40])
    def test_a_base_that_is_not_a_commit_id_never_reaches_git(
        self, tmp_path: Path, base: str
    ):
        with pytest.raises(CanonicalRepositoryError, match="commit id"):
            provision(tmp_path / "canonical.git", source=str(tmp_path), base_sha=base)

    def test_a_base_the_source_does_not_have_is_a_refusal_not_an_empty_repo(
        self, tmp_path: Path
    ):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")

        with pytest.raises(CanonicalRepositoryError):
            provision(tmp_path / "canonical.git", source=str(mirror), base_sha="b" * 40)

    def test_provisioning_over_an_existing_destination_is_refused(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        (tmp_path / "canonical.git").mkdir()

        with pytest.raises(CanonicalRepositoryError, match="already exists"):
            provision(tmp_path / "canonical.git", source=str(mirror), base_sha=base)


class TestPromotionIsTheExactTree:
    """I07: the package becomes one immutable tree, or it becomes nothing."""

    def _promoted(self, tmp_path: Path, files: dict[str, bytes], edits):
        repo, base = _repo(tmp_path, files)
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        edits(repo)
        artifact_dir = tmp_path / "artifact"
        _produce(repo, base, artifact_dir)
        canonical = provision(
            tmp_path / "canonical.git", source=str(mirror), base_sha=base
        )
        artifact = read_artifact(artifact_dir)
        return repo, canonical, artifact

    def test_it_reproduces_the_agents_own_tree_bit_for_bit(self, tmp_path: Path):
        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n", "new/added.txt": b"added\n"})
            (repo / "gone.txt").unlink()
            _commit(repo)

        repo, canonical, artifact = self._promoted(
            tmp_path,
            {"edit.txt": b"before\n", "gone.txt": b"doomed\n", "keep.txt": b"k\n"},
            edits,
        )
        promoted = promote(canonical, artifact, scratch=tmp_path)

        assert promoted.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")
        assert promoted.base_sha == canonical.base_sha
        assert promoted.verification == VERIFICATION_EXACT_TREE
        assert promoted.deletion_count == 1
        assert canonical.holds_tree(promoted.tree_sha)

    def test_uncommitted_work_promotes_the_same_as_committed_work(self, tmp_path: Path):
        """The package spans the workspace (I06), so the tree does too."""

        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n"})  # never committed

        repo, canonical, artifact = self._promoted(
            tmp_path, {"edit.txt": b"before\n"}, edits
        )
        promoted = promote(canonical, artifact, scratch=tmp_path)

        _commit(repo)
        assert promoted.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")

    def test_promoting_the_same_package_twice_names_the_same_tree(self, tmp_path: Path):
        def edits(repo: Path) -> None:
            _write(repo, {"a.txt": b"changed\n"})
            _commit(repo)

        _repo_, canonical, artifact = self._promoted(tmp_path, {"a.txt": b"a\n"}, edits)
        first = promote(canonical, artifact, scratch=tmp_path)
        second = promote(canonical, artifact, scratch=tmp_path)

        assert first == second

    def test_a_package_that_names_another_base_is_refused(self, tmp_path: Path):
        def edits(repo: Path) -> None:
            _write(repo, {"a.txt": b"changed\n"})
            _commit(repo)

        _repo_, canonical, artifact = self._promoted(tmp_path, {"a.txt": b"a\n"}, edits)
        forged = replace(artifact, base_sha="c" * 40)

        with pytest.raises(CanonicalRepositoryError, match="trusted base"):
            promote(canonical, forged, scratch=tmp_path)


class TestTheWholeLineageSeals:
    """I07 end to end: produce → preserve → promote → seal, and recover it.

    Every step here runs against the REAL producer and REAL git, because the
    thing being tested is that the links hold across the seams — a hand-built
    package promoted into a hand-built repository would only prove the fixtures
    agree with each other.
    """

    SEALED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

    def _chain(self, tmp_path: Path, edits, files=None):
        repo, base = _repo(tmp_path, files or {"edit.txt": b"before\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        edits(repo)
        _produce(repo, base, tmp_path / "artifact")
        store = PackageStore(tmp_path / "store")
        package = store.preserve(read_artifact(tmp_path / "artifact"))
        canonical = provision(
            tmp_path / "canonical.git", source=str(mirror), base_sha=base
        )
        promoted = promote(canonical, store.load(package.digest), scratch=tmp_path)
        return repo, store, package, promoted

    def _seal(self, ledger, package, promoted, run_id="run-1"):
        return seal_package_receipt(
            ledger,
            package=package,
            promoted=promoted,
            source_kind="github",
            source_id="acme/widgets",
            run_id=run_id,
            attempt_number=1,
            sealed_at=self.SEALED_AT,
        )

    def test_the_receipt_binds_the_package_the_tree_and_the_prefix(
        self, tmp_path: Path
    ):
        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n", "new/added.txt": b"added\n"})

        repo, _store, package, promoted = self._chain(tmp_path, edits)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as ledger:
            receipt = self._seal(ledger, package, promoted)
            verdict = ledger.verify_artifact_receipt(run_id="run-1", attempt_number=1)
            indexed = ledger.get_artifact(
                run_id="run-1", attempt_number=1, kind=PACKAGE_ARTIFACT_KIND
            )

        _commit(repo)
        assert receipt.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")
        assert receipt.base_sha == promoted.base_sha
        assert receipt.package_digest == package.digest
        assert receipt.verification == VERIFICATION_EXACT_TREE
        assert verdict.ok, verdict.detail
        assert indexed is not None
        assert indexed.digest == package.digest
        assert indexed.locator == package.locator
        assert indexed.byte_length == package.byte_length

    def test_the_tree_comes_from_the_preserved_copy_not_the_agents_directory(
        self, tmp_path: Path
    ):
        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n"})

        repo, store, package, promoted = self._chain(tmp_path, edits)
        shutil.rmtree(tmp_path / "artifact")
        shutil.rmtree(repo)

        again = promote(
            provision(
                tmp_path / "second.git",
                source=str(tmp_path / "mirror.git"),
                base_sha=promoted.base_sha,
            ),
            store.load(package.digest),
            scratch=tmp_path / "again",
        )
        assert again.tree_sha == promoted.tree_sha

    def test_sealing_again_after_a_restart_is_the_same_receipt(self, tmp_path: Path):
        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n"})

        _repo_, store, package, promoted = self._chain(tmp_path, edits)
        db = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db) as ledger:
            first = self._seal(ledger, package, promoted)

        # Restart: the store, the ledger, and the placement all come back.
        restarted = PackageStore(store.root)
        again = restarted.preserve(restarted.load(package.digest))
        with SqliteRunStore(db) as ledger:
            second = self._seal(ledger, again, promoted)
            events = ledger._conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event = ?",
                (RECEIPT_SEALED_EVENT,),
            ).fetchone()[0]

        assert again.digest == package.digest
        assert second == first
        assert events == 1

    def test_producing_the_same_workspace_twice_is_one_package(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"edit.txt": b"before\n"})
        _write(repo, {"edit.txt": b"after\n", "untracked.txt": b"new\n"})
        _produce(repo, base, tmp_path / "first")
        _produce(repo, base, tmp_path / "second")
        os.utime(tmp_path / "second" / "manifest.json", (0, 0))

        store = PackageStore(tmp_path / "store")
        first = store.preserve(read_artifact(tmp_path / "first"))
        second = store.preserve(read_artifact(tmp_path / "second"))

        assert first == second
        assert len(list((store.root / "packages").iterdir())) == 1

    def test_evidence_edited_after_sealing_stops_loading_and_says_so(
        self, tmp_path: Path
    ):
        def edits(repo: Path) -> None:
            _write(repo, {"edit.txt": b"after\n"})

        _repo_, store, package, promoted = self._chain(tmp_path, edits)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as ledger:
            self._seal(ledger, package, promoted)
            blob = next((package.root / "blobs").iterdir())
            blob.chmod(0o600)
            blob.write_bytes(b"substituted\n")

            with pytest.raises(PackageTampered):
                store.load(package.digest)
            # The ledger is not what was edited, and must not claim to be: the
            # receipt still stands, and it is the digest in it that convicts
            # the bytes on disk.
            verdict = ledger.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert verdict.ok, verdict.detail


class TestASourceIsClassifiedBeforeItIsTrusted:
    """Whether a source is a URL decides whether it gets the credential."""

    @pytest.mark.parametrize(
        "source",
        [
            "https://github.com/acme/widgets.git",
            "ssh://git@github.com/acme/widgets.git",
            "git@github.com:acme/widgets.git",
        ],
    )
    def test_a_remote_is_a_remote(self, source: str):
        assert _is_remote_url(source)

    @pytest.mark.parametrize(
        "source", ["/srv/mirror.git", "./mirror.git", "mirror.git", "../up.git"]
    )
    def test_a_path_is_a_path_however_it_is_spelled(self, source: str):
        assert not _is_remote_url(source)

    def test_a_url_is_never_checked_as_a_local_directory(self):
        """No such directory exists — a local source would be refused for it."""
        env = _fetch_env("https://github.com/acme/widgets.git")
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_a_tree_name_that_is_not_an_oid_is_not_looked_up(self, tmp_path: Path):
        repo, base = _repo(tmp_path, {"a.txt": b"a\n"})
        mirror = _clean_clone(tmp_path, repo, base, name="mirror.git")
        canonical = provision(
            tmp_path / "canonical.git", source=str(mirror), base_sha=base
        )
        assert canonical.holds_tree("HEAD^{tree}") is False
