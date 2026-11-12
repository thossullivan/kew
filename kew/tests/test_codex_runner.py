"""Pure tests for the official Codex CLI adapter."""

import json
import os
from pathlib import Path

import pytest

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runners.codex import (
    CODEX_CREDENTIAL_ENV_NAMES,
    CodexProjectConfigError,
    CodexRunner,
    codex_process_env,
    validate_codex_project_tree,
)
from kew.tracking import DispatchResult
from kew.usage import accumulate_usage
from kew.work_dirs import work_root

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _config(*, effort: str = "", service_tier: str = "") -> DispatchConfig:
    config = DispatchConfig()
    config.agent.codex.reasoning_effort = effort
    config.agent.codex.service_tier = service_tier
    return config


def _switchyard_config() -> DispatchConfig:
    return DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route="switchyard/passthrough",
                expected_version="pinned-version",
            ),
        )
    )


def test_codex_identity_and_host_publication():
    runner = CodexRunner()
    assert runner.name == "codex"
    assert runner.loop_style == "native"
    assert runner.publication_style == "host"
    assert runner.containment_posture() == "native"
    assert runner.required_credentials == ()


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (None, "native"),
        ("docker", "native"),
        ("podman", "native"),
        ("openshell", None),
    ],
)
def test_codex_containment_posture_is_per_runtime(
    runtime: str | None, expected: str | None
) -> None:
    """Codex's seatbelt is the boundary everywhere kew has no better one.

    ``None`` is the no-kew-boundary question (a first-class loop's agent, a
    remediation flow's host fallback) and answers ``"native"`` like the docker
    lane, where ``agent-codex`` is OpenShell-only and cannot be launched. Only
    ``openshell`` declines, so ``resolve_effective_sandbox`` stops
    short-circuiting to the host path and contains the dispatch there instead
    (epic ``openshell-runtime``, node N10).
    """
    assert CodexRunner().containment_posture(runtime=runtime) == expected


def test_codex_posture_is_runner_owned_and_not_a_config_value() -> None:
    """A runtime name kew has never heard of does not widen the posture."""
    assert CodexRunner().containment_posture(runtime="openshell-ish") == "native"
    assert CodexRunner().containment_posture(runtime="OPENSHELL") == "native"


def test_codex_switchyard_capability_is_openai_responses():
    assert CodexRunner().transport_capability == RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )


def test_codex_switchyard_command_uses_isolated_provider_and_exact_route():
    command = CodexRunner().build_command(
        prompt="fix it",
        config=_switchyard_config(),
        env={"UNCHANGED": "yes"},
        model="switchyard/passthrough",
    )

    assert command[command.index("--model") + 1] == "switchyard/passthrough"
    for override in (
        'model_provider="kew_switchyard"',
        'model_providers.kew_switchyard.name="Kew Switchyard"',
        'model_providers.kew_switchyard.base_url="http://127.0.0.1:4000/v1"',
        'model_providers.kew_switchyard.wire_api="responses"',
        "model_providers.kew_switchyard.requires_openai_auth=true",
    ):
        assert command[command.index(override) - 1] == "-c"
    assert command[-3:] == ["--json", "--ignore-user-config", "fix it"]


def test_codex_switchyard_provider_is_reapplied_when_resuming():
    command = CodexRunner().build_resume_command(
        session_id="thread-123",
        prompt="ignored",
        config=_switchyard_config(),
        env={},
        model="switchyard/passthrough",
    )

    assert 'model_provider="kew_switchyard"' in command
    assert (
        'model_providers.kew_switchyard.base_url="http://127.0.0.1:4000/v1"' in command
    )
    assert command[-2] == "thread-123"
    assert command[-1].startswith("The previous run was interrupted")


def test_codex_project_tree_accepts_absent_and_allowed_content(tmp_path):
    validate_codex_project_tree(tmp_path)
    rules = tmp_path / ".codex" / "rules" / "nested"
    skills = tmp_path / ".codex" / "skills"
    rules.mkdir(parents=True)
    skills.mkdir(parents=True)
    (rules / "review.md").write_text("synthetic rule")
    (skills / "SKILL.md").write_text("synthetic skill")
    validate_codex_project_tree(tmp_path)


@pytest.mark.parametrize(
    "relative",
    [
        ".codex/config.toml",
        ".codex/hooks.json",
        ".codex/hooks",
        ".codex/plugins",
        ".codex/agents",
        ".codex/unknown",
        ".codex/unknown-dir",
        ".codex/.DS_Store",
        ".codex/rules",
        ".codex/skills",
    ],
)
def test_codex_project_tree_rejects_forbidden_entries(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name in {"rules", "skills"}:
        path.write_text("not a directory")
    elif "." in path.name:
        path.write_text("secret-content-must-not-leak")
    else:
        path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CodexProjectConfigError) as exc:
        validate_codex_project_tree(tmp_path)
    message = str(exc.value)
    assert relative in message
    assert "secret-content-must-not-leak" not in message


@pytest.mark.parametrize(
    "relative",
    [
        ".codex",
        ".codex/rules",
        ".codex/skills",
        ".codex/rules/nested",
        ".codex/rules/nested.md",
    ],
)
def test_codex_project_tree_rejects_symlinks(tmp_path, relative):
    target = tmp_path / "external"
    target.write_text("external-secret")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    with pytest.raises(CodexProjectConfigError) as exc:
        validate_codex_project_tree(tmp_path)
    assert relative in str(exc.value)
    assert "external-secret" not in str(exc.value)
    assert str(target) not in str(exc.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_codex_project_tree_rejects_fifo(tmp_path):
    rules = tmp_path / ".codex" / "rules"
    rules.mkdir(parents=True)
    fifo = rules / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(CodexProjectConfigError, match=r"\.codex/rules/pipe"):
        validate_codex_project_tree(tmp_path)


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_codex_project_tree_inspection_errors_fail_closed(
    tmp_path, monkeypatch, error_type
):
    rules = tmp_path / ".codex" / "rules"
    rules.mkdir(parents=True)
    real_scandir = os.scandir

    def failing_scandir(path):
        if Path(path) == rules:
            raise error_type("sensitive operating-system detail")
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", failing_scandir)
        with pytest.raises(
            CodexProjectConfigError, match=r"\.codex/rules: cannot inspect safely"
        ) as exc:
            validate_codex_project_tree(tmp_path)
    assert "sensitive operating-system detail" not in str(exc.value)


def test_codex_process_env_removes_credentials_without_mutating_source():
    source = {
        "HOME": "/synthetic/home",
        "PATH": "/bin",
        "LANG": "en_US.UTF-8",
        "BUILD_MODE": "release",
        **{name: "secret" for name in CODEX_CREDENTIAL_ENV_NAMES},
        "NOVEL_API_KEY": "secret",
        "NOVEL_ACCESS_TOKEN": "secret",
        "NOVEL_AUTH_TOKEN": "secret",
    }
    original = source.copy()
    sanitized = codex_process_env(source)
    assert source == original
    assert sanitized == {
        "HOME": "/synthetic/home",
        "PATH": "/bin",
        "LANG": "en_US.UTF-8",
        "BUILD_MODE": "release",
    }


def test_fresh_command_is_fixed_and_explicit():
    env: dict[str, str] = {}
    cmd = CodexRunner().build_command(
        prompt="fix it",
        config=_config(effort="high"),
        env=env,
        model="gpt-5.6-sol",
    )
    assert cmd == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "sandbox_workspace_write.writable_roots=[]",
        "-c",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c",
        f"sandbox_workspace_write.writable_roots={json.dumps([env['TMPDIR']])}",
        "-c",
        "web_search=disabled",
        "-c",
        "features.apps=false",
        "-c",
        "features.hooks=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        "features.network_proxy=false",
        "-c",
        "features.skill_mcp_dependency_install=false",
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "--model",
        "gpt-5.6-sol",
        "-c",
        "model_reasoning_effort=high",
        "exec",
        "--json",
        "--ignore-user-config",
        "fix it",
    ]


def _override(argv: list[str], key: str) -> str:
    """The value Codex keeps for ``key``: it applies ``-c`` overrides in order."""
    values = [
        value.partition("=")[2]
        for flag, value in zip(argv, argv[1:], strict=False)
        if flag == "-c" and value.partition("=")[0] == key
    ]
    assert values, (key, argv)
    return values[-1]


_HOST_BUILDS = [
    pytest.param(
        lambda runner, env: runner.build_command(
            prompt="fix it", config=_config(), env=env, model="gpt-5.6-sol"
        ),
        id="fresh",
    ),
    pytest.param(
        lambda runner, env: runner.build_resume_command(
            session_id="thread-123",
            prompt="ignored",
            config=_config(),
            env=env,
            model="gpt-5.6-sol",
        ),
        id="resume",
    ),
]


@pytest.mark.parametrize("build", _HOST_BUILDS)
def test_a_native_run_cannot_write_the_shared_temp_dirs(build) -> None:
    """#2023: Codex's workspace-write sandbox adds ``$TMPDIR`` and ``/tmp`` to
    its writable roots unless told not to, so every native run could write
    where every other run on the host reads."""
    argv = build(CodexRunner(), {"TMPDIR": "/tmp"})

    assert _override(argv, "sandbox_workspace_write.exclude_tmpdir_env_var") == "true"
    assert _override(argv, "sandbox_workspace_write.exclude_slash_tmp") == "true"
    roots = json.loads(_override(argv, "sandbox_workspace_write.writable_roots"))
    assert "/tmp" not in roots


@pytest.mark.parametrize("build", _HOST_BUILDS)
def test_a_native_run_writes_temp_files_in_its_own_directory(build) -> None:
    env = {"TMPDIR": "/tmp", "PATH": "/bin"}

    argv = build(CodexRunner(), env)

    own = Path(env["TMPDIR"])
    roots = json.loads(_override(argv, "sandbox_workspace_write.writable_roots"))
    assert roots == [str(own)]
    assert own.is_dir()
    assert own.name.startswith("kew-codex-tmp-")
    assert own.resolve().is_relative_to(work_root().resolve())
    assert env["PATH"] == "/bin"


def test_each_native_run_gets_a_fresh_temp_directory_and_releases_it() -> None:
    runner = CodexRunner()
    first: dict[str, str] = {}
    second: dict[str, str] = {}

    runner.build_command(
        prompt="fix it", config=_config(), env=first, model="gpt-5.6-sol"
    )
    runner.build_command(
        prompt="fix it", config=_config(), env=second, model="gpt-5.6-sol"
    )

    assert first["TMPDIR"] != second["TMPDIR"]
    assert not Path(first["TMPDIR"]).exists()
    Path(second["TMPDIR"], "scratch.txt").write_text("x")
    runner.enrich_result(
        DispatchResult(exit_code=0, issue_num=1, log_file=Path("run.log")), _config()
    )
    assert not Path(second["TMPDIR"]).exists()


def test_container_args_keep_their_own_sandbox_roots() -> None:
    args = CodexRunner().container_args(config=_config(), model="gpt-5.6-sol")

    assert _override(args, "sandbox_workspace_write.writable_roots") == "[]"
    assert "sandbox_workspace_write.exclude_slash_tmp=true" not in args
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" not in args


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_fast_tier_command_is_explicit_for_luna(effort: str):
    cmd = CodexRunner().build_command(
        prompt="fix it",
        config=_config(effort=effort, service_tier="fast"),
        env={},
        model="gpt-5.6-luna",
    )

    model_index = cmd.index("--model")
    assert cmd[model_index : model_index + 2] == ["--model", "gpt-5.6-luna"]
    for override in (
        f"model_reasoning_effort={effort}",
        'service_tier="fast"',
        "features.fast_mode=true",
    ):
        assert cmd[cmd.index(override) - 1] == "-c"
    assert cmd[-3:] == ["--json", "--ignore-user-config", "fix it"]


def test_unset_service_tier_preserves_codex_cli_default():
    cmd = CodexRunner().build_command(
        prompt="fix it",
        config=_config(effort="xhigh"),
        env={},
        model="gpt-5.6-luna",
    )

    assert not any("service_tier" in arg for arg in cmd)
    assert "features.fast_mode=true" not in cmd


@pytest.mark.parametrize(
    "tier", ["x" * 65, "priority\nfast", "http://user:password@proxy.invalid", 42]
)
def test_service_tier_rejects_nonconforming_values(tier):
    runner = CodexRunner()
    [event] = runner.parse_line(
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "12345678-1234-5678-9abc-123456789abc",
                "service_tier": tier,
            }
        )
    )
    assert event.metadata.get("service_tier") == "invalid"
    for valid in ("fast", "priority", "default", "auto", "x" * 64):
        [event] = runner.parse_line(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "12345678-1234-5678-9abc-123456789abc",
                    "service_tier": valid,
                }
            )
        )
        assert event.metadata["service_tier"] == valid


def test_fast_tier_is_reapplied_when_resuming():
    cmd = CodexRunner().build_resume_command(
        session_id="thread-123",
        prompt="ignored",
        config=_config(effort="xhigh", service_tier="fast"),
        env={},
        model="gpt-5.6-luna",
    )

    assert 'service_tier="fast"' in cmd
    assert "features.fast_mode=true" in cmd
    assert cmd[-2] == "thread-123"
    assert cmd[-1].startswith("The previous run was interrupted")


def test_command_requires_a_named_model():
    with pytest.raises(ValueError, match="explicit model"):
        CodexRunner().build_command(prompt="x", config=_config(), env={}, model="")


def test_container_args_have_no_binary_or_prompt():
    args = CodexRunner().container_args(config=_config(), model="gpt-5.6-sol")
    assert args[0:2] == ["--ask-for-approval", "never"]
    assert "exec" in args
    assert args[args.index("--model") + 1] == "gpt-5.6-sol"
    assert "codex" not in args
    assert "fix it" not in args


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda runner, model: runner.container_args(config=_config(), model=model),
            id="container",
        ),
        pytest.param(
            lambda runner, model: runner.build_command(
                prompt="fix it", config=_config(), env={}, model=model
            ),
            id="host",
        ),
        pytest.param(
            lambda runner, model: runner.build_resume_command(
                session_id="thread-123",
                prompt="ignored",
                config=_config(),
                env={},
                model=model,
            ),
            id="resume",
        ),
    ],
)
def test_every_codex_launch_refuses_without_a_model(build) -> None:
    """A blank model is a refusal on EVERY path, not a silent CLI default.

    The check used to live in the two host builders, so ``container_args``
    built an argv with no ``--model`` in it and Codex ran whatever its own
    default was — billed and attested as the routed model.
    """
    runner = CodexRunner()

    with pytest.raises(ValueError, match="requires an explicit model"):
        build(runner, "")

    argv = build(runner, "gpt-5.6-sol")
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"


def test_a_refused_codex_launch_leaves_the_previous_attempt_untouched() -> None:
    """The model refusal lands before ``_reset_attempt`` clears the stream state."""
    runner = CodexRunner()
    runner.parse_line(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))

    with pytest.raises(ValueError, match="requires an explicit model"):
        runner.build_resume_command(
            session_id="thread-2", prompt="ignored", config=_config(), env={}, model=""
        )

    assert runner._thread_id == "thread-1"


def _line(payload: object) -> str:
    return json.dumps(payload)


def test_thread_started_emits_init_and_captures_resume_id():
    runner = CodexRunner()
    events = runner.parse_line(
        _line({"type": "thread.started", "thread_id": "thread-123"})
    )
    assert len(events) == 1
    assert events[0].event_type == "init"
    assert events[0].metadata["session_id"] == "thread-123"


def test_agent_message_emits_bounded_activity_and_retains_terminal_marker():
    events = CodexRunner().parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "KEW_NEEDS_HUMAN: missing acceptance criteria",
                },
            }
        )
    )
    assert events[0].event_type == "activity"
    assert events[0].message.startswith("KEW_NEEDS_HUMAN")


def test_command_execution_emits_tool_use():
    events = CodexRunner().parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "uv run pytest -q",
                    "status": "completed",
                },
            }
        )
    )
    assert events[0].event_type == "tool_use"
    assert events[0].message == "command_execution"
    assert events[0].tool_uses == 1
    assert events[0].metadata == {"target": "uv run pytest -q"}


def test_tokenless_tool_event_contributes_tool_count_without_tokens():
    event = CodexRunner().parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "pwd",
                    "status": "completed",
                },
            }
        )
    )[0]
    totals = accumulate_usage([event])
    assert totals.tool_uses == 1
    assert totals.input_tokens == totals.output_tokens == 0


def test_turn_completed_splits_uncached_and_cached_tokens():
    runner = CodexRunner()
    assert runner.turn_completed is False
    runner.parse_line(_line({"type": "thread.started", "thread_id": "t1"}))
    events = runner.parse_line(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "output_tokens": 125,
                    "reasoning_output_tokens": 75,
                },
            }
        )
    )
    assert [event.event_type for event in events] == ["usage", "step", "result"]
    usage = events[0]
    assert usage.input_tokens == 200
    assert usage.cache_read_tokens == 800
    assert usage.cache_creation_tokens == 0
    assert usage.output_tokens == 125
    assert events[1].metadata["cost_usd"] is None
    assert events[-1].metadata["session_id"] == "t1"
    assert runner.turn_completed is True
    assert events[-1].metadata["stop_reason"] == "completed"
    assert events[-1].metadata["cost_usd"] is None


def test_negative_and_boolean_usage_is_clamped_to_zero():
    events = CodexRunner().parse_line(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": -1,
                    "cached_input_tokens": True,
                    "output_tokens": -9.5,
                },
            }
        )
    )
    usage = events[0]
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected_types"),
    [
        (
            "0.145.0-events-success.jsonl",
            ["init", "activity", "tool_use", "usage", "step", "result"],
        ),
        ("0.145.0-events-failure.jsonl", ["init", "activity"]),
    ],
)
def test_committed_0145_jsonl_stream_is_consumed(fixture_name, expected_types):
    runner = CodexRunner()
    events = [
        event
        for line in (FIXTURES / fixture_name).read_text().splitlines()
        for event in runner.parse_line(line)
    ]
    assert [event.event_type for event in events] == expected_types


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not-json",
        "null",
        "42",
        "[]",
        _line({"type": "item.completed"}),
        _line({"type": "item.completed", "item": "bad"}),
        _line({"type": "item.completed", "item": {"type": "unknown"}}),
        _line({"type": "item.completed", "item": {"type": "command_execution"}}),
        _line({"type": "turn.completed", "usage": "bad"}),
        _line({"type": "unknown"}),
    ],
)
def test_malformed_and_unknown_lines_are_ignored(line):
    assert CodexRunner().parse_line(line) == []


def test_float_nan_and_infinite_usage_is_ignored():
    for value in (1.5, float("nan"), float("inf")):
        usage = CodexRunner().parse_line(
            _line(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": value, "output_tokens": value},
                }
            )
        )[0]
        assert usage.input_tokens == usage.output_tokens == 0


def test_activity_and_tool_targets_are_bounded():
    runner = CodexRunner()
    activity = runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "a" * 5000},
            }
        )
    )[0]
    tool = runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "b" * 5000,
                    "status": "completed",
                },
            }
        )
    )[0]
    assert len(activity.message) == len(tool.metadata["target"]) == 203


def test_resume_attempt_clears_terminal_state_and_sets_requested_thread():
    runner = CodexRunner()
    runner.parse_line(_line({"type": "thread.started", "thread_id": "old"}))
    runner.parse_line(_line({"type": "turn.completed", "usage": {}}))
    runner.parse_line(_line({"type": "turn.failed", "error": {"message": "timeout"}}))
    runner.build_resume_command(
        session_id="new",
        prompt="ignored",
        config=_config(),
        env={},
        model="gpt-5.6-sol",
    )
    classified = runner.classify_result(exit_code=0, last_output="{}", config=_config())
    assert classified.category == "permanent"
    assert classified.session_id == "new"
    assert classified.stop_reason == "incomplete"


def test_fresh_attempt_clears_prior_thread_and_terminal_state():
    runner = CodexRunner()
    runner.parse_line(_line({"type": "thread.started", "thread_id": "old"}))
    runner.parse_line(_line({"type": "turn.failed", "error": {"message": "timeout"}}))
    runner.build_command(
        prompt="retry from scratch",
        config=_config(),
        env={},
        model="gpt-5.6-sol",
    )
    classified = runner.classify_result(exit_code=0, last_output=None, config=_config())
    assert classified.category == "permanent"
    assert classified.session_id is None
    assert classified.stop_reason == "incomplete"


def test_needs_human_marker_outranks_exit_zero():
    runner = CodexRunner()
    runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "KEW_NEEDS_HUMAN: missing schema decision",
                },
            }
        )
    )
    runner.parse_line(_line({"type": "turn.completed", "usage": {}}))
    classified = runner.classify_result(exit_code=0, last_output=None, config=_config())
    assert classified.category == "needs_human"
    assert classified.subtype == "missing schema decision"


@pytest.mark.parametrize(
    ("exit_code", "error", "expected"),
    [
        (124, "", "transient"),
        (137, "", "watchdog_kill"),
        (143, "", "watchdog_kill"),
        (1, "rate limit exceeded", "transient"),
        (1, "network timeout", "transient"),
        (1, "service overloaded", "transient"),
        (1, "http 503", "transient"),
        (1, "authentication required", "permanent"),
        (1, "model not found", "permanent"),
        (1, "invalid config", "permanent"),
        (1, "git permission denied", "permanent"),
        (1, "sandbox violation", "permanent"),
        (1, "unknown", "transient"),
        (2, "unknown", "permanent"),
    ],
)
def test_failure_classification(exit_code, error, expected):
    runner = CodexRunner()
    if error:
        runner.parse_line(_line({"type": "turn.failed", "error": {"message": error}}))
    classified = runner.classify_result(
        exit_code=exit_code, last_output=None, config=_config()
    )
    assert classified.category == expected


def test_completed_success_and_failed_stop_reasons():
    completed = CodexRunner()
    completed.parse_line(_line({"type": "turn.completed", "usage": {}}))
    result = completed.classify_result(exit_code=0, last_output=None, config=_config())
    assert result.category == ""
    assert result.stop_reason == "completed"

    failed = CodexRunner()
    failed.parse_line(
        _line({"type": "turn.failed", "error": {"message": "bad config"}})
    )
    result = failed.classify_result(exit_code=1, last_output=None, config=_config())
    assert result.category == "permanent"
    assert result.stop_reason == "failed"


def test_marker_must_start_final_message_and_reason_is_bounded():
    runner = CodexRunner()
    runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "prefix KEW_NEEDS_HUMAN: no",
                },
            }
        )
    )
    runner.parse_line(_line({"type": "turn.completed", "usage": {}}))
    assert (
        runner.classify_result(exit_code=0, last_output=None, config=_config()).category
        == ""
    )
    runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "KEW_NEEDS_HUMAN: " + "x" * 1000,
                },
            }
        )
    )
    classified = runner.classify_result(exit_code=0, last_output=None, config=_config())
    assert classified.category == "needs_human"
    assert len(classified.subtype) == 500


def test_help_fixtures_pin_official_0145_contract():
    root = (FIXTURES / "0.145.0-help.txt").read_text()
    exec_help = (FIXTURES / "0.145.0-exec-help.txt").read_text()
    resume_help = (FIXTURES / "0.145.0-exec-resume-help.txt").read_text()
    assert "codex-cli 0.145.0" in root
    assert "--ask-for-approval" in root
    assert "--sandbox" in root
    assert "--json" in exec_help
    assert "--ignore-user-config" in exec_help
    assert "--json" in resume_help
    assert "--ignore-user-config" in resume_help
    assert "SESSION_ID" in resume_help
    assert "PROMPT" in resume_help


def test_resume_command_has_fixed_order_and_ignores_caller_prompt():
    command = CodexRunner().build_resume_command(
        session_id="thread-123",
        prompt="do something unsafe",
        config=_config(effort="high"),
        env={},
        model="gpt-5.6-sol",
    )
    exec_index = command.index("exec")
    assert command[0] == "codex"
    assert command.index("--ask-for-approval") < exec_index
    assert command.index("--sandbox") < exec_index
    assert command.index("--model") < exec_index
    assert command[exec_index : exec_index + 4] == [
        "exec",
        "resume",
        "--json",
        "--ignore-user-config",
    ]
    assert command[-2] == "thread-123"
    assert command[-1].startswith("The previous run was interrupted")
    assert "do something unsafe" not in command
    assert CodexRunner().supports_resume() is True


def test_policy_gate_stop_refuses_publication_naming_the_test():
    """#2601: the stop line the dispatch prompt prescribes for a failing policy
    gate classifies as needs_human, which dispatch never publishes, and the
    reason carries the failing test id."""
    failing = "tests/test_silence_on_failure.py::test_no_new_silent_clean_defaults"
    runner = CodexRunner()
    runner.parse_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": f"KEW_NEEDS_HUMAN: policy gate failed: {failing}",
                },
            }
        )
    )
    runner.parse_line(_line({"type": "turn.completed", "usage": {}}))
    classified = runner.classify_result(exit_code=0, last_output=None, config=_config())
    assert classified.category == "needs_human"
    assert classified.subtype == f"policy gate failed: {failing}"
