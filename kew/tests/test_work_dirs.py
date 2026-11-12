"""Where kew keeps its trees, and the keep rule that decides when they go."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew import work_dirs
from kew.config import load_config
from kew.ledger_identity import open_managed_store
from kew.run_store import RunRecord, SqliteRunStore

#: A pinned clock: the sweep reads "now" from its caller, never the wall.
_NOW = 2_000_000_000.0
_OLD = _NOW - work_dirs.RETENTION.total_seconds() - 3600
_FRESH = _NOW - 3600


def _age(*paths: Path, mtime: float = _OLD) -> None:
    for path in paths:
        os.utime(path, (mtime, mtime), follow_symlinks=False)


def _tree(root: Path, name: str, *, mtime: float = _OLD) -> Path:
    tree = root / name
    (tree / "checkout").mkdir(parents=True)
    (tree / "checkout" / "file.txt").write_text("work\n", encoding="utf-8")
    _age(tree / "checkout" / "file.txt", tree / "checkout", tree, mtime=mtime)
    return tree


def _run(
    store: SqliteRunStore, run_id: str, *, status: str, category: str = ""
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch=f"agent/issue-{run_id}",
            status=status,  # type: ignore[arg-type]
            exit_code=0,
            started_at=datetime.now(UTC),
            error_category=category,
        )
    )


def _bind(store: SqliteRunStore, run_id: str, checkout: Path) -> None:
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="dispatch_checkout_bound",
        run_id=run_id,
        item_id="42",
        payload={"checkout": str(checkout), "branch": f"agent/issue-{run_id}"},
    )


def _kew_root(path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A work root kew created itself, so it carries the marker."""
    monkeypatch.setenv(work_dirs.WORK_ROOT_ENV, str(path))
    return work_dirs.work_root()


def _ledger(project: Path) -> SqliteRunStore:
    """A project's own registered ledger, with its identity."""
    project.mkdir(parents=True, exist_ok=True)
    return open_managed_store(
        project_root=project,
        db_path=project / "dispatch.db",
        anchors_path=project / "audit-anchors.jsonl",
    )


def _scope(root: Path, store: SqliteRunStore) -> Path:
    ledger_id = store.read_ledger_id()
    assert ledger_id
    return root / ledger_id


def _sweep(store: SqliteRunStore | None, root: Path, project: Path) -> list[Path]:
    return work_dirs.sweep_work_root(store, project=project, now=_NOW, root=root)


@pytest.fixture
def unconfigured_root() -> Iterator[None]:
    """No ``[paths] work_root`` recorded; the hermetic root is put back after."""
    hermetic = os.environ[work_dirs.WORK_ROOT_ENV]
    work_dirs.configure(None)
    try:
        yield
    finally:
        work_dirs.configure(hermetic)


@pytest.mark.usefixtures("unconfigured_root")
class TestWorkRoot:
    def test_the_default_root_is_a_noindex_folder_under_local_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(work_dirs.WORK_ROOT_ENV, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        root = work_dirs.work_root()
        assert root == tmp_path / ".local/state/kew/tmp.noindex"
        assert root.is_dir()

    def test_paths_work_root_in_kew_toml_names_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(work_dirs.WORK_ROOT_ENV, raising=False)
        configured = tmp_path / "trees"
        toml = tmp_path / "kew.toml"
        toml.write_text(f'[paths]\nwork_root = "{configured}"\n', encoding="utf-8")
        config = load_config(toml)
        made = work_dirs.make_work_dir("kew-test-")
        assert config.paths.work_root == str(configured)
        assert made.parent == configured / work_dirs.UNSCOPED

    def test_the_environment_wins_over_kew_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = tmp_path / "kew.toml"
        toml.write_text(f'[paths]\nwork_root = "{tmp_path / "toml"}"\n')
        monkeypatch.setenv(work_dirs.WORK_ROOT_ENV, str(tmp_path / "env"))
        load_config(toml)
        made = work_dirs.make_work_dir("kew-test-")
        assert made.parent == tmp_path / "env" / work_dirs.UNSCOPED

    def test_only_a_root_kew_creates_carries_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made = _kew_root(tmp_path / "made", monkeypatch)
        theirs = tmp_path / "theirs"
        theirs.mkdir()
        _kew_root(theirs, monkeypatch)
        assert (made / work_dirs.ROOT_MARKER).is_file()
        assert not (theirs / work_dirs.ROOT_MARKER).exists()

    def test_trees_are_made_under_the_registered_ledgers_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        with _ledger(project) as store:
            ledger_id = store.read_ledger_id()
        (project / "kew.toml").write_text(
            f"[logging]\ndir = {str(project)!r}\n", encoding="utf-8"
        )
        monkeypatch.setenv("KEW_PROJECT", str(project))
        root = _kew_root(tmp_path / "root", monkeypatch)
        load_config(project / "kew.toml")
        made = work_dirs.make_work_dir("kew-sandbox-1-")
        assert made.parent == root / str(ledger_id)


class TestRetention:
    """The keep rule reads the ledger; a tree it cannot clear is kept."""

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            ("awaiting_publication", ""),
            ("failure", "publication_review_required"),
            ("failure", "held_publication"),
            ("failure", "awaiting_continuation"),
            ("pending", ""),
        ],
    )
    def test_a_tree_a_held_run_references_survives_past_retention(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        category: str,
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            held = _tree(_scope(root, store), "kew-sandbox-42-held")
            loose = _tree(_scope(root, store), "kew-sandbox-43-loose")
            _run(store, "held", status=status, category=category)
            _bind(store, "held", held)
            removed = _sweep(store, root, project)
        assert (held.is_dir(), loose.exists(), removed) == (True, False, [loose])

    def test_a_finished_runs_tree_is_removed_after_retention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            done = _tree(_scope(root, store), "kew-sandbox-42-done")
            _run(store, "done", status="success")
            _bind(store, "done", done)
            _sweep(store, root, project)
        assert not done.exists()

    def test_a_tree_younger_than_retention_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            fresh = _tree(_scope(root, store), "kew-review-fresh", mtime=_FRESH)
            _sweep(store, root, project)
        assert fresh.is_dir()

    def test_an_edit_two_levels_deep_keeps_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Unchanged for the retention" holds for the whole tree, not its top."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            tree = _tree(_scope(root, store), "kew-sandbox-48-deep")
            deep = tree / "checkout" / "src" / "edited.py"
            deep.parent.mkdir()
            deep.write_text("x = 1\n", encoding="utf-8")
            _age(deep.parent, tree / "checkout", tree)
            _age(deep, mtime=_FRESH)
            removed = _sweep(store, root, project)
        assert (removed, deep.is_file()) == ([], True)

    def test_the_age_walk_never_follows_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh directory a tree links to neither keeps it nor goes with it."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "fresh.txt").write_text("mine\n", encoding="utf-8")
        _age(outside / "fresh.txt", outside, mtime=_FRESH)
        with _ledger(project) as store:
            tree = _tree(_scope(root, store), "kew-sandbox-49-link")
            (tree / "checkout" / "link").symlink_to(outside, target_is_directory=True)
            _age(tree / "checkout" / "link", tree / "checkout", tree)
            removed = _sweep(store, root, project)
        assert (removed, (outside / "fresh.txt").is_file()) == ([tree], True)

    def test_a_checkout_inside_a_tree_keeps_the_whole_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            tree = _tree(_scope(root, store), "kew-host-local-1")
            _run(store, "held", status="awaiting_publication")
            _bind(store, "held", tree / "checkout")
            _sweep(store, root, project)
        assert (tree / "checkout" / "file.txt").is_file()

    def test_an_unabandoned_continuation_keeps_its_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            awaiting = _tree(_scope(root, store), "kew-sandbox-42-awaiting")
            loose = _tree(_scope(root, store), "kew-sandbox-44-loose")
            store.record_continuation(
                "epic:x",
                "42",
                "original",
                "agent/issue-42",
                awaiting,
                checkout_generation=fixture_checkout_generation(awaiting),
            )
            _sweep(store, root, project)
        assert (awaiting.is_dir(), loose.exists()) == (True, False)

    def test_an_abandoned_continuations_tree_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            abandoned = _tree(_scope(root, store), "kew-sandbox-42-abandoned")
            args = ("epic:x", "42", "original", "agent/issue-42", abandoned)
            store.record_continuation(
                *args, checkout_generation=fixture_checkout_generation(abandoned)
            )
            store.claim_continuation_under_cap(
                *args,
                "owner",
                0,
                claim_evidence=fixture_claim_evidence(store, *args, "owner"),
            )
            _run(store, "owner", status="failure")
            store.finalize_continuation(*args, 1, "owner")
            store.finalize_continuation(
                *args, 1, "owner", abandon_reason="stop", actor_id="operator"
            )
            (record,) = store.list_all_continuations()
            assert store.continuation_was_abandoned(record)
            _sweep(store, root, project)
        assert not abandoned.exists()

    def test_an_unreadable_ledger_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        store = _ledger(project)
        loose = _tree(_scope(root, store), "kew-sandbox-45-loose")
        store.close()
        removed = _sweep(store, root, project)
        assert (removed, loose.is_dir()) == ([], True)

    def test_no_ledger_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        loose = _tree(root / work_dirs.UNSCOPED, "kew-sandbox-46-loose")
        assert _sweep(None, root, tmp_path / "project") == []
        assert loose.is_dir()

    def test_a_ledger_without_an_identity_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        loose = _tree(root / work_dirs.UNSCOPED, "kew-sandbox-46-loose")
        with SqliteRunStore(tmp_path / "legacy.db") as legacy:
            assert legacy.read_ledger_id() is None
            removed = _sweep(legacy, root, tmp_path / "project")
        assert (removed, loose.is_dir()) == ([], True)

    def test_only_entries_kew_names_are_ever_swept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anything else an operator keeps in the scope, or beside it, stays."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        locks = _tree(root, work_dirs.LOCKS_DIR)
        with _ledger(project) as store:
            theirs = _tree(_scope(root, store), "operator-notes")
            _sweep(store, root, project)
        assert (locks.is_dir(), theirs.is_dir()) == (True, True)

    def test_an_expired_stray_file_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            stray = _scope(root, store) / "kew-run-1-abc.cid"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_text("deadbeef")
            _age(stray)
            _sweep(store, root, project)
        assert not stray.exists()


class TestScopes:
    """Every project's trees sit under their own ledger's id (#3183 H2)."""

    def test_another_projects_sweep_and_a_fresh_ledger_leave_a_held_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer's case: one shared root, project A holds a tree."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project_a, project_b = tmp_path / "a", tmp_path / "b"
        with _ledger(project_a) as a:
            work_dirs.configure(None, ledger=(project_a, project_a / "dispatch.db"))
            held = work_dirs.make_work_dir("kew-sandbox-42-")
            (held / "unpublished.txt").write_text("agent work\n", encoding="utf-8")
            loose = work_dirs.make_work_dir("kew-sandbox-43-")
            _age(held / "unpublished.txt", held, loose)
            _run(a, "r1", status="awaiting_publication")
            _bind(a, "r1", held)
        with _ledger(project_b) as b:
            from_b = _sweep(b, root, project_b)
        # Project A's ledger is replaced: a new ledger gets a new id.
        for ledger_file in project_a.glob("dispatch.db*"):
            ledger_file.unlink()
        (project_a / ".kew" / "ledger.json").unlink()
        with _ledger(project_a) as fresh:
            from_fresh = _sweep(fresh, root, project_a)
        assert (from_b, from_fresh, held.is_dir(), loose.is_dir()) == (
            [],
            [],
            True,
            True,
        )

    def test_a_projects_own_sweep_removes_its_expired_unreferenced_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "a"
        with _ledger(project) as a:
            work_dirs.configure(None, ledger=(project, project / "dispatch.db"))
            held = work_dirs.make_work_dir("kew-sandbox-42-")
            loose = work_dirs.make_work_dir("kew-sandbox-43-")
            _age(held, loose)
            _run(a, "r1", status="awaiting_publication")
            _bind(a, "r1", held)
            removed = _sweep(a, root, project)
        assert (removed, held.is_dir()) == ([loose], True)


class TestRootRefusal:
    """A sweep removes nothing from a root kew cannot prove is its own."""

    def test_a_root_holding_the_operators_own_repository_is_never_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer's case: ``KEW_WORK_ROOT`` pointed at ``~/Projects``."""
        projects = tmp_path / "Projects"
        repo = projects / "sibling-repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        (repo / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
        _age(repo / "src" / "module.py", repo / ".git", repo / "src", repo)
        monkeypatch.setenv(work_dirs.WORK_ROOT_ENV, str(projects))
        project = tmp_path / "project"
        with _ledger(project) as store:
            stray = _tree(_scope(projects, store), "kew-sandbox-1-old")
            removed = work_dirs.sweep_work_root(store, project=project, now=_NOW)
        assert (removed, repo.is_dir(), stray.is_dir()) == ([], True, True)
        assert work_dirs.root_refusal(projects, project) == (
            f"{projects} has no {work_dirs.ROOT_MARKER}: kew did not create it"
        )

    def test_a_marked_root_that_is_the_project_or_its_ancestor_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = root / "checkout"
        with _ledger(project) as store:
            loose = _tree(_scope(root, store), "kew-sandbox-2-old")
            removed = _sweep(store, root, project)
            removed_at_project = _sweep(store, project, project)
        assert (removed, removed_at_project, loose.is_dir()) == ([], [], True)

    def test_a_marked_root_that_is_home_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _kew_root(tmp_path / "home", monkeypatch)
        monkeypatch.setenv("HOME", str(home))
        project = tmp_path / "project"
        with _ledger(project) as store:
            loose = _tree(_scope(home, store), "kew-sandbox-3-old")
            removed = _sweep(store, home, project)
        assert (removed, loose.is_dir()) == ([], True)

    @pytest.mark.parametrize(
        ("root", "reason"),
        [
            (Path("relative/root"), "is a relative path"),
            (Path("/"), "is the filesystem root"),
            (Path.home(), "is the home directory or one of its ancestors"),
            (Path.home().parent, "is the home directory or one of its ancestors"),
        ],
    )
    def test_roots_no_marker_can_make_sweepable(
        self, tmp_path: Path, root: Path, reason: str
    ) -> None:
        assert work_dirs.root_refusal(root, tmp_path) == f"{root} {reason}"


class TestClaimLocks:
    """A held tree's claim lock, and any lock another process holds, stay."""

    def test_a_held_trees_claim_lock_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            held = _tree(_scope(root, store), "kew-sandbox-42-held")
            lock = held.with_name(f"{held.name}.lock")
            lock.write_text("123\n", encoding="utf-8")
            _age(lock)
            _run(store, "held", status="awaiting_publication")
            _bind(store, "held", held)
            removed = _sweep(store, root, project)
        assert (removed, lock.is_file()) == ([], True)

    def test_a_lock_another_holder_has_is_kept_and_a_free_one_goes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            scope = _scope(root, store)
            scope.mkdir(parents=True, exist_ok=True)
            held, free = scope / "kew-sandbox-7-x.lock", scope / "kew-sandbox-8-y.lock"
            for lock in (held, free):
                lock.write_text("123\n", encoding="utf-8")
                _age(lock)
            with held.open("a+") as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                removed = _sweep(store, root, project)
        assert (removed, held.is_file(), free.exists()) == ([free], True, False)

    def test_an_old_tree_whose_claim_a_live_process_holds_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-review's walk attack, case 4: old, unreferenced, but claimed."""
        from kew.worktree import claim_checkout, release_checkout_claim

        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            work_dirs.configure(None, ledger=(project, project / "dispatch.db"))
            claimed = work_dirs.make_work_dir("kew-sandbox-held-")
            claim_checkout(claimed)
            try:
                lock = claimed.with_name(f"{claimed.name}.lock")
                _age(claimed, lock)
                removed = _sweep(store, root, project)
            finally:
                release_checkout_claim(claimed)
        assert (removed, claimed.is_dir(), lock.is_file()) == ([], True, True)

    def test_an_old_tree_with_a_free_claim_lock_goes_with_its_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            tree = _tree(_scope(root, store), "kew-sandbox-9-free")
            lock = tree.with_name(f"{tree.name}.lock")
            lock.write_text("123\n", encoding="utf-8")
            _age(lock)
            removed = _sweep(store, root, project)
        assert (removed, tree.exists(), lock.exists()) == ([tree, lock], False, False)


class TestScopeSafety:
    """A scope entry that is a symlink is never followed (#3183 round 4)."""

    def _operator_repo(self, base: Path) -> tuple[Path, Path]:
        operator_dir = base / "Projects"
        repo = operator_dir / "sibling-repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "x.py").write_text("x\n", encoding="utf-8")
        _age(repo / "src" / "x.py", repo / "src", repo)
        return operator_dir, repo

    def test_the_sweep_refuses_a_scope_that_is_a_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-review's attack: ``<root>/<ledger id>`` links to ``~/Projects``."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        operator_dir, repo = self._operator_repo(tmp_path)
        with _ledger(project) as store:
            _scope(root, store).symlink_to(operator_dir, target_is_directory=True)
            removed = _sweep(store, root, project)
        assert (removed, repo.is_dir()) == ([], True)

    def test_no_tree_is_made_through_a_symlinked_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused, not replaced: the link and where it points stay as they are."""
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        with _ledger(project) as store:
            scope = _scope(root, store)
        scope.symlink_to(elsewhere, target_is_directory=True)
        work_dirs.configure(None, ledger=(project, project / "dispatch.db"))
        with pytest.raises(work_dirs.UnsafeWorkScope, match="is a symlink"):
            work_dirs.make_work_dir("kew-sandbox-1-")
        with (
            pytest.raises(work_dirs.UnsafeWorkScope, match="is a symlink"),
            work_dirs.work_tempdir("kew-verify-"),
        ):
            pass
        assert (scope.is_symlink(), list(elsewhere.iterdir())) == (True, [])

    def test_a_scope_that_is_not_a_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _kew_root(tmp_path / "root", monkeypatch)
        project = tmp_path / "project"
        with _ledger(project) as store:
            scope = _scope(root, store)
            scope.write_text("not a scope\n", encoding="utf-8")
            removed = _sweep(store, root, project)
        assert (removed, scope.is_file()) == ([], True)
        assert work_dirs.scope_problem(root, scope) == f"{scope} is not a directory"


def test_a_held_tree_is_never_walked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reference check comes first: a held tree's age is never read."""
    root = _kew_root(tmp_path / "root", monkeypatch)
    project = tmp_path / "project"
    with _ledger(project) as store:
        held = _tree(_scope(root, store), "kew-sandbox-42-held")
        unreadable = held / "checkout" / "private"
        unreadable.mkdir()
        _age(unreadable, held / "checkout", held)
        _run(store, "held", status="awaiting_publication")
        _bind(store, "held", held)
        unreadable.chmod(0)
        try:
            with caplog.at_level("WARNING", logger="kew.work_dirs"):
                removed = _sweep(store, root, project)
        finally:
            unreadable.chmod(0o700)
    assert (removed, held.is_dir()) == ([], True)
    assert "cannot read its age" not in caplog.text


def test_kew_cleanup_removes_an_expired_unreferenced_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's sweep: ``kew cleanup`` lets go of what retention allows.

    Only the process and forge boundaries are faked: the tool probe, ``gh``
    and the GitHub issue query. The first run creates the project's ledger;
    the trees are then made the way a dispatch makes them, in that ledger's
    scope of a root kew created.
    """
    from unittest.mock import patch

    from typer.testing import CliRunner

    from kew.cli import app

    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "owner/repo"\n'
        f"[logging]\ndir = {str(tmp_path / 'logs')!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.setenv(work_dirs.WORK_ROOT_ENV, str(tmp_path / "work-root"))
    monkeypatch.chdir(tmp_path)
    gh_authenticated = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.run", return_value=gh_authenticated),
        patch("kew.labels.fetch_agent_working_issues", return_value=[]),
    ):
        first = CliRunner().invoke(app, ["cleanup"])
        assert first.exit_code == 0, first.output
        expired = work_dirs.make_work_dir("kew-sandbox-47-")
        fresh = work_dirs.make_work_dir("kew-review-")
        _age(expired, mtime=1_000_000_000.0)
        _age(fresh, mtime=time.time())
        result = CliRunner().invoke(app, ["cleanup"])
    assert result.exit_code == 0, result.output
    assert expired.parent.name == work_dirs.ledger_scope() != work_dirs.UNSCOPED
    assert (expired.exists(), fresh.is_dir()) == (False, True), result.output
    assert f"Removed expired work tree {expired}" in result.output
