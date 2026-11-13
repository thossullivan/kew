"""Per-run lifecycle supervision for an operator-installed Switchyard server."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import stat
import subprocess
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast
from urllib.parse import urlsplit

from kew import subprocess_utils, work_dirs
from kew.config import SwitchyardConfig
from kew.readiness import (
    PacketProbeStatus as PacketProbeStatus,
)
from kew.readiness import (
    SidecarPacketEvidence,
    SidecarTopologyEvidence,
)
from kew.readiness import (
    TopologyProbeResult as TopologyProbeResult,
)
from kew.readiness import (
    evaluate_topology_denial as evaluate_topology_denial,
)
from kew.sandbox import (
    SWITCHYARD_ATTEMPT_LABEL,
    _container_proxy_environment_args,
    build_container_cleanup_argv,
    build_network_attachment_argv,
    build_network_cleanup_argv,
    build_per_run_network_argv,
    build_resource_inspect_argv,
    inspect_image_id,
    proxy_redaction_scope,
)
from kew.subprocess_utils import (
    PinnedExecutable,
    pin_executable,
    popen_pinned,
    run_pinned,
)
from kew.switchyard import (
    SwitchyardProbeResult,
    parse_switchyard_version,
    probe_switchyard_endpoint,
)
from kew.switchyard_classifier import (
    ClassifierEvidence,
    parse_classifier_evidence_line,
)
from kew.switchyard_metrics import (
    ProxyMetricsObservation,
    SwitchyardMetricsError,
    SwitchyardSelectionEvidence,
    managed_metrics_delta,
    observe_switchyard_metrics,
)

_MAX_CONFIG_BYTES = 1024 * 1024
_PROBE_INTERVAL_SECONDS = 0.05
_PROBE_TIMEOUT_SECONDS = 1.0
_TERMINATION_GRACE_SECONDS = 5.0
_MAX_CLASSIFIER_EVIDENCE = 1_000
_SIDECAR_CONFIG_PATH = "/run/kew-switchyard/routes.toml"
_SIDECAR_IMAGE_ID = "sha256:"
_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_SIDECAR_COMMAND_TIMEOUT_SECONDS = 30
_SIDECAR_PROXY_PORT = 4000
_SIDECAR_PROXY_UID = 65532
_PROVIDER_CREDENTIAL_NAMES = frozenset(
    {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"}
)
_FOLDED_PROVIDER_CREDENTIAL_NAMES = frozenset(
    name.casefold() for name in _PROVIDER_CREDENTIAL_NAMES
)
_logger = logging.getLogger(__name__)


def _record_sidecar_readiness(reason: str) -> None:
    """Retain one fixed retry reason without exposing a command response."""
    _logger.debug("sidecar readiness pending: %s", reason)


class SwitchyardSupervisorError(RuntimeError):
    """A sanitized refusal from managed-process validation or lifecycle."""


def _sidecar_resource(value: str, field: str) -> str:
    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be one exact Docker resource identity")
    return value


def _sidecar_config_source(config_path: Path) -> Path:
    if not config_path.is_absolute():
        raise ValueError("sidecar config_path must be absolute")
    if "," in str(config_path) or any(
        ord(character) < 32 for character in str(config_path)
    ):
        raise ValueError("sidecar config_path cannot contain mount delimiters")
    return config_path


def build_switchyard_sidecar_argv(
    *,
    runtime: str,
    image_id: str,
    container_name: str,
    internal_network: str,
    owner: str,
    config_path: Path | None = None,
    config_storage: str | None = None,
    credential_env_names: tuple[str, ...] = (),
    proxy_environment: str = "none",
) -> list[str]:
    """Build the hardened candidate sidecar creation command.

    This builder does not start or attach a production route.
    """
    if not image_id.startswith(_SIDECAR_IMAGE_ID):
        raise ValueError("sidecar image must be an immutable sha256 image ID")
    digest = image_id.removeprefix(_SIDECAR_IMAGE_ID)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("sidecar image must be an immutable sha256 image ID")
    if (config_path is None) == (config_storage is None):
        raise ValueError("sidecar requires exactly one private config source")
    name = _sidecar_resource(container_name, "container_name")
    network = _sidecar_resource(internal_network, "internal_network")
    ownership = _sidecar_resource(owner, "owner")
    if config_storage is not None:
        storage = _sidecar_resource(config_storage, "config_storage")
        mount = f"type=volume,src={storage},dst=/run/kew-switchyard,readonly"
        private_runtime_storage: list[str] = []
    else:
        assert config_path is not None
        source = _sidecar_config_source(config_path)
        mount = f"type=bind,src={source},dst={_SIDECAR_CONFIG_PATH},readonly"
        private_runtime_storage = [
            "--tmpfs",
            "/run/kew-switchyard:rw,nosuid,nodev,noexec,size=1048576,mode=1777",
        ]
    env_args = [
        argument
        for name in credential_env_names
        for argument in ("--env", _sidecar_resource(name, "credential_env_name"))
    ]
    return [
        runtime,
        "create",
        "--name",
        name,
        "--label",
        f"{SWITCHYARD_ATTEMPT_LABEL}={ownership}",
        "--env",
        "OTEL_SDK_DISABLED=true",
        *env_args,
        *_container_proxy_environment_args(proxy_environment),
        "--pull=never",
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        *private_runtime_storage,
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--user",
        f"{_SIDECAR_PROXY_UID}:{_SIDECAR_PROXY_UID}",
        "--mount",
        mount,
        image_id,
        "--config",
        _SIDECAR_CONFIG_PATH,
        "--host",
        "0.0.0.0",  # nosec B104 - bind only the sidecar's isolated interfaces
        "--port",
        "4000",
    ]


class EndpointProbe(Protocol):
    """Injected readiness boundary used by deterministic lifecycle tests."""

    def __call__(
        self, *, endpoint: str, route: str, timeout_seconds: float
    ) -> SwitchyardProbeResult: ...


def _open_config_no_follow(path: Path) -> tuple[int, os.stat_result]:
    """Open every path component through an anchored no-follow descriptor."""
    components = path.parts[1:]
    if any(component in {".", ".."} for component in components):
        raise ValueError("Config path components failed validation")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
    file_flags = os.O_RDONLY | no_follow
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        parent_metadata = os.fstat(directory_fd)
        final_component = components[-1] if components else "."
        return os.open(
            final_component, file_flags, dir_fd=directory_fd
        ), parent_metadata
    except OSError:
        raise ValueError("Config path components failed validation") from None
    finally:
        os.close(directory_fd)


def _private_config_metadata_matches(
    parent: os.stat_result, config: os.stat_result
) -> bool:
    return bool(
        parent.st_uid == os.getuid()
        and stat.S_IMODE(parent.st_mode) == 0o700
        and config.st_uid == os.getuid()
        and stat.S_IMODE(config.st_mode) == 0o600
        and config.st_nlink == 1
    )


def _read_config_once(path: Path, *, require_private_storage: bool = False) -> bytes:
    """Read bounded immutable bytes through one no-follow file descriptor."""
    if not path.is_absolute():
        raise ValueError("Config path must be absolute")
    try:
        fd, parent_metadata = _open_config_no_follow(path)
    except OSError:
        raise ValueError("Config path components failed validation") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Config must be a regular file")
        if require_private_storage and not _private_config_metadata_matches(
            parent_metadata, before
        ):
            raise ValueError("Private config metadata failed validation")
        with os.fdopen(fd, "rb", closefd=False) as config_file:
            contents = config_file.read(_MAX_CONFIG_BYTES + 1)
        if len(contents) > _MAX_CONFIG_BYTES:
            raise ValueError("Config exceeds the managed size limit")
        after = os.fstat(fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise ValueError("Config changed while it was read")
        return contents
    finally:
        os.close(fd)


def _write_private_snapshot(contents: bytes) -> tuple[Path, Path]:
    directory = work_dirs.make_work_dir("kew-switchyard-")
    snapshot = directory / "routes.toml"
    try:
        fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as snapshot_file:
            snapshot_file.write(contents)
            snapshot_file.flush()
            os.fsync(snapshot_file.fileno())
        directory_metadata = directory.stat()
        snapshot_metadata = snapshot.stat()
        if (
            stat.S_IMODE(directory_metadata.st_mode) != 0o700
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(snapshot_metadata.st_mode) != 0o600
            or snapshot_metadata.st_uid != os.getuid()
            or snapshot_metadata.st_nlink != 1
            or snapshot.read_bytes() != contents
        ):
            raise ValueError("Private config snapshot verification failed")
    except BaseException:
        with contextlib.suppress(OSError):
            snapshot.unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()
        raise
    return directory, snapshot


def _route_tables(
    contents: bytes,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        document = tomllib.loads(contents.decode("utf-8"))
        routes = document["routes"]
        targets = document["targets"]
        clients = document["llm_clients"]
        if not all(isinstance(table, dict) for table in (routes, targets, clients)):
            raise TypeError
    except (KeyError, RecursionError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("managed route credential inspection failed") from None
    return routes, targets, clients


def _collect_target_names(value: object, names: set[str], key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _collect_target_names(child_value, names, str(child_key))
        return
    if isinstance(value, list):
        _collect_target_list(value, names, key)
        return
    if key == "target" or key.endswith("_target"):
        _collect_target_value(value, names)


def _collect_target_list(value: list[object], names: set[str], key: str) -> None:
    if key != "targets":
        return
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("managed route credential inspection failed")
        names.add(item)


def _collect_target_value(value: object, names: set[str]) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("managed route credential inspection failed")
    names.add(value)


def _target_client_name(
    target_name: str, targets: dict[str, object], clients: dict[str, object]
) -> str:
    try:
        target = targets[target_name]
        if not isinstance(target, dict):
            raise TypeError
        client_name = target["llm_client"]
        if not isinstance(client_name, str) or not client_name:
            raise TypeError
        client = clients[client_name]
        if not isinstance(client, dict):
            raise TypeError
        return client_name
    except (KeyError, TypeError):
        raise ValueError("managed route credential inspection failed") from None


def _validate_client_authentication_fields(client: object) -> dict[str, object]:
    if not isinstance(client, dict):
        raise ValueError("managed route credential inspection failed")
    authentication_fields = {"api_key_env", "extra_headers"}
    if any(
        not isinstance(name, str)
        or name.casefold() in authentication_fields
        and name not in authentication_fields
        for name in client
    ):
        raise ValueError("managed route credential inspection failed")
    return client


def _validate_client_base_url(client: dict[str, object]) -> None:
    base_url = client.get("base_url")
    if not isinstance(base_url, str):
        return
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise ValueError("managed route credential inspection failed") from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("managed route credential inspection failed")


def _client_headers(client: dict[str, object]) -> dict[str, object]:
    headers = client.get("extra_headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(name, str) for name in headers
    ):
        raise ValueError("managed route credential inspection failed")
    folded_names = {name.casefold() for name in headers}
    if folded_names.intersection({"x-api-key", "proxy-authorization"}):
        raise ValueError("managed route credential inspection failed")
    return headers


def _client_api_key_name(client: dict[str, object]) -> str | None:
    api_key_name = client.get("api_key_env")
    if api_key_name is None:
        return None
    if not isinstance(api_key_name, str) or not api_key_name.strip():
        raise ValueError("managed route credential inspection failed")
    return api_key_name


def _validate_oauth_client(
    client: dict[str, object],
    headers: dict[str, object],
    authorization_names: list[str],
) -> None:
    contract_matches = (
        authorization_names == ["authorization"],
        client.get("api_key_env") is None,
        client.get("format") == "anthropic_messages",
        not isinstance(client.get("max_retries"), bool),
        client.get("max_retries") == 0,
    )
    if not all(contract_matches):
        raise ValueError("managed route credential inspection failed")
    authorization = headers["authorization"]
    if not isinstance(authorization, str):
        raise ValueError("managed route credential inspection failed")
    if not authorization.startswith("Bearer "):
        raise ValueError("managed route credential inspection failed")
    oauth_value = authorization.removeprefix("Bearer ")
    if not oauth_value or any(
        character.isspace() or not character.isprintable() for character in oauth_value
    ):
        raise ValueError("managed route credential inspection failed")


def _client_credential_name(client: object) -> str | None:
    """Validate one reachable client and return its environment credential."""
    validated = _validate_client_authentication_fields(client)
    _validate_client_base_url(validated)
    headers = _client_headers(validated)
    authorization_names = [
        name for name in headers if name.casefold() == "authorization"
    ]
    api_key_name = _client_api_key_name(validated)
    if not authorization_names:
        return api_key_name
    _validate_oauth_client(validated, headers, authorization_names)
    return None


def _url_bears_credentials(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None


def _client_bears_credentials(client: object) -> bool:
    if not isinstance(client, dict):
        return False
    fields = {
        name.casefold(): value
        for name, value in client.items()
        if isinstance(name, str)
    }
    headers = fields.get("extra_headers")
    header_bears_credentials = "extra_headers" in fields and (
        not isinstance(headers, dict) or bool(headers)
    )
    return bool(
        "api_key_env" in fields
        or header_bears_credentials
        or _url_bears_credentials(client.get("base_url"))
    )


def _route_by_id(routes: dict[str, object], route_id: str) -> dict[str, object]:
    matches = [
        route
        for route in routes.values()
        if isinstance(route, dict) and route.get("id") == route_id
    ]
    if len(matches) != 1:
        raise ValueError("managed route credential inspection failed")
    return matches[0]


def _reachable_route_credentials(
    route: dict[str, object],
    targets: dict[str, object],
    clients: dict[str, object],
) -> tuple[set[str], set[str]]:
    target_names: set[str] = set()
    try:
        _collect_target_names(route, target_names)
    except RecursionError:
        raise ValueError("managed route credential inspection failed") from None
    required: set[str] = set()
    reachable_clients: set[str] = set()
    for target_name in target_names:
        client_name = _target_client_name(target_name, targets, clients)
        reachable_clients.add(client_name)
        credential_name = _client_credential_name(clients[client_name])
        if credential_name is not None:
            required.add(credential_name)
    return required, reachable_clients


def _reject_unreachable_credentials(
    clients: dict[str, object], reachable_clients: set[str]
) -> None:
    if any(
        name not in reachable_clients and _client_bears_credentials(client)
        for name, client in clients.items()
    ):
        raise ValueError("managed route credential inspection failed")


def _route_credential_names(contents: bytes, route_id: str) -> tuple[str, ...]:
    """Return credential variable names used by the exact configured route."""
    routes, targets, clients = _route_tables(contents)
    route = _route_by_id(routes, route_id)
    required, reachable_clients = _reachable_route_credentials(route, targets, clients)
    _reject_unreachable_credentials(clients, reachable_clients)
    return tuple(sorted(required))


def _route_classifier_upstream_format(contents: bytes, route_id: str) -> str | None:
    """Return the classifier target's declared client format, if configured."""
    routes, targets, clients = _route_tables(contents)
    matches = [
        route
        for route in routes.values()
        if isinstance(route, dict) and route.get("id") == route_id
    ]
    if len(matches) != 1:
        return None
    classifier_target = matches[0].get("classifier_target")
    if not isinstance(classifier_target, str):
        return None
    target = targets.get(classifier_target)
    if not isinstance(target, dict):
        return None
    client_name = target.get("llm_client")
    if not isinstance(client_name, str):
        return None
    client = clients.get(client_name)
    if not isinstance(client, dict):
        return None
    client_format = client.get("format")
    return client_format if isinstance(client_format, str) else None


def _endpoint_binding(endpoint: str) -> tuple[str, int]:
    """Return the exact configured literal-loopback host and port."""
    parts = urlsplit(endpoint)
    host = parts.hostname
    if host is None or parts.scheme not in {"http", "https"}:
        raise ValueError("Managed endpoint is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Managed endpoint must use a literal loopback IP") from exc
    if not address.is_loopback:
        raise ValueError("Managed endpoint must use a literal loopback IP")
    default_port = 443 if parts.scheme == "https" else 80
    return host, parts.port or default_port


def _drain(pipe: IO[str], on_line: Callable[[str], None] | None = None) -> None:
    try:
        for line in pipe:
            if on_line is not None:
                on_line(line)
    except (OSError, ValueError):
        pass


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Terminate, escalate, and reap the entire run-owned process group."""
    if proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            proc.terminate()
            pgid = None
        try:
            proc.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                with contextlib.suppress(OSError):
                    os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
    else:
        proc.wait()


class ManagedSwitchyardSupervisor:
    """Own exactly one validated Switchyard process for exactly one Kew run."""

    def __init__(
        self,
        settings: SwitchyardConfig,
        *,
        endpoint_probe: EndpointProbe = probe_switchyard_endpoint,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        classifier_ingress_format: str | None = None,
    ) -> None:
        self._settings = settings
        self._endpoint_probe = endpoint_probe
        self._monotonic = monotonic
        self._sleep = sleep
        self._classifier_ingress_format = classifier_ingress_format
        self._process: subprocess.Popen[str] | None = None
        self._pid: int | None = None
        self._snapshot_dir: Path | None = None
        self._snapshot_path: Path | None = None
        self._drainers: list[threading.Thread] = []
        self._started = False
        self._closed = False
        self.config_sha256: str | None = None
        self.observed_version: str | None = None
        self.credential_env_names: tuple[str, ...] = ()
        self.classifier_upstream_format: str | None = None
        self._classifier_records: list[ClassifierEvidence] = []
        self._classifier_records_lock = threading.Lock()
        self._accounting_baseline: ProxyMetricsObservation | None = None
        self._accounting_final: ProxyMetricsObservation | None = None

    @property
    def pid(self) -> int:
        if self._pid is None:
            raise SwitchyardSupervisorError("managed Switchyard has not started")
        return self._pid

    def __enter__(self) -> ManagedSwitchyardSupervisor:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def classifier_evidence(self) -> tuple[ClassifierEvidence, ...]:
        """Return parsed classifier records without exposing process log text."""
        with self._classifier_records_lock:
            return tuple(self._classifier_records)

    def accounting_evidence(
        self,
        *,
        served_model: str | None = None,
        runner_input_tokens: int = 0,
        runner_output_tokens: int = 0,
    ) -> SwitchyardSelectionEvidence:
        """Collect final proxy identity observations for the current run.

        Metrics are deliberately best-effort.  The fresh process's bracketed
        delta supplies identity/overhead evidence but never replaces runner
        task usage.  An unavailable endpoint must not turn a successful runner
        result into a failed dispatch.
        """
        final = self._accounting_final
        if final is None:
            final = self._snapshot_metrics()
        baseline = self._accounting_baseline
        observation = (
            managed_metrics_delta(baseline, final)
            if baseline is not None
            else ProxyMetricsObservation.unavailable("baseline_unavailable")
        )
        return observation.selection_evidence(
            route=self._settings.route,
            served_model=served_model,
            runner_input_tokens=runner_input_tokens,
            runner_output_tokens=runner_output_tokens,
        )

    def _snapshot_metrics(self) -> ProxyMetricsObservation:
        """Take one bounded observation without leaking endpoint failures."""
        try:
            return observe_switchyard_metrics(
                endpoint=self._settings.endpoint,
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )
        except SwitchyardMetricsError as exc:
            return ProxyMetricsObservation.unavailable(exc.reason)

    def _observe_classifier_line(self, line: str) -> None:
        evidence = parse_classifier_evidence_line(
            line,
            ingress_format=self._classifier_ingress_format,
            upstream_format=self.classifier_upstream_format,
        )
        if evidence is None:
            return
        with self._classifier_records_lock:
            if len(self._classifier_records) < _MAX_CLASSIFIER_EVIDENCE:
                self._classifier_records.append(evidence)

    def _prepare_executable(self) -> PinnedExecutable:
        binary = self._settings.binary
        if binary is None:
            raise SwitchyardSupervisorError(
                "managed Switchyard executable validation failed"
            )
        try:
            return pin_executable(binary, self._settings.binary_sha256)
        except (OSError, ValueError):
            raise SwitchyardSupervisorError(
                "managed Switchyard executable validation failed"
            ) from None

    def _prepare_snapshot(self) -> tuple[Path, bytes]:
        config_path = self._settings.config_path
        if config_path is None:
            raise SwitchyardSupervisorError(
                "managed Switchyard config validation failed"
            )
        try:
            contents = _read_config_once(config_path)
            self.config_sha256 = hashlib.sha256(contents).hexdigest()
            self.classifier_upstream_format = _route_classifier_upstream_format(
                contents, self._settings.route
            )
            self._snapshot_dir, self._snapshot_path = _write_private_snapshot(contents)
        except (OSError, ValueError):
            raise SwitchyardSupervisorError(
                "managed Switchyard config validation failed"
            ) from None
        return self._snapshot_path, contents

    def _resolve_credential_environment(self, contents: bytes) -> dict[str, str]:
        try:
            required = _route_credential_names(contents, self._settings.route)
        except ValueError:
            raise SwitchyardSupervisorError(
                "managed Switchyard credential declaration validation failed"
            ) from None
        declared = tuple(sorted(self._settings.credential_env))
        if declared != required:
            raise SwitchyardSupervisorError(
                "managed Switchyard credential declaration mismatch"
            )
        values: dict[str, str] = {}
        for name in required:
            value = os.environ.get(name)
            if value is None or not value.strip():
                values.clear()
                raise SwitchyardSupervisorError(
                    "managed Switchyard credential unavailable"
                )
            values[name] = value
        self.credential_env_names = required
        return values

    def _validate_local_contract(
        self,
        pinned: PinnedExecutable,
        snapshot: Path,
        credential_environment: dict[str, str],
    ) -> None:
        timeout = float(self._settings.startup_timeout_seconds)
        try:
            version_result = run_pinned(
                pinned, ["--version"], check=False, timeout=timeout, env={}
            )
            observed = parse_switchyard_version(version_result.stdout)
            if version_result.returncode != 0 or observed is None:
                raise SwitchyardSupervisorError(
                    "managed Switchyard version probe failed"
                )
            if observed != self._settings.expected_version:
                raise SwitchyardSupervisorError(
                    "managed Switchyard reported an unexpected version"
                )
            self.observed_version = observed
            dry_run = run_pinned(
                pinned,
                ["--config", str(snapshot), "--dry-run"],
                check=False,
                timeout=timeout,
                env=dict(credential_environment),
                # The gate must validate the environment the server will run
                # under, so it retains the same declared route credential
                # names the launch does (governed review round 3, 2026-09-21).
                retain_credentials=self.credential_env_names,
            )
            if dry_run.returncode != 0:
                raise SwitchyardSupervisorError(
                    "managed Switchyard config validation failed"
                )
        except SwitchyardSupervisorError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            raise SwitchyardSupervisorError(
                "managed Switchyard local validation failed"
            ) from None

    def _start_drainers(self, proc: subprocess.Popen[str]) -> None:
        for pipe, on_line in (
            (proc.stdout, None),
            (proc.stderr, self._observe_classifier_line),
        ):
            if pipe is None:
                continue
            thread = threading.Thread(target=_drain, args=(pipe, on_line), daemon=True)
            thread.start()
            self._drainers.append(thread)

    def _probe(self, timeout_seconds: float) -> bool:
        result = self._endpoint_probe(
            endpoint=self._settings.endpoint,
            route=self._settings.route,
            timeout_seconds=timeout_seconds,
        )
        return bool(
            result.admitted
            and result.endpoint == self._settings.endpoint
            and result.route == self._settings.route
        )

    def _wait_until_ready(self) -> None:
        assert self._process is not None
        deadline = self._monotonic() + self._settings.startup_timeout_seconds
        while True:
            if self._process.poll() is not None:
                raise SwitchyardSupervisorError(
                    "managed Switchyard exited during startup"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SwitchyardSupervisorError(
                    "managed Switchyard startup unavailable"
                )
            if self._probe(min(_PROBE_TIMEOUT_SECONDS, remaining)):
                return
            delay = min(_PROBE_INTERVAL_SECONDS, deadline - self._monotonic())
            if delay > 0:
                self._sleep(delay)

    def start(self) -> None:
        """Validate, launch, and wait boundedly for this run's endpoint."""
        if self._started or self._closed:
            raise SwitchyardSupervisorError(
                "managed Switchyard supervisors belong to one run"
            )
        self._started = True
        credential_environment: dict[str, str] = {}
        try:
            host, port = _endpoint_binding(self._settings.endpoint)
            pinned = self._prepare_executable()
            snapshot, contents = self._prepare_snapshot()
            credential_environment = self._resolve_credential_environment(contents)
            self._validate_local_contract(pinned, snapshot, credential_environment)
            self._process = popen_pinned(
                pinned,
                [
                    "--config",
                    str(snapshot),
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(credential_environment),
                start_new_session=True,
                # The declared upstream route credential is this boundary's
                # explicit issuance; the child filter must not strip it.
                retain_credentials=self.credential_env_names,
            )
            self._pid = self._process.pid
            self._start_drainers(self._process)
            self._wait_until_ready()
            self._accounting_baseline = self._snapshot_metrics()
        except SwitchyardSupervisorError:
            self.close()
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            self.close()
            raise SwitchyardSupervisorError(
                "managed Switchyard startup failed"
            ) from None
        except BaseException:
            self.close()
            raise
        finally:
            credential_environment.clear()

    def is_healthy(self) -> bool:
        """Return a sanitized liveness verdict while the owned process exists."""
        if self._closed or self._process is None or self._process.poll() is not None:
            return False
        try:
            return self._probe(_PROBE_TIMEOUT_SECONDS)
        except (OSError, TimeoutError, ValueError, TypeError):
            return False

    def close(self) -> None:
        """Release process group, pipes, and private bytes; safe to repeat."""
        if self._closed:
            return
        self._capture_final_accounting_snapshot()
        self._closed = True
        try:
            if self._process is not None:
                _terminate_process_group(self._process)
        finally:
            if self._process is not None:
                for thread in self._drainers:
                    thread.join(timeout=1.0)
                for pipe in (self._process.stdout, self._process.stderr):
                    if pipe is not None:
                        with contextlib.suppress(OSError, ValueError):
                            pipe.close()
                for thread in self._drainers:
                    thread.join(timeout=1.0)
            if self._snapshot_path is not None:
                with contextlib.suppress(OSError):
                    self._snapshot_path.unlink()
            if self._snapshot_dir is not None:
                with contextlib.suppress(OSError):
                    self._snapshot_dir.rmdir()

    def _capture_final_accounting_snapshot(self) -> None:
        """Snapshot the live owned endpoint immediately before termination."""
        if self._accounting_baseline is None or self._process is None:
            return
        if self._process.poll() is None:
            self._accounting_final = self._snapshot_metrics()


@dataclass(frozen=True)
class SidecarAttemptEvidence:
    """Non-secret identities proven for one live sidecar attempt."""

    owner: str
    worker_image_id: str
    proxy_image_id: str
    network_id: str
    proxy_id: str
    worker_id: str
    config_helper_id: str
    filter_helper_id: str
    config_storage: str
    config_sha256: str
    endpoint: str
    observed_version: str
    egress_network_id: str
    packet: SidecarPacketEvidence | None
    filter_verified: bool
    cleanup_verified: bool


class SidecarAttemptSupervisor:
    """Own one verified Docker proxy topology for one paid attempt."""

    def __init__(
        self,
        settings: SwitchyardConfig,
        *,
        runtime: str,
        worker_image_id: str,
        proxy_environment: str = "none",
        owner_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._settings = settings
        self.runtime = runtime
        self.proxy_environment = proxy_environment
        if re.fullmatch(r"sha256:[0-9a-f]{64}", worker_image_id) is None:
            raise SwitchyardSupervisorError("sidecar worker image identity malformed")
        self.worker_image_id = worker_image_id
        self.owner = owner_factory()
        if re.fullmatch(r"[0-9a-f]{32}", self.owner) is None:
            raise SwitchyardSupervisorError("sidecar ownership creation failed")
        prefix = f"kew-switchyard-{self.owner[:12]}"
        self.network_name = f"{prefix}-internal"
        self.proxy_name = f"{prefix}-proxy"
        self.config_storage = f"{prefix}-config"
        self.config_helper_name = f"{prefix}-config-helper"
        self.worker_name = f"{prefix}-worker"
        self.proxy_image_id = ""
        self.network_id = ""
        self.egress_network_id = ""
        self.proxy_id = ""
        self.config_helper_id = ""
        self.filter_helper_id = ""
        self.worker_id = ""
        self.config_sha256 = ""
        self.endpoint = ""
        self.observed_version = ""
        self.credential_env_names: tuple[str, ...] = ()
        self._worker_credential_name = ""
        self._synthetic_credential = f"kew-{uuid.uuid4().hex}"
        self._live_container_ids: list[str] = []
        self._volume_created = False
        self._network_created = False
        self._started = False
        self._closed = False
        self._cleanup_verified = False
        self._filter_verified = False
        self._packet_evidence: SidecarPacketEvidence | None = None

    def __enter__(self) -> SidecarAttemptSupervisor:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def evidence(self) -> SidecarAttemptEvidence:
        if not self.endpoint or not self.proxy_id or not self.network_id:
            raise SwitchyardSupervisorError("sidecar topology is not verified")
        return SidecarAttemptEvidence(
            owner=self.owner,
            worker_image_id=self.worker_image_id,
            proxy_image_id=self.proxy_image_id,
            network_id=self.network_id,
            proxy_id=self.proxy_id,
            worker_id=self.worker_id,
            config_helper_id=self.config_helper_id,
            filter_helper_id=self.filter_helper_id,
            config_storage=self.config_storage,
            config_sha256=self.config_sha256,
            endpoint=self.endpoint,
            observed_version=self.observed_version,
            egress_network_id=self.egress_network_id,
            packet=self._packet_evidence,
            filter_verified=self._filter_verified,
            cleanup_verified=self._cleanup_verified,
        )

    def note_filter_evidence(
        self,
        *,
        worker_id: str,
        helper_id: str,
        image_id: str,
        readback: str,
    ) -> None:
        """Bind the verified NF-1 result to this exact worker attempt."""
        if (
            worker_id != self.worker_id
            or helper_id != self.filter_helper_id
            or image_id != self.worker_image_id
            or not readback.strip()
        ):
            raise SwitchyardSupervisorError("sidecar filter evidence mismatch")
        self._filter_verified = True

    def note_packet_evidence(self, evidence: SidecarPacketEvidence) -> None:
        """Bind one complete trusted packet matrix to this attempt."""
        if not isinstance(evidence, SidecarPacketEvidence):
            raise SwitchyardSupervisorError("sidecar packet evidence malformed")
        if evidence.verdict != "passed":
            raise SwitchyardSupervisorError("sidecar packet evidence refused")
        if self._packet_evidence not in {None, evidence}:
            raise SwitchyardSupervisorError("sidecar packet evidence mismatch")
        self._packet_evidence = evidence

    def topology_evidence(
        self,
        *,
        run_id: str,
        attempt_number: int,
        profile_fingerprint: str,
        stage: str,
    ) -> SidecarTopologyEvidence:
        """Compose validated durable evidence from supervisor observations."""
        evidence = self.evidence
        if (
            not evidence.worker_id
            or not evidence.config_helper_id
            or not evidence.filter_helper_id
            or evidence.packet is None
            or not evidence.filter_verified
        ):
            raise SwitchyardSupervisorError("sidecar admission evidence incomplete")
        cleanup_verdict = "pending"
        if stage == "terminal":
            cleanup_verdict = "passed" if evidence.cleanup_verified else "unavailable"
        return SidecarTopologyEvidence(
            schema_version=1,
            stage=stage,
            run_id=run_id,
            attempt_number=attempt_number,
            profile_fingerprint=profile_fingerprint,
            route=self._settings.route,
            config_sha256=evidence.config_sha256,
            owner=evidence.owner,
            worker_image_id=evidence.worker_image_id,
            proxy_image_id=evidence.proxy_image_id,
            network_id=evidence.network_id,
            egress_network_id=evidence.egress_network_id,
            proxy_id=evidence.proxy_id,
            worker_id=evidence.worker_id,
            config_helper_id=evidence.config_helper_id,
            filter_helper_id=evidence.filter_helper_id,
            config_storage=evidence.config_storage,
            endpoint=evidence.endpoint,
            observed_version=evidence.observed_version,
            packet=evidence.packet,
            filter_verdict="passed",
            cleanup_verdict=cleanup_verdict,
        )

    def worker_environment(self, credential_name: str) -> dict[str, str]:
        """Return one fresh synthetic client identity for this worker."""
        if not self.endpoint:
            raise SwitchyardSupervisorError("sidecar topology is not verified")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", credential_name) is None:
            raise SwitchyardSupervisorError("sidecar worker identity malformed")
        if self._worker_credential_name not in {"", credential_name}:
            raise SwitchyardSupervisorError("sidecar worker identity mismatch")
        self._worker_credential_name = credential_name
        return {credential_name: self._synthetic_credential}

    def _command(
        self, argv: list[str], purpose: str
    ) -> subprocess.CompletedProcess[str]:
        # The worker inherits the declared upstream credential by name from
        # the runtime client's environment; every supervisor command runs the
        # runtime client with exactly those names retained.
        with proxy_redaction_scope(self.proxy_environment):
            try:
                result = subprocess_utils.run(
                    argv,
                    check=False,
                    timeout=_SIDECAR_COMMAND_TIMEOUT_SECONDS,
                    retain_credentials=self.credential_env_names,
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                raise SwitchyardSupervisorError(
                    f"sidecar {purpose} unavailable"
                ) from None
            if result.returncode != 0:
                raise SwitchyardSupervisorError(f"sidecar {purpose} failed")
            return result

    def _bytes_command(
        self, argv: list[str], purpose: str, contents: bytes
    ) -> subprocess.CompletedProcess[bytes]:
        with proxy_redaction_scope(self.proxy_environment):
            try:
                result = subprocess_utils.run_bytes(
                    argv,
                    stdin=contents,
                    timeout=_SIDECAR_COMMAND_TIMEOUT_SECONDS,
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                raise SwitchyardSupervisorError(
                    f"sidecar {purpose} unavailable"
                ) from None
            if result.returncode != 0:
                raise SwitchyardSupervisorError(f"sidecar {purpose} failed")
            return result

    def _exact_id(self, result: subprocess.CompletedProcess[str], purpose: str) -> str:
        identity = result.stdout.strip() if isinstance(result.stdout, str) else ""
        if _FULL_CONTAINER_ID.fullmatch(identity) is None:
            raise SwitchyardSupervisorError(f"sidecar {purpose} identity malformed")
        return identity

    def _inspect(self, kind: str, identity: str) -> dict[str, object]:
        result = self._command(
            build_resource_inspect_argv(
                runtime=self.runtime,
                resource_kind=kind,
                resource_id=identity,
            ),
            f"{kind} inspection",
        )
        try:
            payload = json.loads(result.stdout)
            inspected = payload[0]
        except (IndexError, TypeError, json.JSONDecodeError):
            raise SwitchyardSupervisorError(
                f"sidecar {kind} inspection malformed"
            ) from None
        if not isinstance(inspected, dict):
            raise SwitchyardSupervisorError(f"sidecar {kind} inspection malformed")
        return inspected

    def _inspect_volume(self) -> None:
        result = self._command(
            [self.runtime, "volume", "inspect", self.config_storage],
            "private storage inspection",
        )
        try:
            payload = json.loads(result.stdout)
            inspected = payload[0]
        except (IndexError, TypeError, json.JSONDecodeError):
            raise SwitchyardSupervisorError(
                "sidecar private storage inspection malformed"
            ) from None
        if (
            not isinstance(inspected, dict)
            or inspected.get("Name") != self.config_storage
            or inspected.get("Driver") != "local"
            or inspected.get("Scope") != "local"
            or not isinstance(inspected.get("Labels"), dict)
            or inspected["Labels"].get(SWITCHYARD_ATTEMPT_LABEL) != self.owner
        ):
            raise SwitchyardSupervisorError("sidecar private storage identity mismatch")

    def _resolve_proxy_image(self) -> None:
        sidecar = self._settings.sidecar
        if sidecar is None or not sidecar.enabled:
            raise SwitchyardSupervisorError("sidecar configuration mismatch")
        resolution = inspect_image_id(self.runtime, sidecar.image)
        if resolution.image_id is None:
            raise SwitchyardSupervisorError("sidecar proxy image resolution failed")
        self.proxy_image_id = resolution.image_id

    def _validate_route(self, contents: bytes) -> tuple[str, ...]:
        try:
            required = _route_credential_names(contents, self._settings.route)
        except ValueError:
            raise SwitchyardSupervisorError(
                "sidecar route credential inspection failed"
            ) from None
        declared = tuple(sorted(self._settings.credential_env))
        if declared != required:
            raise SwitchyardSupervisorError(
                "sidecar route credential declaration mismatch"
            )
        if any(not os.environ.get(name, "").strip() for name in required):
            raise SwitchyardSupervisorError("sidecar route credential unavailable")
        self.credential_env_names = required
        return required

    def _read_private_config(self, config_path: Path) -> bytes:
        try:
            return _read_config_once(config_path, require_private_storage=True)
        except (OSError, ValueError):
            raise SwitchyardSupervisorError(
                "sidecar private config validation failed"
            ) from None

    def _create_config_storage(self, contents: bytes) -> None:
        created = self._command(
            [
                self.runtime,
                "volume",
                "create",
                "--label",
                f"{SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
                self.config_storage,
            ],
            "private storage creation",
        )
        if (
            not isinstance(created.stdout, str)
            or created.stdout.strip() != self.config_storage
        ):
            raise SwitchyardSupervisorError(
                "sidecar private storage identity malformed"
            )
        self._inspect_volume()
        self._volume_created = True
        helper = self._command(
            [
                self.runtime,
                "create",
                "--name",
                self.config_helper_name,
                "--label",
                f"{SWITCHYARD_ATTEMPT_LABEL}={self.owner}",
                "--pull=never",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "SETUID",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:size=1m,mode=0700",  # nosec B108 - private container tmpfs
                "--pids-limit",
                "16",
                "--memory",
                "32m",
                "--memory-swap",
                "32m",
                "--cpus",
                "0.10",
                "--user",
                "0:0",
                "--interactive",
                "--mount",
                f"type=volume,src={self.config_storage},dst=/private",
                "--entrypoint",
                "/bin/sh",
                *_container_proxy_environment_args(self.proxy_environment),
                self.worker_image_id,
                "-c",
                'set -euC; test -z "$(ls -A /private)"; umask 077; '
                ": > /private/routes.toml; "
                "dd of=/private/routes.toml status=none; "
                "test -s /private/routes.toml; "
                "chmod 0600 /private/routes.toml; "
                f"chown {_SIDECAR_PROXY_UID}:{_SIDECAR_PROXY_UID} "
                "/private/routes.toml; "
                f"setpriv --reuid={_SIDECAR_PROXY_UID} "
                f"--regid={_SIDECAR_PROXY_UID} --clear-groups "
                "sha256sum /private/routes.toml; "
                "stat --format='%u:%g:%a' /private/routes.toml",
            ],
            "private storage helper creation",
        )
        self.config_helper_id = self._exact_id(helper, "private storage helper")
        self._live_container_ids.append(self.config_helper_id)
        written = self._bytes_command(
            [
                self.runtime,
                "start",
                "--attach",
                "--interactive",
                self.config_helper_id,
            ],
            "private storage write",
            contents,
        )
        raw_stdout = written.stdout if isinstance(written.stdout, bytes) else b""
        try:
            observed = raw_stdout.decode("ascii", errors="strict").splitlines()
        except UnicodeError:
            raise SwitchyardSupervisorError(
                "sidecar private config digest malformed"
            ) from None
        if len(observed) != 2:
            raise SwitchyardSupervisorError(
                "sidecar private config verification malformed"
            )
        if observed[0] != f"{self.config_sha256}  /private/routes.toml":
            raise SwitchyardSupervisorError("sidecar private config digest mismatch")
        if observed[1] != f"{_SIDECAR_PROXY_UID}:{_SIDECAR_PROXY_UID}:600":
            raise SwitchyardSupervisorError("sidecar private config ownership mismatch")
        self._remove_live_container(self.config_helper_id, "private storage helper")

    def _create_network(self) -> None:
        created = self._command(
            build_per_run_network_argv(
                runtime=self.runtime,
                network=self.network_name,
                owner=self.owner,
            ),
            "attempt network creation",
        )
        self.network_id = self._exact_id(created, "attempt network")
        self._network_created = True

    def _create_proxy(self, credential_names: tuple[str, ...]) -> None:
        created = self._command(
            build_switchyard_sidecar_argv(
                runtime=self.runtime,
                image_id=self.proxy_image_id,
                container_name=self.proxy_name,
                internal_network=self.network_name,
                owner=self.owner,
                config_storage=self.config_storage,
                credential_env_names=credential_names,
                proxy_environment=self.proxy_environment,
            ),
            "proxy creation",
        )
        self.proxy_id = self._exact_id(created, "proxy")
        self._live_container_ids.append(self.proxy_id)
        sidecar = self._settings.sidecar
        assert sidecar is not None
        self._command(
            build_network_attachment_argv(
                runtime=self.runtime,
                network=sidecar.egress_network,
                container_id=self.proxy_id,
            ),
            "proxy egress attachment",
        )
        self._command(
            [self.runtime, "start", self.proxy_id],
            "proxy startup",
        )

    def _verify_internal_network(self, network: dict[str, object]) -> None:
        labels = network.get("Labels")
        attachments = network.get("Containers")
        matches = (
            network.get("Id") == self.network_id,
            network.get("Name") == self.network_name,
            network.get("Internal") is True,
            isinstance(labels, dict),
            isinstance(attachments, dict),
        )
        if not all(matches):
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        assert isinstance(labels, dict)
        assert isinstance(attachments, dict)
        if labels.get(SWITCHYARD_ATTEMPT_LABEL) != self.owner or set(attachments) != {
            self.proxy_id
        }:
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")

    def _proxy_components(
        self, proxy: dict[str, object]
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        list[object],
        str,
    ]:
        config = proxy.get("Config")
        state = proxy.get("State")
        network_settings = proxy.get("NetworkSettings")
        host_config = proxy.get("HostConfig")
        mounts = proxy.get("Mounts")
        proxy_path = proxy.get("Path")
        types_match = (
            isinstance(config, dict),
            isinstance(state, dict),
            isinstance(network_settings, dict),
            isinstance(host_config, dict),
            isinstance(mounts, list),
            isinstance(proxy_path, str),
        )
        if not all(types_match):
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        return (
            cast(dict[str, object], config),
            cast(dict[str, object], state),
            cast(dict[str, object], network_settings),
            cast(dict[str, object], host_config),
            cast(list[object], mounts),
            cast(str, proxy_path),
        )

    def _proxy_networks(
        self, proxy: dict[str, object]
    ) -> tuple[dict[str, object], str, list[object]]:
        (
            config,
            state,
            network_settings,
            host_config,
            mounts,
            proxy_path,
        ) = self._proxy_components(proxy)
        identity_matches = (
            proxy.get("Id") == self.proxy_id,
            proxy.get("Image") == self.proxy_image_id,
            proxy_path.startswith("/"),
        )
        if not all(identity_matches):
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        labels = config.get("Labels")
        networks = network_settings.get("Networks")
        if not all((isinstance(labels, dict), isinstance(networks, dict))):
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        labels = cast(dict[str, object], labels)
        networks = cast(dict[str, object], networks)
        runtime_matches = (
            labels.get(SWITCHYARD_ATTEMPT_LABEL) == self.owner,
            config.get("User") == f"{_SIDECAR_PROXY_UID}:{_SIDECAR_PROXY_UID}",
            state.get("Running") is True,
            not bool(host_config.get("CapAdd")),
            host_config.get("Privileged") is False,
            host_config.get("ReadonlyRootfs") is True,
            host_config.get("PidMode") in {None, ""},
        )
        if not all(runtime_matches):
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        self._verify_proxy_environment(config)
        return networks, proxy_path, mounts

    def _verify_proxy_environment(self, config: dict[str, object]) -> None:
        environment = config.get("Env")
        if not isinstance(environment, list) or not all(
            isinstance(entry, str) for entry in environment
        ):
            raise SwitchyardSupervisorError("sidecar proxy environment malformed")
        credential_names = [
            entry.partition("=")[0]
            for entry in environment
            if entry.partition("=")[0].casefold() in _FOLDED_PROVIDER_CREDENTIAL_NAMES
        ]
        if tuple(sorted(credential_names)) != self.credential_env_names:
            raise SwitchyardSupervisorError("sidecar proxy credential mismatch")

    def _proxy_internal_address(self, networks: dict[str, object]) -> str:
        sidecar = self._settings.sidecar
        assert sidecar is not None
        if set(networks) != {self.network_name, sidecar.egress_network}:
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        expected_network_ids = {
            self.network_name: self.network_id,
            sidecar.egress_network: self.egress_network_id,
        }
        for name, expected_id in expected_network_ids.items():
            attachment = networks[name]
            if (
                not isinstance(attachment, dict)
                or attachment.get("NetworkID") != expected_id
            ):
                raise SwitchyardSupervisorError("sidecar proxy topology mismatch")
        internal = networks[self.network_name]
        address = internal.get("IPAddress") if isinstance(internal, dict) else None
        if not isinstance(address, str):
            raise SwitchyardSupervisorError("sidecar proxy address malformed")
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise SwitchyardSupervisorError("sidecar proxy address malformed") from None
        if parsed.version != 4:
            raise SwitchyardSupervisorError("sidecar proxy address malformed")
        return str(parsed)

    def _verify_proxy_mount(self, mounts: list[object]) -> None:
        matches = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Destination") == "/run/kew-switchyard"
            and mount.get("Name") == self.config_storage
            and mount.get("RW") is False
        ]
        if len(matches) != 1 or len(mounts) != 1:
            raise SwitchyardSupervisorError("sidecar proxy topology mismatch")

    def _verify_proxy_version(self, proxy_path: str) -> None:
        version = self._command(
            [self.runtime, "exec", self.proxy_id, proxy_path, "--version"],
            "proxy version inspection",
        )
        observed = parse_switchyard_version(version.stdout)
        if observed != self._settings.expected_version:
            raise SwitchyardSupervisorError("sidecar proxy version mismatch")
        self.observed_version = observed

    def _verify_proxy_topology(self) -> None:
        network = self._inspect("network", self.network_id)
        proxy = self._inspect("container", self.proxy_id)
        self._verify_internal_network(network)
        networks, proxy_path, mounts = self._proxy_networks(proxy)
        address = self._proxy_internal_address(networks)
        self._verify_proxy_mount(mounts)
        self._verify_proxy_version(proxy_path)
        self.endpoint = f"http://{address}:{_SIDECAR_PROXY_PORT}"

    def start(self) -> None:
        """Create and verify the proxy topology without launching agent code."""
        if self._started or self._closed:
            raise SwitchyardSupervisorError("sidecar supervisors belong to one attempt")
        self._started = True
        config_path = self._settings.config_path
        if config_path is None:
            raise SwitchyardSupervisorError("sidecar private config unavailable")
        try:
            contents = self._read_private_config(config_path)
            self.config_sha256 = hashlib.sha256(contents).hexdigest()
            credential_names = self._validate_route(contents)
            self._resolve_proxy_image()
            sidecar = self._settings.sidecar
            assert sidecar is not None
            egress = self._inspect("network", sidecar.egress_network)
            if (
                egress.get("Internal") is not False
                or egress.get("Name") != sidecar.egress_network
                or _FULL_CONTAINER_ID.fullmatch(str(egress.get("Id", ""))) is None
            ):
                raise SwitchyardSupervisorError("sidecar egress topology mismatch")
            self.egress_network_id = str(egress["Id"])
            self._create_config_storage(contents)
            self._create_network()
            self._create_proxy(credential_names)
            self._verify_proxy_topology()
        except BaseException:
            self.close()
            raise

    def create_worker(
        self, argv: list[str], *, on_identity: Callable[[str], None] | None = None
    ) -> str:
        """Create and verify one trusted worker bootstrap in the exact network."""
        if not self.endpoint or self.worker_id:
            raise SwitchyardSupervisorError("sidecar worker creation out of order")
        created = self._command(argv, "worker bootstrap creation")
        self.worker_id = self._exact_id(created, "worker")
        self._live_container_ids.append(self.worker_id)
        if on_identity is not None:
            on_identity(self.worker_id)
        self._command(
            [self.runtime, "start", self.worker_id], "worker bootstrap startup"
        )
        worker = self._inspect("container", self.worker_id)
        self._verify_worker_topology(worker)
        self._wait_for_proxy()
        return self.worker_id

    def _worker_components(
        self, worker: dict[str, object]
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        list[object],
    ]:
        config = worker.get("Config")
        state = worker.get("State")
        network_settings = worker.get("NetworkSettings")
        host_config = worker.get("HostConfig")
        mounts = worker.get("Mounts")
        types_match = (
            isinstance(config, dict),
            isinstance(state, dict),
            isinstance(network_settings, dict),
            isinstance(host_config, dict),
            isinstance(mounts, list),
        )
        if not all(types_match):
            raise SwitchyardSupervisorError("sidecar worker topology mismatch")
        assert isinstance(config, dict)
        assert isinstance(state, dict)
        assert isinstance(network_settings, dict)
        assert isinstance(host_config, dict)
        assert isinstance(mounts, list)
        return config, state, network_settings, host_config, mounts

    def _verify_worker_topology(self, worker: dict[str, object]) -> None:
        config, state, network_settings, host_config, mounts = self._worker_components(
            worker
        )
        labels = config.get("Labels")
        networks = network_settings.get("Networks")
        matches = (
            worker.get("Id") == self.worker_id,
            worker.get("Image") == self.worker_image_id,
            state.get("Running") is True,
            config.get("Entrypoint") == ["/bin/sleep"],
            config.get("Cmd") == ["infinity"],
            isinstance(labels, dict),
            isinstance(networks, dict),
            not bool(host_config.get("CapAdd")),
            host_config.get("Privileged") is False,
            host_config.get("ReadonlyRootfs") is True,
            host_config.get("PidMode") in {None, ""},
        )
        if not all(matches):
            raise SwitchyardSupervisorError("sidecar worker topology mismatch")
        assert isinstance(labels, dict)
        assert isinstance(networks, dict)
        self._verify_worker_environment(config)
        forbidden_mount = self._worker_has_forbidden_mount(mounts)
        identity_matches = (
            labels.get(SWITCHYARD_ATTEMPT_LABEL) == self.owner,
            set(networks) == {self.network_name},
            isinstance(networks.get(self.network_name), dict)
            and networks[self.network_name].get("NetworkID") == self.network_id,
            not forbidden_mount,
        )
        if not all(identity_matches):
            raise SwitchyardSupervisorError("sidecar worker topology mismatch")

    def _verify_worker_environment(self, config: dict[str, object]) -> None:
        environment = config.get("Env")
        if not isinstance(environment, list) or not all(
            isinstance(entry, str) for entry in environment
        ):
            raise SwitchyardSupervisorError("sidecar worker environment malformed")
        provider_entries = [
            entry
            for entry in environment
            if entry.partition("=")[0].casefold() in _FOLDED_PROVIDER_CREDENTIAL_NAMES
        ]
        expected = f"{self._worker_credential_name}={self._synthetic_credential}"
        if provider_entries != [expected]:
            raise SwitchyardSupervisorError("sidecar worker credential mismatch")

    def _worker_has_forbidden_mount(self, mounts: list[object]) -> bool:
        return any(
            isinstance(mount, dict)
            and (
                mount.get("Name") == self.config_storage
                or mount.get("Destination")
                in {
                    "/run/kew-switchyard",
                    _SIDECAR_CONFIG_PATH,
                    "/run/docker.sock",
                    "/var/run/docker.sock",
                }
            )
            for mount in mounts
        )

    def note_filter_helper(self, helper_id: str) -> None:
        """Track one live NF-1 helper for cleanup and final absence proof."""
        if _FULL_CONTAINER_ID.fullmatch(helper_id) is None:
            raise SwitchyardSupervisorError("sidecar filter helper identity malformed")
        if self.filter_helper_id not in {"", helper_id}:
            raise SwitchyardSupervisorError("sidecar filter helper identity mismatch")
        self.filter_helper_id = helper_id
        if helper_id not in self._live_container_ids:
            self._live_container_ids.append(helper_id)

    def note_filter_helper_removed(self, helper_id: str) -> None:
        """Retain one proven-absent NF-1 helper without treating it as live."""
        if helper_id != self.filter_helper_id:
            raise SwitchyardSupervisorError("sidecar filter helper identity mismatch")
        with contextlib.suppress(ValueError):
            self._live_container_ids.remove(helper_id)

    def _worker_http_get(self, path: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess_utils.run(
                [
                    self.runtime,
                    "exec",
                    self.worker_id,
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "1",
                    "--max-time",
                    "2",
                    f"{self.endpoint}{path}",
                ],
                check=False,
                timeout=_SIDECAR_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            _record_sidecar_readiness("worker_probe_unavailable")
            return None

    def _proxy_lists_route(self) -> bool:
        result = self._worker_http_get("/v1/models")
        if result is None or result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout)
            items = payload["data"]
        except (KeyError, TypeError, json.JSONDecodeError):
            _record_sidecar_readiness("route_response_malformed")
            return False
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str) for item in items
        ):
            return False
        return self._settings.route in {item["id"] for item in items}

    def _proxy_is_ready(self) -> bool:
        health = self._worker_http_get("/health")
        return bool(
            health is not None and health.returncode == 0 and self._proxy_lists_route()
        )

    def _wait_for_proxy(self) -> None:
        deadline = time.monotonic() + self._settings.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._proxy_is_ready():
                return
            time.sleep(_PROBE_INTERVAL_SECONDS)
        raise SwitchyardSupervisorError("sidecar proxy startup unavailable")

    def _remove_live_container(self, identity: str, purpose: str) -> None:
        try:
            self._command(
                build_container_cleanup_argv(
                    runtime=self.runtime,
                    container_ids=(identity,),
                ),
                f"{purpose} cleanup",
            )
        except SwitchyardSupervisorError:
            inventory_stdout = self._command(
                [
                    self.runtime,
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                ],
                f"{purpose} cleanup readback",
            ).stdout
            if not isinstance(inventory_stdout, str):
                raise SwitchyardSupervisorError(
                    f"sidecar {purpose} cleanup inventory missing"
                ) from None
            inventory = inventory_stdout.splitlines()
            if (
                any(_FULL_CONTAINER_ID.fullmatch(item) is None for item in inventory)
                or identity in inventory
            ):
                raise
        self._live_container_ids.remove(identity)

    def _id_inventory(self, kind: str, *, all_objects: bool) -> list[str]:
        all_args = ["--all"] if all_objects else []
        stdout = self._command(
            [self.runtime, kind, "ls", *all_args, "--quiet", "--no-trunc"],
            f"{kind} cleanup readback",
        ).stdout
        if not isinstance(stdout, str):
            raise SwitchyardSupervisorError("sidecar cleanup inventory missing")
        identities = stdout.splitlines()
        if any(_FULL_CONTAINER_ID.fullmatch(item) is None for item in identities):
            raise SwitchyardSupervisorError(f"sidecar {kind} inventory malformed")
        return identities

    def _volume_inventory(self) -> list[str]:
        stdout = self._command(
            [self.runtime, "volume", "ls", "--quiet"],
            "private storage cleanup readback",
        ).stdout
        if not isinstance(stdout, str):
            raise SwitchyardSupervisorError("sidecar cleanup inventory missing")
        volumes = stdout.splitlines()
        try:
            for volume in volumes:
                _sidecar_resource(volume, "volume inventory")
        except ValueError:
            raise SwitchyardSupervisorError(
                "sidecar private storage inventory malformed"
            ) from None
        return volumes

    def _prove_absence(self) -> None:
        containers = self._id_inventory("container", all_objects=True)
        networks = self._id_inventory("network", all_objects=False)
        volumes = self._volume_inventory()
        known_containers = {
            self.config_helper_id,
            self.filter_helper_id,
            self.proxy_id,
            self.worker_id,
        } - {""}
        if known_containers.intersection(containers):
            raise SwitchyardSupervisorError("sidecar container absence unproved")
        if self.network_id and self.network_id in networks:
            raise SwitchyardSupervisorError("sidecar network absence unproved")
        if self.config_storage in volumes:
            raise SwitchyardSupervisorError("sidecar private storage absence unproved")

    def close(self) -> None:
        """Remove exact owned resources and prove absence before result release."""
        if self._closed:
            return
        self._closed = True
        failures: list[SwitchyardSupervisorError] = []
        for identity in tuple(reversed(self._live_container_ids)):
            try:
                self._remove_live_container(identity, "container")
            except SwitchyardSupervisorError as exc:
                failures.append(exc)
        if self._network_created:
            try:
                self._command(
                    build_network_cleanup_argv(
                        runtime=self.runtime,
                        network_id=self.network_id,
                    ),
                    "attempt network cleanup",
                )
                self._network_created = False
            except SwitchyardSupervisorError as exc:
                failures.append(exc)
        if self._volume_created:
            try:
                self._command(
                    [self.runtime, "volume", "rm", self.config_storage],
                    "private storage cleanup",
                )
                self._volume_created = False
            except SwitchyardSupervisorError as exc:
                failures.append(exc)
        try:
            self._prove_absence()
        except SwitchyardSupervisorError as exc:
            failures.append(exc)
        self._synthetic_credential = ""
        if failures:
            raise SwitchyardSupervisorError("sidecar cleanup or absence proof failed")
        self._cleanup_verified = True
