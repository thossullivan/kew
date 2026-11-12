"""The OpenShell launch lifecycle (epic ``openshell-runtime``, node N6).

Nothing here contacts a gateway or needs the ``openshell`` CLI installed: the
N2 client (:class:`kew.openshell.OpenShellClient`) is replaced by
:class:`FakeGateway`, which records every call in order and hands back the
shapes the recorded v0.1.2 fixtures have. That makes the call ORDER assertable,
which is the half of this node a type signature cannot pin — a sandbox exec'd
before its checkout was uploaded runs an agent against an empty tree.

The handle half is exercised against a real
:class:`~kew.openshell_launch.OpenShellSandboxHandle` over a fake stdout, so
the caps are the shipped ones: a wall-clock or transcript-cap breach has to
reach the gateway's DELETE, not docker's ``kill``, and the acknowledgement the
host waits for has to be the nonce the launcher was handed as an argument.

The live test at the bottom is the only one that touches a gateway. It is
skipped unless :data:`LIVE_ENV` opts in, which CI never does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from support.process_facts import is_linux

import kew.openshell_launch as openshell_launch
from kew.config import DispatchConfig
from kew.openshell import (
    Gateway,
    GatewayReport,
    OpenShellNotFound,
    OpenShellOutcomeUnknown,
    Sandbox,
)
from kew.openshell_checkout import realized_checkout_dir
from kew.openshell_egress import DISPATCH_FLOW, REVIEW_FLOW
from kew.openshell_launch import (
    CHECKOUT_PATH,
    FLOW_LABEL,
    LAUNCHER_PATH,
    MODEL_FLAGS,
    RUN_LABEL,
    RUNNER_LABEL,
    RUNNER_PROGRAMS,
    OpenShellLaunchRefused,
    OpenShellSandboxHandle,
    exec_argv,
    launch_openshell_container,
    require_model_flag,
    runner_policy_path,
    runner_program,
    sandbox_name,
)
from kew.sandbox import KillStatus

#: The opt-in for the one test that needs a gateway. Unset everywhere kew's CI
#: runs, so the live test is skipped there without a marker expression.
LIVE_ENV = "KEW_LIVE_OPENSHELL"

#: The gateway's own id for a sandbox, in the shape the recorded fixtures use.
#: Deliberately NOT a 64-hex container identity: the run row records it under
#: the gateway's own shape check, never the docker one.
SANDBOX_ID = "sbx_01HN4Q8ZJ2WME5T"

MODEL = "claude-sonnet-5"
RUN_ID = "run-2320"
SLUG = "2320-openshell"


@dataclass
class Call:
    """One recorded gateway call: its name and its keyword shape."""

    name: str
    detail: dict[str, Any] = field(default_factory=dict)


class FakeGateway:
    """The N2 client's surface, recording calls instead of making them.

    Only the five calls the launch lifecycle makes are implemented. A sixth
    one appearing in this module would be an ``AttributeError`` rather than a
    silent no-op, which is the point of faking the class by hand rather than
    with a ``MagicMock``.
    """

    def __init__(
        self,
        *,
        status: str = "running",
        fail: str = "",
        error: BaseException | None = None,
        endpoint: str = "https://127.0.0.1:8443",
    ) -> None:
        self.calls: list[Call] = []
        self.gateway = Gateway(endpoint, "config")
        self.status = status
        #: Name of the call that should raise, and what it raises.
        self.fail = fail
        self.error = error or RuntimeError("gateway said no")
        self.exec_env: Mapping[str, str] | None = None
        self.exec_retain: Sequence[str] = ()
        self.process = _fake_process()

    @property
    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def _record(self, _call: str, **detail: Any) -> None:
        self.calls.append(Call(_call, detail))
        if self.fail == _call:
            raise self.error

    def check_version(self) -> str:
        return "0.1.2"

    def report(self) -> GatewayReport:
        """The gateway's self-report; its endpoint is what tells two apart."""
        return GatewayReport(
            gateway=self.gateway,
            version="0.1.2",
            tls=True,
            telemetry=False,
            agent_proposals=False,
            agent_approval="manual",
            supervisor_image="openshell-supervisor@sha256:" + "f" * 64,
            sandbox_image="openshell-sandbox@sha256:" + "e" * 64,
            landlock_abi=3,
            raw={"gateway": self.gateway.endpoint},
        )

    def sandbox_create(
        self,
        name: str,
        *,
        image: str,
        providers: Iterable[str] = (),
        policy_path: Path | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._record(
            "sandbox_create",
            name=name,
            image=image,
            providers=tuple(providers),
            policy_path=policy_path,
            # Read NOW: a launch hands over a private copy that is gone once
            # the create returns, and the bytes are what the gateway enforces.
            policy=None if policy_path is None else policy_path.read_bytes(),
            labels=dict(labels or {}),
        )

    def sandbox_get(self, name: str) -> Sandbox:
        self._record("sandbox_get", name=name)
        return Sandbox(
            name=name,
            status=self.status,
            identity=SANDBOX_ID,
            image="kew-agent:docker-6ed2f5d69acd",
            labels={},
            raw={},
        )

    def sandbox_upload(self, name: str, source: Path, destination: str) -> None:
        self._record(
            "sandbox_upload", name=name, source=source, destination=destination
        )

    def sandbox_exec(
        self,
        name: str,
        *,
        workdir: str,
        argv: Sequence[str],
        env: dict[str, str] | None = None,
        retain: Sequence[str] = (),
    ) -> Any:
        self._record("sandbox_exec", name=name, workdir=workdir, argv=tuple(argv))
        self.exec_env = env
        self.exec_retain = tuple(retain)
        return _FakeExec(self.process)

    def sandbox_delete(self, name: str) -> None:
        self._record("sandbox_delete", name=name)


#: The production launch gate, kept before the autouse fixture replaces it so
#: the tests that are ABOUT the gate can run the real one.
_REAL_LAUNCH_GATE = openshell_launch.require_launch_certification


@pytest.fixture(autouse=True)
def launch_gate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand in for the certification match every launch makes (#2624).

    The lifecycle tests here are about create / upload / exec order with no
    ledger, and the gate would refuse them all. It is recorded instead of
    skipped, and :class:`TestTheLaunchGate` runs the real one.
    """
    calls: list[dict[str, Any]] = []

    def record(store: Any, **kwargs: Any) -> None:
        calls.append({"store": store, **kwargs})

    monkeypatch.setattr(openshell_launch, "require_launch_certification", record)
    return calls


@dataclass
class _FakeExec:
    """The ``OpenShellExec`` shape the handle consumes: a process, nothing more."""

    process: Any


class _FakeStdout:
    """A merged exec stdout that records the bound each read was given."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self.limits: list[int | None] = []

    def readline(self, limit: int | None = None) -> str:
        self.limits.append(limit)
        if not self._lines:
            return ""
        line = self._lines.pop(0)
        return line if limit is None else line[:limit]


class _SilentStdout:
    """An exec that produces nothing until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def readline(self, limit: int | None = None) -> str:
        self.release.wait(timeout=1)
        return ""


def _fake_process(lines: Sequence[str] = ()) -> MagicMock:
    proc = MagicMock()
    proc.stdout = _FakeStdout(lines)
    proc.pid = 4242
    proc.wait.return_value = 0
    return proc


def _config(providers: Mapping[str, str] | None = None) -> DispatchConfig:
    config = DispatchConfig()
    config.sandbox.runtime = "openshell"
    config.sandbox.openshell.providers = dict(
        providers or {"claude-code": "kew-anthropic"}
    )
    return config


def _handle(
    gateway: FakeGateway,
    *,
    lines: Sequence[str] = (),
    stdout: Any = None,
    nonce: str = "n0nce",
    **caps: Any,
) -> OpenShellSandboxHandle:
    proc = _fake_process(lines)
    if stdout is not None:
        proc.stdout = stdout
    return OpenShellSandboxHandle(
        _FakeExec(proc),
        client=gateway,  # type: ignore[arg-type]
        name="kew-2320",
        identity=SANDBOX_ID,
        startup_nonce=nonce,
        **caps,
    )


def _cap_reason(line: str) -> str:
    from kew.sandbox import RESOURCE_CAP_EVENT

    event = json.loads(line)
    assert event["type"] == RESOURCE_CAP_EVENT
    return event["reason"]


# ── the lifecycle: create, upload, exec, and a delete that always happens ────


class TestTheLaunchLifecycle:
    def test_the_calls_happen_in_the_only_order_that_works(
        self, tmp_path: Path
    ) -> None:
        """Create, read back, upload, then exec.

        Order is the assertion, not an incidental: exec'ing before the upload
        runs the agent against an empty tree with no prompt file, which the
        in-sandbox launcher reports as a usage error and the host reads as a
        launch failure.
        """
        gateway = FakeGateway()

        handle = launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image="sha256:" + "a" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        assert gateway.names == [
            "sandbox_create",
            "sandbox_get",
            "sandbox_upload",
            "sandbox_exec",
        ]
        assert handle.sandbox_identity == SANDBOX_ID

    def test_an_earlier_attempts_realized_tree_is_retired_before_the_launch(
        self, tmp_path: Path
    ) -> None:
        """A retry re-enters the SAME host checkout (PR #2451 review).

        The realization this launch ends in refuses a destination that is
        already occupied — a download into an existing tree keeps the files the
        agent deleted — so the tree the previous attempt realized has to go
        before this attempt starts, or this attempt's work is deleted with its
        sandbox having never been downloaded.
        """
        checkout = tmp_path / "work"
        checkout.mkdir()
        superseded = realized_checkout_dir(checkout)
        superseded.mkdir()
        (superseded / "from-attempt-1.txt").write_text("stale\n", encoding="utf-8")
        gateway = FakeGateway()

        launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image="sha256:" + "e" * 64,
            runner="claude-code",
            checkout_dir=checkout,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        assert not superseded.exists()
        # The checkout this attempt is about to upload is untouched by it.
        assert gateway.names[2] == "sandbox_upload"
        assert checkout.is_dir()

    def test_the_sandbox_is_created_from_the_immutable_image_id(
        self, tmp_path: Path
    ) -> None:
        """A tag names different bytes after a rebuild; the ID is the boundary."""
        gateway = FakeGateway()
        image_id = "sha256:" + "b" * 64

        launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image=image_id,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        create = gateway.calls[0].detail
        assert create["image"] == image_id
        config = _config()
        assert create["name"] == sandbox_name(
            SLUG, project=config.project.repo or config.project.name, run_id=RUN_ID
        )
        assert create["name"].startswith(f"kew-{SLUG}-")
        assert create["providers"] == ("kew-anthropic",)
        assert create["policy"] == runner_policy_path("claude-code").read_bytes()
        assert create["labels"] == {
            RUN_LABEL: RUN_ID,
            RUNNER_LABEL: "claude-code",
            FLOW_LABEL: DISPATCH_FLOW,
        }

    def test_the_review_flow_creates_the_sandbox_under_the_reviewer_policy(
        self, tmp_path: Path
    ) -> None:
        """One image, two boundaries (epic node N12).

        The adversarial reviewer executes the reviewed PR's tests, so it is not
        the boundary a dispatch was certified for. The policy is a parameter of
        the launch rather than a property of the runner, and the flow travels on
        the sandbox label too, so an operator reading the gateway can tell the
        two apart without reading kew's ledger.
        """
        gateway = FakeGateway()

        launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image="sha256:" + "b" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
            policy_flow=REVIEW_FLOW,
        )

        create = gateway.calls[0].detail
        assert (
            create["policy"]
            == runner_policy_path("claude-code", flow=REVIEW_FLOW).read_bytes()
        )
        assert create["policy"] != runner_policy_path("claude-code").read_bytes()
        assert create["labels"][FLOW_LABEL] == REVIEW_FLOW

    def test_the_run_kind_is_what_decides_the_policy_flow(self) -> None:
        """Derived from the kind the launch seam already carries, in ONE place.

        A reviewer launch whose policy came from somewhere other than the
        certification gate's own flow constant could run under a boundary its
        proof was never bound to, which is the mismatch the split exists to
        prevent.
        """
        from kew.openshell_launch import policy_flow_for_run_kind
        from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

        assert policy_flow_for_run_kind(ADVERSARIAL_REVIEW_RUN_KIND) == REVIEW_FLOW
        assert policy_flow_for_run_kind("dispatch") == DISPATCH_FLOW
        assert policy_flow_for_run_kind("review_remediation") == DISPATCH_FLOW

    def test_the_checkout_lands_where_the_in_sandbox_launcher_looks(
        self, tmp_path: Path
    ) -> None:
        """``/work``: the prompt path, the published tree and git's safe.directory.

        The launcher defaults ``KEW_PROMPT_FILE`` to ``/work/.kew-prompt.txt``
        and bootstraps ``safe.directory /work``, and ``openshell sandbox exec``
        takes no environment overrides — so uploading anywhere else loses the
        prompt handoff and makes every agent commit fail on dubious ownership.
        """
        gateway = FakeGateway()

        launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image="sha256:" + "c" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        upload = gateway.calls[2].detail
        assert (upload["source"], upload["destination"]) == (tmp_path, "/work")
        assert CHECKOUT_PATH == "/work"
        assert gateway.calls[3].detail["workdir"] == "/work"

    def test_the_exec_runs_the_launcher_with_the_nonce_then_the_program(
        self, tmp_path: Path
    ) -> None:
        """The nonce is an argument: the gateway owns the child's environment."""
        gateway = FakeGateway()

        launch_openshell_container(
            _config(),
            None,
            run_id=RUN_ID,
            image="sha256:" + "d" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        argv = gateway.calls[3].detail["argv"]
        assert argv[0] == LAUNCHER_PATH
        assert argv[1].isalnum() and len(argv[1]) == 32
        assert argv[2] == "/usr/bin/claude"

    def test_the_permit_is_re_asked_after_the_sandbox_exists(
        self, tmp_path: Path
    ) -> None:
        """The docker path's second creation check, in its gateway spelling.

        Create, the id read-back and a whole-checkout upload all run with the
        sandbox alive and no agent in it yet, so the caller's pre-create check
        cannot see a stop that lands inside that window. The recheck runs
        after the upload and before the exec, and its refusal deletes the
        sandbox like any other mid-launch failure (PR #2401 review).
        """
        gateway = FakeGateway()
        asked: list[list[str]] = []

        def permit() -> None:
            asked.append(list(gateway.names))
            raise RuntimeError("container launch refused after stop")

        with pytest.raises(RuntimeError, match="refused after stop"):
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "9" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                permit=permit,
                client=gateway,  # type: ignore[arg-type]
            )

        assert asked == [["sandbox_create", "sandbox_get", "sandbox_upload"]]
        assert gateway.names == [
            "sandbox_create",
            "sandbox_get",
            "sandbox_upload",
            "sandbox_delete",
        ]

    def test_the_run_records_the_sandbox_name_then_its_gateway_id(
        self, tmp_path: Path
    ) -> None:
        """The name before the create, the gateway id once it is read back.

        The name first, so a create whose delete cannot be confirmed still
        leaves a row a recovery can follow; the gateway id second, under the
        gateway's own shape check, because it is what recovery compares before
        it downloads or deletes anything (#2625).
        """
        gateway = FakeGateway()
        store = MagicMock()

        launch_openshell_container(
            _config(),
            store,
            run_id=RUN_ID,
            image="sha256:" + "e" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=MODEL,
            client=gateway,  # type: ignore[arg-type]
        )

        name = gateway.calls[0].detail["name"]
        assert store.set_container_identity.call_args_list == [
            call(RUN_ID, name, None, runtime="openshell"),
            call(RUN_ID, name, SANDBOX_ID, runtime="openshell"),
        ]


class TestTheModelFlag:
    """The routed model reaches the container through this flag or not at all."""

    @pytest.mark.parametrize("runner", sorted(RUNNER_PROGRAMS))
    def test_every_runners_exec_names_a_model(self, runner: str) -> None:
        from kew.runners.registry import get_runner

        args = get_runner(runner).container_args(config=_config(), model=MODEL)
        argv = exec_argv(runner, "n0nce", args)

        assert {part.partition("=")[0] for part in argv} & set(MODEL_FLAGS)
        assert MODEL in argv

    def test_an_argv_with_no_model_flag_is_refused_before_the_gateway(
        self, tmp_path: Path
    ) -> None:
        """A blank effective model would silently run the CLI's own default."""
        gateway = FakeGateway()

        with pytest.raises(OpenShellLaunchRefused, match="names no model"):
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "f" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model="",
                client=gateway,  # type: ignore[arg-type]
            )
        assert gateway.names == [], "the gateway was touched on a refused launch"

    def test_the_short_spelling_counts_too(self) -> None:
        """OpenCode spells it ``-m``; a check for ``--model`` alone refuses it."""
        assert require_model_flag(["run", "-m", MODEL], "opencode")
        assert require_model_flag([f"--model={MODEL}"], "claude-code")


class TestTheProgramTable:
    def test_every_program_is_the_path_shim_its_policy_governs(self) -> None:
        """An unmapped program would run outside the policy's governed set.

        The policies govern ``/usr/bin/<program>`` — the npm shim on PATH that
        the in-sandbox launcher resolves. A table entry naming anything else
        would exec a binary the gateway enforces no endpoint rule for, which
        reads like containment and governs nothing.
        """
        import yaml

        for runner, program in RUNNER_PROGRAMS.items():
            policy = yaml.safe_load(
                runner_policy_path(runner).read_text(encoding="utf-8")
            )
            governed = {binary["path"] for binary in policy["binaries"]}
            assert f"/usr/bin/{program}" in governed, runner

    def test_the_launchability_gate_asks_for_both_runner_facts(self) -> None:
        """What admission calls, so it refuses what the launch path would.

        `require_launchable_runner` is the whole runner-side question in one
        call. `kew.sandbox` asks it before recording a `sandboxed` posture; a
        runner answering only half of it used to be admitted and then refused
        here (PR #2401 review).
        """
        from kew.openshell_launch import require_launchable_runner

        for runner in sorted(RUNNER_PROGRAMS):
            assert require_launchable_runner(runner) == runner
        with pytest.raises(OpenShellLaunchRefused, match="has no program"):
            require_launchable_runner("pi")
        with pytest.raises(OpenShellLaunchRefused, match="has no program"):
            require_launchable_runner("hermes")

    def test_the_asking_spelling_of_that_gate_returns_the_same_sentences(
        self,
    ) -> None:
        """`launchable_runner_refusal` answers where admission must not refuse.

        `kew.sandbox._offered_runtime` needs to know whether kew has a boundary
        to offer this dispatch BEFORE the runner is asked for its own posture,
        and a runner with no launch path here is a reason to fall back to the
        no-runtime question rather than to refuse a run the runner's own
        sandbox contains (PR #2461 review round 2). Built on the raising form,
        so the two cannot answer differently.
        """
        from kew.openshell_launch import (
            launchable_runner_refusal,
            require_launchable_runner,
        )

        for runner in sorted(RUNNER_PROGRAMS):
            assert launchable_runner_refusal(runner) is None
        for runner in ("pi", "hermes"):
            refusal = launchable_runner_refusal(runner)
            assert refusal is not None and "has no program" in refusal
            with pytest.raises(OpenShellLaunchRefused) as raised:
                require_launchable_runner(runner)
            assert refusal == str(raised.value)

    def test_an_unmapped_runner_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(OpenShellLaunchRefused, match="no program"):
            runner_program("pi")

    def test_the_mapped_runners_are_the_ones_with_an_image(self) -> None:
        assert set(RUNNER_PROGRAMS) == {"claude-code", "codex", "opencode"}

    def test_the_launcher_is_handed_the_governed_path_never_a_bare_name(
        self,
    ) -> None:
        """A bare name is resolved through the sandbox PATH, which a checkout on
        it decides: a committed ``bin/claude`` would run as the runner (#2637).
        The absolute path is the very binary each runner's policy governs.
        """
        import yaml

        for runner in RUNNER_PROGRAMS:
            program = runner_program(runner)
            assert program.startswith("/"), runner
            policy = yaml.safe_load(
                runner_policy_path(runner).read_text(encoding="utf-8")
            )
            assert program in {binary["path"] for binary in policy["binaries"]}
        assert runner_program("claude-code") == "/usr/bin/claude"


class TestStdinAndTheEnvironment:
    def test_the_client_closes_stdin_on_every_exec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run on the shipped client, not the fake: the fake cannot prove it.

        An open stdin would make the host a channel into the contained agent;
        the agent is driven by its prompt file and nothing else. Only the
        resolved executable is swapped for a child that echoes its stdin, and
        the host's fd 0 is a pipe holding input for the duration — so an exec
        that inherited stdin would print that input back.
        """
        import sys

        from kew import openshell, subprocess_utils
        from kew.openshell import Gateway, OpenShellClient

        echo = [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
        monkeypatch.setattr(subprocess_utils, "resolve_cmd", lambda _cmd: echo)
        client = OpenShellClient(
            gateway=Gateway("https://gateway.invalid", openshell.GATEWAY_FROM_CONFIG),
            expected_version="0.1.2",
            environment={},
        )
        read_end, write_end = os.pipe()
        os.write(write_end, b"host input")
        os.close(write_end)
        saved_stdin = os.dup(0)
        try:
            os.dup2(read_end, 0)
            handle = client.sandbox_exec(
                "kew-run-7f3c1a", workdir="/work", argv=["claude"]
            )
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)
            os.close(read_end)
        assert handle.wait(timeout=10.0) == 0
        assert "".join(handle.drained).strip() == "''"

    def test_no_credential_value_reaches_the_sandbox(self, tmp_path: Path) -> None:
        """Names bound to the broker placeholder; values stay on the gateway."""
        from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE

        gateway = FakeGateway()
        real = "sk-ant-not-a-real-key-0123456789"

        with pytest.MonkeyPatch.context() as patched:
            patched.setenv("ANTHROPIC_API_KEY", real)
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "1" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        env = gateway.exec_env or {}
        assert env["ANTHROPIC_API_KEY"] == PLACEHOLDER_CREDENTIAL_VALUE
        assert real not in env.values()
        assert "ANTHROPIC_API_KEY" in gateway.exec_retain

    def test_the_launch_environment_is_an_allowlist_not_a_denylist(
        self, tmp_path: Path
    ) -> None:
        """Shown to fail: a host secret kew does not enumerate crossed anyway.

        The launch environment was the host environment minus kew's credential
        names, so every other variable — ``ACME_DEPLOY_TOKEN`` here, a CI
        token, a cloud key, the operator's editor session token — was handed to
        the contained agent, while the docker boundary beside it forwarded the
        runner's declared credential names and nothing else (PR #2401 review).
        The proxy variables are in the same position: ``[sandbox]
        proxy_environment`` defaults to ``"none"``, which BLANKS all ten in the
        docker argv, and this runtime has no proxy argv to blank them in.

        Asserted as the exact key set, not as "the secret is absent": a
        denylist passes the second form the moment the next unenumerated name
        appears.
        """
        from kew.openshell import SANDBOX_EXEC_ENV_NAMES
        from kew.sandbox import runner_credential_env_vars

        gateway = FakeGateway()
        with pytest.MonkeyPatch.context() as patched:
            patched.setenv("ACME_DEPLOY_TOKEN", "not-enumerated-by-kew")
            patched.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
            patched.setenv("OPENSHELL_GATEWAY", "http://gateway.invalid:8080")
            patched.setenv("PATH", "/usr/bin")
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "1" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        declared = set(runner_credential_env_vars("claude-code"))
        assert declared
        assert set(gateway.exec_env or {}) <= set(SANDBOX_EXEC_ENV_NAMES) | declared
        assert "PATH" in (gateway.exec_env or {})

    def test_the_host_path_never_crosses_into_the_sandbox(self, tmp_path: Path) -> None:
        """The sandbox runs under the fixed PATH, not the host's (#2637).

        ``sandbox exec`` forwards the client's environment, so a host PATH
        naming the checkout (or ``.``, with ``/work`` the workdir) would let a
        committed ``bin/claude`` or root ``uv`` stand in for the governed one.
        """
        from kew.openshell import SANDBOX_PATH

        gateway = FakeGateway()
        with pytest.MonkeyPatch.context() as patched:
            patched.setenv("PATH", f"/work/bin:.:{os.environ.get('PATH', '')}")
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "1" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        assert (gateway.exec_env or {})["PATH"] == SANDBOX_PATH
        assert not {".", "/work", "/work/bin"} & set(SANDBOX_PATH.split(":"))


# ── the launch gate: the bytes matched are the bytes created (#2624) ────────


def _launch(gateway: FakeGateway, tmp_path: Path, store: Any = None) -> None:
    launch_openshell_container(
        _config(),
        store,
        run_id=RUN_ID,
        image="sha256:" + "a" * 64,
        runner="claude-code",
        checkout_dir=tmp_path,
        slug=SLUG,
        model=MODEL,
        client=gateway,  # type: ignore[arg-type]
    )


def _certify_on(store: Any, gateway: FakeGateway, policy: bytes) -> None:
    """Append a passing certification bound to ``gateway`` and ``policy``."""
    from kew.audit import (
        append_runtime_egress_certification,
        egress_certification_actor,
    )
    from kew.egress_certification import (
        CERTIFICATION_SCHEMA_VERSION,
        OPENSHELL_PACKET_CONTROLS,
        OPENSHELL_PACKET_DENIALS,
        RuntimeEgressCertification,
        build_packet_evidence,
        bytes_digest,
    )
    from kew.openshell_egress import (
        openshell_gateway_identity,
        openshell_supervisor_digest,
    )
    from kew.readiness import PacketProbeStatus
    from kew.runtime_names import OPENSHELL_RUNTIME

    packet = build_packet_evidence(
        positive_control=PacketProbeStatus.REACHABLE,
        observations=dict.fromkeys(OPENSHELL_PACKET_DENIALS, PacketProbeStatus.DENIED),
        required=OPENSHELL_PACKET_DENIALS,
        controls=dict.fromkeys(OPENSHELL_PACKET_CONTROLS, PacketProbeStatus.REACHABLE),
    )
    append_runtime_egress_certification(
        store,
        RuntimeEgressCertification(
            schema_version=CERTIFICATION_SCHEMA_VERSION,
            runtime=OPENSHELL_RUNTIME,
            image_id="sha256:" + "a" * 64,
            policy_sha256=bytes_digest(policy),
            gateway_version=gateway.check_version(),
            gateway_identity=openshell_gateway_identity(gateway),  # type: ignore[arg-type]
            supervisor_digest=openshell_supervisor_digest(),
            packets=(("/usr/bin/node", packet),),
        ),
        actor=egress_certification_actor(OPENSHELL_RUNTIME),
    )


class TestTheLaunchGate:
    def test_a_policy_swapped_after_the_match_is_not_what_is_created(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        launch_gate: list[dict[str, Any]],
    ) -> None:
        """Hash once, create from those bytes.

        The file is replaced the moment the gate has matched it. The launch
        used to hand the gateway the packaged PATH, so the swapped bytes were
        what the sandbox ran under; it now hands over a copy of the read.
        """
        import kew.openshell_egress as openshell_egress

        policy = tmp_path / "policy-claude-code.yaml"
        original = runner_policy_path("claude-code").read_bytes()
        policy.write_bytes(original)
        monkeypatch.setattr(
            openshell_egress, "openshell_policy_path", lambda runner, *, flow: policy
        )

        def match_then_swap(store: Any, **kwargs: Any) -> None:
            launch_gate.append(kwargs)
            policy.write_bytes(b"binaries:\n  - path: /usr/bin/node\n")

        monkeypatch.setattr(
            openshell_launch, "require_launch_certification", match_then_swap
        )
        gateway = FakeGateway()
        (tmp_path / "checkout").mkdir()
        _launch(gateway, tmp_path / "checkout")

        [matched] = launch_gate
        assert policy.read_bytes() != original
        assert matched["policy"].data == original
        assert gateway.calls[0].detail["policy"] == original

    def test_a_launch_without_a_ledger_is_refused_before_any_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            openshell_launch, "require_launch_certification", _REAL_LAUNCH_GATE
        )
        gateway = FakeGateway()
        with pytest.raises(OpenShellLaunchRefused, match="no ledger"):
            _launch(gateway, tmp_path)
        assert gateway.names == []

    def test_a_proof_from_another_gateway_does_not_clear_this_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Certify on gateway A, launch on gateway B: refused, nothing created.

        Same CLI version on both, which is all the old binding compared.
        """
        from kew.run_store import SqliteRunStore

        monkeypatch.setattr(
            openshell_launch, "require_launch_certification", _REAL_LAUNCH_GATE
        )
        certified = FakeGateway(endpoint="https://gateway-a.internal:8443")
        policy = runner_policy_path("claude-code").read_bytes()
        (tmp_path / "checkout").mkdir()
        with SqliteRunStore(str(tmp_path / "gate.db")) as store:
            _certify_on(store, certified, policy)
            other = FakeGateway(endpoint="https://gateway-b.internal:8443")
            with pytest.raises(OpenShellLaunchRefused, match="certification"):
                _launch(other, tmp_path / "checkout", store)
            assert other.names == []
            _launch(certified, tmp_path / "checkout", store)
        assert certified.names[0] == "sandbox_create"


# ── the handle: the caps fire, and they fire at the gateway ──────────────────


class TestTheStartupAcknowledgement:
    def test_the_nonce_the_launcher_was_handed_is_the_one_observed(self) -> None:
        """One attempt's acknowledgement, and it never reaches the parsers."""
        gateway = FakeGateway()
        handle = _handle(
            gateway,
            lines=["KEW_AGENT_START n0nce\n", '{"type":"init"}\n'],
            nonce="n0nce",
        )

        streamed = list(handle)

        assert streamed == ['{"type":"init"}\n']
        assert handle.observe_workload_start() is True

    def test_a_different_nonce_is_not_this_attempts_startup(self) -> None:
        """A stale acknowledgement must not admit a relaunched attempt."""
        gateway = FakeGateway()
        handle = _handle(gateway, lines=["KEW_AGENT_START other\n"], nonce="n0nce")

        list(handle)

        assert handle.observe_workload_start() is False


class TestTheCapsKillThroughTheGateway:
    def test_the_wall_clock_breach_deletes_the_sandbox(self) -> None:
        gateway = FakeGateway()
        silent = _SilentStdout()
        handle = _handle(
            gateway,
            stdout=silent,
            wall_clock_seconds=0.001,
            watchdog_interval_seconds=0.02,
        )

        streamed = list(handle)
        silent.release.set()

        assert "wall-clock limit reached" in _cap_reason(streamed[-1])
        assert gateway.names == ["sandbox_delete"]

    def test_the_transcript_cap_deletes_the_sandbox(self) -> None:
        gateway = FakeGateway()
        handle = _handle(
            gateway,
            lines=["x" * 64 + "\n", "never read\n"],
            transcript_byte_cap=8,
        )

        streamed = list(handle)

        assert "transcript" in _cap_reason(streamed[-1])
        assert gateway.names == ["sandbox_delete"]

    def test_the_checkout_quota_is_not_claimed_on_this_runtime(self) -> None:
        """The agent writes to an uploaded copy; a host walk cannot see it."""
        handle = _handle(FakeGateway())

        assert handle._checkout_dir is None


class TestDeleteOnEveryExitPath:
    def test_a_clean_wait_releases_the_sandbox(self) -> None:
        gateway = FakeGateway()
        handle = _handle(gateway)

        assert handle.wait() == 0
        assert gateway.names == ["sandbox_delete"]

    def test_a_cancelled_launch_releases_the_sandbox(self) -> None:
        gateway = FakeGateway()
        handle = _handle(gateway)

        handle.cancel_launch()

        assert gateway.names == ["sandbox_delete"]

    def test_the_delete_happens_exactly_once_however_often_it_is_asked(
        self,
    ) -> None:
        """kill, remove, cleanup_identity and wait all funnel into one call."""
        gateway = FakeGateway()
        handle = _handle(gateway)

        assert handle.kill() is KillStatus.ACCEPTED
        assert handle.remove() is True
        handle.cleanup_identity()
        handle.wait()

        assert gateway.names == ["sandbox_delete"]

    def test_a_failure_part_way_through_the_launch_still_deletes(
        self, tmp_path: Path
    ) -> None:
        """The upload is the first call that can fail with a sandbox alive."""
        gateway = FakeGateway(fail="sandbox_upload", error=OSError("disk went away"))

        with pytest.raises(OSError, match="disk went away"):
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "2" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        assert gateway.names[-1] == "sandbox_delete"

    def test_a_create_of_unknown_outcome_deletes_before_it_propagates(
        self, tmp_path: Path
    ) -> None:
        """ "Unknown" never means "nothing happened" — the gateway may have made it."""
        gateway = FakeGateway(
            fail="sandbox_create",
            error=OpenShellOutcomeUnknown(["openshell"], reason="cut off"),
        )

        with pytest.raises(OpenShellOutcomeUnknown):
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "3" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        assert gateway.names == ["sandbox_create", "sandbox_delete"]

    def test_an_already_absent_sandbox_is_the_outcome_asked_for(self) -> None:
        gateway = FakeGateway(
            fail="sandbox_delete",
            error=OpenShellNotFound(["openshell"], reason="no such sandbox"),
        )
        handle = _handle(gateway)

        assert handle.kill() is KillStatus.ACCEPTED


class TestAnUncertainDeleteIsSettledByReading:
    def _unknown(self, status: str) -> OpenShellSandboxHandle:
        gateway = FakeGateway(
            status=status,
            fail="sandbox_delete",
            error=OpenShellOutcomeUnknown(["openshell"], reason="cut off"),
        )
        return _handle(gateway)

    def test_a_sandbox_still_running_refuses_the_kill(self) -> None:
        """The kill itself settles: a timed-out delete is never its outcome."""
        handle = self._unknown("running")

        assert handle.kill() is KillStatus.REFUSED
        assert handle._resolve_unknown_kill() is KillStatus.REFUSED
        assert handle.released is False

    def test_a_stopped_sandbox_settles_the_kill_as_accepted(self) -> None:
        handle = self._unknown("stopped")

        assert handle._resolve_unknown_kill() is KillStatus.ACCEPTED

    def test_an_absent_sandbox_settles_the_kill_as_accepted(self) -> None:
        gateway = FakeGateway(
            fail="sandbox_get", error=OpenShellNotFound(["x"], reason="gone")
        )
        handle = _handle(gateway)

        assert handle._resolve_unknown_kill() is KillStatus.ACCEPTED

    def test_an_unreadable_sandbox_stays_unknown(self) -> None:
        from kew.openshell import OpenShellGatewayUnreachable

        gateway = FakeGateway(
            fail="sandbox_get",
            error=OpenShellGatewayUnreachable(["x"], reason="unreachable"),
        )
        handle = _handle(gateway)

        assert handle._resolve_unknown_kill() is KillStatus.UNKNOWN


class TestTheIdentityDiagnostic:
    def test_no_daemon_probe_is_attempted_against_the_gateway(self) -> None:
        """``openshell events`` does not exist; the probe must say so instead.

        The base class, finding no cidfile, would report a missing id file and
        send the OOM probe shelling out to a subcommand the CLI has never had.
        """
        handle = _handle(FakeGateway())

        identity, diagnostic = handle._container_identity()

        assert identity is None
        assert "exposes no daemon container id" in diagnostic
        assert SANDBOX_ID in diagnostic


# ── the codex lane (epic openshell-runtime, node N10) ────────────────────────


class TestTheCodexExec:
    """What the gateway is asked to run for ``codex``, built end to end.

    The argv here is produced by the shipped
    :meth:`kew.runners.codex.CodexRunner.container_args` through
    :func:`_agent_args`, not by a literal in this file: the two facts worth
    pinning at this seam are that the subcommand sequence survives the trip
    (ONE ``exec``, after the root flags) and that the model the caller routed
    is in it. Both were silently wrong before this node — the deleted
    reference entrypoint added a second ``exec``, and a blank model produced
    an argv the Codex CLI answered with its own default model.
    """

    CODEX_MODEL = "gpt-5.6-sol"

    def _exec_argv(self, tmp_path: Path, model: str) -> tuple[str, ...]:
        gateway = FakeGateway()
        config = _config({"codex": "kew-openai"})

        launch_openshell_container(
            config,
            None,
            run_id=RUN_ID,
            image="sha256:" + "c" * 64,
            runner="codex",
            checkout_dir=tmp_path,
            slug=SLUG,
            model=model,
            client=gateway,  # type: ignore[arg-type]
        )

        return tuple(gateway.calls[-1].detail["argv"])

    def test_the_agent_is_exec_d_as_codex_with_exactly_one_exec(
        self, tmp_path: Path
    ) -> None:
        argv = self._exec_argv(tmp_path, self.CODEX_MODEL)

        assert argv[:3] == (LAUNCHER_PATH, argv[1], "/usr/bin/codex")
        agent_args = argv[3:]
        assert agent_args.count("exec") == 1
        assert agent_args[agent_args.index("exec") + 1 :] == (
            "--json",
            "--ignore-user-config",
        )

    def test_the_routed_model_reaches_the_sandbox(self, tmp_path: Path) -> None:
        argv = self._exec_argv(tmp_path, self.CODEX_MODEL)

        assert argv[argv.index("--model") + 1] == self.CODEX_MODEL
        assert require_model_flag(argv, "codex") == argv

    def test_codex_keeps_its_own_seatbelt_inside_the_sandbox(
        self, tmp_path: Path
    ) -> None:
        """Nested, not replaced — see the companion test in
        ``tests/test_runner_container_args.py`` for why."""
        argv = self._exec_argv(tmp_path, self.CODEX_MODEL)

        assert argv[argv.index("--sandbox") + 1] == "workspace-write"

    def test_a_blank_model_refuses_before_the_gateway_is_touched(
        self, tmp_path: Path
    ) -> None:
        """The runner refuses first, so no sandbox is created to clean up.

        ``require_model_flag`` is the backstop for a runner that omits the flag
        instead of raising; codex raises, which is why the refusal here is the
        runner's ``ValueError`` and not :class:`OpenShellLaunchRefused`.
        """
        gateway = FakeGateway()

        with pytest.raises(ValueError, match="requires an explicit model"):
            launch_openshell_container(
                _config({"codex": "kew-openai"}),
                None,
                run_id=RUN_ID,
                image="sha256:" + "c" * 64,
                runner="codex",
                checkout_dir=tmp_path,
                slug=SLUG,
                model="",
                client=gateway,  # type: ignore[arg-type]
            )

        assert gateway.names == []


# ── the live test ────────────────────────────────────────────────────────────


@pytest.mark.live_openshell
@pytest.mark.serial
@pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1",
    reason=(
        f"needs an operator OpenShell gateway and the built agent image; "
        f"set {LIVE_ENV}=1 to run it. Never set in CI."
    ),
)
def test_a_live_gateway_runs_claude_and_answers_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real dispatch through :func:`launch_openshell_container`.

    This proves the one thing the fakes above cannot: that a sandbox created
    from kew's image, governed by kew's policy, with the credential held in a
    gateway provider and only a placeholder inside, actually reaches the model
    and answers. The prompt handoff is the shipped one — ``claude -p`` is the
    ``--print`` the in-sandbox launcher injects plus the prompt file this
    writes into the checkout — so the live run is the dispatch path, not a
    simplified stand-in for it.

    It also proves the return trip (node N7): the agent is asked to COMMIT in
    the sandbox, and the assertion is that the commit is in the realized
    checkout on the host. That is the one thing the download fakes cannot
    show, because every piece of it is the gateway's — whether the upload
    carried a usable ``.git``, whether the agent's commit lands in it, and
    whether ``sandbox download`` brings the objects and the modes back out.

    ``[sandbox.openshell] providers`` and the image come from the operator's
    environment, because the gateway, the provider and the built image are all
    theirs; nothing here invents a default that would dial somewhere.
    """
    from kew import subprocess_utils
    from kew.openshell import OpenShellClient
    from kew.sandbox import PROMPT_FILENAME

    # The realization reads the realized tree's index through the subprocess
    # chokepoint; a dispatch has run its tool preflight by then and a test
    # has not.
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)

    config = _config({"claude-code": os.environ["KEW_LIVE_OPENSHELL_PROVIDER"]})
    config.sandbox.openshell.gateway = os.environ.get("KEW_LIVE_OPENSHELL_GATEWAY", "")
    OpenShellClient.from_settings(config.sandbox.openshell).check_version()

    checkout = _live_checkout(tmp_path / "work")
    (checkout / PROMPT_FILENAME).write_text(
        "Create a file named realized.txt holding the word OK, commit it with "
        'the message "realized", and then reply with OK.\n',
        encoding="utf-8",
    )
    handle = launch_openshell_container(
        config,
        None,
        run_id="live-openshell",
        image=os.environ["KEW_LIVE_OPENSHELL_IMAGE"],
        runner="claude-code",
        checkout_dir=checkout,
        slug=f"live-{int(time.time())}",
        model=os.environ.get("KEW_LIVE_OPENSHELL_MODEL", MODEL),
    )
    try:
        output = "".join(handle)
    finally:
        exit_code = handle.wait()

    assert exit_code == 0, output
    assert "OK" in output

    realization = handle.realization
    assert realization is not None and realization.refusal == "", realization
    realized = realization.checkout.path
    assert realized == realized_checkout_dir(checkout)
    # The commit the agent made inside the sandbox reached the host.
    assert "realized" in _live_git(realized, "log", "--format=%s")
    assert (realized / "realized.txt").is_file()
    # And the host's own tree is still the launch snapshot, which is why the
    # realized one had to be a separate directory.
    assert not (checkout / "realized.txt").exists()


def _live_checkout(root: Path) -> Path:
    """A real standalone checkout for the live run to upload.

    ``git init`` plus one commit, because the agent is asked to commit: an
    uploaded directory that is not a repository would fail the live assertion
    for a reason that has nothing to do with the gateway.

    The identity is written into the repository's own config rather than
    passed as ``-c`` to this function's commit, because the commit that
    matters is the AGENT's, inside the sandbox. Only what travels in
    ``.git/config`` reaches it: a ``-c`` override lives for one host process,
    so the agent's ``git commit`` would exit on "unable to auto-detect email
    address" unless the image happened to carry a global identity, and the N7
    assertion below would fail for a reason that is this fixture's
    (PR #2451 review).
    """
    root.mkdir(parents=True, exist_ok=True)
    _live_git(root, "init", "--quiet")
    _live_git(root, "config", "user.email", "tests@kew.invalid")
    _live_git(root, "config", "user.name", "kew")
    (root / "README.md").write_text("live\n", encoding="utf-8")
    _live_git(root, "add", "-A")
    _live_git(root, "commit", "--quiet", "-m", "base")
    return root


def _live_git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, operator-local repository
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


# ── the recovery download (node N8) ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _a_host_with_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery refuses a download the host has no room for (#2626).

    It asks for a checkout at the cap (4 GiB) plus the free-space floor
    (1 GiB), which the test device is not obliged to have, so the probe is
    faked for the module the way ``test_openshell_checkout`` fakes it. The
    host-room tests below fake it the other way.
    """
    _fake_free_space(monkeypatch, lambda _path: 64 * 1024**3)


def _fake_free_space(monkeypatch: pytest.MonkeyPatch, free: Any) -> None:
    """Report ``free(path)`` bytes free, for the test's duration."""
    monkeypatch.setattr(
        openshell_launch.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=free(Path(path))),
    )


class _Download:
    """A gateway whose download writes a recorded tree at the destination."""

    def __init__(self, tree: dict[str, str], *, nested: bool = False) -> None:
        self.tree = tree
        self.nested = nested
        self.calls: list[tuple[str, str]] = []

    def sandbox_download(self, name: str, source: str, destination: Path) -> None:
        self.calls.append((name, source))
        root = Path(destination) / "work" if self.nested else Path(destination)
        for relative, contents in self.tree.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")


@pytest.mark.parametrize("nested", [False, True])
def test_the_download_lands_the_checkout_for_either_cli_shape(
    tmp_path: Path, nested: bool
) -> None:
    """``sandbox download /work DEST`` may fill DEST or create DEST/work in it.

    v0.1.2 is undocumented on which, and the spike recorded no fixture, so both
    are accepted. Guessing wrong would copy an empty tree over the recovered
    work and report success.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    destination.mkdir()
    gateway = _Download({"wip.txt": "agent work\n"}, nested=nested)

    assert download_checkout(gateway, "kew-issue-1010", destination) == "merged"

    assert gateway.calls == [("kew-issue-1010", CHECKOUT_PATH)]
    assert (destination / "wip.txt").read_text(encoding="utf-8") == "agent work\n"


def test_the_download_merges_and_keeps_the_checkout_its_own_identity(
    tmp_path: Path,
) -> None:
    """The recovery binds this checkout by directory identity and a nonce.

    Replacing the directory would take both with it and refuse every later
    recovery write, so the copy may only add and overwrite — never remove, and
    never re-create the checkout itself.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)
    (destination / ".git" / "kew-generation").write_text("nonce", encoding="utf-8")
    (destination / "host-only.txt").write_text("kept\n", encoding="utf-8")
    identity = destination.stat().st_ino
    gateway = _Download({"wip.txt": "agent work\n", "README.md": "edited\n"})

    assert download_checkout(gateway, "kew-issue-1010", destination) == "merged"

    assert destination.stat().st_ino == identity
    assert (destination / ".git" / "kew-generation").read_text(
        encoding="utf-8"
    ) == "nonce"
    assert (destination / "host-only.txt").read_text(encoding="utf-8") == "kept\n"
    assert (destination / "wip.txt").read_text(encoding="utf-8") == "agent work\n"


def test_a_download_the_gateway_refused_reports_unmerged_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller is finalizing a run; a raise would replace its outcome.

    The work staying in the sandbox is bad and has to be said out loud, but it
    must not stop the run from being finalized and its capacity released.
    """
    from kew.openshell import OpenShellGatewayUnreachable
    from kew.openshell_launch import download_checkout

    class Refusing:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            raise OpenShellGatewayUnreachable(["openshell"], reason="dial tcp")

    with caplog.at_level("ERROR"):
        assert download_checkout(Refusing(), "kew-issue-1010", tmp_path) == "unmerged"

    assert "did not hand back" in caplog.text


def test_a_download_that_cannot_be_merged_at_all_reports_unmerged(
    tmp_path: Path,
) -> None:
    """A destination that is not a directory loses the work, loudly, not silently.

    ``copytree`` fails on the destination itself before it reads a single
    entry, so nothing of the sandbox reached the host: ``"unmerged"``, the
    verdict that says the checkout is as it was.
    """
    from kew.openshell_launch import download_checkout

    blocked = tmp_path / "issue-1010"
    blocked.write_text("not a checkout\n", encoding="utf-8")

    assert (
        download_checkout(_Download({"wip.txt": "x\n"}), "kew", blocked) == "unmerged"
    )


def test_a_nested_download_with_a_cli_sibling_still_merges_at_the_right_level(
    tmp_path: Path,
) -> None:
    """Counting entries cannot tell ``DEST/work`` plus a sibling from a filled DEST.

    Guessing "filled" there merges the checkout one level down, as
    ``<checkout>/work/...``, and reports the work recovered — so the sandbox
    holding the only copy is deleted over a checkout that never got it. The
    uploaded tree's own ``.git`` directory says which level is the checkout.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)

    class NestedWithSibling:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            checkout = Path(destination) / "work"
            (checkout / ".git").mkdir(parents=True)
            (checkout / "wip.txt").write_text("agent work\n", encoding="utf-8")
            (Path(destination) / "manifest.json").write_text("{}", encoding="utf-8")

    assert (
        download_checkout(NestedWithSibling(), "kew-issue-1010", destination)
        == "merged"
    )

    assert (destination / "wip.txt").read_text(encoding="utf-8") == "agent work\n"
    assert not (destination / "work").exists()


def test_a_filled_download_of_a_checkout_holding_its_own_work_directory(
    tmp_path: Path,
) -> None:
    """The mirror reading: a repo may have a top-level ``work`` of its own.

    Refusing the ambiguity outright would strand the recovery for any such
    repo; the checkout's ``.git`` is at the top of a filled DEST, so the merge
    takes that level and the repo's own ``work/`` comes back as itself.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)

    class FilledWithWorkDir:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            root = Path(destination)
            (root / ".git").mkdir(parents=True)
            (root / "work").mkdir()
            (root / "work" / "notes.md").write_text("kept\n", encoding="utf-8")

    assert (
        download_checkout(FilledWithWorkDir(), "kew-issue-1010", destination)
        == "merged"
    )

    assert (destination / "work" / "notes.md").read_text(encoding="utf-8") == "kept\n"


@pytest.mark.parametrize(
    ("shape", "describe"),
    [
        (lambda destination: None, "an untouched destination"),
        (lambda destination: (destination / "work").mkdir(), "an empty DEST/work"),
        (
            lambda destination: (destination / "src" / "pkg").mkdir(parents=True),
            "directories carrying no files",
        ),
    ],
)
def test_a_download_that_wrote_no_files_reports_unmerged_rather_than_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    shape: Any,
    describe: str,
) -> None:
    """A download may exit 0 and hand back nothing; that is not a recovery.

    ``"merged"`` is what deletes the sandbox holding the only copy of the
    agent's work and attests ``checkout_recovered``, and merging an empty tree
    succeeds — so success is claimed off bytes actually downloaded, never off
    a copy that had nothing to copy. The host checkout is left as it was.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    destination.mkdir()
    (destination / "host-only.txt").write_text("kept\n", encoding="utf-8")

    class Silent:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            shape(Path(destination))

    with caplog.at_level("ERROR"):
        assert download_checkout(Silent(), "kew-issue-1010", destination) == "unmerged"

    assert "left no files behind" in caplog.text, describe
    assert [path.name for path in destination.iterdir()] == ["host-only.txt"]


def test_a_merge_that_failed_part_way_reports_part_merged_not_unmerged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The merge is not atomic, and the half-merged tree must say so.

    ``copytree`` copies every entry it can and raises carrying only the ones
    it could not, so one unwritable path leaves the rest of the sandbox's
    files on the host. Reporting that as ``"unmerged"`` would attest
    ``checkout_recovered: false`` over a checkout that is half the sandbox's —
    and the orphan path reads that same tree as the agent's work and offers a
    continuation from it, so the operator deciding whether to continue would
    be told the host was untouched.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)
    # A host FILE where the sandbox has a directory: the one entry copytree
    # cannot write, whatever the uid running the tests.
    (destination / "notes").write_text("host file\n", encoding="utf-8")

    class PartlyBlocked:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            root = Path(destination)
            (root / "landed.txt").write_text("agent work\n", encoding="utf-8")
            (root / "notes").mkdir()
            (root / "notes" / "new.txt").write_text("blocked\n", encoding="utf-8")

    with caplog.at_level("ERROR"):
        merge = download_checkout(PartlyBlocked(), "kew-issue-1010", destination)

    assert merge == "part_merged"
    assert (destination / "landed.txt").read_text(encoding="utf-8") == "agent work\n"
    assert (destination / "notes").read_text(encoding="utf-8") == "host file\n"
    assert "merged only part-way" in caplog.text


def test_a_merge_blocked_on_its_only_entry_is_unmerged_not_part_merged(
    tmp_path: Path,
) -> None:
    """Nothing of the sandbox landed, so the host checkout is as it was.

    The mirror of the part-way case, and the reason the verdict is read off
    the host checkout rather than off the fact that an error was raised: a
    multi-file copy reports the same exception type whether it wrote one entry
    or none.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)
    (destination / "notes").write_text("host file\n", encoding="utf-8")

    class WhollyBlocked:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            root = Path(destination)
            (root / "notes").mkdir()
            (root / "notes" / "new.txt").write_text("blocked\n", encoding="utf-8")

    assert (
        download_checkout(WhollyBlocked(), "kew-issue-1010", destination) == "unmerged"
    )

    assert (destination / "notes").read_text(encoding="utf-8") == "host file\n"
    assert sorted(path.name for path in destination.iterdir()) == [".git", "notes"]


def test_a_download_of_a_symlink_is_recovered_work(tmp_path: Path) -> None:
    """The merge preserves symlinks, so a tree of them is work that came back.

    The emptiness check looks for files; a symlink is not one, and counting it
    out would refuse a real recovery and strand the sandbox.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    destination.mkdir()

    class Linking:
        def sandbox_download(self, name: str, source: str, destination: Path) -> None:
            (Path(destination) / "latest").symlink_to("run-7/log.txt")

    assert download_checkout(Linking(), "kew-issue-1010", destination) == "merged"

    assert (destination / "latest").is_symlink()


def test_the_merge_never_writes_through_a_host_symlink(tmp_path: Path) -> None:
    """A host ``escape -> /outside`` met by sandbox entries of that name (#2626).

    ``copytree(dirs_exist_ok=True)`` followed the link and wrote the sandbox's
    files into ``/outside``. The link is replaced where the sandbox has an
    entry of its name, and what it pointed at is never written: not a
    directory's contents, not a file's bytes.
    """
    from kew.openshell_launch import download_checkout

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    destination = tmp_path / "issue-1010"
    (destination / ".git").mkdir(parents=True)
    (destination / "escape").symlink_to(outside_dir, target_is_directory=True)
    (destination / "pointer.txt").symlink_to(outside_file)

    gateway = _Download({"escape/payload.txt": "agent\n", "pointer.txt": "agent\n"})

    assert download_checkout(gateway, "kew-issue-1010", destination) == "merged"

    assert list(outside_dir.iterdir()) == []
    assert outside_file.read_text(encoding="utf-8") == "outside\n"
    assert not (destination / "escape").is_symlink()
    assert (destination / "escape" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "agent\n"
    assert not (destination / "pointer.txt").is_symlink()
    assert (destination / "pointer.txt").read_text(encoding="utf-8") == "agent\n"


def test_a_symlinked_host_checkout_is_refused_not_followed(tmp_path: Path) -> None:
    """The checkout root itself as a link: nothing lands at its target."""
    from kew.openshell_launch import download_checkout

    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "issue-1010"
    destination.symlink_to(outside, target_is_directory=True)

    assert (
        download_checkout(_Download({"wip.txt": "x\n"}), "kew", destination)
        == "unmerged"
    )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("caps", "describe"),
    [
        ({"quota_bytes": 4}, "over the byte cap"),
        ({"quota_entries": 1}, "over the entry cap"),
    ],
)
def test_a_recovered_checkout_over_the_caps_is_not_merged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    caps: dict[str, int],
    describe: str,
) -> None:
    """Recovery applies the checkout caps the realization does (#2626).

    The work stays in the sandbox (``"unmerged"`` never deletes it) rather
    than an over-cap tree landing on the host.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    destination.mkdir()
    gateway = _Download({"a.txt": "agent work\n", "b.txt": "more work\n"})

    with caplog.at_level("ERROR"):
        merge = download_checkout(
            gateway, "kew-issue-1010", destination, free_floor=0, **caps
        )

    assert merge == "unmerged", describe
    assert "breaches the checkout caps" in caplog.text
    assert list(destination.iterdir()) == []


def test_a_recovery_the_host_has_no_room_for_is_never_downloaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The room for a cap-sized checkout is taken before the transfer starts."""
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    destination.mkdir()
    gateway = _Download({"wip.txt": "agent work\n"})
    _fake_free_space(monkeypatch, lambda _path: 1024)

    with caplog.at_level("ERROR"):
        merge = download_checkout(
            gateway, "kew-issue-1010", destination, quota_bytes=1024, free_floor=1
        )

    assert merge == "unmerged"
    assert gateway.calls == []
    assert "free-space floor" in caplog.text
    assert list(destination.iterdir()) == []


def test_a_recovery_whose_merge_the_checkout_has_no_room_for_is_not_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge writes what was measured; the checkout's device must hold it.

    The driver merges into a host checkout path proven free, so the device is
    probed at the closest directory that exists rather than skipped.
    """
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "issue-1010"
    _fake_free_space(monkeypatch, lambda path: 0 if path == tmp_path else 4096)
    gateway = _Download({"wip.txt": "agent work\n"})

    assert (
        download_checkout(
            gateway, "kew-issue-1010", destination, quota_bytes=1024, free_floor=1
        )
        == "unmerged"
    )

    assert gateway.calls == [("kew-issue-1010", CHECKOUT_PATH)]
    assert not destination.exists()


def test_the_merge_creates_a_host_checkout_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    """The driver's recovery merges into a free path, as copytree created it."""
    from kew.openshell_launch import download_checkout

    destination = tmp_path / "agent" / "issue-1010"
    gateway = _Download({"wip.txt": "agent work\n"})

    assert download_checkout(gateway, "kew-issue-1010", destination) == "merged"

    assert (destination / "wip.txt").read_text(encoding="utf-8") == "agent work\n"


@pytest.mark.live_openshell
@pytest.mark.serial
@pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1",
    reason=(
        f"needs an operator OpenShell gateway and the built agent-codex image; "
        f"set {LIVE_ENV}=1 to run it. Never set in CI."
    ),
)
def test_a_live_gateway_runs_codex_exec_with_an_explicit_model(tmp_path: Path) -> None:
    """One real ``codex exec`` through the gateway (epic node N10).

    The sibling of the claude live test above, and the proof the fakes cannot
    give for this runner: that the argv kew builds — root flags, ONE ``exec``,
    ``--json`` — is an argv the Codex CLI accepts inside an OpenShell sandbox,
    that the ChatGPT-mode placeholder ``auth.json`` the in-sandbox launcher
    writes lets it reach the endpoint its policy governs, and that Codex's own
    ``workspace-write`` seatbelt installs nested inside that sandbox rather
    than aborting the run.

    The model is read WITHOUT a default, because "an explicit model" is the
    thing under test: every Codex launch names one (``_require_model``), and a
    live run that fell back to a constant in this file would be evidence about
    the constant.
    """
    from kew.openshell import OpenShellClient
    from kew.sandbox import PROMPT_FILENAME

    config = _config({"codex": os.environ["KEW_LIVE_OPENSHELL_CODEX_PROVIDER"]})
    config.sandbox.openshell.gateway = os.environ.get("KEW_LIVE_OPENSHELL_GATEWAY", "")
    OpenShellClient.from_settings(config.sandbox.openshell).check_version()

    (tmp_path / PROMPT_FILENAME).write_text("reply with OK\n", encoding="utf-8")
    handle = launch_openshell_container(
        config,
        None,
        run_id="live-openshell-codex",
        image=os.environ["KEW_LIVE_OPENSHELL_CODEX_IMAGE"],
        runner="codex",
        checkout_dir=tmp_path,
        slug=f"live-codex-{int(time.time())}",
        model=os.environ["KEW_LIVE_OPENSHELL_CODEX_MODEL"],
    )
    try:
        output = "".join(handle)
    finally:
        exit_code = handle.wait()

    assert exit_code == 0, output
    assert "OK" in output


def test_the_live_test_cannot_run_by_accident() -> None:
    """The live test is opt-in, and the opt-in is an environment variable.

    A marker alone would still run under a bare ``pytest``; this is the guard
    that keeps a CI job from dialling an operator's gateway, and it fails if
    the variable is ever given a third, truthy-looking spelling.
    """
    assert os.environ.get(LIVE_ENV) in (None, "", "1")


# ── sandbox identity and an unconfirmed release (#2625) ─────────────────────


class TestTheSandboxNameIsTheRunsOwn:
    def test_two_projects_with_the_same_item_get_different_names(self) -> None:
        """``kew-42`` in two repositories on one gateway was one sandbox name."""
        first = sandbox_name("42", project="acme/api", run_id="run-1")
        second = sandbox_name("42", project="acme/web", run_id="run-1")

        assert first != second
        assert first.startswith("kew-42-") and second.startswith("kew-42-")

    def test_two_runs_of_one_item_get_different_names(self) -> None:
        assert sandbox_name("42", project="acme/api", run_id="run-1") != (
            sandbox_name("42", project="acme/api", run_id="run-2")
        )

    def test_the_name_is_stable_for_one_run(self) -> None:
        """Every attempt of one run, and every reader of its row, agree on it."""
        assert sandbox_name("42", project="acme/api", run_id="run-1") == (
            sandbox_name("42", project="acme/api", run_id="run-1")
        )

    def test_a_name_without_a_run_is_refused(self) -> None:
        with pytest.raises(ValueError, match="run id"):
            sandbox_name("42", project="acme/api", run_id=" ")


class _StuckGateway(FakeGateway):
    """A gateway whose deletes time out and whose sandbox cannot be read."""

    def __init__(self, *, create_fails: bool = False) -> None:
        super().__init__()
        self.create_fails = create_fails

    def sandbox_create(self, name: str, **detail: Any) -> None:
        self.calls.append(Call("sandbox_create", {"name": name, **detail}))
        if self.create_fails:
            raise OpenShellOutcomeUnknown(["openshell"], reason="cut off")

    def sandbox_delete(self, name: str) -> None:
        self.calls.append(Call("sandbox_delete", {"name": name}))
        raise OpenShellOutcomeUnknown(["openshell"], reason="cut off")

    def sandbox_get(self, name: str) -> Sandbox:
        if self.names and self.names[-1] == "sandbox_delete":
            self.calls.append(Call("sandbox_get", {"name": name}))
            from kew.openshell import OpenShellGatewayUnreachable

            raise OpenShellGatewayUnreachable(["openshell"], reason="dial tcp")
        return super().sandbox_get(name)


class TestAnUnconfirmedReleaseIsNotAFinishedOne:
    def test_a_twice_timed_out_launch_unwind_names_the_sandbox_on_the_error(
        self, tmp_path: Path
    ) -> None:
        """The create AND its unwind's delete time out: the error says so.

        ``kew.dispatch_sandbox`` turns that attribute into the unconfirmed
        cleanup evidence that keeps the run pending, rather than finalizing it
        ``sandbox_launch_error`` beside a sandbox that may still hold its
        provider.
        """
        from kew.openshell_launch import UNRELEASED_SANDBOX_ATTR

        gateway = _StuckGateway(create_fails=True)

        with pytest.raises(OpenShellOutcomeUnknown) as raised:
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "3" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        name = gateway.calls[0].detail["name"]
        assert getattr(raised.value, UNRELEASED_SANDBOX_ATTR) == name
        # Settled by reading, then retried, then settled again.
        assert gateway.names[1:] == [
            "sandbox_delete",
            "sandbox_get",
            "sandbox_delete",
            "sandbox_get",
        ]

    def test_a_confirmed_unwind_leaves_the_error_unmarked(self, tmp_path: Path) -> None:
        from kew.openshell_launch import UNRELEASED_SANDBOX_ATTR

        gateway = FakeGateway(
            fail="sandbox_create",
            error=OpenShellOutcomeUnknown(["openshell"], reason="cut off"),
        )

        with pytest.raises(OpenShellOutcomeUnknown) as raised:
            launch_openshell_container(
                _config(),
                None,
                run_id=RUN_ID,
                image="sha256:" + "3" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug=SLUG,
                model=MODEL,
                client=gateway,  # type: ignore[arg-type]
            )

        assert not hasattr(raised.value, UNRELEASED_SANDBOX_ATTR)

    def test_cleanup_that_cannot_confirm_the_delete_leaves_the_handle_unreleased(
        self,
    ) -> None:
        handle = _handle(_StuckGateway())

        handle.cleanup_identity()

        assert handle.released is False

    def test_a_timed_out_delete_settled_by_reading_is_released(self) -> None:
        """The resolver runs on cleanup: an absent sandbox IS the delete."""
        gateway = FakeGateway(
            fail="sandbox_delete",
            error=OpenShellOutcomeUnknown(["openshell"], reason="cut off"),
        )
        gateway.status = "deleted"
        handle = _handle(gateway)

        handle.cleanup_identity()

        assert handle.released is True
        assert gateway.names == ["sandbox_delete", "sandbox_get"]


class _ProbedGateway:
    def __init__(self, answer: str | BaseException) -> None:
        self.answer = answer
        self.argv: Sequence[str] = ()

    def sandbox_run(self, name: str, *, workdir: str, argv: Sequence[str]) -> str:
        self.argv = argv
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TestTheRemoteAgentIsAskedNotInferred:
    @pytest.mark.parametrize(
        ("answer", "state"),
        [
            ("KEW_AGENT_EXEC running\n", "running"),
            ("KEW_AGENT_EXEC ended\n", "ended"),
            ("KEW_AGENT_EXEC absent\n", "ended"),
            ("", "unknown"),
            ("KEW_AGENT_EXEC maybe\n", "unknown"),
            (OpenShellOutcomeUnknown(["openshell"], reason="cut off"), "unknown"),
        ],
    )
    def test_each_probe_answer_maps_to_one_state(
        self, answer: str | BaseException, state: str
    ) -> None:
        from kew.openshell_launch import AGENT_EXEC_PROBE, agent_exec_state

        gateway = _ProbedGateway(answer)

        assert agent_exec_state(gateway, "kew-2320") == state  # type: ignore[arg-type]
        assert gateway.argv == ("/bin/sh", "-c", AGENT_EXEC_PROBE)

    def test_the_probe_reads_a_live_and_an_ended_process_on_this_host(
        self, tmp_path: Path
    ) -> None:
        """The probe script itself, run against a real marker and process.

        The marker is written by the real launcher snippet's arithmetic — pid
        and ``/proc`` start time — so a probe that miscounted the stat fields
        would read the live process as ended.
        """
        import subprocess

        from kew.openshell_launch import AGENT_EXEC_MARKER, AGENT_EXEC_PROBE

        if not is_linux():
            pytest.skip("no /proc on this host")
        marker = tmp_path / "marker"
        probe = AGENT_EXEC_PROBE.replace(AGENT_EXEC_MARKER, str(marker))
        child = subprocess.Popen(["sleep", "30"])
        try:
            fields = Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1]
            marker.write_text(f"{child.pid} {fields.split()[19]}\n")

            def ask() -> str:
                return subprocess.run(
                    ["/bin/sh", "-c", probe],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

            assert ask() == "KEW_AGENT_EXEC running"
            marker.write_text(f"{child.pid} 1\n")
            assert ask() == "KEW_AGENT_EXEC ended"  # a reused pid
        finally:
            child.kill()
            child.wait()
        marker.write_text(f"{child.pid} {fields.split()[19]}\n")
        assert ask() == "KEW_AGENT_EXEC ended"
        marker.unlink()
        assert ask() == "KEW_AGENT_EXEC absent"
