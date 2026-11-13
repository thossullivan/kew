"""The ``openshell`` CLI client (epic ``openshell-runtime``, node N2).

Everything here runs against **recorded CLI output** under
``tests/fixtures/openshell/``: no test contacts a gateway, and none needs the
CLI installed. The seam is ``kew.subprocess_utils.run`` / ``.popen`` — the same
chokepoint the docker calls go through — so a fake there sees the exact argv
kew would launch, which is what most of these tests assert on.

The load-bearing assertion is :func:`assert_no_credential_value_in_argv`: a
credential value must never appear in an argument, because the spike found that
``--credential NAME=$(…)`` publishes the token to every local ``ps``. That
assertion is itself tested — ``test_the_argv_check_fails_on_a_credential_in_argv``
feeds it a poisoned call and requires it to fail — so a sweep that silently
stopped looking cannot pass. What the sweep is *applied to* is guarded the same
way: :func:`every_call` is compared against the client's public surface read
off the class, so a method added by a later node of this epic fails
:class:`TestTheSweepCoversTheWholeSurface` instead of escaping both sweeps.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kew import openshell, subprocess_utils
from kew.config import DispatchConfig, OpenShellConfig
from kew.openshell import (
    Gateway,
    OpenShellClient,
    OpenShellError,
    OpenShellGatewayUnreachable,
    OpenShellMissing,
    OpenShellNotFound,
    OpenShellOutcomeUnknown,
    OpenShellTimeout,
    OpenShellVersionMismatch,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openshell"

#: A stand-in for a real provider credential. Only ever placed in the
#: environment — the whole point is that it must not reach argv.
CREDENTIAL_NAME = "ANTHROPIC_API_KEY"
CREDENTIAL_VALUE = "openshell-spike-placeholder-credential-value"

#: The endpoint the client is pointed at. A literal in a test, never a default
#: in the shipped config: nothing here opens a connection.
TEST_GATEWAY = "http://gateway.invalid:8080"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _subcommand(command: Sequence[str]) -> tuple[str, ...]:
    """The argv with the CLI name and any gateway flag pair removed."""
    rest = list(command[1:])
    if rest[:1] == [openshell.GATEWAY_FLAG]:
        rest = rest[2:]
    return tuple(rest)


def _recorded_stdout(command: Sequence[str]) -> str:
    """The recorded output v0.1.2 printed for this subcommand."""
    tokens = _subcommand(command)
    if "--version" in tokens:
        return _fixture("version.txt")
    if tokens[:1] == ("status",):
        return _fixture("status.json" if "json" in tokens else "status.txt")
    if tokens[:2] == ("sandbox", "get"):
        return _fixture("sandbox_get_running.json")
    if tokens[:2] == ("provider", "get"):
        return _fixture("provider_get.json")
    if tokens[:1] == ("logs",):
        return "2026-10-03T18:04:12Z started\n"
    return ""


@dataclass
class Call:
    """One recorded invocation: argv, deadline, and the child environment."""

    command: tuple[str, ...]
    timeout: float | None
    env: dict[str, str] | None
    retain: tuple[str, ...]
    streaming: bool = False


@dataclass
class FakeCli:
    """Stands in for the subprocess chokepoint, recording every call."""

    stdout: str | None = None
    stderr: str = ""
    returncode: int = 0
    raises: BaseException | None = None
    overrun: bool = False
    calls: list[Call] = field(default_factory=list)
    processes: list[StubProcess] = field(default_factory=list)

    def run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        retain_credentials: Sequence[str] = (),
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(Call(tuple(cmd), timeout, env, tuple(retain_credentials)))
        if self.raises is not None:
            raise self.raises
        out = _recorded_stdout(cmd) if self.stdout is None else self.stdout
        return subprocess.CompletedProcess(cmd, self.returncode, out, self.stderr)

    def popen(self, cmd: list[str], **kwargs: object) -> StubProcess:
        self.calls.append(Call(tuple(cmd), None, None, (), streaming=True))
        if self.raises is not None:
            raise self.raises
        process = StubProcess(kwargs, overrun=self.overrun)
        self.processes.append(process)
        return process

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeCli:
        monkeypatch.setattr(subprocess_utils, "run", self.run)
        monkeypatch.setattr(subprocess_utils, "popen", self.popen)
        return self


class StubProcess:
    """A streaming child that never existed, with its launch kwargs kept.

    ``communicate`` is here because that is what bounds the real wait: it
    empties the pipe while it waits. A stub that only answered ``wait`` would
    let the undrained-pipe deadlock back in unseen, which is why the two tests
    that guard it drive a real child instead (:func:`real_exec`).
    """

    def __init__(self, kwargs: dict[str, object], *, overrun: bool = False) -> None:
        self.kwargs = kwargs
        self.stdout = io.StringIO("ok\n")
        self.killed = False
        self.reaped = False
        self.returncode: int | None = None
        self._overrun = overrun

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        if self._overrun:
            raise subprocess.TimeoutExpired(
                "openshell", timeout or 0.0, output=self.stdout.read()
            )
        output = self.stdout.read()
        self.returncode = 0
        return output, None

    def wait(self, timeout: float | None = None) -> int:
        if self._overrun and not self.killed:
            raise subprocess.TimeoutExpired("openshell", timeout or 0.0)
        self.reaped = True
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True


#: Comfortably more than any pipe buffer (64KiB on Linux, 16KiB on macOS), so
#: a child writing this much blocks unless something is reading the other end.
PIPE_FLOODING_BYTES = 1_000_000


@contextlib.contextmanager
def real_exec(script: str) -> Iterator[openshell.OpenShellExec]:
    """An :class:`OpenShellExec` over a real child running ``script``.

    Real, because both failures it guards belong to the OS and not to a stub: a
    pipe buffer that fills, and a signalled child that nobody reaped. Killed on
    the way out, so a failing assertion cannot leave one running.
    """
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield openshell.OpenShellExec(
            (openshell.CLI, "sandbox", "exec", "kew-run-7f3c1a"), process
        )
    finally:
        process.kill()
        process.wait()


def make_client(
    *,
    gateway: str = TEST_GATEWAY,
    version: str = "0.1.2",
    environment: dict[str, str] | None = None,
) -> OpenShellClient:
    return OpenShellClient(
        gateway=Gateway(gateway, openshell.GATEWAY_FROM_CONFIG),
        expected_version=version,
        environment={CREDENTIAL_NAME: CREDENTIAL_VALUE}
        if environment is None
        else environment,
    )


#: Methods of :class:`OpenShellClient` that launch no CLI call, so they have
#: no argv to keep a credential out of and no deadline to carry.
#: ``from_settings`` is a constructor: it reads ``[sandbox.openshell]`` and
#: builds a client without contacting anything.
NOT_A_CLI_CALL = frozenset({"from_settings"})


def public_calls_on_the_client(
    client_class: type = OpenShellClient,
) -> frozenset[str]:
    """Every public method of the client, read off the class.

    The inventory comes from the class rather than from a list kept here,
    because the two sweeps below are only as wide as what they are handed: a
    method added by a later node of this epic has to appear in
    :func:`every_call` or fail ``test_the_sweep_calls_every_public_method``,
    rather than quietly sitting outside the no-credential-in-argv and
    every-call-is-bounded guards.
    """
    return (
        frozenset(
            name
            for name, _ in inspect.getmembers(client_class, callable)
            if not name.startswith("_")
        )
        - NOT_A_CLI_CALL
    )


def every_call(client: OpenShellClient) -> dict[str, Callable[[], object]]:
    """One zero-argument invocation of each public call, keyed by its name."""
    return {
        "version": client.version,
        "check_version": client.check_version,
        "status": client.status,
        "report": client.report,
        "sandbox_create": lambda: client.sandbox_create(
            "kew-run-7f3c1a",
            image="kew-agent:docker-6ed2f5d69acd",
            providers=("kew-anthropic",),
            policy_path=Path("policy.yaml"),
            labels={"kew.run": "7f3c1a"},
        ),
        "sandbox_get": lambda: client.sandbox_get("kew-run-7f3c1a"),
        "sandbox_upload": lambda: client.sandbox_upload(
            "kew-run-7f3c1a", Path("checkout"), "/work"
        ),
        "sandbox_download": lambda: client.sandbox_download(
            "kew-run-7f3c1a", "/work/out.txt", Path("out.txt")
        ),
        "sandbox_exec": lambda: client.sandbox_exec(
            "kew-run-7f3c1a", workdir="/work", argv=["claude", "-p"]
        ),
        "sandbox_run": lambda: client.sandbox_run(
            "kew-run-7f3c1a", workdir="/", argv=["/bin/sh", "-c", "true"]
        ),
        "logs": lambda: client.logs("kew-run-7f3c1a", since="5m"),
        "provider_create": lambda: client.provider_create(
            "kew-anthropic", profile="anthropic", env_names=(CREDENTIAL_NAME,)
        ),
        "provider_get": lambda: client.provider_get("kew-anthropic"),
        "provider_delete": lambda: client.provider_delete("kew-anthropic"),
        "sandbox_delete": lambda: client.sandbox_delete("kew-run-7f3c1a"),
    }


def exercise_every_call(client: OpenShellClient) -> None:
    """Call every public method on the client exactly once."""
    for call in every_call(client).values():
        call()


def assert_no_credential_value_in_argv(calls: Sequence[Call]) -> None:
    """No argument of any recorded call may contain a credential value."""
    assert calls, "no calls were recorded, so nothing was checked"
    for call in calls:
        for argument in call.command:
            assert CREDENTIAL_VALUE not in argument, (
                f"credential value reached argv: {' '.join(call.command)}"
            )


class TestSandboxGetJson:
    """``sandbox get -o json`` is parsed; a payload kew cannot read raises."""

    def test_parses_the_recorded_running_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        sandbox = make_client().sandbox_get("kew-run-7f3c1a")

        assert sandbox.name == "kew-run-7f3c1a"
        assert sandbox.status == "running"
        assert sandbox.identity == "sbx_01HN4Q8ZJ2WME5T"
        assert sandbox.image == "kew-agent:docker-6ed2f5d69acd"
        assert sandbox.labels == {"kew.run": "7f3c1a", "kew.issue": "2316"}
        assert sandbox.raw["created_at"] == "2026-10-03T18:04:11Z"
        assert _subcommand(cli.calls[0].command) == (
            "sandbox",
            "get",
            "kew-run-7f3c1a",
            "-o",
            "json",
        )

    def test_accepts_the_state_and_sandbox_id_spellings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v0.1.2 spells these two ways across subcommands."""
        FakeCli(stdout=_fixture("sandbox_get_stopped.json")).install(monkeypatch)
        sandbox = make_client().sandbox_get("kew-run-91bd20")

        assert sandbox.status == "stopped"
        assert sandbox.identity == "sbx_01HN4QBB7KQ0R3V"
        assert sandbox.labels == {}

    def test_json_without_a_status_raises_instead_of_inventing_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = json.loads(_fixture("sandbox_get_running.json"))
        del payload["status"]
        FakeCli(stdout=json.dumps(payload)).install(monkeypatch)

        with pytest.raises(OpenShellError, match="no status / state field"):
            make_client().sandbox_get("kew-run-7f3c1a")

    def test_output_that_is_not_json_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(stdout="NAME STATUS\nkew-run-7f3c1a running\n").install(monkeypatch)

        with pytest.raises(OpenShellError, match="did not return JSON"):
            make_client().sandbox_get("kew-run-7f3c1a")

    def test_json_array_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeCli(stdout="[]").install(monkeypatch)

        with pytest.raises(OpenShellError, match="JSON list, not an object"):
            make_client().sandbox_get("kew-run-7f3c1a")

    def test_provider_get_parses_its_credential_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli().install(monkeypatch)
        provider = make_client().provider_get("kew-anthropic")

        assert provider.profile == "anthropic"
        assert provider.credential_names == (CREDENTIAL_NAME,)

    def test_provider_without_a_credentials_list_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = json.loads(_fixture("provider_get.json"))
        del payload["credentials"]
        FakeCli(stdout=json.dumps(payload)).install(monkeypatch)

        with pytest.raises(OpenShellError, match="no credentials list"):
            make_client().provider_get("kew-anthropic")


class TestErrorMapping:
    """A non-zero exit becomes the narrowest error its stderr supports."""

    @pytest.mark.parametrize(
        ("fixture", "expected", "message"),
        (
            (
                "error_sandbox_not_found.txt",
                OpenShellNotFound,
                "no such sandbox or provider",
            ),
            (
                "error_gateway_unreachable.txt",
                OpenShellGatewayUnreachable,
                "gateway is unreachable",
            ),
            ("error_policy_rejected.txt", OpenShellError, "refused the call"),
        ),
    )
    def test_recorded_stderr_maps_to_its_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fixture: str,
        expected: type[OpenShellError],
        message: str,
    ) -> None:
        FakeCli(stdout="", stderr=_fixture(fixture), returncode=1).install(monkeypatch)

        with pytest.raises(expected, match=message) as caught:
            make_client().sandbox_get("kew-run-missing")
        assert caught.value.exit_code == 1

    @pytest.mark.parametrize(
        "stderr",
        (
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "read tcp 127.0.0.1:54112->127.0.0.1:8080: read: connection reset by peer",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "context deadline exceeded (Client.Timeout awaiting headers)",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "read tcp 127.0.0.1:54112->127.0.0.1:8080: i/o timeout",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "EOF",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "unexpected EOF",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "write tcp 127.0.0.1:54112->127.0.0.1:8080: write: broken pipe",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "http: server closed idle connection",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "http2: server sent GOAWAY and closed the connection; "
            "LastStreamID=1, ErrCode=NO_ERROR",
            'Error: Delete "http://gateway.invalid:8080/v1/sandboxes/kew-run-7f3c1a": '
            "use of closed network connection",
        ),
    )
    def test_a_break_after_the_request_was_sent_is_not_called_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        """A delete cut off mid-reply has usually already deleted the sandbox.

        Reporting these as :class:`OpenShellGatewayUnreachable` would promise the
        caller the gateway was never reached, i.e. that the mutation did not
        happen — the one thing this failure cannot establish.
        """
        FakeCli(stdout="", stderr=stderr, returncode=1).install(monkeypatch)

        with pytest.raises(OpenShellOutcomeUnknown, match="broke after it was sent"):
            make_client().sandbox_delete("kew-run-7f3c1a")

    def test_eof_inside_a_longer_word_is_not_a_severed_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The EOF match is on word boundaries, so an image name cannot trip it.

        A refusal the gateway *answered* must keep saying so: widening it to an
        unknown outcome would send the caller re-reading state after a call that
        provably did nothing.
        """
        FakeCli(
            stdout="",
            stderr="Error: policy rejected image ghcr.io/acme/leofs:1.2",
            returncode=1,
        ).install(monkeypatch)

        with pytest.raises(OpenShellError) as caught:
            make_client().sandbox_delete("kew-run-7f3c1a")
        assert type(caught.value) is OpenShellError

    def test_a_failed_dial_is_still_an_unreachable_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deadline spent *dialling* proves nothing was sent: still never reached."""
        FakeCli(
            stdout="",
            stderr="Error: dial tcp 127.0.0.1:8080: i/o timeout",
            returncode=1,
        ).install(monkeypatch)

        with pytest.raises(OpenShellGatewayUnreachable) as caught:
            make_client().sandbox_delete("kew-run-7f3c1a")
        assert not isinstance(caught.value, OpenShellOutcomeUnknown)

    def test_a_timeout_is_an_unknown_outcome_too(self) -> None:
        """One ``except`` covers both ways a call can end without an answer."""
        assert issubclass(OpenShellTimeout, OpenShellOutcomeUnknown)
        assert not issubclass(OpenShellGatewayUnreachable, OpenShellOutcomeUnknown)

    def test_an_unrecognised_failure_stays_the_base_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the base class: guessing a cause would be worse than naming none."""
        FakeCli(stdout="", stderr="Error: something else", returncode=7).install(
            monkeypatch
        )

        with pytest.raises(OpenShellError) as caught:
            make_client().sandbox_delete("kew-run-7f3c1a")
        assert type(caught.value) is OpenShellError
        assert caught.value.exit_code == 7

    def test_the_error_carries_the_command_and_trimmed_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(stdout="", stderr="x" * 4000, returncode=1).install(monkeypatch)

        with pytest.raises(OpenShellError) as caught:
            make_client().sandbox_delete("kew-run-7f3c1a")
        error = caught.value
        assert error.command[0] == openshell.CLI
        assert openshell.GATEWAY_FLAG in error.command
        assert "kew-run-7f3c1a" in error.command
        assert len(error.stderr) == openshell.MAX_STDERR_CHARS + 1
        assert error.stderr.endswith("…")

    def test_a_timeout_is_typed_and_names_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expired = subprocess.TimeoutExpired(
            "openshell", openshell.PROBE_TIMEOUT_SECONDS, stderr=b"partial output"
        )
        FakeCli(raises=expired).install(monkeypatch)

        with pytest.raises(OpenShellTimeout, match="exceeded 10s") as caught:
            make_client().status()
        assert caught.value.exit_code is None
        assert caught.value.stderr == "partial output"

    def test_an_unavailable_cli_is_typed_rather_than_a_bare_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``resolve_cmd`` path: not allowlisted means not installed."""
        monkeypatch.delitem(subprocess_utils.TOOL_PATHS, openshell.CLI, raising=False)

        with pytest.raises(OpenShellMissing, match="not installed"):
            make_client().status()

    def test_an_unlaunchable_cli_is_typed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(raises=OSError(13, "Permission denied")).install(monkeypatch)

        with pytest.raises(OpenShellMissing, match="Permission denied"):
            make_client().status()


class TestGatewayAndVersion:
    """Which gateway is contacted, and which CLI is allowed to answer."""

    def test_environment_wins_over_config(self) -> None:
        gateway = openshell.resolve_gateway(
            "http://configured.invalid", {openshell.GATEWAY_ENV: TEST_GATEWAY}
        )
        assert gateway == Gateway(TEST_GATEWAY, openshell.GATEWAY_FROM_ENVIRONMENT)

    def test_config_is_used_when_the_environment_is_unset(self) -> None:
        gateway = openshell.resolve_gateway(TEST_GATEWAY, {})
        assert gateway == Gateway(TEST_GATEWAY, openshell.GATEWAY_FROM_CONFIG)

    def test_neither_input_leaves_the_cli_default_and_passes_no_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        client = OpenShellClient.from_settings(OpenShellConfig(), {})

        assert client.gateway == Gateway("", openshell.GATEWAY_FROM_CLI_DEFAULT)
        client.status()
        assert openshell.GATEWAY_FLAG not in cli.calls[0].command

    def test_from_settings_reads_the_config_section(self) -> None:
        settings = OpenShellConfig(gateway=f" {TEST_GATEWAY} ", version="0.1.2")
        client = OpenShellClient.from_settings(settings, {})

        assert client.gateway.endpoint == TEST_GATEWAY
        assert client.expected_version == "0.1.2"

    def test_the_configured_gateway_is_passed_on_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        exercise_every_call(make_client())

        for call in cli.calls:
            assert call.command[1:3] == (openshell.GATEWAY_FLAG, TEST_GATEWAY)

    def test_version_is_read_from_the_recorded_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli().install(monkeypatch)
        assert make_client().version() == "0.1.2"

    def test_a_cli_that_prints_no_version_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(stdout="openshell\n").install(monkeypatch)

        with pytest.raises(OpenShellError, match="printed no version"):
            make_client().version()

    def test_check_version_refuses_a_cli_the_client_was_not_written_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli().install(monkeypatch)

        with pytest.raises(OpenShellVersionMismatch, match="written against 0.2.0"):
            make_client(version="0.2.0").check_version()

    def test_status_carries_what_the_gateway_said(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli().install(monkeypatch)
        status = make_client().status()

        assert status.gateway.endpoint == TEST_GATEWAY
        assert status.detail == "gateway: https://127.0.0.1:8443"

    def test_a_silent_status_is_not_proof_of_a_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(stdout="  \n").install(monkeypatch)

        with pytest.raises(OpenShellError, match="gateway is unproven"):
            make_client().status()


class TestArgvShape:
    """What kew actually launches, pinned argument by argument."""

    def test_create_carries_image_providers_policy_and_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        make_client().sandbox_create(
            "kew-run-7f3c1a",
            image="kew-agent:docker-6ed2f5d69acd",
            providers=("kew-anthropic",),
            policy_path=Path("policy.yaml"),
            labels={"kew.run": "7f3c1a"},
        )

        assert _subcommand(cli.calls[0].command) == (
            "sandbox",
            "create",
            "kew-run-7f3c1a",
            "--image",
            "kew-agent:docker-6ed2f5d69acd",
            "--provider",
            "kew-anthropic",
            "--policy",
            "policy.yaml",
            "--label",
            "kew.run=7f3c1a",
        )

    def test_upload_always_disables_gitignore_filtering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        make_client().sandbox_upload("kew-run-7f3c1a", Path("checkout"), "/work")

        assert _subcommand(cli.calls[0].command) == (
            "sandbox",
            "upload",
            "kew-run-7f3c1a",
            "checkout",
            "/work",
            openshell.NO_GIT_IGNORE_FLAG,
        )

    def test_download_copies_back_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = FakeCli().install(monkeypatch)
        make_client().sandbox_download(
            "kew-run-7f3c1a", "/work/out.txt", Path("out.txt")
        )

        assert _subcommand(cli.calls[0].command) == (
            "sandbox",
            "download",
            "kew-run-7f3c1a",
            "/work/out.txt",
            "out.txt",
        )

    def test_logs_omits_since_when_it_is_not_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        client = make_client()
        client.logs("kew-run-7f3c1a")
        client.logs("kew-run-7f3c1a", since="5m")

        assert _subcommand(cli.calls[0].command) == ("logs", "kew-run-7f3c1a")
        assert _subcommand(cli.calls[1].command) == (
            "logs",
            "kew-run-7f3c1a",
            "--since",
            "5m",
        )

    @pytest.mark.parametrize("name", ("", "   ", "--gateway-endpoint=evil.invalid"))
    def test_an_argument_that_reads_as_a_flag_is_refused(self, name: str) -> None:
        """A sandbox name sits beside real flags; it may not become one."""
        with pytest.raises(ValueError, match="sandbox name"):
            make_client().sandbox_get(name)

    def test_a_label_key_cannot_smuggle_a_second_pair(self) -> None:
        with pytest.raises(ValueError, match="label key"):
            make_client().sandbox_create(
                "kew-run-7f3c1a", image="i", labels={"a=b": "c"}
            )


class TestExec:
    """``sandbox exec`` streams, with stdin closed and a caller-owned deadline."""

    def test_stdin_is_closed_and_output_is_streamed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        handle = make_client().sandbox_exec(
            "kew-run-7f3c1a", workdir="/work", argv=["claude", "-p", "--verbose"]
        )

        assert _subcommand(handle.command) == (
            "sandbox",
            "exec",
            "kew-run-7f3c1a",
            "--workdir",
            "/work",
            "--",
            "claude",
            "-p",
            "--verbose",
        )
        kwargs = cli.processes[0].kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.STDOUT
        assert handle.stdout.read() == "ok\n"
        assert handle.wait(timeout=1.0) == 0

    def test_an_overrunning_exec_is_killed_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeCli(overrun=True).install(monkeypatch)
        handle = make_client().sandbox_exec(
            "kew-run-7f3c1a", workdir="/work", argv=["sleep", "600"]
        )

        with pytest.raises(OpenShellTimeout, match="exceeded 30s"):
            handle.wait(timeout=30.0)
        assert isinstance(handle.process, StubProcess)
        assert handle.process.killed
        assert handle.process.reaped, "a killed exec must also be waited on"

    def test_a_chatty_exec_is_not_deadlocked_by_its_own_pipe(self) -> None:
        """Shown to fail: with ``wait`` calling ``Popen.wait`` on an undrained
        pipe, this child — which writes 1MB and exits at once — filled the pipe
        buffer, blocked in ``write`` forever, and was reported as a timeout at
        the deadline. The exec's bound is its runtime, not its output size.
        """
        flood = f"import sys; sys.stdout.write('x' * {PIPE_FLOODING_BYTES})"
        with real_exec(flood) as handle:
            assert handle.wait(timeout=10.0) == 0
            assert len("".join(handle.drained)) == PIPE_FLOODING_BYTES

    def test_an_overrun_leaves_no_zombie_behind(self) -> None:
        """Shown to fail: ``kill`` only posts the signal, so without the reap
        ``returncode`` stayed ``None`` — a zombie child still holding its pipe
        for as long as the dispatch lives.
        """
        with real_exec("import time; time.sleep(600)") as handle:
            with pytest.raises(OpenShellTimeout, match="was killed"):
                handle.wait(timeout=0.2)

            assert handle.process.returncode is not None

    def test_the_exec_client_is_its_own_process_group_leader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shown to fail: the launch registers this pid as the run's pgid.

        ``dispatch_sandbox._register_openshell_client`` writes the client's pid
        to the run row as ``attempt_pgid``. Started in kew's own group, that
        pid is a pgid of nothing: a later process holding only the row cannot
        ``killpg`` the live client, and ``process_groups.attempts_are_dead``
        reports a running attempt as dead — so a fleet stop misses it and a
        reconcile is free to finalize it (PR #2401 review).

        Read off the launch keywords here; the test below asserts the same
        thing against the kernel, on a real child.
        """
        cli = FakeCli().install(monkeypatch)

        make_client().sandbox_exec("kew-run-7f3c1a", workdir="/work", argv=["claude"])

        assert cli.processes[0].kwargs["start_new_session"] is True

    def test_a_real_exec_client_is_signallable_as_a_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same assertion against the kernel, on a real parked child.

        Only the resolved executable is replaced — the launch itself is the
        shipped one, so the group this asserts on is the one the chokepoint
        actually created and not a recorded keyword.
        """
        parked = [sys.executable, "-c", "import time; time.sleep(30)"]
        monkeypatch.setattr(subprocess_utils, "resolve_cmd", lambda _cmd: parked)

        handle = make_client().sandbox_exec(
            "kew-run-7f3c1a", workdir="/work", argv=["claude"]
        )
        process = handle.process
        try:
            assert os.getpgid(process.pid) == process.pid
            os.killpg(process.pid, 0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    def test_exec_needs_a_command(self) -> None:
        with pytest.raises(ValueError, match="needs a command"):
            make_client().sandbox_exec("kew-run-7f3c1a", workdir="/work", argv=[])

    def test_an_unavailable_cli_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(subprocess_utils.TOOL_PATHS, openshell.CLI, raising=False)

        with pytest.raises(OpenShellMissing, match="not installed"):
            make_client().sandbox_exec("kew-run-7f3c1a", workdir="/work", argv=["true"])

    def test_only_the_allowlisted_names_cross_into_the_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shown to fail: this exec's environment IS the agent's environment.

        v0.1.2 forwards the client's environment into the sandbox, and this
        method used to forward whatever mapping it was handed. Both callers
        handed it the host environment minus a credential DENYLIST, so a host
        secret kew does not enumerate — ``ACME_DEPLOY_TOKEN`` here — reached
        the contained agent, and so did the proxy variables ``[sandbox]
        proxy_environment = "none"`` blanks on the docker boundary. The gateway
        endpoint is left out too: it travels as ``--gateway-endpoint``, and the
        agent being contained has no business learning it (PR #2401 review).
        """
        cli = FakeCli().install(monkeypatch)
        client = make_client(
            environment={
                "PATH": "/work/bin:.:/usr/bin",
                "HOME": "/home/operator",
                "ACME_DEPLOY_TOKEN": "not-enumerated-by-kew",
                "HTTPS_PROXY": "http://proxy.invalid:3128",
                openshell.GATEWAY_ENV: TEST_GATEWAY,
            }
        )

        client.sandbox_exec("kew-run-7f3c1a", workdir="/work", argv=["claude"])

        assert cli.processes[0].kwargs["env"] == {
            "PATH": openshell.SANDBOX_PATH,
            "HOME": "/home/operator",
        }

    def test_the_caller_may_add_the_credential_names_it_declares_and_no_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``retain`` is the whole of what a caller adds to the allowlist.

        The launch binds the runner's credential variables to the broker
        placeholder and names them in ``retain``; those cross. A name in
        neither list does not, so the narrowing cannot be undone by the mapping
        a caller hands over — which is why it lives here, at the one call that
        reaches into a sandbox, and not in each caller.
        """
        cli = FakeCli().install(monkeypatch)

        make_client().sandbox_exec(
            "kew-run-7f3c1a",
            workdir="/work",
            argv=["claude"],
            env={
                "PATH": "/work/bin:.:/usr/bin",
                CREDENTIAL_NAME: CREDENTIAL_VALUE,
                "ACME_DEPLOY_TOKEN": "not-enumerated-by-kew",
            },
            retain=(CREDENTIAL_NAME,),
        )

        assert cli.processes[0].kwargs["env"] == {
            "PATH": openshell.SANDBOX_PATH,
            CREDENTIAL_NAME: CREDENTIAL_VALUE,
        }


class TestTheSweepCoversTheWholeSurface:
    """The sweeps guard the calls; this guards the sweep.

    Both sweeps below drive the client through :func:`every_call`, so a call
    missing from that mapping is a call neither guard ever sees. The inventory
    it is compared against is read off the class, not written down twice.
    """

    def test_the_sweep_calls_every_public_method(self) -> None:
        assert set(every_call(make_client())) == public_calls_on_the_client()

    def test_a_call_added_without_the_sweep_is_caught(self) -> None:
        """Shown to fail: a later node's method, absent from ``every_call``."""

        class WithANewCall(OpenShellClient):
            def sandbox_restart(self, name: str) -> None:
                """The shape a later node of this epic would add."""

        surface = public_calls_on_the_client(WithANewCall)
        assert "sandbox_restart" in surface
        assert set(every_call(make_client())) != surface


class TestCredentialsNeverReachArgv:
    """The spike's finding, enforced: a token in argv is a token in ``ps``."""

    def test_provider_create_passes_the_name_and_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        make_client().provider_create(
            "kew-anthropic", profile="anthropic", env_names=(CREDENTIAL_NAME,)
        )

        call = cli.calls[0]
        assert _subcommand(call.command) == (
            "provider",
            "create",
            "kew-anthropic",
            "--profile",
            "anthropic",
            openshell.CREDENTIAL_ENV_FLAG,
            CREDENTIAL_NAME,
        )
        assert_no_credential_value_in_argv(cli.calls)
        assert call.env is not None
        assert call.env[CREDENTIAL_NAME] == CREDENTIAL_VALUE
        assert call.retain == (CREDENTIAL_NAME,)

    def test_no_call_in_the_module_puts_a_value_in_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shown to fail: with ``provider_create`` temporarily emitting
        ``--credential-env NAME=<value>`` — the spike's argv — this test and
        the one above both failed, and passed again once reverted.
        """
        cli = FakeCli().install(monkeypatch)
        exercise_every_call(make_client())

        assert len(cli.calls) == len(public_calls_on_the_client())
        assert_no_credential_value_in_argv(cli.calls)

    def test_the_argv_check_fails_on_a_credential_in_argv(self) -> None:
        """The check itself, shown failing on the argv the spike warned about."""
        poisoned = Call(
            command=(
                openshell.CLI,
                "provider",
                "create",
                "kew-anthropic",
                "--credential",
                f"{CREDENTIAL_NAME}={CREDENTIAL_VALUE}",
            ),
            timeout=openshell.CONTROL_TIMEOUT_SECONDS,
            env=None,
            retain=(),
        )

        with pytest.raises(AssertionError, match="credential value reached argv"):
            assert_no_credential_value_in_argv([poisoned])

    def test_the_argv_check_fails_when_nothing_was_recorded(self) -> None:
        with pytest.raises(AssertionError, match="nothing was checked"):
            assert_no_credential_value_in_argv([])

    def test_an_unset_credential_is_refused_before_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        client = make_client(environment={CREDENTIAL_NAME: "   "})

        with pytest.raises(ValueError, match=f"unset .*{CREDENTIAL_NAME}"):
            client.provider_create(
                "kew-anthropic", profile="anthropic", env_names=(CREDENTIAL_NAME,)
            )
        assert cli.calls == []

    def test_a_credential_a_child_may_not_hold_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Management authority is stripped from every child environment."""
        from kew.credential_issuer import MANAGEMENT_CREDENTIAL_ENVS

        management = sorted(MANAGEMENT_CREDENTIAL_ENVS)[0]
        cli = FakeCli().install(monkeypatch)
        client = make_client(environment={management: CREDENTIAL_VALUE})

        with pytest.raises(ValueError, match="cannot be forwarded"):
            client.provider_create(
                "kew-anthropic", profile="anthropic", env_names=(management,)
            )
        assert cli.calls == []

    @pytest.mark.parametrize(
        "name",
        (f"{CREDENTIAL_NAME}={CREDENTIAL_VALUE}", "lowercase_name", "", "A B"),
    )
    def test_only_a_plain_environment_variable_name_is_accepted(
        self, name: str
    ) -> None:
        with pytest.raises(ValueError, match="environment variable NAME"):
            make_client().provider_create(
                "kew-anthropic", profile="anthropic", env_names=(name,)
            )

    def test_a_provider_with_no_credential_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one credential"):
            make_client().provider_create(
                "kew-anthropic", profile="anthropic", env_names=()
            )


class TestEveryCallIsBounded:
    """No call may block forever: a hung gateway must become an error."""

    def test_every_blocking_call_passes_a_positive_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        exercise_every_call(make_client())

        streaming = [call for call in cli.calls if call.streaming]
        blocking = [call for call in cli.calls if not call.streaming]
        assert len(streaming) == 1, "sandbox exec is the only streaming call"
        assert len(blocking) == len(public_calls_on_the_client()) - 1
        for call in blocking:
            assert isinstance(call.timeout, float) and call.timeout > 0, call.command

    def test_transfers_get_the_longest_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = FakeCli().install(monkeypatch)
        client = make_client()
        client.status()
        client.sandbox_delete("kew-run-7f3c1a")
        client.sandbox_upload("kew-run-7f3c1a", Path("checkout"), "/work")

        assert [call.timeout for call in cli.calls] == [
            openshell.PROBE_TIMEOUT_SECONDS,
            openshell.CONTROL_TIMEOUT_SECONDS,
            openshell.TRANSFER_TIMEOUT_SECONDS,
        ]


class TestConfigSection:
    """``[sandbox.openshell]`` — and that it points nowhere by default."""

    def test_defaults_name_no_gateway_and_pin_the_spiked_version(self) -> None:
        settings = OpenShellConfig()
        assert settings.gateway == ""
        assert settings.version == "0.1.2"

    def test_the_section_loads_under_sandbox(self) -> None:
        config = DispatchConfig.model_validate(
            {
                "sandbox": {
                    "runtime": "openshell",
                    "openshell": {"gateway": TEST_GATEWAY, "version": "0.1.3"},
                }
            }
        )
        assert config.sandbox.openshell.gateway == TEST_GATEWAY
        assert config.sandbox.openshell.version == "0.1.3"

    def test_a_blank_version_pin_is_refused(self) -> None:
        with pytest.raises(ValueError, match="version cannot be blank"):
            OpenShellConfig(version="  ")

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            OpenShellConfig.model_validate({"endpoint": TEST_GATEWAY})
