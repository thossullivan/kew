"""A relative ``logging.dir`` names one ledger per project, from any worktree.

The ledger registry lives in the git common directory, so the main checkout
and every linked worktree share it. A relative ``logging.dir`` resolved against
the worktree registered a database inside that worktree, and removing the
worktree then locked the main checkout out with WRONG-LEDGER (#3576).
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - the tests build real git repositories
from collections.abc import Iterator
from pathlib import Path

import pytest

import kew.config as kew_config
from kew import subprocess_utils
from kew.audit import DEFAULT_ANCHORS_PATH
from kew.cli import _managed_store
from kew.config import load_config
from kew.ledger_identity import (
    LedgerIdentityError,
    ledger_rebind,
    open_managed_store,
)
from kew.worktree import clone_local_checkout


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # nosec B603 B607 - fixed git argv in a test
        ["git", *args], cwd=cwd, check=True, capture_output=True
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "test")
    (path / "README.md").write_text("project\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path.resolve()


@pytest.fixture
def default_log_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: a relative ``./logs/dispatch``, no env override."""
    monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", None)
    for name in ("KEW_LOG_DIR", "DISPATCH_LOG_DIR", "KEW_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def _open_from(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(cwd)
    db_path = Path(load_config().logging.dir) / "dispatch.db"
    with _managed_store(db_path):
        pass
    return db_path


@pytest.mark.usefixtures("default_log_dir")
def test_a_removed_worktree_leaves_the_main_checkout_its_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _repo(tmp_path / "main")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "side", str(linked))

    from_worktree = _open_from(linked, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _git(main, "worktree", "remove", "--force", str(linked))
    from_main = _open_from(main, monkeypatch)

    assert from_worktree == from_main == main / "logs" / "dispatch" / "dispatch.db"
    assert from_main.is_file()


@pytest.mark.usefixtures("default_log_dir")
def test_a_project_in_a_subdirectory_keeps_it_from_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _repo(tmp_path / "main")
    (main / "kew").mkdir()
    (main / "kew" / "kew.toml").write_text("")
    _git(main, "add", "kew/kew.toml")
    _git(main, "commit", "-q", "-m", "kew")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "side", str(linked))

    from_main = _open_from(main / "kew", monkeypatch)
    from_worktree = _open_from(linked / "kew", monkeypatch)

    assert (
        from_worktree == from_main == main / "kew" / "logs" / "dispatch" / "dispatch.db"
    )


@pytest.mark.usefixtures("default_log_dir")
def test_an_absolute_log_dir_in_a_worktree_is_left_where_it_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _repo(tmp_path / "main")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "side", str(linked))
    absolute = tmp_path / "operator-logs"
    (linked / "kew.toml").write_text(f'[logging]\ndir = "{absolute}"\n')
    monkeypatch.chdir(linked)

    assert Path(load_config().logging.dir) == absolute
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path / "env-logs"))
    assert Path(load_config().logging.dir) == tmp_path / "env-logs"


@pytest.fixture
def git_allowed() -> Iterator[None]:
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


@pytest.mark.usefixtures("default_log_dir", "git_allowed")
def test_a_sandbox_clone_never_resolves_to_the_host_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _repo(tmp_path / "host")
    checkout = tmp_path / "sandbox"
    clone_local_checkout(host, checkout, "main")

    db_path = _open_from(checkout, monkeypatch)

    assert db_path == checkout.resolve() / "logs" / "dispatch" / "dispatch.db"
    assert not (host / "logs").exists()


def test_a_registry_naming_a_missing_database_names_the_recovery(
    tmp_path: Path,
) -> None:
    registered = tmp_path / "gone" / "dispatch.db"
    anchors = tmp_path / "anchors.jsonl"
    with open_managed_store(
        project_root=tmp_path, db_path=registered, anchors_path=anchors
    ) as store:
        ledger_id = store.read_ledger_id()
    for suffix in ("", "-wal", "-shm"):
        registered.with_name(registered.name + suffix).unlink(missing_ok=True)
    configured = tmp_path / "logs" / "dispatch.db"

    with pytest.raises(LedgerIdentityError) as refused:
        open_managed_store(
            project_root=tmp_path, db_path=configured, anchors_path=anchors
        )

    message = str(refused.value)
    assert message.startswith("WRONG-LEDGER: configured path mismatch")
    assert f"registered database {registered} is missing" in message
    assert (
        f"kew audit ledger-rebind --target {configured}"
        f" --expected-ledger-id {ledger_id}"
    ) in message


def test_the_named_recovery_reopens_a_restored_ledger(tmp_path: Path) -> None:
    registered = tmp_path / "gone" / "dispatch.db"
    anchors = tmp_path / "anchors.jsonl"
    with open_managed_store(
        project_root=tmp_path, db_path=registered, anchors_path=anchors
    ) as store:
        ledger_id = store.read_ledger_id()
    configured = tmp_path / "logs" / "dispatch.db"
    configured.parent.mkdir()
    shutil.move(registered, configured)
    assert ledger_id is not None

    ledger_rebind(
        project_root=tmp_path,
        target=configured,
        anchors_path=anchors,
        expected_ledger_id=ledger_id,
        reason="recover ledger lost with a removed worktree",
    )

    with open_managed_store(
        project_root=tmp_path, db_path=configured, anchors_path=anchors
    ) as store:
        assert store.read_ledger_id() == ledger_id


@pytest.mark.usefixtures("default_log_dir")
def test_a_ledger_registered_inside_a_live_worktree_names_its_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real project root, so the registry is the one in the git common dir.
    monkeypatch.setattr("kew.cli.project_root", kew_config.project_root)
    main = _repo(tmp_path / "main")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "side", str(linked))
    # Where an earlier release put the ledger: inside the linked worktree.
    registered = linked.resolve() / "logs" / "dispatch" / "dispatch.db"
    anchors = main / DEFAULT_ANCHORS_PATH
    with open_managed_store(
        project_root=main, db_path=registered, anchors_path=anchors
    ) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None
    configured = main / "logs" / "dispatch" / "dispatch.db"

    for cwd in (linked, main):
        with pytest.raises(LedgerIdentityError) as refused:
            _open_from(cwd, monkeypatch)
        message = str(refused.value)
        assert f"registered path exists at {registered}" in message
        assert (
            f"kew audit ledger-rebind --target {configured}"
            f" --expected-ledger-id {ledger_id}"
        ) in message

    configured.parent.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        moved = registered.with_name(registered.name + suffix)
        if moved.exists():
            shutil.move(moved, configured.with_name(configured.name + suffix))
    ledger_rebind(
        project_root=main,
        target=configured,
        anchors_path=anchors,
        expected_ledger_id=ledger_id,
        reason="move ledger out of a linked worktree",
    )

    assert _open_from(linked, monkeypatch) == configured
    assert _open_from(main, monkeypatch) == configured
