import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from kew.config import DispatchConfig, RouteEntry, RoutingConfig
from kew.policy import (
    HOOK_ABSENT,
    HOOK_UNREADABLE,
    canonical_json,
    compute_policy,
    hook_digest,
    policy_hash,
    policy_snapshot,
    repo_root,
)


def _git_init(path: Path) -> None:
    """A real repository at *path*: an empty ``.git/`` is none to git (#3108)."""
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", str(path)], check=True)  # noqa: S603


def _absent_hook(tmp_path: Path) -> Path:
    """A hook path guaranteed not to exist (hermetic — never the real repo hook)."""
    return tmp_path / ".claude" / "hooks" / "security-guard.sh"


def test_default_config_yields_real_hash_not_none():
    # F1: routing is None by default; compute must still produce a hash.
    config = DispatchConfig()
    assert config.routing is None
    snap = policy_snapshot(config, HOOK_ABSENT)
    assert snap["routing"] is None
    h = policy_hash(snap)
    assert isinstance(h, str) and len(h) == 64


def test_snapshot_is_deterministic_and_key_order_independent():
    config = DispatchConfig()
    a = canonical_json(policy_snapshot(config, HOOK_ABSENT))
    b = canonical_json(policy_snapshot(config, HOOK_ABSENT))
    assert a == b
    # canonical_json sorts keys, so a manually reordered dict hashes identically.
    snap = policy_snapshot(config, HOOK_ABSENT)
    reordered = dict(reversed(list(snap.items())))
    assert canonical_json(reordered) == canonical_json(snap)


def test_bare_string_route_equals_routeentry_form():
    str_cfg = DispatchConfig(routing=RoutingConfig(default="opencode"))
    obj_cfg = DispatchConfig(
        routing=RoutingConfig(default=RouteEntry(runner="opencode"))
    )
    assert policy_hash(policy_snapshot(str_cfg, HOOK_ABSENT)) == policy_hash(
        policy_snapshot(obj_cfg, HOOK_ABSENT)
    )


def test_reordered_approvers_hash_identically():
    # approvers is an order-independent allowlist — reordering is the same policy.
    a = DispatchConfig()
    a.approval.approvers = ["alice", "bob", "carol"]
    b = DispatchConfig()
    b.approval.approvers = ["carol", "alice", "bob"]
    assert policy_hash(policy_snapshot(a, HOOK_ABSENT)) == policy_hash(
        policy_snapshot(b, HOOK_ABSENT)
    )


def test_routing_none_differs_from_populated():
    none_cfg = DispatchConfig()
    set_cfg = DispatchConfig(routing=RoutingConfig(default="claude-code"))
    assert policy_hash(policy_snapshot(none_cfg, HOOK_ABSENT)) != policy_hash(
        policy_snapshot(set_cfg, HOOK_ABSENT)
    )


def test_allowlist_excludes_webhook_url_and_unknown_fields():
    config = DispatchConfig()
    config.alerting.webhook_url = "https://hooks.example.com/SECRET-TOKEN"
    blob = canonical_json(policy_snapshot(config, HOOK_ABSENT))
    assert "SECRET-TOKEN" not in blob
    assert "webhook_url" not in blob
    assert "alerting" not in blob


def test_hash_sensitive_to_each_attested_field():
    base = DispatchConfig()
    base_h = policy_hash(policy_snapshot(base, HOOK_ABSENT))

    def _mutated(apply) -> DispatchConfig:
        cfg = DispatchConfig()
        apply(cfg)
        return cfg

    mutators = [
        lambda c: setattr(c.budget, "per_issue_usd", 999.0),
        lambda c: setattr(c.approval, "label", "different-label"),
        lambda c: setattr(c.approval, "approvers", ["alice"]),
        lambda c: setattr(c.agent, "max_budget_usd", 99.0),
        lambda c: setattr(c.agent, "timeout_minutes", 5),
        lambda c: setattr(c.security, "blocked_exact_patterns", ["rm -rf /"]),
        lambda c: setattr(c.breaker, "min_samples", 99),
    ]
    for mutate in mutators:
        cfg = _mutated(mutate)
        assert policy_hash(policy_snapshot(cfg, HOOK_ABSENT)) != base_h
    # The hook digest is part of the hash too.
    assert policy_hash(policy_snapshot(base, "deadbeef")) != base_h


def test_run_report_sink_path_is_attested():
    base = DispatchConfig()
    changed = DispatchConfig()
    changed.run_reports.sink_path = "/var/lib/kew/run-reports"

    snapshot = policy_snapshot(base, HOOK_ABSENT)

    assert snapshot["run_reports"] == {
        "sink_path": base.run_reports.sink_path,
    }
    assert policy_hash(snapshot) != policy_hash(policy_snapshot(changed, HOOK_ABSENT))


def test_hook_digest_match_absent_unreadable(tmp_path: Path):
    present = tmp_path / "hook.sh"
    present.write_bytes(b"#!/bin/sh\necho hi\n")
    import hashlib

    assert hook_digest(present) == hashlib.sha256(b"#!/bin/sh\necho hi\n").hexdigest()
    assert hook_digest(_absent_hook(tmp_path)) == HOOK_ABSENT
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    # Reading a directory as bytes raises OSError (not FileNotFoundError) -> UNREADABLE.
    assert hook_digest(a_dir) == HOOK_UNREADABLE


def test_compute_policy_subdir_invariance(tmp_path: Path, monkeypatch):
    """F2: same deployed policy hashes identically from the root and a subdir.

    Behavior change (#3108): the fixture is a real ``git init``, not an empty
    ``.git/`` — ``repo_root`` no longer anchors on a ``.git`` git would refuse.
    """
    _git_init(tmp_path)
    hook = tmp_path / ".claude" / "hooks" / "security-guard.sh"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(b"# managed-by: kew\n")
    config = DispatchConfig()
    monkeypatch.chdir(tmp_path)
    root_hash, _ = compute_policy(config)
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    sub_hash, _ = compute_policy(config)
    assert root_hash == sub_hash
    # And it actually picked up the hook (not the ABSENT sentinel).
    assert compute_policy(config, hook_path=hook)[0] == root_hash


def test_repo_root_finds_dot_git_dir_and_file(tmp_path: Path, monkeypatch):
    """Behavior change (#3108): the ``.git`` dir is a real repository and the
    worktree's ``gitdir:`` names one; an empty dir or a dangling gitdir file
    is no longer an anchor (pinned by the skip tests below)."""
    _git_init(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert repo_root(nested) == tmp_path.resolve()
    # Worktree: .git is a FILE, not a dir.
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {tmp_path / '.git'}\n")
    assert repo_root(wt) == wt.resolve()


def test_compute_policy_returns_hash_and_json(tmp_path: Path):
    config = DispatchConfig()
    h, snap_json = compute_policy(config, hook_path=_absent_hook(tmp_path))
    assert policy_hash(policy_snapshot(config, HOOK_ABSENT)) == h
    assert canonical_json(policy_snapshot(config, HOOK_ABSENT)) == snap_json


def test_policy_hash_reflects_db_override(tmp_path: Path):
    """Overridden config produces a different policy hash than file-only config."""
    from kew.config import load_config
    from kew.run_store import SqliteRunStore

    toml = tmp_path / "kew.toml"
    toml.write_text("[budget]\nper_issue_usd = 15.0\n")
    hook = _absent_hook(tmp_path)

    file_cfg = load_config(config_path=toml)
    file_hash, _ = compute_policy(file_cfg, hook_path=hook)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.set_config_override(
            field="budget.per_issue_usd",
            value_json="99.0",
            file_value_json="15.0",
            set_at="2026-06-29T00:00:00+00:00",
            set_by="test",
        )
        override_cfg = load_config(config_path=toml, store=store)

    override_hash, _ = compute_policy(override_cfg, hook_path=hook)

    assert override_cfg.budget.per_issue_usd == 99.0
    assert file_hash != override_hash


def _git_toplevel(start: Path) -> Path:
    git = shutil.which("git")
    assert git is not None
    done = subprocess.run(  # noqa: S603 — test fixture over a tmp repo
        [git, "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(done.stdout.strip()).resolve()


def test_repo_root_skips_an_invalid_git_entry_as_git_does(tmp_path: Path) -> None:
    """An empty ``svc/.git/`` is no repository to git, which reports the
    enclosing toplevel; ``repo_root`` lands on the same directory."""
    _git_init(tmp_path)
    svc = tmp_path / "svc"
    (svc / ".git").mkdir(parents=True)

    assert repo_root(svc) == _git_toplevel(svc) == tmp_path.resolve()


def test_repo_root_skips_an_invalid_git_file_whose_gitdir_is_gone(
    tmp_path: Path,
) -> None:
    """A stale worktree's ``.git`` file whose ``gitdir:`` is gone names no
    repository: git refuses to report a toplevel from inside it at all, so
    ``repo_root`` skips it to the toplevel git reports for the directory
    above, never anchoring the reviewer copy at the stale checkout."""
    _git_init(tmp_path)
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / ".git").write_text(f"gitdir: {tmp_path / 'gone' / 'worktrees' / 'x'}\n")

    assert repo_root(svc) == _git_toplevel(tmp_path) == tmp_path.resolve()


def test_repo_root_accepts_a_linked_worktree_as_git_does(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    main = tmp_path / "main"
    for args in (
        ["init", "-q", "-b", "main", str(main)],
        [
            "-C",
            str(main),
            "-c",
            "user.email=t@t.t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "root",
        ],
        ["-C", str(main), "worktree", "add", "-q", str(tmp_path / "wt")],
    ):
        subprocess.run([git, *args], check=True)  # noqa: S603
    nested = tmp_path / "wt" / "a"
    nested.mkdir()

    assert repo_root(nested) == _git_toplevel(nested) == (tmp_path / "wt").resolve()


def test_repo_root_records_why_it_skips_an_unreadable_git_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``.git`` file that cannot be read is skipped to the enclosing
    repository, and the skip says why rather than defaulting silently."""
    _git_init(tmp_path)
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / ".git").write_bytes(b"\xff\xfe not utf-8")

    with caplog.at_level(logging.DEBUG, logger="kew.policy"):
        assert repo_root(svc) == tmp_path.resolve()

    (record,) = [r for r in caplog.records if r.name == "kew.policy"]
    assert str(svc / ".git") in record.getMessage()
    assert "unreadable" in record.getMessage()


def test_repo_root_skips_a_fifo_git_entry_without_waiting_on_a_writer(
    tmp_path: Path,
) -> None:
    """A ``.git`` that is a FIFO is no gitdir file to git, which reads only a
    regular file: ``repo_root`` skips it to the enclosing repository at once
    instead of blocking until something writes to the pipe."""
    _git_init(tmp_path)
    svc = tmp_path / "svc"
    svc.mkdir()
    fifo = svc / ".git"
    os.mkfifo(fifo)
    found: list[Path] = []
    worker = threading.Thread(target=lambda: found.append(repo_root(svc)), daemon=True)

    worker.start()
    worker.join(10)
    if worker.is_alive():
        # Release the blocked reader so a regression fails rather than hangs.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        worker.join(10)
        pytest.fail("repo_root blocked opening a FIFO .git")

    assert found == [tmp_path.resolve()]
