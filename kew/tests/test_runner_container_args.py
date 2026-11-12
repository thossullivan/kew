"""``AgentRunner.container_args()`` — the sandbox `"$@"` slot, per runner (#1145).

The container entrypoint owns the binary and the prompt handoff (prompt file →
stdin); everything else in the agent's argv is the HOST's to supply, through
this one protocol method. See ``docs/sandbox-entrypoint-contract.md`` element 3.
"""

from __future__ import annotations

import inspect
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.dispatch_core import run_container_and_stream
from kew.inference_transport import (
    TransportResolutionError,
    resolve_inference_transport,
)
from kew.runners.registry import KNOWN_RUNNER_NAMES, get_runner
from kew.tracking import DispatchResult

_PROMPT = "SENTINEL-PROMPT-do-the-work"
# OpenRouter-shaped so hermes (OpenRouter-only) can build a command at all.
_MODEL = "openrouter/vendor/model-x"


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    return config


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(token in it for token in needle)


#: Runners whose ``container_args`` RAISE on a blank model instead of letting
#: the image's own default stand, and why. Both answers are legitimate; what is
#: not legitimate is an argv that silently omits the flag, so each runner here
#: is held to the refusal by
#: :func:`test_a_runner_that_needs_a_model_refuses_rather_than_omitting_it`.
_MODEL_IS_MANDATORY = {
    "hermes": "OpenRouter-only, so there is no default to fall back to",
    "codex": "every Codex launch names a model, host or contained (N10)",
}


def test_run_container_and_stream_requires_a_result() -> None:
    parameter = inspect.signature(run_container_and_stream).parameters["result"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("runner_name", sorted(KNOWN_RUNNER_NAMES))
def test_container_args_invent_nothing_and_leak_no_prompt(
    runner_name: str, tmp_path: Path
) -> None:
    """Every runner's container args are a subsequence of the argv it would
    build on the host, minus the binary and the prompt handoff.

    Subsequence, not equality: a runner may legitimately DROP a host-only flag
    (pi's/lemonade's ``--session-id``, hermes' host-path ``--usage-file``) —
    but it may never invent a flag the host path has never sent, and it may
    never carry the prompt, which travels by mounted file only (#695)."""
    config = _config(tmp_path)
    runner = get_runner(runner_name)

    args = runner.container_args(config=config, model=_MODEL)
    host_argv = runner.build_command(
        prompt=_PROMPT, config=config, env={}, model=_MODEL
    )

    assert all(isinstance(arg, str) for arg in args)
    assert _PROMPT not in args, "the prompt must never reach the container argv"
    assert _is_subsequence(args, host_argv[1:]), (
        f"{runner_name} container args {args!r} are not a subsequence of its "
        f"host argv {host_argv!r}"
    )


@pytest.mark.parametrize("runner_name", sorted(KNOWN_RUNNER_NAMES))
def test_container_args_omit_the_model_flag_when_no_model_is_routed(
    runner_name: str, tmp_path: Path
) -> None:
    """An empty model means "the image's default" — never a host-side guess."""
    config = _config(tmp_path)
    if runner_name in _MODEL_IS_MANDATORY:
        why = _MODEL_IS_MANDATORY[runner_name]
        pytest.skip(f"{runner_name} fails closed without a model: {why}")

    args = get_runner(runner_name).container_args(config=config, model="")

    assert "--model" not in args and "-m" not in args


@pytest.mark.parametrize(("runner_name", "why"), sorted(_MODEL_IS_MANDATORY.items()))
def test_a_runner_that_needs_a_model_refuses_rather_than_omitting_it(
    runner_name: str, why: str, tmp_path: Path
) -> None:
    """The other half of the test above, so the skip is never a silent hole.

    "No model" is a runner-owned choice between two answers: run the image's
    own default, or refuse. These runners refuse, and they must refuse on the
    CONTAINER path too — an argv with no model flag runs the CLI's own default
    while the dispatch bills and attests the routed one.
    """
    with pytest.raises(ValueError):
        get_runner(runner_name).container_args(config=_config(tmp_path), model="")


def test_claude_container_args_are_the_block_the_sandbox_inlined_until_1145(
    tmp_path: Path,
) -> None:
    """Byte-for-byte characterization of the claude route (the only route the
    containment epic dispatches on)."""
    config = _config(tmp_path)
    runner = get_runner("claude-code")

    assert runner.container_args(config=config, model="claude-opus-5") == [
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format",
        "stream-json",
        "--max-budget-usd",
        "25.0",
        "--model",
        "claude-opus-5",
    ]

    config.agent.claude_code.skip_permissions = False
    assert runner.container_args(config=config, model="") == [
        "--verbose",
        "--output-format",
        "stream-json",
        "--max-budget-usd",
        "25.0",
    ]


def test_claude_container_args_ignore_the_config_model_fallback(
    tmp_path: Path,
) -> None:
    """``model`` is the ALREADY-ROUTED effective model the caller resolved; a
    runner re-applying ``config.agent.model`` here would let a stale config
    default override the route (the #761 class, inverted)."""
    config = _config(tmp_path)
    config.agent.model = "claude-sonnet-4-5"

    args = get_runner("claude-code").container_args(config=config, model="")

    assert "--model" not in args


def test_claude_switchyard_container_args_carry_exact_resolved_route(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.inference_transport = InferenceTransportConfig(
        mode="switchyard",
        switchyard=SwitchyardConfig(
            endpoint="http://127.0.0.1:4000",
            route="switchyard/passthrough",
            expected_version="pinned-version",
        ),
    )
    runner = get_runner("claude-code")

    resolved = resolve_inference_transport(config, runner, "claude-sonnet-4-6")
    args = runner.container_args(config=config, model=resolved.model)

    assert args[args.index("--model") + 1] == "switchyard/passthrough"


def test_opencode_container_args_differ_in_shape_from_claude(tmp_path: Path) -> None:
    """The abstraction is real: opencode's slot is a subcommand plus its own
    flags, not claude's."""
    config = _config(tmp_path)
    config.agent.opencode.agent = "build"
    config.agent.opencode.extra_args = ["--no-color"]

    assert get_runner("opencode").container_args(config=config, model=_MODEL) == [
        "run",
        "--format",
        "json",
        "-m",
        _MODEL,
        "--agent",
        "build",
        "--no-color",
    ]


def test_opencode_switchyard_refusal_does_not_change_direct_container_args(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runner = get_runner("opencode")

    assert runner.transport_capability.incoming_api_formats == ()
    assert runner.container_args(config=config, model=_MODEL) == [
        "run",
        "--format",
        "json",
        "-m",
        _MODEL,
    ]


def test_codex_container_args_carry_governed_fast_tier(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agent.codex.reasoning_effort = "xhigh"
    config.agent.codex.service_tier = "fast"

    args = get_runner("codex").container_args(config=config, model="gpt-5.6-luna")

    assert 'service_tier="fast"' in args
    assert "features.fast_mode=true" in args
    assert "model_reasoning_effort=xhigh" in args
    assert "--ignore-user-config" in args


def test_codex_switchyard_container_args_carry_isolated_provider(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.inference_transport.mode = "switchyard"
    config.inference_transport.switchyard.endpoint = "http://127.0.0.1:4000"
    config.inference_transport.switchyard.route = "switchyard/passthrough"
    config.inference_transport.switchyard.expected_version = "pinned-version"

    args = get_runner("codex").container_args(
        config=config, model="switchyard/passthrough"
    )

    assert 'model_provider="kew_switchyard"' in args
    assert 'model_providers.kew_switchyard.base_url="http://127.0.0.1:4000/v1"' in args
    assert 'model_providers.kew_switchyard.wire_api="responses"' in args


def test_codex_container_args_carry_exactly_one_exec(tmp_path: Path) -> None:
    """``exec`` appears ONCE, and after the root flags.

    The duplicate this pins against is invisible on the host path and fatal on
    the contained one: ``codex exec ... exec --json`` is what came out when the
    image contributed an ``exec`` of its own (the deleted reference entrypoint
    ran ``codex exec "$@"``). ``docker/agent-codex.Dockerfile`` now installs
    ``kew-openshell-launch`` and no agent entrypoint, and that launcher adds no
    flags for ``codex`` — so this list IS the subcommand sequence, and the host
    argv's is the same one minus the binary (epic ``openshell-runtime``, N10).
    """
    args = get_runner("codex").container_args(config=_config(tmp_path), model=_MODEL)
    host_argv = get_runner("codex").build_command(
        prompt=_PROMPT, config=_config(tmp_path), env={}, model=_MODEL
    )

    assert args.count("exec") == 1
    assert host_argv.count("exec") == 1
    assert args[args.index("exec") + 1 :] == ["--json", "--ignore-user-config"]
    assert "--ask-for-approval" in args[: args.index("exec")]
    assert args[args.index("--model") + 1] == _MODEL
    assert args.index("--model") < args.index("exec")


def test_codex_keeps_its_own_sandbox_inside_the_openshell_container(
    tmp_path: Path,
) -> None:
    """Nested containment, not a replaced one.

    Codex's ``workspace-write`` seatbelt is applied with Landlock plus a
    seccomp filter on its own process rather than by entering a new user
    namespace, so it is kept inside the OpenShell sandbox: the two boundaries
    compose restrictively, and a Codex that cannot install its filter refuses
    loudly at startup (``"sandbox"`` is a PERMANENT error for this runner)
    rather than running unsandboxed. If the live gateway run ever disproves
    that, this is the test that has to change, and the posture has to say the
    inner sandbox is off — never this list quietly.
    """
    config = _config(tmp_path)
    config.sandbox.runtime = "openshell"

    args = get_runner("codex").container_args(config=config, model=_MODEL)

    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in args


@pytest.mark.parametrize(
    ("runtime", "sandboxed_by_kew"),
    [("docker", False), ("podman", False), ("openshell", True)],
)
def test_the_codex_posture_decides_who_holds_the_boundary(
    runtime: str, sandboxed_by_kew: bool
) -> None:
    """The runner declaration the container path hangs off, read per runtime.

    ``container_args`` only ever reaches a container because the posture
    declined to claim the host path, so the two belong in one test file: a
    posture that said ``"native"`` on every runtime would make every argv
    assertion above unreachable code on the OpenShell lane.
    """
    posture = get_runner("codex").containment_posture(runtime=runtime)

    assert (posture is None) is sandboxed_by_kew


def test_switchyard_container_receives_only_authorized_endpoint_and_credentials(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.inference_transport = InferenceTransportConfig(
        mode="switchyard",
        switchyard=SwitchyardConfig(
            endpoint="http://127.0.0.1:4000",
            route="switchyard/passthrough",
            expected_version="pinned-version",
        ),
    )
    with (
        patch("kew.dispatch_core._launch_container", return_value=object()) as launch,
        patch("kew.dispatch_core._stream_sandbox_log", return_value=0),
    ):
        exit_code = run_container_and_stream(
            config,
            None,
            image="kew-agent:latest",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="sfd-173",
            run_id="switchyard-container",
            model="claude-sonnet-4-6",
            log_file=tmp_path / "container.log",
            run_kind="dispatch",
            result=DispatchResult(
                exit_code=-1,
                issue_num=173,
                log_file=tmp_path / "container.log",
            ),
        )

    assert exit_code == 0
    argv = launch.call_args.args[0]
    from kew.sandbox import CONTROLLED_PROXY_NAMES, TEST_WORKER_ENV_NAMES

    environment = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    # kew blanks the ten proxy variables explicitly (#1933), in a fixed order,
    # so the runtime cannot inject a proxy URL the posture record never named.
    proxy_blanks = [f"{name}=" for name in CONTROLLED_PROXY_NAMES]
    assert [entry for entry in environment if entry in proxy_blanks] == proxy_blanks
    # The guard marker leads (#1691), then exactly the credential surface.
    # The startup nonce and the --cpus-sized test pools (#2887) carry none.
    environment = [
        value
        for value in environment
        if not value.startswith(
            (
                "KEW_AGENT_START_NONCE=",
                *(f"{name}=" for name in TEST_WORKER_ENV_NAMES),
            )
        )
    ]
    assert [entry for entry in environment if entry not in proxy_blanks] == [
        "AGENT_ASSIGNED_WORKTREE=/work",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL=http://127.0.0.1:4000",
    ]


def _late_guard_launch(
    clock,
    handle,
    _argv,
    *,
    admission,
    on_spawn,
    prepare,
    env,
    credential,
    separate_stderr=False,
):
    assert admission is not None
    handle.accept_admission(admission)
    assert credential is None
    assert "OPENROUTER_API_KEY" not in env
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    assert callable(on_spawn)
    assert callable(prepare)
    clock["now"] = 3.0
    return handle


def _late_guard_stream(clock, handle, *_args, **_kwargs):
    clock["now"] = 6.0
    handle._guard(wall_clock_seconds=60)
    clock["now"] = 64.0
    return 137


def _late_guard_absent_probe(clock):
    from kew.sandbox import OOMProbeResult, OOMProbeStatus

    clock["now"] = 66.0
    return OOMProbeResult(OOMProbeStatus.ABSENT)


def test_direct_137_uses_the_late_stream_guards_enforced_window(
    tmp_path: Path,
) -> None:
    from kew.sandbox import ContainerHandle

    clock = {"now": 0.0}
    handle = ContainerHandle(
        MagicMock(),
        "kew-direct-timing",
        now=partial(clock.__getitem__, "now"),
    )
    result = DispatchResult(
        exit_code=-1,
        issue_num=1767,
        log_file=tmp_path / "direct-timing.log",
        # Deliberately unrelated accounting evidence. Classification must read
        # the handle's enforced window instead of this caller-owned duration.
        duration_seconds=600.0,
    )

    with (
        patch(
            "kew.dispatch_sandbox.time.monotonic",
            side_effect=partial(clock.__getitem__, "now"),
        ),
        patch(
            "kew.dispatch_core._launch_container",
            autospec=True,
            side_effect=partial(_late_guard_launch, clock, handle),
        ),
        patch(
            "kew.dispatch_core._stream_sandbox_log",
            side_effect=partial(_late_guard_stream, clock, handle),
        ),
        patch.object(
            handle,
            "note_oom_kill",
            side_effect=partial(_late_guard_absent_probe, clock),
        ),
        patch("kew.dispatch_sandbox.WALL_CLOCK_LIMIT_SECONDS", 60),
    ):
        exit_code = run_container_and_stream(
            _config(tmp_path),
            None,
            image="kew-agent:latest",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="sfd-1767",
            run_id="direct-timing",
            model="claude-sonnet-4-6",
            log_file=result.log_file,
            run_kind="dispatch",
            result=result,
        )

    assert exit_code == 137
    assert result.duration_seconds == 58.0
    assert result.enforced_duration_seconds == 58.0
    assert clock["now"] == 66.0
    assert result.hard_stop == "hard_kill"


def test_switchyard_unsupported_runner_is_refused_before_container_launch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.inference_transport = InferenceTransportConfig(
        mode="switchyard",
        switchyard=SwitchyardConfig(
            endpoint="http://127.0.0.1:4000",
            route="switchyard/passthrough",
            expected_version="pinned-version",
        ),
    )

    with (
        patch("kew.dispatch_core._launch_container") as launch,
        pytest.raises(
            TransportResolutionError,
            match="pi has no proven Switchyard incoming API capability",
        ),
    ):
        run_container_and_stream(
            config,
            None,
            image="kew-pi:latest",
            runner="pi",
            checkout_dir=tmp_path,
            slug="sfd-173",
            run_id="unsupported-container",
            model=_MODEL,
            log_file=tmp_path / "container.log",
            run_kind="dispatch",
            result=DispatchResult(
                exit_code=-1,
                issue_num=173,
                log_file=tmp_path / "container.log",
            ),
        )

    launch.assert_not_called()


def test_get_runner_rejects_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown runner"):
        get_runner("not-a-runner")
