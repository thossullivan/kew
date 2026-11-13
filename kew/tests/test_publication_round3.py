"""Review rounds 3 and 4 of #1440 (S1-R2): regressions, each with real git.

1. A self-publishing review's pushed head is never adopted: the host proves it
   carries the checkout's tree and republishes that tree as one host commit on
   the reviewed head. A head with another tree is refused and the checkout kept.
2. A canonical or host-style CI fix is published by the host even when it did
   not run in a container.
3. A fresh dispatch needs a recorded base and a durable publication intent; a
   remote branch at a head no intent of this item names is refused.
4. The host walker bounds what the checkout adds, files and directories alike,
   and refuses a checkout that changes while it is read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from support.checkout_generations import fixture_claim_evidence

from kew import dispatch_core, publication_extract, worktree
from kew.publication import PublicationError
from kew.publication_canonical import (
    CanonicalNoChange,
    CanonicalPublication,
    host_commit_identity,
    publish_checkout,
    republish_agent_head,
)
from kew.publication_extract import ExtractionFailed, extract_on_host
from kew.run_store import LoopRecord, SqliteRunStore
from kew.tracking import DispatchResult


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603, B607 — fixture setup
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from kew import subprocess_utils

    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def scene(tmp_path: Path) -> Iterator[dict[str, Path | str]]:
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "keep.txt").write_text("keep\n")
    (seed / "edit.txt").write_text("before\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")
    checkout = tmp_path / "agent" / "issue-7"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    _git(checkout, "config", "user.name", "Agent")
    _git(checkout, "config", "user.email", "agent@example.invalid")
    try:
        yield {"origin": origin, "seed": seed, "checkout": checkout, "base": base}
    finally:
        worktree.release_checkout_claims()


def _remote_head(origin: Path, branch: str) -> str:
    return _git(origin, "rev-parse", f"refs/heads/{branch}")


# -- 1. agent-pushed review heads are republished, never adopted --------------


def _push_feature_from(repo: Path, base: str, text: str) -> str:
    _git(repo, "checkout", "-q", "-B", "feature", base)
    (repo / "edit.txt").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    _git(repo, "push", "-q", "-f", "origin", "feature")
    return _git(repo, "rev-parse", "HEAD")


def _republish(scene: dict[str, Path | str], tmp_path: Path, observed: str):
    return republish_agent_head(
        Path(scene["checkout"]),
        remote=str(scene["origin"]),
        branch="feature",
        expected_sha=str(scene["base"]),
        observed_sha=observed,
        message="fix: address review feedback",
        identity=host_commit_identity(),
        output_guard=None,
        scratch=tmp_path / "republish",
    )


def test_an_agent_pushed_head_is_replaced_by_a_host_commit_of_its_tree(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    pushed = _push_feature_from(checkout, base, "agent revised\n")
    published = _republish(scene, tmp_path, pushed)
    head = _remote_head(origin, "feature")
    assert head == published.commit_sha and head != pushed
    assert _git(origin, "rev-parse", f"{head}^") == base
    assert _git(origin, "rev-parse", f"{head}^{{tree}}") == _git(
        origin, "rev-parse", f"{pushed}^{{tree}}"
    )


def test_merged_in_foreign_history_does_not_survive_republication(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    """A head that merges an unrelated parent with the right tree loses it."""
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    seed = Path(scene["seed"])
    _git(seed, "checkout", "-q", "--orphan", "foreign")
    _git(seed, "rm", "-rfq", ".")
    (seed / "smuggled.txt").write_text("foreign history\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "foreign root")
    foreign = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "foreign")
    _git(checkout, "fetch", "-q", "origin", "foreign")
    _git(checkout, "checkout", "-q", "-B", "feature", base)
    (checkout / "edit.txt").write_text("agent revised\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "work")
    _git(checkout, "merge", "-q", "--allow-unrelated-histories", "--no-edit", foreign)
    _git(checkout, "rm", "-q", "smuggled.txt")
    _git(checkout, "commit", "-qm", "drop the smuggled file")
    _git(checkout, "push", "-q", "-f", "origin", "feature")
    pushed = _git(checkout, "rev-parse", "HEAD")
    assert _git(checkout, "merge-base", "--is-ancestor", foreign, pushed) == ""

    published = _republish(scene, tmp_path, pushed)
    head = _remote_head(origin, "feature")
    assert head == published.commit_sha
    reachable = _git(origin, "rev-list", head)
    assert foreign not in reachable.split()
    assert _git(origin, "rev-parse", f"{head}^") == base


def test_a_foreign_head_with_another_tree_is_refused_and_kept(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    _push_feature_from(checkout, base, "agent revised\n")
    seed = Path(scene["seed"])
    _git(seed, "checkout", "-q", "-B", "feature", base)
    (seed / "edit.txt").write_text("someone else\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "foreign")
    _git(seed, "push", "-q", "-f", "origin", "feature")
    foreign = _git(seed, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="does not carry"):
        _republish(scene, tmp_path, foreign)
    assert _remote_head(origin, "feature") == foreign


def test_a_head_behind_an_unpushed_remediation_is_refused(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    pushed = _push_feature_from(checkout, base, "agent revised\n")
    (checkout / "edit.txt").write_text("unpushed remediation\n")
    with pytest.raises(PublicationError, match="does not carry"):
        _republish(scene, tmp_path, pushed)
    assert _remote_head(origin, "feature") == pushed


# -- 3. fresh dispatches need a base and a durable intent ---------------------


def _push(scene, checkout, branch, *, base, store, item_id):
    return dispatch_core._git_push_branch(
        branch,
        checkout,
        repo=str(scene["origin"]),
        base_sha=base,
        store=store,
        run_id=f"run-{item_id}",
        item_id=item_id,
        max_attempts=1,
    )


def test_a_stale_remote_branch_is_refused_and_own_pushes_resume(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    (checkout / "edit.txt").write_text("dispatch work\n")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        seed = Path(scene["seed"])
        _git(seed, "checkout", "-q", "-b", "agent/issue-7", base)
        (seed / "keep.txt").write_text("stale dispatch\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "stale")
        _git(seed, "push", "-q", "origin", "agent/issue-7")
        stale = _git(seed, "rev-parse", "HEAD")

        with pytest.raises(PublicationError, match="stale branch"):
            _push(scene, checkout, "agent/issue-7", base=base, store=store, item_id="7")
        assert _git(seed, "rev-parse", "origin/agent/issue-7") == stale

        first = _push(
            scene, checkout, "agent/issue-8", base=base, store=store, item_id="8"
        )
        assert isinstance(first, CanonicalPublication)
        assert dispatch_core._recorded_publication_intents(
            store, item_id="8", branch="agent/issue-8"
        ) == {first.commit_sha}
        retry = _push(
            scene, checkout, "agent/issue-8", base=base, store=store, item_id="8"
        )
        assert isinstance(retry, CanonicalNoChange)
        assert retry.base_sha == first.commit_sha
        assert not dispatch_core._nothing_to_publish(retry, base)


def test_a_dispatch_without_a_recorded_base_cannot_publish(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("dispatch work\n")
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        pytest.raises(PublicationError, match="no trusted base"),
    ):
        _push(scene, checkout, "agent/issue-9", base="", store=store, item_id="9")
    assert "agent/issue-9" not in _git(Path(scene["origin"]), "branch", "--list")


def test_a_dispatch_without_a_durable_store_cannot_publish(
    scene: dict[str, Path | str],
) -> None:
    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("dispatch work\n")
    with pytest.raises(PublicationError, match="durable"):
        _push(
            scene,
            checkout,
            "agent/issue-10",
            base=str(scene["base"]),
            store=None,
            item_id="10",
        )
    assert "agent/issue-10" not in _git(Path(scene["origin"]), "branch", "--list")


# -- 4. the walker bounds what the checkout adds -------------------------------


def _no_base(path: str) -> bytes:
    raise AssertionError(f"no base blob should be read for {path}")


def test_the_host_walker_refuses_more_added_entries_than_it_may_retain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for index in range(6):
        (checkout / f"empty-{index}").write_bytes(b"")
    monkeypatch.setattr(publication_extract, "MAX_ENTRIES", 4)
    with pytest.raises(ExtractionFailed, match="entries"):
        extract_on_host(checkout, tmp_path / "artifact", {}, _no_base, "a" * 40)


def test_the_walk_total_is_bounded_across_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    for folder in ("a", "b", "c"):
        (checkout / folder).mkdir(parents=True)
        for index in range(2):
            (checkout / folder / f"empty-{index}").write_bytes(b"")
    monkeypatch.setattr(publication_extract, "MAX_ENTRIES", 4)
    with pytest.raises(ExtractionFailed, match="more than 4 entries"):
        extract_on_host(checkout, tmp_path / "artifact", {}, _no_base, "a" * 40)


def test_an_empty_directory_fan_out_is_bounded_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    # No single listing exceeds the bound; the walk total of added directories does.
    for parent in ("a", "b", "c"):
        for child in ("x", "y"):
            (checkout / parent / child).mkdir(parents=True)
    monkeypatch.setattr(publication_extract, "MAX_ENTRIES", 4)
    with pytest.raises(ExtractionFailed, match="entries"):
        extract_on_host(checkout, tmp_path / "artifact", {}, _no_base, "a" * 40)


def test_paths_the_base_already_names_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    base_state = {}
    for index in range(6):
        (checkout / f"tracked-{index}").write_bytes(b"same\n")
        base_state[f"tracked-{index}"] = "100644"
    monkeypatch.setattr(publication_extract, "MAX_ENTRIES", 4)
    artifact = extract_on_host(
        checkout, tmp_path / "artifact", base_state, lambda _p: b"same\n", "a" * 40
    )
    assert not artifact.entries and not artifact.deletions


def test_a_checkout_that_changes_while_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "steady.txt").write_bytes(b"steady\n")
    passes = {"count": 0}
    original = publication_extract._collect_workspace

    def racing(*args, **kwargs):
        passes["count"] += 1
        if passes["count"] == 2:
            (checkout / "late.txt").write_bytes(b"lingering writer\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(publication_extract, "_collect_workspace", racing)
    with pytest.raises(ExtractionFailed, match="still writing"):
        extract_on_host(checkout, tmp_path / "artifact", {}, _no_base, "a" * 40)


# -- 2. native canonical CI fixes are published by the host -------------------


def test_a_native_canonical_ci_fix_is_published_by_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.ci_fix import dispatch_ci_fix
    from kew.config import DispatchConfig, ProjectConfig, TriggersConfig
    from kew.sandbox import SandboxDecision

    config = DispatchConfig(
        project=ProjectConfig(repo="owner/repo"),
        triggers=TriggersConfig(ci_failure_max_retries=2),
    )
    config.logging.dir = str(tmp_path / "logs")
    decision = SandboxDecision(
        sandboxed=False,
        runtime=None,
        posture="native",
        notice="Sandbox: native",
        image=None,
    )
    resolved = SimpleNamespace(
        runner=SimpleNamespace(name="codex", publication_style="agent"),
        model="gpt-5.6-sol",
    )
    prompts: list[dict[str, object]] = []

    def _prompt(**kwargs: object) -> str:
        prompts.append(kwargs)
        return "prompt"

    store = MagicMock()
    store.count_launched_ci_fix_attempts.return_value = 0
    store.count_refused_ci_fix_attempts.return_value = 0
    store.find_run_by_branch.return_value = None
    store.normalized_jobs_for_head.return_value = []
    store.list_runs_by_branch_after.return_value = ([], None)
    meta = {
        "number": 99,
        "headRefName": "agent/issue-42",
        "headRefOid": "b" * 40,
        "baseRefName": "main",
        "body": "Closes #42",
        "title": "Fix widget",
        "labels": [{"name": "agent-pr"}],
    }
    with (
        patch("kew.ci_fix.get_pr_for_run", return_value=meta),
        patch("kew.ci_fix.extract_ci_log", return_value="FAILED test_foo"),
        # Match test_ci_fix's admission seam for this synthetic checkout.
        patch("kew.ci_fix.require_pinned_pr_head", return_value=None),
        patch(
            "kew.ci_fix.checkout_existing_worktree",
            return_value=(tmp_path / "worktree", None),
        ),
        patch("kew.ci_fix.resolve_runner", return_value=resolved),
        patch("kew.sandbox.resolve_effective_sandbox", return_value=decision),
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None),
        patch("kew.ci_fix.build_ci_fix_prompt", side_effect=_prompt),
        patch("kew.ci_fix.update_pr_labels"),
        patch("kew.ci_fix.run_tracked_agent", return_value=MagicMock(exit_code=0)),
        patch("kew.ci_fix.publish_existing_pr_branch") as publish,
        patch("kew.ci_fix.cleanup_worktree"),
        patch("kew.ci_fix._emit_ci_fix_event"),
    ):
        result = dispatch_ci_fix(run_id="123", config=config, store=store)

    assert result == 0
    assert prompts and prompts[0]["host_publication"] is True
    publish.assert_called_once()
    assert publish.call_args.kwargs["publish"] == "canonical"
    assert publish.call_args.kwargs["expected_remote_sha"] == "b" * 40


# -- round 5 ------------------------------------------------------------------


def test_a_foreign_branch_parked_at_the_base_is_still_refused(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    (checkout / "edit.txt").write_text("dispatch work\n")
    seed = Path(scene["seed"])
    _git(seed, "push", "-q", "origin", f"{base}:refs/heads/agent/issue-11")
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        pytest.raises(PublicationError, match="stale branch"),
    ):
        _push(scene, checkout, "agent/issue-11", base=base, store=store, item_id="11")
    assert _remote_head(Path(scene["origin"]), "agent/issue-11") == base


def test_change_then_revert_resets_the_branch_to_the_reviewed_head(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    first = _push_feature_from(checkout, base, "changed\n")
    (checkout / "edit.txt").write_text("before\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "revert")
    _git(checkout, "push", "-q", "-f", "origin", "feature")
    reverted = _git(checkout, "rev-parse", "HEAD")
    published = _republish(scene, tmp_path, reverted)
    assert published.commit_sha == base
    assert _remote_head(origin, "feature") == base
    for stray in (first, reverted):
        assert stray not in _git(origin, "rev-list", "--all").split()


def test_intent_read_back_ignores_another_items_envelope(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class ForgetfulStore:
        def list_audit_events(self, **_kwargs):
            return [
                SimpleNamespace(
                    id=1,
                    item_id="other",
                    payload={"branch": "agent/issue-8", "commit_sha": "c" * 40},
                )
            ]

    assert (
        dispatch_core._recorded_publication_intents(
            ForgetfulStore(),  # type: ignore[arg-type]
            item_id="8",
            branch="agent/issue-8",
        )
        == set()
    )


def test_a_base_file_replaced_by_a_directory_is_a_deletion_plus_additions(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "thing").mkdir(parents=True)
    (checkout / "thing" / "child").write_bytes(b"child\n")
    artifact = extract_on_host(
        checkout,
        tmp_path / "artifact",
        {"thing": "100644"},
        lambda _p: b"old file\n",
        "a" * 40,
    )
    assert [entry.path for entry in artifact.entries] == ["thing/child"]
    assert artifact.deletions == ("thing",)


def test_a_base_directory_replaced_by_a_file_is_a_deletion_plus_an_addition(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "thing").write_bytes(b"now a file\n")
    artifact = extract_on_host(
        checkout,
        tmp_path / "artifact",
        {"thing/child": "100644"},
        lambda _p: b"child\n",
        "a" * 40,
    )
    assert [entry.path for entry in artifact.entries] == ["thing"]
    assert artifact.deletions == ("thing/child",)


def test_a_large_unchanged_base_costs_a_bounded_number_of_git_processes(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    from support.host_git_boundary import record_git_calls

    seed = Path(scene["seed"])
    for index in range(300):
        (seed / f"tracked-{index}.txt").write_text(f"{index}\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "wide base")
    _git(seed, "push", "-q", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")
    checkout = Path(scene["checkout"])
    _git(checkout, "pull", "-q", "origin", "main")
    (checkout / "edit.txt").write_text("one change\n")
    with record_git_calls() as calls:
        published = publish_checkout(
            checkout,
            remote=str(scene["origin"]),
            branch="wide",
            parents=(base,),
            base_sha=base,
            message="one change over a wide base",
            identity=host_commit_identity(),
            force_with_lease="wide:",
            output_guard=None,
            scratch=tmp_path / "scratch-wide",
        )
    assert isinstance(published, CanonicalPublication)
    git_calls = [call for call in calls if call.is_git]
    assert len(git_calls) < 40, len(git_calls)


def test_budget_exhaustion_asks_the_walker_not_the_checkout(
    scene: dict[str, Path | str],
) -> None:
    from support.host_git_boundary import record_git_calls

    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    _git(checkout, "config", "core.fsmonitor", "/bin/false")
    with record_git_calls() as calls:
        clean = dispatch_core.canonical_checkout_has_work(
            checkout, repo=str(scene["origin"]), base_sha=base
        )
    assert clean is False
    (checkout / "edit.txt").write_text("budget ran out mid-edit\n")
    with record_git_calls() as calls:
        dirty = dispatch_core.canonical_checkout_has_work(
            checkout, repo=str(scene["origin"]), base_sha=base
        )
    assert dirty is True
    inside = [c for c in calls if c.is_git and c.repository().resolves_inside(checkout)]
    assert not inside, [c.describe() for c in inside]
    assert dispatch_core.canonical_checkout_has_work(
        checkout, repo=str(scene["origin"]), base_sha=""
    )


# -- round 6 ------------------------------------------------------------------


def test_the_child_env_drops_the_publication_credential_when_the_host_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.orchestrator import _build_child_env

    monkeypatch.setenv("GH_TOKEN", "ghp_sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "github_sentinel")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.https://github.com/.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Basic sentinel")
    monkeypatch.setenv("PATH_FOR_THE_AGENT", "kept")
    kept = _build_child_env(host_publishes=False)
    assert kept["GH_TOKEN"] == "ghp_sentinel"
    stripped = _build_child_env(host_publishes=True)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"):
        assert name not in stripped, name
    assert stripped["PATH_FOR_THE_AGENT"] == "kept"


def test_canonical_mode_builds_every_prompt_as_host_published() -> None:
    from kew.config import DispatchConfig
    from kew.orchestrator import host_publishes_for

    agent_style = SimpleNamespace(name="claude-code", publication_style="agent")
    config = DispatchConfig()
    assert config.publication.publish == "canonical"
    assert dispatch_core._effective_publication_style(config, agent_style) == "host"
    assert host_publishes_for(config, agent_style) is True
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    assert dispatch_core._effective_publication_style(config, agent_style) == "agent"
    assert host_publishes_for(config, agent_style) is False


def test_the_filter_accepts_both_replacement_directions() -> None:
    from kew.publication_filter import ManifestEntry, filter_manifest

    file_to_dir = filter_manifest(
        [ManifestEntry(path="thing/child", mode="100644", sha256="0" * 64)],
        deletions=["thing"],
        existing_modes={"thing": "100644", "keep.txt": "100644"},
        destination_paths=["thing", "keep.txt"],
        read_text=lambda _e: b"child\n",
    )
    assert file_to_dir.rejections == ()
    dir_to_file = filter_manifest(
        [ManifestEntry(path="thing", mode="100644", sha256="0" * 64)],
        deletions=["thing/child"],
        existing_modes={"thing/child": "100644"},
        destination_paths=["thing/child"],
        read_text=lambda _e: b"now a file\n",
    )
    assert dir_to_file.rejections == ()


def test_budget_probe_treats_any_extraction_failure_as_work(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.publication_artifact import ArtifactRejected

    def rejected(*_a, **_k):
        raise ArtifactRejected("manifest deletes more than 20000 paths")

    monkeypatch.setattr(dispatch_core, "checkout_changed_paths", rejected)
    assert dispatch_core.canonical_checkout_has_work(
        Path(scene["checkout"]), repo=str(scene["origin"]), base_sha=str(scene["base"])
    )


def test_a_reset_that_finds_the_reviewed_head_already_in_place_succeeds(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    first = _push_feature_from(checkout, base, "changed\n")
    (checkout / "edit.txt").write_text("before\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "revert")
    _git(checkout, "push", "-q", "-f", "origin", "feature")
    reverted = _git(checkout, "rev-parse", "HEAD")
    # Another writer reset the branch to the reviewed head first.
    _git(
        Path(scene["seed"]), "push", "-q", "-f", "origin", f"{base}:refs/heads/feature"
    )
    published = _republish(scene, tmp_path, reverted)
    assert published.commit_sha == base
    assert published.flags == ("reset-to-reviewed-head",)
    assert _remote_head(origin, "feature") == base
    assert first not in _git(origin, "rev-list", "--all").split()


def test_cache_only_republish_resets_an_empty_agent_head(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    _git(checkout, "checkout", "-q", "-B", "feature", base)
    _git(checkout, "commit", "--allow-empty", "-qm", "empty agent head")
    _git(checkout, "push", "-q", "-f", "origin", "feature")
    observed = _git(checkout, "rev-parse", "HEAD")
    cache = checkout / ".mypy_cache" / "state.json"
    cache.parent.mkdir()
    cache.write_text("generated\n")

    published = _republish(scene, tmp_path, observed)

    assert published.commit_sha == base
    assert published.changed_paths == ()
    assert published.flags == ("reset-to-reviewed-head",)
    assert _remote_head(origin, "feature") == base


def test_a_reset_racing_a_move_elsewhere_is_refused(
    scene: dict[str, Path | str], tmp_path: Path
) -> None:
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    base = str(scene["base"])
    _push_feature_from(checkout, base, "changed\n")
    (checkout / "edit.txt").write_text("before\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "revert")
    _git(checkout, "push", "-q", "-f", "origin", "feature")
    reverted = _git(checkout, "rev-parse", "HEAD")
    seed = Path(scene["seed"])
    _git(seed, "checkout", "-q", "-B", "feature", base)
    (seed / "keep.txt").write_text("someone else\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "elsewhere")
    _git(seed, "push", "-q", "-f", "origin", "feature")
    elsewhere = _git(seed, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="moved"):
        _republish(scene, tmp_path, reverted)
    assert _remote_head(origin, "feature") == elsewhere


def test_base_blobs_above_the_bound_are_not_read_for_digests(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import publication_canonical
    from kew.publication_repository import provision

    monkeypatch.setattr(publication_canonical, "MAX_BLOB_BYTES", 4)
    repo = provision(
        tmp_path / "canonical", source=str(scene["origin"]), base_sha=str(scene["base"])
    )
    objects = repo.base_blob_oids()
    digests = publication_canonical._base_digests(repo, objects)
    # every base blob in the scene is longer than 4 bytes: marked, never read
    assert digests == dict.fromkeys(objects, publication_extract.OVERSIZE_BASE_BLOB)
    monkeypatch.setattr(publication_canonical, "MAX_BLOB_BYTES", 1 << 20)
    digests = publication_canonical._base_digests(repo, objects)
    assert set(digests) == set(objects)


# -- round 7 ------------------------------------------------------------------


def _never(*_a, **_k):
    raise AssertionError("must not be reached")


def _canonical_loop_harness(
    scene: dict[str, Path | str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from kew.config import DispatchConfig
    from kew.loops.delegate import DelegateDriver, _Step
    from kew.loops.verification import GateVerdict
    from kew.runners.registry import ResolvedRoute

    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    origin = Path(scene["origin"])
    config = DispatchConfig()
    config.project.repo = str(origin)
    config.branches.worktree_base = str(tmp_path)
    store = SqliteRunStore(str(tmp_path / "loop-runs.db"))
    loop = LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="#7: canonical loop",
        runner_name="claude-code",
        model="claude-sonnet-4-6",
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
        issue_num=7,
        item_id="7",
        repo=str(origin),
        budget_usd=5.0,
        wall_clock_secs=600,
        max_steps=20,
    )
    store.save_loop(loop)

    class _Runner:
        name = "claude-code"
        loop_style = "native"
        publication_style = "agent"

    route = ResolvedRoute(_Runner(), "claude-sonnet-4-6")
    observed_paths: list[list[str]] = []

    class _Gate:
        def evaluate(self, **kwargs):
            observed_paths.append(kwargs["changed_paths"])
            return GateVerdict(passed=False, evidence="review", sabotage=False)

    event_file = tmp_path / "loop.events.jsonl"
    event_file.write_text("")

    def _tracked(**kwargs):
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "loop.log",
            event_file=event_file,
            run_id=kwargs["run_id"],
        )

    def _prepare(*_a, **kwargs):
        # The loop revalidates the generation recorded at setup at every paid
        # boundary (#1931 round 20); the seam records it as production does.
        binding = kwargs.get("binding")
        if binding is not None:
            binding.checkout_generation = worktree.read_checkout_generation(checkout)
        return checkout

    monkeypatch.setattr("kew.loops.delegate._prepare_worktree", _prepare)
    monkeypatch.setattr(
        "kew.loops.delegate.reuse_or_restore_worktree", lambda **_k: checkout
    )
    monkeypatch.setattr("kew.loops.delegate.build_repo_map", lambda *_a, **_k: "")
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **_k: "prompt")
    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _tracked)
    monkeypatch.setattr(
        "kew.loops.delegate._read_steps",
        lambda _path: [_Step(10, 5, 0.01, 1.0)],
    )
    monkeypatch.setattr(
        "kew.loops.delegate._require_loop_transport_admission", lambda *_a: None
    )
    monkeypatch.setattr(
        "kew.loops.delegate._resolved_runner_preflight_failure", lambda *_a: None
    )
    monkeypatch.setattr(dispatch_core, "_trusted_dispatch_base", lambda _c: base)
    monkeypatch.setattr(
        dispatch_core, "require_publication_credentials", lambda *_a, **_k: None
    )
    monkeypatch.setattr(dispatch_core, "_apply_created_pr_labels", lambda *_a: None)
    opened: list[str] = []

    def _create(*_args, **_kwargs):
        opened.append("pr")
        return "https://example.invalid/pr/7", 7

    monkeypatch.setattr(dispatch_core, "_create_pull_request", _create)
    driver = DelegateDriver(store, config=config, gate=_Gate())
    return driver, loop, route, store, opened, observed_paths, checkout, origin


def test_canonical_loop_publishes_with_base_intent_and_no_checkout_git(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    driver, loop, route, store, opened, paths, checkout, origin = (
        _canonical_loop_harness(scene, tmp_path, monkeypatch)
    )
    (checkout / "edit.txt").write_text("loop work\n")

    with record_git_calls() as calls:
        result = driver.run(loop, route=route)

    assert result.status == "needs_human"
    assert opened == ["pr"]
    assert paths == [["edit.txt"]]
    assert len(store.list_audit_events(event="publication_base", limit=None)) == 1
    assert len(store.list_audit_events(event="publication_intent", limit=None)) == 1
    assert _remote_head(origin, "agent/issue-7")
    assert not [call for call in calls if call.repository().resolves_inside(checkout)]
    pushes = [call for call in calls if call.subcommand == "push"]
    assert any("--force-with-lease=agent/issue-7:" in call.argv for call in pushes)
    store.close()


def test_canonical_loop_no_change_records_evidence_and_pushes_nothing(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    driver, loop, route, store, opened, paths, _checkout, origin = (
        _canonical_loop_harness(scene, tmp_path, monkeypatch)
    )

    with record_git_calls() as calls:
        result = driver.run(loop, route=route)

    assert result.status == "needs_human"
    assert "no changes" in (result.status_reason or "")
    assert opened == []
    assert paths == []
    assert store.list_audit_events(event="publication_intent", limit=None) == []
    assert (
        len(store.list_audit_events(event="checkout_no_change_evidence", limit=None))
        == 1
    )
    assert "agent/issue-7" not in _git(origin, "branch", "--list")
    assert [call for call in calls if call.subcommand == "push"] == []
    store.close()


def test_canonical_loop_resume_guards_the_complete_published_diff(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.loops.verification import ConditionResult, GateEvaluator

    driver, loop, route, store, opened, paths, checkout, _origin = (
        _canonical_loop_harness(scene, tmp_path, monkeypatch)
    )
    monkeypatch.setattr(driver, "_loop_has_pr", lambda *_a: bool(opened))
    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route", lambda *_a, **_k: route
    )

    class _MetCondition:
        def check(self, **_kwargs):
            return ConditionResult(met=True, evidence="met")

    evaluator = GateEvaluator([_MetCondition()], guarded_paths=[])

    class _RecordingGate:
        def evaluate(self, **kwargs):
            paths.append(kwargs["changed_paths"])
            return evaluator.evaluate(**kwargs)

    driver._gate = _RecordingGate()
    guarded = checkout / "tests" / "test_guarded.py"
    guarded.parent.mkdir(exist_ok=True)
    guarded.write_text("def test_guarded():\n    assert True\n")

    first = driver.run(loop, route=route)
    assert first.status == "needs_human"
    (checkout / "edit.txt").write_text("second session work\n")
    second = driver.resume(loop, "continue")

    assert second.status == "needs_human"
    assert "tests/test_guarded.py" in (second.status_reason or "")
    assert paths[0] == ["tests/test_guarded.py"]
    assert set(paths[1]) == {"edit.txt", "tests/test_guarded.py"}
    store.close()


def test_canonical_loop_resume_reuses_base_and_exact_published_head(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    driver, loop, route, store, opened, paths, checkout, origin = (
        _canonical_loop_harness(scene, tmp_path, monkeypatch)
    )
    monkeypatch.setattr(driver, "_loop_has_pr", lambda *_a: bool(opened))
    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route", lambda *_a, **_k: route
    )
    (checkout / "edit.txt").write_text("first loop work\n")

    with record_git_calls() as calls:
        driver.run(loop, route=route)
        first_head = _remote_head(origin, "agent/issue-7")
        (checkout / "edit.txt").write_text("resumed loop work\n")
        driver.resume(loop, "continue")

    second_head = _remote_head(origin, "agent/issue-7")
    assert second_head != first_head
    assert opened == ["pr"]
    assert paths == [["edit.txt"], ["edit.txt"]]
    assert len(store.list_audit_events(event="publication_base", limit=None)) == 1
    assert len(store.list_audit_events(event="publication_intent", limit=None)) == 2
    assert any(
        f"--force-with-lease=agent/issue-7:{first_head}" in call.argv
        for call in calls
        if call.subcommand == "push"
    )
    assert not [call for call in calls if call.repository().resolves_inside(checkout)]
    store.close()


def test_canonical_loop_resume_preserves_binding_when_restoration_changes_generation(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, loop, route, store, opened, paths, checkout, _origin = (
        _canonical_loop_harness(scene, tmp_path, monkeypatch)
    )
    monkeypatch.setattr(driver, "_loop_has_pr", lambda *_a: bool(opened))
    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route", lambda *_a, **_k: route
    )
    (checkout / "edit.txt").write_text("first loop work\n")
    driver.run(loop, route=route)
    first_generation = worktree.read_checkout_generation(checkout)
    before = store.list_audit_events(event="loop_checkout_bound", limit=None)
    bases = store.list_audit_events(event="publication_base", limit=None)

    def _restore(**kwargs):
        worktree.revoke_checkout_generation(checkout)
        restored_generation = worktree.write_checkout_generation(checkout)
        assert restored_generation != first_generation
        kwargs["on_restore"](checkout)
        return checkout

    monkeypatch.setattr("kew.loops.delegate.reuse_or_restore_worktree", _restore)
    (checkout / "edit.txt").write_text("restored loop work\n")

    # The restore now rebinds under the resuming run, so canonical mode
    # refuses on the base it cannot prove rather than on the binding
    # (governed review round 5, 2026-09-21).
    with pytest.raises(
        RuntimeError, match="start a fresh loop with kew dispatch <issue> --loop"
    ):
        driver.resume(loop, "continue")

    assert paths == [["edit.txt"]], "restoration cannot launch under rewritten evidence"
    # The resuming run records its own binding for the directory the restore
    # produced, which is what makes a restore possible at all (governed review
    # round 5, 2026-09-21). Canonical mode then refuses on the base it cannot
    # prove, so no publication base moves and no session runs.
    bound = store.list_audit_events(event="loop_checkout_bound", limit=None)
    assert len(bound) == len(before) + 1
    previous_ids = {event.id for event in before}
    assert previous_ids < {event.id for event in bound}
    added = next(event for event in bound if event.id not in previous_ids)
    assert added.run_id not in {event.run_id for event in before}
    assert added.payload["checkout"] == str(checkout.resolve())
    assert store.list_audit_events(event="publication_base", limit=None) == bases
    assert len(store.list_audit_events(event="publication_intent", limit=None)) == 1
    store.close()


def test_an_oversize_base_blob_is_never_read_back(
    scene: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import publication_canonical
    from kew.publication_repository import CanonicalRepository

    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    # The base's edit.txt (7 bytes) is "oversize" at a 6-byte bound while
    # keep.txt (5 bytes) is not; the agent shrinks edit.txt to one byte, which
    # the walker must publish without asking the canonical repository for the
    # base bytes, and keep.txt must still read as unchanged.
    monkeypatch.setattr(publication_canonical, "MAX_BLOB_BYTES", 6)

    def never(self, oid):  # noqa: ANN001
        raise AssertionError(f"read_blob called for {oid}")

    monkeypatch.setattr(CanonicalRepository, "read_blob", never)
    (checkout / "edit.txt").write_bytes(b"x")
    published = publish_checkout(
        checkout,
        remote=str(scene["origin"]),
        branch="shrink",
        parents=(base,),
        base_sha=base,
        message="shrink a huge file",
        identity=host_commit_identity(),
        force_with_lease="shrink:",
        output_guard=None,
        scratch=tmp_path / "scratch-shrink",
    )
    assert isinstance(published, CanonicalPublication)
    assert published.changed_paths == ("edit.txt",)


# -- round 9 ------------------------------------------------------------------


def _ctx(config, store=None, **extra):
    base = dict(
        config=config,
        issue_num=7,
        issue_title="t",
        item_id="7",
        branch="agent/issue-7",
        store=store,
        run_id="run-7",
        complexity="medium",
        provider="github",
        run_kind="dispatch",
        publication_base_sha="",
        checkout_generation="",
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _save_terminal_implementation(store, run_id: str) -> None:
    """Record the failed implementation whose preserved bytes continue."""
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            run_kind="dispatch",
        )
    )


def _claim_existing_continuation(
    store, checkout: Path, original_run_id: str, continuation_run_id: str
) -> None:
    """Claim one positive continuation through the managed store boundary."""
    assert (
        store.claim_continuation_under_cap(
            "alpha",
            "7",
            original_run_id,
            "agent/issue-7",
            checkout,
            continuation_run_id,
            5,
            claim_evidence=fixture_claim_evidence(
                store,
                "alpha",
                "7",
                original_run_id,
                "agent/issue-7",
                checkout,
                continuation_run_id,
            ),
        )
        == "claimed"
    )
    dispatch_core._validate_continuation_dispatch_identity(
        store=store,
        epic_ref="alpha",
        item_id="7",
        worktree_dir=checkout,
        branch="agent/issue-7",
        run_id=continuation_run_id,
    )


def test_a_persisted_pr_survives_an_empty_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.config import DispatchConfig

    store = SimpleNamespace(
        get=lambda _run_id: SimpleNamespace(pr_url="https://example.invalid/pull/7")
    )
    monkeypatch.setattr(dispatch_core, "_record_no_change", _never)
    finalized: list[int] = []
    monkeypatch.setattr(
        dispatch_core,
        "_mark_success_pr",
        lambda _ctx, _result, number, failed: finalized.append((number, failed)),
    )
    result = SimpleNamespace(run_id="run-7", canonical_no_change=None)
    dispatch_core._record_sandboxed_exit(
        _ctx(DispatchConfig(), store), result, Path("/x")
    )
    assert finalized == [(7, False)]


def test_no_evidence_means_no_judgment_and_no_checkout_git(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    from kew.config import DispatchConfig

    checkout = Path(scene["checkout"])
    _git(checkout, "config", "core.fsmonitor", "/bin/false")
    monkeypatch.setattr(dispatch_core, "_record_no_change", _never)
    store = SimpleNamespace(get=lambda _run_id: SimpleNamespace(pr_url=""))
    result = SimpleNamespace(run_id="run-7", canonical_no_change=None)
    with record_git_calls() as calls:
        dispatch_core._record_sandboxed_exit(
            _ctx(DispatchConfig(), store), result, checkout
        )
        assert dispatch_core._no_change_base_sha(checkout, DispatchConfig()) == ""
    assert not [c for c in calls if c.is_git], [c.describe() for c in calls]


def test_budget_stop_says_where_uncommitted_work_is(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.config import DispatchConfig

    checkout = Path(scene["checkout"])
    (checkout / "edit.txt").write_text("budget ran out mid-edit\n")
    config = DispatchConfig()
    config.project.repo = str(scene["origin"])
    reasons: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(dispatch_core, "_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch_core, "_warn", warnings.append)
    monkeypatch.setattr(dispatch_core, "_print_failure_next_steps", lambda **_k: None)
    monkeypatch.setattr(
        dispatch_core,
        "mark_failed",
        lambda _n, _c, reason, **_k: reasons.append(reason),
    )
    monkeypatch.setattr(dispatch_core, "_get_changed_files", lambda *_a, **_k: [])
    result = SimpleNamespace(
        exit_code=0,
        attempt_number=1,
        estimated_cost_usd=1.0,
        log_file=None,
        run_id="run-7",
        realized_checkout=None,
    )
    ctx = _ctx(config, publication_base_sha=str(scene["base"]))
    dispatch_core._collect_budget_stop(ctx, result, checkout)
    assert reasons == [
        "graceful budget stop (uncommitted WIP preserved in the checkout only)"
    ]
    assert any("ONLY in the preserved checkout" in w for w in warnings)


def test_epic_recovery_never_runs_git_in_the_checkout_under_canonical_mode(
    scene: dict[str, Path | str],
) -> None:
    from support.host_git_boundary import record_git_calls

    from kew.config import DispatchConfig
    from kew.graph.driver import tick

    checkout = Path(scene["checkout"])
    base = str(scene["base"])
    _git(checkout, "checkout", "-q", "-b", "agent/issue-7")
    _git(checkout, "push", "-q", "origin", "agent/issue-7")
    _git(checkout, "config", "core.fsmonitor", "/bin/false")
    config = DispatchConfig()
    config.project.repo = str(scene["origin"])
    with record_git_calls() as calls:
        assert tick._git_worktree_matches(checkout, "agent/issue-7", config=config)
        assert not tick._git_worktree_matches(checkout, "agent/issue-8", config=config)
        assert (
            tick._checkout_holds_work(config, checkout, "agent/issue-7", base_sha=base)
            is False
        )
        (checkout / "edit.txt").write_text("preserved work\n")
        assert tick._checkout_holds_work(config, checkout, "agent/issue-7") is True
        assert tick._checkout_holds_work(config, checkout, "never-pushed") is True
    inside = [c for c in calls if c.is_git and c.repository().resolves_inside(checkout)]
    assert not inside, [c.describe() for c in inside]
    assert base


# -- round 10 -----------------------------------------------------------------


def test_an_existing_agent_checkout_is_moved_aside_never_reused(
    tmp_path: Path,
) -> None:
    from kew.config import DispatchConfig
    from kew.worktree import quarantine_agent_checkout

    checkout = tmp_path / "issue-9"
    (checkout / ".git" / "hooks").mkdir(parents=True)
    (checkout / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\nexit 1\n")
    (checkout / "work.txt").write_text("preserved bytes\n")
    hatch = DispatchConfig()
    hatch.publication.publish = "checkout"
    hatch.sandbox.dangerously_skip_containment = True
    assert quarantine_agent_checkout(checkout, hatch, reason="x") is None
    assert checkout.exists()
    preserved = quarantine_agent_checkout(checkout, DispatchConfig(), reason="x")
    assert preserved is not None and preserved.parent == tmp_path
    assert not checkout.exists()
    assert (preserved / "work.txt").read_text() == "preserved bytes\n"
    assert quarantine_agent_checkout(checkout, DispatchConfig(), reason="x") is None


def test_head_reads_are_bounded_and_never_follow_links(tmp_path: Path) -> None:
    import os

    from kew.graph.driver import tick

    fifo_checkout = tmp_path / "fifo"
    (fifo_checkout / ".git").mkdir(parents=True)
    os.mkfifo(fifo_checkout / ".git" / "HEAD")
    assert tick._recorded_head_branch(fifo_checkout) is None
    linked = tmp_path / "linked"
    linked.mkdir()
    real = tmp_path / "elsewhere" / ".git"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/agent/issue-7\n")
    (linked / ".git").symlink_to(real)
    assert tick._recorded_head_branch(linked) is None
    huge = tmp_path / "huge"
    (huge / ".git").mkdir(parents=True)
    (huge / ".git" / "HEAD").write_bytes(b"ref: refs/heads/" + b"x" * 8192)
    assert tick._recorded_head_branch(huge) is None
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    (plain / ".git" / "HEAD").write_text("ref: refs/heads/agent/issue-7\n")
    assert tick._recorded_head_branch(plain) == "agent/issue-7"


def test_an_indeterminate_budget_probe_reads_as_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.config import DispatchConfig

    def boom(*_a, **_k):
        raise RuntimeError("scratch dir could not be created")

    monkeypatch.setattr(dispatch_core, "canonical_checkout_has_work", boom)
    assert dispatch_core._budget_stop_wip(_ctx(DispatchConfig()), Path("/x")) == (
        False,
        True,
    )


def test_a_pr_carried_on_the_result_survives_a_lost_store_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew.config import DispatchConfig

    monkeypatch.setattr(dispatch_core, "_record_no_change", _never)
    finalized: list[int] = []
    monkeypatch.setattr(
        dispatch_core,
        "_mark_success_pr",
        lambda _ctx, _result, number, _failed: finalized.append(number),
    )
    store = SimpleNamespace(get=lambda _run_id: None)
    result = SimpleNamespace(
        run_id="run-7",
        canonical_no_change=None,
        published_pr_url="https://example.invalid/pull/7",
    )
    dispatch_core._record_sandboxed_exit(
        _ctx(DispatchConfig(), store), result, Path("/x")
    )
    assert finalized == [7]
    # A URL without a number finalizes nothing and records no no-change.
    result.published_pr_url = "https://example.invalid/pulls"
    dispatch_core._record_sandboxed_exit(
        _ctx(DispatchConfig(), store), result, Path("/x")
    )
    assert finalized == [7]


# -- round 11 -----------------------------------------------------------------


def test_a_git_file_in_a_canonical_checkout_is_refused_as_data(
    tmp_path: Path,
) -> None:
    from support.host_git_boundary import record_git_calls

    from kew.config import DispatchConfig
    from kew.worktree import _reuse_existing_worktree

    checkout = tmp_path / "issue-9"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /nowhere/.git/worktrees/issue-9\n")
    (checkout / "work.txt").write_text("live\n")
    with (
        record_git_calls() as calls,
        pytest.raises(RuntimeError, match="runs no git inside it"),
    ):
        _reuse_existing_worktree(checkout, "agent/issue-9", "", config=DispatchConfig())
    assert not [c for c in calls if c.is_git], [c.describe() for c in calls]
    assert (checkout / "work.txt").read_text() == "live\n"
    assert not list(tmp_path.glob("issue-9.preserved-*"))


def _hold_lock(path: Path):
    import fcntl

    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 — the test holds it
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_a_claimed_checkout_is_neither_moved_nor_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    from kew import worktree
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path)
    checkout = tmp_path / "agent" / "issue-9"
    checkout.mkdir(parents=True)
    (checkout / "work.txt").write_text("live\n")
    monkeypatch.setattr(worktree, "quarantine_agent_checkout", _never)
    other = _hold_lock(tmp_path / "agent" / "issue-9.lock")
    try:
        with record_git_calls() as calls:
            with pytest.raises(worktree.CheckoutBusy):
                worktree.setup_worktree(issue_num=9, config=config)
            with pytest.raises(worktree.CheckoutBusy):
                worktree.checkout_existing_worktree(
                    issue_num=9, branch_name="agent/issue-9", config=config
                )
        assert not [c for c in calls if c.is_git], [c.describe() for c in calls]
        assert checkout.exists()
    finally:
        other.close()
    worktree.claim_checkout(checkout)
    worktree.claim_checkout(checkout)  # re-entrant for the owner
    worktree.release_checkout_claim(checkout)
    hatch = DispatchConfig()
    hatch.publication.publish = "checkout"
    hatch.sandbox.dangerously_skip_containment = True
    worktree.claim_checkout(checkout)  # both modes retain the lease
    worktree.require_checkout_claim(checkout)
    worktree.release_checkout_claim(checkout)
    assert not (tmp_path / "agent" / "issue-9.lock").read_text().endswith("x")


def test_a_review_claims_before_it_moves_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import review_checkout, worktree
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path)
    review_dir = review_checkout.review_checkout_path(config, 5)
    review_dir.mkdir(parents=True)
    (review_dir / "work.txt").write_text("live\n")
    monkeypatch.setattr(worktree, "quarantine_agent_checkout", _never)
    other = _hold_lock(review_dir.parent / f"{review_dir.name}.lock")
    try:
        with pytest.raises(review_checkout.ReviewCheckoutError) as busy:
            review_checkout.materialize_review_checkout(
                pr_number=5, branch_name="agent/issue-5", config=config
            )
        assert busy.value.classification == review_checkout.CHECKOUT_BUSY
        assert (review_dir / "work.txt").exists()
    finally:
        other.close()

    def _cannot_move(*_a, **_k):
        raise RuntimeError("could not move the existing checkout aside")

    monkeypatch.setattr(worktree, "quarantine_agent_checkout", _cannot_move)
    with pytest.raises(review_checkout.ReviewCheckoutError) as unusable:
        review_checkout.materialize_review_checkout(
            pr_number=5, branch_name="agent/issue-5", config=config
        )
    assert unusable.value.classification == review_checkout.CHECKOUT_UNUSABLE
    # The claim was released with the refusal.
    _hold_lock(review_dir.parent / f"{review_dir.name}.lock").close()


def test_a_clean_budget_exhaustion_records_a_no_change_candidate(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from support.host_git_boundary import record_git_calls

    from kew.config import DispatchConfig

    checkout = Path(scene["checkout"])
    _git(checkout, "config", "core.fsmonitor", "/bin/false")
    config = DispatchConfig()
    config.project.repo = str(scene["origin"])
    calls_: dict[str, list] = {"completed": [], "failed": [], "no_change": []}
    monkeypatch.setattr(
        dispatch_core, "mark_completed", lambda *a, **k: calls_["completed"].append(a)
    )
    monkeypatch.setattr(dispatch_core, "_auto_unblock_dependents", _never)
    monkeypatch.setattr(
        dispatch_core, "mark_failed", lambda *a, **k: calls_["failed"].append(a)
    )
    monkeypatch.setattr(
        dispatch_core,
        "_record_no_change",
        lambda _ctx, _result, _dir, evidence: calls_["no_change"].append(evidence),
    )
    monkeypatch.setattr(dispatch_core, "_cleanup_dispatch_dir", lambda *a, **k: None)
    ctx = _ctx(config, publication_base_sha=str(scene["base"]))
    result = SimpleNamespace(run_id="run-7", exit_code=0)
    with record_git_calls() as calls:
        outcome = dispatch_core._collect_budget_exceeded(ctx, result, checkout, False)
    inside = [c for c in calls if c.is_git and c.repository().resolves_inside(checkout)]
    assert not inside, [c.describe() for c in inside]
    assert outcome.exit_code == 0
    assert calls_["completed"] == [] and calls_["failed"] == []
    assert len(calls_["no_change"]) == 1
    assert isinstance(calls_["no_change"][0], CanonicalNoChange)

    (checkout / "edit.txt").write_text("budget ran out mid-edit\n")
    outcome = dispatch_core._collect_budget_exceeded(ctx, result, checkout, False)
    assert outcome.exit_code == 1
    assert calls_["completed"] == [] and len(calls_["failed"]) == 1
    assert len(calls_["no_change"]) == 1

    # No recorded base: nothing is proved, nothing closes.
    outcome = dispatch_core._collect_budget_exceeded(
        _ctx(config, publication_base_sha=""), result, checkout, False
    )
    assert outcome.exit_code == 1 and calls_["completed"] == []


# -- round 12 -----------------------------------------------------------------


class _OrderedCheckoutClaim:
    """Record the first acquisition while preserving the real reentrant lease."""

    def __init__(self, order):
        self.order = order
        self.claim = worktree.claim_checkout
        self.seen = set()

    def __call__(self, checkout):
        if checkout not in self.seen:
            self.order.append("claim")
            self.seen.add(checkout)
        self.claim(checkout)


def test_a_continuation_claims_its_checkout_and_publishes_against_its_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path)
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # Full producer identity includes a terminal implementation row.
        _save_terminal_implementation(store, "run-7")
        # The owning run mints the continuation and binds its base to itself.
        owner = _ctx(config, store, publication_base_sha="a" * 40, epic_ref="alpha")
        dispatch_core._record_publication_continuation(
            owner, SimpleNamespace(run_id="run-7"), checkout
        )
        # Another run at the same path never lends its base (nor takes this one).
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-8", branch="agent/issue-7", checkout=checkout
            )
            == ""
        )
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-7", branch="agent/issue-7", checkout=checkout
            )
            == "a" * 40
        )

        order: list[str] = []
        claim = _OrderedCheckoutClaim(order)
        monkeypatch.setattr(worktree, "claim_checkout", claim)
        monkeypatch.setattr(dispatch_core, "claim_checkout", claim)
        monkeypatch.setattr(
            dispatch_core,
            "_resolve_and_apply_sandbox",
            lambda *_a, **_k: SimpleNamespace(
                posture="host",
                runtime=None,
                image=None,
                sandboxed=False,
                egress_verification="unavailable",
            ),
        )
        monkeypatch.setattr(dispatch_core, "_emit_sandbox_resolved", _never_kw)
        monkeypatch.setattr(
            dispatch_core, "_launch_host", lambda *_a, **_k: order.append("launch")
        )
        ctx = _ctx(
            config,
            store,
            worktree_dir=checkout,
            publication_base_sha="",
            checkout_generation="",
            epic_ref="alpha",
            run_id="run-7-continued",
        )
        _claim_existing_continuation(store, checkout, "run-7", "run-7-continued")
        dispatch_core._launch_workspace(
            ctx,
            dispatch_core._LaunchSetup(),
            config=config,
            resolved=SimpleNamespace(runner=SimpleNamespace(name="x"), model="m"),
            runner=SimpleNamespace(name="x"),
            early=dispatch_core._EarlyRunInfo(),
            admit_budget_check=None,
        )
    assert order == ["claim", "launch"]
    assert ctx.publication_base_sha == "a" * 40
    descriptor = ctx.checkout_binding.descriptor
    worktree.release_checkout_claim(checkout)
    with pytest.raises(OSError):
        os.fstat(descriptor)


def _never_kw(*_a, **_k) -> None:
    return None


def test_a_lease_ends_with_the_run_and_the_bytes_stay(tmp_path: Path) -> None:
    from kew import worktree

    one = tmp_path / "agent" / "issue-1"
    two = tmp_path / "agent" / "issue-2"
    for path in (one, two):
        path.mkdir(parents=True)
        (path / "work.txt").write_text("preserved\n")
    worktree.claim_checkout(one)
    worktree.claim_checkout(two)
    with pytest.raises(OSError):
        _hold_lock(tmp_path / "agent" / "issue-1.lock")
    worktree.release_checkout_claims()
    _hold_lock(tmp_path / "agent" / "issue-1.lock").close()
    _hold_lock(tmp_path / "agent" / "issue-2.lock").close()
    assert (one / "work.txt").read_text() == "preserved\n"
    worktree.release_checkout_claims()  # idempotent


def test_epic_resume_refuses_a_checkout_another_run_holds(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from kew.cli.epic import _validate_resume_worktree
    from kew.config import DispatchConfig
    from kew.run_store import ContinuationRecord

    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path)
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    now = datetime.now(UTC)
    record = ContinuationRecord(
        epic_ref="alpha",
        item_id="7",
        original_run_id="run-7",
        branch="agent/issue-7",
        worktree_identity=str(checkout),
        state="awaiting_continuation",
        generation=1,
        continuation_run_id=None,
        created_at=now,
        updated_at=now,
    )
    other = _hold_lock(tmp_path / "agent" / "issue-7.lock")
    try:
        with pytest.raises(ValueError, match="claimed"):
            _validate_resume_worktree(record, config, MagicMock())
    finally:
        other.close()


# -- round 13 -----------------------------------------------------------------


def test_recorded_base_rejects_conflicting_bindings_for_one_identity(
    tmp_path: Path,
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig
    from kew.publication import PublicationError

    config = DispatchConfig()
    real = tmp_path / "real"
    (real / "agent" / "issue-7").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    checkout = real / "agent" / "issue-7"
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        older = _ctx(config, store, publication_base_sha="a" * 40)
        dispatch_core._record_publication_base(older, run_id="run-7", checkout=checkout)
        conflicting = _ctx(config, store, publication_base_sha="b" * 40)
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            dispatch_core._record_publication_base(
                conflicting, run_id="run-7", checkout=alias / "agent" / "issue-7"
            )
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            dispatch_core._record_publication_base(
                older, run_id="run-7", checkout=checkout
            )
        for spelling in (checkout, alias / "agent" / "issue-7", Path(f"{checkout}/")):
            with pytest.raises(PublicationError, match="conflicting publication bases"):
                dispatch_core._recorded_publication_base(
                    store, run_id="run-7", branch="agent/issue-7", checkout=spelling
                )


def test_recorded_base_accepts_identical_bindings_and_path_aliases(
    tmp_path: Path,
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig

    config = DispatchConfig()
    real = tmp_path / "real"
    checkout = real / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for spelling in (checkout, alias / "agent" / "issue-7"):
            dispatch_core._record_publication_base(
                _ctx(config, store, publication_base_sha="a" * 40),
                run_id="run-7",
                checkout=spelling,
            )

        for spelling in (checkout, alias / "agent" / "issue-7", Path(f"{checkout}/")):
            assert (
                dispatch_core._recorded_publication_base(
                    store,
                    run_id="run-7",
                    item_id="7",
                    branch="agent/issue-7",
                    checkout=spelling,
                )
                == "a" * 40
            )


def test_a_fresh_checkout_starts_at_the_trusted_base_even_if_the_remote_moved(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree

    seed = Path(scene["seed"])
    base = str(scene["base"])
    # The remote advances after the base was queried.
    (seed / "later.txt").write_text("upstream moved\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "upstream moved")
    _git(seed, "push", "-q", "origin", "main")
    moved = _git(seed, "rev-parse", "HEAD")
    assert moved != base
    monkeypatch.setattr(worktree, "_repo_root", lambda: seed)
    checkout = seed.parent / "fresh"
    worktree._clone_checkout_on(
        checkout, "agent/issue-7", start_point="main", exact_sha=base
    )
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "agent/issue-7"
    # Without the exact base the branch follows the fetched head, as before.
    worktree._clone_checkout_on(
        seed.parent / "loose", "agent/issue-8", start_point="main"
    )
    assert _git(seed.parent / "loose", "rev-parse", "HEAD") == moved
    # A base the fetch did not bring is a refusal and leaves no half clone.
    with pytest.raises(RuntimeError, match="not reachable"):
        worktree._clone_checkout_on(
            seed.parent / "gone",
            "agent/issue-9",
            start_point="main",
            exact_sha="c" * 40,
        )
    assert not (seed.parent / "gone").exists()


# -- round 14 -----------------------------------------------------------------


def test_a_base_the_remote_rewrote_away_is_refused_even_if_held_locally(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree

    seed = Path(scene["seed"])
    base = str(scene["base"])
    # The remote's main is rewritten to unrelated history; the operator's
    # local clone still holds the old base.
    _git(seed, "checkout", "-q", "--orphan", "rewrite")
    (seed / "keep.txt").write_text("rewritten\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "rewritten history")
    _git(seed, "push", "-q", "--force", "origin", "rewrite:main")
    _git(seed, "checkout", "-q", "main")
    assert _git(seed, "rev-parse", "HEAD") == base
    monkeypatch.setattr(worktree, "_repo_root", lambda: seed)
    with pytest.raises(RuntimeError, match="not reachable"):
        worktree._clone_checkout_on(
            seed.parent / "divergent",
            "agent/issue-7",
            start_point="main",
            exact_sha=base,
        )
    assert not (seed.parent / "divergent").exists()


def test_recorded_base_rejects_a_b_a_conflicts(tmp_path: Path) -> None:
    from kew import worktree
    from kew.config import DispatchConfig
    from kew.publication import PublicationError

    config = DispatchConfig()
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        dispatch_core._record_publication_base(
            _ctx(config, store, publication_base_sha="a" * 40),
            run_id="run-7",
            checkout=checkout,
        )
        for base in ("b" * 40, "a" * 40):
            with pytest.raises(PublicationError, match="conflicting publication bases"):
                dispatch_core._record_publication_base(
                    _ctx(config, store, publication_base_sha=base),
                    run_id="run-7",
                    checkout=checkout,
                )
        with pytest.raises(PublicationError, match="conflicting publication bases"):
            dispatch_core._recorded_publication_base(
                store, run_id="run-7", branch="agent/issue-7", checkout=checkout
            )


def test_a_sandbox_checkout_starts_at_the_trusted_base_too(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.config import DispatchConfig

    seed = Path(scene["seed"])
    base = str(scene["base"])
    (seed / "later.txt").write_text("upstream moved\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "upstream moved")
    _git(seed, "push", "-q", "origin", "main")
    moved = _git(seed, "rev-parse", "HEAD")
    monkeypatch.chdir(seed)
    config = DispatchConfig()
    pinned = dispatch_core._materialize_checkout(
        "7", "agent/issue-7", config, base_sha=base
    )
    try:
        assert _git(pinned, "rev-parse", "HEAD") == base
    finally:
        shutil.rmtree(pinned, ignore_errors=True)
    loose = dispatch_core._materialize_checkout("8", "agent/issue-8", config)
    try:
        assert _git(loose, "rev-parse", "HEAD") == moved
    finally:
        shutil.rmtree(loose, ignore_errors=True)


def test_the_escape_hatch_keeps_following_the_fetched_head() -> None:
    from kew.config import DispatchConfig

    canonical = _ctx(DispatchConfig(), publication_base_sha="a" * 40)
    assert dispatch_core._pinned_base(canonical) == "a" * 40
    hatch = DispatchConfig()
    hatch.publication.publish = "checkout"
    hatch.sandbox.dangerously_skip_containment = True
    assert dispatch_core._pinned_base(_ctx(hatch, publication_base_sha="a" * 40)) == ""


# -- round 15 -----------------------------------------------------------------


def test_an_older_clones_base_is_never_revived_and_no_base_mints_nothing(
    tmp_path: Path,
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig
    from kew.publication import PublicationError

    config = DispatchConfig()
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # Run A's terminal implementation row is part of the producer proof.
        _save_terminal_implementation(store, "run-a")
        # Run A preserved this path with base A and minted a continuation.
        run_a = _ctx(config, store, publication_base_sha="a" * 40, epic_ref="alpha")
        dispatch_core._record_publication_continuation(
            run_a, SimpleNamespace(run_id="run-a"), checkout
        )
        # Run B recreated the path at base B, but its base query came back
        # empty: nothing is minted for it, and nothing of A's is borrowed.
        run_b = _ctx(config, store, publication_base_sha="", epic_ref="alpha")
        with pytest.raises(PublicationError, match="not minted"):
            dispatch_core._record_publication_continuation(
                run_b, SimpleNamespace(run_id="run-b"), checkout
            )
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-b", branch="agent/issue-7", checkout=checkout
            )
            == ""
        )
        # The continuation of run A restores A; a continuation naming no
        # known record restores nothing.
        resumed = _ctx(
            config,
            store,
            worktree_dir=checkout,
            epic_ref="alpha",
            run_id="run-a-continued",
        )
        _claim_existing_continuation(store, checkout, "run-a", "run-a-continued")
        assert dispatch_core._continuation_base(resumed) == "a" * 40
        stranger = _ctx(
            config, store, worktree_dir=tmp_path / "agent" / "issue-9", epic_ref="alpha"
        )
        assert dispatch_core._continuation_base(stranger) == ""
        # The escape hatch minted nothing and needs nothing.
        hatch = DispatchConfig()
        hatch.publication.publish = "checkout"
        hatch.sandbox.dangerously_skip_containment = True
        dispatch_core._record_publication_base(
            _ctx(hatch, store, publication_base_sha=""),
            run_id="run-h",
            checkout=checkout,
        )


def test_a_held_contained_publication_can_be_continued_against_its_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox path pins base A and, when publication is held, mints a
    continuation that carries A (review round 15)."""
    from kew import worktree
    from kew.config import DispatchConfig
    from kew.publication_canonical import PublicationHeld

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # The held bytes belong to a terminal implementation, not a review.
        _save_terminal_implementation(store, "run-held")
        ctx = _ctx(config, store, publication_base_sha="a" * 40, epic_ref="alpha")
        result = SimpleNamespace(
            run_id="run-held", log_file=tmp_path / "held.log", exit_code=0
        )
        monkeypatch.setattr(
            dispatch_core, "_normalized_publication_job", lambda *_a: None
        )
        # The run row is the finished implementation run; only its transition
        # matters here.
        monkeypatch.setattr(store, "transition_run_status", lambda *_a, **_k: True)
        monkeypatch.setattr(dispatch_core, "append_publication_failure", _never_kw)
        monkeypatch.setattr(dispatch_core, "_error", _never_kw)
        dispatch_core._defer_publication(ctx, result, checkout, PublicationHeld(("x",)))
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-held", branch="agent/issue-7", checkout=checkout
            )
            == "a" * 40
        )
        resumed = _ctx(config, store, worktree_dir=checkout, epic_ref="alpha")
        _claim_existing_continuation(store, checkout, "run-held", "run-7")
        assert dispatch_core._continuation_base(resumed) == "a" * 40


# -- round 16 -----------------------------------------------------------------


def test_a_replaced_checkout_at_the_same_path_cannot_be_continued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path)
    checkout = tmp_path / "agent" / "issue-7"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # The positive control starts with one complete implementation producer.
        _save_terminal_implementation(store, "run-a")
        owner = _ctx(config, store, publication_base_sha="a" * 40, epic_ref="alpha")
        dispatch_core._record_publication_continuation(
            owner, SimpleNamespace(run_id="run-a"), checkout
        )
        resumed = _ctx(config, store, worktree_dir=checkout, epic_ref="alpha")
        _claim_existing_continuation(store, checkout, "run-a", "run-7")
        assert dispatch_core._continuation_base(resumed) == "a" * 40
        # A fresh dispatch moves A aside and creates B at the same path.
        preserved = worktree.quarantine_agent_checkout(checkout, config, reason="x")
        assert preserved is not None
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with pytest.raises(PublicationError, match="checkout identity changed"):
            dispatch_core._continuation_base(resumed)
        monkeypatch.setattr(dispatch_core, "claim_checkout", _never_kw)
        with pytest.raises(PublicationError, match="checkout identity changed"):
            dispatch_core._launch_workspace(
                resumed,
                dispatch_core._LaunchSetup(),
                config=config,
                resolved=SimpleNamespace(runner=SimpleNamespace(name="x"), model="m"),
                runner=SimpleNamespace(name="x"),
                early=dispatch_core._EarlyRunInfo(),
                admit_budget_check=None,
            )
        # The preserved original still names its generation, under its new name.
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-a", branch="agent/issue-7", checkout=preserved
            )
            == ""
        ), "the record names the original path, not the sibling"


def test_a_contained_publication_failure_is_deferred_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.config import DispatchConfig
    from kew.dispatch_sandbox import SANDBOX_PUBLICATION_FAILED_CATEGORY
    from kew.publication import PublicationError

    deferred: list[tuple[str, str]] = []

    def _defer(_ctx, _result, _dir, exc):
        assert isinstance(exc, PublicationError)
        deferred.append((exc.step, exc.detail))
        return "outcome"

    monkeypatch.setattr(dispatch_core, "_defer_publication", _defer)
    result = SimpleNamespace(
        error_category=SANDBOX_PUBLICATION_FAILED_CATEGORY,
        subtype="push\nremote rejected the lease",
        exit_code=0,
    )
    outcome = dispatch_core._collect_special_category(
        _ctx(DispatchConfig()), result, tmp_path, True
    )
    assert outcome == "outcome"
    assert deferred == [("push", "remote rejected the lease")]


# -- round 17 -----------------------------------------------------------------


def test_a_checkout_identity_is_minted_retired_and_never_inherited(
    scene: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import worktree
    from kew.config import DispatchConfig

    seed = Path(scene["seed"])
    monkeypatch.setattr(worktree, "_repo_root", lambda: seed)
    checkout = seed.parent / "agent" / "issue-7"
    worktree._clone_checkout_on(checkout, "agent/issue-7", start_point="main")
    first = worktree.read_checkout_generation(checkout)
    assert first
    # The sidecar lives beside the checkout, never inside it.
    assert not (checkout / ".generation").exists()
    assert (checkout.parent / "issue-7.generation").exists()
    # Quarantine retires it; the successor at the same path gets its own.
    worktree.quarantine_agent_checkout(checkout, DispatchConfig(), reason="x")
    assert worktree.read_checkout_generation(checkout) == ""
    worktree._clone_checkout_on(checkout, "agent/issue-7", start_point="main")
    second = worktree.read_checkout_generation(checkout)
    assert second and second != first
    # A sandbox checkout is minted the same way.
    monkeypatch.chdir(seed)
    boxed = dispatch_core._materialize_checkout("8", "agent/issue-8", DispatchConfig())
    try:
        assert worktree.read_checkout_generation(boxed)
    finally:
        shutil.rmtree(boxed, ignore_errors=True)
    # A tampered sidecar refuses instead of binding nothing: since #1931 round
    # 20 a bound token is required and a malformed binding fails closed.
    from kew.continuation_recovery import CheckoutGenerationRefusal

    (checkout.parent / "issue-7.generation").write_text("not a token\n")
    with pytest.raises(CheckoutGenerationRefusal, match="malformed"):
        worktree.read_checkout_generation(checkout)


def test_a_contained_publication_failure_is_parked_before_the_sandbox_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from kew import dispatch_sandbox, worktree
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord, SqliteRunStore

    (tmp_path / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(tmp_path)
    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(
            RunRecord(
                run_id="run-boxed",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="running",
                exit_code=0,
                started_at=now,
            )
        )
        monkeypatch.setattr(
            dispatch_core,
            "_push_and_open_pr",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh pr create failed")),
        )
        finalized: list[tuple[int, str]] = []

        def _finalize(exit_code: int, category: str = "", status=None) -> None:
            finalized.append((exit_code, category, status))
            store.transition_run_status(
                "run-boxed",
                expected_status="running",
                new_status=status or ("success" if exit_code == 0 else "failure"),
                error_category=category,
            )

        result = dispatch_sandbox._publish_completed_sandbox(
            SimpleNamespace(error_category="", subtype="", published_pr_url=""),
            finalize=_finalize,
            checkout_dir=tmp_path,
            branch="agent/issue-7",
            item_id="7",
            title="t",
            config=DispatchConfig(),
            store=store,
            run_id="run-boxed",
            publication_base_sha="a" * 40,
            image_id="img",
            log_file=tmp_path / "log",
            issue_num=7,
            epic_ref="alpha",
        )
        assert result.error_category == "sandbox_push_failed"
        assert result.subtype.startswith("pr_create\n")
        row = store.get("run-boxed")
        assert row is not None and row.status == "awaiting_publication"
        # One write: the row never read as a plain success (review round 18).
        assert finalized == [(0, "publication_unavailable", "awaiting_publication")]
        # The resume handle and its base already exist when the host arrives.
        [handle] = store.list_continuations("alpha")
        assert handle.original_run_id == "run-boxed"
        assert (
            dispatch_core._recorded_publication_base(
                store, run_id="run-boxed", branch="agent/issue-7", checkout=tmp_path
            )
            == "a" * 40
        )
        # The host's deferral finds the row parked and repeats idempotently.
        assert dispatch_core._already_awaiting_publication(store, "run-boxed")
        dispatch_core._mint_publication_continuation(
            store,
            epic_ref="alpha",
            item_id="7",
            branch="agent/issue-7",
            issue_num=7,
            repo=None,
            publish="canonical",
            base="a" * 40,
            run_id="run-boxed",
            worktree_dir=tmp_path,
        )
        assert len(store.list_continuations("alpha")) == 1


# -- round 18 -----------------------------------------------------------------


def test_the_identity_is_retired_before_any_removal_or_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as _shutil

    from kew import worktree

    seen: list[str] = []

    def _rmtree(path, *a, **k):
        seen.append(f"rmtree:{worktree.read_checkout_generation(Path(path))}")
        return _real_rmtree(path, *a, **k)

    _real_rmtree = _shutil.rmtree
    monkeypatch.setattr(worktree.shutil, "rmtree", _rmtree)
    monkeypatch.setattr(worktree, "_repo_root_or_none", lambda: None)

    discarded = tmp_path / "agent" / "issue-1"
    discarded.mkdir(parents=True)
    (discarded / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(discarded)
    worktree.discard_checkout(discarded)
    boxed = tmp_path / "agent" / "issue-2"
    boxed.mkdir()
    (boxed / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(boxed)
    worktree.remove_checkout_tree(boxed)
    assert seen == ["rmtree:", "rmtree:"], "the token was gone before each removal"

    renamed = tmp_path / "agent" / "issue-3"
    renamed.mkdir()
    (renamed / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(renamed)
    real_rename = Path.rename

    def _rename(self: Path, target):
        seen.append(f"rename:{worktree.read_checkout_generation(self)}")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _rename)
    worktree._quarantine_checkout(renamed)
    assert seen[-1] == "rename:"

    # A token a crashed removal left beside an absent path does not survive
    # into the clone that takes the path.
    stale = tmp_path / "agent" / "issue-4"
    stale.mkdir()
    (stale / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(stale)
    _real_rmtree(stale)
    assert (tmp_path / "agent" / "issue-4.generation").exists()

    def _boom(*_a, **_k):
        raise RuntimeError("clone refused")

    monkeypatch.setattr(worktree, "clone_local_checkout", _boom)
    monkeypatch.setattr(worktree, "_repo_root", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="clone refused"):
        worktree._clone_checkout_on(stale, "agent/issue-4", start_point="main")
    assert not (tmp_path / "agent" / "issue-4.generation").exists()
