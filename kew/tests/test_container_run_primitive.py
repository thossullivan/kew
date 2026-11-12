"""Guard test for the shared container-run primitive (#1023, Wave 1).

``run_container_and_stream`` (``dispatch_sandbox.py``) is the ONE place the
sandbox argv is built. Both the implementation-dispatch caller
(``_run_sandboxed`` → ``_run_sandboxed_container``) and the reviewer launcher
(``sandbox_reviewer_launcher._launch``) route through it, so the #1017 invariant
— ``argv[0]`` is the canonical BARE ``config.sandbox.runtime`` name, never the
resolved ``shutil.which()`` path — is enforced in a single line exercised by
BOTH callers. A single mutation to that argv build (e.g. passing the resolved
path) fails this test via both paths.

The same harness pins the container **command** (the entrypoint's ``"$@"``
slot, contract element 3). Since #1145 those args are derived from the resolved
runner rather than a hardcoded claude block; the characterization test below
locks the claude-route argv byte-for-byte so that derivation is provably a
refactor, and the opencode case proves the derivation is really per-runner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from support.container_launch import ContainerLaunchCapture
from support.issued_credentials import issued_openrouter as issued_openrouter
from test_liveness_owner import process_table as process_table

import kew.dispatch_core as dc
from kew.config import DispatchConfig, SandboxConfig
from kew.dispatch_core import _run_sandboxed
from kew.graph.driver.review_launcher import sandbox_reviewer_launcher
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.sandbox import DEFAULT_IMAGE_RUNNER

# The claude container command exactly as the host has built it since #695,
# minus the leading `--dangerously-skip-permissions` (config-gated) and with the
# routed model appended. This is the characterization baseline: #1145 derives it
# from ClaudeCodeRunner instead of inlining it, and must not move a byte.
_CLAUDE_ARGS_TODAY = ["--verbose", "--output-format", "stream-json"]
# The run cap, in the host argv's position (#1462), per caller: dispatch carries
# the default ``agent.max_budget_usd`` and the reviewer the default
# ``review.max_review_usd``, which every caller here leaves unset.
_CLAUDE_RUN_CAPS = (["--max-budget-usd", "25.0"], ["--max-budget-usd", "10.0"])


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """The fail-closed image gate (#1550 F1) refuses when no runtime resolves
    the tag; these tests run with detect_runtime_cached forced to None, so
    resolve to a stable fake ID by default."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


class _FakeHandle:
    """Stand-in container handle; the primitive only passes it to the (patched)
    stream loop, so it needs no behavior."""


def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:  # noqa: ANN001
    # Non-zero exit keeps the dispatch caller from reaching the push broker
    # and the reviewer from needing a real sidecar — argv is already captured
    # at launch, which is all these tests assert.
    return 1


def _reviewer_ctx(tmp_path: Path) -> ReviewerLaunchContext:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    return ReviewerLaunchContext(
        item_id="1",
        run_id="rev-run",
        prompt="review the work",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def _container_owner(monkeypatch, process_table):
    owner = 100001
    process_table.members[owner] = (owner, "S", "python3", "kew dispatch")
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)


def _capture_argvs(
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: str = DEFAULT_IMAGE_RUNNER,
    image: str = "",
    model: str = "",
    reviewer: bool = True,
    store: SqliteRunStore | None = None,
    capture: ContainerLaunchCapture | None = None,
) -> list[list[str]]:
    """Run the dispatch caller (and optionally the reviewer) with the launch and
    stream sites faked, returning every argv that reached the launch site.

    Pass *capture* to inspect the rest of what each launch was asked for, not
    just its argv."""
    store = store or SqliteRunStore(":memory:", project_root=tmp_path)
    launch = capture or ContainerLaunchCapture(handle=_FakeHandle())

    # Production reality (#1017): the runtime probe returns the resolved path.
    resolved_path = "/usr/local/bin/docker"
    monkeypatch.setattr(dc, "_detect_sandbox_runtime", lambda _cfg: resolved_path)
    monkeypatch.setattr(
        "kew.dispatch_sandbox._contained_auth_path",
        lambda _runner: "workload-identity",
    )
    import kew.graph.driver.review_launcher as _rl

    monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: resolved_path)

    with (
        patch.object(dc, "_launch_container", side_effect=launch),
        patch.object(dc, "_stream_sandbox_log", side_effect=_fake_stream),
    ):
        # Caller 1 — implementation dispatch.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        _run_sandboxed(
            prompt="do the work",
            checkout_dir=checkout,
            config=config,
            image=image or config.sandbox.image,
            runner=runner,
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            model=model,
            store=store,
        )

        # Caller 2 — the read-only reviewer. These configs leave
        # ``review.runner`` unset, so it takes the claude route; a pinned
        # non-claude reviewer is covered in the wiring tests (#1163).
        if reviewer:
            sandbox_reviewer_launcher(config, store)(_reviewer_ctx(tmp_path))

    assert all(admission.store is store for admission in launch.admissions)
    for env, credential in zip(launch.envs, launch.credentials, strict=True):
        assert env is not None
        assert "OPENROUTER_PROVISIONING_KEY" not in env
        if credential is not None:
            assert credential.secret == env["OPENROUTER_API_KEY"]
            assert env["OPENROUTER_API_KEY"] == "issued-container-secret"
        else:
            assert "OPENROUTER_API_KEY" not in env
    return launch.argvs


def test_only_the_reviewer_launch_separates_the_containers_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2053: the same launch site, two pipe layouts, decided by the run kind.

    A governed review reads its verdict from the stream it folds, so its client
    must hand over the container's stdout alone; implementation dispatch keeps
    the single merged pipe (the operator view) it always had.
    """
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    launch = ContainerLaunchCapture(handle=_FakeHandle())

    _capture_argvs(config, tmp_path, monkeypatch, capture=launch)

    assert launch.separated_stderr == [False, True], (
        "dispatch keeps one merged pipe; the reviewer launch asks for two"
    )


def _container_command(argv: list[str], image: str) -> list[str]:
    """The `"$@"` slot: everything after the image name in the run argv."""
    return argv[argv.index(image) + 1 :]


def test_shared_primitive_argv0_is_bare_runtime_for_both_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both callers reach the single shared launch site, and its argv[0] is the
    bare runtime name even when the runtime probe returns a resolved PATH."""
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")

    captured = _capture_argvs(config, tmp_path, monkeypatch)

    assert len(captured) == 2, (
        "both callers must reach the shared launch site (dispatch + reviewer)"
    )
    for argv in captured:
        assert argv[0] == config.sandbox.runtime, (
            f"argv[0] must be the bare runtime name, got {argv[0]!r}"
        )
        # Both callers take the image from the runner-derived resolution, so
        # the reviewer can never run in a different image than dispatch (#1144).
        # The launch binds to the RESOLVED immutable ID, never the mutable tag
        # (#1550 review F1; the autouse fixture resolves to the fake sha).
        assert "sha256:" + "f" * 64 in argv, (
            f"the resolved image must reach the argv, got {argv!r}"
        )
        assert "/" not in argv[0], (
            f"argv[0] must be a bare name (no path) to pass the allowlist, "
            f"got {argv[0]!r}"
        )


@pytest.mark.parametrize("skip_permissions", [True, False])
def test_claude_container_command_is_unchanged_for_both_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_permissions: bool
) -> None:
    """Characterization (#1145): deriving the container args from the runner
    reproduces the claude flag block byte-for-byte, on BOTH callers.

    This is the refactor's safety net — the claude route is the only route the
    containment epic itself dispatches on, so any drift here is a live incident,
    not a hypothetical one.
    """
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    config.agent.model = "claude-opus-5"
    config.agent.claude_code.skip_permissions = skip_permissions

    captured = _capture_argvs(config, tmp_path, monkeypatch)

    assert len(captured) == 2
    for argv, run_cap in zip(captured, _CLAUDE_RUN_CAPS, strict=True):
        assert _container_command(argv, "sha256:" + "f" * 64) == [
            *(["--dangerously-skip-permissions"] if skip_permissions else []),
            *_CLAUDE_ARGS_TODAY,
            *run_cap,
            # The ROUTED effective model must reach the container, not just the
            # run row (#761) — for dispatch it is the caller's effective model,
            # for the reviewer `review.model or agent.model`.
            "--model",
            "claude-opus-5",
        ]


def test_claude_container_command_omits_model_when_none_is_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No routed model -> no `--model` flag (the image's own default wins),
    exactly as the inlined block behaved."""
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")

    captured = _capture_argvs(config, tmp_path, monkeypatch)

    assert len(captured) == 2
    for argv, run_cap in zip(captured, _CLAUDE_RUN_CAPS, strict=True):
        command = _container_command(argv, "sha256:" + "f" * 64)
        assert command == [
            "--dangerously-skip-permissions",
            *_CLAUDE_ARGS_TODAY,
            *run_cap,
        ]
        assert "--model" not in command


def test_the_reviewer_container_carries_the_review_cap_not_the_agent_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer's run ceiling is ``review.max_review_usd`` (#1462): one set
    above ``agent.max_budget_usd`` must not self-terminate the reviewer at the
    implementation cap and end it without a verdict."""
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    config.agent.max_budget_usd = 25.0
    config.review.max_review_usd = 250.0

    dispatch_argv, reviewer_argv = _capture_argvs(config, tmp_path, monkeypatch)

    image = "sha256:" + "f" * 64
    for argv, cap in ((dispatch_argv, "25.0"), (reviewer_argv, "250.0")):
        command = _container_command(argv, image)
        assert command[command.index("--max-budget-usd") + 1] == cap
    # The reviewer's cap rides on a copy; the caller's config keeps its own.
    assert config.agent.max_budget_usd == 25.0


def test_prompt_never_reaches_the_container_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt travels by mounted file only (#695) — never as an argument,
    where a host `ps` would leak it. The runner-derived args must not smuggle
    it back in via `build_command`'s prompt handoff."""
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")

    captured = _capture_argvs(config, tmp_path, monkeypatch)

    for argv in captured:
        assert not any("the work" in arg or "review the work" in arg for arg in argv), (
            f"the prompt must never appear in argv, got {argv!r}"
        )
        # `-p`/`--print` is claude's prompt-handoff flag: the IMAGE entrypoint
        # owns it, the host must not pass it.
        assert "-p" not in argv and "--print" not in argv


def test_container_command_is_derived_from_a_non_claude_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issued_openrouter
) -> None:
    """The whole point of #1145: an opencode dispatch gets OPENCODE's flags.

    The hardcoded claude block could not express this — it would have handed an
    opencode image `--dangerously-skip-permissions --output-format stream-json`,
    which opencode does not accept.
    """
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    config.agent.opencode.agent = "build"
    image = "ghcr.io/example/kew-opencode:1"

    captured = _capture_argvs(
        config,
        tmp_path,
        monkeypatch,
        runner="opencode",
        image=image,
        model="openrouter/deepseek/deepseek-v4-flash",
        reviewer=False,
    )

    assert len(captured) == 1
    command = _container_command(captured[0], "sha256:" + "f" * 64)
    assert command == [
        "run",
        "--format",
        "json",
        "-m",
        "openrouter/deepseek/deepseek-v4-flash",
        "--agent",
        "build",
    ]
    assert "--dangerously-skip-permissions" not in command
    mint, revoke = issued_openrouter
    mint.assert_called_once()
    revoke.assert_called_once()


def test_contained_non_openrouter_runner_needs_no_openrouter_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 scopes provisioning to direct routes that consume OpenRouter keys."""
    from unittest.mock import Mock

    from kew import credential_issuer
    from kew.runners.opencode import OpenCodeRunner

    for name in (*credential_issuer.MANAGEMENT_CREDENTIAL_ENVS, "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # This runner uses a different provider; no OpenRouter credential is needed.
    monkeypatch.setattr(OpenCodeRunner, "required_credentials", ("OTHER_API_KEY",))
    monkeypatch.setenv("OTHER_API_KEY", "other-provider-secret")
    mint, revoke = Mock(), Mock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    captured = _capture_argvs(
        config,
        tmp_path,
        monkeypatch,
        runner="opencode",
        image="ghcr.io/example/kew-opencode:1",
        model="other/model",
        reviewer=False,
    )
    assert len(captured) == 1
    assert _container_command(captured[0], "sha256:" + "f" * 64) == [
        "run",
        "--format",
        "json",
        "-m",
        "other/model",
    ]
    assert "OTHER_API_KEY" in captured[0]
    assert "OPENROUTER_API_KEY" not in captured[0]
    mint.assert_not_called()
    revoke.assert_not_called()


def test_unknown_runner_fails_closed_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner name the registry cannot resolve must refuse, not fall back to
    the claude flags — guessing is how #1104 happened. And because the caller
    already wrote the ``pending`` row, the refusal must finalize it rather than
    leave a corpse the driver later reclaims."""
    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    store = SqliteRunStore(":memory:")

    with pytest.raises(ValueError, match="Unknown runner"):
        _capture_argvs(
            config,
            tmp_path,
            monkeypatch,
            runner="not-a-runner",
            image="ghcr.io/example/nope:1",
            reviewer=False,
            store=store,
        )

    # The refusal now happens BEFORE any row is written (boundary and
    # transport resolve pre-admission, #1550 review F1/F3): a dispatch that
    # never launched never existed in the ledger, so there is no orphan
    # failure row to reconcile — the driver reads the empty ledger as
    # dispatch_child_died and fails the node honestly.
    assert store.list_by_issue(1) == []
