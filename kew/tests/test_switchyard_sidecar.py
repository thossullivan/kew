"""Real SC-0 packet and namespace proofs for the candidate sidecar topology."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from kew.audit import (
    DEFAULT_ANCHORS_PATH,
    Actor,
    append_sidecar_packet_certification,
    matching_sidecar_packet_evidence,
)
from kew.config import SandboxConfig, SwitchyardSidecarConfig
from kew.dispatch_sandbox import (
    WorkerNetnsFilterEvidence,
    start_agent_after_worker_netns_filter,
)
from kew.ledger_identity import (
    LedgerIdentityError,
    RegistryRecord,
    canonical_project_root,
    open_managed_store,
    registry_paths,
)
from kew.readiness import (
    SIDECAR_PACKET_DENIALS,
    SidecarPacketCertification,
    SidecarPacketEvidence,
)
from kew.run_store import SqliteRunStore
from kew.sandbox import (
    ALLOWLIST_NETWORK,
    SWITCHYARD_ATTEMPT_LABEL,
    WorkerNetnsFilterError,
    build_container_cleanup_argv,
    build_network_attachment_argv,
    build_network_cleanup_argv,
    build_per_run_network_argv,
    build_resource_inspect_argv,
)
from kew.switchyard_supervisor import (
    PacketProbeStatus,
    TopologyProbeResult,
    build_switchyard_sidecar_argv,
    evaluate_topology_denial,
)

_RUNTIME = "docker"
_PROXY_PORT = 4000
_PEER_PORT = 4101
_EXTERNAL_PORT = 4102
_UDP_PORT = 4103
_DNS_PORT = 4104
_EXTRA_PROXY_PORT = 4105
_GATEWAY_PORT = 4106
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_PROXY_REVISION = "6ff491acd7eb1ec239525a1baa015730c3d3564a"
_PROBE_TIMEOUT_SECONDS = 3
_AGENT_MARKER = "/tmp/kew-agent-started"

_IDLE_SCRIPT = "setInterval(() => {}, 1000000)"
_AGENT_START_SCRIPT = (
    'require("fs").writeFileSync("/tmp/kew-agent-started", "started\\n")'
)
_TCP_SERVER_SCRIPT = r"""
const net = require("net");
const port = Number(process.argv[1]);
const host = process.argv[2] || "0.0.0.0";
net.createServer((stream) => stream.end("SC0\n")).listen(port, host);
setInterval(() => {}, 1000000);
"""
_UPSTREAM_SERVER_SCRIPT = r"""
const http = require("http");
const port = Number(process.argv[1]);
http.createServer((request, response) => {
  request.resume();
  request.on("end", () => {
    const payload = JSON.stringify({
      id: "resp_sc0",
      object: "response",
      created_at: 0,
      status: "completed",
      model: "synthetic/model",
      output: [],
      usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}
    });
    response.writeHead(200, {"content-type": "application/json"});
    response.end(payload);
  });
}).listen(port, "0.0.0.0");
setInterval(() => {}, 1000000);
"""
_TCP_PROBE_SCRIPT = r"""
const net = require("net");
let finished = false;
const finish = (code) => {
  if (finished) return;
  finished = true;
  socket.destroy();
  process.exit(code);
};
const socket = net.connect({host: process.argv[1], port: Number(process.argv[2])});
socket.once("connect", () => finish(0));
socket.once("error", () => finish(7));
setTimeout(() => finish(7), 1500);
"""
_UDP_SERVER_SCRIPT = r"""
const dgram = require("dgram");
const server = dgram.createSocket("udp4");
server.on("message", (message, remote) => {
  server.send(Buffer.from("SC0-UDP"), remote.port, remote.address);
});
server.bind(Number(process.argv[1]), "0.0.0.0");
setInterval(() => {}, 1000000);
"""
_UDP_PROBE_SCRIPT = r"""
const dgram = require("dgram");
const client = dgram.createSocket("udp4");
let finished = false;
const finish = (code) => {
  if (finished) return;
  finished = true;
  client.close();
  process.exit(code);
};
client.once("message", () => finish(0));
client.once("error", (error) => {
  const denied = new Set([
    "EACCES", "ECONNREFUSED", "EHOSTUNREACH", "ENETUNREACH", "EPERM"
  ]);
  finish(denied.has(error.code) ? 7 : 8);
});
client.send(Buffer.from("SC0-UDP"), Number(process.argv[2]), process.argv[1]);
setTimeout(() => finish(7), 1500);
"""
_DNS_SERVER_SCRIPT = r"""
const dgram = require("dgram");
const server = dgram.createSocket("udp4");
server.on("message", (query, remote) => {
  if (query.length < 12) return;
  let end = 12;
  while (end < query.length && query[end] !== 0) end += query[end] + 1;
  if (end + 5 > query.length) return;
  const question = query.subarray(12, end + 5);
  const header = Buffer.from(query.subarray(0, 12));
  header[2] = 0x81;
  header[3] = 0x80;
  header.writeUInt16BE(1, 6);
  const answer = Buffer.from([0xc0, 0x0c, 0x00, 0x01, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x04, 192, 0, 2, 10]);
  const response = Buffer.concat([header, question, answer]);
  server.send(response, remote.port, remote.address);
});
server.bind(Number(process.argv[1]), "0.0.0.0");
setInterval(() => {}, 1000000);
"""
_DNS_PROBE_SCRIPT = r"""
const dns = require("dns");
const resolver = new dns.promises.Resolver();
resolver.setServers([process.argv[1] + ":" + process.argv[2]]);
const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error()), 1500));
Promise.race([resolver.resolve4("sc0.invalid"), timeout])
  .then((addresses) => process.exit(addresses[0] === "192.0.2.10" ? 0 : 8))
  .catch(() => process.exit(7));
"""


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _must_run(argv: list[str], *, purpose: str) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(
            f"SC-0 {purpose} unavailable: {type(exc).__name__}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()[-500:]
        raise AssertionError(f"SC-0 {purpose} failed: {detail}")
    return result


def _require_worker_not_running(worker_id: str) -> None:
    """A filter readback failure terminates the worker (round 28 of #1951);
    the agent marker probe cannot run inside a stopped container, so prove
    the stop: the worker is gone or its state is not running."""
    result = _run(
        build_resource_inspect_argv(
            runtime=_RUNTIME, resource_kind="container", resource_id=worker_id
        )
    )
    if result.returncode != 0:
        return
    payload = json.loads(result.stdout)
    assert payload and payload[0]["State"]["Running"] is False, (
        "SC-0 worker still running after filter readback failure"
    )


def _require_agent_marker_absent(worker_id: str) -> None:
    _must_run(
        [
            _RUNTIME,
            "exec",
            worker_id,
            "test",
            "!",
            "-e",
            _AGENT_MARKER,
        ],
        purpose="agent startup marker absence",
    )


def _inspect(kind: str, identity: str) -> dict[str, object]:
    result = _must_run(
        build_resource_inspect_argv(
            runtime=_RUNTIME,
            resource_kind=kind,
            resource_id=identity,
        ),
        purpose=f"{kind} inspection",
    )
    try:
        payload = json.loads(result.stdout)
        inspected = payload[0]
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"SC-0 {kind} inspection was malformed") from exc
    if not isinstance(inspected, dict):
        raise AssertionError(f"SC-0 {kind} inspection was not an object")
    return inspected


def _resolved_image_id(reference: str, *, role: str) -> tuple[str, dict[str, object]]:
    result = _must_run(
        [_RUNTIME, "image", "inspect", reference], purpose=f"{role} image resolution"
    )
    try:
        payload = json.loads(result.stdout)
        image = payload[0]
        image_id = image["Id"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"SC-0 {role} image identity was malformed") from exc
    if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
        raise AssertionError(f"SC-0 {role} image did not resolve to an immutable ID")
    if not isinstance(image, dict):
        raise AssertionError(f"SC-0 {role} image inspection was not an object")
    return image_id, image


def _authoritative_resource_ids(kind: str) -> set[str] | None:
    argv = [_RUNTIME, kind, "ls", "--quiet", "--no-trunc"]
    if kind == "container":
        argv.insert(3, "--all")
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    identities = set(result.stdout.splitlines())
    if any(_CONTAINER_ID.fullmatch(identity) is None for identity in identities):
        return None
    return identities


class _HostTcpListener:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", 0))
        self._socket.listen()
        self._socket.settimeout(0.1)
        self.port = int(self._socket.getsockname()[1])
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                stream, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with stream:
                stream.sendall(b"SC0-HOST\n")

    def close(self) -> None:
        self._stopped.set()
        self._socket.close()
        self._thread.join(timeout=2)


@dataclass(frozen=True)
class _TopologyEvidence:
    owner: str
    proxy_image_id: str
    probe_image_id: str
    internal_network_id: str
    external_network_id: str
    egress_network_name: str
    proxy: dict[str, object]
    worker: dict[str, object]
    worker_id: str
    proxy_internal_ip: str
    internal_network: dict[str, object]
    filter: WorkerNetnsFilterEvidence
    topology_before_filter: tuple[object, ...]
    topology_after_filter: tuple[object, ...]
    proxy_health: PacketProbeStatus
    proxy_route: PacketProbeStatus
    outcomes: dict[str, TopologyProbeResult]

    @property
    def packet_evidence(self) -> SidecarPacketEvidence:
        """Convert the actual SC-0 observations into the production contract."""
        return SidecarPacketEvidence(
            proxy_health=self.proxy_health,
            proxy_route=self.proxy_route,
            denials=tuple(self.outcomes[name] for name in SIDECAR_PACKET_DENIALS),
        )


class _TopologyAttempt:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.owner = uuid.uuid4().hex
        self.prefix = f"kew-sc0-{self.owner[:12]}"
        self.internal_network_name = f"{self.prefix}-internal"
        self.external_network_name = f"{self.prefix}-external"
        self.container_ids: list[str] = []
        self.network_ids: list[str] = []
        self.creation_started = False
        self.host_listener: _HostTcpListener | None = None
        self.proxy_image_id = ""
        self.probe_image_id = ""
        self.worker_id = ""
        self.egress_network = os.environ.get(
            "KEW_SC0_EGRESS_NETWORK", ALLOWLIST_NETWORK
        )
        self.synthetic_api_value = f"sc0-{uuid.uuid4().hex}"

    def _create_networks(self) -> tuple[str, str]:
        self.creation_started = True
        internal = _must_run(
            build_per_run_network_argv(
                runtime=_RUNTIME,
                network=self.internal_network_name,
                owner=self.owner,
            ),
            purpose="internal IPv6 network creation",
        ).stdout.strip()
        if _CONTAINER_ID.fullmatch(internal) is None:
            raise AssertionError("SC-0 internal network returned no exact identity")
        self.network_ids.append(internal)

        ipv6_subnet = (
            f"fc{self.owner[:2]}:{self.owner[2:6]}:"
            f"{self.owner[6:10]}:{self.owner[10:14]}::/64"
        )
        external = _must_run(
            [
                _RUNTIME,
                "network",
                "create",
                "--driver",
                "bridge",
                "--ipv6",
                "--subnet",
                ipv6_subnet,
                "--label",
                f"{SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
                self.external_network_name,
            ],
            purpose="external IPv6 control network creation",
        ).stdout.strip()
        if _CONTAINER_ID.fullmatch(external) is None:
            raise AssertionError("SC-0 external network returned no exact identity")
        self.network_ids.append(external)
        return internal, external

    def _create_node(
        self,
        role: str,
        network: str,
        script: str,
        *arguments: str,
        environment: tuple[str, ...] = (),
        publish_port: int | None = None,
    ) -> str:
        name = f"{self.prefix}-{role}"
        created = _must_run(
            [
                _RUNTIME,
                "create",
                "--name",
                name,
                "--label",
                f"{SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
                "--pull=never",
                "--network",
                network,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:size=1m,uid=1001,gid=1001,mode=0700",
                "--pids-limit",
                "32",
                "--memory",
                "64m",
                "--memory-swap",
                "64m",
                "--cpus",
                "0.25",
                "--entrypoint",
                "node",
                *(argument for value in environment for argument in ("--env", value)),
                *(("--publish", f"{publish_port}/tcp") if publish_port else ()),
                self.probe_image_id,
                "-e",
                script,
                *arguments,
            ],
            purpose=f"{role} creation",
        ).stdout.strip()
        if _CONTAINER_ID.fullmatch(created) is None:
            raise AssertionError(f"SC-0 {role} returned no exact identity")
        self.container_ids.append(created)
        _must_run([_RUNTIME, "start", created], purpose=f"{role} startup")
        return created

    def _start_agent_marker(self, worker_id: str) -> None:
        _must_run(
            [
                _RUNTIME,
                "exec",
                worker_id,
                "node",
                "-e",
                _AGENT_START_SCRIPT,
            ],
            purpose="agent startup marker",
        )

    def _install_filter_and_start_agent(
        self, worker_id: str, proxy_address: str
    ) -> WorkerNetnsFilterEvidence:
        from kew.subprocess_utils import TOOL_PATHS

        runtime_path = shutil.which(_RUNTIME)
        if runtime_path is None:
            raise AssertionError("SC-0 Docker executable was unavailable")
        previous = TOOL_PATHS.get(_RUNTIME)
        TOOL_PATHS[_RUNTIME] = runtime_path
        try:
            return start_agent_after_worker_netns_filter(
                runtime=_RUNTIME,
                image_id=self.probe_image_id,
                worker_id=worker_id,
                owner=self.owner,
                proxy_address=proxy_address,
                proxy_port=_PROXY_PORT,
                start_agent=lambda: self._start_agent_marker(worker_id),
            )
        finally:
            if previous is None:
                TOOL_PATHS.pop(_RUNTIME, None)
            else:
                TOOL_PATHS[_RUNTIME] = previous

    def _create_proxy(self, config: Path, internal_network: str) -> str:
        created = _must_run(
            build_switchyard_sidecar_argv(
                runtime=_RUNTIME,
                image_id=self.proxy_image_id,
                container_name=f"{self.prefix}-proxy",
                internal_network=internal_network,
                owner=self.owner,
                config_path=config,
            ),
            purpose="proxy creation",
        ).stdout.strip()
        if _CONTAINER_ID.fullmatch(created) is None:
            raise AssertionError("SC-0 proxy returned no exact identity")
        self.container_ids.append(created)
        _must_run(
            build_network_attachment_argv(
                runtime=_RUNTIME,
                network=self.egress_network,
                container_id=created,
            ),
            purpose="proxy egress attachment",
        )
        _must_run([_RUNTIME, "start", created], purpose="proxy startup")
        return created

    def _create_port_sentinel(self, proxy_id: str, bind_address: str) -> str:
        created = _must_run(
            [
                _RUNTIME,
                "create",
                "--name",
                f"{self.prefix}-port-sentinel",
                "--label",
                f"{SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
                "--pull=never",
                "--network",
                f"container:{proxy_id}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--entrypoint",
                "node",
                self.probe_image_id,
                "-e",
                _TCP_SERVER_SCRIPT,
                str(_EXTRA_PROXY_PORT),
                bind_address,
            ],
            purpose="extra proxy port sentinel creation",
        ).stdout.strip()
        if _CONTAINER_ID.fullmatch(created) is None:
            raise AssertionError("SC-0 port sentinel returned no exact identity")
        self.container_ids.append(created)
        _must_run([_RUNTIME, "start", created], purpose="extra proxy port startup")
        return created

    def _probe_node(
        self, container_id: str, script: str, *args: str
    ) -> PacketProbeStatus:
        try:
            result = _run(
                [_RUNTIME, "exec", container_id, "node", "-e", script, *args],
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return PacketProbeStatus.UNAVAILABLE
        if result.returncode == 0:
            return PacketProbeStatus.REACHABLE
        if result.returncode == 7:
            return PacketProbeStatus.DENIED
        return PacketProbeStatus.UNAVAILABLE

    def _wait_proxy(self, worker_id: str, proxy_address: str) -> PacketProbeStatus:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                result = _run(
                    [
                        _RUNTIME,
                        "exec",
                        worker_id,
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--connect-timeout",
                        "1",
                        "--max-time",
                        "2",
                        f"http://{proxy_address}:{_PROXY_PORT}/health",
                    ],
                    timeout=_PROBE_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError):
                return PacketProbeStatus.UNAVAILABLE
            if result.returncode == 0:
                return PacketProbeStatus.REACHABLE
            time.sleep(0.1)
        return PacketProbeStatus.DENIED

    def _probe_proxy_route(
        self, worker_id: str, proxy_address: str
    ) -> PacketProbeStatus:
        try:
            result = _run(
                [
                    _RUNTIME,
                    "exec",
                    worker_id,
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "1",
                    "--max-time",
                    "3",
                    "--header",
                    "content-type: application/json",
                    "--header",
                    f"authorization: Bearer {self.synthetic_api_value}",
                    "--data",
                    '{"model":"synthetic-no-credentials","input":"SC0"}',
                    f"http://{proxy_address}:{_PROXY_PORT}/v1/responses",
                ],
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return PacketProbeStatus.UNAVAILABLE
        return (
            PacketProbeStatus.REACHABLE
            if result.returncode == 0
            else PacketProbeStatus.DENIED
        )

    def _wait_control(
        self, container_id: str, script: str, host: str, port: int
    ) -> PacketProbeStatus:
        status = PacketProbeStatus.UNAVAILABLE
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self._probe_node(container_id, script, host, str(port))
            if status is PacketProbeStatus.REACHABLE:
                return status
            time.sleep(0.1)
        return status

    def _paired(
        self,
        name: str,
        control_id: str,
        worker_id: str,
        host: str,
        port: int,
        *,
        script: str = _TCP_PROBE_SCRIPT,
    ) -> TopologyProbeResult:
        control = self._wait_control(control_id, script, host, port)
        worker = self._probe_node(worker_id, script, host, str(port))
        return evaluate_topology_denial(
            name=name,
            positive_control=control,
            worker=worker,
        )

    def start(self) -> _TopologyEvidence:
        info = _must_run([_RUNTIME, "info"], purpose="Docker daemon access")
        if not info.stdout and not info.stderr:
            raise AssertionError("SC-0 Docker daemon returned no observation")

        probe_tag = SandboxConfig().image
        proxy_tag = f"kew-switchyard:{probe_tag.partition(':')[2]}"
        candidate = SwitchyardSidecarConfig(
            image=proxy_tag,
            egress_network=self.egress_network,
        )
        self.egress_network = candidate.egress_network
        egress = _inspect("network", self.egress_network)
        if egress.get("Internal") is not False:
            raise AssertionError("SC-0 approved egress network is internal")
        self.proxy_image_id, proxy_image = _resolved_image_id(
            candidate.image, role="proxy"
        )
        self.probe_image_id, _ = _resolved_image_id(probe_tag, role="probe")
        labels = proxy_image.get("Config", {})
        if not isinstance(labels, dict):
            raise AssertionError("SC-0 proxy image configuration was unavailable")
        label_values = labels.get("Labels", {})
        if (
            not isinstance(label_values, dict)
            or label_values.get("org.opencontainers.image.revision") != _PROXY_REVISION
        ):
            raise AssertionError("SC-0 proxy image revision did not match SC-IMG")

        internal_id, external_id = self._create_networks()
        self.host_listener = _HostTcpListener()

        worker = self._create_node(
            "worker",
            self.internal_network_name,
            _IDLE_SCRIPT,
            environment=(f"OPENAI_API_KEY={self.synthetic_api_value}",),
        )
        self.worker_id = worker
        egress_control = self._create_node(
            "egress-control", self.egress_network, _IDLE_SCRIPT
        )
        ipv6_control = self._create_node(
            "ipv6-control", self.external_network_name, _IDLE_SCRIPT
        )
        peer = self._create_node(
            "egress-peer",
            self.egress_network,
            _UPSTREAM_SERVER_SCRIPT,
            str(_PEER_PORT),
        )
        gateway_listener = self._create_node(
            "gateway-listener",
            self.egress_network,
            _TCP_SERVER_SCRIPT,
            str(_GATEWAY_PORT),
            publish_port=_GATEWAY_PORT,
        )
        external = self._create_node(
            "external",
            self.external_network_name,
            _TCP_SERVER_SCRIPT,
            str(_EXTERNAL_PORT),
        )
        udp = self._create_node(
            "udp-listener", self.egress_network, _UDP_SERVER_SCRIPT, str(_UDP_PORT)
        )
        dns = self._create_node(
            "dns-listener", self.egress_network, _DNS_SERVER_SCRIPT, str(_DNS_PORT)
        )
        ipv6 = self._create_node(
            "ipv6-listener",
            self.external_network_name,
            _TCP_SERVER_SCRIPT,
            str(_EXTERNAL_PORT),
            "::",
        )

        peer_ip = _container_address(
            _inspect("container", peer), self.egress_network, ipv6=False
        )
        gateway_host_port = _published_host_port(
            _inspect("container", gateway_listener), _GATEWAY_PORT
        )
        external_ip = _container_address(
            _inspect("container", external), self.external_network_name, ipv6=False
        )
        udp_ip = _container_address(
            _inspect("container", udp), self.egress_network, ipv6=False
        )
        dns_ip = _container_address(
            _inspect("container", dns), self.egress_network, ipv6=False
        )
        ipv6_ip = _container_address(
            _inspect("container", ipv6), self.external_network_name, ipv6=True
        )

        config = self.directory / "routes.toml"
        config.write_text(
            "schema_version = 1\n"
            "[llm_clients.synthetic]\n"
            'format = "openai_responses"\n'
            f'base_url = "http://{peer_ip}:{_PEER_PORT}/v1"\n'
            "[targets.synthetic]\n"
            'id = "synthetic/model"\n'
            'llm_client = "synthetic"\n'
            "[routes.synthetic]\n"
            'id = "synthetic-no-credentials"\n'
            'type = "passthrough"\n'
            'target = "synthetic"\n',
            encoding="utf-8",
        )
        config.chmod(0o444)
        proxy = self._create_proxy(config, self.internal_network_name)
        proxy_inspection = _inspect("container", proxy)
        proxy_internal_ip = _container_address(
            proxy_inspection, self.internal_network_name, ipv6=False
        )
        port_sentinel = self._create_port_sentinel(proxy, proxy_internal_ip)

        network = _inspect("network", internal_id)
        topology_before_filter = _topology_signature(
            _inspect("container", worker), proxy_inspection, network
        )
        filter_evidence = self._install_filter_and_start_agent(
            worker, proxy_internal_ip
        )
        topology_after_filter = _topology_signature(
            _inspect("container", worker),
            _inspect("container", proxy),
            _inspect("network", internal_id),
        )
        gateway = _network_gateway(network)
        proxy_health = self._wait_proxy(worker, proxy_internal_ip)
        proxy_route = (
            self._probe_proxy_route(worker, proxy_internal_ip)
            if proxy_health is PacketProbeStatus.REACHABLE
            else PacketProbeStatus.UNAVAILABLE
        )

        outcomes = {
            "egress_peer": self._paired(
                "egress_peer", egress_control, worker, peer_ip, _PEER_PORT
            ),
            "host_gateway": self._paired(
                "host_gateway",
                egress_control,
                worker,
                gateway,
                gateway_host_port,
            ),
            "macos_host": self._paired(
                "macos_host",
                egress_control,
                worker,
                "host.docker.internal",
                self.host_listener.port,
            ),
            "external": self._paired(
                "external", ipv6_control, worker, external_ip, _EXTERNAL_PORT
            ),
            "udp": self._paired(
                "udp",
                egress_control,
                worker,
                udp_ip,
                _UDP_PORT,
                script=_UDP_PROBE_SCRIPT,
            ),
            "dns": self._paired(
                "dns",
                egress_control,
                worker,
                dns_ip,
                _DNS_PORT,
                script=_DNS_PROBE_SCRIPT,
            ),
            "ipv6": self._paired("ipv6", ipv6_control, worker, ipv6_ip, _EXTERNAL_PORT),
            "extra_proxy_port": self._paired(
                "extra_proxy_port",
                port_sentinel,
                worker,
                proxy_internal_ip,
                _EXTRA_PROXY_PORT,
            ),
        }

        return _TopologyEvidence(
            owner=self.owner,
            proxy_image_id=self.proxy_image_id,
            probe_image_id=self.probe_image_id,
            internal_network_id=internal_id,
            external_network_id=external_id,
            egress_network_name=self.egress_network,
            proxy=proxy_inspection,
            worker=_inspect("container", worker),
            worker_id=worker,
            proxy_internal_ip=proxy_internal_ip,
            internal_network=network,
            filter=filter_evidence,
            topology_before_filter=topology_before_filter,
            topology_after_filter=topology_after_filter,
            proxy_health=proxy_health,
            proxy_route=proxy_route,
            outcomes=outcomes,
        )

    def _discover_owned(self, kind: str) -> tuple[list[str], str | None]:
        plural = "container" if kind == "container" else "network"
        argv = [
            _RUNTIME,
            plural,
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
        ]
        if kind == "container":
            argv.insert(3, "--all")
        try:
            listed = _run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], f"{kind} discovery {type(exc).__name__}"
        if listed.returncode != 0:
            return [], f"{kind} discovery"
        owned: list[str] = []
        for identity in listed.stdout.splitlines():
            if _CONTAINER_ID.fullmatch(identity) is None:
                return [], f"{kind} discovery identity"
            try:
                inspected = _run(
                    build_resource_inspect_argv(
                        runtime=_RUNTIME,
                        resource_kind=kind,
                        resource_id=identity,
                    )
                )
            except (OSError, subprocess.SubprocessError):
                return [], f"{kind} ownership inspection"
            if inspected.returncode != 0:
                return [], f"{kind} ownership inspection"
            try:
                item = json.loads(inspected.stdout)[0]
                labels = (
                    item["Config"]["Labels"] if kind == "container" else item["Labels"]
                )
                name = item["Name"].removeprefix("/")
            except (
                AttributeError,
                IndexError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ):
                return [], f"{kind} ownership inspection"
            if labels.get(
                SWITCHYARD_ATTEMPT_LABEL
            ) != self.owner or not name.startswith(self.prefix):
                return [], f"{kind} ownership mismatch"
            owned.append(identity)
        return owned, None

    def close(self) -> None:
        failures: list[str] = []
        container_ids = list(self.container_ids)
        network_ids = list(self.network_ids)
        if self.creation_started:
            discovered_containers, container_error = self._discover_owned("container")
            discovered_networks, network_error = self._discover_owned("network")
            if container_error is not None:
                failures.append(container_error)
            if network_error is not None:
                failures.append(network_error)
            container_ids.extend(
                identity
                for identity in discovered_containers
                if identity not in container_ids
            )
            network_ids.extend(
                identity
                for identity in discovered_networks
                if identity not in network_ids
            )
        if container_ids:
            try:
                result = _run(
                    build_container_cleanup_argv(
                        runtime=_RUNTIME,
                        container_ids=tuple(reversed(container_ids)),
                    )
                )
            except (OSError, subprocess.SubprocessError):
                failures.append("container cleanup")
            else:
                if result.returncode != 0:
                    failures.append("container cleanup")
            self.container_ids.clear()
        for network_id in reversed(network_ids):
            try:
                result = _run(
                    build_network_cleanup_argv(
                        runtime=_RUNTIME,
                        network_id=network_id,
                    )
                )
            except (OSError, subprocess.SubprocessError):
                failures.append(f"network cleanup {network_id}")
            else:
                if result.returncode != 0:
                    failures.append(f"network cleanup {network_id}")
        for kind, removed_ids in (
            ("container", container_ids),
            ("network", network_ids),
        ):
            if not removed_ids:
                continue
            remaining_ids = _authoritative_resource_ids(kind)
            if remaining_ids is None:
                failures.append(f"{kind} cleanup verification unavailable")
            elif remaining_ids.intersection(removed_ids):
                failures.append(f"{kind} cleanup verification retained exact ID")
        self.network_ids.clear()
        if self.host_listener is not None:
            self.host_listener.close()
            self.host_listener = None
        if failures:
            raise AssertionError("SC-0 cleanup failed: " + ", ".join(failures))


def _container_address(
    inspected: dict[str, object], network: str, *, ipv6: bool
) -> str:
    try:
        networks = inspected["NetworkSettings"]
        attachment = networks["Networks"][network]  # type: ignore[index]
        key = "GlobalIPv6Address" if ipv6 else "IPAddress"
        address = attachment[key]
    except (KeyError, TypeError) as exc:
        raise AssertionError(f"SC-0 {network} address was unavailable") from exc
    if not isinstance(address, str) or not address:
        raise AssertionError(f"SC-0 {network} address was empty")
    return address


def _topology_signature(
    worker: dict[str, object],
    proxy: dict[str, object],
    network: dict[str, object],
) -> tuple[object, ...]:
    worker_host = worker["HostConfig"]  # type: ignore[index]
    proxy_host = proxy["HostConfig"]  # type: ignore[index]
    worker_network = worker["NetworkSettings"]  # type: ignore[index]
    proxy_network = proxy["NetworkSettings"]  # type: ignore[index]
    return (
        worker["Id"],
        proxy["Id"],
        worker_host["NetworkMode"],
        proxy_host["NetworkMode"],
        worker_host["PidMode"],
        proxy_host["PidMode"],
        worker_network["SandboxID"],
        proxy_network["SandboxID"],
        json.dumps(worker_network["Networks"], sort_keys=True),
        json.dumps(proxy_network["Networks"], sort_keys=True),
        network["Id"],
        network["Name"],
        network["Internal"],
        network["EnableIPv6"],
    )


def _network_gateway(inspected: dict[str, object]) -> str:
    try:
        configurations = inspected["IPAM"]
        entries = configurations["Config"]  # type: ignore[index]
        gateway = next(entry["Gateway"] for entry in entries if "." in entry["Gateway"])
    except (KeyError, StopIteration, TypeError) as exc:
        raise AssertionError("SC-0 runtime gateway was unavailable") from exc
    if not isinstance(gateway, str) or not gateway:
        raise AssertionError("SC-0 runtime gateway was empty")
    return gateway


def _published_host_port(inspected: dict[str, object], container_port: int) -> int:
    try:
        bindings = inspected["NetworkSettings"]["Ports"][  # type: ignore[index]
            f"{container_port}/tcp"
        ]
        host_port = int(bindings[0]["HostPort"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise AssertionError("SC-0 runtime gateway port was unavailable") from exc
    if not 1 <= host_port <= 65535:
        raise AssertionError("SC-0 runtime gateway port was invalid")
    return host_port


def _assert_denied(evidence: _TopologyEvidence, name: str) -> None:
    outcome = evidence.outcomes[name]
    assert outcome.passed, (
        f"SC-0 {name} failed: control={outcome.positive_control.value}, "
        f"worker={outcome.worker.value}, reason={outcome.reason}"
    )


@pytest.fixture(scope="module")
def topology(tmp_path_factory: pytest.TempPathFactory) -> _TopologyEvidence:
    attempt = _TopologyAttempt(tmp_path_factory.mktemp("switchyard-sidecar"))
    evidence: _TopologyEvidence | None = None
    try:
        evidence = attempt.start()
    except BaseException as start_error:
        try:
            attempt.close()
        except BaseException as cleanup_error:
            raise ExceptionGroup(
                "SC-0 topology start and cleanup both failed",
                [start_error, cleanup_error],
            ) from None
        raise
    try:
        yield evidence
    finally:
        attempt.close()


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_internal_worker_reaches_only_proxy_listener(
    topology: _TopologyEvidence,
) -> None:
    assert topology.proxy_health is PacketProbeStatus.REACHABLE
    assert topology.proxy_route is PacketProbeStatus.REACHABLE
    for name in topology.outcomes:
        _assert_denied(topology, name)


def _packet_gate_store(directory: Path) -> SqliteRunStore:
    project = os.environ.get("KEW_SC2_PACKET_GATE_PROJECT")
    if project is None:
        return SqliteRunStore(str(directory / "packet-proof.db"))
    if not project or not Path(project).is_absolute():
        raise LedgerIdentityError("packet gate requires an absolute registered project")
    root = canonical_project_root(project)
    registry = RegistryRecord.from_json(registry_paths(root).registry.read_text())
    return open_managed_store(
        project_root=root,
        db_path=registry.db_path,
        anchors_path=root / DEFAULT_ANCHORS_PATH,
        require_registered=True,
    )


def _persist_packet_certification(
    certification: SidecarPacketCertification, directory: Path
) -> None:
    with _packet_gate_store(directory) as store:
        append_sidecar_packet_certification(
            store,
            certification,
            actor=Actor("policy", "sidecar-packet-gate"),
        )
        restored = matching_sidecar_packet_evidence(
            store,
            worker_image_id=certification.worker_image_id,
            proxy_image_id=certification.proxy_image_id,
        )
    assert restored == certification.packet
    assert restored.verdict == "passed"
    print(certification.to_json())


def _run_packet_gate(attempt: _TopologyAttempt, directory: Path) -> None:
    try:
        topology = attempt.start()
        certification = SidecarPacketCertification(
            schema_version=1,
            worker_image_id=topology.probe_image_id,
            proxy_image_id=topology.proxy_image_id,
            packet=topology.packet_evidence,
        )
        assert certification.packet.verdict == "passed", "packet proof is incomplete"
    finally:
        attempt.close()
    _persist_packet_certification(certification, directory)


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_packet_matrix_enters_the_durable_production_contract(tmp_path: Path) -> None:
    _run_packet_gate(_TopologyAttempt(tmp_path), tmp_path)


@pytest.mark.parametrize(
    "failure", ["start", "missing", "packet", "cleanup", "append", None]
)
def test_packet_gate_writes_only_after_complete_proof_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    gate = globals().get("_run_packet_gate")
    assert callable(gate), "the packet gate must own cleanup before publication"
    operations: list[str] = []
    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=(
            PacketProbeStatus.UNAVAILABLE
            if failure == "packet"
            else PacketProbeStatus.REACHABLE
        ),
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )

    def start():
        operations.append("start")
        if failure == "start":
            raise AssertionError("start unavailable")
        return SimpleNamespace(
            probe_image_id="sha256:" + "a" * 64,
            proxy_image_id="sha256:" + "b" * 64,
            packet_evidence=None if failure == "missing" else packet,
        )

    def close():
        operations.append("cleanup")
        if failure == "cleanup":
            raise AssertionError("cleanup unavailable")

    def publish_result(*args):
        operations.append("publish")
        if failure == "append":
            raise AssertionError("append unconfirmed")

    publish = Mock(side_effect=publish_result)
    monkeypatch.setitem(globals(), "_persist_packet_certification", publish)
    attempt = SimpleNamespace(start=start, close=close)
    if failure is None:
        gate(attempt, tmp_path)
        assert operations == ["start", "cleanup", "publish"]
        assert publish.call_args.args[0].packet == packet
    else:
        with pytest.raises((AssertionError, ValueError)):
            gate(attempt, tmp_path)
        if failure == "append":
            assert operations == ["start", "cleanup", "publish"]
            publish.assert_called_once()
        else:
            assert operations == ["start", "cleanup"]
            publish.assert_not_called()


def test_packet_gate_default_output_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEW_SC2_PACKET_GATE_PROJECT", raising=False)
    with _packet_gate_store(tmp_path) as store:
        assert store.read_ledger_id() is None
    assert (tmp_path / "packet-proof.db").is_file()
    assert not registry_paths(tmp_path).registry.exists()


def test_packet_gate_operator_output_requires_registered_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db = project / "dispatch.db"
    monkeypatch.setenv("KEW_SC2_PACKET_GATE_PROJECT", str(project))
    with pytest.raises(FileNotFoundError):
        _packet_gate_store(tmp_path)
    assert not db.exists()
    assert not (tmp_path / "packet-proof.db").exists()
    with open_managed_store(
        project_root=project, db_path=db, anchors_path=project / DEFAULT_ANCHORS_PATH
    ) as store:
        expected = store.read_ledger_id()
    with _packet_gate_store(tmp_path) as store:
        assert store.read_ledger_id() == expected
        assert store.append_audit_event(timestamp="test", event="packet-gate-test")
    assert not (tmp_path / "packet-proof.db").exists()


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_worker_netns_filter_blocks_host_gateway_and_peers(
    topology: _TopologyEvidence,
) -> None:
    for name in ("host_gateway", "egress_peer", "macos_host", "external"):
        _assert_denied(topology, name)


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_worker_netns_filter_allows_only_proxy(
    topology: _TopologyEvidence,
) -> None:
    assert topology.proxy_health is PacketProbeStatus.REACHABLE
    assert topology.proxy_route is PacketProbeStatus.REACHABLE
    _assert_denied(topology, "extra_proxy_port")
    _must_run(
        [_RUNTIME, "exec", topology.worker_id, "test", "-f", _AGENT_MARKER],
        purpose="post-filter agent startup marker readback",
    )
    assert topology.filter.worker_id == topology.worker_id
    assert topology.filter.image_id == topology.probe_image_id


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_netns_filter_preserves_sc0_topology_baseline(
    topology: _TopologyEvidence,
) -> None:
    assert topology.topology_after_filter == topology.topology_before_filter


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_filter_readback_failure_prevents_agent_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kew.dispatch_sandbox as dispatch_sandbox

    attempt = _TopologyAttempt(tmp_path)
    real_run = dispatch_sandbox._run

    def corrupt_filter_readback(
        argv: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        result = real_run(argv, *args, **kwargs)  # type: ignore[arg-type]
        if "nft" in argv and "--json" in argv and "table" in argv:
            return subprocess.CompletedProcess(argv, 0, '{"nftables": []}\n', "")
        return result

    monkeypatch.setattr(dispatch_sandbox, "_run", corrupt_filter_readback)
    try:
        with pytest.raises(WorkerNetnsFilterError, match="readback"):
            attempt.start()
        assert attempt.worker_id
        _require_worker_not_running(attempt.worker_id)
    finally:
        attempt.close()


@pytest.mark.parametrize("returncode", [125, 126, 137])
def test_agent_marker_absence_rejects_failed_probe(
    returncode: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def failed_marker_probe(
        argv: list[str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, "", "probe failed")

    monkeypatch.setitem(globals(), "_run", failed_marker_probe)

    with pytest.raises(AssertionError, match="marker absence"):
        _require_agent_marker_absent("a" * 64)
    assert calls == [
        [
            _RUNTIME,
            "exec",
            "a" * 64,
            "test",
            "!",
            "-e",
            _AGENT_MARKER,
        ]
    ]


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_proxy_image_starts_nonroot_with_final_hardening(
    topology: _TopologyEvidence,
) -> None:
    proxy = topology.proxy
    config = proxy["Config"]
    host = proxy["HostConfig"]
    state = proxy["State"]
    proxy_id = str(proxy["Id"])
    assert proxy["Image"] == topology.proxy_image_id
    assert config["User"] == "65532:65532"  # type: ignore[index]
    effective_uid = _must_run(
        [_RUNTIME, "exec", proxy_id, "id", "-u"],
        purpose="proxy effective identity inspection",
    ).stdout.strip()
    assert effective_uid == "65532"
    assert state["Running"] is True  # type: ignore[index]
    assert host["ReadonlyRootfs"] is True  # type: ignore[index]
    assert host["CapDrop"] == ["ALL"]  # type: ignore[index]
    assert any(
        str(option).startswith("no-new-privileges")
        for option in host["SecurityOpt"]  # type: ignore[index]
    )
    assert host["PidsLimit"] == 64  # type: ignore[index]
    assert host["Memory"] == 256 * 1024 * 1024  # type: ignore[index]
    assert host["MemorySwap"] == 256 * 1024 * 1024  # type: ignore[index]
    assert host["NanoCpus"] == 1_000_000_000  # type: ignore[index]
    assert "/run/kew-switchyard" in host["Tmpfs"]  # type: ignore[index]
    environment = config["Env"]  # type: ignore[index]
    assert not any("API_KEY=" in item for item in environment)


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_egress_peer_positive_control_then_worker_denial(
    topology: _TopologyEvidence,
) -> None:
    _assert_denied(topology, "egress_peer")


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_host_gateway_positive_control_then_worker_denial(
    topology: _TopologyEvidence,
) -> None:
    _assert_denied(topology, "host_gateway")


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_macos_host_positive_control_then_worker_denial(
    topology: _TopologyEvidence,
) -> None:
    _assert_denied(topology, "macos_host")


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_external_address_positive_control_then_worker_denial(
    topology: _TopologyEvidence,
) -> None:
    _assert_denied(topology, "external")


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_dns_udp_ipv6_and_extra_proxy_port_are_denied(
    topology: _TopologyEvidence,
) -> None:
    for name in ("dns", "udp", "ipv6", "extra_proxy_port"):
        _assert_denied(topology, name)


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_worker_and_proxy_share_no_pid_mount_or_socket(
    topology: _TopologyEvidence,
) -> None:
    worker_host = topology.worker["HostConfig"]
    proxy_host = topology.proxy["HostConfig"]
    worker_networks = topology.worker["NetworkSettings"]["Networks"]  # type: ignore[index]
    proxy_networks = topology.proxy["NetworkSettings"]["Networks"]  # type: ignore[index]
    assert topology.worker["Id"] != topology.proxy["Id"]
    assert topology.worker["Image"] == topology.probe_image_id
    assert topology.proxy["Image"] == topology.proxy_image_id
    assert topology.worker["Config"]["Labels"][SWITCHYARD_ATTEMPT_LABEL] == (  # type: ignore[index]
        topology.owner
    )
    assert topology.proxy["Config"]["Labels"][SWITCHYARD_ATTEMPT_LABEL] == (  # type: ignore[index]
        topology.owner
    )
    assert topology.internal_network["Id"] == topology.internal_network_id
    assert topology.internal_network["Internal"] is True
    assert topology.internal_network["EnableIPv6"] is True
    assert topology.internal_network["Labels"][SWITCHYARD_ATTEMPT_LABEL] == (  # type: ignore[index]
        topology.owner
    )
    assert worker_host["PidMode"] == ""  # type: ignore[index]
    assert proxy_host["PidMode"] == ""  # type: ignore[index]
    assert worker_host["NetworkMode"] == topology.internal_network["Name"]  # type: ignore[index]
    assert proxy_host["NetworkMode"] == topology.internal_network["Name"]  # type: ignore[index]
    assert topology.worker["State"]["Pid"] != topology.proxy["State"]["Pid"]  # type: ignore[index]
    assert (
        topology.worker["NetworkSettings"]["SandboxID"]
        != (  # type: ignore[index]
            topology.proxy["NetworkSettings"]["SandboxID"]  # type: ignore[index]
        )
    )
    assert set(worker_networks) == {topology.internal_network["Name"]}
    assert set(proxy_networks) == {
        topology.internal_network["Name"],
        topology.egress_network_name,
    }
    assert topology.worker["Mounts"] == []
    proxy_mounts = topology.proxy["Mounts"]
    bind_mounts = [mount for mount in proxy_mounts if mount["Type"] == "bind"]
    assert len(bind_mounts) == 1
    assert bind_mounts[0]["Destination"] == "/run/kew-switchyard/routes.toml"
    assert bind_mounts[0]["RW"] is False
    assert (
        not Path(str(bind_mounts[0]["Source"]))
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[1])
    )
    assert not any(
        mount["Destination"] in {"/work", "/var/run/docker.sock"}
        for mount in proxy_mounts
    )
    worker_environment = topology.worker["Config"]["Env"]  # type: ignore[index]
    assert any(item.startswith("OPENAI_API_KEY=sc0-") for item in worker_environment)
    worker_uid = _must_run(
        [_RUNTIME, "exec", topology.worker_id, "id", "-u"],
        purpose="worker effective identity inspection",
    ).stdout.strip()
    assert worker_uid == "1001"
    assert worker_host["ReadonlyRootfs"] is True  # type: ignore[index]
    assert worker_host["CapDrop"] == ["ALL"]  # type: ignore[index]
    assert worker_host["CapAdd"] is None  # type: ignore[index]
    assert any(
        str(option).startswith("no-new-privileges")
        for option in worker_host["SecurityOpt"]  # type: ignore[index]
    )


@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_failed_topology_start_removes_all_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _TopologyAttempt(tmp_path)
    create_node = attempt._create_node

    def fail_after_worker_start(
        role: str,
        network: str,
        script: str,
        *arguments: str,
        environment: tuple[str, ...] = (),
        publish_port: int | None = None,
    ) -> str:
        identity = create_node(
            role,
            network,
            script,
            *arguments,
            environment=environment,
            publish_port=publish_port,
        )
        if role == "worker":
            raise RuntimeError("injected partial-startup failure")
        return identity

    monkeypatch.setattr(attempt, "_create_node", fail_after_worker_start)
    try:
        with pytest.raises(RuntimeError, match="injected partial-startup failure"):
            attempt.start()
        assert attempt.container_ids
        assert attempt.network_ids
    finally:
        attempt.close()

    assert attempt.container_ids == []
    assert attempt.network_ids == []


def test_missing_packet_control_fails_instead_of_skipping() -> None:
    outcome = evaluate_topology_denial(
        name="missing-control",
        positive_control=PacketProbeStatus.UNAVAILABLE,
        worker=PacketProbeStatus.DENIED,
    )

    assert outcome.passed is False
    assert outcome.reason == "positive_control_unavailable"


def test_topology_targets_exact_worker_facing_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _TopologyAttempt(tmp_path)
    internal_network_id = "a" * 64
    external_network_id = "b" * 64
    proxy_image_id = "sha256:" + "c" * 64
    probe_image_id = "sha256:" + "d" * 64
    internal_gateway = "192.0.2.1"
    egress_gateway = "198.51.100.1"
    proxy_internal_ip = "192.0.2.20"
    proxy_egress_ip = "198.51.100.20"
    sentinel_calls: list[tuple[str, str]] = []
    paired_calls: dict[str, tuple[str, str, str, int]] = {}

    class _FakeHostListener:
        port = 42000

        def close(self) -> None:
            pass

    def fake_inspect(kind: str, identity: str) -> dict[str, object]:
        return {"Id": identity, "Internal": False}

    def fake_resolved_image_id(
        reference: str, *, role: str
    ) -> tuple[str, dict[str, object]]:
        del reference
        if role == "proxy":
            return proxy_image_id, {
                "Config": {
                    "Labels": {"org.opencontainers.image.revision": _PROXY_REVISION}
                }
            }
        return probe_image_id, {}

    def fake_container_address(
        inspected: dict[str, object], network: str, *, ipv6: bool
    ) -> str:
        del ipv6
        if inspected["Id"] == "proxy":
            if network == attempt.internal_network_name:
                return proxy_internal_ip
            return proxy_egress_ip
        return "203.0.113.10"

    def fake_network_gateway(inspected: dict[str, object]) -> str:
        if inspected["Id"] == attempt.egress_network:
            return egress_gateway
        return internal_gateway

    def fake_create_node(
        role: str,
        network: str,
        script: str,
        *arguments: str,
        environment: tuple[str, ...] = (),
        publish_port: int | None = None,
    ) -> str:
        del network, script, arguments, environment, publish_port
        return role

    def fake_create_port_sentinel(proxy_id: str, bind_address: str) -> str:
        sentinel_calls.append((proxy_id, bind_address))
        return "port-sentinel"

    def fake_paired(
        name: str,
        control_id: str,
        worker_id: str,
        host: str,
        port: int,
        *,
        script: str = _TCP_PROBE_SCRIPT,
        control_host: str | None = None,
    ) -> TopologyProbeResult:
        del worker_id, script
        paired_calls[name] = (control_id, control_host or host, host, port)
        return TopologyProbeResult(
            name=name,
            positive_control=PacketProbeStatus.REACHABLE,
            worker=PacketProbeStatus.DENIED,
            passed=True,
        )

    monkeypatch.setitem(globals(), "_HostTcpListener", _FakeHostListener)
    monkeypatch.setitem(globals(), "_inspect", fake_inspect)
    monkeypatch.setitem(globals(), "_resolved_image_id", fake_resolved_image_id)
    monkeypatch.setitem(globals(), "_container_address", fake_container_address)
    monkeypatch.setitem(globals(), "_network_gateway", fake_network_gateway)
    monkeypatch.setitem(globals(), "_topology_signature", lambda *args: ())
    monkeypatch.setitem(
        globals(), "_published_host_port", lambda inspected, port: 43000
    )
    monkeypatch.setitem(
        globals(),
        "_must_run",
        lambda argv, *, purpose: subprocess.CompletedProcess(argv, 0, "ok\n", ""),
    )
    monkeypatch.setattr(
        attempt,
        "_create_networks",
        lambda: (internal_network_id, external_network_id),
    )
    monkeypatch.setattr(attempt, "_create_node", fake_create_node)
    monkeypatch.setattr(attempt, "_create_proxy", lambda config, network: "proxy")
    monkeypatch.setattr(attempt, "_create_port_sentinel", fake_create_port_sentinel)
    monkeypatch.setattr(
        attempt,
        "_install_filter_and_start_agent",
        lambda worker_id, proxy_address: WorkerNetnsFilterEvidence(
            worker_id=worker_id,
            helper_id="e" * 64,
            image_id=probe_image_id,
            table=f"kew_sc0_{attempt.owner}",
            readback="{}",
        ),
    )
    monkeypatch.setattr(
        attempt,
        "_wait_proxy",
        lambda worker_id, proxy_address: PacketProbeStatus.REACHABLE,
    )
    monkeypatch.setattr(
        attempt,
        "_probe_proxy_route",
        lambda worker_id, proxy_address: PacketProbeStatus.REACHABLE,
    )
    monkeypatch.setattr(attempt, "_paired", fake_paired)

    attempt.start()

    assert sentinel_calls == [("proxy", proxy_internal_ip)]
    assert paired_calls["extra_proxy_port"] == (
        "port-sentinel",
        proxy_internal_ip,
        proxy_internal_ip,
        _EXTRA_PROXY_PORT,
    )
    assert paired_calls["host_gateway"] == (
        "egress-control",
        internal_gateway,
        internal_gateway,
        43000,
    )


def test_partial_start_cleanup_requires_authoritative_absence_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _TopologyAttempt(tmp_path)
    internal_network_id = "e" * 64
    creation_calls = 0

    def fail_during_network_creation(
        argv: list[str], *, purpose: str
    ) -> subprocess.CompletedProcess[str]:
        nonlocal creation_calls
        del purpose
        creation_calls += 1
        if creation_calls == 1:
            return subprocess.CompletedProcess(argv, 0, internal_network_id + "\n", "")
        raise AssertionError("injected partial-startup failure")

    def cleanup_without_readback(
        argv: list[str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if argv[1:3] == ["network", "rm"]:
            return subprocess.CompletedProcess(argv, 0, internal_network_id + "\n", "")
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "Cannot connect to the Docker daemon",
        )

    monkeypatch.setitem(globals(), "_must_run", fail_during_network_creation)
    monkeypatch.setitem(globals(), "_run", cleanup_without_readback)
    monkeypatch.setattr(attempt, "_discover_owned", lambda kind: ([], None))

    with pytest.raises(AssertionError, match="injected partial-startup failure"):
        attempt._create_networks()

    assert attempt.network_ids == [internal_network_id]
    with pytest.raises(AssertionError, match="cleanup verification"):
        attempt.close()


def test_partial_start_cleanup_proves_exact_ids_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _TopologyAttempt(tmp_path)
    removed_container_id = "f" * 64
    removed_network_id = "1" * 64
    unrelated_container_id = "2" * 64
    unrelated_network_id = "3" * 64
    attempt.creation_started = True
    attempt.container_ids.append(removed_container_id)
    attempt.network_ids.append(removed_network_id)
    calls: list[list[str]] = []

    def successful_cleanup_and_readback(
        argv: list[str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(argv)
        stdout = ""
        if argv[1:3] == ["container", "ls"]:
            stdout = unrelated_container_id + "\n"
        elif argv[1:3] == ["network", "ls"]:
            stdout = unrelated_network_id + "\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setitem(globals(), "_run", successful_cleanup_and_readback)
    monkeypatch.setattr(attempt, "_discover_owned", lambda kind: ([], None))

    attempt.close()

    assert [call[1:3] for call in calls] == [
        ["container", "rm"],
        ["network", "rm"],
        ["container", "ls"],
        ["network", "ls"],
    ]
