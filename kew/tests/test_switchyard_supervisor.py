"""Managed Switchyard process lifecycle and orchestration tests."""

from __future__ import annotations

import asyncio
import hashlib
import io
import signal
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.config import DispatchConfig, SwitchyardConfig
from kew.inference_transport import ResolvedInferenceTransport
from kew.orchestrator import run_agent
from kew.runners.protocol import ResultClassification
from kew.subprocess_utils import pin_executable, popen_pinned
from kew.switchyard import (
    ProbeResponse,
    SwitchyardProbeResult,
    probe_switchyard_endpoint,
)
from kew.switchyard_metrics import parse_switchyard_metrics
from kew.switchyard_supervisor import (
    ManagedSwitchyardSupervisor,
    PacketProbeStatus,
    SidecarAttemptSupervisor,
    SwitchyardSupervisorError,
    _route_credential_names,
    build_switchyard_sidecar_argv,
    evaluate_topology_denial,
)


class _FakeProcess:
    _next_pid = 4100

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stdout = io.StringIO("server output\n")
        self.stderr = io.StringIO("server error\n")
        self.returncode: int | None = None
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        self.returncode = -signal.SIGTERM
        return self.returncode


def _settings(
    tmp_path: Path,
    *,
    endpoint: str = "http://127.0.0.1:4400",
    startup_timeout_seconds: int = 2,
    credential_env: tuple[str, ...] = (),
    route_credential_env: str | None = None,
) -> SwitchyardConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / f"switchyard-{endpoint.rsplit(':', 1)[-1]}"
    binary.write_bytes(b"pinned switchyard executable")
    binary.chmod(0o700)
    config_path = tmp_path / f"routes-{endpoint.rsplit(':', 1)[-1]}.toml"
    credential_line = (
        f'api_key_env = "{route_credential_env}"\n'
        if route_credential_env is not None
        else ""
    )
    config_path.write_text(
        "schema_version = 1\n"
        "[llm_clients.primary]\n"
        'format = "openai_responses"\n'
        'base_url = "https://api.example/v1"\n'
        f"{credential_line}"
        "[targets.model]\n"
        'id = "provider/model"\n'
        'llm_client = "primary"\n'
        "[routes.passthrough]\n"
        'id = "exact-route"\n'
        'type = "passthrough"\n'
        'target = "model"\n'
    )
    return SwitchyardConfig(
        endpoint=endpoint,
        route="exact-route",
        config_path=config_path,
        expected_version="0.1.0",
        managed=True,
        binary=binary,
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        startup_timeout_seconds=startup_timeout_seconds,
        credential_env=credential_env,
    )


def _ready_probe(
    *, endpoint: str, route: str, timeout_seconds: float
) -> SwitchyardProbeResult:
    assert timeout_seconds > 0
    return SwitchyardProbeResult(
        admitted=True,
        reason="ready",
        endpoint=endpoint,
        route=route,
        observed_version=None,
    )


def _install_process_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[object, list[str], dict[str, object]]], list[tuple[int, int]]]:
    from kew import switchyard_supervisor

    launches: list[tuple[object, list[str], dict[str, object]]] = []
    signals: list[tuple[int, int]] = []

    def fake_run(pinned, args, **kwargs):
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "switchyard-server 0.1.0\n", "")
        assert args[-1] == "--dry-run"
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    def fake_popen(pinned, args, **kwargs):
        launches.append((pinned, list(args), dict(kwargs)))
        return _FakeProcess()

    monkeypatch.setattr(switchyard_supervisor, "run_pinned", fake_run)
    monkeypatch.setattr(switchyard_supervisor, "popen_pinned", fake_popen)
    monkeypatch.setattr(switchyard_supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        switchyard_supervisor.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    return launches, signals


def test_managed_switchyard_uses_private_snapshot_and_exact_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    source_bytes = settings.config_path.read_bytes()  # type: ignore[union-attr]
    launches, signals = _install_process_fakes(monkeypatch)

    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe) as managed:
        assert managed.is_healthy()
        assert managed.config_sha256 == hashlib.sha256(source_bytes).hexdigest()
        assert managed.observed_version == "0.1.0"
        assert len(launches) == 1
        pinned, args, kwargs = launches[0]
        snapshot = Path(args[args.index("--config") + 1])
        assert pinned.path == settings.binary
        assert snapshot != settings.config_path
        assert snapshot.read_bytes() == source_bytes
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
        assert args[-4:] == ["--host", "127.0.0.1", "--port", "4400"]
        assert kwargs["env"] == {}
        assert kwargs["start_new_session"] is True

    assert not snapshot.exists()
    assert signals == [(managed.pid, signal.SIGTERM)]


def test_managed_switchyard_passes_only_declared_route_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-value-that-must-not-persist"
    settings = _settings(
        tmp_path,
        credential_env=("OPENAI_API_KEY",),
        route_credential_env="OPENAI_API_KEY",
    )
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross-boundary")
    launches, _ = _install_process_fakes(monkeypatch)

    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe) as managed:
        assert managed.credential_env_names == ("OPENAI_API_KEY",)
        _, args, kwargs = launches[0]
        assert kwargs["env"] == {"OPENAI_API_KEY": secret}
        assert secret not in " ".join(args)
        assert secret not in settings.config_path.read_text()  # type: ignore[union-attr]
        assert secret not in repr(managed.__dict__)


def test_switchyard_sidecar_argv_uses_only_private_config_and_final_hardening(
    tmp_path: Path,
) -> None:
    config = tmp_path / "synthetic-switchyard.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    image_id = "sha256:" + "a" * 64

    argv = build_switchyard_sidecar_argv(
        runtime="docker",
        image_id=image_id,
        container_name="kew-switchyard-attempt-1-proxy",
        internal_network="kew-switchyard-attempt-1",
        owner="0123456789abcdef",
        config_path=config,
    )

    assert argv[:2] == ["docker", "create"]
    assert argv[-7:] == [
        image_id,
        "--config",
        "/run/kew-switchyard/routes.toml",
        "--host",
        "0.0.0.0",
        "--port",
        "4000",
    ]
    for expected in (
        "--pull=never",
        "--cap-drop",
        "ALL",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--user",
        "65532:65532",
        "--network",
        "kew-switchyard-attempt-1",
    ):
        assert expected in argv
    joined = " ".join(argv)
    assert str(config.resolve()) in joined
    assert (
        f"type=bind,src={config.resolve()},dst=/run/kew-switchyard/routes.toml,readonly"
    ) in joined
    assert "/work" not in joined
    assert "docker.sock" not in joined
    assert "API_KEY" not in joined


def test_switchyard_sidecar_argv_is_pure_for_declared_config_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-created.toml"

    argv = build_switchyard_sidecar_argv(
        runtime="docker",
        image_id="sha256:" + "a" * 64,
        container_name="kew-switchyard-attempt-1-proxy",
        internal_network="kew-switchyard-attempt-1",
        owner="0123456789abcdef",
        config_path=missing,
    )

    assert str(missing) in argv[argv.index("--mount") + 1]
    assert not missing.exists()


def test_switchyard_sidecar_argv_refuses_a_mutable_image_reference(
    tmp_path: Path,
) -> None:
    config = tmp_path / "synthetic-switchyard.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="immutable"):
        build_switchyard_sidecar_argv(
            runtime="docker",
            image_id="kew-switchyard:latest",
            container_name="kew-switchyard-attempt-1-proxy",
            internal_network="kew-switchyard-attempt-1",
            owner="0123456789abcdef",
            config_path=config,
        )


def test_sidecar_owns_exact_network_and_container_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.sandbox import ImageIdStatus, ImageResolution

    owner = "0" * 32
    proxy_image = "sha256:" + "a" * 64
    worker_image = "sha256:" + "b" * 64
    network_id = "c" * 64
    helper_id = "d" * 64
    proxy_id = "e" * 64
    worker_id = "6" * 64
    worker_environment = [""]
    config_path = tmp_path / "routes.toml"
    config_path.write_text(
        "schema_version = 1\n"
        "[llm_clients.synthetic]\n"
        'format = "openai_responses"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        "[targets.synthetic]\n"
        'id = "synthetic"\n'
        'llm_client = "synthetic"\n'
        "[routes.passthrough]\n"
        'id = "switchyard/passthrough"\n'
        'target = "synthetic"\n',
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    settings = SwitchyardConfig(
        route="switchyard/passthrough",
        expected_version="0.1.0",
        config_path=config_path,
        credential_env=("OPENAI_API_KEY",),
        sidecar={
            "enabled": True,
            "image": "switchyard:pinned",
            "egress_network": "approved-egress",
        },
    )
    commands: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            identity = argv[-1]
            if identity == "approved-egress":
                payload = [
                    {
                        "Id": "f" * 64,
                        "Name": "approved-egress",
                        "Internal": False,
                    }
                ]
            else:
                payload = [
                    {
                        "Id": network_id,
                        "Name": "kew-switchyard-000000000000-internal",
                        "Internal": True,
                        "Labels": {"io.kew.switchyard.attempt": owner},
                        "Containers": {proxy_id: {}},
                    }
                ]
            return subprocess.CompletedProcess(
                argv, 0, __import__("json").dumps(payload), ""
            )
        if argv[1:3] == ["container", "inspect"]:
            if argv[-1] == worker_id:
                payload = [
                    {
                        "Id": worker_id,
                        "Image": worker_image,
                        "Config": {
                            "Entrypoint": ["/bin/sleep"],
                            "Cmd": ["infinity"],
                            "Env": [worker_environment[0]],
                            "Labels": {"io.kew.switchyard.attempt": owner},
                        },
                        "State": {"Running": True},
                        "NetworkSettings": {
                            "Networks": {
                                "kew-switchyard-000000000000-internal": {
                                    "NetworkID": network_id
                                }
                            }
                        },
                        "HostConfig": {
                            "CapAdd": None,
                            "Privileged": False,
                            "ReadonlyRootfs": True,
                            "PidMode": "",
                        },
                        "Mounts": [
                            {
                                "Type": "bind",
                                "Destination": "/work",
                                "Source": str(tmp_path),
                            }
                        ],
                    }
                ]
            else:
                payload = [
                    {
                        "Id": proxy_id,
                        "Image": proxy_image,
                        "Path": "/usr/local/bin/switchyard-server",
                        "Config": {
                            "User": "65532:65532",
                            "Env": [
                                "OTEL_SDK_DISABLED=true",
                                "OPENAI_API_KEY=synthetic-upstream",
                            ],
                            "Labels": {"io.kew.switchyard.attempt": owner},
                        },
                        "State": {"Running": True},
                        "HostConfig": {
                            "CapAdd": None,
                            "Privileged": False,
                            "ReadonlyRootfs": True,
                            "PidMode": "",
                        },
                        "NetworkSettings": {
                            "Networks": {
                                "kew-switchyard-000000000000-internal": {
                                    "NetworkID": network_id,
                                    "IPAddress": "172.30.0.2",
                                },
                                "approved-egress": {
                                    "NetworkID": "f" * 64,
                                    "IPAddress": "172.31.0.2",
                                },
                            }
                        },
                        "Mounts": [
                            {
                                "Type": "volume",
                                "Destination": "/run/kew-switchyard",
                                "Name": "kew-switchyard-000000000000-config",
                                "RW": False,
                            }
                        ],
                    }
                ]
            return subprocess.CompletedProcess(
                argv, 0, __import__("json").dumps(payload), ""
            )
        if argv[1:3] == ["volume", "create"]:
            stdout = "kew-switchyard-000000000000-config\n"
        elif argv[1:3] == ["volume", "inspect"]:
            payload = [
                {
                    "Name": "kew-switchyard-000000000000-config",
                    "Driver": "local",
                    "Scope": "local",
                    "Labels": {"io.kew.switchyard.attempt": owner},
                }
            ]
            stdout = __import__("json").dumps(payload)
        elif argv[1] == "create" and any(
            value.endswith("config-helper") for value in argv
        ):
            stdout = helper_id + "\n"
        elif argv[1] == "create" and "kew-switchyard-000000000000-worker" in argv:
            stdout = worker_id + "\n"
        elif argv[1:3] == ["network", "create"]:
            stdout = network_id + "\n"
        elif argv[1] == "create":
            stdout = proxy_id + "\n"
        elif argv[1:3] == ["container", "ls"]:
            stdout = "1" * 64 + "\n"
        elif argv[1:3] == ["network", "ls"]:
            stdout = "2" * 64 + "\n"
        elif argv[1:3] == ["volume", "ls"]:
            stdout = "unrelated-volume\n"
        elif argv[1] == "exec" and argv[-1] == "--version":
            stdout = "switchyard-server 0.1.0\n"
        elif argv[1] == "exec" and str(argv[-1]).endswith("/v1/models"):
            stdout = '{"data":[{"id":"switchyard/passthrough"}]}\n'
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "kew.switchyard_supervisor.inspect_image_id",
        lambda runtime, image: ImageResolution(
            ImageIdStatus.RESOLVED,
            image_id=proxy_image,
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-upstream")
    monkeypatch.setattr("kew.switchyard_supervisor.subprocess_utils.run", run)
    monkeypatch.setattr(
        "kew.switchyard_supervisor.subprocess_utils.run_bytes",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            (f"{digest}  /private/routes.toml\n65532:65532:600\n").encode(),
            b"",
        ),
    )

    with SidecarAttemptSupervisor(
        settings,
        runtime="docker",
        worker_image_id=worker_image,
        owner_factory=lambda: owner,
    ) as supervisor:
        evidence = supervisor.evidence
        assert evidence.network_id == network_id
        assert evidence.proxy_id == proxy_id
        assert evidence.endpoint == "http://172.30.0.2:4000"
        environment = supervisor.worker_environment("OPENAI_API_KEY")
        worker_environment[0] = f"OPENAI_API_KEY={environment['OPENAI_API_KEY']}"
        assert (
            supervisor.create_worker(
                ["docker", "create", "--name", supervisor.worker_name]
            )
            == worker_id
        )

    evidence = supervisor.evidence
    assert evidence.worker_id == worker_id
    assert evidence.cleanup_verified is True
    removals = [argv for argv in commands if argv[1:3] == ["container", "rm"]]
    assert ["docker", "container", "rm", "--force", helper_id] in removals
    assert ["docker", "container", "rm", "--force", proxy_id] in removals
    assert ["docker", "container", "rm", "--force", worker_id] in removals
    assert ["docker", "network", "rm", network_id] in commands
    proxy_create = next(
        argv
        for argv in commands
        if argv[1] == "create" and "kew-switchyard-000000000000-proxy" in argv
    )
    assert "OPENAI_API_KEY" in proxy_create
    assert "synthetic-upstream" not in proxy_create


def _attachment_supervisor() -> SidecarAttemptSupervisor:
    settings = SwitchyardConfig(
        route="switchyard/passthrough",
        expected_version="0.1.0",
        sidecar={
            "enabled": True,
            "image": "switchyard:pinned",
            "egress_network": "approved-egress",
        },
    )
    supervisor = SidecarAttemptSupervisor(
        settings,
        runtime="docker",
        worker_image_id="sha256:" + "b" * 64,
        owner_factory=lambda: "0" * 32,
    )
    supervisor.network_id = "a" * 64
    supervisor.egress_network_id = "b" * 64
    return supervisor


@pytest.mark.parametrize(
    ("attachment_id", "accepted"),
    [("a" * 64, True), (None, False), ("c" * 64, False)],
)
def test_worker_attachment_requires_the_captured_network_id(
    attachment_id: str | None,
    accepted: bool,
) -> None:
    supervisor = _attachment_supervisor()
    supervisor.worker_id = "c" * 64
    supervisor.endpoint = "http://172.30.0.2:4000"
    environment = supervisor.worker_environment("ANTHROPIC_API_KEY")
    attachment = {} if attachment_id is None else {"NetworkID": attachment_id}
    worker = {
        "Id": supervisor.worker_id,
        "Image": supervisor.worker_image_id,
        "Config": {
            "Entrypoint": ["/bin/sleep"],
            "Cmd": ["infinity"],
            "Env": [f"ANTHROPIC_API_KEY={environment['ANTHROPIC_API_KEY']}"],
            "Labels": {"io.kew.switchyard.attempt": supervisor.owner},
        },
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {supervisor.network_name: attachment}},
        "HostConfig": {
            "CapAdd": None,
            "Privileged": False,
            "ReadonlyRootfs": True,
            "PidMode": "",
        },
        "Mounts": [],
    }

    if not accepted:
        with pytest.raises(SwitchyardSupervisorError, match="worker topology"):
            supervisor._verify_worker_topology(worker)
        return
    supervisor._verify_worker_topology(worker)


def test_proxy_attachments_accept_both_captured_network_ids() -> None:
    supervisor = _attachment_supervisor()

    address = supervisor._proxy_internal_address(
        {
            supervisor.network_name: {
                "NetworkID": supervisor.network_id,
                "IPAddress": "172.30.0.2",
            },
            "approved-egress": {
                "NetworkID": supervisor.egress_network_id,
                "IPAddress": "172.31.0.2",
            },
        }
    )

    assert address == "172.30.0.2"


@pytest.mark.parametrize("network_name", ["internal", "egress"])
@pytest.mark.parametrize("attachment_id", [None, "c" * 64])
def test_proxy_attachment_rejects_missing_or_mismatched_network_id(
    network_name: str,
    attachment_id: str | None,
) -> None:
    supervisor = _attachment_supervisor()
    networks = {
        supervisor.network_name: {
            "NetworkID": supervisor.network_id,
            "IPAddress": "172.30.0.2",
        },
        "approved-egress": {
            "NetworkID": supervisor.egress_network_id,
            "IPAddress": "172.31.0.2",
        },
    }
    selected_name = (
        supervisor.network_name if network_name == "internal" else "approved-egress"
    )
    attachment = networks[selected_name]
    if attachment_id is None:
        attachment.pop("NetworkID")
    else:
        attachment["NetworkID"] = attachment_id

    with pytest.raises(SwitchyardSupervisorError, match="proxy topology"):
        supervisor._proxy_internal_address(networks)


def test_config_storage_helper_transfers_exact_bytes_with_minimum_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "0" * 32
    worker_image = "sha256:" + "b" * 64
    helper_id = "d" * 64
    contents = b'# synthetic config\nroute = "offline"\n'
    digest = hashlib.sha256(contents).hexdigest()
    commands: list[list[str]] = []
    delivered: list[bytes | None] = []
    settings = SwitchyardConfig(route="offline", expected_version="0.1.0")
    supervisor = SidecarAttemptSupervisor(
        settings,
        runtime="docker",
        worker_image_id=worker_image,
        owner_factory=lambda: owner,
    )
    supervisor.config_sha256 = digest

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(argv)
        if argv[1:3] == ["volume", "create"]:
            stdout = supervisor.config_storage + "\n"
        elif argv[1:3] == ["volume", "inspect"]:
            stdout = __import__("json").dumps(
                [
                    {
                        "Name": supervisor.config_storage,
                        "Driver": "local",
                        "Scope": "local",
                        "Labels": {"io.kew.switchyard.attempt": owner},
                    }
                ]
            )
        elif argv[1] == "create":
            stdout = helper_id + "\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def run_bytes(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        stdin = kwargs.get("stdin")
        assert isinstance(stdin, bytes)
        delivered.append(stdin)
        stdout = (f"{digest}  /private/routes.toml\n65532:65532:600\n").encode()
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr("kew.switchyard_supervisor.subprocess_utils.run", run)
    monkeypatch.setattr(
        "kew.switchyard_supervisor.subprocess_utils.run_bytes", run_bytes
    )

    supervisor._create_config_storage(contents)
    supervisor.close()

    helper_create = next(argv for argv in commands if argv[1] == "create")
    added_capabilities = {
        helper_create[index + 1]
        for index, value in enumerate(helper_create)
        if value == "--cap-add"
    }
    assert helper_create[helper_create.index("--cap-drop") + 1] == "ALL"
    assert added_capabilities == {"CHOWN", "SETGID", "SETUID"}
    assert "--interactive" in helper_create
    helper_script = helper_create[-1]
    assert "test -s /private/routes.toml" in helper_script
    assert (
        "setpriv --reuid=65532 --regid=65532 --clear-groups "
        "sha256sum /private/routes.toml"
    ) in helper_script
    assert "stat --format='%u:%g:%a' /private/routes.toml" in helper_script
    assert delivered == [contents]


def test_rejected_config_storage_is_not_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "0" * 32
    worker_image = "sha256:" + "b" * 64
    settings = SwitchyardConfig(route="offline", expected_version="0.1.0")
    supervisor = SidecarAttemptSupervisor(
        settings,
        runtime="docker",
        worker_image_id=worker_image,
        owner_factory=lambda: owner,
    )
    commands: list[list[str]] = []
    remaining = [supervisor.config_storage]

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(argv)
        if argv[1:3] == ["volume", "create"]:
            stdout = supervisor.config_storage + "\n"
        elif argv[1:3] == ["volume", "inspect"]:
            stdout = __import__("json").dumps(
                [
                    {
                        "Name": supervisor.config_storage,
                        "Driver": "local",
                        "Scope": "local",
                        "Labels": {"io.kew.switchyard.attempt": "different-owner"},
                    }
                ]
            )
        elif argv[1:3] == ["volume", "rm"]:
            remaining.clear()
            stdout = supervisor.config_storage + "\n"
        elif argv[1:3] == ["volume", "ls"]:
            stdout = "\n".join(remaining)
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("kew.switchyard_supervisor.subprocess_utils.run", run)

    with pytest.raises(SwitchyardSupervisorError, match="storage identity mismatch"):
        supervisor._create_config_storage(b"synthetic bytes")
    with pytest.raises(SwitchyardSupervisorError, match="absence proof failed"):
        supervisor.close()

    assert not any(argv[1:3] == ["volume", "rm"] for argv in commands)


def test_sidecar_rejects_undeclared_proxy_or_worker_credentials() -> None:
    settings = SwitchyardConfig(
        route="switchyard/passthrough",
        expected_version="0.1.0",
        sidecar={
            "enabled": True,
            "image": "switchyard:pinned",
            "egress_network": "approved-egress",
        },
    )
    supervisor = SidecarAttemptSupervisor(
        settings,
        runtime="docker",
        worker_image_id="sha256:" + "b" * 64,
        owner_factory=lambda: "0" * 32,
    )
    supervisor.credential_env_names = ("OPENAI_API_KEY",)

    with pytest.raises(SwitchyardSupervisorError, match="proxy credential"):
        supervisor._verify_proxy_environment(
            {
                "Env": [
                    "OPENAI_API_KEY=synthetic-upstream",
                    "ANTHROPIC_API_KEY=undeclared",
                ]
            }
        )

    supervisor.endpoint = "http://172.30.0.2:4000"
    worker_environment = supervisor.worker_environment("ANTHROPIC_API_KEY")
    with pytest.raises(SwitchyardSupervisorError, match="worker credential"):
        supervisor._verify_worker_environment(
            {
                "Env": [
                    f"ANTHROPIC_API_KEY={worker_environment['ANTHROPIC_API_KEY']}",
                    "OPENAI_API_KEY=reusable-upstream",
                ]
            }
        )


@pytest.mark.parametrize(
    ("control", "worker", "passed", "reason"),
    [
        (PacketProbeStatus.REACHABLE, PacketProbeStatus.DENIED, True, None),
        (
            PacketProbeStatus.UNAVAILABLE,
            PacketProbeStatus.DENIED,
            False,
            "positive_control_unavailable",
        ),
        (
            PacketProbeStatus.DENIED,
            PacketProbeStatus.DENIED,
            False,
            "positive_control_failed",
        ),
        (
            PacketProbeStatus.REACHABLE,
            PacketProbeStatus.UNAVAILABLE,
            False,
            "worker_probe_unavailable",
        ),
        (
            PacketProbeStatus.REACHABLE,
            PacketProbeStatus.REACHABLE,
            False,
            "packet_leak",
        ),
    ],
)
def test_topology_denial_requires_a_reachable_control_and_observed_denial(
    control: PacketProbeStatus,
    worker: PacketProbeStatus,
    passed: bool,
    reason: str | None,
) -> None:
    result = evaluate_topology_denial(
        name="synthetic-listener", positive_control=control, worker=worker
    )

    assert result.passed is passed
    assert result.reason == reason


@pytest.mark.parametrize(
    "worker",
    [None, False, 1, "reachable", "unavailable", "bogus"],
)
def test_topology_denial_rejects_malformed_worker_observations(
    worker: object,
) -> None:
    with pytest.raises(ValueError, match="worker"):
        evaluate_topology_denial(
            name="synthetic-domain-control",
            positive_control=PacketProbeStatus.REACHABLE,
            worker=worker,  # type: ignore[arg-type]
        )


def test_topology_denial_rejects_malformed_control_observations() -> None:
    with pytest.raises(ValueError, match="positive_control"):
        evaluate_topology_denial(
            name="synthetic-domain-control",
            positive_control="reachable",  # type: ignore[arg-type]
            worker=PacketProbeStatus.DENIED,
        )


def test_managed_switchyard_derives_credentials_across_classifier_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        tmp_path,
        credential_env=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    )
    assert settings.config_path is not None
    settings.config_path.write_text(
        "schema_version = 1\n"
        "[llm_clients.openai]\n"
        'format = "openai_responses"\n'
        'base_url = "https://api.openai.example/v1"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        "[llm_clients.anthropic]\n"
        'format = "anthropic_messages"\n'
        'base_url = "https://api.anthropic.example"\n'
        'api_key_env = "ANTHROPIC_API_KEY"\n'
        "[targets.weak]\n"
        'id = "weak"\n'
        'llm_client = "openai"\n'
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "anthropic"\n'
        "[routes.classified]\n"
        'id = "exact-route"\n'
        'type = "llm_classifier"\n'
        'classifier_target = "strong"\n'
        'weak_target = "weak"\n'
        'strong_target = "strong"\n'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    launches, _ = _install_process_fakes(monkeypatch)

    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe) as managed:
        assert managed.credential_env_names == (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        )
        assert launches[0][2]["env"] == {
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
        }


def _oauth_route(client_fields: str, *, extra_clients: str = "") -> bytes:
    return (
        "schema_version = 1\n"
        "[llm_clients.anthropic]\n"
        f"{client_fields}\n"
        f"{extra_clients}"
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "anthropic"\n'
        "[routes.passthrough]\n"
        'id = "exact-route"\n'
        'target = "strong"\n'
    ).encode()


def test_oauth_anthropic_route_uses_only_canonical_private_header() -> None:
    contents = _oauth_route(
        'format = "anthropic_messages"\n'
        "max_retries = 0\n"
        'extra_headers = { authorization = "Bearer synthetic-oauth" }'
    )

    assert _route_credential_names(contents, "exact-route") == ()


@pytest.mark.parametrize(
    "client_fields",
    [
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { Authorization = "Bearer value" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { authorization = "Bearer value", '
        'Authorization = "Bearer value" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { x-api-key = "value" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { proxy-authorization = "value" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'api_key_env = "ANTHROPIC_API_KEY"\n'
        'extra_headers = { authorization = "Bearer value" }',
        'format = "anthropic_messages"\nmax_retries = 1\n'
        'extra_headers = { authorization = "Bearer value" }',
        'format = "openai_responses"\nmax_retries = 0\n'
        'extra_headers = { authorization = "Bearer value" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { authorization = "Bearer two values" }',
        'format = "anthropic_messages"\nmax_retries = 0\n'
        'extra_headers = { authorization = "Bearer value\u200b" }',
    ],
)
def test_oauth_route_rejects_mixed_or_ambiguous_authentication(
    client_fields: str,
) -> None:
    with pytest.raises(ValueError, match="credential inspection failed"):
        _route_credential_names(_oauth_route(client_fields), "exact-route")


def test_route_rejects_unreachable_credential_bearing_client() -> None:
    contents = _oauth_route(
        'format = "anthropic_messages"\n'
        "max_retries = 0\n"
        'extra_headers = { authorization = "Bearer synthetic-oauth" }',
        extra_clients=(
            "[llm_clients.unreachable]\n"
            'format = "openai_responses"\n'
            'api_key_env = "OPENAI_API_KEY"\n'
        ),
    )

    with pytest.raises(ValueError, match="credential inspection failed"):
        _route_credential_names(contents, "exact-route")


def test_private_config_rejects_symlinked_parent(tmp_path: Path) -> None:
    from kew.switchyard_supervisor import _read_config_once

    private = tmp_path / "private"
    private.mkdir()
    config = private / "routes.toml"
    config.write_bytes(b"schema_version = 1\n")
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)

    with pytest.raises(ValueError, match="components"):
        _read_config_once(linked / "routes.toml")


def test_sidecar_private_config_requires_owned_private_metadata(
    tmp_path: Path,
) -> None:
    from kew.switchyard_supervisor import _read_config_once

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = private / "routes.toml"
    config.write_bytes(b"schema_version = 1\n")

    with pytest.raises(ValueError, match="metadata"):
        _read_config_once(config, require_private_storage=True)

    config.chmod(0o600)
    assert _read_config_once(config, require_private_storage=True)

    private.chmod(0o755)
    with pytest.raises(ValueError, match="metadata"):
        _read_config_once(config, require_private_storage=True)


def test_classifier_stderr_is_reduced_to_sanitized_evidence(
    tmp_path: Path,
) -> None:
    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path),
        classifier_ingress_format="openai_responses",
    )
    managed.classifier_upstream_format = "openai_chat"

    managed._observe_classifier_line(  # noqa: SLF001 - exercise the log boundary
        'classifier_signals={"confidence":0.8,"selected_tier":"strong",'
        '"error":"private classifier response"}'
    )

    evidence = managed.classifier_evidence
    assert len(evidence) == 1
    assert evidence[0].verdict == "classifier_unavailable_fallback_strong"
    assert evidence[0].upstream_format == "openai_chat"
    assert evidence[0].fail_closed_reason == "classifier_unavailable"
    assert "private classifier response" not in repr(managed.__dict__)


def test_classifier_stderr_is_drained_before_managed_close_returns(
    tmp_path: Path,
) -> None:
    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path),
        classifier_ingress_format="openai_responses",
    )
    managed.classifier_upstream_format = "openai_chat"
    process = _FakeProcess()
    process.stderr = io.StringIO(
        'classifier_signals={"confidence":0.8,"selected_tier":"strong"}\n'
    )
    managed._process = process  # noqa: SLF001 - exercise managed pipe shutdown

    managed._start_drainers(process)  # noqa: SLF001 - exercise pipe shutdown
    with patch("kew.switchyard_supervisor._terminate_process_group"):
        managed.close()

    assert len(managed.classifier_evidence) == 1
    assert managed.classifier_evidence[0].verdict == "selected_strong"


def test_managed_accounting_uses_bracketed_snapshots_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_process_fakes(monkeypatch)
    baseline = parse_switchyard_metrics(
        'switchyard_requests_total{route="exact-route"} 1\n'
    )
    final = parse_switchyard_metrics(
        "\n".join(
            (
                'switchyard_requests_total{route="exact-route"} 2',
                "switchyard_model_selections_total{"
                'model="provider/model",route="exact-route"} 1',
                'switchyard_prompt_tokens_total{model="provider/model"} 12',
                'switchyard_completion_tokens_total{model="provider/model"} 3',
            )
        )
        + "\n"
    )
    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path), endpoint_probe=_ready_probe
    )

    with patch(
        "kew.switchyard_supervisor.observe_switchyard_metrics",
        side_effect=(baseline, final),
    ) as observe:
        with managed:
            pass
        evidence = managed.accounting_evidence(
            runner_input_tokens=12,
            runner_output_tokens=3,
        )

    assert observe.call_count == 2
    assert evidence.observation.scope == "managed_run_delta"
    assert evidence.observation.per_run_attribution is True
    assert evidence.selected_model == "provider/model"


@pytest.mark.parametrize(
    ("declared", "route_name"),
    [
        ((), "OPENAI_API_KEY"),
        (("OPENROUTER_API_KEY",), "OPENAI_API_KEY"),
        (("OPENAI_API_KEY", "OPENROUTER_API_KEY"), "OPENAI_API_KEY"),
    ],
)
def test_managed_switchyard_rejects_mismatched_credential_identity_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: tuple[str, ...],
    route_name: str,
) -> None:
    settings = _settings(
        tmp_path,
        credential_env=declared,
        route_credential_env=route_name,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    launches, _ = _install_process_fakes(monkeypatch)

    with pytest.raises(SwitchyardSupervisorError, match="credential declaration"):
        ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe).start()

    assert launches == []


@pytest.mark.parametrize("value", [None, "", "   "])
def test_managed_switchyard_rejects_missing_or_empty_credential_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    settings = _settings(
        tmp_path,
        credential_env=("OPENAI_API_KEY",),
        route_credential_env="OPENAI_API_KEY",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    launches, _ = _install_process_fakes(monkeypatch)

    with pytest.raises(SwitchyardSupervisorError, match="credential unavailable"):
        ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe).start()

    assert launches == []


@pytest.mark.parametrize("boundary", ["parse", "traverse"])
def test_route_credential_inspection_sanitizes_recursion_failures(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from kew import switchyard_supervisor

    if boundary == "parse":
        monkeypatch.setattr(
            switchyard_supervisor.tomllib,
            "loads",
            lambda _text: (_ for _ in ()).throw(RecursionError("secret detail")),
        )
    else:
        monkeypatch.setattr(
            switchyard_supervisor,
            "_collect_target_names",
            lambda *_args: (_ for _ in ()).throw(RecursionError("secret detail")),
        )

    with pytest.raises(
        ValueError, match="^managed route credential inspection failed$"
    ) as exc_info:
        _route_credential_names(
            b"schema_version = 1\n"
            b"[routes.route]\nid = 'exact-route'\n"
            b"[targets.target]\nllm_client = 'client'\n"
            b"[llm_clients.client]\nbase_url = 'https://example.test'\n",
            "exact-route",
        )
    assert exc_info.value.__suppress_context__ is True
    assert exc_info.value.__cause__ is None


def test_each_concurrent_run_owns_a_distinct_process_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches, _ = _install_process_fakes(monkeypatch)
    first = ManagedSwitchyardSupervisor(
        _settings(tmp_path / "first", endpoint="http://127.0.0.1:4401"),
        endpoint_probe=_ready_probe,
    )
    second = ManagedSwitchyardSupervisor(
        _settings(tmp_path / "second", endpoint="http://127.0.0.1:4402"),
        endpoint_probe=_ready_probe,
    )

    with first, second:
        snapshots = [Path(call[1][1]) for call in launches]
        assert first.pid != second.pid
        assert len(set(snapshots)) == 2
        assert all(path.exists() for path in snapshots)

    assert all(not path.exists() for path in snapshots)


def test_cancellation_releases_process_group_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches, signals = _install_process_fakes(monkeypatch)
    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path), endpoint_probe=_ready_probe
    )

    with pytest.raises(asyncio.CancelledError), managed:
        snapshot = Path(launches[0][1][1])
        raise asyncio.CancelledError

    assert signals == [(managed.pid, signal.SIGTERM)]
    assert not snapshot.exists()


def test_substituted_executable_is_rejected_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    launches, _ = _install_process_fakes(monkeypatch)
    assert settings.binary is not None
    settings.binary.write_bytes(b"attacker-substituted executable")
    settings.binary.chmod(0o700)

    with (
        pytest.raises(SwitchyardSupervisorError, match="executable validation"),
        ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe),
    ):
        pass

    assert launches == []


def test_pinned_launcher_rejects_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "server"
    binary.write_bytes(b"first executable")
    binary.chmod(0o700)
    pinned = pin_executable(binary, hashlib.sha256(binary.read_bytes()).hexdigest())
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"second executable")
    replacement.chmod(0o700)
    replacement.replace(binary)

    with (
        patch("kew.subprocess_utils.subprocess.Popen") as raw_popen,
        pytest.raises(ValueError, match="digest mismatch"),
    ):
        popen_pinned(pinned, ["--version"])

    raw_popen.assert_not_called()


def test_path_lookup_cannot_substitute_the_configured_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    attacker_dir = tmp_path / "attacker-bin"
    attacker_dir.mkdir()
    (attacker_dir / "switchyard-server").write_bytes(b"attacker")
    monkeypatch.setenv("PATH", str(attacker_dir))
    launches, _ = _install_process_fakes(monkeypatch)

    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe):
        pass

    assert launches[0][0].path == settings.binary
    assert launches[0][2]["env"] == {}


def test_startup_deadline_failure_terminates_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches, signals = _install_process_fakes(monkeypatch)
    now = [0.0]

    def unavailable_probe(
        *, endpoint: str, route: str, timeout_seconds: float
    ) -> SwitchyardProbeResult:
        return SwitchyardProbeResult(False, "unhealthy", endpoint, route)

    def advance(seconds: float) -> None:
        now[0] += seconds

    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path, startup_timeout_seconds=1),
        endpoint_probe=unavailable_probe,
        monotonic=lambda: now[0],
        sleep=advance,
    )

    with pytest.raises(SwitchyardSupervisorError, match="startup unavailable"), managed:
        pass

    snapshot = Path(launches[0][1][1])
    assert signals == [(managed.pid, signal.SIGTERM)]
    assert not snapshot.exists()
    assert now[0] >= 1.0


def test_cancellation_during_startup_still_terminates_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches, signals = _install_process_fakes(monkeypatch)

    def cancelled_probe(
        *, endpoint: str, route: str, timeout_seconds: float
    ) -> SwitchyardProbeResult:
        raise asyncio.CancelledError

    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path), endpoint_probe=cancelled_probe
    )

    with pytest.raises(asyncio.CancelledError):
        managed.start()

    snapshot = Path(launches[0][1][1])
    assert signals == [(managed.pid, signal.SIGTERM)]
    assert not snapshot.exists()


def test_released_supervisor_cannot_be_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_process_fakes(monkeypatch)
    managed = ManagedSwitchyardSupervisor(
        _settings(tmp_path), endpoint_probe=_ready_probe
    )
    with managed:
        pass

    with pytest.raises(SwitchyardSupervisorError, match="one run"):
        managed.start()


class _EndpointTransport:
    def get_json(self, url: str, timeout_seconds: float) -> ProbeResponse:
        if url.endswith("/health"):
            return ProbeResponse(200, {"status": "healthy"})
        return ProbeResponse(200, {"data": [{"id": "exact-route"}]})


def test_endpoint_probe_checks_health_and_route_without_local_reprobe() -> None:
    result = probe_switchyard_endpoint(
        endpoint="http://127.0.0.1:4400",
        route="exact-route",
        transport=_EndpointTransport(),
        timeout_seconds=0.25,
    )

    assert result.admitted is True
    assert result.reason == "ready"


class _Runner:
    name = "fake"

    def build_command(self, *, prompt, config, env, model=""):
        return ["echo", prompt]

    def parse_line(self, line):
        return []

    def classify_result(self, *, exit_code, last_output, config):
        return ResultClassification(category="permanent")

    def supports_resume(self):
        return False

    def build_resume_command(self, **kwargs):
        raise NotImplementedError

    def enrich_result(self, result, config):
        return None


def _resolved(settings: SwitchyardConfig) -> ResolvedInferenceTransport:
    return ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="original",
        model=settings.route,
        config_sha256="a" * 64,
        endpoint=settings.endpoint,
        route=settings.route,
        managed=True,
        expected_version=settings.expected_version,
    )


def test_orchestrator_owns_managed_switchyard_until_agent_finishes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config = DispatchConfig(
        logging={"dir": str(tmp_path / "logs")},
        inference_transport={"mode": "switchyard", "switchyard": settings},
    )
    events: list[str] = []

    class FakeSupervisor:
        def __init__(
            self,
            received: SwitchyardConfig,
            *,
            classifier_ingress_format: str | None = None,
        ) -> None:
            assert received == settings
            assert classifier_ingress_format is None

        def __enter__(self):
            events.append("supervisor_enter")
            return self

        def __exit__(self, *args):
            events.append("supervisor_exit")

        def is_healthy(self) -> bool:
            events.append("health")
            return True

    def fake_run_process(*args, **kwargs):
        events.append("agent")
        return None, "exact-route", 0

    with (
        patch("kew.orchestrator.ManagedSwitchyardSupervisor", FakeSupervisor),
        patch("kew.orchestrator._run_process", side_effect=fake_run_process),
    ):
        result = run_agent(
            runner=_Runner(),
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="exact-route",
            inference_transport=_resolved(settings),
        )

    assert result.exit_code == 0
    assert events == ["supervisor_enter", "agent", "health", "supervisor_exit"]


@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("mode", ["none", "inherit"])
@pytest.mark.parametrize("storage", ["bind", "volume"])
def test_sidecar_builder_applies_proxy_policy(
    tmp_path, monkeypatch, runtime, mode, storage
):
    from kew.sandbox import CONTAINER_PROXY_NAMES, CONTROLLED_PROXY_NAMES

    # All ten controlled names: the six inheritable ones follow the policy,
    # the four always-blank ones are assigned empty in every mode.
    names = CONTROLLED_PROXY_NAMES
    secret = "http://user:s3cret@proxy.invalid:3128"
    for name in names:
        monkeypatch.setenv(name, secret)
    source = (
        {"config_path": tmp_path / "routes.toml"}
        if storage == "bind"
        else {"config_storage": "private-config"}
    )
    policy = {} if mode == "none" else {"proxy_environment": mode}
    argv = build_switchyard_sidecar_argv(
        runtime=runtime,
        image_id="sha256:" + "a" * 64,
        container_name="attempt-proxy",
        internal_network="attempt-network",
        owner="b" * 32,
        **source,
        **policy,
    )
    actual = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    assert actual == [
        name if mode == "inherit" and name in CONTAINER_PROXY_NAMES else f"{name}="
        for name in names
    ]
    assert secret not in " ".join(argv)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_sidecar_supervisor_applies_proxy_policy_to_config_helper_and_proxy(
    monkeypatch, runtime, mode
):
    from kew.sandbox import CONTAINER_PROXY_NAMES, CONTROLLED_PROXY_NAMES

    # All ten controlled names: the six inheritable ones follow the policy,
    # the four always-blank ones are assigned empty in every mode.
    names = CONTROLLED_PROXY_NAMES
    secret = "http://user:s3cret@proxy.invalid:3128"
    for name in names:
        monkeypatch.setenv(name, secret)
    settings = SwitchyardConfig(
        sidecar={
            "enabled": True,
            "image": "switchyard:pinned",
            "egress_network": "egress",
        }
    )
    policy = {} if mode == "none" else {"proxy_environment": mode}
    supervisor = SidecarAttemptSupervisor(
        settings, runtime=runtime, worker_image_id="sha256:" + "a" * 64, **policy
    )
    supervisor.proxy_image_id = "sha256:" + "b" * 64
    commands = []

    def command(argv, label):
        commands.append(argv)
        stdout = (
            supervisor.config_storage if argv[1:3] == ["volume", "create"] else "c" * 64
        )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(supervisor, "_command", command)
    monkeypatch.setattr(supervisor, "_inspect_volume", lambda: None)
    monkeypatch.setattr(supervisor, "_remove_live_container", lambda *args: None)
    monkeypatch.setattr(
        supervisor,
        "_bytes_command",
        lambda *args: subprocess.CompletedProcess(
            [], 0, b"digest  /private/routes.toml\n65532:65532:600\n", b""
        ),
    )
    supervisor.config_sha256 = "digest"
    supervisor._create_config_storage(b"synthetic config")
    supervisor._create_proxy(())
    launches = [argv for argv in commands if argv[1] == "create"]
    assert len(launches) == 2
    for argv in launches:
        actual = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
        assert actual == [
            name if mode == "inherit" and name in CONTAINER_PROXY_NAMES else f"{name}="
            for name in names
        ]
        assert secret not in " ".join(argv)
