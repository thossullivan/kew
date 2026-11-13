"""W1.1 node 2 — make the host/agent git boundary measurable before fixing it.

Acceptance criteria 1 and 2 of
``docs/design/2026-08-06-host-owned-publication-broker.md``:

1. No host git process — read or write — resolves a repository inside an
   agent-writable path.
2. No git process whose repository resolves inside an agent path carries
   ``GH_TOKEN``, ``GIT_CONFIG_*``, or the publication token in its environment.

Canonical publication now makes both criteria release gates. The checkout is
walked as data and every git process resolves in a fresh host-owned repository.

The written inventory is
``docs/audit/2026-08-08-host-git-boundary-inventory.md@4a387c0b`` (git history).

Three guards keep the boundary tests meaningful:

* ``test_flow_is_observable`` runs each flow *without* the marker and asserts it
  did real git work. A fixture regression fails there, loudly.
* ``test_detector_clears_a_host_owned_repository`` proves the detector can say
  "no". This prevents a vacuous pass.
* ``test_no_module_bypasses_the_subprocess_chokepoint`` proves the recorder sees
  everything, since it observes one chokepoint rather than the whole process
  table.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from support.host_git_boundary import (
    GitInvocation,
    record_git_calls,
    resolve_repository,
)

from kew import (
    dispatch_core,
    publication_canonical,
    publication_repository,
    resolve,
    worktree,
)
from kew.config import DispatchConfig, load_config
from kew.publication import PublicationError
from kew.publication_credentials import publication_producer_for_head
from kew.publication_filter import PublicationRefused
from kew.publication_shadow_hook import run_shadow_comparison
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore
from kew.subprocess_utils import preflight_tools, run

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kew"

# Modules permitted to call ``subprocess`` without going through
# ``subprocess_utils``. Anything else appearing here means the recorder below
# has a blind spot and every assertion in this file is weaker than it looks.
CHOKEPOINT_EXEMPT = {
    # The chokepoint itself.
    "subprocess_utils.py",
    # Fixed ps-only identity probes. Store admission cannot import runners.
    # The executable guard prevents this path from launching git or agents.
    "process_groups.py",
    # Fixed runtime inspections and gh issue labels only; cannot launch git.
    # Store recovery must not import runner inspection through subprocess_utils.
    "stop_recovery.py",
    # The lemonade bash tool runs a model-supplied shell command by design; an
    # executable allowlist would defeat the tool rather than secure it. cwd is
    # the agent worktree, so it is inside the container's blast radius, not the
    # host's — but it does not launch git and is not a publication path.
    "runners/lemonade_agent.py",
}

_DIRECT_CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def _git(*args: str, cwd: Path) -> None:
    """Real git, outside the recorder — fixture setup, not code under test."""
    subprocess.run(  # noqa: S603  # nosec B603, B607 — fixture setup
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def agent_checkout(tmp_path: Path):
    """A repository the agent owns, plus an origin the host publishes to.

    ``agent_root`` stands in for everything a dispatched agent can write: the
    checkout, its ``.git``, and anything it plants there. The host must never
    point git at anything under it.
    """
    preflight_tools()
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / "seed"
    _git("clone", str(origin), str(seed), cwd=tmp_path)
    _configure(seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    checkout = agent_root / "checkout"
    _git("clone", str(origin), str(checkout), cwd=agent_root)
    worktree.write_checkout_generation(checkout)
    _configure(checkout)
    _git("checkout", "-b", "feature", cwd=checkout)
    (checkout / "work.txt").write_text("agent work\n")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-m", "agent work", cwd=checkout)
    _git("push", "-u", "origin", "feature", cwd=checkout)

    # Left uncommitted so the commit-then-push flows have something to do.
    (checkout / "work.txt").write_text("agent work, revised\n")

    # A fresh dispatch publishes from a base recorded before the agent ran and
    # binds its commit to the run durably, so the flows get both.
    base = subprocess.run(  # noqa: S603  # nosec B603, B607 — fixture setup
        ["git", "-C", str(origin), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with SqliteRunStore(str(tmp_path / "boundary.db")) as store:
        yield {
            "origin": origin,
            "agent_root": agent_root,
            "checkout": checkout,
            "base": base,
            "store": store,
        }


def _configure(repo: Path) -> None:
    _git("config", "user.email", "test@example.invalid", cwd=repo)
    _git("config", "user.name", "kew test", cwd=repo)


# -- the flows that publish agent work from the host ------------------------
#
# Three independent implementations exist; the broker is meant to replace all
# of them. Each entry drives one for real against ``agent_checkout``.

Flow = Callable[[dict[str, Path]], object]


def _flow_dispatch_push(env: dict[str, Path]) -> object:
    """``dispatch_core._git_push_branch`` — the fleet's publication push."""
    checkout = env["checkout"]
    _git("add", "-A", cwd=checkout)
    _git("commit", "-m", "revision", cwd=checkout)
    return dispatch_core._git_push_branch(
        "agent/issue-1440",
        checkout,
        repo=str(env["origin"]),
        base_sha=str(env["base"]),
        store=env["store"],  # type: ignore[arg-type]
        run_id="run-1440",
        item_id="1440",
        max_attempts=1,
    )


def _flow_dispatch_read(env: dict[str, Path]) -> object:
    """``dispatch_core._get_changed_files`` — the read side, and the RCE path.

    A writer-only inventory misses this entirely, which is precisely why the
    original acceptance criterion said "no host git **write**" and passed.
    """
    return dispatch_core._get_changed_files(
        env["checkout"], "main", repo=str(env["origin"])
    )


def _flow_worktree_publish(env: dict[str, Path]) -> object:
    """``worktree.publish_existing_pr_branch`` — the review/remediation push."""
    return worktree.publish_existing_pr_branch(
        env["checkout"],
        "feature",
        commit_message="review remediation",
        repo=str(env["origin"]),
    )


def _flow_broker_publish(env: dict[str, Path]) -> object:
    """The broker publishes the tree and then opens the PR."""
    config = DispatchConfig()
    config.project.repo = str(env["origin"])
    with (
        mock.patch(
            "kew.dispatch_core._create_pull_request",
            return_value=("https://example.invalid/pr/1", 1),
        ),
        mock.patch("kew.dispatch_core._apply_created_pr_labels"),
    ):
        return dispatch_core._broker_agent_work(
            env["checkout"],
            "agent/issue-1440",
            "1440",
            "canonical publication",
            config,
            store=env["store"],  # type: ignore[arg-type]
            run_id="run-1440",
            base_sha=str(env["base"]),
        )


def _flow_resolve_push(env: dict[str, Path]) -> object:
    """``resolve._push_clean_integration`` — reaches the repo via ``-C``."""
    expected_head = subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "rev-parse", "HEAD"],
        cwd=env["checkout"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git("add", "-A", cwd=env["checkout"])
    _git("commit", "-m", "integration", cwd=env["checkout"])
    return resolve._push_clean_integration(
        worktree_dir=env["checkout"],
        branch_name="feature",
        base_branch="main",
        strategy="merge",
        expected_head_sha=expected_head,
        base_sha=subprocess.run(
            ["git", "-C", str(env["origin"]), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        repo=str(env["origin"]),
    )


def _flow_agent_style_resolve(env: dict[str, Path]) -> object:
    """Uncontained agent-style resolve still uses canonical publication."""
    from kew.remediation_containment import RemediationContainment
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute
    from kew.tracking import DispatchResult

    checkout = env["checkout"]
    origin = env["origin"]
    expected_head = subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_head = subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "-C", str(origin), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = DispatchConfig()
    config.project.repo = str(origin)
    metadata = {
        "headRefName": "feature",
        "headRefOid": expected_head,
        "baseRefName": "main",
        "title": "resolve safely",
        "body": "Closes #1440",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
    }

    def resolve_tree(**_kwargs: object) -> DispatchResult:
        (checkout / "work.txt").write_text("resolved by agent\n")
        return DispatchResult(exit_code=0, issue_num=1440, log_file=checkout / "run")

    with (
        mock.patch("kew.resolve._get_pr_metadata", return_value=metadata),
        mock.patch("kew.resolve._check_github_connectivity", return_value=True),
        mock.patch(
            "kew.resolve.checkout_existing_worktree",
            return_value=(
                checkout,
                worktree.ConflictInfo("merge", ["work.txt"], "main", base_head),
            ),
        ),
        mock.patch(
            "kew.resolve.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-sonnet-4-6"),
        ),
        mock.patch(
            "kew.resolve.resolve_remediation_containment",
            return_value=RemediationContainment(
                posture="uncontained_flow", reason="test"
            ),
        ),
        mock.patch("kew.resolve.run_tracked_agent", side_effect=resolve_tree),
        mock.patch("kew.resolve.cleanup_worktree"),
        mock.patch("kew.resolve.attest_flow_posture"),
    ):
        return resolve.dispatch_resolve(pr_number=1440, config=config)


FLOWS: dict[str, Flow] = {
    "dispatch_core._git_push_branch": _flow_dispatch_push,
    "dispatch_core._get_changed_files": _flow_dispatch_read,
    "dispatch_broker._broker_agent_work": _flow_broker_publish,
    "worktree.publish_existing_pr_branch": _flow_worktree_publish,
    "resolve._push_clean_integration": _flow_resolve_push,
    "resolve.dispatch_resolve.agent_style": _flow_agent_style_resolve,
}


def _run_flow(name: str, env: dict[str, Path]) -> list[GitInvocation]:
    with record_git_calls() as calls:
        FLOWS[name](env)
    return [call for call in calls if call.is_git]


def _violations(calls: list[GitInvocation], agent_root: Path) -> list[GitInvocation]:
    return [call for call in calls if call.repository().resolves_inside(agent_root)]


# -- criterion 1 ------------------------------------------------------------


@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_flow_is_observable(flow: str, agent_checkout: dict[str, Path]) -> None:
    """Each flow really runs git, so the boundary test measures the flow.

    Without this, a fixture that raised on the first line would make every
    boundary test pass without exercising the intended path.
    """
    calls = _run_flow(flow, agent_checkout)
    assert calls, f"{flow} launched no git process — the flow did not run"
    assert any(call.repository().paths() for call in calls), (
        f"{flow} launched no repository-bound git process"
    )


@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_no_host_git_resolves_inside_an_agent_path(
    flow: str, agent_checkout: dict[str, Path]
) -> None:
    """Criterion 1, stated exactly: read or write, no exceptions."""
    calls = _run_flow(flow, agent_checkout)
    offenders = _violations(calls, agent_checkout["agent_root"])
    assert not offenders, "host git resolved inside the agent path:\n" + "\n".join(
        f"  {call.describe()}" for call in offenders
    )


@pytest.mark.parametrize(
    "flow",
    ["dispatch_core._git_push_branch", "dispatch_broker._broker_agent_work"],
    ids=("native-host-worker", "contained-host-broker"),
)
def test_canonical_worker_paths_never_run_checkout_hooks(
    flow: str, agent_checkout: dict[str, Path], tmp_path: Path
) -> None:
    checkout = agent_checkout["checkout"]
    hooks = checkout / "redirected-hooks"
    hooks.mkdir()
    sentinels = []
    for name in ("pre-push", "pre-commit"):
        sentinel = tmp_path / f"{name}-ran"
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
        hook.chmod(0o755)
        sentinels.append(sentinel)
    _git("config", "core.hooksPath", str(hooks), cwd=checkout)

    if flow == "dispatch_core._git_push_branch":
        with record_git_calls() as calls:
            dispatch_core._git_push_branch(
                "agent/issue-hooks",
                checkout,
                repo=str(agent_checkout["origin"]),
                base_sha=str(agent_checkout["base"]),
                store=agent_checkout["store"],  # type: ignore[arg-type]
                run_id="run-hooks",
                item_id="hooks",
                max_attempts=1,
            )
    else:
        calls = _run_flow(flow, agent_checkout)

    assert calls
    assert not [sentinel for sentinel in sentinels if sentinel.exists()]


def test_recovery_identity_matching_never_runs_git_in_agent_checkout(
    agent_checkout: dict[str, Path],
) -> None:
    from kew.graph.driver import tick

    checkout = agent_checkout["checkout"]
    config = DispatchConfig()
    config.branches.worktree_base = str(checkout.parent.parent)
    with SqliteRunStore(str(checkout.parent.parent / "recovery.db")) as store:
        store.save(
            RunRecord(
                run_id="producer",
                issue_num=1440,
                item_id="1440",
                branch="feature",
                status="failure",
                exit_code=1,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        dispatch_core._bind_publication_base(
            store,
            publish="canonical",
            base=str(agent_checkout["base"]),
            run_id="producer",
            issue_num=1440,
            item_id="1440",
            branch="feature",
            checkout=checkout,
        )

        with record_git_calls() as calls:
            run, base, generation = tick._terminal_run_for_preexisting_work(
                store, "1440", "feature", checkout, config
            )

    assert run.run_id == "producer"
    assert base == agent_checkout["base"]
    assert generation == worktree.read_checkout_generation(checkout)
    offenders = [call for call in calls if call.repository().resolves_inside(checkout)]
    assert offenders == []


# -- criterion 2 ------------------------------------------------------------


def test_agent_rooted_git_carries_no_publication_credential(
    agent_checkout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential travels into a process the agent's repo can steer."""

    def credential() -> dict[str, str]:
        return {
            "GH_TOKEN": "ghp_sentinel",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
            "GIT_TERMINAL_PROMPT": "0",
        }

    monkeypatch.setattr(publication_canonical, "publication_git_env", credential)
    monkeypatch.setattr(publication_repository, "publication_git_env", credential)
    monkeypatch.setattr(dispatch_core, "publication_git_env", credential)
    calls = _run_flow("dispatch_core._git_push_branch", agent_checkout)
    leaked = [
        call
        for call in _violations(calls, agent_checkout["agent_root"])
        if any(
            key == "GH_TOKEN" or key.startswith("GIT_CONFIG_")
            for key in call.effective_env
        )
    ]
    assert not leaked, "credential reached agent-rooted git:\n" + "\n".join(
        f"  {call.describe()}" for call in leaked
    )


def test_canonical_noop_attestation_never_runs_checkout_fsmonitor_with_credential(
    agent_checkout: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verified empty artifact reaches attestation without checkout Git."""
    checkout = agent_checkout["checkout"]
    _git("reset", "--hard", "origin/feature", cwd=checkout)
    dispatch_base = subprocess.run(  # noqa: S603  # nosec B603, B607
        ["git", "rev-parse", "origin/feature"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    sentinel = tmp_path / "credential-leak"
    monitor = tmp_path / "monitor.sh"
    monitor.write_text(
        '#!/bin/sh\nprintf \'%s|%s\' "$GH_TOKEN" "$GITHUB_TOKEN" > '
        f"'{sentinel}'\nexit 1\n"
    )
    monitor.chmod(0o755)
    _git("config", "core.fsmonitor", str(monitor), cwd=checkout)

    # The branch pre-exists at the dispatch base; only a recorded intent of
    # this item makes it ours (review round 5).
    dispatch_core._record_publication_intent(
        agent_checkout["store"],  # type: ignore[arg-type]
        run_id="run-noop",
        item_id="1440",
        branch="feature",
        commit_sha=dispatch_base,
        tree_sha="0" * 40,
        parent_sha="0" * 40,
        owner=dispatch_core._publication_owner(
            agent_checkout["store"],
            run_id="run-noop",
            item_id="1440",
            branch="feature",
            checkout=checkout,
            base_sha=dispatch_base,
        ),
    )
    no_changes: list[publication_canonical.CanonicalNoChange] = []
    result = dispatch_core._git_push_branch(
        "feature",
        checkout,
        repo=str(agent_checkout["origin"]),
        base_sha=dispatch_base,
        store=agent_checkout["store"],  # type: ignore[arg-type]
        run_id="run-noop",
        item_id="1440",
        max_attempts=1,
        on_no_change=no_changes.append,
    )
    assert isinstance(result, publication_canonical.CanonicalNoChange)
    assert len(no_changes) == 1

    monkeypatch.setenv("KEW_PROJECT", str(tmp_path / "operator-project"))
    monkeypatch.setenv("GH_TOKEN", "ghp_noop_sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "github_noop_sentinel")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="run-noop",
                issue_num=1440,
                item_id="1440",
                branch="feature",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
        with record_git_calls() as calls:
            judgment = dispatch_core._complete_no_changes(
                DispatchConfig(),
                item_id="1440",
                provider="github",
                store=store,
                run_id="run-noop",
                worktree_dir=checkout,
                canonical_no_change=no_changes[0],
            )

    assert judgment is not None
    assert not sentinel.exists()
    assert not _violations(
        [call for call in calls if call.is_git], agent_checkout["agent_root"]
    )


def test_shipped_canonical_shadow_never_reads_the_checkout(
    agent_checkout: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped canonical-plus-shadow configuration skips legacy Git reads."""
    shipped = Path(__file__).resolve().parents[2] / "kew.toml"
    config = load_config(config_path=shipped)
    assert config.publication.publish == "canonical"
    assert config.publication.shadow is True

    with (
        SqliteRunStore(str(tmp_path / "shadow.db")) as store,
        record_git_calls() as calls,
    ):
        outcome = run_shadow_comparison(
            config,
            checkout_dir=agent_checkout["checkout"],
            slug="review-finding-5",
            image="unused:canonical-shadow",
            store=store,
            run_id="run-finding-5",
            issue_num=1440,
            item_id="1440",
            branch="feature",
            base_branch="main",
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )
        skipped = store.list_audit_events(event="shadow.skipped")

    assert outcome is None
    assert len(skipped) == 1
    assert skipped[0].payload == {
        "schema_version": 1,
        "reason": "canonical_publication",
        "branch": "feature",
    }
    assert "shadow[review-finding-5]: skipped for canonical publication" in (
        capsys.readouterr().err
    )
    assert not _violations(
        [call for call in calls if call.is_git], agent_checkout["agent_root"]
    )


def test_published_tree_matches_regular_files_and_symlink_never_lands(
    agent_checkout: dict[str, Path],
) -> None:
    checkout = agent_checkout["checkout"]
    origin = agent_checkout["origin"]
    (checkout / "work.txt").write_text("revised\n")
    (checkout / "added.txt").write_text("added\n")
    (checkout / "README.md").unlink()
    executable = checkout / "run.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    assert worktree.publish_existing_pr_branch(
        checkout,
        "feature",
        commit_message="regular tree",
        repo=str(origin),
    )
    published = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "refs/heads/feature"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    listing = subprocess.run(
        ["git", "-C", str(origin), "ls-tree", "-r", published],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert {line.rpartition("\t")[2] for line in listing} == {
        "added.txt",
        "run.sh",
        "work.txt",
    }
    assert "100755 blob" in next(line for line in listing if "run.sh" in line)

    (checkout / "unsafe").symlink_to("/etc/passwd")
    with pytest.raises(PublicationRefused):
        worktree.publish_existing_pr_branch(
            checkout,
            "feature",
            commit_message="must hold",
            repo=str(origin),
        )
    current = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "refs/heads/feature"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current == published


# -- the detector's own guardrails ------------------------------------------


def _subprocess_bypasses(tree: ast.Module) -> list[tuple[int, str]]:
    """Find launches that skip the allowlist, following import bindings.

    Matching the literal name ``subprocess`` is not enough: ``import subprocess
    as sp`` and ``from subprocess import run`` both launch processes under a
    name the scan would not recognise, and this scan is what certifies the
    recorder has no blind spots.
    """
    module_aliases: set[str] = set()
    bound_callables: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _DIRECT_CALLS:
                    bound_callables[alias.asname or alias.name] = alias.name

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
            and func.attr in _DIRECT_CALLS
        ):
            found.append((node.lineno, f"{func.value.id}.{func.attr}"))
        elif isinstance(func, ast.Name) and func.id in bound_callables:
            found.append((node.lineno, f"subprocess.{bound_callables[func.id]}"))
    return sorted(found)


def test_detector_clears_a_host_owned_repository(
    agent_checkout: dict[str, Path],
) -> None:
    """The detector must be able to clear a safe host-owned repository."""
    host_repo = agent_checkout["origin"].parent / "seed"
    calls = [GitInvocation(argv=("git", "status"), cwd=host_repo)]
    assert not _violations(calls, agent_checkout["agent_root"])


def test_a_violation_survives_the_worktree_being_removed(tmp_path: Path) -> None:
    """Resolution is snapshotted at launch, not replayed afterwards.

    ``cleanup_worktree`` removes the agent's checkout at the end of a flow.
    Resolving lazily would let that erase the flow's own violations: the
    walk-up finds nothing and every call reports clean.
    """
    preflight_tools()
    agent_root = tmp_path / "agent"
    checkout = agent_root / "checkout"
    checkout.mkdir(parents=True)
    _git("init", "-b", "main", ".", cwd=checkout)

    with record_git_calls() as calls:
        run(["git", "status", "--porcelain"], cwd=checkout, check=False)
    shutil.rmtree(checkout)

    assert _violations([c for c in calls if c.is_git], agent_root)


def test_the_scan_sees_aliased_and_imported_launches() -> None:
    """Import-binding evasions are still bypasses."""
    aliased = ast.parse("import subprocess as sp\nsp.run(['git'])\n")
    imported = ast.parse("from subprocess import run\nrun(['git'])\n")
    renamed = ast.parse("from subprocess import Popen as P\nP(['git'])\n")
    unrelated = ast.parse("import kew\nkew.run(['git'])\n")

    assert _subprocess_bypasses(aliased) == [(2, "sp.run")]
    assert _subprocess_bypasses(imported) == [(2, "subprocess.run")]
    assert _subprocess_bypasses(renamed) == [(2, "subprocess.Popen")]
    assert _subprocess_bypasses(unrelated) == []


def test_an_inherited_environment_is_what_the_child_actually_saw() -> None:
    """``env=None`` is inheritance, not emptiness."""
    inheriting = GitInvocation(argv=("git", "status"), cwd=Path.cwd())
    explicit = GitInvocation(argv=("git", "status"), cwd=Path.cwd(), env={})
    assert inheriting.effective_env is os.environ
    assert explicit.effective_env == {}


@pytest.mark.policy
def test_no_module_bypasses_the_subprocess_chokepoint() -> None:
    """The recorder watches one chokepoint. Prove nothing routes around it.

    ``subprocess_utils`` states the rule in its own docstring; this is the
    assertion that keeps it true. A new entry means the boundary tests above
    have a blind spot — add the module to ``CHOKEPOINT_EXEMPT`` only with a
    reason, and only if it cannot launch git.
    """
    bypasses: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative in CHOKEPOINT_EXEMPT:
            continue
        source = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for lineno, call in _subprocess_bypasses(source):
            bypasses.append(f"{relative}:{lineno} {call}")
    listing = "\n".join(f"  {entry}" for entry in bypasses)
    assert not bypasses, f"subprocess calls outside the chokepoint:\n{listing}"


# -- resolve_repository: the detector is only as good as this ---------------


def _hide_enclosing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep discovery tests independent of the temporary root's ancestors."""
    original_dir, original_file = Path.is_dir, Path.is_file

    def visible(path: Path) -> bool:
        return path.name != ".git" or path.is_relative_to(tmp_path)

    monkeypatch.setattr(Path, "is_dir", lambda p: visible(p) and original_dir(p))
    monkeypatch.setattr(Path, "is_file", lambda p: visible(p) and original_file(p))


class TestResolveRepository:
    """git's discovery rules, which decide where every assertion above lands."""

    @pytest.fixture(autouse=True)
    def _no_inherited_git_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """These cases test argv and discovery, so pin the environment.

        ``resolve_repository`` inherits ``os.environ`` by default — correct for
        modelling a real launch, but it would make these assertions depend on
        the shell that started pytest.
        """
        monkeypatch.delenv("GIT_DIR", raising=False)
        monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    def test_walks_up_to_the_enclosing_repository(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "src" / "deep").mkdir(parents=True)
        (repo / ".git").mkdir()
        resolved = resolve_repository(["git", "status"], repo / "src" / "deep")
        assert resolved.work_tree == repo
        assert resolved.git_dir == repo / ".git"
        assert resolved.discovered

    def test_dash_c_relocates_the_starting_directory(self, tmp_path: Path) -> None:
        """``resolve._push_clean_integration`` reaches its repo this way."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        resolved = resolve_repository(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], elsewhere
        )
        assert resolved.work_tree == repo

    def test_dash_c_is_cumulative_and_may_be_relative(self, tmp_path: Path) -> None:
        repo = tmp_path / "a" / "b"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        resolved = resolve_repository(["git", "-C", "a", "-C", "b", "status"], tmp_path)
        assert resolved.work_tree == repo

    def test_git_dir_option_overrides_discovery(self, tmp_path: Path) -> None:
        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        planted = tmp_path / "agent" / "planted.git"
        planted.mkdir(parents=True)
        resolved = resolve_repository(["git", f"--git-dir={planted}", "status"], outer)
        assert resolved.git_dir == planted
        assert not resolved.discovered

    def test_a_value_option_does_not_swallow_a_later_dash_c(
        self, tmp_path: Path
    ) -> None:
        """``git -c a.b=c -C <agent path>`` must still be seen.

        Stopping at ``a.b=c`` — mistaking an option's value for the subcommand
        — resolves to the process cwd and reports the agent path as untouched.
        A false negative here is invisible; that is the failure mode that
        matters for this detector.
        """
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / ".git").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        argv = ["git", "-c", "protocol.ext.allow=always", "-C", str(agent), "status"]
        resolved = resolve_repository(argv, elsewhere)
        assert resolved.work_tree == agent
        assert resolved.resolves_inside(agent)

    def test_repeated_dash_c_resolves_through_a_symlink(self, tmp_path: Path) -> None:
        """Each ``-C`` hop is canonicalized, not accumulated lexically.

        Walking the *textual* ancestors of ``decoy/link`` finds ``decoy``'s
        repository and clears the call, while git — which chdir's — lands in
        the agent's checkout.
        """
        agent_root = tmp_path / "agent"
        checkout = agent_root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        decoy = tmp_path / "decoy"
        (decoy / ".git").mkdir(parents=True)
        (decoy / "link").symlink_to(checkout)

        resolved = resolve_repository(
            ["git", "-C", "decoy", "-C", "link", "status"], tmp_path
        )
        assert resolved.resolves_inside(agent_root)
        assert not resolved.resolves_inside(decoy)

    def test_an_inherited_git_dir_is_visible(self, tmp_path: Path) -> None:
        """A real launch with no ``env=`` inherits the parent environment."""
        planted = tmp_path / "planted.git"
        planted.mkdir()
        os.environ["GIT_DIR"] = str(planted)
        try:
            resolved = resolve_repository(["git", "status"], tmp_path, None)
        finally:
            del os.environ["GIT_DIR"]
        assert resolved.git_dir == planted

    def test_an_inherited_work_tree_is_visible(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent"
        agent.mkdir()
        os.environ["GIT_WORK_TREE"] = str(agent)
        try:
            resolved = resolve_repository(["git", "status"], tmp_path, None)
        finally:
            del os.environ["GIT_WORK_TREE"]
        assert resolved.resolves_inside(agent)

    def test_an_explicit_empty_environment_inherits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``{}`` means empty; only ``None`` means inherit."""
        _hide_enclosing_git(tmp_path, monkeypatch)
        planted = tmp_path / "planted.git"
        planted.mkdir()
        os.environ["GIT_DIR"] = str(planted)
        try:
            # Model a filesystem root without the enclosing checkout metadata.
            with mock.patch.object(
                Path, "parents", new_callable=mock.PropertyMock, return_value=()
            ):
                resolved = resolve_repository(["git", "status"], tmp_path, {})
        finally:
            del os.environ["GIT_DIR"]
        assert resolved.git_dir is None

    def test_git_dir_environment_is_honoured(self, tmp_path: Path) -> None:
        planted = tmp_path / "agent" / "planted.git"
        planted.mkdir(parents=True)
        resolved = resolve_repository(
            ["git", "status"], tmp_path, {"GIT_DIR": str(planted)}
        )
        assert resolved.git_dir == planted

    def test_argv_beats_the_environment(self, tmp_path: Path) -> None:
        from_argv = tmp_path / "argv.git"
        from_env = tmp_path / "env.git"
        from_argv.mkdir()
        from_env.mkdir()
        resolved = resolve_repository(
            ["git", "--git-dir", str(from_argv), "status"],
            tmp_path,
            {"GIT_DIR": str(from_env)},
        )
        assert resolved.git_dir == from_argv

    def test_gitfile_redirection_is_followed(self, tmp_path: Path) -> None:
        """A linked worktree's ``.git`` is a file pointing at the real gitdir."""
        main = tmp_path / "main"
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        linked = tmp_path / "linked"
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {gitdir}\n")
        resolved = resolve_repository(["git", "status"], linked)
        assert resolved.work_tree == linked
        assert resolved.git_dir == gitdir

    def test_option_parsing_stops_at_the_subcommand(self, tmp_path: Path) -> None:
        """``git log -C`` is a diff flag, not a directory change."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        argv = ["git", "log", "-C", str(tmp_path / "not-a-repo")]
        resolved = resolve_repository(argv, repo)
        assert resolved.work_tree == repo

    def test_no_repository_anywhere_resolves_to_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _hide_enclosing_git(tmp_path, monkeypatch)
        resolved = resolve_repository(["git", "status"], tmp_path)
        assert not resolved.discovered
        assert resolved.paths() == ()

    def test_a_linked_worktree_binds_both_the_tree_and_the_main_gitdir(
        self, tmp_path: Path
    ) -> None:
        """Why ``resolves_inside`` checks both paths, not just the work tree.

        Doors 2-4 are host worktrees sharing the operator's ``.git``: the tree
        is agent-writable and the metadata is the operator's. Either side being
        inside an agent path is a finding.
        """
        agent_root = tmp_path / "agent"
        linked = agent_root / "wt"
        linked.mkdir(parents=True)
        gitdir = tmp_path / "operator" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (linked / ".git").write_text(f"gitdir: {gitdir}\n")
        resolved = resolve_repository(["git", "status"], linked)
        assert resolved.resolves_inside(agent_root)
        assert not resolved.resolves_inside(tmp_path / "operator" / "nope")

    def test_a_symlinked_path_cannot_escape_the_agent_root(
        self, tmp_path: Path
    ) -> None:
        """Resolution is by real path, so a symlink out is still a symlink in."""
        agent_root = tmp_path / "agent"
        real = agent_root / "checkout"
        (real / ".git").mkdir(parents=True)
        link = tmp_path / "innocent"
        link.symlink_to(real)
        resolved = resolve_repository(["git", "status"], link)
        assert resolved.resolves_inside(agent_root)


# -- canonical remediation binds its head to its run -------------------------


def _feature_head(origin: Path) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603, B607 — read-only readback
        ["git", "-C", str(origin), "rev-parse", "refs/heads/feature"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_canonical_remediation_records_a_publication_intent_under_the_run_item(
    agent_checkout: dict[str, Path],
) -> None:
    """The next round's producer lookup binds a head through ``publication_intent``.

    A remediation publishes onto the PR branch through this path; without the
    intent the head was only bindable by the newest-run heuristic, which any
    unattested review on the branch defeats (#1917).
    """
    checkout = agent_checkout["checkout"]
    origin = agent_checkout["origin"]
    store = agent_checkout["store"]
    parent = _feature_head(origin)
    _remediation_run(store, "remediation-1", branch="feature")

    outcome = worktree.publish_existing_pr_branch(
        checkout,
        "feature",
        commit_message="fix: address review feedback on PR #1916",
        expected_remote_sha=parent,
        repo=str(origin),
        store=store,  # type: ignore[arg-type]
        run_id="remediation-1",
        item_id="1916",  # the caller's review item: the PR number, not the tracker id
    )

    published = _feature_head(origin)
    assert getattr(outcome, "commit_sha", None) == published
    events = [
        event
        for event in store.list_audit_events(  # type: ignore[union-attr]
            limit=None, event="publication_intent"
        )
        if event.payload.get("commit_sha") == published
    ]
    assert len(events) == 1
    intent = events[0]
    assert (intent.actor_type, intent.actor_id) == ("policy", "host-publication")
    assert intent.run_id == "remediation-1"
    assert intent.item_id == "SFD-1916", "the intent is filed under the run's item"
    assert intent.payload["branch"] == "feature"
    assert intent.payload["parent_sha"] == parent
    assert intent.payload["tree_sha"] == getattr(outcome, "tree_sha", None)
    # The consumer: the next round binds the new head to this run, not to
    # whatever ran on the branch last.
    assert (
        publication_producer_for_head(store, branch="feature", head_sha=published)  # type: ignore[arg-type]
        == "remediation-1"
    )


def _remediation_run(store: object, run_id: str, *, branch: str, **fields) -> None:
    """A completed review remediation run with valid native credentials."""
    store.save(  # type: ignore[attr-defined]
        RunRecord(
            run_id=run_id,
            issue_num=1916,
            item_id="SFD-1916",
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 13, tzinfo=UTC),
            attempt_number=1,
            runner_name="claude-code",
            model="claude-opus-5",
            **{"run_kind": "review_remediation", **fields},
        )
    )
    store.record_route_profile(  # type: ignore[attr-defined]
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE, auth_path="cli-login"
            ),
        )
    )


def test_canonical_remediation_records_no_intent_when_the_run_names_no_branch(
    agent_checkout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run row with an empty branch cannot pass the next round's branch check.

    Recording an intent for it would turn the legacy fallback into a hard
    ``publication_lineage_ambiguous`` refusal, so the publication proceeds
    unattested and says so (round 2 of the review of this change).
    """
    checkout = agent_checkout["checkout"]
    origin = agent_checkout["origin"]
    store = agent_checkout["store"]
    _remediation_run(store, "unbound", branch="")
    warnings: list[str] = []
    monkeypatch.setattr(worktree, "_warn", warnings.append)
    before = _feature_head(origin)

    outcome = worktree.publish_existing_pr_branch(
        checkout,
        "feature",
        commit_message="fix: unbound remediation",
        expected_remote_sha=before,
        repo=str(origin),
        store=store,  # type: ignore[arg-type]
        run_id="unbound",
        item_id="1916",
    )

    published = _feature_head(origin)
    assert getattr(outcome, "commit_sha", None) == published != before
    assert store.list_audit_events(limit=None, event="publication_intent") == []  # type: ignore[union-attr]
    assert any("run unbound names no branch" in w for w in warnings), warnings
    # Next round: no binding, and no refusal either — the legacy fallback
    # applies exactly as it did before intents were recorded here.
    assert (
        publication_producer_for_head(store, branch="feature", head_sha=published)  # type: ignore[arg-type]
        is None
    )


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"branch": "other"}, "belongs to branch 'other', not 'feature'"),
        ({"branch": "feature", "run_kind": "planning"}, "is a planning run"),
    ],
    ids=["another-branch", "non-publishing-kind"],
)
def test_canonical_remediation_refuses_a_run_that_cannot_own_the_branch(
    agent_checkout: dict[str, Path], fields: dict[str, str], reason: str
) -> None:
    checkout = agent_checkout["checkout"]
    origin = agent_checkout["origin"]
    store = agent_checkout["store"]
    _remediation_run(store, "stranger", **fields)
    before = _feature_head(origin)

    with pytest.raises(PublicationError, match=reason):
        worktree.publish_existing_pr_branch(
            checkout,
            "feature",
            commit_message="fix: from a stranger",
            expected_remote_sha=before,
            repo=str(origin),
            store=store,  # type: ignore[arg-type]
            run_id="stranger",
            item_id="1916",
        )

    assert _feature_head(origin) == before
    assert store.list_audit_events(limit=None, event="publication_intent") == []  # type: ignore[union-attr]


def test_publication_intent_readback_requires_this_runs_own_event(
    agent_checkout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another run's retained intent for the same commit is not ours.

    With this run's append dropped, a commit-membership readback would pass on
    the other run's event and push under evidence naming that run.
    """
    import kew.audit as audit

    store = agent_checkout["store"]
    head = _feature_head(agent_checkout["origin"])
    dispatch_core._record_publication_intent(
        store,  # type: ignore[arg-type]
        run_id="other-run",
        item_id="1916",
        branch="feature",
        commit_sha=head,
        tree_sha="0" * 40,
        parent_sha="0" * 40,
    )

    monkeypatch.setattr(audit, "emit", lambda *_a, **_k: None)
    with pytest.raises(PublicationError, match="not durably recorded"):
        dispatch_core._record_publication_intent(
            store,  # type: ignore[arg-type]
            run_id="this-run",
            item_id="1916",
            branch="feature",
            commit_sha=head,
            tree_sha="0" * 40,
            parent_sha="0" * 40,
        )
    monkeypatch.undo()

    dispatch_core._record_publication_intent(
        store,  # type: ignore[arg-type]
        run_id="this-run",
        item_id="1916",
        branch="feature",
        commit_sha=head,
        tree_sha="0" * 40,
        parent_sha="0" * 40,
    )
    owners = {
        event.run_id
        for event in store.list_audit_events(limit=None, event="publication_intent")  # type: ignore[union-attr]
        if event.payload.get("commit_sha") == head
    }
    assert owners == {"other-run", "this-run"}


def test_canonical_remediation_without_a_store_records_no_intent(
    agent_checkout: dict[str, Path],
) -> None:
    checkout = agent_checkout["checkout"]
    origin = agent_checkout["origin"]
    store = agent_checkout["store"]
    before = len(store.list_audit_events(limit=None, event="publication_intent"))  # type: ignore[union-attr]
    head_before = _feature_head(origin)

    assert worktree.publish_existing_pr_branch(
        checkout, "feature", commit_message="no store", repo=str(origin)
    )

    assert _feature_head(origin) != head_before, "the publication itself happened"
    assert (
        len(store.list_audit_events(limit=None, event="publication_intent")) == before
    )  # type: ignore[union-attr]


@pytest.mark.policy
def test_round8_staleness_has_no_subprocess_bypass() -> None:
    from kew import staleness

    source = ast.parse(Path(staleness.__file__).read_text(encoding="utf-8"))
    assert not list(_subprocess_bypasses(source))


def test_round8_lemonade_shell_filters_management(tmp_path, monkeypatch) -> None:
    import json
    import shlex
    import sys

    from kew.runners.lemonade_agent import _tool_run_bash

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-round8")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    result = _tool_run_bash(
        tmp_path, {"command": shlex.join([sys.executable, "-c", code])}
    )
    environment = json.loads(result.split("\n", 1)[1])
    assert "OPENROUTER_PROVISIONING_KEY" not in environment


def _hide_enclosing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model an isolated filesystem without hiding Git entries inside the test."""
    for method in ("is_dir", "is_file"):
        original = getattr(Path, method)

        def isolated(path, *args, original=original, **kwargs):
            if path.name == ".git" and not path.is_relative_to(tmp_path):
                return False
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, isolated)
