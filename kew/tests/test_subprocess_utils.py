"""Tests for subprocess utility functions."""

import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.subprocess_utils import (
    RUNNER_REQUIREMENTS,
    TOOL_PATHS,
    RunnerReachability,
    _probe_claude_cli_auth,
    inspect_runner,
    preflight_tools,
    resolve_cmd,
)


def test_subprocess_exemption_list_is_fixed_and_filtered():
    import re

    from kew import subprocess_utils

    assert re.findall(r"^\* ``([^`]+)``", subprocess_utils.__doc__, re.MULTILINE) == [
        "process_groups.py",
        "stop_recovery.py",
        "runners/lemonade_agent.py",
    ]


@pytest.mark.serial
@pytest.mark.parametrize("inference", ["absent", "inherited", "issued"])
@pytest.mark.parametrize(
    "probe",
    [
        "version",
        "claude-auth",
        "codex-inspect",
        "readiness-boundary",
        "tracked-boundary",
    ],
)
def test_discovery_children_filter_management_credentials(
    tmp_path, monkeypatch, real_runner_preflight, probe, inference
):
    import json
    import os
    from types import SimpleNamespace

    from kew import subprocess_utils
    from kew.credential_issuer import BoundedRunKey
    from kew.run_credentials import launch_environment

    captured = tmp_path / "environment.jsonl"
    binary = tmp_path / "claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(captured)!r}, 'a') as output:\n"
        "    print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}), "
        "file=output)\n"
        "print('codex-cli 0.145.0' if '--version' in sys.argv "
        "else '{\"loggedIn\": true}')\n"
    )
    binary.chmod(0o755)
    environment = {
        "PATH": str(tmp_path),
        "OPENROUTER_PROVISIONING_KEY": "management-secret-1944",
    }
    if inference == "inherited":
        environment["OPENROUTER_API_KEY"] = "host-secret-1944"
    # Discovery never receives the key being held for the actual launch.
    key = BoundedRunKey("issued-secret-1944", "a" * 64, 25, "2099-01-01T00:00:00Z")
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(TOOL_PATHS, "codex", str(binary))
    if probe == "version":
        assert subprocess_utils._probe_runner_version(str(binary)) == "0.145.0"
    elif probe == "claude-auth":
        assert _probe_claude_cli_auth()
    elif probe == "codex-inspect":
        assert subprocess_utils._probe_codex(str(binary)) == ("0.145.0", True, True)
    elif probe == "readiness-boundary":
        from kew.probe_route import _host_probe_boundary

        boundary = _host_probe_boundary("codex", SimpleNamespace(posture="native"), {})
        assert boundary.runner_version == "0.145.0"
    else:
        from kew.dispatch_core import _host_boundary

        boundary = _host_boundary(
            SimpleNamespace(sandbox_posture="native", credential=key), "codex", {}
        )
        assert boundary.runner_version == "0.145.0"
    records = [json.loads(line) for line in captured.read_text().splitlines()]
    assert len(records) == (2 if probe == "codex-inspect" else 1)
    for record in records:
        child_env = record["env"]
        assert "OPENROUTER_PROVISIONING_KEY" not in child_env
        expected = None
        assert child_env.get("OPENROUTER_API_KEY") == expected
        assert "issued-secret-1944" not in child_env.values()
    launch = launch_environment(environment, key if inference == "issued" else None)
    expected_launch = "issued-secret-1944" if inference == "issued" else expected
    if inference == "inherited":
        expected_launch = "host-secret-1944"
    assert launch.get("OPENROUTER_API_KEY") == expected_launch
    assert environment["OPENROUTER_PROVISIONING_KEY"] == "management-secret-1944"


@pytest.mark.serial
@pytest.mark.parametrize(
    "api", ["run", "run_bytes", "run_pinned", "popen", "popen_bytes", "popen_pinned"]
)
@pytest.mark.parametrize("explicit_env", [False, True])
def test_subprocess_boundary_filters_discovery_environments(
    tmp_path, monkeypatch, api, explicit_env
):
    import json
    import os
    from pathlib import Path

    from kew import subprocess_utils

    environment = {
        "PATH": str(tmp_path),
        "OPENROUTER_PROVISIONING_KEY": "management-secret-1944",
        "OPENROUTER_API_KEY": "host-secret-1944",
    }
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(TOOL_PATHS, "codex", sys.executable)
    args = ["-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    call = getattr(subprocess_utils, api)
    if api.endswith("_pinned"):
        executable = Path(sys.executable).resolve()
        pinned = subprocess_utils.pin_executable(
            executable, subprocess_utils._executable_sha256(executable)
        )
        positional = [pinned, args]
    else:
        positional = [["codex", *args]]
    kwargs = {"env": environment} if explicit_env else {}
    if api.startswith("popen"):
        with call(*positional, **kwargs) as child:
            output, _ = child.communicate(timeout=5)
            assert child.returncode == 0
    else:
        output = call(*positional, timeout=5, **kwargs).stdout
    child_env = json.loads(output)
    assert "OPENROUTER_PROVISIONING_KEY" not in child_env
    assert "OPENROUTER_API_KEY" not in child_env
    assert environment["OPENROUTER_PROVISIONING_KEY"] == "management-secret-1944"


def test_bounded_output_keeps_head_tail_and_preserves_normal_output():
    from kew import subprocess_utils

    buffer_type = getattr(subprocess_utils, "BoundedOutput", None)
    assert buffer_type is not None, "streaming capture requires bounded buffers"
    output = buffer_type(128)
    for chunk in (b"normal\n", b"output\n"):
        output.append(chunk)
    assert output.text() == "normal\noutput\n"
    output.append(b"M" * 300)
    output.append(b"tail\n")
    assert len(output.head) == len(output.tail) == 64
    assert output.text().startswith("normal\noutput\n")
    assert output.text().endswith("tail\n")
    assert output.marker.decode() in output.text()
    assert len(output.text()) == 128 + len(output.marker)


@pytest.mark.serial
def test_bounded_process_preserves_both_streams_and_shared_deadline(monkeypatch):
    import time

    from kew import subprocess_utils

    capture = getattr(subprocess_utils, "run_bounded", None)
    assert capture is not None, "streaming capture must enforce its deadline"
    monkeypatch.setitem(TOOL_PATHS, "sh", shutil.which("sh"))
    result = capture(
        ["sh", "-c", "printf 'hello\\r\\n'; printf 'error\\n' >&2; exit 7"],
        deadline=time.monotonic() + 5,
        output_limit=128,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        7,
        "hello\n",
        "error\n",
    )
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        capture(["sh", "-c", "exec sleep 5"], deadline=start + 0.15, output_limit=128)
    assert time.monotonic() - start < 2


class TestResolveCmd:
    @pytest.fixture(autouse=True)
    def _empty_tool_paths(self):
        # These tests assert against a controlled allowlist. Any test module
        # that runs a real preflight_tools() (loop resume, epic status, the
        # gh smoke) populates the global TOOL_PATHS — clear IN PLACE before
        # (aliases share the dict object) and restore after, mirroring
        # test_epic_driver_gh_smoke's _reset_tool_paths.
        saved = dict(TOOL_PATHS)
        TOOL_PATHS.clear()
        yield
        TOOL_PATHS.clear()
        TOOL_PATHS.update(saved)

    def test_uses_full_path(self):
        TOOL_PATHS["gh"] = "/usr/bin/gh"
        resolved = resolve_cmd(["gh", "issue", "list"])
        assert resolved == ["/usr/bin/gh", "issue", "list"]

    def test_rejects_unknown_executable(self):
        with pytest.raises(ValueError, match="not in allowlist"):
            resolve_cmd(["curl", "https://evil.com"])


class TestPreflightTools:
    @pytest.fixture(autouse=True)
    def _save_tool_paths(self):
        saved = TOOL_PATHS.copy()
        yield
        TOOL_PATHS.clear()
        TOOL_PATHS.update(saved)

    def test_resolves_required_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            shutil,
            "which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd in ("gh", "git") else None,
        )
        TOOL_PATHS.clear()
        preflight_tools()
        assert "gh" in TOOL_PATHS
        assert "git" in TOOL_PATHS

    def test_resolves_optional_tools(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            shutil,
            "which",
            lambda cmd: (
                f"/usr/bin/{cmd}" if cmd in ("gh", "git", "claude", "podman") else None
            ),
        )
        TOOL_PATHS.clear()
        preflight_tools()
        assert "claude" in TOOL_PATHS
        assert TOOL_PATHS["podman"] == "/usr/bin/podman"

    def test_raises_if_required_tool_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        TOOL_PATHS.clear()
        with pytest.raises(SystemExit):
            preflight_tools()

    def test_optional_tools_not_required(self, monkeypatch: pytest.MonkeyPatch):
        """Preflight succeeds even when optional tools (claude) are missing."""
        monkeypatch.setattr(
            shutil,
            "which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd in ("gh", "git") else None,
        )
        TOOL_PATHS.clear()
        preflight_tools()  # Should not raise
        assert "claude" not in TOOL_PATHS

    def test_a_narrowed_requirement_does_not_demand_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`kew verify` needs git and a shell, never gh (#2379 review)."""
        monkeypatch.setattr(
            shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "git" else None
        )
        TOOL_PATHS.clear()
        preflight_tools(required=("git",))  # Should not raise over a missing gh
        assert TOOL_PATHS["git"] == "/usr/bin/git"
        assert "gh" not in TOOL_PATHS

    def test_a_narrowed_requirement_still_registers_an_installed_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Narrowing drops the *requirement*, not the allowlist entry."""
        monkeypatch.setattr(
            shutil,
            "which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd in ("gh", "git") else None,
        )
        TOOL_PATHS.clear()
        preflight_tools(required=("git",))
        assert TOOL_PATHS["gh"] == "/usr/bin/gh"

    def test_a_narrowed_requirement_still_exits_on_its_own_missing_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        TOOL_PATHS.clear()
        with pytest.raises(SystemExit):
            preflight_tools(required=("git",))


def _make_completed(returncode: int = 0, stderr: str = "", stdout: str = ""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stderr = stderr
    result.stdout = stdout
    return result


class TestIsRateLimited:
    def test_detects_rate_limit_in_stderr(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(
            returncode=403, stderr="error: API rate limit exceeded"
        )
        assert is_rate_limited(result) is True

    def test_detects_secondary_rate_limit(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(
            returncode=1, stderr="You have exceeded a secondary rate limit"
        )
        assert is_rate_limited(result) is True

    def test_detects_rate_limit_in_stdout(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(
            returncode=1, stdout="API rate limit exceeded for user"
        )
        assert is_rate_limited(result) is True

    def test_not_rate_limited_on_success(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(returncode=0, stdout="{}")
        assert is_rate_limited(result) is False

    def test_not_rate_limited_on_generic_error(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(returncode=1, stderr="repository not found")
        assert is_rate_limited(result) is False

    def test_case_insensitive(self):
        from kew.subprocess_utils import is_rate_limited

        result = _make_completed(returncode=1, stderr="GitHub Rate Limit reached")
        assert is_rate_limited(result) is True


class TestIsTransientError:
    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: HTTP 503",
            "HTTP 503: Service Unavailable (https://api.github.com/...)",
            "HTTP 502: Bad Gateway",
            "HTTP 500: Internal Server Error",
            "HTTP 504: Gateway Timeout",
        ],
    )
    def test_detects_5xx(self, stderr):
        from kew.subprocess_utils import is_transient_error

        assert is_transient_error(_make_completed(returncode=1, stderr=stderr)) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "read tcp 10.0.0.1:443: connection reset by peer",
            "dial tcp api.github.com:443: connect: connection refused",
            "write: broken pipe",
            'Get "https://api.github.com": net/http: TLS handshake timeout',
            "context deadline exceeded (i/o timeout)",
            "unexpected EOF",
        ],
    )
    def test_detects_connection_shapes(self, stderr):
        from kew.subprocess_utils import is_transient_error

        assert is_transient_error(_make_completed(returncode=1, stderr=stderr)) is True

    def test_rate_limit_is_transient(self):
        from kew.subprocess_utils import is_transient_error

        result = _make_completed(returncode=1, stderr="API rate limit exceeded")
        assert is_transient_error(result) is True

    @pytest.mark.parametrize("stderr", ["HTTP 404: Not Found", "HTTP 422", "HTTP 403"])
    def test_4xx_fails_fast(self, stderr):
        from kew.subprocess_utils import is_transient_error

        # A plain 4xx is a client error — never retried. (A 403 rate limit is
        # caught by the rate-limit marker, not the status code.)
        assert is_transient_error(_make_completed(returncode=1, stderr=stderr)) is False

    def test_generic_error_not_transient(self):
        from kew.subprocess_utils import is_transient_error

        result = _make_completed(returncode=1, stderr="repository not found")
        assert is_transient_error(result) is False

    def test_success_never_transient(self):
        from kew.subprocess_utils import is_transient_error

        # A 200 response whose body happens to mention these shapes must not be
        # treated as a failure to retry.
        result = _make_completed(
            returncode=0, stdout='{"body": "saw HTTP 503 / connection reset by peer"}'
        )
        assert is_transient_error(result) is False


class TestRunWithBackoff:
    @pytest.fixture(autouse=True)
    def _set_gh_path(self):
        TOOL_PATHS["gh"] = "/usr/bin/gh"
        yield
        TOOL_PATHS.pop("gh", None)

    @patch("kew.subprocess_utils.run")
    def test_returns_immediately_on_success(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        mock_run.return_value = _make_completed(returncode=0)
        sleeps: list[float] = []

        result = run_with_backoff(["gh", "api", "rate_limit"], _sleep=sleeps.append)

        assert result.returncode == 0
        assert mock_run.call_count == 1
        assert sleeps == []

    @patch("kew.subprocess_utils.run")
    def test_retries_once_on_rate_limit_then_succeeds(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        rate_limited = _make_completed(returncode=1, stderr="API rate limit exceeded")
        success = _make_completed(returncode=0)
        mock_run.side_effect = [rate_limited, success]
        sleeps: list[float] = []

        result = run_with_backoff(
            ["gh", "issue", "list"], backoff_base=10.0, _sleep=sleeps.append
        )

        assert result.returncode == 0
        assert mock_run.call_count == 2
        assert len(sleeps) == 1
        assert sleeps[0] == 10.0  # backoff_base * 2^0

    @patch("kew.subprocess_utils.run")
    def test_retries_on_5xx_then_succeeds(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        five_oh_three = _make_completed(returncode=1, stderr="gh: HTTP 503")
        success = _make_completed(returncode=0)
        mock_run.side_effect = [five_oh_three, success]
        sleeps: list[float] = []

        result = run_with_backoff(
            ["gh", "api", "repos/x/y/pulls/1/reviews"],
            backoff_base=10.0,
            _sleep=sleeps.append,
        )

        assert result.returncode == 0
        assert mock_run.call_count == 2
        assert sleeps == [10.0]

    @patch("kew.subprocess_utils.run")
    def test_retries_on_connection_reset_then_succeeds(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        reset = _make_completed(
            returncode=1, stderr="read tcp: connection reset by peer"
        )
        success = _make_completed(returncode=0, stdout="[]")
        mock_run.side_effect = [reset, success]
        sleeps: list[float] = []

        result = run_with_backoff(
            ["gh", "issue", "list"], backoff_base=10.0, _sleep=sleeps.append
        )

        assert result.returncode == 0
        assert mock_run.call_count == 2

    @patch("kew.subprocess_utils.run")
    def test_does_not_retry_on_4xx(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        mock_run.return_value = _make_completed(returncode=1, stderr="HTTP 404")
        sleeps: list[float] = []

        result = run_with_backoff(
            ["gh", "issue", "list"], check=False, _sleep=sleeps.append
        )

        assert result.returncode == 1
        assert mock_run.call_count == 1  # 4xx fails fast, no retry
        assert sleeps == []

    @patch("kew.subprocess_utils.run")
    def test_uses_exponential_backoff(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        rate_limited = _make_completed(returncode=1, stderr="rate limit exceeded")
        success = _make_completed(returncode=0)
        mock_run.side_effect = [rate_limited, rate_limited, success]
        sleeps: list[float] = []

        run_with_backoff(
            ["gh", "issue", "list"],
            max_attempts=3,
            backoff_base=5.0,
            _sleep=sleeps.append,
        )

        assert sleeps == [5.0, 10.0]  # base * 2^0, base * 2^1

    @patch("kew.subprocess_utils.run")
    def test_stops_after_max_attempts(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        rate_limited = _make_completed(returncode=1, stderr="rate limit exceeded")
        mock_run.return_value = rate_limited
        sleeps: list[float] = []

        run_with_backoff(
            ["gh", "issue", "list"],
            max_attempts=3,
            check=False,
            _sleep=sleeps.append,
        )

        assert mock_run.call_count == 3
        assert len(sleeps) == 2  # sleeps between attempt 0→1 and 1→2

    @patch("kew.subprocess_utils.run")
    def test_raises_on_non_rate_limit_failure_when_check_true(
        self, mock_run: MagicMock
    ):
        from kew.subprocess_utils import run_with_backoff

        mock_run.return_value = _make_completed(returncode=1, stderr="not found")

        with pytest.raises(subprocess.CalledProcessError):
            run_with_backoff(["gh", "issue", "list"], check=True, _sleep=lambda _: None)

        assert mock_run.call_count == 1  # no retry for non-rate-limit errors

    @patch("kew.subprocess_utils.run")
    def test_check_false_returns_failed_result(self, mock_run: MagicMock):
        from kew.subprocess_utils import run_with_backoff

        mock_run.return_value = _make_completed(returncode=1, stderr="not found")

        result = run_with_backoff(
            ["gh", "issue", "list"], check=False, _sleep=lambda _: None
        )

        assert result.returncode == 1


class TestPopenStartNewSession:
    """popen() can launch the child as its own process-group leader."""

    def test_start_new_session_makes_child_group_leader(self, monkeypatch):
        import os
        import shutil

        from kew import subprocess_utils

        sleep_path = shutil.which("sleep")
        assert sleep_path is not None
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "sleep", sleep_path)

        proc = subprocess_utils.popen(["sleep", "30"], start_new_session=True)
        try:
            assert os.getpgid(proc.pid) == proc.pid
        finally:
            proc.kill()
            proc.wait()

    def test_default_keeps_parent_process_group(self, monkeypatch):
        import os
        import shutil

        from kew import subprocess_utils

        sleep_path = shutil.which("sleep")
        assert sleep_path is not None
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "sleep", sleep_path)

        proc = subprocess_utils.popen(["sleep", "30"])
        try:
            assert os.getpgid(proc.pid) == os.getpgid(0)
        finally:
            proc.kill()
            proc.wait()


class TestRunnerBinariesAreAllowlisted:
    """Every registered runner must invoke an allowlisted executable.

    resolve_cmd() raises on any binary outside the allowlist, so a runner
    whose binary is missing from _OPTIONAL_TOOLS/_REQUIRED_TOOLS fails the
    moment kew tries to launch it. This guards the whole runner set at once.
    """

    def test_every_runner_binary_is_in_the_allowlist(self):
        from kew.config import DispatchConfig
        from kew.runners.registry import _get_runner_factories
        from kew.subprocess_utils import _OPTIONAL_TOOLS, _REQUIRED_TOOLS

        allowed = set(_OPTIONAL_TOOLS) | set(_REQUIRED_TOOLS)
        config = DispatchConfig()
        # A provider/model form every runner's build_command accepts (hermes is
        # OpenRouter-only and fails closed on other models); only affects flag
        # values, never the binary (cmd[0]) this test asserts on.
        config.agent.model = "openrouter/deepseek/deepseek-v4-flash"
        for name, factory in _get_runner_factories().items():
            cmd = factory().build_command(
                prompt="x", config=config, env={}, model=config.agent.model
            )
            assert cmd[0] in allowed, (
                f"runner {name!r} invokes {cmd[0]!r}, which is not in the "
                f"subprocess allowlist — kew could not launch it"
            )


class TestPreflightRegistersPs:
    def test_ps_added_to_tool_paths(self, monkeypatch):
        from kew import subprocess_utils

        monkeypatch.setattr(
            subprocess_utils.shutil, "which", lambda name: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(subprocess_utils, "TOOL_PATHS", {})
        subprocess_utils.preflight_tools()
        assert subprocess_utils.TOOL_PATHS["ps"] == "/usr/bin/ps"


# ---------------------------------------------------------------------------
# Runner reachability inspection (#247)
# ---------------------------------------------------------------------------


def _claude_config() -> DispatchConfig:
    # default routing resolves to the claude-code runner
    return DispatchConfig()


def test_inspect_runner_binary_present_with_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"):
        r = inspect_runner(_claude_config())
    assert r.runner_name == "claude-code"
    assert r.binary == "claude"
    assert r.binary_present is True
    assert r.credential_detected is True


def test_inspect_runner_binary_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with patch("kew.subprocess_utils.shutil.which", return_value=None):
        r = inspect_runner(_claude_config())
    assert r.binary_present is False


def test_inspect_runner_no_credential(monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(var, raising=False)
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils._probe_claude_cli_auth", return_value=False),
    ):
        r = inspect_runner(_claude_config())
    assert r.credential_detected is False
    assert isinstance(r, RunnerReachability)


def test_inspect_runner_cli_auth_fallback(monkeypatch):
    """CLI login detected → credential_detected is True even without env var."""
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(var, raising=False)
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils._probe_claude_cli_auth", return_value=True),
    ):
        r = inspect_runner(_claude_config())
    assert r.credential_detected is True


# -- _probe_claude_cli_auth unit tests --


def test_probe_claude_cli_auth_logged_in():
    result = MagicMock(returncode=0, stdout='{"loggedIn": true}')
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils.subprocess.run", return_value=result),
    ):
        assert _probe_claude_cli_auth() is True


def test_probe_claude_cli_auth_not_logged_in():
    result = MagicMock(returncode=0, stdout='{"loggedIn": false}')
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils.subprocess.run", return_value=result),
    ):
        assert _probe_claude_cli_auth() is False


def test_probe_claude_cli_auth_binary_not_found():
    with patch("kew.subprocess_utils.shutil.which", return_value=None):
        assert _probe_claude_cli_auth() is False


def test_probe_claude_cli_auth_nonzero_exit():
    result = MagicMock(returncode=1, stdout="")
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils.subprocess.run", return_value=result),
    ):
        assert _probe_claude_cli_auth() is False


def test_probe_claude_cli_auth_timeout():
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "kew.subprocess_utils.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
        ),
    ):
        assert _probe_claude_cli_auth() is False


def test_inspect_runners_covers_all_routing_tiers(monkeypatch):
    """A complex-tier runner whose binary is missing must be visible."""
    from kew.config import DispatchConfig, RouteEntry, RoutingConfig
    from kew.subprocess_utils import inspect_runners

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = DispatchConfig(
        routing=RoutingConfig(
            default="claude-code", complex=RouteEntry(runner="opencode")
        )
    )

    def fake_which(binary):
        return "/usr/bin/claude" if binary == "claude" else None  # opencode absent

    with patch("kew.subprocess_utils.shutil.which", side_effect=fake_which):
        reaches = inspect_runners(config)

    names = {r.runner_name for r in reaches}
    assert "claude-code" in names and "opencode" in names
    opencode = next(r for r in reaches if r.runner_name == "opencode")
    assert opencode.binary_present is False


def test_the_subscription_token_counts_as_a_credential(monkeypatch):
    """#2298: the common auth path used to read as "no credential at all".

    ``CLAUDE_CODE_OAUTH_TOKEN`` is what ``claude setup-token`` mints and the only
    name a container can inherit for a subscription account. The hand-kept
    ``CLAUDE_AUTH_ENVS`` tuple did not contain it, so ``kew health`` warned about
    a working install — and would have warned forever, because nothing held the
    tuple to the runner's own declaration.
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils._probe_claude_cli_auth", return_value=False),
    ):
        r = inspect_runner(_claude_config())
    assert r.credential_detected is True


def test_the_host_only_auth_paths_still_count(monkeypatch):
    """Bedrock and Vertex authenticate a HOST run and no container can inherit
    them, so they are not in the runner's declaration — and must not be lost by
    composing the check from that declaration."""
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude"),
        patch("kew.subprocess_utils._probe_claude_cli_auth", return_value=False),
    ):
        r = inspect_runner(_claude_config())
    assert r.credential_detected is True


def test_the_claude_auth_names_are_the_runner_declaration_plus_the_host_paths():
    """One list, composed, never two hand-kept ones."""
    from kew.runners.registry import runner_required_credentials
    from kew.subprocess_utils import CLAUDE_HOST_ONLY_AUTH_ENVS, _claude_auth_envs

    names = set(_claude_auth_envs())
    assert names >= set(runner_required_credentials("claude-code"))
    assert names >= set(CLAUDE_HOST_ONLY_AUTH_ENVS)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names


def test_inspect_runners_includes_the_review_lane(monkeypatch):
    """The review lane has its own runner pin (#2298).

    A reading taken over the routing tiers alone said nothing about the stage
    that holds every PR: with every node routed to opencode, a reviewer whose
    binary is missing read as a healthy control plane until the first round.
    """
    from kew.config import DispatchConfig, ReviewConfig, RouteEntry, RoutingConfig
    from kew.subprocess_utils import inspect_runners

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="opencode"),
            simple=RouteEntry(runner="opencode"),
            medium=RouteEntry(runner="opencode"),
            complex=RouteEntry(runner="opencode"),
        ),
        review=ReviewConfig(runner="claude-code"),
    )

    def fake_which(binary):
        return "/usr/bin/opencode" if binary == "opencode" else None  # claude absent

    with (
        patch("kew.subprocess_utils.shutil.which", side_effect=fake_which),
        patch("kew.subprocess_utils._probe_claude_cli_auth", return_value=False),
    ):
        reaches = inspect_runners(config)

    reviewer = next(r for r in reaches if r.runner_name == "claude-code")
    assert reviewer.binary_present is False


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("codex-cli 0.145.0", (0, 145, 0)),
        ("codex-cli 1.2.3", (1, 2, 3)),
        ("codex-cli 0.145.0-beta.1", (0, 145, 0)),
        ("codex-cli 0.144.9", (0, 144, 9)),
        ("codex 0.145.0", None),
        ("not a version", None),
    ],
)
def test_parse_codex_version(output, expected):
    from kew.runners.codex import parse_codex_version

    assert parse_codex_version(output) == expected


def test_inspect_codex_uses_bounded_sanitized_official_probes(monkeypatch):
    from kew.runners.codex import MIN_CODEX_VERSION

    assert MIN_CODEX_VERSION == (0, 145, 0)
    assert RUNNER_REQUIREMENTS["codex"] == (
        "codex",
        "npm install -g @openai/codex",
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="codex-cli 0.145.0\n", stderr=""
        )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-probe")
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/opt/bin/codex"),
        patch("kew.subprocess_utils.subprocess.run", side_effect=fake_run),
    ):
        reach = inspect_runner(DispatchConfig(), runner_name="codex")

    assert reach.version == "0.145.0"
    assert reach.version_supported is True
    assert reach.credential_detected is True
    assert calls[0][0] == ["/opt/bin/codex", "--version"]
    assert calls[0][1]["timeout"] == 5
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]
    assert calls[1][0] == ["/opt/bin/codex", "login", "status"]
    assert calls[1][1]["stdout"] is subprocess.DEVNULL
    assert calls[1][1]["stderr"] is subprocess.DEVNULL
    assert calls[1][1]["timeout"] == 5
    assert calls[1][1]["env"] == calls[0][1]["env"]


def test_inspect_codex_distinguishes_unsupported_version_and_login():
    results = [
        subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout="codex-cli 0.144.9\n", stderr=""
        ),
        subprocess.CompletedProcess(["codex", "login", "status"], 1),
    ]
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/opt/bin/codex"),
        patch("kew.subprocess_utils.subprocess.run", side_effect=results),
    ):
        reach = inspect_runner(DispatchConfig(), runner_name="codex")
    assert reach.binary_present is True
    assert reach.version == "0.144.9"
    assert reach.version_supported is False
    assert reach.credential_detected is False


def test_inspect_codex_skips_login_when_version_cannot_be_parsed():
    version_result = subprocess.CompletedProcess(
        ["codex", "--version"], 0, stdout="unexpected version", stderr=""
    )
    with (
        patch("kew.subprocess_utils.shutil.which", return_value="/opt/bin/codex"),
        patch(
            "kew.subprocess_utils.subprocess.run", return_value=version_result
        ) as invoke,
    ):
        reach = inspect_runner(DispatchConfig(), runner_name="codex")
    assert reach.version is None
    assert reach.version_supported is False
    assert reach.credential_detected is False
    invoke.assert_called_once()


@pytest.mark.serial
@pytest.mark.parametrize("api", ["run", "popen"])
@pytest.mark.parametrize("explicit_env", [False, True])
def test_round6_real_grandchild_drops_inherited_key(monkeypatch, api, explicit_env):
    import json
    import os

    from kew import subprocess_utils

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "bounded-secret-1944")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
    env = dict(os.environ) if explicit_env else None
    command = ["python", "-c", "import json, os; print(json.dumps(sorted(os.environ)))"]
    if api == "run":
        output = subprocess_utils.run(command, env=env, timeout=10).stdout
    else:
        proc = subprocess_utils.popen(command, env=env)
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
    names = json.loads(output)
    assert "OPENROUTER_API_KEY" not in names
    assert "KEW_ISSUED_RUN_KEY_HASH" not in names
    assert "OPENROUTER_PROVISIONING_KEY" not in names
    assert os.environ["OPENROUTER_API_KEY"] == "bounded-secret-1944"


def test_round6_generic_explicit_environment_cannot_hide_ambient_marker(monkeypatch):
    from kew.subprocess_utils import child_environment

    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    assert child_environment({"OPENROUTER_API_KEY": "bounded-secret-1944"}) == {}


# --- subprocess_deadline (#2499 review) ---------------------------------------


def test_subprocess_deadline_caps_an_unbounded_run(monkeypatch) -> None:
    seen: list[float | None] = []

    def record(cmd, *, timeout=None, **_kwargs):
        seen.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess_utils, "resolve_cmd", lambda cmd: cmd)
    monkeypatch.setattr(subprocess_utils.subprocess, "run", record)
    with subprocess_utils.subprocess_deadline(30):
        subprocess_utils.run(["git", "status"], check=False)
        subprocess_utils.run(["git", "status"], check=False, timeout=2)
        with subprocess_utils.subprocess_deadline(3600):  # never extends the outer
            subprocess_utils.run(["git", "status"], check=False)
    subprocess_utils.run(["git", "status"], check=False)

    assert seen[0] is not None and 0 < seen[0] <= 30
    assert seen[1] == 2
    assert seen[2] is not None and seen[2] <= 30
    assert seen[3] is None


def test_subprocess_deadline_refuses_to_launch_once_it_has_passed(
    monkeypatch,
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(subprocess_utils, "resolve_cmd", lambda cmd: cmd)
    monkeypatch.setattr(
        subprocess_utils.subprocess, "run", lambda cmd, **_k: launched.append(cmd)
    )
    with (
        subprocess_utils.subprocess_deadline(-1),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        subprocess_utils.run(["git", "status"], check=False)
    assert launched == []


def _record_direct_probe_timeouts(monkeypatch) -> list[float | None]:
    seen: list[float | None] = []

    def record(cmd, *, timeout=None, **_kwargs):
        seen.append(timeout)
        stdout = '{"loggedIn": true}' if "auth" in cmd else "codex-cli 99.0.0\n"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(subprocess_utils.subprocess, "run", record)
    monkeypatch.setattr(subprocess_utils.shutil, "which", lambda _n: "/bin/claude")
    return seen


def _call_every_direct_probe() -> None:
    subprocess_utils._probe_claude_cli_auth()
    subprocess_utils._probe_codex("/bin/codex")
    subprocess_utils._probe_runner_version("/bin/runner")


def test_direct_version_probes_honour_the_remaining_deadline(monkeypatch) -> None:
    """Round 2: a probe that calls ``subprocess.run`` itself still ends in time.

    Tested against the time LEFT: a deadline with under a second remaining
    must cap every probe below its own 5 s, not only below the overall budget.
    """
    seen = _record_direct_probe_timeouts(monkeypatch)
    with subprocess_utils.subprocess_deadline(0.5):
        _call_every_direct_probe()

    assert len(seen) == 4
    assert all(timeout is not None and 0 < timeout <= 0.5 for timeout in seen)


def test_direct_version_probes_do_not_launch_once_the_deadline_has_passed(
    monkeypatch,
) -> None:
    seen = _record_direct_probe_timeouts(monkeypatch)
    with subprocess_utils.subprocess_deadline(-1):
        assert subprocess_utils._probe_claude_cli_auth() is False
        assert subprocess_utils._probe_codex("/bin/codex") == (None, False, False)
        assert subprocess_utils._probe_runner_version("/bin/runner") is None
    assert seen == []


def test_direct_version_probes_keep_their_own_timeout_without_a_deadline(
    monkeypatch,
) -> None:
    seen = _record_direct_probe_timeouts(monkeypatch)
    _call_every_direct_probe()
    assert seen == [5, 5, 5, 5]


def test_register_tools_reports_missing_tools_without_printing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        subprocess_utils, "_REQUIRED_TOOLS", {"kew-no-such-tool": "https://x.test"}
    )
    assert subprocess_utils.register_tools() == {"kew-no-such-tool": "https://x.test"}
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit):
        subprocess_utils.preflight_tools()
    assert "kew-no-such-tool not found" in capsys.readouterr().err


def test_agent_image_git_history_read_is_bounded(monkeypatch, tmp_path) -> None:
    from kew import sandbox

    def hang(cmd, **kwargs):
        assert kwargs.get("timeout") is not None
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(sandbox.subprocess_utils, "run", hang)
    assert sandbox._get_dockerfile_ts(tmp_path) is None
