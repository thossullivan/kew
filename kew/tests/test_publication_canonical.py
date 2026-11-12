"""End-to-end tests for host-owned canonical publication."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.host_git_boundary import record_git_calls

from kew import (
    dispatch_core,
    publication_canonical,
    publication_extract,
    subprocess_utils,
    worktree,
)
from kew.artifact_store import PackageStore
from kew.config import DispatchConfig
from kew.no_change_seal import seal_no_change_package
from kew.publication import PublicationError
from kew.publication_canonical import (
    CanonicalNoChange,
    CanonicalPublication,
    PublicationHeld,
    publish_checkout,
)
from kew.publication_extract import ExtractionFailed, extract_on_host
from kew.publication_filter import PublicationRefused
from kew.publication_job import CommitIdentity
from kew.publication_repository import CanonicalRepositoryError, provision
from kew.run_store import SqliteRunStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_IDENTITY = CommitIdentity(name="kew test", email="kew@example.invalid")
_STATS_BASELINE = b'{"stars": 1841}\n'
_STATS_GENERATED = b'{"stars": 9999}\n'
_GATE_RUNNER = Path(__file__).parents[2] / "www" / "scripts" / "run-gates.mjs"


@pytest.mark.parametrize("scheme", ["", "socks5://"])
def test_canonical_push_redacts_proxy_before_truncation(tmp_path, monkeypatch, scheme):
    from unittest.mock import MagicMock

    secret = f"{scheme}user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    failure = MagicMock(returncode=1, stderr="x" * 275 + " " + secret)
    monkeypatch.setattr(publication_canonical, "run", MagicMock(return_value=failure))
    repo = MagicMock(path=tmp_path)
    with pytest.raises(PublicationError) as error:
        publication_canonical._push(
            repo,
            remote="origin",
            branch="test",
            commit_sha="a" * 40,
            force_with_lease=None,
        )
    assert "s3cret" not in str(error.value)
    assert "<HTTP_PROXY redacted>" in str(error.value)


def test_publication_config_defaults_to_canonical_and_rejects_unknown() -> None:
    assert DispatchConfig().publication.publish == "canonical"
    assert (
        DispatchConfig.model_validate(
            {"publication": {"publish": "checkout"}}
        ).publication.publish
        == "checkout"
    )
    with pytest.raises(ValidationError, match="publish"):
        DispatchConfig.model_validate({"publication": {"publish": "unsafe"}})


def test_runtime_publication_mode_rejects_an_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(PublicationError, match="unknown publication mode"):
        dispatch_core._git_push_branch(
            "feature",
            tmp_path / "checkout",
            publish="unsafe",
            max_attempts=1,
        )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def scene(tmp_path: Path) -> dict[str, Path | str]:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "keep.txt").write_text("keep\n")
    (seed / "edit.txt").write_text("before\n")
    (seed / "gone.txt").write_text("gone\n")
    stats = seed / "www" / "src" / "lib" / "data" / "repo-stats.json"
    stats.parent.mkdir(parents=True)
    stats.write_bytes(_STATS_BASELINE)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    return {"origin": origin, "seed": seed, "checkout": checkout, "base": base}


def _publish(
    scene: dict[str, Path | str],
    tmp_path: Path,
    *,
    branch: str = "feature",
    parents: tuple[str, ...] | None = None,
    base_sha: str | None = None,
    force_with_lease: str | None = None,
    require_resolved: bool = False,
    before_push=None,
):
    base = str(base_sha or scene["base"])
    lease = force_with_lease
    if lease is None:
        lease = f"{branch}:"
    return publish_checkout(
        Path(scene["checkout"]),
        remote=str(scene["origin"]),
        branch=branch,
        parents=parents or (base,),
        base_sha=base,
        message="publish canonical work",
        identity=_IDENTITY,
        force_with_lease=lease,
        require_resolved=require_resolved,
        output_guard=None,
        before_push=before_push,
        scratch=tmp_path / f"scratch-{branch.replace('/', '-')}",
    )


def _remote_head(origin: Path, branch: str) -> str:
    return _git(origin, "rev-parse", f"refs/heads/{branch}")


def _remote_bytes(origin: Path, revision: str, path: str) -> bytes:
    result = subprocess_utils.run_bytes(
        ["git", "-C", str(origin), "show", f"{revision}:{path}"],
    )
    result.check_returncode()
    return result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize(
    "entry_bytes",
    [_STATS_BASELINE, b'{\n  "stars": 1845,\n  "intentional": true\n}\n'],
    ids=("verification-only-churn", "intentional-statistics-edit"),
)
def test_gate_restoration_reaches_canonical_publication(
    scene, tmp_path: Path, entry_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(scene["checkout"])
    stats = checkout / "www" / "src" / "lib" / "data" / "repo-stats.json"
    stats.write_bytes(entry_bytes)
    (checkout / "edit.txt").write_text("intended\n")
    gate_log = tmp_path / "gate-commands.log"
    bin_dir = tmp_path / "gate-bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$KEW_GATE_LOG"\n'
        "if [ \"$*\" = 'run build' ]; then\n"
        '  printf \'{"stars": 9999}\\n\' > "$KEW_GATE_STATS"\n'
        "fi\n"
    )
    npm.chmod(0o755)
    env = {
        **os.environ,
        "KEW_GATE_LOG": str(gate_log),
        "KEW_GATE_STATS": str(stats),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }

    sh = shutil.which("sh")
    assert sh is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "sh", sh)
    completed = subprocess_utils.run(
        ["sh", "-c", 'exec node "$@"', "kew-node", str(_GATE_RUNNER)],
        cwd=checkout / "www",
        env=env,
        check=False,
    )

    # The fake build rewrites the snapshot, as a build that fetched statistics
    # would. The gates refuse that and name the file; they still put the entry
    # back, which is what publication depends on below.
    assert completed.returncode == 1, completed.stderr
    assert "the gates changed" in completed.stderr
    assert "repo-stats.json" in completed.stderr
    assert gate_log.read_text().splitlines() == [
        "test",
        "run tokens",
        "run check",
        "run build",
    ]
    assert stats.read_bytes() == entry_bytes
    assert entry_bytes != _STATS_GENERATED
    with record_git_calls() as calls:
        publication = _publish(scene, tmp_path)
    violations = [
        call.describe()
        for call in calls
        if call.is_git and call.repository().resolves_inside(checkout)
    ]

    assert violations == []
    assert publication is not None
    expected_paths = ("edit.txt",)
    if entry_bytes != _STATS_BASELINE:
        expected_paths += ("www/src/lib/data/repo-stats.json",)
    assert publication.changed_paths == expected_paths
    assert (
        _remote_bytes(
            Path(scene["origin"]),
            str(scene["base"]),
            "www/src/lib/data/repo-stats.json",
        )
        == _STATS_BASELINE
    )
    assert (
        _remote_bytes(
            Path(scene["origin"]),
            publication.commit_sha,
            "www/src/lib/data/repo-stats.json",
        )
        == entry_bytes
    )
    assert (
        _remote_bytes(Path(scene["origin"]), publication.commit_sha, "edit.txt")
        == b"intended\n"
    )


def test_new_branch_uses_the_dispatch_base_as_parent(scene, tmp_path: Path) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("after\n")

    result = _publish(scene, tmp_path)

    assert result is not None
    assert result.parent_shas == (scene["base"],)
    assert _remote_head(Path(scene["origin"]), "feature") == result.commit_sha
    assert result.changed_paths == ("edit.txt",)


def test_existing_branch_uses_an_exact_lease_and_parent(scene, tmp_path: Path) -> None:
    origin, seed = Path(scene["origin"]), Path(scene["seed"])
    _git(seed, "checkout", "-qb", "feature")
    (seed / "prior.txt").write_text("prior\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "prior")
    _git(seed, "push", "-q", "origin", "feature")
    parent = _remote_head(origin, "feature")
    checkout = Path(scene["checkout"])
    _git(checkout, "fetch", "-q", "origin", "feature")
    _git(checkout, "checkout", "-q", "-B", "feature", "origin/feature")
    (checkout / "edit.txt").write_text("existing branch\n")

    result = _publish(
        scene,
        tmp_path,
        parents=(parent,),
        base_sha=parent,
        force_with_lease=f"feature:{parent}",
    )

    assert result is not None
    assert _git(origin, "show", "-s", "--format=%P", result.commit_sha) == parent


def test_stale_exact_lease_refuses_remote_move(scene, tmp_path: Path) -> None:
    origin, seed = Path(scene["origin"]), Path(scene["seed"])
    _git(seed, "checkout", "-qb", "feature")
    (seed / "prior.txt").write_text("prior\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "prior")
    _git(seed, "push", "-q", "origin", "feature")
    expected = _remote_head(origin, "feature")
    (seed / "remote.txt").write_text("remote move\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "remote move")
    _git(seed, "push", "-q", "origin", "feature")
    moved = _remote_head(origin, "feature")
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("stale lease\n")

    with pytest.raises(PublicationError, match="canonical git push failed"):
        _publish(
            scene,
            tmp_path,
            parents=(expected,),
            base_sha=expected,
            force_with_lease=f"feature:{expected}",
        )

    assert _remote_head(origin, "feature") == moved


def test_lost_push_response_reconciles_the_commit_as_success(
    scene, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("accepted before response was lost\n")
    real_push = publication_canonical._push

    def push_then_lose_response(*args, **kwargs):
        real_push(*args, **kwargs)
        raise PublicationError("push", "connection closed after receive")

    monkeypatch.setattr(publication_canonical, "_push", push_then_lose_response)
    result = _publish(scene, tmp_path)

    assert result is not None
    assert _remote_head(Path(scene["origin"]), "feature") == result.commit_sha


def test_retry_never_adopts_a_concurrently_created_branch(
    scene, tmp_path: Path
) -> None:
    origin = Path(scene["origin"])
    checkout = Path(scene["checkout"])
    seed = Path(scene["seed"])
    (checkout / "edit.txt").write_text("agent intent\n")

    rejected_once = tmp_path / "rejected-once"
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'if [ ! -e "{rejected_once}" ]; then\n'
        f'  : > "{rejected_once}"\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    hook.chmod(0o755)
    concurrent_head = ""

    def create_concurrent_branch(_delay: float) -> None:
        nonlocal concurrent_head
        _git(seed, "checkout", "-qb", "feature")
        (seed / "concurrent.txt").write_text("must survive\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "concurrent branch")
        _git(seed, "push", "-q", "origin", "feature")
        concurrent_head = _remote_head(origin, "feature")

    store = SqliteRunStore(str(tmp_path / "retry.db"))
    with pytest.raises(PublicationError, match="2 attempts"):
        dispatch_core._git_push_branch(
            "feature",
            checkout,
            repo=str(origin),
            base_sha=str(scene["base"]),
            store=store,
            run_id="run-retry",
            item_id="retry",
            max_attempts=2,
            delays=(0,),
            _sleep=create_concurrent_branch,
        )

    assert concurrent_head
    assert _remote_head(origin, "feature") == concurrent_head
    assert _git(origin, "show", f"{concurrent_head}:concurrent.txt") == "must survive"


def test_merge_publication_has_two_parents(scene, tmp_path: Path) -> None:
    origin, seed = Path(scene["origin"]), Path(scene["seed"])
    _git(seed, "checkout", "-qb", "feature")
    (seed / "feature.txt").write_text("feature\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "feature")
    _git(seed, "push", "-q", "origin", "feature")
    feature = _remote_head(origin, "feature")
    _git(seed, "checkout", "-q", "main")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base advance")
    _git(seed, "push", "-q", "origin", "main")
    main = _remote_head(origin, "main")

    checkout = Path(scene["checkout"])
    _git(checkout, "fetch", "-q", "origin", "feature", "main")
    _git(checkout, "checkout", "-q", "-B", "feature", "origin/feature")
    (checkout / "base.txt").write_text("base\n")
    result = _publish(
        scene,
        tmp_path,
        parents=(feature, main),
        base_sha=feature,
        force_with_lease=f"feature:{feature}",
    )

    assert result is not None
    assert result.parent_shas == (feature, main)
    assert _git(origin, "show", "-s", "--format=%P", result.commit_sha) == (
        f"{feature} {main}"
    )


def test_no_op_returns_trusted_evidence_and_pushes_nothing(
    scene, tmp_path: Path
) -> None:
    publication = _publish(scene, tmp_path)
    assert isinstance(publication, CanonicalNoChange)
    assert publication.base_sha == scene["base"]
    assert publication.tree_sha == _git(
        Path(scene["origin"]), "rev-parse", "main^{tree}"
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(scene["origin"]),
            "show-ref",
            "--verify",
            "refs/heads/feature",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_before_push_runs_before_a_new_head_reaches_the_remote(
    scene, tmp_path: Path
) -> None:
    """The #2033 hook runs once there is a head to push, and before the push."""
    origin = Path(scene["origin"])
    (Path(scene["checkout"]) / "edit.txt").write_text("after\n")
    seen: list[str] = []

    def before_push() -> None:
        present = subprocess.run(
            ["git", "-C", str(origin), "show-ref", "--verify", "refs/heads/feature"],
            capture_output=True,
        )
        seen.append("absent" if present.returncode else "present")

    result = _publish(scene, tmp_path, before_push=before_push)
    assert isinstance(result, CanonicalPublication)
    assert seen == ["absent"]
    assert _remote_head(origin, "feature") == result.commit_sha


def test_before_push_never_runs_for_no_change(scene, tmp_path: Path) -> None:
    calls: list[str] = []
    result = _publish(scene, tmp_path, before_push=lambda: calls.append("hook"))
    assert isinstance(result, CanonicalNoChange)
    assert calls == []


def test_a_failing_before_push_pushes_nothing(scene, tmp_path: Path) -> None:
    (Path(scene["checkout"]) / "edit.txt").write_text("after\n")

    def refuse() -> None:
        raise PublicationError("push", "approval could not be cleared")

    with pytest.raises(PublicationError, match="approval could not be cleared"):
        _publish(scene, tmp_path, before_push=refuse)
    missing = subprocess.run(
        [
            "git",
            "-C",
            str(scene["origin"]),
            "show-ref",
            "--verify",
            "refs/heads/feature",
        ],
        capture_output=True,
    )
    assert missing.returncode != 0


def test_cache_only_checkout_returns_sealable_empty_evidence(
    scene, tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    for directory in (
        ".mypy_cache",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        ".import_linter_cache",
    ):
        output = checkout / "nested" / directory / "state.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("generated\n")

    probe = publication_canonical.checkout_no_change_evidence(
        checkout,
        remote=str(scene["origin"]),
        base_sha=str(scene["base"]),
        scratch=tmp_path / "probe",
    )
    assert isinstance(probe, CanonicalNoChange)
    assert probe.artifact.entries == ()

    publication = _publish(scene, tmp_path)

    assert isinstance(publication, CanonicalNoChange)
    assert publication.artifact.entries == ()
    assert publication.artifact.deletions == ()
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )

    with SqliteRunStore(str(tmp_path / "seal.db")) as store:
        receipt = seal_no_change_package(
            store,
            worktree_dir=checkout,
            packages_root=tmp_path / "packages",
            base_sha=str(scene["base"]),
            source_kind="github",
            source_id="1851",
            run_id="cache-only",
            attempt_number=1,
            now=datetime(2026, 9, 4, tzinfo=UTC),
            verified_artifact=publication.artifact,
            verified_tree_sha=publication.tree_sha,
        )
        sealed = PackageStore(tmp_path / "packages").load(receipt.package_digest)
    assert sealed.entries == ()
    assert sealed.deletions == ()


def test_generated_caches_do_not_enter_a_source_publication(
    scene, tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("intended\n")
    cache = checkout / "kew" / ".mypy_cache" / "state.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("generated\n")

    publication = _publish(scene, tmp_path)

    assert publication.changed_paths == ("edit.txt",)
    assert _git(
        Path(scene["origin"]), "show", f"{publication.commit_sha}:edit.txt"
    ) == ("intended")
    missing = subprocess.run(
        [
            "git",
            "-C",
            str(scene["origin"]),
            "cat-file",
            "-e",
            f"{publication.commit_sha}:kew/.mypy_cache/state.json",
        ],
        capture_output=True,
    )
    assert missing.returncode != 0


def test_review_required_holds_with_nothing_pushed(scene, tmp_path: Path) -> None:
    workflow = Path(scene["checkout"]) / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: unsafe\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path)

    assert any(".github/workflows/ci.yml" in flag for flag in raised.value.flags)
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )


def test_a_refused_base_sha_pushes_nothing(scene, tmp_path: Path) -> None:
    with pytest.raises(CanonicalRepositoryError):
        _publish(scene, tmp_path, base_sha="f" * 40, parents=("f" * 40,))
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )


def test_gitfile_outside_the_checkout_is_never_read(scene, tmp_path: Path) -> None:
    checkout = Path(scene["checkout"])
    metadata = checkout.parent / "metadata"
    (checkout / ".git").rename(metadata)
    (checkout / ".git").write_text(f"gitdir: {metadata}\n")
    (checkout / "edit.txt").write_text("gitfile safe\n")

    result = _publish(scene, tmp_path)

    assert result is not None
    assert result.changed_paths == ("edit.txt",)


@pytest.mark.parametrize(
    "redirect_hooks",
    [False, True],
    ids=("checkout-hooks", "redirected-hooks"),
)
def test_checkout_hook_and_fsmonitor_never_run(
    scene, tmp_path: Path, redirect_hooks: bool
) -> None:
    checkout = Path(scene["checkout"])
    pre_push_sentinel = tmp_path / "pre-push-ran"
    pre_commit_sentinel = tmp_path / "pre-commit-ran"
    monitor_sentinel = tmp_path / "monitor-ran"
    hook_dir = checkout / ".git" / "hooks"
    if redirect_hooks:
        hook_dir = checkout / "redirected-hooks"
        hook_dir.mkdir()
        _git(checkout, "config", "core.hooksPath", str(hook_dir))
    for name, sentinel in (
        ("pre-push", pre_push_sentinel),
        ("pre-commit", pre_commit_sentinel),
    ):
        hook = hook_dir / name
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
        hook.chmod(0o755)
    _git(
        checkout,
        "config",
        "core.fsmonitor",
        f"touch '{monitor_sentinel}'",
    )
    (checkout / "edit.txt").write_text("safe\n")

    assert _publish(scene, tmp_path) is not None
    assert not pre_push_sentinel.exists()
    assert not pre_commit_sentinel.exists()
    assert not monitor_sentinel.exists()


def test_checkout_remote_metadata_cannot_redirect_canonical_push(
    scene, tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    expected_origin = Path(scene["origin"])
    attacker = tmp_path / "attacker.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(expected_origin), str(attacker)],
        check=True,
    )
    _git(checkout, "remote", "set-url", "origin", str(attacker))
    _git(checkout, "remote", "set-url", "--push", "origin", str(attacker))
    (checkout / "edit.txt").write_text("publish to operator origin\n")

    with record_git_calls() as calls:
        result = _publish(scene, tmp_path)

    assert result is not None
    expected_head = subprocess.run(
        [
            "git",
            "-C",
            str(expected_origin),
            "rev-parse",
            "refs/heads/feature",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert expected_head.returncode == 0
    assert expected_head.stdout.strip() == result.commit_sha
    assert (
        subprocess.run(
            ["git", "-C", str(attacker), "show-ref", "--verify", "refs/heads/feature"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    assert not [
        call
        for call in calls
        if call.is_git and call.repository().resolves_inside(checkout)
    ]


def test_pushed_tree_matches_regular_file_workspace(scene, tmp_path: Path) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("after\n")
    (checkout / "added.txt").write_text("added\n")
    (checkout / "gone.txt").unlink()
    executable = checkout / "run.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    result = _publish(scene, tmp_path)

    assert result is not None
    assert result.changed_paths == ("added.txt", "edit.txt", "gone.txt", "run.sh")
    tree = _git(Path(scene["origin"]), "ls-tree", "-r", result.commit_sha)
    assert "100755 blob" in next(line for line in tree.splitlines() if "run.sh" in line)
    assert {line.rpartition("\t")[2] for line in tree.splitlines()} == {
        "added.txt",
        "edit.txt",
        "keep.txt",
        "run.sh",
        "www/src/lib/data/repo-stats.json",
    }


def test_symlink_is_rejected_and_never_published(scene, tmp_path: Path) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "link").symlink_to("/etc/passwd")
    with pytest.raises(PublicationRefused, match="mode 120000"):
        _publish(scene, tmp_path)
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )


def test_mid_walk_regular_file_to_symlink_swap_is_refused(
    scene, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(scene["checkout"])
    target = checkout / "edit.txt"
    target.write_text("changed\n")
    real_open = publication_extract.os.open
    swapped = False

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == target.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to("/etc/passwd")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(publication_extract.os, "open", swap_then_open)
    with pytest.raises(ExtractionFailed):
        extract_on_host(
            checkout,
            tmp_path / "artifact",
            {"edit.txt": "100644"},
            lambda _path: b"before\n",
            str(scene["base"]),
        )


def test_mid_walk_directory_swap_cannot_escape_checkout(
    scene, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = Path(scene["checkout"])
    nested = checkout / "nested"
    nested.mkdir()
    (nested / "safe.txt").write_text("safe\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("operator secret\n")
    real_open = publication_extract.os.open
    swapped = False

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == nested.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (nested / "safe.txt").unlink()
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(publication_extract.os, "open", swap_then_open)
    with pytest.raises(ExtractionFailed, match="directory.*nested"):
        _publish(scene, tmp_path)
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )


def test_hardlinked_regular_file_is_refused(scene, tmp_path: Path) -> None:
    checkout = Path(scene["checkout"])
    target = checkout / "edit.txt"
    target.write_text("changed through a shared inode\n")
    os.link(target, tmp_path / "outside-link")

    with pytest.raises(ExtractionFailed, match="multiple links"):
        _publish(scene, tmp_path)
    assert "feature" not in _git(
        Path(scene["origin"]), "branch", "--format=%(refname:short)"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"||||||| base\n",
        b"\xef\xbb\xbf<<<<<<< HEAD\n",
        b"<<<<<<< HEAD\r\nleft\r\n=======\r\nright\r\n>>>>>>> other\r\n",
        b"caf\xe9\n<<< HEAD\nleft\n===\nright\n>>> other\n",
    ],
    ids=("base-marker", "bom", "crlf", "invalid-utf8-short-block"),
)
def test_resolve_scans_unchanged_files_in_the_promoted_tree(
    scene, tmp_path: Path, payload: bytes
) -> None:
    seed = Path(scene["seed"])
    origin = Path(scene["origin"])
    checkout = Path(scene["checkout"])
    (seed / "marker.txt").write_bytes(payload)
    _git(seed, "add", "marker.txt")
    _git(seed, "commit", "-qm", "base with marker residue")
    _git(seed, "push", "-q", "origin", "main")
    base = _remote_head(origin, "main")
    _git(checkout, "fetch", "-q", "origin", "main")
    _git(checkout, "reset", "-q", "--hard", base)
    (checkout / "edit.txt").write_text("resolved change\n")

    with pytest.raises(CanonicalRepositoryError, match="conflict markers"):
        _publish(
            scene,
            tmp_path,
            base_sha=base,
            parents=(base,),
            require_resolved=True,
        )

    assert "feature" not in _git(origin, "branch", "--format=%(refname:short)")


def test_whole_tree_conflict_scan_uses_one_bounded_batch_process(
    scene, tmp_path: Path
) -> None:
    seed = Path(scene["seed"])
    origin = Path(scene["origin"])
    for index in range(100):
        (seed / f"many-{index:03}.txt").write_text(f"safe {index}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "many safe files")
    _git(seed, "push", "-q", "origin", "main")
    head = _remote_head(origin, "main")
    repo = provision(tmp_path / "canonical-scan", source=str(origin), base_sha=head)
    tree = _git(origin, "rev-parse", f"{head}^{{tree}}")

    with record_git_calls() as calls:
        publication_canonical._require_no_conflict_blocks(repo, tree)

    git_calls = [call for call in calls if call.is_git]
    assert len(git_calls) == 2
    assert sum("--batch" in call.argv for call in git_calls) == 1
    assert not any("blob" in call.argv for call in git_calls)
