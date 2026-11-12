"""`kew gc` lists what closed work left on disk and, with ``--apply``, removes
exactly that (#2417).

Each test builds a worktree base and a work root holding checkouts of one
closed item (issue #7, its merged PR #70) and one open item (issue #8, its open
PR #71). ``gh`` is answered by a :class:`FakeForge` and the container runtime
by the ``docker`` stub, both at the process boundary; the ledger is a real
SQLite store.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.fake_docker import (
    add_image,
    add_image_container,
    image_references,
    install_fake_docker,
)
from support.gh_fakes import FakeForge, GhProcessBoundary
from typer.testing import CliRunner

from kew import gc, subprocess_utils, work_dirs
from kew.cli import app
from kew.config import load_config
from kew.ledger_identity import open_managed_store
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()

_STAMP = "20261001T000000000000Z"
_IMAGE_OLD = f"sha256:{'a' * 64}"
_IMAGE_PINNED = f"sha256:{'b' * 64}"
_IMAGE_USED = f"sha256:{'c' * 64}"


class Site:
    """The temp project, its checkouts, and the fakes answering for it."""

    def __init__(
        self, root: Path, forge: FakeForge, docker: Path, ledger_id: str
    ) -> None:
        self.root = root
        self.forge = forge
        self.docker = docker
        self.db = root / "project" / "logs" / "dispatch.db"
        self.agent = root / "worktrees" / "agent"
        self.review = root / "worktrees" / "review"
        self.sandbox = work_dirs.configured_root() / ledger_id
        self.unscoped = work_dirs.configured_root() / work_dirs.UNSCOPED
        self.bytes: dict[Path, int] = {}

    def checkout(self, path: Path, size: int, *, sidecars: bool = True) -> None:
        """A checkout directory holding *size* bytes, with its lock and
        generation files beside it."""
        (path / "src").mkdir(parents=True)
        (path / "src" / "work.txt").write_bytes(b"x" * size)
        self.bytes[path] = size
        if sidecars:
            Path(f"{path}.lock").write_text("1\n")
            Path(f"{path}.generation").write_text("0" * 32)
            self.bytes[Path(f"{path}.lock")] = 2
            self.bytes[Path(f"{path}.generation")] = 32

    def item_paths(self, issue: int, pr: int) -> list[Path]:
        """Every path this site made for one item, sidecars included."""
        names = (f"issue-{issue}", f"kew-sandbox-{issue}-", f"pr-{pr}")
        return sorted(path for path in self.bytes if path.name.startswith(names))

    def tree(self) -> dict[str, bytes]:
        """Every file and directory under the checkout roots, with contents."""
        found: dict[str, bytes] = {}
        paths = [*(self.root / "worktrees").rglob("*"), *self.sandbox.rglob("*")]
        for path in sorted(paths):
            relative = path.relative_to(self.root).as_posix()
            found[relative] = path.read_bytes() if path.is_file() else b"<dir>"
        return found


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_config(project: Path, root: Path, *, pinned: str = "") -> None:
    images = f'[sandbox.images]\nclaude-code = "{pinned}"\n' if pinned else ""
    (project / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        f'[logging]\ndir = "{project / "logs"}"\n'
        f'[branches]\nworktree_base = "{root / "worktrees"}"\n'
        f'[paths]\nwork_root = "{root / "work"}"\n'
        '[sandbox]\nruntime = "docker"\n' + images
    )


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    project = tmp_path / "project"
    (project / "logs").mkdir(parents=True)
    _write_config(project, tmp_path, pinned="kew-agent:pinned")
    # The project's git checkout, which --apply unregisters worktrees from.
    _git(project, "init", "-q", "-b", "main")
    _git(project, "commit", "-q", "--allow-empty", "-m", "base")
    monkeypatch.chdir(project)
    monkeypatch.setenv(work_dirs.WORK_ROOT_ENV, str(tmp_path / "work"))
    # The work root as kew makes it: private, carrying its marker.
    work_dirs.work_root()
    # The CLI's project root is tmp_path here (conftest); register it there.
    with open_managed_store(
        project_root=tmp_path,
        db_path=project / "logs" / "dispatch.db",
        anchors_path=tmp_path / "audit-anchors.jsonl",
    ) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id
    load_config()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = install_fake_docker(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # The forge answers every `gh` argv; this stub only lets preflight find one.
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "gh").chmod(0o755)

    forge = FakeForge(origin=tmp_path)
    forge.open_issue(7)
    forge.open_issue(8)
    forge.issues[7]["state"] = "CLOSED"
    forge.open_pr(issue=7, branch="agent/issue-7", head="a" * 40)["state"] = "MERGED"
    forge.open_pr(issue=8, branch="agent/issue-8", head="b" * 40)
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )

    built = Site(tmp_path, forge, docker, ledger_id)
    for issue, pr, size in ((7, 70, 100), (8, 71, 1000)):
        built.checkout(built.agent / f"issue-{issue}", size)
        built.checkout(built.agent / f"issue-{issue}.preserved-{_STAMP}", size + 1)
        built.checkout(
            built.agent / f"issue-{issue}.replaced-{_STAMP}-abcdefabcdef", size + 2
        )
        built.checkout(built.sandbox / f"kew-sandbox-{issue}-ab_cd123", size + 3)
        built.checkout(
            built.review / f"pr-{pr}.preserved-{_STAMP}", size + 4, sidecars=False
        )
    return built


def _gc(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["gc", *args])
    return result.exit_code, result.output


def test_dry_run_lists_closed_items_checkouts_and_removes_nothing(site: Site) -> None:
    before = site.tree()

    code, output = _gc()

    assert code == 0, output
    for path in site.item_paths(7, 70):
        assert str(path) in output
    for path in site.item_paths(8, 71):
        assert str(path) not in output
    assert "Dry run: nothing was removed" in output
    assert site.tree() == before


def test_unscoped_work_dir_is_never_listed_or_removed(site: Site) -> None:
    """Every unregistered project shares ``unscoped``; this ledger cannot say
    whether a clone there is another project's live work."""
    other = site.unscoped / "kew-sandbox-7-zz_yy987"
    site.checkout(other, 50)
    before = sorted(p.relative_to(site.unscoped) for p in site.unscoped.rglob("*"))

    code, output = _gc("--apply")

    assert code == 0, output
    assert str(other) not in output
    assert sorted(p.relative_to(site.unscoped) for p in site.unscoped.rglob("*")) == (
        before
    )
    assert not os.path.lexists(site.sandbox / "kew-sandbox-7-ab_cd123")


def test_dry_run_reports_the_bytes_it_would_free(site: Site) -> None:
    expected = sum(site.bytes[path] for path in site.item_paths(7, 70))

    code, output = _gc()

    assert code == 0, output
    assert f"Total: {expected} bytes" in output
    assert f"in {len(site.item_paths(7, 70))} paths" in output


def test_apply_removes_closed_items_checkouts_and_keeps_open_items(
    site: Site,
) -> None:
    open_before = {
        path: sorted(p.name for p in path.rglob("*")) if path.is_dir() else []
        for path in site.item_paths(8, 71)
    }

    code, output = _gc("--apply")

    assert code == 0, output
    assert [p for p in site.item_paths(7, 70) if os.path.lexists(p)] == []
    for path, contents in open_before.items():
        assert path.exists(), path
        if path.is_dir():
            assert sorted(p.name for p in path.rglob("*")) == contents
    assert not Path(f"{site.review / 'pr-70'}.lock").exists()


def test_replaced_generations_go_with_their_lock_and_generation_files(
    site: Site,
) -> None:
    replaced = site.agent / f"issue-7.replaced-{_STAMP}-abcdefabcdef"
    preserved = site.agent / f"issue-7.preserved-{_STAMP}"

    code, output = _gc("--apply")

    assert code == 0, output
    for checkout in (replaced, preserved, site.agent / "issue-7"):
        for path in (
            checkout,
            Path(f"{checkout}.lock"),
            Path(f"{checkout}.generation"),
        ):
            assert not os.path.lexists(path), path
    open_replaced = site.agent / f"issue-8.replaced-{_STAMP}-abcdefabcdef"
    assert Path(f"{open_replaced}.lock").exists()
    assert Path(f"{open_replaced}.generation").exists()


def test_unpinned_agent_tags_are_listed_and_pinned_or_used_tags_are_kept(
    site: Site,
) -> None:
    add_image(site.docker, "kew-agent:old", _IMAGE_OLD, "1.2GB")
    add_image(site.docker, "kew-agent:pinned", _IMAGE_PINNED)
    add_image(site.docker, "kew-agent:used", _IMAGE_USED)
    add_image_container(site.docker, "kew-agent:used")

    code, output = _gc("--images")

    assert code == 0, output
    assert "image kew-agent:old" in output
    assert "1.2GB" in output
    assert "kew-agent:pinned" not in output
    assert "kew-agent:used" not in output
    assert image_references(site.docker) == {
        "kew-agent:old",
        "kew-agent:pinned",
        "kew-agent:used",
    }

    code, output = _gc("--apply", "--images")

    assert code == 0, output
    assert image_references(site.docker) == {"kew-agent:pinned", "kew-agent:used"}


def test_unreadable_tracker_state_removes_nothing(site: Site) -> None:
    del site.forge.issues[7]
    del site.forge.prs[70]
    before = site.tree()

    code, output = _gc("--apply")

    assert code == 2, output
    assert "issue #7: tracker state could not be read" in output
    assert "PR #70: tracker state could not be read" in output
    assert site.tree() == before


def test_unreadable_tracker_state_names_why_it_could_not_be_read(
    site: Site,
) -> None:
    del site.forge.issues[7]

    code, output = _gc()

    assert code == 2, output
    assert "issue #7: tracker state could not be read (gh exited 1:" in output


class _IdentityUnreadable:
    """A real store whose identity read fails at the database."""

    def __init__(self, store: SqliteRunStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> object:
        return getattr(self._store, name)

    def read_ledger_id(self) -> str | None:
        raise sqlite3.OperationalError("database is locked")


def test_an_unreadable_ledger_identity_keeps_sandbox_clones_and_says_so(
    site: Site,
) -> None:
    with SqliteRunStore(str(site.db)) as store:
        plan = gc.select(
            load_config(),
            _IdentityUnreadable(store),  # type: ignore[arg-type]
            lambda _kind, number: "CLOSED" if number in ("7", "70") else "OPEN",
            [],
            open_prs=lambda _branch: [],
            project=site.root,
        )

    assert plan.unreadable == [
        "run ledger identity could not be read (database is locked)"
    ]
    selected = {checkout.path for checkout in plan.checkouts}
    assert site.agent / "issue-7" in selected
    assert not any(path.is_relative_to(site.sandbox) for path in selected)


def _live_run(store: SqliteRunStore, site: Site) -> None:
    store.save(
        RunRecord(
            run_id="live-7",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )


def _deferral(store: SqliteRunStore, site: Site) -> None:
    store.save(
        RunRecord(
            run_id="deferred-7",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="awaiting_publication",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )


def _continuation(store: SqliteRunStore, site: Site) -> None:
    assert store.reserve_epic_node_item("e", "7", "temporary") is True
    record = store.register_continuation_and_release_reservation(
        "e",
        "7",
        "original-7",
        "agent/issue-7",
        site.agent / "issue-7",
        "temporary",
        checkout_generation="",
    )
    assert record is not None


@pytest.mark.parametrize("hold", [_live_run, _deferral, _continuation])
def test_a_checkout_bound_to_a_live_run_or_deferral_is_kept(site: Site, hold) -> None:
    with SqliteRunStore(str(site.db)) as store:
        hold(store, site)
    agent_paths = [
        p
        for p in site.item_paths(7, 70)
        if p.name.startswith(("issue-", "kew-sandbox-"))
    ]

    code, output = _gc("--apply")

    assert code == 0, output
    for path in agent_paths:
        assert os.path.lexists(path), path
    assert not (site.review / f"pr-70.preserved-{_STAMP}").exists()


def test_a_checkout_a_live_run_claims_is_kept(site: Site) -> None:
    import fcntl

    lock = Path(f"{site.agent / 'issue-7'}.lock")
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, output = _gc("--apply")

    assert code == 1, output
    assert (site.agent / "issue-7").is_dir()
    assert lock.exists()
    assert not (site.agent / f"issue-7.preserved-{_STAMP}").exists()


def test_a_registered_worktree_is_unregistered_from_the_primary_checkout(
    site: Site,
) -> None:
    project = site.root / "project"
    registered = site.agent / "issue-7"
    shutil.rmtree(registered)
    _git(project, "worktree", "add", "-q", "-b", "agent/issue-7", str(registered))
    assert str(registered) in _git(project, "worktree", "list", "--porcelain")

    code, output = _gc("--apply")

    assert code == 0, output
    assert not registered.exists()
    assert str(registered) not in _git(project, "worktree", "list", "--porcelain")


# --- The work root: what sweep_work_root refuses, gc does not enter ----------


def test_an_unscoped_ledger_keeps_the_unscoped_clones_and_exits_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger whose scope is ``unscoped`` shares it with every unregistered
    project, so no clone there can be vouched for."""
    other = site.unscoped / "kew-sandbox-7-zz_yy987"
    site.checkout(other, 50)
    monkeypatch.setattr(
        SqliteRunStore, "read_ledger_id", lambda _self: work_dirs.UNSCOPED
    )

    code, output = _gc("--apply")

    assert code == 2, output
    assert "shared unscoped directory" in output
    assert other.is_dir()
    assert Path(f"{other}.lock").exists()
    assert (site.sandbox / "kew-sandbox-7-ab_cd123").is_dir()
    assert not (site.agent / "issue-7").exists()


def test_a_symlinked_ledger_scope_is_not_entered(site: Site) -> None:
    elsewhere = site.root / "elsewhere"
    shutil.move(site.sandbox, elsewhere)
    site.sandbox.symlink_to(elsewhere, target_is_directory=True)

    code, output = _gc("--apply")

    assert code == 2, output
    assert "ledger scope refused" in output
    assert (elsewhere / "kew-sandbox-7-ab_cd123").is_dir()
    assert Path(f"{elsewhere / 'kew-sandbox-7-ab_cd123'}.lock").exists()


def test_a_work_root_kew_did_not_create_is_not_entered(site: Site) -> None:
    (work_dirs.configured_root() / work_dirs.ROOT_MARKER).unlink()

    code, output = _gc("--apply")

    assert code == 2, output
    assert "work root refused" in output
    assert (site.sandbox / "kew-sandbox-7-ab_cd123").is_dir()
    assert not (site.agent / "issue-7").exists()


# --- An open PR keeps its issue's checkouts -----------------------------------


def _issue_paths(site: Site, issue: int) -> list[Path]:
    return [
        p
        for p in site.bytes
        if p.name.startswith((f"issue-{issue}", f"kew-sandbox-{issue}-"))
    ]


def test_an_open_pr_from_the_issue_branch_keeps_a_closed_issues_checkouts(
    site: Site,
) -> None:
    site.forge.prs[70]["state"] = "OPEN"

    code, output = _gc("--apply")

    assert code == 0, output
    assert "issue #7: PR #70 is open" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


def test_an_open_pr_the_ledger_records_keeps_a_closed_issues_checkouts(
    site: Site,
) -> None:
    pr = site.forge.open_pr(issue=7, branch="feature/elsewhere", head="c" * 40)
    with SqliteRunStore(str(site.db)) as store:
        store.save(
            RunRecord(
                run_id="done-7",
                issue_num=7,
                item_id="7",
                branch="feature/elsewhere",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                pr_url=pr["url"],
            )
        )

    code, output = _gc("--apply")

    assert code == 0, output
    assert f"issue #7: PR #{pr['number']} is open" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


def _gh_fails(site: Site, monkeypatch: pytest.MonkeyPatch, verb: list[str]) -> None:
    """``gh <verb>`` fails at the process boundary; the forge answers the rest."""

    def forge(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == verb:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502: Bad Gateway")
        return site.forge(cmd, **kwargs)

    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )


def test_unreadable_open_prs_keep_the_issues_checkouts_and_exit_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gh_fails(site, monkeypatch, ["pr", "list"])

    code, output = _gc("--apply")

    assert code == 2, output
    assert "issue #7: open PRs could not be read (gh exited 1:" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


# --- The ledger's holds ---------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        "awaiting_publication",
        "awaiting_continuation",
        # A held publication, set by dispatch_sandbox; not a holding category
        # of the work-root sweep, so only the deferred-publication rule holds.
        "publication_held",
    ],
)
def test_a_finished_row_in_a_holding_category_keeps_the_checkouts(
    site: Site, category: str
) -> None:
    with SqliteRunStore(str(site.db)) as store:
        store.save(
            RunRecord(
                run_id="held-7",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="failure",
                exit_code=1,
                started_at=datetime.now(UTC),
                error_category=category,
            )
        )

    code, output = _gc("--apply")

    assert code == 0, output
    assert f"run held-7 (failure, {category}) holds it" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


def _pending_review(store: SqliteRunStore) -> None:
    store.save(
        RunRecord(
            run_id="review-70",
            issue_num=70,
            branch="agent/issue-7",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
            run_kind="review",
        )
    )


def _pr_reservation(store: SqliteRunStore) -> None:
    assert store.reserve_epic_node_item("e", "70", "temporary") is True


@pytest.mark.parametrize("hold", [_pending_review, _pr_reservation])
def test_a_review_run_or_reservation_on_the_pr_keeps_its_review_checkouts(
    site: Site, hold
) -> None:
    with SqliteRunStore(str(site.db)) as store:
        hold(store)
    copy = site.review / f"pr-70.preserved-{_STAMP}"

    code, output = _gc("--apply")

    assert code == 0, output
    assert copy.is_dir()
    assert not (site.agent / "issue-7").exists()


def test_the_hold_is_read_again_once_the_claim_is_held(site: Site) -> None:
    """A run that starts between the listing and the removal keeps its tree."""
    config = load_config()
    subprocess_utils.preflight_tools(required=("gh", "git"))
    tracker = gc.github_tracker("o/r")
    open_prs = gc.github_open_prs("o/r")
    with SqliteRunStore(str(site.db)) as store:
        plan = gc.select(
            config, store, tracker, [], open_prs=open_prs, project=site.root
        )
        assert site.agent / "issue-7" in {c.path for c in plan.checkouts}
        _live_run(store, site)
        outcome = gc.apply(
            plan,
            runtime="docker",
            repo_root=site.root / "project",
            hold=gc.recheck(config, store, tracker, open_prs),
        )

    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path
    assert any(
        f"{site.agent / 'issue-7'}: kept, held once claimed: issue #7: run live-7"
        in failure
        for failure in outcome.failed
    ), outcome.failed
    assert not (site.review / f"pr-70.preserved-{_STAMP}").exists()


# --- Image tags only with --images ----------------------------------------------


def test_apply_without_images_removes_no_tag(site: Site) -> None:
    add_image(site.docker, "kew-agent:old", _IMAGE_OLD, "1.2GB")

    code, output = _gc("--apply")

    assert code == 0, output
    assert "kew-agent:old" not in output
    assert image_references(site.docker) == {"kew-agent:old"}


def _path_without_a_container_runtime(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH holds only the ``gh`` stub and a ``git`` that execs the real one:
    no system ``docker`` or ``podman`` (/usr/local/bin, OrbStack's) is on it."""
    real_git = shutil.which("git")
    assert real_git is not None
    tools = site.root / "tools"
    tools.mkdir()
    (tools / "git").write_text(f'#!/bin/sh\nexec "{real_git}" "$@"\n')
    (tools / "git").chmod(0o755)
    (site.root / "bin" / "docker").unlink()
    monkeypatch.setenv("PATH", f"{site.root / 'bin'}{os.pathsep}{tools}")
    on_path = {entry.name for d in (site.root / "bin", tools) for entry in d.iterdir()}
    assert not on_path & {"docker", "podman"}, on_path


def test_images_with_no_runtime_binary_keeps_everything_and_exits_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path_without_a_container_runtime(site, monkeypatch)
    before = site.tree()

    code, output = _gc("--images")

    assert code == 2, output
    assert "runtime 'docker' was not found" in output
    assert site.tree() == before


def test_an_unreadable_image_inventory_removes_no_tag_and_exits_2(
    site: Site,
) -> None:
    add_image(site.docker, "kew-agent:old", _IMAGE_OLD)
    inventory = site.docker / "images.json"
    listed = inventory.read_bytes()
    # The stub's `image ls` fails on a state it cannot parse.
    inventory.write_text("not json")

    code, output = _gc("--apply", "--images")

    assert code == 2, output
    assert "agent image inventory could not be read" in output
    assert inventory.read_text() == "not json"
    inventory.write_bytes(listed)
    assert image_references(site.docker) == {"kew-agent:old"}


class _ImageRmFails:
    """``<runtime> image rm`` fails at the process boundary; every other argv
    runs as before."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(str(cmd[0])).name == "docker" and cmd[1:3] == ["image", "rm"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: image is in use")
        return self._inner.run(cmd, **kwargs)  # type: ignore[attr-defined]


def test_an_image_tag_the_runtime_will_not_remove_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_image(site.docker, "kew-agent:old", _IMAGE_OLD)
    monkeypatch.setattr(
        subprocess_utils, "subprocess", _ImageRmFails(subprocess_utils.subprocess)
    )

    code, output = _gc("--apply", "--images")

    assert code == 1, output
    assert "Not removed: image kew-agent:old: Error: image is in use" in output
    assert image_references(site.docker) == {"kew-agent:old"}


# --- No silent git failure ------------------------------------------------------


class _GitFails:
    """``git worktree <verb>`` fails at the process boundary — exits 128, or
    cannot start when *raises* — and every other argv runs as before."""

    def __init__(self, inner: object, verb: str, *, raises: bool = False) -> None:
        self._inner = inner
        self._verb = verb
        self._raises = raises

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(str(cmd[0])).name == "git" and cmd[3:5] == ["worktree", self._verb]:
            if self._raises:
                raise OSError("injected: git not startable")
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: injected")
        return self._inner.run(cmd, **kwargs)  # type: ignore[attr-defined]


def _registered_issue_7(site: Site) -> Path:
    project = site.root / "project"
    registered = site.agent / "issue-7"
    shutil.rmtree(registered)
    _git(project, "worktree", "add", "-q", "-b", "agent/issue-7", str(registered))
    return registered


def test_an_unreadable_worktree_list_keeps_every_checkout_and_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_issue_7(site)
    monkeypatch.setattr(
        subprocess_utils, "subprocess", _GitFails(subprocess_utils.subprocess, "list")
    )

    code, output = _gc("--apply")

    assert code == 1, output
    assert "kept, git worktree list failed (exit 128: fatal: injected)" in output
    assert registered.is_dir()
    for path in site.item_paths(7, 70):
        assert os.path.lexists(path), path


def test_a_failed_worktree_remove_keeps_the_tree_and_exits_1(site: Site) -> None:
    registered = _registered_issue_7(site)
    # A locked worktree refuses a single --force.
    _git(site.root / "project", "worktree", "lock", str(registered))

    code, output = _gc("--apply")

    assert code == 1, output
    assert f"{registered}: kept, git worktree remove failed" in output
    assert registered.is_dir()
    assert str(registered) in _git(site.root / "project", "worktree", "list")
    assert not (site.agent / f"issue-7.preserved-{_STAMP}").exists()


def test_a_failed_worktree_prune_is_reported_and_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_issue_7(site)
    monkeypatch.setattr(
        subprocess_utils, "subprocess", _GitFails(subprocess_utils.subprocess, "prune")
    )

    code, output = _gc("--apply")

    assert code == 1, output
    assert "git worktree prune failed (exit 128: fatal: injected)" in output
    assert not registered.exists()


def test_a_worktree_prune_that_cannot_run_is_reported_and_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_issue_7(site)
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        _GitFails(subprocess_utils.subprocess, "prune", raises=True),
    )

    code, output = _gc("--apply")

    assert code == 1, output
    assert "git worktree prune could not run (injected: git not startable)" in output
    assert not registered.exists()


# --- The dry run opens the ledger read-only -------------------------------------


def test_the_dry_run_never_registers_a_ledger(site: Site) -> None:
    """A read-only open refuses a ledger no command registered; a writable
    one would write the registry."""
    registry = site.root / ".kew" / "ledger.json"
    assert registry.is_file()
    registry.unlink()
    (site.root / "audit-anchors.jsonl").unlink(missing_ok=True)
    site.db.unlink()
    for sidecar in ("-wal", "-shm"):
        Path(f"{site.db}{sidecar}").unlink(missing_ok=True)
    SqliteRunStore(str(site.db)).close()
    before = site.tree()

    code, output = _gc()

    assert code == 2, output
    assert not registry.exists()
    assert site.tree() == before


# --- Guards the earlier rounds added ---------------------------------------------


def test_a_symlink_named_like_a_checkout_is_never_followed_or_removed(
    site: Site,
) -> None:
    target = site.root / "precious"
    (target / "src").mkdir(parents=True)
    (target / "src" / "keep.txt").write_text("keep")
    link = site.agent / f"issue-7.preserved-{_STAMP}-link"
    link.symlink_to(target, target_is_directory=True)

    code, output = _gc("--apply")

    assert code == 0, output
    assert str(link) not in output
    assert link.is_symlink()
    assert (target / "src" / "keep.txt").read_text() == "keep"


def test_a_checkout_that_is_a_symlink_is_kept_with_its_sidecars(
    site: Site,
) -> None:
    project = site.root / "project"
    victim = site.root / "live-worktree"
    _git(project, "worktree", "add", "-q", "-b", "agent/issue-8", str(victim))
    (victim / "precious.txt").write_text("uncommitted")
    link = site.agent / "issue-7"
    shutil.rmtree(link)
    Path(f"{link}.generation").unlink()
    link.symlink_to(victim, target_is_directory=True)
    lock = Path(f"{link}.lock")

    _, dry_output = _gc()
    code, output = _gc("--apply")

    assert code == 0, output
    assert str(lock) not in dry_output
    assert str(lock) not in output
    assert lock.read_text() == "1\n"
    assert link.is_symlink()
    assert (victim / "precious.txt").read_text() == "uncommitted"
    assert str(victim) in _git(project, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("suffix", [".lock", ".generation"])
def test_a_checkout_with_a_symlinked_sidecar_is_kept_whole(
    site: Site, suffix: str
) -> None:
    """Claiming or removing the checkout would write through or unlink the
    link, so the checkout, its link and its other sidecar all stay."""
    checkout = site.agent / "issue-7"
    target = site.root / "outside"
    target.write_text("ORIGINAL\n")
    sidecar = Path(f"{checkout}{suffix}")
    sidecar.unlink()
    sidecar.symlink_to(target)

    _, dry_output = _gc()
    code, output = _gc("--apply")

    assert code == 0, output
    for listed in (dry_output, output):
        paths = {line.split()[-1] for line in listed.splitlines() if line.strip()}
        assert str(checkout) not in paths
        assert str(sidecar) not in paths
    assert sidecar.is_symlink()
    assert target.read_text() == "ORIGINAL\n"
    assert (checkout / "src" / "work.txt").is_file()
    assert Path(f"{checkout}.lock").exists()
    assert Path(f"{checkout}.generation").exists()


def test_a_sidecar_replaced_by_a_symlink_once_selected_is_kept(
    site: Site,
) -> None:
    """A lock link swapped in after selection is never claimed through."""
    checkout = site.agent / "issue-7"
    lock = Path(f"{checkout}.lock")
    target = site.root / "outside"
    target.write_text("ORIGINAL\n")
    config = load_config()
    subprocess_utils.preflight_tools(required=("gh", "git"))
    tracker = gc.github_tracker("o/r")
    open_prs = gc.github_open_prs("o/r")

    with SqliteRunStore(str(site.db)) as store:
        plan = gc.select(
            config, store, tracker, [], open_prs=open_prs, project=site.root
        )
        assert checkout in {c.path for c in plan.checkouts}
        lock.unlink()
        lock.symlink_to(target)
        outcome = gc.apply(
            plan, runtime="docker", repo_root=site.root / "project", hold=lambda _: None
        )

    assert f"{checkout}: kept, {lock} is a symlink" in outcome.failed
    assert lock.is_symlink()
    assert target.read_text() == "ORIGINAL\n"
    assert (checkout / "src" / "work.txt").is_file()


def test_a_checkout_replaced_by_a_symlink_once_claimed_is_kept(
    site: Site,
) -> None:
    """A link swapped in after selection is never resolved by the removal."""
    project = site.root / "project"
    victim = site.root / "live-worktree"
    _git(project, "worktree", "add", "-q", "-b", "agent/issue-8", str(victim))
    (victim / "precious.txt").write_text("uncommitted")
    checkout = site.agent / "issue-7"
    config = load_config()
    subprocess_utils.preflight_tools(required=("gh", "git"))
    tracker = gc.github_tracker("o/r")
    open_prs = gc.github_open_prs("o/r")

    def swap_then_release(selected: gc.Checkout) -> str | None:
        if selected.path == checkout:
            shutil.rmtree(checkout)
            checkout.symlink_to(victim, target_is_directory=True)
        return None

    with SqliteRunStore(str(site.db)) as store:
        plan = gc.select(
            config, store, tracker, [], open_prs=open_prs, project=site.root
        )
        assert checkout in {c.path for c in plan.checkouts}
        outcome = gc.apply(
            plan, runtime="docker", repo_root=project, hold=swap_then_release
        )

    assert f"{checkout}: kept, {checkout} is a symlink" in outcome.failed
    assert checkout.is_symlink()
    assert (victim / "precious.txt").read_text() == "uncommitted"
    assert str(victim) in _git(project, "worktree", "list", "--porcelain")


def test_a_non_numeric_item_is_kept_and_exits_2(site: Site) -> None:
    odd = site.agent / "issue-abc"
    site.checkout(odd, 10)

    code, output = _gc("--apply")

    assert code == 2, output
    assert "issue #abc: tracker state could not be read ('abc' is not an item" in (
        output
    )
    assert odd.is_dir()


def _deferred_review(store: SqliteRunStore, checkout: str) -> None:
    from kew.epic_holds import deferred_publication_key

    store.save(
        RunRecord(
            run_id="deferred-70",
            issue_num=70,
            branch="agent/issue-7",
            status="awaiting_publication",
            exit_code=0,
            started_at=datetime.now(UTC),
            run_kind="review",
        )
    )
    store.set_meta(
        deferred_publication_key("deferred-70"),
        json.dumps({"pr": 70, "checkout": checkout}),
    )


def test_a_deferral_keeps_only_the_review_copy_it_names(site: Site) -> None:
    named = site.review / f"pr-70.preserved-{_STAMP}"
    other = site.review / "pr-70.preserved-20261002T000000000000Z"
    site.checkout(other, 5, sidecars=False)
    with SqliteRunStore(str(site.db)) as store:
        _deferred_review(store, str(named))

    code, output = _gc("--apply")

    assert code == 0, output
    assert named.is_dir()
    assert not other.exists()


def test_a_deferral_whose_copy_is_unknown_keeps_every_review_copy(
    site: Site,
) -> None:
    other = site.review / "pr-70.preserved-20261002T000000000000Z"
    site.checkout(other, 5, sidecars=False)
    with SqliteRunStore(str(site.db)) as store:
        _deferred_review(store, "")

    code, output = _gc("--apply")

    assert code == 0, output
    assert other.is_dir()
    assert (site.review / f"pr-70.preserved-{_STAMP}").is_dir()


# --- The review claim ---------------------------------------------------------------


def test_a_review_claim_another_reviewer_holds_keeps_the_copy_and_its_lock(
    site: Site,
) -> None:
    import fcntl

    lock = site.review / "pr-70.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, output = _gc("--apply")
        assert os.stat(handle.fileno()).st_ino == os.stat(lock).st_ino

    assert code == 1, output
    assert "claimed by another reviewer" in output
    assert (site.review / f"pr-70.preserved-{_STAMP}").is_dir()


def test_the_review_lock_stays_while_the_review_directory_it_claims_stays(
    site: Site,
) -> None:
    """The claim on ``pr-70.lock`` is the live review directory's; removing
    one of its copies must not unlink it from under the next reviewer."""
    review_dir = site.review / "pr-70"
    site.checkout(review_dir, 7)
    lock = Path(f"{review_dir}.lock")
    with SqliteRunStore(str(site.db)) as store:
        _deferred_review(store, str(review_dir))

    code, output = _gc("--apply")

    assert code == 0, output
    assert review_dir.is_dir()
    assert lock.read_text() != ""
    assert not (site.review / f"pr-70.preserved-{_STAMP}").exists()


# --- Review reservations, as kew review takes them ----------------------------------


def test_a_governed_review_reservation_on_the_owner_issue_keeps_the_review_copies(
    site: Site,
) -> None:
    """A governed review reserves its node item, the PR's owner issue, under
    the review pseudo-epic; the review runs of the PR record that item."""
    from kew.pseudo_epics import REVIEW_EPIC_REF

    with SqliteRunStore(str(site.db)) as store:
        store.save(
            RunRecord(
                run_id="review-70-round-1",
                issue_num=70,
                item_id="7",
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                run_kind="review",
            )
        )
        assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "7", "round-2") is True
    copy = site.review / f"pr-70.preserved-{_STAMP}"

    code, output = _gc("--apply")

    assert code == 0, output
    assert "PR #70: a live reservation holds it" in output
    assert copy.is_dir()


def test_a_hand_run_review_reservation_keeps_the_review_copies(site: Site) -> None:
    """``kew review <PR>`` reserves the owner issue before it writes any row;
    its reservation record names the PR."""
    from kew.review_occupancy import reserve_standalone_review

    with SqliteRunStore(str(site.db)) as store:
        holder = reserve_standalone_review(
            store, "7", "hand-review", pr_number=70, repo="o/r"
        )
        assert holder is None
    copy = site.review / f"pr-70.preserved-{_STAMP}"

    code, output = _gc("--apply")

    assert code == 0, output
    assert "PR #70: a live review reservation holds it" in output
    assert copy.is_dir()


def test_a_live_reservation_on_the_issue_keeps_its_checkouts(site: Site) -> None:
    with SqliteRunStore(str(site.db)) as store:
        assert store.reserve_epic_node_item("e", "7", "live") is True

    code, output = _gc("--apply")

    assert code == 0, output
    assert "issue #7: a live reservation holds it" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


# --- The review lock -----------------------------------------------------------------


def test_a_symlinked_review_lock_is_never_written_through(site: Site) -> None:
    lock = site.review / "pr-70.lock"
    target = site.root / "outside"
    target.write_text("ORIGINAL\n")
    lock.symlink_to(target)
    copy = site.review / f"pr-70.preserved-{_STAMP}"

    code, output = _gc("--apply")

    assert code == 1, output
    assert f"{copy}: kept, {lock} is a symlink" in output
    assert target.read_text() == "ORIGINAL\n"
    assert lock.is_symlink()
    assert copy.is_dir()


def _apply_with(site: Site, hold: gc.Recheck) -> tuple[gc.Plan, gc.Outcome]:
    config = load_config()
    subprocess_utils.preflight_tools(required=("gh", "git"))
    tracker = gc.github_tracker("o/r")
    open_prs = gc.github_open_prs("o/r")
    with SqliteRunStore(str(site.db)) as store:
        plan = gc.select(
            config, store, tracker, [], open_prs=open_prs, project=site.root
        )
        outcome = gc.apply(
            plan, runtime="docker", repo_root=site.root / "project", hold=hold
        )
    return plan, outcome


def test_a_review_lock_replaced_while_it_is_claimed_is_refused(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another reviewer's lock put at the path between gc's open and its
    ``flock`` is not the file gc holds: the copy stays."""
    import fcntl

    lock = site.review / "pr-70.lock"
    copy = site.review / f"pr-70.preserved-{_STAMP}"
    real_flock = fcntl.flock

    def swap_then_flock(fd: int, operation: int) -> None:
        if os.path.lexists(lock) and os.fstat(fd).st_ino == os.stat(lock).st_ino:
            replacement = site.review / "replacement"
            replacement.write_text("other\n")
            replacement.rename(lock)
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", swap_then_flock)

    _, outcome = _apply_with(site, lambda _checkout: None)

    assert any(
        f"{copy}: kept, " in failure
        and "was replaced while it was being claimed" in failure
        for failure in outcome.failed
    ), outcome.failed
    assert copy.is_dir()
    assert lock.read_text() == "other\n"


def test_a_review_lock_replaced_during_the_removal_is_not_unlinked(
    site: Site,
) -> None:
    """The lock at the path is another reviewer's once gc's was replaced:
    gc unlinks only the file it still holds."""
    lock = site.review / "pr-70.lock"
    copy = site.review / f"pr-70.preserved-{_STAMP}"

    def replace_the_lock(checkout: gc.Checkout) -> str | None:
        if checkout.path == copy:
            replacement = site.review / "replacement"
            replacement.write_text("other\n")
            replacement.rename(lock)
        return None

    _, outcome = _apply_with(site, replace_the_lock)

    assert str(copy) in outcome.removed
    assert lock.read_text() == "other\n"


# --- No silent git failure, and the primary checkout ---------------------------------


def test_a_worktree_list_that_cannot_run_keeps_every_checkout_and_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_issue_7(site)
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        _GitFails(subprocess_utils.subprocess, "list", raises=True),
    )

    code, output = _gc("--apply")

    assert code == 1, output
    assert "kept, git worktree list could not run (injected: git not startable)" in (
        output
    )
    assert registered.is_dir()
    for path in site.item_paths(7, 70):
        assert os.path.lexists(path), path


def test_a_worktree_remove_that_cannot_run_keeps_the_tree_and_exits_1(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_issue_7(site)
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        _GitFails(subprocess_utils.subprocess, "remove", raises=True),
    )

    code, output = _gc("--apply")

    assert code == 1, output
    assert (
        f"{registered}: kept, git worktree remove could not run "
        "(injected: git not startable)"
    ) in output
    assert registered.is_dir()
    assert str(registered) in _git(site.root / "project", "worktree", "list")


def test_apply_without_a_git_checkout_removes_nothing_and_exits_2(
    site: Site,
) -> None:
    shutil.rmtree(site.root / "project" / ".git")
    before = site.tree()

    code, output = _gc("--apply")

    assert code == 2, output
    assert "no git checkout found" in output
    assert "(git names no checkout there)" in output
    assert site.tree() == before


class _RevParseCannotStart:
    """``git rev-parse`` cannot start at the process boundary; every other
    argv runs as before."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(str(cmd[0])).name == "git" and "rev-parse" in cmd:
            raise OSError("injected: git not startable")
        return self._inner.run(cmd, **kwargs)  # type: ignore[attr-defined]


def test_apply_when_git_cannot_name_the_checkout_says_why_and_exits_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = site.tree()
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        _RevParseCannotStart(subprocess_utils.subprocess),
    )

    code, output = _gc("--apply")

    assert code == 2, output
    assert "no git checkout found" in output
    assert "(git could not run (injected: git not startable))" in output
    assert site.tree() == before


@pytest.mark.parametrize("args", [(), ("--apply",)])
def test_without_a_run_ledger_nothing_is_listed_or_removed_and_exits_2(
    site: Site, args: tuple[str, ...]
) -> None:
    site.db.unlink()
    before = site.tree()

    code, output = _gc(*args)

    assert code == 2, output
    assert "no run ledger at" in output
    assert "Would remove" not in output
    assert site.tree() == before


def test_the_primary_checkout_is_the_projects_not_the_cwds(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from inside another repository, gc still unregisters the
    worktree from the project's checkout."""
    registered = _registered_issue_7(site)
    elsewhere = site.root / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "main")
    monkeypatch.setenv("KEW_PROJECT", str(site.root / "project"))
    monkeypatch.chdir(elsewhere)

    code, output = _gc("--apply")

    assert code == 0, output
    assert not registered.exists()
    assert str(registered) not in _git(
        site.root / "project", "worktree", "list", "--porcelain"
    )


# --- The ledger and the tracker ---------------------------------------------------


def _ledger_fails(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    def fails(_self: object, *_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteRunStore, method, fails)


def test_an_unreadable_ledger_keeps_the_checkouts_and_exits_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_fails(monkeypatch, "list_by_item")
    before = site.tree()

    code, output = _gc("--apply")

    assert code == 2, output
    assert "run ledger could not be read (database is locked)" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path
    assert {k: v for k, v in site.tree().items() if "issue-7" in k} == {
        k: v for k, v in before.items() if "issue-7" in k
    }


def test_an_unreadable_deferral_record_keeps_the_review_copies_and_exits_2(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_fails(monkeypatch, "list_meta")

    code, output = _gc("--apply")

    assert code == 2, output
    assert "run ledger could not be read (database is locked)" in output
    assert (site.review / f"pr-70.preserved-{_STAMP}").is_dir()


def test_a_recorded_pr_of_another_repository_keeps_the_issues_checkouts(
    site: Site,
) -> None:
    """Another repository's PR #70 is not this repository's merged PR #70."""
    foreign = "https://github.com/other/repo/pull/70"
    with SqliteRunStore(str(site.db)) as store:
        store.save(
            RunRecord(
                run_id="done-7",
                issue_num=7,
                item_id="7",
                branch="feature/elsewhere",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                pr_url=foreign,
            )
        )

    code, output = _gc("--apply")

    assert code == 0, output
    assert f"issue #7: a run recorded {foreign}, not a PR of o/r" in output
    for path in _issue_paths(site, 7):
        assert os.path.lexists(path), path


# --- What the scan enters ---------------------------------------------------------


def test_a_symlinked_checkout_directory_is_not_entered(site: Site) -> None:
    elsewhere = site.root / "elsewhere-agent"
    shutil.move(site.agent, elsewhere)
    site.agent.symlink_to(elsewhere, target_is_directory=True)

    code, output = _gc("--apply")

    assert code == 0, output
    assert str(site.agent / "issue-7") not in output
    assert site.agent.is_symlink()
    assert (elsewhere / "issue-7" / "src" / "work.txt").is_file()
    assert Path(f"{elsewhere / 'issue-7'}.lock").exists()


def test_a_closed_items_lone_lock_and_generation_files_are_removed(
    site: Site,
) -> None:
    """A checkout whose directory is already gone leaves its sidecars: they
    are listed and removed like any other."""
    gone = site.agent / f"issue-7.replaced-{_STAMP}-012345678901"
    lone = [Path(f"{gone}.lock"), Path(f"{gone}.generation")]
    lone[0].write_text("1\n")
    lone[1].write_text("0" * 32)
    open_lone = Path(f"{site.agent / 'issue-8.replaced-x'}.lock")
    open_lone.write_text("1\n")

    _, dry_output = _gc()
    code, output = _gc("--apply")

    assert code == 0, output
    for path in lone:
        assert str(path) in dry_output
        assert not os.path.lexists(path), path
    assert open_lone.exists()
