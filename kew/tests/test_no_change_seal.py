"""The empty work package a no-change candidate is attested from (#1610).

Attestation reads two things off a *sealed receipt*: the trusted base the run
worked from, and the work package that attempt produced. The contained path
publishes through the sandbox, so nothing on it ever sealed either — the
candidate landed with `evidence_json` NULL and could never be confirmed.

This module seals what the host itself can prove: an EMPTY, workspace-scoped
package at the base, and only when the host has re-read the workspace and found
it identical to that base. A workspace that still carries work refuses the
seal, because an empty package would then be a false statement about it.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew.artifact_store import PackageStore
from kew.no_change_seal import (
    VERIFICATION_EMPTY_WORKSPACE,
    NoChangeSealError,
    seal_no_change_package,
)
from kew.publication_artifact import WORKSPACE_SCOPE, Artifact
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _git_allowlisted(monkeypatch: Any) -> None:
    """The dispatch allowlist as a real run has it (``preflight_tools``)."""
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [TOOL_PATHS["git"], *args], cwd=repo, capture_output=True, check=True
    )


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A dispatch workspace whose ``origin/main`` resolves, like a worktree."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


@pytest.fixture()
def packages(tmp_path: Path) -> Path:
    return tmp_path / "packages"


def _base_sha(checkout: Path) -> str:
    result = subprocess.run(
        [TOOL_PATHS["git"], "rev-parse", "origin/main"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _seal(store: Any, checkout: Path, packages: Path, **kwargs: Any) -> Any:
    return seal_no_change_package(
        store,
        worktree_dir=checkout,
        packages_root=packages,
        base_sha=kwargs.pop("base_sha", None) or _base_sha(checkout),
        source_kind="github",
        source_id="42",
        run_id=kwargs.pop("run_id", "run-1"),
        attempt_number=kwargs.pop("attempt_number", 1),
        now=NOW,
    )


class TestSealingACleanWorkspace:
    def test_the_receipt_names_the_base_and_an_empty_workspace_package(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        base = _base_sha(checkout)

        receipt = _seal(store, checkout, packages)

        assert receipt.base_sha == base
        assert receipt.verification == VERIFICATION_EMPTY_WORKSPACE
        artifact = PackageStore(packages).load(receipt.package_digest)
        assert artifact.scope == WORKSPACE_SCOPE
        assert artifact.entries == ()
        assert artifact.deletions == ()
        assert artifact.base_sha == base

    def test_the_sealed_tree_is_the_base_tree_the_host_read_back(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """The tree kew would publish IS the base's — that is the no-change."""
        expected = subprocess.run(
            [TOOL_PATHS["git"], "rev-parse", "HEAD^{tree}"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        receipt = _seal(store, checkout, packages)

        assert receipt.tree_sha == expected

    def test_the_seal_keeps_the_audit_chain_verifiable(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """Sealing appends a RECEIPT_SEALED event to the hash chain; the chain
        must still verify afterward. Ledger integrity for the no-change
        attestation path specifically, not only transitively via the receipt
        store's own tests -- this is attested evidence a person will confirm."""
        from kew.audit_chain import verify_chain

        rows_before = store.read_chain_rows()
        assert verify_chain(rows_before).ok

        _seal(store, checkout, packages)

        rows_after = store.read_chain_rows()
        assert len(rows_after) > len(rows_before)
        assert verify_chain(rows_after).ok

    def test_sealing_twice_converges_on_one_receipt(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """A retry after a crash resumes the seal instead of forking it."""
        first = _seal(store, checkout, packages)
        second = _seal(store, checkout, packages)

        assert first.receipt_digest == second.receipt_digest
        assert len(store.list_artifact_receipts(run_id="run-1")) == 1

    def test_an_already_sealed_attempt_is_returned_untouched(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """Real work sealed by the publication path is never overwritten by an
        empty package — the receipt that exists is the one attestation reads."""
        first = _seal(store, checkout, packages)

        second = seal_no_change_package(
            store,
            worktree_dir=checkout,
            packages_root=packages,
            base_sha="0" * 40,
            source_kind="github",
            source_id="42",
            run_id="run-1",
            attempt_number=1,
            now=NOW,
        )

        assert second == first


class TestWorkspaceThatStillCarriesWork:
    def test_an_untracked_file_refuses_the_seal(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        (checkout / "left-behind.py").write_text("print('work')\n")

        with pytest.raises(NoChangeSealError):
            _seal(store, checkout, packages)

        assert store.list_artifact_receipts(run_id="run-1") == []

    def test_an_uncommitted_edit_refuses_the_seal(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        (checkout / "README.md").write_text("edited\n")

        with pytest.raises(NoChangeSealError):
            _seal(store, checkout, packages)

    def test_a_commit_the_base_does_not_carry_refuses_the_seal(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """Committed work is still work: HEAD's tree is not the base's."""
        (checkout / "shipped.py").write_text("print('shipped')\n")
        _git(checkout, "add", "shipped.py")
        _git(checkout, "commit", "-m", "work")

        with pytest.raises(NoChangeSealError):
            _seal(store, checkout, packages)

    def test_an_unreadable_base_refuses_the_seal(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        with pytest.raises(NoChangeSealError):
            _seal(store, checkout, packages, base_sha="0" * 40)

    def test_a_workspace_that_is_not_a_repository_refuses_the_seal(
        self, store: Any, tmp_path: Path, packages: Path
    ) -> None:
        empty = tmp_path / "gone"
        empty.mkdir()

        with pytest.raises(NoChangeSealError):
            seal_no_change_package(
                store,
                worktree_dir=empty,
                packages_root=packages,
                base_sha="0" * 40,
                source_kind="github",
                source_id="42",
                run_id="run-1",
                attempt_number=1,
                now=NOW,
            )


class TestAVerifiedArtifactThatLeftADirectoryUnread:
    """#2201: canonical extraction walks PAST an untracked nested worktree or
    clone, so a workspace whose only work sits inside one produces an EMPTY
    artifact. Sealing that would attest "nothing was done" about a directory
    nothing ever read -- and the sibling, checkout-reading path refuses the
    very same workspace, because `git status --untracked-files=all` reports
    the nested directory as untracked. Which producer ran must not decide
    whether a run is attested.
    """

    def _artifact(
        self, base: str, tmp_path: Path, *, nested: tuple[str, ...]
    ) -> Artifact:
        return Artifact(
            base_sha=base,
            entries=(),
            blobs_dir=tmp_path,
            deletions=(),
            scope=WORKSPACE_SCOPE,
            omitted_nested_repositories=nested,
        )

    def _seal_verified(
        self, store: Any, checkout: Path, packages: Path, artifact: Artifact
    ) -> Any:
        return seal_no_change_package(
            store,
            worktree_dir=checkout,
            packages_root=packages,
            base_sha=artifact.base_sha,
            source_kind="github",
            source_id="42",
            run_id="run-1",
            attempt_number=1,
            now=NOW,
            verified_artifact=artifact,
            verified_tree_sha="a" * 40,
        )

    def test_an_omitted_nested_repository_refuses_the_seal(
        self, store: Any, checkout: Path, packages: Path, tmp_path: Path
    ) -> None:
        artifact = self._artifact(_base_sha(checkout), tmp_path, nested=(".wt-main",))

        with pytest.raises(NoChangeSealError) as refused:
            self._seal_verified(store, checkout, packages, artifact)

        assert "nested git worktree or repository" in str(refused.value)
        assert store.list_artifact_receipts(run_id="run-1") == []

    def test_the_refusal_names_a_count_and_never_the_agent_chosen_path(
        self, store: Any, checkout: Path, packages: Path, tmp_path: Path
    ) -> None:
        """The directory name is agent-chosen and this string reaches an
        operator's log, exactly as `_refuse_dirty_workspace` already has it."""
        artifact = self._artifact(
            _base_sha(checkout), tmp_path, nested=(".wt-main", "vendor/other")
        )

        with pytest.raises(NoChangeSealError) as refused:
            self._seal_verified(store, checkout, packages, artifact)

        message = str(refused.value)
        assert "2" in message
        assert ".wt-main" not in message
        assert "vendor/other" not in message

    def test_a_verified_artifact_that_read_everything_still_seals(
        self, store: Any, checkout: Path, packages: Path, tmp_path: Path
    ) -> None:
        """The control: the refusal is about the unread directory, not about
        the verified path as a whole."""
        artifact = self._artifact(_base_sha(checkout), tmp_path, nested=())

        receipt = self._seal_verified(store, checkout, packages, artifact)

        assert receipt.verification == VERIFICATION_EMPTY_WORKSPACE
        assert receipt.tree_sha == "a" * 40

    def test_the_checkout_reading_path_refuses_the_same_workspace(
        self, store: Any, checkout: Path, packages: Path
    ) -> None:
        """Parity with the sibling seal path, which is what makes the refusal
        above the CONSISTENT answer rather than a second opinion."""
        nested = checkout / ".wt-main"
        nested.mkdir()
        (nested / ".git").write_text("gitdir: /elsewhere/.git/worktrees/main\n")
        (nested / "work.py").write_text("print('the only work there is')\n")

        with pytest.raises(NoChangeSealError):
            _seal(store, checkout, packages)

        assert store.list_artifact_receipts(run_id="run-1") == []
