"""`kew doctor` — local environment diagnostics.

Checks the local setup for known issues that cause silent failures or wasted
dispatch spend. Currently checks:

- Runner credential: each configured runner's container credential is in the
  environment (containers cannot inherit CLI login, #721). Names only, never
  values; a runner with its own native sandbox is exempt.
- gh login: ``gh auth status`` succeeds, and a missing login is told apart from
  a GitHub that ``gh`` could not reach (#1731).
- Container runtime: verifies the configured binary and its daemon response.
- Route image: verifies the default route's effective image exists locally.
- Allowlist network: verifies the configured posture and local network.
- Agent image freshness: warns if the local agent image predates the last
  commit to ``docker/agent.Dockerfile`` (stale image can fail-close a fleet).
- Retained agent tags: warns about unprotected tags in the three exact packaged
  agent repositories. It reads image identities and container references only.
- Pinned reservations: warns if any pseudo-epic reservation (delegation OR
  review — every ref in ``kew.pseudo_epics.PSEUDO_EPIC_REFS``) is pinned: a
  reservation whose run has ended (or whose dispatch never started) has sat
  unreleased past a grace, so the watcher is likely down rather than just
  between sweeps.
- Shadow dispatch.db: warns if a ``dispatch.db`` exists at the CWD or in a
  common wrong location that would shadow the canonical configured path and
  mislead raw SQLite queries (2026-07-23 footgun — #1037).
- Anchor age: warns when the newest line in ``audit-anchors.jsonl`` is older
  than ``[audit] anchor_max_age_days`` (default 7). An unanchored chain still
  verifies clean, so integrity cannot be the alarm for a dead witness (#1408).
- Route live validity: reports the STORED verdict for the complex route's exact
  execution boundary. Read-only and free — every other check here is static, so
  a route with no probe evidence reads ``unverified``, never "fine".
- Temporary storage: reports bounded, metadata-only pytest and task storage.
- Host providers: reports the configured ``[hosts]`` providers. ``local`` is
  always listed; a configured gcp host is probed read-only for the gcloud CLI,
  a usable login, and instance reachability, and each failure carries the
  command that fixes it (an expired ``gcloud auth login`` is the common one). A
  probe blocked by an earlier failure reads "not probed", never as a pass.

Each check records one :class:`kew.readiness.CheckResult` under a stable id
(``CHECK_IDS``). The prose report and ``kew doctor --json`` both render from
that one list (:func:`run_checks`), so the two cannot disagree. Every id is
reported exactly once, in ``CHECK_IDS`` order: a check that does not apply, or
that an earlier failure blocked, is ``skip`` with the reason, and a check that
raises is ``fail`` rather than the end of the report.

``kew doctor route-live-confirm`` is the one action in kew that spends money on
a diagnostic: one bounded provider call across that exact boundary (#1516). It
is a separate, explicitly confirmed subcommand precisely so no passive
surface — bare ``kew doctor`` included — can ever trigger a charge.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from kew import subprocess_utils, work_dirs
from kew.audit import DEFAULT_ANCHORS_PATH, default_anchors_path, newest_anchor_age
from kew.audit_chain import Anchor, parse_anchors
from kew.cli._apps import doctor_app
from kew.config import (
    DispatchConfig,
    SandboxConfig,
    load_config,
    project_root,
    resolved_config_path,
)
from kew.hosts.diagnostics import FAIL, SKIP, HostCheck, check_hosts
from kew.ledger_identity import (
    LedgerIdentityError,
    canonical_project_root,
    open_managed_store,
)
from kew.model_eol_snapshot import check_lifecycle
from kew.openshell import (
    GatewayReport,
    OpenShellClient,
    OpenShellError,
    OpenShellGatewayUnreachable,
    OpenShellMissing,
    OpenShellTimeout,
)
from kew.openshell_doctor import (
    CHECK_AGENT_AUTHORITY,
    CHECK_CERTIFICATION,
    CHECK_IMAGE_PINS,
    CHECK_LANDLOCK,
    CHECK_PROVIDERS,
    CHECK_TELEMETRY,
    CHECK_TLS,
    CHECK_VERSION,
    OpenShellCheck,
    check_agent_authority,
    check_certification,
    check_image_pins,
    check_landlock,
    check_providers,
    check_telemetry,
    check_tls,
    check_versions,
    mapped_runners,
)
from kew.progress import _dim, _error, _log, _success, _warn
from kew.readiness import CheckResult, CheckStatus, readiness_report
from kew.readiness_probe import (
    PROBE_CEILING_USD,
    PROBE_TIMEOUT_SECONDS,
    LiveConfirmation,
    LiveStatus,
    ProbeRoute,
    confirm_route_live,
    resolve_probe_route,
    route_live_status,
)
from kew.run_store import (
    AuditEvent,
    EpicReservation,
    RunStore,
    SchemaTooNewError,
    SqliteRunStore,
)
from kew.runner_liveness import check_runner_liveness
from kew.runner_quota import RunnerQuotaHold, read_runner_quota_hold
from kew.runners.registry import (
    KNOWN_RUNNER_NAMES,
    resolve_runner,
    runner_credential_fingerprint,
)
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.sandbox import (
    _PACKAGED_IMAGE_TAGS,
    ALLOWLIST_NETWORK,
    DEFAULT_IMAGE_RUNNER,
    POSTURE_UNCONTAINED,
    STOCK_AGENT_IMAGE,
    SandboxDecision,
    SandboxImageUnmapped,
    SandboxRuntimeUnavailable,
    _expected_packaged_image_pin,
    _injected_credentials,
    credential_missing_message,
    detect_runtime,
    evaluate_agent_image_staleness,
    network_attributes,
    packaged_agent_image_context,
    resolve_effective_sandbox,
    resolve_runner_image,
    runner_credential_env_vars,
    runner_native_containment,
    runner_uncontainable,
)
from kew.staleness import check_editable_staleness
from kew.subprocess_utils import (
    preflight_tools,
    register_tools,
    subprocess_deadline,
)

# A reservation whose run has ended but is not yet swept is transient during
# normal operation (the watcher reconciles every ~20s). Only flag it as pinned
# once it has sat terminal past this grace, so `kew doctor` reports a real leak
# rather than the healthy window between a run finishing and the next sweep
# (#972 review Finding 4).
_PINNED_TERMINAL_GRACE_SECONDS = 180
_DEGRADED_REVIEW_THRESHOLD = 3
_CONTAINER_CHECKS = ("container-runtime", "route-image", "allowlist-network")
_TEMP_STORAGE_WARNING_BYTES = 2 * 1024**3
_STORAGE_ENTRY_LIMIT = 100_000
_STORAGE_SCAN_SECONDS = 5.0
_TASK_STORAGE_NAMES = frozenset({".venv", "uv-cache", "uv-tools", "venv"})
_ENV_CACHE_UNSET = object()
# Derived from the packaged image map so a new packaged runner cannot be added
# without doctor sweeping its repository. A packaged repository missing here
# falls past _exact_agent_tag into a live `image inspect` that exits 1 for an
# image this host has not built, degrading the whole check to "inventory
# unknown" and failing `kew doctor` (#2317 review: the codex pin did exactly
# this while `kew-agent-codex` was absent from these two tables).
_RETAINED_AGENT_REPOSITORIES = tuple(
    sorted({tag.partition(":")[0] for tag in _PACKAGED_IMAGE_TAGS.values()})
)
_PODMAN_AGENT_REPOSITORIES = {
    f"localhost/{repository}": repository for repository in _RETAINED_AGENT_REPOSITORIES
}
_IMAGE_LIST_FORMAT = "{{json .Repository}}\t{{json .Tag}}\t{{json .ID}}\t{{json .Size}}"
_CONTAINER_IMAGE_FORMAT = "{{json .Image}}"
_PODMAN_CONTAINER_IMAGE_FORMAT = "{{json .ImageID}}"
_IMAGE_ID_FORMAT = "{{json .Id}}"
_IMAGE_ID_RE = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")
_IMAGE_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_IMAGE_SIZE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:B|kB|KB|KiB|MB|MiB|GB|GiB|TB|TiB)")


#: Every id a check can report under, pinned by ``tests/test_doctor_cli.py`` so a
#: rename is a deliberate change: ``kew init``, the console and any agent
#: reading ``kew doctor --json`` key on these strings.
CHECK_IDS: tuple[str, ...] = (
    "project-config",
    "required-tools",
    "runner-credential",
    "gh-auth",
    "containment",
    "container-runtime",
    "route-image",
    "allowlist-network",
    "openshell-gateway",
    "openshell-version",
    "openshell-tls",
    "openshell-telemetry",
    "openshell-image-pins",
    "openshell-providers",
    "openshell-agent-authority",
    "openshell-landlock",
    "openshell-certification",
    "model-eol",
    "anchor-age",
    "temporary-storage",
    "editable-install",
    "runner-liveness",
    "host-local",
    "host-gcloud",
    "host-gcloud-auth",
    "host-gcp-instance",
    "agent-image",
    "retained-agent-tags",
    "degraded-review",
    "reservations",
    "shadow-db",
    "route-live",
    "runner-quota",
)

_OK = CheckStatus.OK
_WARN = CheckStatus.WARN
_FAIL = CheckStatus.FAIL
_SKIP = CheckStatus.SKIP

_HUMAN_STATUS = {_OK: "PASS", _WARN: "WARN", _FAIL: "FAIL", _SKIP: "skipped"}

#: The OpenShell checks below the gateway, in report order. Each needs the
#: gateway's answer, so a gateway that gave none blocks all of them.
_OPENSHELL_DOWNSTREAM_IDS = (
    CHECK_VERSION,
    CHECK_TLS,
    CHECK_TELEMETRY,
    CHECK_IMAGE_PINS,
    CHECK_PROVIDERS,
    CHECK_AGENT_AUTHORITY,
    CHECK_LANDLOCK,
    CHECK_CERTIFICATION,
)
_OPENSHELL_IDS = ("openshell-gateway", *_OPENSHELL_DOWNSTREAM_IDS)
_GCP_HOST_IDS = ("host-gcloud", "host-gcloud-auth", "host-gcp-instance")

#: The most one :func:`run_checks` may spend on subprocesses. Every probe it
#: reaches is also bounded on its own; this caps the sum, so a web caller gets
#: an answer in bounded time even when several probes hang.
_RUN_CHECKS_DEADLINE_SECONDS = 120.0
_DEADLINE_REASON = "not run: kew doctor's overall deadline passed"


@dataclass
class _Recorder:
    results: dict[str, CheckResult] = field(default_factory=dict)
    #: Advisory lines raised outside a check's own verdict, by check id.
    warnings: dict[str, list[str]] = field(default_factory=dict)
    deadline: float = field(
        default_factory=lambda: time.monotonic() + _RUN_CHECKS_DEADLINE_SECONDS
    )

    def ordered(self) -> list[CheckResult]:
        """Every id once, in ``CHECK_IDS`` order, with its advisory lines."""
        results = []
        for check_id in CHECK_IDS:
            result = self.results.get(check_id) or CheckResult(
                check_id, _SKIP, "not reported by its check"
            )
            extra = tuple(self.warnings.get(check_id, ()))
            if extra:
                result = replace(result, warnings=(*result.warnings, *extra))
            results.append(result)
        return results


#: The run collecting results. Outside one (a check called on its own) each
#: result is rendered the moment it is recorded.
_RECORDER: ContextVar[_Recorder | None] = ContextVar("doctor_recorder", default=None)


def _emitter(status: CheckStatus) -> Callable[[str], None]:
    # Looked up per call, not tabled at import, so a patched emitter is honoured.
    if status is _OK:
        return _success
    if status is _SKIP:
        return _dim
    return _warn


def _render_check(result: CheckResult) -> None:
    """The human rendering of one result — the only way a verdict is printed."""
    emit = _emitter(result.status)
    emit(f"  {result.id}: {_HUMAN_STATUS[result.status]} ({result.summary})")
    for detail in result.details:
        emit(f"  {result.id}: {detail}")
    # Advisory lines keep the warning emitter whatever the verdict: a passing
    # check's warning is still a warning.
    for warning in result.warnings:
        _warn(f"  {result.id}: WARN ({warning})")
    if result.hint:
        emit(f"  hint: {result.hint}")
    if result.fix:
        emit(f"  fix: {result.fix}")


def _record(
    check_id: str,
    status: CheckStatus,
    summary: str,
    *,
    hint: str | None = None,
    fix: str | None = None,
    details: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> None:
    if check_id not in CHECK_IDS:
        raise ValueError(f"unknown doctor check id {check_id!r}")
    result = CheckResult(check_id, status, summary, hint, fix, details, warnings)
    recorder = _RECORDER.get()
    if recorder is None:
        _render_check(result)
        return
    if check_id in recorder.results:
        raise ValueError(f"doctor check id {check_id!r} reported twice")
    recorder.results[check_id] = result


def _record_advisory(check_id: str, warning: str) -> None:
    """Attach an advisory line to ``check_id`` without changing its verdict."""
    recorder = _RECORDER.get()
    if recorder is None:
        _warn(f"  {check_id}: WARN ({warning})")
        return
    recorder.warnings.setdefault(check_id, []).append(warning)


def _skip_unreported(check_ids: tuple[str, ...], reason: str) -> None:
    """Record ``reason`` as a skip for each of ``check_ids`` not yet reported."""
    recorder = _RECORDER.get()
    reported = recorder.results if recorder is not None else {}
    for check_id in check_ids:
        if check_id not in reported:
            _record(check_id, _SKIP, reason)


class _RetainedTagInventoryError(RuntimeError):
    """A read-only runtime inventory cannot support a trusted conclusion."""


@dataclass(frozen=True)
class _RetainedAgentTag:
    repository: str
    tag: str
    identity: str
    logical_size: str

    @property
    def reference(self) -> str:
        return f"{self.repository}:{self.tag}"


@dataclass
class _StorageInventory:
    """Bound every metadata traversal used by the storage check."""

    deadline: float = field(
        default_factory=lambda: time.monotonic() + _STORAGE_SCAN_SECONDS
    )
    entries: int = 0
    errors: int = 0
    entry_limited: bool = False
    time_limited: bool = False
    seen_files: set[tuple[int, int]] = field(default_factory=set)
    directories: dict[Path, tuple[int, int]] = field(default_factory=dict)
    tree_totals: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def incomplete(self) -> bool:
        return bool(self.errors or self.entry_limited or self.time_limited)

    def record_error(self, count: int = 1) -> None:
        """Retain inventory failures for the incomplete warning."""
        self.errors += count

    def admit_entry(self) -> bool:
        if self.entries >= _STORAGE_ENTRY_LIMIT:
            self.entry_limited = True
            return False
        if time.monotonic() >= self.deadline:
            self.time_limited = True
            return False
        self.entries += 1
        return True

    @contextmanager
    def directory(self, root: Path) -> Iterator[int]:
        """Bind metadata reads to a directory, never following a path component."""
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise OSError("no-follow directory inventory is unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        absolute = root.absolute()
        descriptor = os.open(absolute.anchor, flags)
        try:
            for component in absolute.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if root in self.directories and self.directories[root] != identity:
                raise OSError("directory changed during inventory")
            self.directories[root] = identity
            yield descriptor
        finally:
            os.close(descriptor)

    def children(self, root: Path) -> list[tuple[Path, os.stat_result]]:
        children: list[tuple[Path, os.stat_result]] = []
        try:
            with self.directory(root) as descriptor, os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if not self.admit_entry():
                        break
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        self.errors += 1
                        continue
                    path = root / entry.name
                    if stat.S_ISDIR(metadata.st_mode):
                        self.directories[path] = (metadata.st_dev, metadata.st_ino)
                    children.append((path, metadata))
        except FileNotFoundError:
            self.errors += int(root in self.directories)
        except OSError:
            self.errors += 1
        return children

    def tree_bytes(self, root: Path) -> int:
        try:
            with self.directory(root) as descriptor:
                root_metadata = os.fstat(descriptor)
        except FileNotFoundError:
            self.record_error(int(root in self.directories))
            return 0
        except OSError:
            self.record_error()
            return 0
        root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        if root_identity in self.tree_totals:
            return self.tree_totals[root_identity]

        total = 0
        pending = [root]
        while pending:
            for path, metadata in self.children(pending.pop()):
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                else:
                    total += self.file_bytes(metadata)
            if self.entry_limited or self.time_limited:
                break
        self.tree_totals[root_identity] = total
        return total

    def file_bytes(self, metadata: os.stat_result) -> int:
        if not stat.S_ISREG(metadata.st_mode):
            return 0
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in self.seen_files:
            return 0
        self.seen_files.add(identity)
        return metadata.st_size

    def reset_subtotals(self) -> None:
        """Start an independent logical view without resetting traversal limits."""
        self.seen_files.clear()
        self.tree_totals.clear()


def _same_user(metadata: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return getuid is None or metadata.st_uid == getuid()


def _resolved_temp_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    # Read-only metadata inventory; this root never allocates temporary files.
    for candidate in (work_dirs.os_temp_dir(), Path("/tmp")):  # nosec B108
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _measured_work_root(
    given: Path | None, temp_roots: tuple[Path, ...] | None
) -> Path | None:
    """The work root the storage inventory measures; never created by it.

    The machine's own inventory (no roots named) measures the configured work
    root. A caller that names its roots names the work root too, or measures
    none.
    """
    if given is not None or temp_roots is not None:
        return given
    return work_dirs.configured_root()


def _work_root_bytes(inventory: _StorageInventory, work_root: Path | None) -> int:
    return 0 if work_root is None else inventory.tree_bytes(work_root)


def _work_root_line(work_root: Path | None, work_bytes: int) -> str:
    if work_root is None:
        return "work root: not measured"
    return f"work root ({work_root}): {work_bytes} apparent bytes"


def _supported_uv_cache_dir() -> Path:
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache).expanduser() / "uv"
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "uv" / "cache"
    return Path.home() / ".cache" / "uv"


def _storage_directories(
    roots: tuple[Path, ...], inventory: _StorageInventory
) -> tuple[list[Path], list[Path]]:
    pytest_roots: list[Path] = []
    task_storage: list[Path] = []
    seen_roots: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        for task in _owned_storage_directories(resolved, inventory):
            if task.name.startswith("pytest-of-"):
                pytest_roots.append(task)
            elif task.name.startswith("kew-"):
                task_storage.extend(
                    child
                    for child in _owned_storage_directories(task, inventory)
                    if child.name in _TASK_STORAGE_NAMES
                )
    return pytest_roots, task_storage


def _owned_storage_directories(
    root: Path, inventory: _StorageInventory
) -> Iterator[Path]:
    for path, metadata in inventory.children(root):
        if stat.S_ISDIR(metadata.st_mode) and _same_user(metadata):
            yield path


def _directory_word(count: int) -> str:
    return "directory" if count == 1 else "directories"


def _incomplete_detail(inventory: _StorageInventory) -> str:
    reasons: list[str] = []
    if inventory.errors:
        reasons.append(f"{inventory.errors} filesystem error(s)")
    if inventory.entry_limited:
        reasons.append(f"the {_STORAGE_ENTRY_LIMIT}-entry limit was reached")
    if inventory.time_limited:
        reasons.append(f"the {_STORAGE_SCAN_SECONDS:g}-second limit was reached")
    return ", ".join(reasons)


def _check_temporary_storage(
    *,
    temp_roots: tuple[Path, ...] | None = None,
    explicit_uv_cache: Path | None | object = _ENV_CACHE_UNSET,
    default_uv_cache: Path | None = None,
    work_root: Path | None = None,
) -> str | None:
    """Report bounded logical sizes without deleting or reading file contents.

    The work root (:func:`kew.work_dirs.configured_root`) is measured beside
    the temp roots, in the same pass, so a file under both counts once
    (:func:`_measured_work_root`).
    """
    work_root = _measured_work_root(work_root, temp_roots)
    roots = temp_roots if temp_roots is not None else _resolved_temp_roots()
    if explicit_uv_cache is _ENV_CACHE_UNSET:
        cache_setting = os.environ.get("UV_CACHE_DIR")
        explicit_uv_cache = Path(cache_setting).expanduser() if cache_setting else None
    assert explicit_uv_cache is None or isinstance(explicit_uv_cache, Path)
    if default_uv_cache is None:
        default_uv_cache = _supported_uv_cache_dir()

    inventory = _StorageInventory()
    pytest_roots, task_storage = _storage_directories(roots, inventory)
    pytest_bytes = sum(inventory.tree_bytes(path) for path in pytest_roots)
    task_bytes = sum(inventory.tree_bytes(path) for path in task_storage)
    work_bytes = _work_root_bytes(inventory, work_root)
    inventory.reset_subtotals()
    explicit_bytes = (
        inventory.tree_bytes(explicit_uv_cache) if explicit_uv_cache is not None else 0
    )
    inventory.reset_subtotals()
    default_bytes = inventory.tree_bytes(default_uv_cache)

    if explicit_uv_cache is None:
        explicit_line = "uv-cache: explicit UV_CACHE_DIR: not set"
    else:
        explicit_line = (
            f"uv-cache: explicit UV_CACHE_DIR ({explicit_uv_cache}): "
            f"{explicit_bytes} apparent bytes"
        )
    details = (
        f"pytest: {len(pytest_roots)} {_directory_word(len(pytest_roots))}, "
        f"{pytest_bytes} apparent bytes",
        "task caches/environments: "
        f"{len(task_storage)} {_directory_word(len(task_storage))}, "
        f"{task_bytes} apparent bytes",
        _work_root_line(work_root, work_bytes),
        explicit_line,
        f"uv-cache: uv default ({default_uv_cache}): {default_bytes} apparent bytes",
    )
    return _storage_inventory_verdict(
        inventory, pytest_bytes + task_bytes + work_bytes, details
    )


def _storage_inventory_verdict(
    inventory: _StorageInventory, temporary_bytes: int, details: tuple[str, ...]
) -> str | None:
    # An incomplete inventory is advisory: a large or unreadable shared temp
    # directory says nothing about the install, so it never fails doctor
    # (#2002). Only a measured total over the threshold is an issue; an
    # incomplete one under it is a skip — never a pass.
    if inventory.incomplete:
        incomplete = (
            "advisory; inventory is an incomplete lower bound: "
            f"{_incomplete_detail(inventory)}"
        )
        details = (*details, incomplete)
    if temporary_bytes > _TEMP_STORAGE_WARNING_BYTES:
        _record(
            "temporary-storage",
            _WARN,
            f"{temporary_bytes} apparent bytes exceeds the 2 GiB threshold",
            details=details,
        )
        return "temporary storage exceeds 2 GiB"
    if inventory.incomplete:
        _record("temporary-storage", _SKIP, incomplete, details=details[:-1])
        return None
    _record(
        "temporary-storage",
        _OK,
        "observed temporary storage is at most 2 GiB",
        details=details,
    )
    return None


def _runtime_hint(runtime: str) -> str:
    """Describe how to restore the configured runtime."""
    command = Path(runtime).name
    return (
        f"install {command!r}, or change [sandbox] runtime to an installed "
        "container runtime; alternatively, set [sandbox] enabled = false plus "
        "dangerously_skip_containment = true to run on the host"
    )


_NETWORK_CAVEAT = (
    "kew installs no firewall rules on it, so a plain bridge gives the "
    "container full egress until you restrict it yourself"
)


#: Creates the allowlist network on whichever runtime ``[sandbox]`` configures,
#: with exactly the settings this check verifies (#2501).
_NETWORK_FIX = "kew sandbox network"


def _network_hint() -> str:
    """Describe how to create the allowlist network on the configured runtime."""
    return f"run '{_NETWORK_FIX}'; {_NETWORK_CAVEAT}"


def _report_non_container_route(
    decision: SandboxDecision, runner_name: str
) -> list[str]:
    """Report every effective route that does not need container checks."""
    if decision.posture != POSTURE_UNCONTAINED:
        for check in _CONTAINER_CHECKS:
            _record(
                check,
                _OK,
                f"not needed on this route; {runner_name!r} declares "
                f"{decision.posture} containment",
            )
        return []

    marker = "Reason containment was unavailable: "
    notice = decision.notice or ""
    reason = notice.rpartition(marker)[2] or "effective route is uncontained"
    issues: list[str] = []
    for check in _CONTAINER_CHECKS:
        _record(
            check,
            _WARN,
            f"host path admitted by dangerously_skip_containment: {reason}",
        )
        issues.append(f"{check} uses the admitted host path: {reason}")
    return issues


def _runtime_failure(reason: str, runtime: str) -> tuple[None, str]:
    _record("container-runtime", _FAIL, reason, hint=_runtime_hint(runtime))
    return None, reason


def _check_container_runtime(config: SandboxConfig) -> tuple[str | None, str | None]:
    """Verify the configured runtime exists and its daemon responds."""
    runtime = detect_runtime(config)
    if runtime is None:
        reason = f"configured runtime {config.runtime!r} is not on PATH"
        return _runtime_failure(reason, config.runtime)

    try:
        result = subprocess_utils.run([config.runtime, "info"], check=False, timeout=5)
    except subprocess.TimeoutExpired:
        return _runtime_failure(
            f"configured runtime {config.runtime!r} did not answer within five seconds",
            config.runtime,
        )
    except (OSError, ValueError) as exc:
        return _runtime_failure(
            f"configured runtime {config.runtime!r} could not start: {exc}",
            config.runtime,
        )
    if result.returncode != 0:
        return _runtime_failure(
            f"configured runtime {config.runtime!r} did not answer successfully "
            f"(exit {result.returncode})",
            config.runtime,
        )
    _record("container-runtime", _OK, f"{config.runtime!r} answered `info`")
    return config.runtime, None


def _image_fix(runtime: str, image: str, runner_name: str) -> str | None:
    """The build command for the stock image; kew cannot build a custom one."""
    if runner_name != DEFAULT_IMAGE_RUNNER or image != STOCK_AGENT_IMAGE:
        return None
    command = Path(runtime).name
    return shlex.join(
        [command, "build", "-f", "docker/agent.Dockerfile", "-t", image, "."]
    )


def _image_hint(runtime: str, image: str, runner_name: str) -> str:
    fix = _image_fix(runtime, image, runner_name)
    if fix is None:
        return f"build or pull {image!r} into the local runtime; kew never pulls it"
    return f"from a kew checkout, run '{fix}'"


def _route_image_failure(
    summary: str, runtime: str, image: str, runner_name: str
) -> None:
    _record(
        "route-image",
        _FAIL,
        summary,
        hint=_image_hint(runtime, image, runner_name),
        fix=_image_fix(runtime, image, runner_name),
    )


def _check_route_image(
    config: SandboxConfig,
    runner_name: str,
    image: str,
    runtime: str | None,
) -> str | None:
    """Verify the default route's effective image exists locally."""
    if runtime is None:
        _route_image_failure(
            f"cannot inspect {image!r}; runtime unavailable",
            config.runtime,
            image,
            runner_name,
        )
        return f"route image {image!r} was not inspected"
    try:
        result = subprocess_utils.run(
            [runtime, "image", "inspect", image], check=False, timeout=5
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _route_image_failure(
            f"could not inspect {image!r}: {exc}", runtime, image, runner_name
        )
        return f"route image {image!r} was not inspected"
    if result.returncode != 0:
        _route_image_failure(
            f"{image!r} is not present locally", runtime, image, runner_name
        )
        return f"route image {image!r} is missing"
    _record("route-image", _OK, f"{image!r} is present locally; no pull attempted")
    return None


def _check_allowlist_network(config: SandboxConfig, runtime: str | None) -> str | None:
    """Verify the allowlist posture and its local attachment network."""
    network_hint = _network_hint()
    if config.network != "allowlist":
        _record(
            "allowlist-network",
            _FAIL,
            f"[sandbox] network is {config.network!r}, not 'allowlist'",
            hint=f'set [sandbox] network = "allowlist", then {network_hint}',
        )
        return "sandbox network is not allowlist"
    if runtime is None:
        _record(
            "allowlist-network",
            _FAIL,
            f"cannot inspect {ALLOWLIST_NETWORK!r}; runtime unavailable",
            hint=f"restore the container runtime, then {network_hint}",
        )
        return f"network {ALLOWLIST_NETWORK!r} was not inspected"
    try:
        result = subprocess_utils.run(
            [runtime, "network", "inspect", ALLOWLIST_NETWORK],
            check=False,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _record(
            "allowlist-network",
            _FAIL,
            f"could not inspect {ALLOWLIST_NETWORK!r}: {exc}",
            hint=network_hint,
        )
        return f"network {ALLOWLIST_NETWORK!r} was not inspected"
    if result.returncode != 0:
        _record(
            "allowlist-network",
            _FAIL,
            f"{ALLOWLIST_NETWORK!r} is not present",
            hint=_NETWORK_CAVEAT,
            fix=_NETWORK_FIX,
        )
        return f"network {ALLOWLIST_NETWORK!r} is missing"
    _record(
        "allowlist-network",
        _OK,
        f"{ALLOWLIST_NETWORK!r} is present",
        warnings=_open_attachment_bridge_note(result.stdout),
    )
    return None


def _open_attachment_bridge_note(inspected: str) -> tuple[str, ...]:
    """Say plainly that a present network is not, by itself, a boundary."""
    driver, internal = network_attributes(inspected)
    if driver != "bridge" or internal is not False:
        return ()
    return (
        f"{ALLOWLIST_NETWORK} is a plain bridge: egress is open unless "
        "the host filters it, and kew cannot see host filters.",
    )


@contextmanager
def _open_certification_ledger(
    config: DispatchConfig, check_id: str
) -> Iterator[RunStore | None]:
    """Open the ledger the OpenShell egress gate reads its proof from.

    ``kew doctor`` only DESCRIBES the boundary, but it describes it by
    resolving it, and the resolution reads the certification off a ledger. A
    storeless resolution reported every OpenShell boundary as
    ``openshell-egress-uncertified``, hinted that "no ledger is available" and
    prescribed a ``kew sandbox certify`` that had already run — with the fresh
    passing proof sitting on this project's own ledger (#2389 review).

    Opened the way every other read-only doctor check opens it: raw, so a
    diagnostic reports on whatever ledger is there without migrating it or
    refusing over identity, and an absent or unreadable one yields ``None``.
    ``None`` still fails the gate closed, which is the honest answer when there
    is no ledger to read the proof from.
    """
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        yield None
        return
    try:
        store = SqliteRunStore(str(db_path), read_only=True)
    except (SchemaTooNewError, sqlite3.DatabaseError) as exc:
        # Said out loud rather than defaulted to None in silence: the refusal
        # that follows would otherwise blame a missing certification for a
        # ledger kew could not open.
        _record_advisory(check_id, f"ledger unreadable ({exc}); no egress proof read")
        yield None
        return
    try:
        yield store
    finally:
        store.close()


def _resolve_doctor_sandbox(
    config: SandboxConfig,
    runner_name: str,
    *,
    direct: bool = True,
    store: RunStore | None = None,
) -> tuple[SandboxDecision | None, SandboxRuntimeUnavailable | None]:
    """Resolve dispatch posture while retaining fail-closed diagnostics.

    ``direct`` carries the configured transport mode, because dispatch resolves
    with it: on the OpenShell runtime a switchyard route needs a provider on
    the gateway and a direct OpenRouter route may mint its own, so a doctor
    that assumed "direct" would report a readiness the dispatch then refuses.

    ``store`` is the ledger the OpenShell egress gate reads its certification
    from (:func:`_open_certification_ledger`).
    """
    try:
        return resolve_effective_sandbox(
            config, runner=runner_name, store=store, direct=direct
        ), None
    except SandboxRuntimeUnavailable as exc:
        return None, exc


def _report_sandbox_resolution_error(error: SandboxRuntimeUnavailable) -> str:
    """Report the same fail-closed refusal that dispatch would enforce."""
    _record("containment", _FAIL, error.reason, hint=str(error))
    return f"containment refused: {error.reason}"


def _resolve_readiness_image(
    config: SandboxConfig,
    runner_name: str,
    decision: SandboxDecision | None,
    resolution_error: SandboxRuntimeUnavailable | None,
) -> tuple[str | None, str | None]:
    """Resolve one image for both inspection and staleness checks."""
    if decision is not None and decision.image is not None:
        return decision.image, None
    if resolution_error is not None and resolution_error.image is not None:
        return resolution_error.image, None
    try:
        return resolve_runner_image(config, runner_name), None
    except SandboxImageUnmapped as exc:
        _record(
            "route-image",
            _FAIL,
            str(exc),
            hint=f"configure [sandbox.images].{runner_name} with a local image",
        )
        return None, f"default route {runner_name!r} has no image"


def _present_issues(*issues: str | None) -> list[str]:
    """Return only concrete diagnostic issues."""
    return [issue for issue in issues if issue is not None]


def _check_containment_readiness(
    config: DispatchConfig,
) -> tuple[list[str], str | None, bool, str]:
    """Run every check needed before the default route can be contained."""
    route = resolve_runner(config)
    with _open_certification_ledger(config, "containment") as ledger:
        decision, resolution_error = _resolve_doctor_sandbox(
            config.sandbox,
            route.runner.name,
            direct=config.inference_transport.mode == "direct",
            store=ledger,
        )
    if decision is not None:
        _record(
            "containment",
            _OK,
            f"the default route {route.runner.name!r} resolves to "
            f"{decision.posture} containment, as dispatch would resolve it",
        )

    if decision is not None and not decision.sandboxed:
        return (
            _report_non_container_route(decision, route.runner.name),
            None,
            False,
            route.runner.name,
        )

    runtime, runtime_issue = _check_container_runtime(config.sandbox)
    image, image_resolution_issue = _resolve_readiness_image(
        config.sandbox, route.runner.name, decision, resolution_error
    )
    resolution_issue = (
        _report_sandbox_resolution_error(resolution_error)
        if resolution_error is not None
        else None
    )
    issues = _present_issues(resolution_issue, runtime_issue, image_resolution_issue)
    if image is not None:
        image_issue = _check_route_image(
            config.sandbox, route.runner.name, image, runtime
        )
        issues.extend(_present_issues(image_issue))
    network_issue = _check_allowlist_network(config.sandbox, runtime)
    issues.extend(_present_issues(network_issue))
    return issues, image, True, route.runner.name


def _check_agent_image(
    sandbox_cfg: SandboxConfig,
    image: str | None,
    container_route: bool,
    runner_name: str,
) -> str | None:
    if not container_route:
        _record("agent-image", _SKIP, "not needed on this route")
        return None
    if image is None:
        _record("agent-image", _SKIP, "route image unresolved")
        return None
    runtime = detect_runtime(sandbox_cfg)
    if runtime is None:
        _record("agent-image", _SKIP, f"runtime {sandbox_cfg.runtime!r} not found")
        return None
    result = evaluate_agent_image_staleness(
        sandbox_cfg.runtime, image, runner=runner_name
    )
    if result.status == "stale":
        _record("agent-image", _WARN, str(result.warning))
        return "agent-image stale"
    if result.status == "fresh":
        _record("agent-image", _OK, f"{image!r} is up to date")
    else:  # unknown — could not run; never reported as a pass
        _record("agent-image", _SKIP, str(result.detail))
    return None


def _runtime_inventory_output(runtime: str, arguments: list[str], subject: str) -> str:
    """Run one bounded runtime read and require a successful result."""
    command = [runtime, *arguments]
    try:
        result = subprocess_utils.run(command, check=False, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise _RetainedTagInventoryError(f"{subject} timed out") from exc
    except (OSError, ValueError) as exc:
        raise _RetainedTagInventoryError(f"{subject} could not run") from exc
    if result.returncode != 0:
        raise _RetainedTagInventoryError(
            f"{subject} failed with exit {result.returncode}"
        )
    return result.stdout


def _json_string_fields(line: str, count: int, subject: str) -> tuple[str, ...]:
    fields = line.split("\t")
    if len(fields) != count:
        raise _RetainedTagInventoryError(f"{subject} returned a malformed record")
    try:
        values = tuple(json.loads(field) for field in fields)
    except json.JSONDecodeError as exc:
        raise _RetainedTagInventoryError(
            f"{subject} returned a malformed record"
        ) from exc
    if not all(type(value) is str for value in values):
        raise _RetainedTagInventoryError(f"{subject} returned a malformed record")
    return values


def _normalized_image_identity(value: str) -> str | None:
    match = _IMAGE_ID_RE.fullmatch(value)
    if match is None:
        return None
    return f"sha256:{match.group(1).lower()}"


def _canonical_agent_repository(runtime: str, repository: str) -> str | None:
    if repository in _RETAINED_AGENT_REPOSITORIES:
        return repository
    if Path(runtime).name == "podman":
        return _PODMAN_AGENT_REPOSITORIES.get(repository)
    return None


def _parse_retained_agent_tag(
    line: str, repository: str, runtime: str
) -> _RetainedAgentTag | None:
    subject = f"{repository} image inventory"
    observed_repository, tag, raw_identity, size = _json_string_fields(line, 4, subject)
    if _canonical_agent_repository(runtime, observed_repository) != repository:
        return None
    if tag == "<none>":
        return None
    identity = _normalized_image_identity(raw_identity)
    if (
        _IMAGE_TAG_RE.fullmatch(tag) is None
        or identity is None
        or _IMAGE_SIZE_RE.fullmatch(size) is None
    ):
        raise _RetainedTagInventoryError(f"{subject} returned a malformed record")
    return _RetainedAgentTag(observed_repository, tag, identity, size)


def _store_retained_agent_tag(
    records: dict[str, _RetainedAgentTag],
    sizes: dict[str, str],
    record: _RetainedAgentTag,
    repository: str,
) -> None:
    references = {record.reference, f"{repository}:{record.tag}"}
    for reference in references:
        previous = records.get(reference)
        if previous is not None and previous != record:
            raise _RetainedTagInventoryError(
                f"{repository} image inventory returned conflicting records"
            )
    previous_size = sizes.get(record.identity)
    if previous_size is not None and previous_size != record.logical_size:
        raise _RetainedTagInventoryError(
            f"{repository} image inventory returned conflicting sizes"
        )
    for reference in references:
        records[reference] = record
    sizes[record.identity] = record.logical_size


def _read_retained_agent_tags(runtime: str) -> dict[str, _RetainedAgentTag]:
    records: dict[str, _RetainedAgentTag] = {}
    sizes: dict[str, str] = {}
    for repository in _RETAINED_AGENT_REPOSITORIES:
        output = _runtime_inventory_output(
            runtime,
            [
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"reference={repository}",
                "--format",
                _IMAGE_LIST_FORMAT,
            ],
            f"{repository} image inventory",
        )
        for line in output.splitlines():
            if not line.strip():
                continue
            record = _parse_retained_agent_tag(line, repository, runtime)
            if record is None:
                continue
            _store_retained_agent_tag(records, sizes, record, repository)
    return records


def _read_container_image_references(runtime: str) -> set[str]:
    podman = Path(runtime).name == "podman"
    image_format = _PODMAN_CONTAINER_IMAGE_FORMAT if podman else _CONTAINER_IMAGE_FORMAT
    output = _runtime_inventory_output(
        runtime,
        [
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            image_format,
        ],
        "container image-reference inventory",
    )
    references: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        (reference,) = _json_string_fields(
            line, 1, "container image-reference inventory"
        )
        if podman:
            identity = _normalized_image_identity(reference)
            if identity is None:
                raise _RetainedTagInventoryError(
                    "container image-reference inventory returned a malformed record"
                )
            references.add(identity)
            continue
        if not reference or any(character.isspace() for character in reference):
            raise _RetainedTagInventoryError(
                "container image-reference inventory returned a malformed record"
            )
        references.add(reference)
    return references


def _current_packaged_agent_pins() -> tuple[str, ...]:
    """Compute every packaged runner's pin from the inputs shipped with this
    install, so none of them is reported stale right after a build."""
    context = packaged_agent_image_context()
    return tuple(
        _expected_packaged_image_pin(context, runner)
        for runner in sorted(_PACKAGED_IMAGE_TAGS)
    )


def _exact_agent_tag(runtime: str, reference: str) -> str | None:
    repository, separator, tag = reference.rpartition(":")
    if not separator:
        repository = reference
        tag = "latest"
    canonical_repository = _canonical_agent_repository(runtime, repository)
    if canonical_repository is not None and _IMAGE_TAG_RE.fullmatch(tag):
        return f"{canonical_repository}:{tag}"
    return None


def _resolve_protected_identity(runtime: str, reference: str) -> str:
    output = _runtime_inventory_output(
        runtime,
        ["image", "inspect", "--format", _IMAGE_ID_FORMAT, "--", reference],
        "protected image-reference resolution",
    )
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise _RetainedTagInventoryError(
            "protected image-reference resolution returned an incomplete record"
        )
    (raw_identity,) = _json_string_fields(
        lines[0], 1, "protected image-reference resolution"
    )
    identity = _normalized_image_identity(raw_identity)
    if identity is None:
        raise _RetainedTagInventoryError(
            "protected image-reference resolution returned a malformed identity"
        )
    return identity


def _protected_agent_image_identities(
    runtime: str,
    config: SandboxConfig,
    records: dict[str, _RetainedAgentTag],
    container_references: set[str],
) -> set[str]:
    try:
        packaged_pins = _current_packaged_agent_pins()
    except (OSError, ValueError, KeyError) as exc:
        raise _RetainedTagInventoryError(
            "current packaged image pins could not be computed"
        ) from exc
    references = {
        *_PACKAGED_IMAGE_TAGS.values(),
        config.image,
        *config.images.values(),
        *packaged_pins,
        *container_references,
    }
    protected: set[str] = set()
    for reference in sorted(references):
        identity = _normalized_image_identity(reference)
        if identity is not None:
            protected.add(identity)
            continue
        exact_tag = _exact_agent_tag(runtime, reference)
        if exact_tag is not None:
            record = records.get(exact_tag)
            if record is not None:
                protected.add(record.identity)
            continue
        protected.add(_resolve_protected_identity(runtime, reference))
    return protected


def _counted(noun: str, count: int, *, plural: str | None = None) -> str:
    word = noun if count == 1 else (plural or f"{noun}s")
    return f"{count} {word}"


def _report_stale_agent_tags(records: list[_RetainedAgentTag]) -> str:
    grouped: dict[str, list[_RetainedAgentTag]] = {}
    for record in records:
        grouped.setdefault(record.identity, []).append(record)
    tag_count = len(records)
    image_count = len(grouped)
    tag_summary = _counted("stale tag", tag_count)
    image_summary = _counted("image identity", image_count, plural="image identities")
    details = [
        f"{', '.join(sorted(record.reference for record in aliases))} -> "
        f"{identity} (logical size {aliases[0].logical_size})"
        for identity, aliases in sorted(grouped.items())
    ]
    details.append(
        "logical image sizes can share layers; they are not physical storage estimates"
    )
    _record(
        "retained-agent-tags",
        _WARN,
        f"{tag_summary} across {image_summary}",
        details=tuple(details),
    )
    return f"{_counted('stale retained agent tag', tag_count)} across {image_summary}"


def _retained_agent_tag_unknown(reason: str) -> str:
    _record("retained-agent-tags", _WARN, f"inventory unknown: {reason}")
    return "retained agent tag inventory unknown"


def _stale_retained_agent_tags(config: SandboxConfig) -> list[_RetainedAgentTag]:
    """The agent tags no pin and no container protects, read without changing
    runtime state; ``kew gc`` removes the same set. Raises
    :class:`_RetainedTagInventoryError` when the inventory cannot be read."""
    records = _read_retained_agent_tags(config.runtime)
    container_references = _read_container_image_references(config.runtime)
    protected = _protected_agent_image_identities(
        config.runtime, config, records, container_references
    )
    return sorted(
        {record for record in records.values() if record.identity not in protected},
        key=lambda record: record.reference,
    )


def _check_retained_agent_tags(
    config: SandboxConfig, container_route: bool
) -> str | None:
    """Warn about unprotected tags without changing container runtime state."""
    runtime = detect_runtime(config)
    if runtime is None:
        if not container_route:
            _record(
                "retained-agent-tags",
                _SKIP,
                f"runtime {config.runtime!r} not found; not needed on this route",
            )
            return None
        return _retained_agent_tag_unknown(
            f"configured runtime {config.runtime!r} is unavailable"
        )
    try:
        stale = _stale_retained_agent_tags(config)
    except _RetainedTagInventoryError as exc:
        return _retained_agent_tag_unknown(str(exc))
    if stale:
        return _report_stale_agent_tags(stale)
    _record("retained-agent-tags", _OK, "no stale tags found")
    return None


def _check_pinned_reservations(db_path: Path) -> str | None:
    if not db_path.is_file():
        _record("reservations", _SKIP, "no dispatch database found")
        return None
    from kew.graph.driver.tick import _RESERVATION_STARTUP_GRACE_SECONDS
    from kew.run_store import SqliteRunStore

    try:
        # Open read-only: doctor is a diagnostics command and must not migrate
        # the schema (which would hard-block a live older kew) as a side effect
        # (#972 review Finding 3).
        with SqliteRunStore(str(db_path), read_only=True) as store:
            now = datetime.now(UTC)
            pinned = _find_pinned_reservations(
                store, now, _RESERVATION_STARTUP_GRACE_SECONDS
            )
    except SchemaTooNewError as exc:
        _record("reservations", _WARN, str(exc))
        return "dispatch.db schema too new for this kew"
    except sqlite3.DatabaseError as exc:
        # An older-schema DB (pre-v25, before epic_reservations/item_id existed),
        # an empty/foreign DB ("no such table"), or a corrupt file ("file is not
        # a database") reaches list_epic_reservations as a sqlite3 error, not a
        # SchemaTooNewError (which only rejects NEWER schemas). Skip the check
        # with the DB error rather than crashing `kew doctor` — the one command
        # whose job is to report issues, not raise them (#972 review Finding 3
        # residual; CodeRabbit follow-up).
        _record("reservations", _SKIP, f"dispatch database unreadable: {exc}")
        return None
    if pinned:
        _record(
            "reservations",
            _WARN,
            f"{len(pinned)} pinned — " + ", ".join(pinned),
            hint=(
                "a reservation's run has ended but nothing released it — the "
                "watcher may be down (or just hasn't ticked yet). Run "
                "'kew epic run <epic>' to sweep, or restart 'kew delegate watch'."
            ),
        )
        return f"{len(pinned)} pinned reservation(s)"
    _record("reservations", _OK, "no pinned reservations")
    return None


def _find_pinned_reservations(
    store: RunStore, now: datetime, grace_seconds: int
) -> list[str]:
    """Sweep EVERY pseudo-epic (delegation + review + any future one) for
    stranded reservations, not just delegation (#979-F2). A ``review``
    reservation orphaned by a crashed reviewer is otherwise invisible to
    ``kew doctor``. Each pinned entry is labelled ``<pseudo-epic>:<item_id>`` so
    the report says which pool leaked."""
    from kew.pseudo_epics import PSEUDO_EPIC_REFS

    pinned: list[str] = []
    for epic_ref in PSEUDO_EPIC_REFS:
        for res in store.list_epic_reservations(epic_ref):
            if _reservation_is_pinned(store, res, now, grace_seconds):
                pinned.append(f"{epic_ref}:{res.item_id}")
    return pinned


def _reservation_is_pinned(
    store: RunStore, res: EpicReservation, now: datetime, grace_seconds: int
) -> bool:
    from kew.graph.driver.tick import resolve_reservation_run

    run = resolve_reservation_run(store, res)
    # A dead spawn that never wrote a run row, past the startup grace.
    past_startup_grace = run is None and (now - res.reserved_at) > timedelta(
        seconds=grace_seconds
    )
    # A terminated run whose reservation has sat unreleased past a short grace —
    # long enough that the watcher's next sweep should have caught it (Finding 4:
    # don't flag the healthy just-finished window).
    settled = run.finished_at or run.started_at if run is not None else None
    terminal_past_grace = (
        run is not None
        and run.status != "pending"
        and settled is not None
        and (now - settled) > timedelta(seconds=_PINNED_TERMINAL_GRACE_SECONDS)
    )
    return past_startup_grace or terminal_past_grace


def _check_shadow_db(canonical: Path) -> str | None:
    """Warn if a dispatch.db exists at CWD that is NOT the configured canonical path.

    A stray CWD-level file (e.g. ``./dispatch.db``) shadows the live store when
    a developer runs raw SQLite queries — they hit the empty shadow and conclude
    the feature is broken (#1037). This is a read-only diagnostic: the shadow is
    reported but never deleted.
    """
    shadow = Path.cwd() / "dispatch.db"
    if shadow != canonical and shadow.is_file():
        _record(
            "shadow-db",
            _WARN,
            f"found {shadow}; the canonical store is at {canonical}",
            hint=(
                "queries against the shadow return empty or stale data. "
                "Delete or move the shadow file to remove the ambiguity."
            ),
        )
        return f"shadow dispatch.db at {shadow}"
    _record("shadow-db", _OK, "no dispatch.db shadows the canonical store")
    return None


_LIVE_CONFIRM_HINT = (
    "run 'kew doctor route-live-confirm' to verify it with one bounded call"
)


def _check_route_live(db_path: Path, config: DispatchConfig) -> str | None:
    """Report STORED live validity for the complex route. Spends nothing.

    Every other doctor check is static: it reads config, manifests, images and
    binaries. None of them can tell you a turn actually completes across the
    boundary, so this check refuses to imply it — an unprobed route is
    ``unverified``, which is a skip, not a pass. Only a probe that ran and
    FAILED is an issue.
    """
    if not db_path.is_file():
        _record("route-live", _SKIP, "no dispatch database found")
        return None
    from kew.run_store import SqliteRunStore

    try:
        with SqliteRunStore(str(db_path), read_only=True) as store:
            route = resolve_probe_route(config, complexity="complex", store=store)
            status, probe = route_live_status(store, route.fingerprint)
    except (SchemaTooNewError, sqlite3.DatabaseError) as exc:
        _record("route-live", _SKIP, f"dispatch database unreadable: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - doctor must remain diagnostic
        _record("route-live", _SKIP, str(exc))
        return None
    label = f"{route.runner_name}/{route.model}"
    if status is LiveStatus.CONFIRMED:
        _record("route-live", _OK, f"{label} confirmed live at this exact boundary")
        return None
    if status is LiveStatus.REFUTED:
        detail = probe.detail if probe is not None else "a probe failed"
        _record("route-live", _FAIL, f"{label} FAILED its last live probe — {detail}")
        return f"{label} failed its last live probe"
    # The confirmation spends money, so it is a hint and never a `fix`: a
    # caller that runs every fix must not be able to trigger a charge.
    _record(
        "route-live",
        _SKIP,
        f"{label} is configured but live-unverified",
        hint=_LIVE_CONFIRM_HINT,
    )
    return None


def _review_route_for_event(
    event: AuditEvent, store: RunStore, config: DispatchConfig
) -> str:
    """Resolve the runner/model route attached to a verdict event."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    run = store.get(event.run_id) if event.run_id else None
    runner = (run.runner_name if run is not None else "") or config.review.runner
    model = (run.model if run is not None else "") or payload.get("reviewer_model", "")
    runner = runner or "unknown-runner"
    model = model or "unknown-model"
    return f"{runner}/{model}"


def _read_runner_quota_holds(db_path: Path) -> list[RunnerQuotaHold]:
    """Each runner's last recorded quota hold, active or not, by runner name."""
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(db_path), read_only=True) as store:
        return [
            hold
            for name in sorted(KNOWN_RUNNER_NAMES)
            if (hold := read_runner_quota_hold(store, name)) is not None
        ]


def _check_runner_quota(db_path: Path) -> str | None:
    """Report a runner lane the provider refused for quota (#2643). Free.

    Reads the rejection each runner's last refused run left in the store — the
    rate-limit event's limit and reset time — and makes no provider call. A
    lane still inside its hold is a ``fail``: every dispatch into it would be
    refused in seconds, which is exactly what readiness used to call healthy.
    """
    if not db_path.is_file():
        _record("runner-quota", _SKIP, "no dispatch database found")
        return None
    try:
        holds = _read_runner_quota_holds(db_path)
    except (SchemaTooNewError, sqlite3.DatabaseError) as exc:
        _record("runner-quota", _SKIP, f"dispatch database unreadable: {exc}")
        return None
    fingerprints = {
        hold.runner: runner_credential_fingerprint(hold.runner) for hold in holds
    }
    return _report_runner_quota(holds, datetime.now(UTC), fingerprints)


def _quota_hold_aftermath(
    hold: RunnerQuotaHold, now: datetime, fingerprint: str | None
) -> str:
    """One line for a hold that no longer applies: its refusal and its end."""
    line = hold.rejection.describe()
    if hold.cleared is not None:
        return f"{line} ({hold.cleared.describe()})"
    if hold.active(now) and hold.released_by_credential(fingerprint):
        return f"{line} (released: the credential in the environment changed)"
    return line


def _report_runner_quota(
    holds: list[RunnerQuotaHold],
    now: datetime,
    fingerprints: dict[str, str] | None = None,
) -> str | None:
    """Record the ``runner-quota`` verdict for each runner's last hold."""
    seen = fingerprints or {}
    active = [
        hold
        for hold in holds
        if hold.applies(now, credential_fingerprint=seen.get(hold.runner))
    ]
    if active:
        _record(
            "runner-quota",
            _FAIL,
            "; ".join(hold.reason() for hold in active),
            hint="the lane resumes on its own at the reset time, or at the next "
            "dispatch once a different credential is in the environment",
            fix="; ".join(f"kew quota clear {hold.runner}" for hold in active),
        )
        return ", ".join(f"{hold.runner} quota exhausted" for hold in active)
    if holds:
        _record(
            "runner-quota",
            _OK,
            "no runner lane held; last refusal: "
            + "; ".join(
                _quota_hold_aftermath(hold, now, seen.get(hold.runner))
                for hold in holds
            ),
        )
        return None
    _record("runner-quota", _OK, "no runner refused for quota")
    return None


def _check_degraded_review_lane(db_path: Path, config: DispatchConfig) -> str | None:
    """Warn when a reviewer route has produced only crash verdicts lately.

    Verdict events are the durable lane-health signal. Counts are maintained
    independently per route, so activity from a healthy route cannot mask a
    dark one. A real verdict resets that route's trailing count. Both crash
    verdicts count: DEGRADED (advisory) and UNPARSEABLE (blocking, #1393) are
    the same dark lane seen under two policies.
    """
    if not db_path.is_file():
        _record("degraded-review", _SKIP, "no dispatch database found")
        return None
    from kew.review_audit import (
        REVIEW_VERDICT_EVENT,
        VERDICT_DEGRADED,
        VERDICT_UNPARSEABLE,
    )
    from kew.run_store import SqliteRunStore

    crash_verdicts = {VERDICT_DEGRADED, VERDICT_UNPARSEABLE}
    try:
        with SqliteRunStore(str(db_path), read_only=True) as store:
            events = store.list_audit_events(event=REVIEW_VERDICT_EVENT, limit=None)
            trailing: dict[str, int] = {}
            for event in reversed(events):
                route = _review_route_for_event(event, store, config)
                payload = event.payload if isinstance(event.payload, dict) else {}
                if payload.get("verdict") in crash_verdicts:
                    trailing[route] = trailing.get(route, 0) + 1
                else:
                    trailing[route] = 0
    except (SchemaTooNewError, sqlite3.DatabaseError) as exc:
        _record("degraded-review", _SKIP, f"dispatch database unreadable: {exc}")
        return None

    dark = {
        route: count
        for route, count in trailing.items()
        if count >= _DEGRADED_REVIEW_THRESHOLD
    }
    if not dark:
        _record("degraded-review", _OK, "no consecutive degraded reviewer route")
        return None
    details = ", ".join(f"{route} ({count})" for route, count in dark.items())
    _record(
        "degraded-review",
        _WARN,
        f"dark reviewer route(s): {details}",
        hint="inspect launch error categories and sandbox credentials",
    )
    return f"{len(dark)} degraded reviewer route(s)"


def _check_model_lifecycle(config: DispatchConfig) -> str | None:
    """Warn about every configured route and pinned reviewer model.

    Lifecycle data is advisory in v1: a feed gap or an end-of-life status is
    visible to the operator, but never prevents a dispatch from proceeding.
    """
    models = [config.agent.model, config.review.model]
    skipped: list[str] = []
    for complexity in ("simple", "medium", "complex"):
        try:
            models.append(resolve_runner(config, complexity=complexity).model)
        except Exception as exc:  # noqa: BLE001 - doctor must remain diagnostic
            skipped.append(f"skipped route {complexity} ({exc})")
    lifecycle = getattr(config, "model_eol", None)
    entries = getattr(lifecycle, "snapshot_models", {}) if lifecycle else {}
    warnings = check_lifecycle(models, entries)
    details = (*skipped, *warnings)
    if warnings:
        summary = f"{len(warnings)} model lifecycle warning(s)"
        _record("model-eol", _WARN, summary, details=details)
        return summary
    _record(
        "model-eol",
        _OK,
        "configured models have current feed entries",
        details=details,
    )
    return None


def _read_anchors(path: Path) -> list[Anchor] | None:
    """Parse the anchors file, or report why it cannot be read.

    Every unreadable state is a *skipped* check, never a pass: doctor is a
    diagnostic, and a corrupt anchors file is already reported (as exit 1) by
    ``kew audit verify --anchors``.
    """
    if not path.is_file():
        _record("anchor-age", _SKIP, f"no anchors file at {path}")
        return None
    try:
        return parse_anchors(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _record("anchor-age", _SKIP, f"unreadable anchors file {path}: {exc}")
        return None


def _check_anchor_age(config: DispatchConfig) -> str | None:
    """Warn when the newest anchor is older than ``[audit] anchor_max_age_days``.

    The anchor workflow can die silently — it did, from 2026-07-30 (#1408) —
    and a chain nobody is anchoring still verifies perfectly. Chain integrity
    therefore cannot be the alarm; anchor *age* is.
    """
    threshold = config.audit.anchor_max_age_days
    if threshold <= 0:
        _record("anchor-age", _SKIP, "[audit] anchor_max_age_days = 0")
        return None
    anchors = _read_anchors(default_anchors_path())
    if anchors is None:
        return None
    age = newest_anchor_age(anchors)
    if age is None:
        _record("anchor-age", _SKIP, "no readable anchor timestamp")
        return None
    if age.days < 0:
        # A future stamp is clock skew or a forged one. Either way it is not
        # evidence that anchoring is alive, so it is skipped, never passed.
        _record(
            "anchor-age",
            _SKIP,
            f"newest anchor {age.newest.isoformat()} is in the future — "
            "check clock skew",
        )
        return None
    if age.days > threshold:
        _record(
            "anchor-age",
            _WARN,
            f"newest anchor is {age.days:.1f} day(s) old "
            f"(threshold {threshold}) — the audit chain has been unwitnessed "
            f"since {age.newest.isoformat()}",
            hint="check the audit-anchor workflow on the self-hosted runner, "
            "then re-run it",
            fix=shlex.join(["gh", "workflow", "run", "audit-anchor.yml"]),
        )
        return f"audit chain unanchored for {age.days:.1f} day(s)"
    _record("anchor-age", _OK, f"newest anchor is {age.days:.1f} day(s) old")
    return None


def _check_project_config() -> None:
    """Whether a config file is in force; ``kew serve`` runs on defaults without one."""
    path = resolved_config_path()
    if path is not None:
        _record("project-config", _OK, f"config in force: {path}")
        return
    _record(
        "project-config",
        _FAIL,
        "no kew.toml found; running on built-in defaults",
        hint="kew init writes this project's kew.toml from detection and flags",
        fix="kew init",
    )


def _check_editable_install() -> str | None:
    warning = check_editable_staleness()
    if warning is None:
        _record("editable-install", _OK, "current or not an editable install")
        return None
    _record("editable-install", _WARN, warning)
    return "editable install is behind origin/main"


def _check_runner_liveness(config: DispatchConfig) -> str | None:
    repo = config.project.repo.strip()
    if not repo:
        _record("runner-liveness", _SKIP, "no GitHub repository configured")
        return None
    result = check_runner_liveness(repo)
    if not result.configured:
        detail = result.error or "RUNNER_TARGET is not configured"
        _record("runner-liveness", _SKIP, detail)
        return None
    if result.error:
        _record("runner-liveness", _SKIP, result.error)
        return None
    if result.offline_runners:
        names = ", ".join(result.offline_runners)
        _record(
            "runner-liveness",
            _WARN,
            f"offline self-hosted runners: {names}",
            hint="check the runner host and restart its service (for example, "
            "launchctl kickstart) before relying on queued CI.",
        )
        return f"{len(result.offline_runners)} offline self-hosted runner(s)"
    _record("runner-liveness", _OK, "all registered self-hosted runners are online")
    return None


def _report_host_check(check: HostCheck) -> None:
    """Record one host check, with the fix command whenever there is one."""
    status = {FAIL: _FAIL, SKIP: _SKIP}.get(check.status, _OK)
    fix = (check.fix or None) if status is _FAIL else None
    _record(
        f"host-{check.name}", status, check.detail, hint=check.hint or None, fix=fix
    )


def _check_hosts(config: DispatchConfig) -> str | None:
    """Report the configured host providers and name every failing probe."""
    failed: list[str] = []
    for check in check_hosts(config):
        _report_host_check(check)
        if check.failed:
            failed.append(check.name)
    _skip_unreported(_GCP_HOST_IDS, "no gcp host configured ([hosts.gcp] instance)")
    if not failed:
        return None
    return f"host provider check(s) failed: {', '.join(failed)}"


#: Where an operator reads the whole setup, printed with every OpenShell
#: failure: the checks below name one fix each, and the runbook is the order
#: they have to happen in.
_OPENSHELL_RUNBOOK_HINT = "setup runbook: docs/openshell.md"


def _report_openshell_checks(checks: list[OpenShellCheck]) -> list[str]:
    """Print each verdict and return the names of the failures."""
    issues = []
    for check in checks:
        if check.passed:
            _record(check.name, _OK, check.detail)
            continue
        _record(check.name, _FAIL, check.detail, hint=_OPENSHELL_RUNBOOK_HINT)
        issues.append(check.name)
    return issues


def _openshell_gateway_facts(
    client: OpenShellClient, config: SandboxConfig
) -> tuple[str, GatewayReport, OpenShellCheck]:
    """Every read that talks to the gateway, so one failure reports once.

    Grouped deliberately: a timeout on the provider read says nothing about
    whether the provider exists, so it must surface as an unproven gateway
    rather than as a missing credential the operator would go create.
    """
    return (
        client.version(),
        client.report(),
        check_providers(client, providers=config.openshell.providers),
    )


#: The failures that mean no byte of a reply reached this host, so every
#: setting below is unproven because nothing was read — as distinct from a
#: gateway that answered with something kew cannot parse. Both collapse to the
#: one `openshell-gateway` issue, but they are different host problems and an
#: operator told the wrong one restarts the wrong thing.
_OPENSHELL_SILENT_GATEWAY = (
    OpenShellMissing,
    OpenShellGatewayUnreachable,
    OpenShellTimeout,
)


def _openshell_gateway_fault(exc: OpenShellError) -> str:
    """Why nothing about this gateway is proven, naming which of the two it is."""
    if isinstance(exc, _OPENSHELL_SILENT_GATEWAY):
        return f"the gateway did not answer: {exc}"
    return f"the gateway answered, but kew cannot read its report: {exc}"


def _check_openshell(config: DispatchConfig) -> list[str]:
    """Report the OpenShell control plane, or say why none of it is proven.

    Inert on every other runtime: ``[sandbox.openshell]`` is only read when
    ``[sandbox] runtime`` selects openshell, so checking it elsewhere would
    report on settings no dispatch consults.
    """
    sandbox_cfg = config.sandbox
    if sandbox_cfg.runtime != OPENSHELL_RUNTIME:
        _skip_unreported(
            _OPENSHELL_IDS,
            f"[sandbox] runtime is {sandbox_cfg.runtime!r}, not {OPENSHELL_RUNTIME!r}",
        )
        return []
    client = OpenShellClient.from_settings(sandbox_cfg.openshell)
    try:
        cli_version, report, providers = _openshell_gateway_facts(client, sandbox_cfg)
    except OpenShellError as exc:
        _record(
            "openshell-gateway",
            _FAIL,
            _openshell_gateway_fault(exc),
            hint=_OPENSHELL_RUNBOOK_HINT,
        )
        _skip_unreported(
            _OPENSHELL_DOWNSTREAM_IDS, "not probed: the openshell-gateway check failed"
        )
        return ["openshell gateway unproven"]
    endpoint = report.gateway.endpoint or f"the CLI default ({report.gateway.source})"
    _record("openshell-gateway", _OK, f"{endpoint} answered")
    runners = mapped_runners(sandbox_cfg)
    with _open_certification_ledger(config, CHECK_CERTIFICATION) as ledger:
        certification = check_certification(sandbox_cfg, runners=runners, store=ledger)
    return _report_openshell_checks(
        [
            check_versions(
                pinned=sandbox_cfg.openshell.version,
                cli=cli_version,
                gateway=report.version,
            ),
            check_tls(report),
            check_telemetry(report),
            check_image_pins(report),
            providers,
            check_agent_authority(report),
            check_landlock(report),
            certification,
        ]
    )


def _check_required_tools() -> str | None:
    """Every command-line tool kew shells out to resolves on PATH.

    Uses the silent half of ``preflight_tools``: it registers what is installed
    (later checks resolve their executables through that allowlist) and returns
    what is missing, without printing or exiting.
    """
    missing = register_tools()
    if missing:
        _record(
            "required-tools",
            _FAIL,
            f"not found on PATH: {', '.join(missing)}",
            hint="install each missing tool from the source its detail names",
            details=tuple(
                f"{tool}: install from {url}" for tool, url in missing.items()
            ),
        )
        return "required command-line tool is missing"
    _record("required-tools", _OK, "every required command-line tool resolved")
    return None


#: The routing tiers whose runners dispatch can launch; ``None`` is the default.
_ROUTE_COMPLEXITIES: tuple[str | None, ...] = (None, "simple", "medium", "complex")


def _configured_runners(config: DispatchConfig) -> tuple[str, ...]:
    """Every runner a configured route resolves to, default route first."""
    names = (resolve_runner(config, tier).runner.name for tier in _ROUTE_COMPLEXITIES)
    return tuple(dict.fromkeys(names))


def _runner_credential_exemption(config: SandboxConfig, runner: str) -> str | None:
    """Why ``runner`` needs no credential in this environment, or ``None``."""
    if runner_native_containment(runner, runtime=config.runtime) is not None:
        return (
            f"{runner}: native posture — it runs in its own sandbox on the host "
            "CLI login, so no container credential is needed"
        )
    if config.runtime == OPENSHELL_RUNTIME:
        return (
            f"{runner}: the OpenShell gateway's providers authenticate it "
            "(see openshell-providers), not the environment"
        )
    if config.enabled is False:
        return f"{runner}: containment is disabled, so it runs on the host CLI login"
    uncontainable = runner_uncontainable(runner)
    if uncontainable is not None:
        return f"{runner}: never contained ({uncontainable})"
    if not runner_credential_env_vars(runner):
        return f"{runner}: declares no environment credential"
    return None


def _credential_host_name(declared: tuple[str, ...]) -> str:
    """The variable to set on this host for a runner declaring ``declared``.

    A bounded OpenRouter key is minted per run from the provisioning key, so
    that is what the host holds; otherwise the runner's first declared name.
    """
    if "OPENROUTER_API_KEY" in declared:
        return "OPENROUTER_PROVISIONING_KEY"
    return declared[0]


def _report_host_fallback(
    missing: list[str], hints: list[str], details: tuple[str, ...]
) -> str:
    """Report missing credentials that dispatch answers with a host run.

    With ``dangerously_skip_containment`` set, dispatch resolves a contained
    runner whose credential is absent to the uncontained host path instead of
    refusing it (:func:`kew.sandbox._credential_missing_outcome`), so the gap
    costs containment, not the dispatch: a warning, not a failure.
    """
    _record(
        "runner-credential",
        _WARN,
        f"no {', '.join(missing)} credential in the environment — "
        "dangerously_skip_containment = true, so dispatch runs it uncontained "
        "on the host CLI login",
        hint="; ".join(hints),
        details=details,
    )
    return "runner credential is missing, so dispatch runs uncontained"


def _check_runner_credentials(config: DispatchConfig) -> str | None:
    """Each configured runner's container credential is in the environment.

    Containers cannot inherit a CLI login (#721), so a contained runner with
    none of its declared names set is refused at dispatch — unless
    ``dangerously_skip_containment`` is set, where dispatch runs it on the host
    CLI login instead (:func:`_report_host_fallback`). Presence only, and
    names only: no value is read past a truthiness check or ever reported.
    """
    present: list[str] = []
    missing: list[str] = []
    hints: list[str] = []
    exports: list[str] = []
    exempt: list[str] = []
    for runner in _configured_runners(config):
        reason = _runner_credential_exemption(config.sandbox, runner)
        if reason is not None:
            exempt.append(reason)
            continue
        declared = runner_credential_env_vars(runner)
        injected = _injected_credentials(declared)
        if injected:
            present.append(f"{runner} ({', '.join(injected)})")
            continue
        missing.append(runner)
        hints.append(f"{runner}: {credential_missing_message(runner, declared)}")
        exports.append(_credential_host_name(declared))
    details = tuple(exempt)
    if missing and config.sandbox.dangerously_skip_containment:
        return _report_host_fallback(missing, hints, details)
    if missing:
        _record(
            "runner-credential",
            _FAIL,
            f"no {', '.join(missing)} credential in the environment — containers "
            "cannot inherit CLI login, so dispatch would be refused",
            hint="; ".join(hints),
            fix="export " + " ".join(f"{n}=<key>" for n in dict.fromkeys(exports)),
            details=details,
        )
        return "runner credential is missing"
    if present:
        _record(
            "runner-credential",
            _OK,
            f"credential set for {', '.join(present)}",
            details=details,
        )
        return None
    _record("runner-credential", _SKIP, "; ".join(exempt))
    return None


_GH_AUTH_TIMEOUT_SECONDS = 15
#: ``gh auth status`` lines that mean no usable login (lower-cased).
_GH_LOGGED_OUT_MARKERS = ("not logged in", "failed to log in", "is invalid")
#: ``gh auth status`` lines that mean the login could not be checked at all.
_GH_UNREACHABLE_MARKERS = (
    "timeout",
    "error connecting",
    "dial tcp",
    "no such host",
    "could not resolve",
    "connection refused",
    "network is unreachable",
    "check your internet connection",
)


def _first_output_line(result: subprocess.CompletedProcess[str]) -> str:
    for line in f"{result.stderr or ''}\n{result.stdout or ''}".splitlines():
        if line.strip():
            return line.strip()
    return f"exit {result.returncode}"


def _gh_auth_status() -> subprocess.CompletedProcess[str]:
    return subprocess_utils.run(
        ["gh", "auth", "status"], check=False, timeout=_GH_AUTH_TIMEOUT_SECONDS
    )


def _report_github_unreachable(detail: str) -> str:
    _record(
        "gh-auth",
        _FAIL,
        "GitHub is unreachable: gh could not check its login",
        hint="check the network, any proxy, and https://www.githubstatus.com",
        details=(detail,),
    )
    return "GitHub is unreachable"


def _check_gh_auth() -> str | None:
    """``gh`` is logged in, told apart from a GitHub ``gh`` could not reach.

    A missing login and an unreachable host fail differently at dispatch
    (#1731), so they get different summaries here: only one is fixed by
    ``gh auth login``. ``gh`` masks the token it prints, and every result field
    is redacted besides, so no credential value reaches either rendering.
    """
    try:
        result = _gh_auth_status()
    except subprocess.TimeoutExpired:
        return _report_github_unreachable(
            f"gh auth status did not answer within {_GH_AUTH_TIMEOUT_SECONDS}s"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _record(
            "gh-auth",
            _SKIP,
            f"gh could not be run ({type(exc).__name__}); see required-tools",
        )
        return None
    if result.returncode == 0:
        _record("gh-auth", _OK, "gh is logged in to GitHub")
        return None
    output = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if any(marker in output for marker in _GH_LOGGED_OUT_MARKERS):
        _record(
            "gh-auth",
            _FAIL,
            "gh is not logged in to GitHub",
            hint="kew reads issues and publishes pull requests through gh",
            fix="gh auth login",
        )
        return "gh is not logged in"
    if any(marker in output for marker in _GH_UNREACHABLE_MARKERS):
        return _report_github_unreachable(_first_output_line(result))
    _record(
        "gh-auth",
        _FAIL,
        f"gh auth status failed: {_first_output_line(result)}",
        hint="run gh auth status to see the full answer",
    )
    return "gh auth status failed"


@dataclass
class _RouteFacts:
    """What the containment checks learned that later checks need."""

    image: str | None = None
    container_route: bool = False
    runner_name: str = ""
    #: Why the later checks cannot run, until containment completes.
    blocked: str | None = "not run: the containment checks did not complete"

    def resolve(self, config: DispatchConfig) -> None:
        _, self.image, self.container_route, self.runner_name = (
            _check_containment_readiness(config)
        )
        self.blocked = None


def _check_route_dependent(
    check_id: str, facts: _RouteFacts, check: Callable[[], object]
) -> None:
    if facts.blocked is not None:
        _record(check_id, _SKIP, facts.blocked)
        return
    check()


def _check_groups(
    config: DispatchConfig,
) -> list[tuple[tuple[str, ...], Callable[[], object]]]:
    """Each check, in report order, with every id it is responsible for."""
    facts = _RouteFacts()
    db_path = _dispatch_db_path(config)
    sandbox_cfg = config.sandbox
    return [
        (("project-config",), _check_project_config),
        (("required-tools",), _check_required_tools),
        (("runner-credential",), lambda: _check_runner_credentials(config)),
        (("gh-auth",), _check_gh_auth),
        (("containment", *_CONTAINER_CHECKS), lambda: facts.resolve(config)),
        (_OPENSHELL_IDS, lambda: _check_openshell(config)),
        (("model-eol",), lambda: _check_model_lifecycle(config)),
        (("anchor-age",), lambda: _check_anchor_age(config)),
        (("temporary-storage",), _check_temporary_storage),
        (("editable-install",), _check_editable_install),
        (("runner-liveness",), lambda: _check_runner_liveness(config)),
        (("host-local", *_GCP_HOST_IDS), lambda: _check_hosts(config)),
        (
            ("agent-image",),
            lambda: _check_route_dependent(
                "agent-image",
                facts,
                lambda: _check_agent_image(
                    sandbox_cfg, facts.image, facts.container_route, facts.runner_name
                ),
            ),
        ),
        (
            ("retained-agent-tags",),
            lambda: _check_route_dependent(
                "retained-agent-tags",
                facts,
                lambda: _check_retained_agent_tags(sandbox_cfg, facts.container_route),
            ),
        ),
        (("degraded-review",), lambda: _check_degraded_review_lane(db_path, config)),
        (("reservations",), lambda: _check_pinned_reservations(db_path)),
        (("shadow-db",), lambda: _check_shadow_db(db_path)),
        (("route-live",), lambda: _check_route_live(db_path, config)),
        (("runner-quota",), lambda: _check_runner_quota(db_path)),
    ]


def _run_guarded(
    recorder: _Recorder, check_ids: tuple[str, ...], check: Callable[[], object]
) -> None:
    """Run one check so that nothing it does can end the report.

    A check that raises is a ``fail`` for every id it had not yet reported,
    naming the exception; the next check still runs. Past the overall deadline
    a check is not started, and a probe the deadline cut short is a skip.
    """
    if time.monotonic() >= recorder.deadline:
        _skip_unreported(check_ids, _DEADLINE_REASON)
        return
    try:
        check()
    except Exception as exc:  # noqa: BLE001 - one broken check must not end the report
        if isinstance(exc, subprocess.TimeoutExpired) and (
            time.monotonic() >= recorder.deadline
        ):
            _skip_unreported(check_ids, _DEADLINE_REASON)
            return
        reason = f"the check itself failed: {type(exc).__name__}: {exc}"
        for check_id in check_ids:
            if check_id not in recorder.results:
                _record(check_id, _FAIL, reason)


def _dispatch_db_path(config: DispatchConfig) -> Path:
    return Path(config.logging.dir).resolve() / "dispatch.db"


def run_checks(config: DispatchConfig) -> list[CheckResult]:
    """Run every ``kew doctor`` check silently and return the results.

    The one entry point for anything that needs readiness — ``kew doctor``
    itself, ``kew init`` and the console — so none of them re-implements a
    check. Read-only and free: it never spends on a probe, never prints, and
    never creates a file (the readiness key included). It returns one result
    per id in ``CHECK_IDS``, in that order. Every subprocess it reaches is
    bounded, and all of them together by one overall deadline. It blocks while
    they run, so an async caller runs it off its event loop.

    It lives here rather than in :mod:`kew.readiness` because the dispatch
    stack imports that module and must never import the CLI.
    """
    recorder = _Recorder()
    token = _RECORDER.set(recorder)
    try:
        with subprocess_deadline(_RUN_CHECKS_DEADLINE_SECONDS):
            for check_ids, check in _check_groups(config):
                _run_guarded(recorder, check_ids, check)
    finally:
        _RECORDER.reset(token)
    return recorder.ordered()


#: The first check that reads the dispatch database; the report names the
#: database just before it, where it always has.
_FIRST_STORE_CHECK = "degraded-review"


def render_human(results: list[CheckResult], db_path: Path | None = None) -> None:
    """Print ``results`` as the prose report, ending in the issue summary."""
    for result in results:
        if db_path is not None and result.id == _FIRST_STORE_CHECK:
            _log(f"  dispatch.db: {db_path}")
        _render_check(result)
    issues = [result.id for result in results if result.is_issue]
    if issues:
        _warn(f"kew doctor: {len(issues)} issue(s) found: {', '.join(issues)}")
    else:
        _success("kew doctor: all checks passed")


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the check results as JSON on stdout instead of prose.",
        ),
    ] = False,
) -> None:
    """Run local environment diagnostics and report any known issues."""
    # Registered as a group callback (not a subcommand) so the command is
    # `kew doctor`, not `kew doctor doctor`. invoke_without_command lets it run
    # bare while leaving room to hang future `kew doctor <check>` subcommands.
    if ctx.invoked_subcommand is not None:
        return

    config = load_config()
    if not json_output:
        _log("kew doctor: checking local environment...")
    results = run_checks(config)
    report = readiness_report(results)
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        render_human(results, _dispatch_db_path(config))
    if not report["ok"]:
        raise typer.Exit(1)


@contextmanager
def _open_spend_ledger(config: DispatchConfig) -> Iterator[SqliteRunStore]:
    """Open the REGISTERED ledger for the one doctor action that writes.

    Every other check on this command opens raw and read-only on purpose: a
    diagnostic must report on whatever ledger is there without migrating it or
    refusing over identity. This action is different in kind — it records real
    spend — so it goes through the managed identity boundary, where a stray or
    unregistered ledger is a refusal rather than a second place money lands.
    """
    root = canonical_project_root(project_root())
    try:
        store = open_managed_store(
            project_root=root,
            db_path=Path(config.logging.dir).resolve() / "dispatch.db",
            anchors_path=root / DEFAULT_ANCHORS_PATH,
        )
    except (LedgerIdentityError, SchemaTooNewError) as exc:
        _error(str(exc))
        raise typer.Exit(2) from exc
    try:
        yield store
    finally:
        store.close()


def _describe_boundary(route: ProbeRoute) -> str:
    """One line naming the exact boundary a confirm would cross."""
    facts = route.profile.column_values()
    containment = facts.get("containment") or "unknown"
    identity = facts.get("image_id") or facts.get("runner_digest") or "unidentified"
    endpoint = facts.get("endpoint") or "provider default"
    return f"{containment} / {identity[:19]} / {endpoint}"


def _report_confirmation(result: LiveConfirmation) -> int:
    """Print one confirmation and return the process exit code."""
    status, detail, cost, spent = (
        result.status,
        result.detail,
        result.cost,
        result.spent,
    )
    if status is LiveStatus.CONFIRMED:
        _success(f"  route-live-confirm: confirmed — {detail}")
    elif status is LiveStatus.REFUTED:
        _warn(f"  route-live-confirm: NOT live — {detail}")
    else:
        _error(f"  route-live-confirm: refused without spending — {detail}")
    if spent:
        _log(
            f"  spend: {cost.amount_usd:.4f} USD ({cost.certainty}"
            f"{f' via {cost.source}' if cost.source else ''})"
        )
    elif result.from_evidence:
        _dim("  spend: none — answered from evidence inside the 24-hour window")
    # Exit scheme (docs/cli/exit-codes.md): a completed probe that refuted the
    # route is a finding (1); a refusal never crossed the boundary, so the
    # confirmation could not run (2).
    if status is LiveStatus.CONFIRMED:
        return 0
    if status is LiveStatus.REFUTED:
        return 1
    return 2


@doctor_app.command("route-live-confirm")
def route_live_confirm(
    complexity: Annotated[
        str,
        typer.Option(help="Routing tier whose exact boundary to confirm."),
    ] = "complex",
    reclaim: Annotated[
        bool,
        typer.Option(
            help=(
                "Release and acknowledge probes an earlier crash stranded on "
                "this boundary before spending again."
            )
        ),
    ] = False,
    force_probe: Annotated[
        bool,
        typer.Option(
            "--force-probe",
            help="Spend even when fresh evidence already answers.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the spend confirmation prompt."),
    ] = False,
) -> None:
    """Confirm one exact execution boundary with a single bounded provider call.

    THE ONLY kew command that spends money to diagnose. It makes one
    reply-exactly-OK call, one attempt, capped at
    ``PROBE_TIMEOUT_SECONDS`` seconds and ``PROBE_CEILING_USD`` — no retry, no
    customer checkout, no tracker or publication credentials, and no write to
    any customer run, epic, or publication surface.
    """
    preflight_tools()
    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    # One resolution, inside the ledger: this leg's OpenShell certification is
    # read off that store, so resolving it storeless first only to describe it
    # refused the whole command before a word was logged (#2389 review).
    with _open_spend_ledger(config) as store:
        route = resolve_probe_route(
            config, complexity=complexity, store=store, create_key=True
        )
        _log(f"kew doctor: confirming {route.runner_name}/{route.model} is live")
        _log(f"  boundary: {_describe_boundary(route)}")
        status, _probe = route_live_status(store, route.fingerprint)
        if status is not LiveStatus.UNVERIFIED and not force_probe:
            _dim("  fresh evidence exists for this exact boundary; not spending")
        # Ask before the lease: a decline must leave no row or charge.
        elif not yes and not typer.confirm(
            f"  This makes one provider call costing up to "
            f"${PROBE_CEILING_USD:.2f} (cap {PROBE_TIMEOUT_SECONDS}s). Proceed?",
        ):
            _warn("  route-live-confirm: declined — nothing was spent")
            raise typer.Exit(2)
        result = confirm_route_live(
            store, config, complexity=complexity, reclaim=reclaim, force=force_probe
        )
    code = _report_confirmation(result)
    if code:
        raise typer.Exit(code)
