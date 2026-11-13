"""The adversarial reviewer under OpenShell (epic ``openshell-runtime``, N12).

The reviewer is the single most exposed launch kew makes: its prompt requires it
to execute the reviewed PR's tests, and before this node it did that beside a
reusable credential, on a plain bridge network, with nothing on the audit chain
to say so. These tests pin the four properties that change:

* the launch goes through the OpenShell backend and **never** falls back to
  docker — a fallback would hand the real token to the one launch that runs the
  reviewed code;
* the reviewer sandbox holds the placeholder and the provisioning sandbox holds
  no provider at all, so there is no credential in either;
* every reviewer launch attests ``sandbox_resolved``, on both routes, so the
  docker route's unverified egress is at least visible;
* an OpenShell reviewer without a fresh certification for ITS OWN boundary — the
  review policy's digest, not the dispatch one — refuses.

The gateway is faked at :class:`FakeGateway`, the N2 client's surface. Everything
above it is production: ``sandbox_reviewer_launcher`` is the real launcher, the
real ``run_container_and_stream``, the real ``launch_openshell_container``, the
real ``provision_review_environment`` and the real certification gate reading a
proof the real ``kew sandbox certify`` path appended.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support.image_resolution import built_by_kew
from support.review_stream import reviewer_stream

from kew.openshell import SANDBOX_EXEC_ENV_NAMES, Sandbox
from kew.openshell_egress import PROVISION_FLOW, REVIEW_FLOW
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore

HEAD = "a" * 40
ITEM = "4221"
RUN_ID = "rev-openshell"
IMAGE_ID = "sha256:" + "e" * 64
REAL_TOKEN = "sk-ant-not-the-placeholder-0123456789"

_READY = {
    "verdict": "READY",
    "findings": [],
    "acceptance_evidence": [{"command": "pytest", "output_digest": "sha256:z"}],
}

#: What the environment check must print for the host to accept the venv it
#: built — the tagged line from an interpreter inside the pinned environment.
INTERPRETER_LINE = "kew-interpreter /work/kew/.venv/bin/python\n"


# --------------------------------------------------------------------------- #
# The gateway double                                                          #
# --------------------------------------------------------------------------- #
class _Stream:
    """A merged exec stdout that serves both consumers kew has for one.

    ``ContainerHandle`` reads it with a bounded ``readline(limit)``; the
    provisioning reader iterates it. The real pipe does both, so the double
    must too — a fake that only did one would let a reader regress unnoticed.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.closed = False

    def readline(self, limit: int | None = None) -> str:
        if not self._lines:
            return ""
        line = self._lines.pop(0)
        return line if limit is None else line[:limit]

    def __iter__(self) -> _Stream:
        return self

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self) -> None:
        self._lines = []
        self.closed = True


def _process(lines: list[str]) -> MagicMock:
    proc = MagicMock()
    proc.stdout = _Stream(lines)
    proc.pid = os.getpid()
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    return proc


@dataclass
class _Exec:
    """The ``OpenShellExec`` surface both callers use."""

    process: Any
    exit_code: int = 0

    @property
    def stdout(self) -> Any:
        return self.process.stdout

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code


@dataclass
class Call:
    name: str
    detail: dict[str, Any] = field(default_factory=dict)


class FakeGateway:
    """Records every gateway call; answers them the way v0.1.2 does.

    One instance serves BOTH sandboxes a review needs, which is what makes
    "provisioning and review never share a sandbox" assertable: the names and
    the create arguments of both are on one list.
    """

    def __init__(
        self,
        *,
        transcript: list[str] | None = None,
        install_exit: int = 0,
        version_error: BaseException | None = None,
    ) -> None:
        from kew.openshell import Gateway

        self.gateway = Gateway("", "cli-default")
        self.calls: list[Call] = []
        self.transcript = transcript if transcript is not None else []
        self.install_exit = install_exit
        self.version_error = version_error
        self.exec_envs: list[dict[str, str] | None] = []
        self.exec_retains: list[tuple[str, ...]] = []

    # -- bookkeeping -------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def created(self, flow: str) -> dict[str, Any]:
        """The single ``sandbox_create`` whose labels name ``flow``."""
        matches = [
            call.detail
            for call in self.calls
            if call.name == "sandbox_create"
            and call.detail["labels"].get("kew.flow") == flow
        ]
        assert len(matches) == 1, f"{flow}: {matches}"
        return matches[0]

    def _record(self, _call: str, **detail: Any) -> None:
        self.calls.append(Call(_call, detail))

    # -- the client surface ------------------------------------------------
    def check_version(self) -> str:
        if self.version_error is not None:
            raise self.version_error
        self._record("check_version")
        return "0.1.2"

    def report(self) -> Any:
        from kew.openshell import GatewayReport

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
            raw={},
        )

    def sandbox_create(
        self,
        name: str,
        *,
        image: str,
        providers: Any = (),
        policy_path: Path | None = None,
        labels: Any = None,
    ) -> None:
        self._record(
            "sandbox_create",
            name=name,
            image=image,
            providers=tuple(providers),
            policy_path=policy_path,
            policy=None if policy_path is None else policy_path.read_bytes(),
            labels=dict(labels or {}),
        )

    def sandbox_get(self, name: str) -> Sandbox:
        self._record("sandbox_get", name=name)
        return Sandbox(
            name=name,
            status="running",
            identity="sbx_01HN4Q8ZJ2WME5T",
            image=IMAGE_ID,
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
        argv: Any,
        env: dict[str, str] | None = None,
        retain: Any = (),
    ) -> _Exec:
        argv = tuple(argv)
        self._record("sandbox_exec", name=name, workdir=workdir, argv=argv)
        self.exec_envs.append(env)
        self.exec_retains.append(tuple(retain))
        if argv[0] == "/usr/local/bin/uv":
            # The install, then the environment check: the check is the one that
            # has to print the tagged interpreter line.
            lines = [INTERPRETER_LINE] if argv[1] == "run" else []
            return _Exec(_process(lines), self.install_exit)
        # The reviewer, through the in-sandbox launcher: argv[1] is the startup
        # nonce the host waits to see echoed before it parses agent events.
        return _Exec(_process([f"KEW_AGENT_START {argv[1]}\n", *self.transcript]))

    def sandbox_download(self, name: str, source: str, destination: Path) -> None:
        self._record(
            "sandbox_download", name=name, source=source, destination=destination
        )
        if not source.endswith("/.venv"):
            # The reviewer's own checkout realization: nothing comes back,
            # which the N7 refusal records and the read-only reviewer ignores.
            return
        built = Path(destination) / ".venv"
        (built / "bin").mkdir(parents=True, exist_ok=True)
        (built / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    def sandbox_delete(self, name: str) -> None:
        self._record("sandbox_delete", name=name)

    def logs(self, name: str, *, since: str = "") -> str:
        self._record("logs", name=name)
        return ""


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
#: The marker on the one test in this module that talks to a real gateway.
LIVE_MARKER = "live_openshell"


def _is_live(request: pytest.FixtureRequest) -> bool:
    """Whether this test is the AC7 round against the operator's gateway.

    Every autouse fixture below steps aside for it. The fakes are what make
    the faked rounds assertable and what would make the live round a lie: an
    image id this module invents is handed straight to a real
    ``sandbox_create`` (``openshell_launch`` passes the resolved id through),
    a runtime of ``/bin/true`` is not the gateway, a free-space probe answered
    here lets a transfer start on a device that cannot hold it, and a fake
    credential planted in the environment turns "no real token in the
    sandbox" into a sweep for a value nothing ever held (PR #2520 review).
    """
    return request.node.get_closest_marker(LIVE_MARKER) is not None


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "review.db"), project_root=tmp_path) as bound:
        yield bound


@pytest.fixture(autouse=True)
def _host_has_room(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip the two pre-transfer free-space probes a review makes.

    The realization refuses unless the device can hold a checkout at the 4 GiB
    cap AND still leave the 1 GiB floor, and the provisioner refuses to copy
    the checkout at all within that floor — properties of the machine running
    the suite, not of the code under test. Their own refusals are covered by
    ``tests/test_openshell_checkout.py`` and
    ``tests/test_review_environment.py``; without this the whole provisioning
    half of this module silently takes the "no environment" path on any host
    with a 1 GiB ``/tmp`` (a normal container shape), and every assertion
    below it asserts against a refusal it is not about.
    """
    if _is_live(request):
        # The live round copies a real checkout: the probes are the real
        # refusal the operator's host should get, not a property to fake.
        return

    import kew.openshell_checkout as checkout
    import kew.review_environment as provisioner

    monkeypatch.setattr(checkout, "_require_host_room", lambda *_a: None)
    free = 64 * 1024**3
    monkeypatch.setattr(
        provisioner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=free, used=0, free=free),
    )


@pytest.fixture(autouse=True)
def _reviewer_host(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that can resolve the image and a real credential in the env.

    The credential is real-looking on purpose: every assertion that the sandbox
    sees only a placeholder is worthless unless a real value was there to leak.

    None of it applies to the live round — see :func:`_is_live`.
    """
    if _is_live(request):
        return

    import kew.sandbox as sandbox

    monkeypatch.setenv("ANTHROPIC_API_KEY", REAL_TOKEN)
    monkeypatch.setattr(
        sandbox,
        "inspect_image_id",
        lambda runtime, image: built_by_kew(image, IMAGE_ID),
    )
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: IMAGE_ID,
    )
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda config: "/bin/true")
    monkeypatch.setattr(
        "kew.graph.driver.review_launcher.detect_runtime_cached",
        lambda config: "/bin/true",
    )


def _config(tmp_path: Path, *, runtime: str) -> Any:
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.review.adversarial = True
    # Pinned because the gateway launch REFUSES an exec that names no model
    # (``openshell_launch.require_model_flag``): the routed model reaches the
    # agent through that flag alone, so a blank one would silently run the
    # runner CLI's default.
    config.review.model = "claude-sonnet-5"
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    config.sandbox.enabled = True
    config.sandbox.runtime = runtime
    config.sandbox.images = {"claude-code": "kew-agent:docker-000000000000"}
    config.sandbox.openshell.providers = {"claude-code": "kew-anthropic"}
    return config


def _install_gateway(monkeypatch: pytest.MonkeyPatch, gateway: FakeGateway) -> None:
    """Every module that reaches the gateway gets this one object."""
    from kew.openshell import OpenShellClient

    monkeypatch.setattr(
        OpenShellClient,
        "from_settings",
        classmethod(lambda cls, settings, environment=None: gateway),
    )


def _certify(store: Any, config: Any, *, flow: str) -> None:
    """Append a passing certification for one boundary, as the command does."""
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
        file_digest,
    )
    from kew.openshell_egress import (
        openshell_gateway_identity,
        openshell_policy_path,
        openshell_supervisor_digest,
    )
    from kew.readiness import PacketProbeStatus
    from kew.runtime_names import OPENSHELL_RUNTIME

    certification = RuntimeEgressCertification(
        schema_version=CERTIFICATION_SCHEMA_VERSION,
        runtime=OPENSHELL_RUNTIME,
        image_id=IMAGE_ID,
        policy_sha256=file_digest(openshell_policy_path("claude-code", flow=flow)),
        gateway_version="0.1.2",
        gateway_identity=openshell_gateway_identity(FakeGateway()),  # type: ignore[arg-type]
        supervisor_digest=openshell_supervisor_digest(),
        packets=(
            (
                "/usr/bin/node",
                build_packet_evidence(
                    positive_control=PacketProbeStatus.REACHABLE,
                    observations=dict.fromkeys(
                        OPENSHELL_PACKET_DENIALS, PacketProbeStatus.DENIED
                    ),
                    required=OPENSHELL_PACKET_DENIALS,
                    controls=dict.fromkeys(
                        OPENSHELL_PACKET_CONTROLS, PacketProbeStatus.REACHABLE
                    ),
                ),
            ),
        ),
    )
    append_runtime_egress_certification(
        store, certification, actor=egress_certification_actor(OPENSHELL_RUNTIME)
    )


def _worktree(tmp_path: Path) -> Path:
    """A reviewed checkout the provisioner will accept as a kew project."""
    worktree = tmp_path / "worktree"
    (worktree / "kew").mkdir(parents=True)
    (worktree / "kew" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (worktree / "kew" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return worktree


def _ctx(tmp_path: Path, config: Any) -> ReviewerLaunchContext:
    return ReviewerLaunchContext(
        item_id=ITEM,
        run_id=RUN_ID,
        prompt="review this change",
        prompt_artifact_sha="sha256:prompt",
        head_sha=HEAD,
        worktree=_worktree(tmp_path),
        log_dir=Path(config.logging.dir).resolve(),
    )


def _review(store: Any, config: Any, ctx: ReviewerLaunchContext) -> Any:
    """Run the production launcher with the container stream faked at nothing.

    Nothing between the launcher and the gateway is replaced: the argv build, the
    sandbox create, the upload, the exec environment and the stream capture are
    all production, so what the assertions read is what a real review would do.
    """
    from kew.graph.driver.review_launcher import sandbox_reviewer_launcher

    return sandbox_reviewer_launcher(config, store)(ctx)


def _postures(store: Any) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in store.list_audit_events(event="sandbox_resolved", limit=None)
        if event.payload is not None
    ]


# --------------------------------------------------------------------------- #
# AC1 — the backend, and no fallback                                          #
# --------------------------------------------------------------------------- #
def test_the_reviewer_launches_through_the_openshell_backend(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``runtime = "openshell"`` puts the reviewer in a gateway sandbox.

    Under its OWN policy, not the dispatch one: the reviewer executes the
    reviewed PR's tests, so the boundary it runs behind is not the boundary a
    dispatch was certified for.
    """
    import kew.dispatch_core as dispatch_core
    from kew.openshell_egress import openshell_policy_path

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)
    ctx = _ctx(tmp_path, config)

    with patch.object(dispatch_core, "_launch_container") as docker:
        collected = _review(store, config, ctx)

    docker.assert_not_called()
    created = gateway.created("review")
    assert created["name"].startswith("kew-4221-")
    assert created["image"] == IMAGE_ID
    assert (
        created["policy"]
        == openshell_policy_path("claude-code", flow=REVIEW_FLOW).read_bytes()
    )
    assert collected.parsed.verdict == "READY"


def test_a_reviewer_that_cannot_get_a_sandbox_refuses_rather_than_using_docker(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No gateway, no review. The docker route is not a fallback.

    Falling back would put the reviewer's real credential in the environment of
    the one launch that executes code written by another agent — the exposure
    this node exists to close — and it would do it silently, on a host the
    operator configured for the gateway.
    """
    import kew.dispatch_core as dispatch_core
    from kew.openshell import OpenShellMissing

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(
        version_error=OpenShellMissing(
            ("openshell", "version"), reason="the openshell CLI is not installed"
        )
    )
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    with patch.object(dispatch_core, "_launch_container") as docker:
        collected = _review(store, config, _ctx(tmp_path, config))

    docker.assert_not_called()
    assert "sandbox_create" not in gateway.names
    assert collected.error_category == "sandbox_refused"
    assert collected.parsed.verdict != "READY"
    assert store.get(RUN_ID).error_category == "sandbox_refused"


def test_a_reviewer_pinned_off_claude_is_refused_by_the_verdict_channel_gate(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which gate an ``opencode`` reviewer really meets, named as itself.

    This test used to claim the review-POLICY refusal below, and never reached
    it: the verdict-channel gate (``STREAM_VERDICT_RUNNERS``) is asked first
    and refuses any runner without a typed terminal result event, so the
    launcher never asks the OpenShell layer anything (PR #2520 review). The
    two are told apart here rather than left to a shared
    ``error_category``: this one names the verdict channel, resolves no
    boundary, and therefore attests no posture at all.
    """
    config = _config(tmp_path, runtime="openshell")
    config.review.runner = "opencode"
    config.sandbox.images["opencode"] = "kew-agent-opencode:docker-000000000000"
    gateway = FakeGateway()
    _install_gateway(monkeypatch, gateway)

    collected = _review(store, config, _ctx(tmp_path, config))

    assert collected.error_category == "sandbox_refused"
    assert "sandbox_create" not in gateway.names
    host_failure = json.loads(collected.stored_path.read_text(encoding="utf-8"))[
        "host_failure"
    ]
    assert "verdict channel" in host_failure
    # Nothing was resolved, so nothing is attested: the posture writers run
    # only past this gate, and a refusal here must not claim a boundary.
    assert _postures(store) == []


def test_the_review_policy_refusal_names_the_flow_it_could_not_resolve(
    tmp_path: Path, store: Any
) -> None:
    """Only ``claude-code`` ships a review policy, and the refusal says so.

    Asserted on the production gate itself
    (``review_launcher._openshell_review_refusal``) rather than through the
    launcher, because the verdict-channel gate above refuses every runner that
    could reach this one first. It stays a gate: the launch and admission
    paths ask the same question for a dispatch, and a reviewer admitted on the
    strength of a DISPATCH policy it will not run under is exactly what it
    exists to stop.
    """
    from kew.graph.driver.review_launcher import (
        REFUSAL_OPENSHELL_REVIEW_UNLAUNCHABLE,
        _openshell_review_refusal,
    )
    from kew.openshell_egress import packaged_openshell_policy_runners

    config = _config(tmp_path, runtime="openshell")

    refused = _openshell_review_refusal(config, runner="opencode", store=store)

    assert packaged_openshell_policy_runners(REVIEW_FLOW) == ("claude-code",)
    assert refused is not None
    reason, token = refused
    assert REVIEW_FLOW in reason and "opencode" in reason
    assert token == REFUSAL_OPENSHELL_REVIEW_UNLAUNCHABLE


# --------------------------------------------------------------------------- #
# AC2 — no credential in either sandbox                                       #
# --------------------------------------------------------------------------- #
def test_the_reviewer_sandbox_sees_the_placeholder_and_never_the_real_token(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider holds the credential; the sandbox holds a placeholder.

    Both halves are asserted: the create names the provider the launch
    authenticates through, and the exec's whole forwarded environment is swept
    for the real value.
    """
    from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    _review(store, config, _ctx(tmp_path, config))

    assert gateway.created("review")["providers"] == ("kew-anthropic",)
    review_env = gateway.exec_envs[-1]
    assert review_env is not None
    assert review_env["ANTHROPIC_API_KEY"] == PLACEHOLDER_CREDENTIAL_VALUE
    assert "ANTHROPIC_API_KEY" in gateway.exec_retains[-1]
    for forwarded in gateway.exec_envs:
        assert REAL_TOKEN not in (forwarded or {}).values()


def test_the_provisioning_sandbox_is_created_with_no_provider_at_all(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not even a placeholder: nothing in there authenticates to anything.

    The install runs the reviewed PR's own build backend. A provider on that
    sandbox would be a credential kew could not account for reaching code it
    has not read, for no benefit — uv needs an index, not a model.
    """
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    _review(store, config, _ctx(tmp_path, config))

    provisioning = gateway.created("review-provision")
    assert provisioning["providers"] == ()
    install_env = gateway.exec_envs[0]
    assert install_env is not None
    assert install_env["UV_PROJECT_ENVIRONMENT"] == "/work/kew/.venv"
    # The pin plus the baseline, and NOTHING else: every name that could hold a
    # credential is outside the exec allowlist, so this is the whole surface.
    assert set(install_env) <= {*SANDBOX_EXEC_ENV_NAMES, "UV_PROJECT_ENVIRONMENT"}
    assert REAL_TOKEN not in install_env.values()


def test_the_provisioning_exec_client_is_launched_with_a_path_and_a_home(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway CLI is a host program, and it needs the baseline to run.

    ``sandbox_exec`` intersects whatever it is handed with the admitted names
    (``kew.openshell.SANDBOX_EXEC_ENV_NAMES``), so passing the environment pin
    ALONE launched the openshell client with neither ``PATH`` nor ``HOME`` —
    the two names every other call site forwards and that the allowlist
    documents as required (PR #2520 review).

    ``PATH`` is the fixed sandbox one rather than the host's: the client's
    environment is the sandbox's, and a host PATH naming the checkout let the
    PR's own root ``uv`` run in place of the governed one (#2637). The install
    names uv by its absolute governed path as well.
    """
    from kew.openshell import SANDBOX_PATH
    from kew.review_environment import UV_PROGRAM

    monkeypatch.setenv("PATH", f"/work:.:{os.environ['PATH']}")
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    _review(store, config, _ctx(tmp_path, config))

    install_env = gateway.exec_envs[0]
    assert install_env is not None
    assert install_env["PATH"] == SANDBOX_PATH
    assert install_env["HOME"] == os.environ["HOME"]
    uv_execs = [
        call.detail["argv"]
        for call in gateway.calls
        if call.name == "sandbox_exec" and "sync" in call.detail["argv"]
    ]
    assert uv_execs and all(argv[0] == UV_PROGRAM for argv in uv_execs)


def test_the_provisioning_policy_governs_the_install_sandbox(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install runs under the package-index policy, never the reviewer's."""
    from kew.openshell_egress import openshell_policy_path

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    _review(store, config, _ctx(tmp_path, config))

    assert gateway.created("review-provision")["policy_path"] == openshell_policy_path(
        "claude-code", flow=PROVISION_FLOW
    )


def test_provisioning_and_review_never_share_a_sandbox(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sandboxes, and the install one is gone before the reviewer starts.

    Ordering matters as much as separation: the install sandbox holds a tree the
    PR's build backend wrote into, and the reviewer must not be able to reach
    it. The provisioned environment travels on the realized download instead.
    """
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)
    ctx = _ctx(tmp_path, config)

    _review(store, config, ctx)

    install = gateway.created("review-provision")["name"]
    review = gateway.created("review")["name"]
    assert install != review
    order = [
        (call.name, call.detail.get("name"))
        for call in gateway.calls
        if call.name in {"sandbox_create", "sandbox_delete"}
    ]
    assert order.index(("sandbox_delete", install)) < order.index(
        ("sandbox_create", review)
    )
    # And the environment the install built reached the reviewed checkout, so
    # the reviewer's uploaded copy carries it.
    assert (ctx.worktree / "kew" / ".venv" / "pyvenv.cfg").is_file()


# --------------------------------------------------------------------------- #
# AC4 — a posture on every reviewer launch, on both routes                     #
# --------------------------------------------------------------------------- #
def test_the_openshell_reviewer_records_a_certified_posture(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sandbox_resolved``, with the certified value its gate proved."""
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    _review(store, config, _ctx(tmp_path, config))

    [posture] = _postures(store)
    assert posture["posture"] == "sandboxed"
    assert posture["runtime"] == "openshell"
    assert posture["egress_verification"] == "openshell-certified"
    assert posture["egress_verification_basis"] == "openshell-selftest"
    assert posture["image"] == IMAGE_ID
    # The provider holds the credential; every variable claude-code declares is
    # named as holding a placeholder, and none as holding a value.
    assert posture["credential_names"] == [
        "provider:kew-anthropic",
        "placeholder:ANTHROPIC_API_KEY",
        "placeholder:CLAUDE_CODE_OAUTH_TOKEN",
    ]


def test_the_docker_reviewer_records_the_unverified_posture_it_really_has(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docker route is not certifiable, and now says so out loud.

    It was the absence of this record that made the reviewer's exposure
    invisible: the credential is in the container's environment and the network
    is whatever the operator configured, which is exactly what
    ``operator-managed-unverified`` means.
    """
    from support.review_stream import FakeReviewerContainer, draining_stream

    import kew.dispatch_core as dispatch_core
    from kew.sandbox import DIRECT_ROUTE_EGRESS_VERIFICATION

    config = _config(tmp_path, runtime="docker")
    container = FakeReviewerContainer(reviewer_stream(_READY, HEAD))
    stream = draining_stream()
    with (
        patch.object(dispatch_core, "_launch_container", return_value=container),
        patch.object(dispatch_core, "_stream_sandbox_log", side_effect=stream),
        patch("kew.review_environment.provision_review_environment", return_value=True),
    ):
        _review(store, config, _ctx(tmp_path, config))

    [posture] = _postures(store)
    assert posture["posture"] == "sandboxed"
    assert posture["runtime"] == "docker"
    assert posture["egress_verification"] == DIRECT_ROUTE_EGRESS_VERIFICATION
    assert posture["egress_verification_basis"] == "operator-asserted"
    # The docker reviewer's credential is a VALUE in the container environment,
    # which is the exposure the OpenShell route removes.
    assert posture["credential_names"] == ["ANTHROPIC_API_KEY"]


def test_a_refused_reviewer_records_a_refused_posture_with_its_cause(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal leaves the same kind of record a refused dispatch does.

    Silence for a launch that did not happen is how "was this contained?" became
    unanswerable for the reviewer in the first place.
    """
    from kew.sandbox import REFUSAL_OPENSHELL_UNCERTIFIED

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway()
    _install_gateway(monkeypatch, gateway)

    _review(store, config, _ctx(tmp_path, config))

    [posture] = _postures(store)
    assert posture["posture"] == "refused"
    assert posture["runtime"] is None
    assert posture["reason"] == REFUSAL_OPENSHELL_UNCERTIFIED
    assert posture["egress_verification"] == "unavailable"


# --------------------------------------------------------------------------- #
# AC5 — the reviewer's own certification                                      #
# --------------------------------------------------------------------------- #
def test_an_uncertified_openshell_reviewer_refuses(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No proof for the reviewer boundary, no reviewer — and no sandbox."""
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway()
    _install_gateway(monkeypatch, gateway)

    collected = _review(store, config, _ctx(tmp_path, config))

    assert collected.error_category == "sandbox_refused"
    assert "sandbox_create" not in gateway.names
    reason = collected.parsed.unparseable_reason or ""
    assert "no openshell egress certification matches this boundary" in reason
    assert "--flow review" in reason


def test_a_dispatch_certification_does_not_satisfy_the_reviewer_boundary(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one property the separate policy file buys.

    Same image, same gateway, same launcher — a different policy digest. An
    operator who certified the dispatch boundary and nothing else has proven
    nothing about the launch that executes the reviewed PR's tests, and a gate
    that matched on the dispatch digest would tell them otherwise.
    """
    from kew.openshell_egress import DISPATCH_FLOW

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway()
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=DISPATCH_FLOW)

    collected = _review(store, config, _ctx(tmp_path, config))

    assert collected.error_category == "sandbox_refused"
    assert "sandbox_create" not in gateway.names
    # And the remedy names the flow whose proof is missing, not the one that is
    # already on the chain.
    assert "--flow review" in (collected.parsed.unparseable_reason or "")


# --------------------------------------------------------------------------- #
# AC6 — the verdict channel survives the N6 stream                            #
# --------------------------------------------------------------------------- #
def test_the_typed_verdict_survives_the_openshell_exec_stream(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result event the capture stores is the one the sandbox emitted.

    ``openshell sandbox exec`` gives ONE merged pipe, so the review contract's
    checks — one result event, terminal, ``subtype == "success"``, exit 0 — are
    applied to a transcript the gateway folded rather than to a docker one. The
    capture is re-parsed here through the same production reader the stage uses.
    """
    from kew.review_contract import collect_stream_verdict
    from kew.review_stream import read_capture

    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)
    ctx = _ctx(tmp_path, config)

    collected = _review(store, config, ctx)

    assert collected.parsed.verdict == "READY"
    capture = read_capture(Path(config.logging.dir) / f"review-{RUN_ID}.log")
    assert capture is not None
    # The contract's four checks, read off the capture the gateway's one merged
    # pipe produced: exactly one result event, terminal, success, exit 0.
    assert capture["result_events"] == 1
    assert capture["terminal_is_result"] is True
    assert capture["subtype"] == "success"
    assert capture["is_error"] is False
    assert capture["unparsed_lines"] == 0
    assert capture["split_lines"] == 0
    assert json.loads(capture["verdict_block"])["verdict"] == "READY"
    # And re-collecting that stored transcript gives the same verdict, which is
    # what makes the capture the record rather than a by-product.
    again = collect_stream_verdict(
        capture,
        head_sha=HEAD,
        exit_code=0,
        dest_dir=tmp_path / "again",
        run_id=RUN_ID,
    )
    assert again.parsed.verdict == "READY"


# --------------------------------------------------------------------------- #
# AC7 — one real round on the operator gateway                                #
# --------------------------------------------------------------------------- #
def _operator_credential_values(
    declared: tuple[str, ...], scrubbed: dict[str, str]
) -> set[str]:
    """The real credential values the live round sweeps its sandbox for.

    NOT ``os.environ``: ``tests/conftest.py`` deletes every secret-looking
    variable at import, which is exactly the set ``declared`` names, so a sweep
    built from the environment of this process is empty and "no real credential
    reached the sandbox" would hold however badly the launch leaked (PR #2520
    review). The values come from conftest's ``live_round_credentials``
    fixture, which holds them only for an opted-in live run and only until the
    round has used them (#2638).

    Refuses an empty sweep rather than returning one: the operator's shell must
    export the credential their gateway provider holds, or this round proves
    nothing about leakage and should say so instead of passing.
    """
    values = {scrubbed[name] for name in declared if scrubbed.get(name)}
    assert values, (
        "nothing for the leak sweep to look for: export the credential the "
        f"gateway provider holds as one of {', '.join(declared)} in the shell "
        "that runs this round"
    )
    return values


@pytest.mark.live_openshell
@pytest.mark.skipif(
    os.environ.get("KEW_LIVE_OPENSHELL") != "1",
    reason="needs an operator OpenShell gateway; set KEW_LIVE_OPENSHELL=1",
)
def test_live_openshell_reviewer_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_round_credentials: dict[str, str],
) -> None:
    """One real review round in a real sandbox, with no real token inside it.

    What the fakes above cannot show: that a sandbox created from kew's image
    under the REVIEWER policy, with the credential held in a gateway provider
    and only a placeholder inside, actually reaches the model, answers, and
    returns a verdict kew records — and that the reviewer's own environment is
    provisioned in a second sandbox that never held a provider.

    The gateway, the provider, the image and the reviewed checkout all come from
    the operator's environment; nothing here invents a default that would dial
    somewhere. Skipped in CI by both the marker and the variable.

    The round also needs the credential value its gateway provider holds
    exported in the shell that starts it (``ANTHROPIC_API_KEY`` or
    ``CLAUDE_CODE_OAUTH_TOKEN``) — not to run the review, which reads it from
    the provider, but to give the leak sweep below a real value to look for.
    See :func:`_operator_credential_values`; without one the round refuses.
    """
    import shutil

    from kew import subprocess_utils
    from kew.openshell import OpenShellClient
    from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE
    from kew.openshell_egress import certify_openshell_egress
    from kew.sandbox import runner_credential_env_vars
    from kew.subprocess_utils import TOOL_PATHS, run

    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    assert subprocess_utils.TOOL_PATHS["git"] == git

    config = _config(tmp_path, runtime="openshell")
    config.sandbox.images["claude-code"] = os.environ["KEW_LIVE_OPENSHELL_IMAGE"]
    config.sandbox.openshell.providers = {
        "claude-code": os.environ["KEW_LIVE_OPENSHELL_PROVIDER"]
    }
    config.sandbox.openshell.gateway = os.environ.get("KEW_LIVE_OPENSHELL_GATEWAY", "")
    config.review.model = os.environ.get("KEW_LIVE_OPENSHELL_MODEL", "")
    OpenShellClient.from_settings(config.sandbox.openshell).check_version()

    worktree = _worktree(tmp_path)
    run(["git", "init", "-q", "-b", "main"], cwd=worktree, check=True)
    run(["git", "add", "-A"], cwd=worktree, check=True)
    run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=worktree,
        check=True,
    )
    head = run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True).stdout.strip()

    # A pass-through recorder on the real client. Nothing about the exec
    # changes — the real method runs with the real arguments — but the
    # environment the host hands each one becomes readable, which is what
    # makes "only the placeholder crossed" assertable on a real gateway and
    # not just against the double above.
    forwarded: list[dict[str, str]] = []
    real_exec = OpenShellClient.sandbox_exec

    def _recording_exec(client: Any, name: str, **kwargs: Any) -> Any:
        forwarded.append(dict(kwargs.get("env") or {}))
        return real_exec(client, name, **kwargs)

    monkeypatch.setattr(OpenShellClient, "sandbox_exec", _recording_exec)

    with SqliteRunStore(str(tmp_path / "live.db"), project_root=tmp_path) as store:
        # The real boundary, proven for the real reviewer policy on this host.
        proof = certify_openshell_egress(
            config, runner="claude-code", store=store, flow=REVIEW_FLOW
        )
        assert proof.passed, proof.details
        ctx = ReviewerLaunchContext(
            item_id=ITEM,
            run_id=RUN_ID,
            prompt=(
                "This is a liveness check, not a real review. Reply with the "
                "verdict block below and nothing else."
            ),
            prompt_artifact_sha="sha256:live",
            head_sha=head,
            worktree=worktree,
            log_dir=Path(config.logging.dir).resolve(),
        )
        collected = _review(store, config, ctx)
        postures = _postures(store)

    assert collected.parsed.verdict in {"READY", "CHANGES_REQUESTED"}, collected
    assert postures[-1]["egress_verification"] == "openshell-certified"

    # Only the placeholder crossed into the real sandbox: every credential
    # variable claude-code declares arrived bound to it, which is the whole
    # point of holding the credential in a gateway provider. This is the half
    # that cannot go vacuous — it asserts a value that must be present, not
    # the absence of one this process may not even be able to see.
    declared = tuple(runner_credential_env_vars("claude-code"))
    assert declared, "claude-code must declare a model credential"
    reviewer_env = forwarded[-1]
    assert [reviewer_env.get(name) for name in declared] == [
        PLACEHOLDER_CREDENTIAL_VALUE
    ] * len(declared)

    # And no real credential value anywhere the sandbox produced or was
    # handed. The module's fake token is NOT in force here (``_is_live``), so
    # the only values swept for are the operator's own: a sweep for the fake
    # would be a sweep for a value the gateway provider never holds.
    assert REAL_TOKEN not in os.environ.values(), (
        "the module's fake credential is still planted for the live round, so "
        "the sweep below would be looking for a value nothing can leak"
    )
    transcript = (Path(config.logging.dir) / f"review-{RUN_ID}.log").read_text(
        encoding="utf-8", errors="replace"
    )
    for value in _operator_credential_values(declared, live_round_credentials):
        assert value not in transcript
        for env in forwarded:
            assert value not in env.values()


def test_every_host_fake_in_this_module_steps_aside_for_the_live_round() -> None:
    """The AC7 round must meet the operator's host, not this module's doubles.

    Nothing else can cover this: the live test is skipped wherever this suite
    runs, so an autouse fixture that forgets to stand down is invisible until
    an operator runs the round and gets a test of the fakes. The two that
    matter most are an image id (``openshell_launch`` hands the resolved id
    straight to ``sandbox_create``, so one invented here names an image the
    gateway cannot pull) and a planted credential (it turns the "no real
    token" sweep into a search for a value the provider never holds) — PR
    #2520 review.

    Asserted on behaviour, not on source text: each fixture is called twice
    with a recording stand-in for ``monkeypatch``, once for a live node and
    once for an ordinary one, and must patch nothing in the first case and
    something in the second.
    """

    class _Recorder:
        """Counts the fakes a fixture installs, without installing any."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def setattr(self, *_a: Any, **_k: Any) -> None:
            self.calls.append("setattr")

        def setenv(self, *_a: Any, **_k: Any) -> None:
            self.calls.append("setenv")

    def _request(*, live: bool) -> Any:
        marker = pytest.mark.live_openshell.mark if live else None
        node = SimpleNamespace(
            get_closest_marker=lambda name: marker if name == LIVE_MARKER else None
        )
        return SimpleNamespace(node=node)

    autouse = []
    for value in globals().values():
        marker = getattr(value, "_fixture_function_marker", None)
        if marker is not None and marker.autouse:
            autouse.append(value)
    assert len(autouse) == 2, (
        f"expected both autouse host fixtures, found {[f.name for f in autouse]} "
        "— a renamed pytest fixture internal would silently assert nothing"
    )

    for fixture in autouse:
        function = fixture._get_wrapped_function()
        live_recorder = _Recorder()
        function(_request(live=True), live_recorder)
        assert live_recorder.calls == [], (
            f"{fixture.name} fakes the host for the live round: {live_recorder.calls}"
        )
        plain_recorder = _Recorder()
        function(_request(live=False), plain_recorder)
        assert plain_recorder.calls, (
            f"{fixture.name} fakes nothing at all, so the gate above is empty"
        )

    live = globals()["test_live_openshell_reviewer_round"]
    assert any(mark.name == LIVE_MARKER for mark in live.pytestmark), (
        "the live round must carry the marker every fixture above reads"
    )


def test_the_live_rounds_leak_sweep_cannot_look_for_nothing() -> None:
    """The AC7 sweep must hold a real value to look for, or refuse the round.

    This is the shipped defect, not a hypothetical one: the sweep was built
    from ``os.environ``, and ``tests/conftest.py`` deletes every secret-looking
    variable at import — which is exactly the set claude-code declares — so on
    the operator's own host it iterated an empty set and neither the transcript
    nor the per-exec leak assertion ever ran (PR #2520 review).

    The live round cannot cover its own sweep: it is skipped wherever this
    suite runs, so a vacuous one is invisible until an operator trusts it.
    """
    import inspect

    import conftest

    from kew.sandbox import runner_credential_env_vars

    declared = tuple(runner_credential_env_vars("claude-code"))
    assert declared, "claude-code must declare a model credential"
    # Why the environment cannot be the source: conftest removes every name
    # this predicate accepts, before this module is collected.
    assert [name for name in declared if not conftest.is_secret_env_name(name)] == []

    # An empty hold fails the round rather than passing it in silence.
    with pytest.raises(AssertionError, match="nothing for the leak sweep"):
        _operator_credential_values(declared, {})

    # And a live run's hold gives the sweep the operator's own value — only for
    # the names this runner declares, never another runner's.
    held = {declared[0]: REAL_TOKEN, "LINEAR_API_KEY": "some-other-secret"}
    assert _operator_credential_values(declared, held) == {REAL_TOKEN}

    # And the round itself reaches the sweep through here, fed by the conftest
    # fixture, which is the only part of this a suite that always skips the
    # round can check: a call site that went back to reading ``os.environ``
    # would be vacuous again.
    live = globals()["test_live_openshell_reviewer_round"]
    assert _operator_credential_values.__name__ in live.__code__.co_names, (
        "the live round must build its leak sweep from the conftest hold"
    )
    assert conftest._LIVE_ROUND_FIXTURE in inspect.signature(live).parameters, (
        "the live round must request the fixture that holds the credentials"
    )


# --------------------------------------------------------------------------- #
# Provisioning: fail closed on a sandbox nobody confirmed gone                 #
# --------------------------------------------------------------------------- #
def test_a_provisioning_sandbox_that_cannot_be_deleted_stops_the_review(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install sandbox kew cannot prove is gone must not outlive its review.

    It holds a tree the reviewed PR's build backend wrote into, and on this
    runtime ending a workload IS deleting the sandbox — so a delete whose
    outcome the gateway could not report leaves the review refused rather than
    started beside a sandbox that may still be running.
    """
    from kew.openshell import OpenShellError

    config = _config(tmp_path, runtime="openshell")

    class _StuckDelete(FakeGateway):
        def sandbox_delete(self, name: str) -> None:
            super().sandbox_delete(name)
            if name.startswith("kew-review-env-"):
                raise OpenShellError(("openshell", "sandbox", "delete"), reason="busy")

    gateway = _StuckDelete(transcript=reviewer_stream(_READY, HEAD))
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)

    collected = _review(store, config, _ctx(tmp_path, config))

    assert collected.parsed.verdict is None
    assert (
        "review"
        not in [
            call.detail["labels"].get("kew.flow")
            for call in gateway.calls
            if call.name == "sandbox_create"
        ][1:]
    ), "the reviewer launched beside an unconfirmed install sandbox"


def test_a_failed_install_degrades_the_review_rather_than_blocking_it(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install that exits non-zero is reported; the review still runs.

    Unchanged from the docker route on purpose: a reviewer with no installed
    environment reports unverifiable acceptance, which is an answer. Only a
    sandbox kew cannot account for is fail-closed.
    """
    config = _config(tmp_path, runtime="openshell")
    gateway = FakeGateway(transcript=reviewer_stream(_READY, HEAD), install_exit=2)
    _install_gateway(monkeypatch, gateway)
    _certify(store, config, flow=REVIEW_FLOW)
    ctx = _ctx(tmp_path, config)

    collected = _review(store, config, ctx)

    assert collected.parsed.verdict == "READY"
    assert gateway.created("review")["name"].startswith("kew-4221-")
    assert not (ctx.worktree / "kew" / ".venv").exists()


# --------------------------------------------------------------------------- #
# Provisioning: the two host caps the exec pipe has no runtime flag for        #
# --------------------------------------------------------------------------- #
class _LiveStream:
    """An exec pipe that stays OPEN and silent, the way a live build backend does.

    The reviewed PR's own build backends are what ``uv`` executes, so "still
    running, printing nothing" is a state the host has to survive rather than
    an unlikely one. ``readline`` blocks until the test releases it, which is
    what a real pipe with a live writer at the other end does — and what no
    deadline-free reader ever returns from.
    """

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.closed = False

    def readline(self, limit: int | None = None) -> str:
        self._release.wait(timeout=30.0)
        return ""

    def __iter__(self) -> _LiveStream:
        return self

    def __next__(self) -> str:
        if not self.readline():
            raise StopIteration
        raise AssertionError("unreachable: this stream only ever ends")

    def close(self) -> None:
        self.closed = True


def _provision_only(gateway: FakeGateway, tmp_path: Path, config: Any, **kwargs: Any):
    """Run the real provisioner against *gateway*; whether it installed."""
    from kew.review_environment import provision_review_environment

    return provision_review_environment(
        config,
        image=IMAGE_ID,
        runner="claude-code",
        checkout_dir=_worktree(tmp_path),
        slug=ITEM,
        **kwargs,
    )


def _provision_deletes(gateway: FakeGateway) -> list[str]:
    return [
        call.detail["name"]
        for call in gateway.calls
        if call.name == "sandbox_delete"
        and call.detail["name"].startswith("kew-review-env-")
    ]


def test_a_silent_install_is_cut_off_at_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A build backend that stays alive and quiet must not hold the review stage.

    The provisioning read used to be a plain ``for line in stream``, which has
    no deadline at all: an exec whose pipe stayed open and silent held the
    reader — and the whole review stage behind it — for as long as the workload
    chose to live. The docker path's equivalent read is output-independent and
    cuts off at the deadline, and this one now is too (PR #2520 review).
    """
    release = threading.Event()

    class _Silent(FakeGateway):
        def sandbox_exec(self, name: str, **kwargs: Any) -> _Exec:
            super().sandbox_exec(name, **kwargs)
            process = MagicMock()
            process.stdout = _LiveStream(release)
            return _Exec(process)

    config = _config(tmp_path, runtime="openshell")
    gateway = _Silent()
    _install_gateway(monkeypatch, gateway)

    try:
        installed = _provision_only(gateway, tmp_path, config, timeout_seconds=0.05)
    finally:
        release.set()

    assert installed is False
    assert "host cap" in capsys.readouterr().err
    # And the sandbox that held the live workload is gone, not left running.
    assert _provision_deletes(gateway)


def test_a_transcript_flood_ends_the_install_rather_than_being_left_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cap breach ends the WORKLOAD, as the docker path's kill does.

    Stopping the reader alone left the sandbox running and handed everything it
    printed next to ``OpenShellExec.wait``, which takes the whole remainder off
    the pipe into one host string — so the breach that exists to bound host
    memory grew it instead (PR #2520 review).
    """
    import kew.review_environment as provisioner

    monkeypatch.setattr(provisioner, "TRANSCRIPT_BYTE_CAP", 64)
    killed: list[int] = []

    class _Flood(FakeGateway):
        def sandbox_exec(self, name: str, **kwargs: Any) -> _Exec:
            super().sandbox_exec(name, **kwargs)
            process = MagicMock()
            process.stdout = _Stream(["x" * 40 + "\n"] * 100)
            process.kill.side_effect = lambda: killed.append(1)
            return _Exec(process)

    config = _config(tmp_path, runtime="openshell")
    gateway = _Flood()
    _install_gateway(monkeypatch, gateway)

    installed = _provision_only(gateway, tmp_path, config)

    assert installed is False
    assert "host cap" in capsys.readouterr().err
    assert killed, "the exec client was left reading past the transcript cap"
    # The sandbox is deleted on the breach itself, not only by the cleanup that
    # follows it: on this runtime that delete IS the end of the workload.
    assert len(_provision_deletes(gateway)) == 2
