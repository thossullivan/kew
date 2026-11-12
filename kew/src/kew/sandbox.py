"""Container-launch helpers for sandboxed dispatch (Nodes S2 and S3).

Node S2: pure argv construction and runtime detection — no process is spawned.
Node S3: ContainerHandle and launch_container for running argv and streaming stdout.

Container entrypoint contract
----------------------------
``docs/sandbox-entrypoint-contract.md`` is the CANONICAL text: mounted checkout,
prompt-file→stdin handoff, ``"$@"`` extra-args slot, one JSON event per line on
stdout, and the reviewer verdict sidecar. Change it there first — this module is
the host half of that contract, not a second copy of it. The constants below and
``docker/agent-entrypoint.sh`` are asserted against the page by
``tests/test_docs_truthfulness.py``.

The one thing worth repeating here, because it is the reason this module exists
(#695): the prompt travels in a **mounted file**, never in argv and never on a
host-piped stdin.
"""

from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import logging
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final, Literal, Protocol

from kew import credential_redaction, stop_recovery, subprocess_utils, work_dirs
from kew.cancellation import DeferredSIGINT
from kew.config import SandboxConfig
from kew.credential_issuer import BoundedRunKey
from kew.credential_surface import (
    _BARE_KEY_WORD as _BARE_KEY_WORD,
)
from kew.credential_surface import (
    _CREDENTIAL_FORMS_CACHE as _CREDENTIAL_FORMS_CACHE,
)
from kew.credential_surface import (
    _CREDENTIAL_MARKERS as _CREDENTIAL_MARKERS,
)
from kew.credential_surface import (
    _CREDENTIAL_NAME_RE as _CREDENTIAL_NAME_RE,
)
from kew.credential_surface import (
    _JSON_SEPARATOR_RE as _JSON_SEPARATOR_RE,
)
from kew.credential_surface import (
    _JSON_TOKEN_RE as _JSON_TOKEN_RE,
)
from kew.credential_surface import (
    _KEY_ESCAPE as _KEY_ESCAPE,
)
from kew.credential_surface import (
    _LAUNCH_PROXY_VALUES as _LAUNCH_PROXY_VALUES,
)
from kew.credential_surface import (
    _METRIC_NUMBER_RE as _METRIC_NUMBER_RE,
)
from kew.credential_surface import (
    _NO_PROXY_HOST_RE as _NO_PROXY_HOST_RE,
)
from kew.credential_surface import (
    _OPAQUE_FIELD_RE as _OPAQUE_FIELD_RE,
)
from kew.credential_surface import (
    _OPAQUE_PLAIN_VALUE_RE as _OPAQUE_PLAIN_VALUE_RE,
)
from kew.credential_surface import (
    _OPAQUE_QUOTED_VALUE_RE as _OPAQUE_QUOTED_VALUE_RE,
)
from kew.credential_surface import (
    _PLAIN_PRIMITIVE_RE as _PLAIN_PRIMITIVE_RE,
)
from kew.credential_surface import (
    _PROXY_BUILD_ARGS as _PROXY_BUILD_ARGS,
)
from kew.credential_surface import (
    _PROXY_BUILD_NAMES as _PROXY_BUILD_NAMES,
)
from kew.credential_surface import (
    _REDACT_MIN_CHARS as _REDACT_MIN_CHARS,
)
from kew.credential_surface import (
    _REDACT_PERCENT_DECODE_PASSES as _REDACT_PERCENT_DECODE_PASSES,
)
from kew.credential_surface import (
    _REPOSITORY_CREDENTIAL_KEYS as _REPOSITORY_CREDENTIAL_KEYS,
)
from kew.credential_surface import (
    _SCALAR_TEXT_RE as _SCALAR_TEXT_RE,
)
from kew.credential_surface import (
    _SERIALIZED_KEY_ESCAPE_RE as _SERIALIZED_KEY_ESCAPE_RE,
)

# Re-exported for callers that predate the split (governed review round 6 of
# PR #1941, 2026-09-22): the redaction surface moved to a leaf module so the
# progress display can redact without importing the launch stack.
from kew.credential_surface import (
    MAX_REDACTED_FIELD_CHARS as MAX_REDACTED_FIELD_CHARS,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_DIGIT_SUFFIX as OPAQUE_CREDENTIAL_DIGIT_SUFFIX,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_KEY_QUALIFIERS as OPAQUE_CREDENTIAL_KEY_QUALIFIERS,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_KEYS as OPAQUE_CREDENTIAL_KEYS,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_METRIC_WORDS as OPAQUE_CREDENTIAL_METRIC_WORDS,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_PLURAL_SUFFIXES as OPAQUE_CREDENTIAL_PLURAL_SUFFIXES,
)
from kew.credential_surface import (
    OPAQUE_CREDENTIAL_STEMS as OPAQUE_CREDENTIAL_STEMS,
)
from kew.credential_surface import (
    _bare_url_key as _bare_url_key,
)
from kew.credential_surface import (
    _build_credential_forms as _build_credential_forms,
)
from kew.credential_surface import (
    _credential_forms as _credential_forms,
)
from kew.credential_surface import (
    _credential_forms_inputs as _credential_forms_inputs,
)
from kew.credential_surface import (
    _credential_key_words as _credential_key_words,
)
from kew.credential_surface import (
    _credential_shaped_proxy as _credential_shaped_proxy,
)
from kew.credential_surface import (
    _credential_word_forms as _credential_word_forms,
)
from kew.credential_surface import (
    _decode_serialized_key as _decode_serialized_key,
)
from kew.credential_surface import (
    _decoded_key_escape as _decoded_key_escape,
)
from kew.credential_surface import (
    _host_credential_forms as _host_credential_forms,
)
from kew.credential_surface import (
    _inherited_proxy_name as _inherited_proxy_name,
)
from kew.credential_surface import (
    _inline_value_is_open as _inline_value_is_open,
)
from kew.credential_surface import (
    _InlineScan as _InlineScan,
)
from kew.credential_surface import (
    _matches_credential_words as _matches_credential_words,
)
from kew.credential_surface import (
    _named_proxy_components as _named_proxy_components,
)
from kew.credential_surface import (
    _named_proxy_credential_forms as _named_proxy_credential_forms,
)
from kew.credential_surface import (
    _no_proxy_component_forms as _no_proxy_component_forms,
)
from kew.credential_surface import (
    _no_proxy_entry_components as _no_proxy_entry_components,
)
from kew.credential_surface import (
    _normalized_proxy_value as _normalized_proxy_value,
)
from kew.credential_surface import (
    _numeric_metric_value as _numeric_metric_value,
)
from kew.credential_surface import (
    _opaque_block_end as _opaque_block_end,
)
from kew.credential_surface import (
    _opaque_block_ends_at as _opaque_block_ends_at,
)
from kew.credential_surface import (
    _opaque_key_words as _opaque_key_words,
)
from kew.credential_surface import (
    _opaque_scalar_span as _opaque_scalar_span,
)
from kew.credential_surface import (
    _opaque_value_span as _opaque_value_span,
)
from kew.credential_surface import (
    _percent_decoded_stages as _percent_decoded_stages,
)
from kew.credential_surface import (
    _plain_numeric_metric as _plain_numeric_metric,
)
from kew.credential_surface import (
    _plain_primitive_value as _plain_primitive_value,
)
from kew.credential_surface import (
    _plain_scalar_within_indent as _plain_scalar_within_indent,
)
from kew.credential_surface import (
    _proxy_component_selected as _proxy_component_selected,
)
from kew.credential_surface import (
    _proxy_credential_forms as _proxy_credential_forms,
)
from kew.credential_surface import (
    _proxy_credential_key as _proxy_credential_key,
)
from kew.credential_surface import (
    _proxy_url_components as _proxy_url_components,
)
from kew.credential_surface import (
    _public_plain_field as _public_plain_field,
)
from kew.credential_surface import (
    _redact_json_credentials as _redact_json_credentials,
)
from kew.credential_surface import (
    _redact_json_token as _redact_json_token,
)
from kew.credential_surface import (
    _redact_plain_authorization as _redact_plain_authorization,
)
from kew.credential_surface import (
    _redact_plain_opaque_fields as _redact_plain_opaque_fields,
)
from kew.credential_surface import (
    _retained_json_token as _retained_json_token,
)
from kew.credential_surface import (
    _scalar_continues_as_block as _scalar_continues_as_block,
)
from kew.credential_surface import (
    _select_credential_markers as _select_credential_markers,
)
from kew.credential_surface import (
    _short_credential_form as _short_credential_form,
)
from kew.credential_surface import (
    _split_proxy as _split_proxy,
)
from kew.credential_surface import (
    is_opaque_credential_field as is_opaque_credential_field,
)
from kew.credential_surface import (
    is_opaque_credential_key as is_opaque_credential_key,
)
from kew.credential_surface import (
    redact_credential_values as redact_credential_values,
)
from kew.credential_surface import (
    redact_credentials as redact_credentials,
)
from kew.dispatch_admission import PendingAdmission, execution_cancellation
from kew.exit_signal import DiagnosticTail
from kew.image_inputs import (
    AGENT_IMAGE_INPUTS_LABEL,
    agent_image_inputs_digest,
    inputs_label,
)
from kew.inference_transport import ResolvedInferenceTransport
from kew.runner_fields import exception_text
from kew.runtime_backend import (
    DOCKER_COMPATIBLE_RUNTIMES,
    LAUNCHABLE_RUNTIMES,
    OPENSHELL_RUNTIME,
    RuntimeBackendUnavailable,
    require_launchable_runtime,
)
from kew.sandbox_caps import (
    HARD_MAX_CPUS,
    HARD_MAX_MEMORY,
    ResourceCaps,
    derived_tmpfs_size,
    parse_memory_size,
)
from kew.tracking import MAX_RESOURCE_CAP_OBSERVATIONS, DispatchResult

if TYPE_CHECKING:
    from kew.openshell import OpenShellClient
    from kew.openshell_broker import ProviderAdmission

#: The per-container process/thread ceiling: a fork bomb's bound, and a HARD
#: cap like the S3 ones below — no config key, env var or flag reads into it.
#: Docker counts THREADS toward it, so 256 was a ceiling an ordinary frontend
#: run reached: ``npm run ui-check`` (vite plus headless chrome) beside the
#: agent CLI and a second dev server hit 256 and the runner died with exit 134
#: (#2927). 1024 fits that workload; memory (swap off) still bounds the bomb.
_PIDS_LIMIT: Final = 1024

#: Permanent acceptance of operator-asserted direct egress (#1945).
DIRECT_ROUTE_EGRESS_VERIFICATION: Final = "operator-managed-unverified"

#: What a dispatch on ``[sandbox] runtime = "openshell"`` records once a fresh
#: passing egress certification matches its exact boundary tuple. A separate
#: value from ``sidecar-certified`` because the evidence is different: the
#: sidecar's comes from a packet filter kew generated and a proxy it launched,
#: this one from a self-test run inside the boundary under the runner's own
#: policy. A ledger query that could not tell them apart would read one
#: boundary's proof as the other's.
OPENSHELL_ROUTE_EGRESS_VERIFICATION: Final = "openshell-certified"

#: The basis recorded beside it: the proof came from kew's own in-sandbox probe,
#: not from an operator's assertion and not from a sidecar admission.
OPENSHELL_EGRESS_VERIFICATION_BASIS: Final = "openshell-selftest"

#: The basis for an OpenShell container posture kew holds NO proof for. Not a
#: "pending" twin of the sidecar's: nothing later upgrades it. It is what an
#: attestation records when its caller states the boundary was not certified —
#: see :func:`openshell_boundary_certified` for when a flow may state the
#: opposite. Recorded beside ``egress_verification = "unavailable"``, never
#: beside a certified value.
OPENSHELL_UNCERTIFIED_VERIFICATION_BASIS: Final = "openshell-uncertified"

#: The egress route name for the OpenShell boundary. The same string as the
#: runtime, because here the runtime IS the route: there is no proxy to choose.
OPENSHELL_EGRESS_ROUTE: Final = OPENSHELL_RUNTIME

#: The OpenShell policy flow a caller that names none is asking about. Spelled
#: here rather than imported from :mod:`kew.openshell_egress`, which imports
#: this module; ``tests/test_openshell_policy.py`` holds the two equal so the
#: duplicate cannot become a second answer.
OPENSHELL_DISPATCH_FLOW: Final = "dispatch"

# ── Hard resource limits (S3, #1431) ─────────────────────────────────────────
# ``--pids-limit`` alone bounds ONE exhaustion primitive. A container that can
# still allocate every page of host RAM, spin every core, fill the bind-mounted
# checkout, or stream output forever is contained but not isolated: the agent
# cannot escape the boundary and can still take the machine down through it.
#
# Every value below is a HARD cap, deliberately not an operator knob: a knob is
# a way to set the cap back to "none", and the caps exist because "none" is the
# state this node removes. They are pinned by tests/test_sandbox.py.
#
# Two of them — memory and CPU — carry a LOWER-ONLY seam (``[sandbox]
# max_memory`` / ``max_cpus``, #2360) so an operator whose container VM is
# smaller than max_workers x 8 GiB can fit the caps to the VM. Lowering a cap
# is not the knob the rule above forbids: the values here remain the ceilings,
# nothing raises or removes them, and ``kew.sandbox_caps`` refuses an
# out-of-range value at config load rather than clamping it. The defaults in
# this module stay the single source of "unset".
#
# Memory and swap are set EQUAL, which is how Docker spells "no swap": with a
# swap allowance a memory-bomb agent pushes the host into swap-thrash — the
# machine survives the cgroup limit and dies of IO instead. That equality is
# structural (``ResourceCaps.memory_swap``), so it holds at every lowered value.
_MEMORY_LIMIT = HARD_MAX_MEMORY
_CPU_LIMIT = str(HARD_MAX_CPUS)
_DAEMON_CPU_COUNT_TIMEOUT_SECONDS = 5.0

# `info --format` template that yields the daemon's CPU count, per runtime.
# Docker exposes it at the top level; Podman nests it under Host. Asking
# Podman for Docker's field returns an error, which would look like "daemon
# unavailable" and silently degrade every Podman launch.
_DAEMON_CPU_COUNT_TEMPLATES: dict[str, str] = {
    "docker": "{{.NCPU}}",
    "podman": "{{.Host.CPUs}}",
}
#: Cap used when the daemon cannot be asked. One CPU is the only value every
#: daemon accepts; a client-side guess can exceed a small VM and be rejected,
#: which means no container at all.
_CPU_LIMIT_UNKNOWN_DAEMON = "1"

#: Test-runner pool sizes, set to the container's ``--cpus`` value (#2887).
#: ``--cpus`` is a CFS quota, not a CPU set: ``nproc`` inside the container
#: still prints the HOST's count, so ``pytest -n auto`` and vitest size their
#: worker pools to the host and run a 3-CPU container out of memory.
#: ``VITEST_MAX_WORKERS`` is what kew's own ``frontend/vite.config.ts`` reads;
#: ``VITEST_MAX_THREADS`` and ``VITEST_MAX_FORKS`` are the names vitest itself
#: honoured before 4.0, for the repos kew dispatches into.
TEST_WORKER_ENV_NAMES = (
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
    "VITEST_MAX_WORKERS",
    "VITEST_MAX_THREADS",
    "VITEST_MAX_FORKS",
)

# Docker rejects an invalid --cpus value before container creation with this
# stable error. Other failures must retain their original diagnosis.
_CPU_CAP_REJECTION = re.compile(
    r"^docker: Error response from daemon: Range of CPUs is from "
    r"(?P<minimum>[0-9]+(?:\.[0-9]+)?) to "
    r"(?P<maximum>[0-9]+(?:\.[0-9]+)?), as there are only "
    # Docker 29 ends the sentence without a period ("... 2 CPUs available");
    # older daemons ended it with one. Both are the same refusal.
    r"(?P<available>[0-9]+) CPUs available\.?$",
    re.IGNORECASE,
)
_DOCKER_LAUNCH_REFUSAL_EXIT_STATUS = 125

# tmpfs pages are HOST RAM, charged to the container's memory cgroup. An
# unbounded tmpfs is therefore a second, unmetered path to the same exhaustion
# the memory limit closes, so both mounts carry an explicit size. Sized under
# ``_MEMORY_LIMIT`` so filling them is an OOM kill of the container, never of
# the host — and they are the CEILINGS of that sizing, so a lowered
# ``max_memory`` pulls both mounts down with it (:func:`derived_tmpfs_size`)
# instead of leaving a tmpfs larger than the memory cap it belongs to.
_TMP_TMPFS_SIZE = "1g"
_HOME_TMPFS_SIZE = "4g"
#: The divisor each mount's share of the memory cap is taken with.
_HOME_TMPFS_MEMORY_SHARE = 2
_TMP_TMPFS_MEMORY_SHARE = 4
#: The size of each :data:`CHECK_IMAGE_SCRATCH_PATHS` mount.
_SCRATCH_TMPFS_SIZE = "64m"
#: Paths inside ``[sandbox] check_image`` that a check writes beside the
#: image's own files, each mounted as a small tmpfs while the root stays
#: ``--read-only`` (#3495). kew's check image links ``kew-check-frontend``'s
#: ``frontend/node_modules`` to the image's, and Vite bundles
#: ``vite.config.ts`` into the nearest ``node_modules/.vite-temp``: without a
#: writable one there, every frontend check fails with ENOENT on any tree.
CHECK_IMAGE_SCRATCH_PATHS = ("/opt/kew-check/frontend/node_modules/.vite-temp",)

# Per-process rlimits, enforced by the kernel rather than by a watchdog.
# ``fsize`` is the single-file write cap: it is what stops one ``dd`` or one
# runaway log from filling the bind-mounted checkout's filesystem, and it is
# per-FILE, so the aggregate checkout quota below still has work to do.
# ``core=0`` keeps a crashing agent from dumping GiB-scale cores into the mount.
# ``nproc`` is deliberately NOT set: it is a per-UID HOST limit, and every agent
# container runs as the same uid 1001, so one container's limit would apply
# across all of them. Fork bombs are bounded per container by --pids-limit.
_ULIMIT_NOFILE = "4096:8192"
_ULIMIT_FSIZE = str(1 * 1024**3)
_ULIMIT_CORE = "0"

#: Aggregate cap on the bind-mounted checkout. Docker has no quota flag for a
#: bind mount — the host filesystem is the host's — so this one is enforced by
#: :class:`ContainerHandle` while it streams, and a breach kills the container.
CHECKOUT_QUOTA_BYTES = 4 * 1024**3
# An inode bomb measures 0 bytes: 200k empty files defeat a byte quota while
# making every walk slower. Entries are counted in the same walk.
CHECKOUT_QUOTA_ENTRIES = 500_000

#: Largest single line kew will buffer from a container's stdout. The stream is
#: another process's untrusted output: ``readline()`` with no bound turns one
#: newline-free write into unbounded HOST memory, before any parser sees it.
MAX_LINE_BYTES = 1 * 1024**2

#: Total bytes of container stdout admitted for one run. A huge-log agent is a
#: disk-fill primitive aimed at the operator's log directory, not just noise.
TRANSCRIPT_BYTE_CAP = 64 * 1024**2

#: Wall-clock ceiling on one contained run, enforced host-side while streaming.
#: Distinct from ``agent.timeout_minutes`` (an operator's budget, applied by the
#: orchestrator): this is the backstop for the sandbox stream path, which had no
#: wall-clock kill of its own at all.
WALL_CLOCK_LIMIT_SECONDS = 4 * 60 * 60

#: How often the checkout quota is re-measured. The measurement walks the
#: checkout, so it is throttled rather than run per line.
WATCHDOG_INTERVAL_SECONDS = 2.0
# Runtime control commands must not pin a watchdog or terminal finalizer behind
# an unresponsive daemon. Reapers can retry after each bounded failure.
RUNTIME_CONTROL_TIMEOUT_SECONDS = 10.0
# A checkout walk that took t seconds is not repeated for QUOTA_WALK_BACKOFF*t:
# the quota probe never spends more than ~1/QUOTA_WALK_BACKOFF of wall time
# walking a large tree (a node_modules checkout is seconds per walk).
QUOTA_WALK_BACKOFF = 10.0
# ...but the walk time is agent-controlled, so the blind window it buys is
# capped: an agent cannot make its own quota probe wait longer than this.
QUOTA_WALK_MAX_BACKOFF_SECONDS = 20.0
# The walk can be outrun (disk speed x the back-off window); statvfs cannot.
# Checked every interval, O(1), independent of the tree size.
HOST_FREE_SPACE_FLOOR_BYTES = 1 * 1024**3
# The guard reason for the host watchdog's own deadline; the orchestrator
# classifies this one as a timeout, every other reason as a resource cap.
WALL_CLOCK_REASON = "wall-clock limit reached"


class ResourceCapCode(StrEnum):
    """Stable identities for container resource-cap observations."""

    WALL_CLOCK = "wall_clock"
    TRANSCRIPT = "transcript"
    HOST_FREE_SPACE = "host_free_space"
    CHECKOUT_ENTRIES = "checkout_entries"
    CHECKOUT_BYTES = "checkout_bytes"
    OOM = "oom"


@dataclass(frozen=True)
class ResourceCapObservation:
    """One host observation, with stable identity and operator text."""

    code: ResourceCapCode
    reason: str


#: Stream event type emitted, in the container's own stdout shape, when a cap is
#: breached. It is the LAST line of the stream and it carries the reason, so a
#: capped run lands in the run evidence as a named terminal instead of looking
#: like an agent that simply stopped talking (rule 4: no silent clean default).
RESOURCE_CAP_EVENT = "kew_resource_cap"

# The agent user baked into docker/agent.Dockerfile (`useradd -u 1001 agent`).
# Its HOME is tmpfs-mounted writable per container (#742) — the read-only
# root otherwise breaks Claude Code's Bash tool at shell-init (EROFS on
# ~/.claude). Keep in sync with the Dockerfile.
AGENT_HOME = "/home/agent"
_AGENT_UID = 1001

# The runner whose image the legacy ``sandbox.image`` field names: the stock
# kew-agent image ships only the claude CLI, so a committed ``image`` has only
# ever meant "the claude-code image" (#1144).
DEFAULT_IMAGE_RUNNER = "claude-code"

# Actionable "how do I get one" guidance per credential env var, appended to the
# provider-specific "not logged in" preflight message (#1146). A tuple of pairs
# rather than a dict because bandit reads a dict key ending in `_TOKEN` as a
# hardcoded password (B105) — these are NAMES and hints, never values.
# The API key leads: subscription OAuth is for interactive use, and
# docs/authentication.md warns against it for autonomous dispatch (F24).
_CREDENTIAL_MINT_HINTS: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY is issued at console.anthropic.com"),
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "`claude setup-token` mints CLAUDE_CODE_OAUTH_TOKEN, a subscription token; "
        "do not use subscription auth for autonomous dispatch",
    ),
    (
        "OPENROUTER_API_KEY",
        "direct OpenRouter routes need OPENROUTER_PROVISIONING_KEY in the host "
        "environment; kew issues OPENROUTER_API_KEY for each run",
    ),
)

# The Docker network name used in allowlist egress mode. Operators must provision
# this network with restrictions for approved endpoints. kew never creates it.
# If the network does not exist, Docker will refuse to start the container.
ALLOWLIST_NETWORK = "kew-allowlist"

# Bound for the read-only `network inspect` on the dispatch admission path;
# a daemon that cannot answer in this time must not stall the fleet driver.
_NETWORK_INSPECT_TIMEOUT_SECONDS = 5

#: Where the extraction container writes, and the entrypoint that writes it.
#: Content is addressed by digest under this directory — see
#: docs/sandbox-entrypoint-contract.md element 6.
EXTRACT_MOUNT_PATH = "/artifact"

# The security guard's dispatched-agent marker. orchestrator.run_agent sets it
# on the host child env; a container never inherits that env, so
# build_run_argv sets it again as the container path of the mount. Without
# it the guard's agent-only rules (the .claude Write/Edit block and the
# cross-checkout ref rule) are inert inside containment (#1691).
AGENT_MARKER_ENV = "AGENT_ASSIGNED_WORKTREE"
WORK_MOUNT_PATH = "/work"
EXTRACT_ENTRYPOINT = "/usr/local/bin/kew-extract-artifact"

#: A full hex object id, at either width git supports. The producer takes the
#: base as ``$1``; anything beginning with "-" would be read as an option by the
#: git commands it runs.
_FULL_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# A Switchyard endpoint enters a container only through a runner's proven,
# child-local override. Codex carries its endpoint in governed argv provider
# configuration, while Claude Code uses this documented environment name.
# Unsupported runners never reach argv construction because the shared launch
# primitive resolves their empty transport capability first.
_SWITCHYARD_ENDPOINT_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("claude-code", "ANTHROPIC_BASE_URL"),
)


def runner_credential_env_vars(runner: str) -> tuple[str, ...]:
    """The container env allowlist for ``runner`` — SECURITY SURFACE (#1146).

    The names :func:`build_run_argv` forwards (by name only) for a contained
    ``runner`` dispatch, and the names the credential preflight checks. Derived
    from the runner's own ``required_credentials`` declaration via the registry,
    so there is exactly ONE runner→credential table in the codebase and a
    contained run only ever sees its own provider's credentials — an opencode
    container that could read ``CLAUDE_CODE_OAUTH_TOKEN`` would be able to spend
    the operator's Anthropic subscription, and vice versa.

    ``()`` means the runner declares no in-container credential (or is unknown),
    which is un-containable: see :class:`SandboxCredentialUndeclared`. There is
    deliberately no fallback to another runner's names — that guess is the #1104
    incident with an exfiltration surface attached.

    Imported lazily: ``kew.runners.registry`` pulls in the runner modules, which
    are the heavy half of the import graph, and dispatch reaches this on the
    contained path only.
    """
    from kew.runners.registry import runner_required_credentials

    return runner_required_credentials(runner)


def runner_native_containment(runner: str, *, runtime: str | None = None) -> str | None:
    """Return ``runner``'s code-declared native containment posture.

    ``runtime`` names the container boundary kew would otherwise put this
    launch inside, and ``None`` — the default — means kew is offering none
    (a first-class loop's agent, a remediation flow on its documented host
    fallback). Pass the configured runtime only where a contained launch is
    actually on the table, because a runner may decline its own posture in
    favour of kew's boundary on that runtime and the host-launch sites would
    then refuse a run they can contain perfectly well. The admission resolver
    answers that per DISPATCH rather than per config — see
    :func:`_offered_runtime`, which hands this a runtime only when the
    boundary is one it can really launch that dispatch into.
    """
    from kew.runners.registry import runner_containment_posture

    return runner_containment_posture(runner, runtime=runtime)


def runner_uncontainable(runner: str) -> str | None:
    """Return why ``runner`` can never be contained, or ``None``."""
    from kew.runners.registry import runner_uncontainable_reason

    return runner_uncontainable_reason(runner)


_INVALID_GUARD_SETTINGS = object()


def _read_guard_settings(settings: Path) -> object:
    """Read one checkout's Claude settings or return an explicit invalid marker."""
    try:
        return json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _INVALID_GUARD_SETTINGS


def _guard_entries(settings: dict[str, object]) -> list[object]:
    """Return configured Claude pre-tool hook entries."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get("PreToolUse")
    return entries if isinstance(entries, list) else []


def _loaded_guard_matcher(entry: object) -> str | None:
    """Return an entry's matcher when it loads the exact shipped guard."""
    if not isinstance(entry, dict):
        return None
    commands = entry.get("hooks")
    if not isinstance(commands, list):
        return None
    guard = ".claude/hooks/security-guard.sh"
    loaded = any(
        isinstance(command, dict)
        and command.get("type") == "command"
        and command.get("command") == guard
        for command in commands
    )
    matcher = entry.get("matcher")
    return matcher if loaded and isinstance(matcher, str) else None


def _guard_matcher_covers(matcher: str, tool: str) -> bool:
    """Return whether one valid Claude matcher covers ``tool``.

    Invalid patterns produce a warning before returning the fail-closed result.
    """
    try:
        return re.search(matcher, tool) is not None
    except (re.error, OverflowError, RecursionError) as exc:
        from kew.progress import _warn

        _warn(
            f"Sandbox: invalid Claude command guard matcher {matcher!r}: "
            f"{exception_text(exc)}"
        )
        return False


def _guard_matchers_are_complete(matchers: set[str]) -> bool:
    """Return whether the configured matchers cover every guarded tool."""
    required_tools = {"Bash", "Write", "Edit"}
    return all(
        any(_guard_matcher_covers(matcher, tool) for matcher in matchers)
        for tool in required_tools
    )


def _guard_content_is_trusted(hook: Path) -> bool:
    """Return whether the checkout hook matches the trusted project hook."""
    from kew.policy import (
        HOOK_ABSENT,
        HOOK_REL_PATH,
        HOOK_UNREADABLE,
        hook_digest,
        repo_root,
    )

    unavailable = {HOOK_ABSENT, HOOK_UNREADABLE}
    trusted_digest = hook_digest(repo_root() / HOOK_REL_PATH)
    return trusted_digest not in unavailable and hook_digest(hook) == trusted_digest


def command_guard_state(runner: str, checkout_dir: Path | None) -> str:
    """Return whether this target can load the Claude command guard."""
    from kew.runners.protocol import COMMAND_GUARD_RUNNER

    if runner != COMMAND_GUARD_RUNNER:
        return "not-applicable"
    if checkout_dir is None:
        return "absent"
    hook = checkout_dir / ".claude" / "hooks" / "security-guard.sh"
    settings = _read_guard_settings(checkout_dir / ".claude" / "settings.json")
    if (
        not hook.is_file()
        or not os.access(hook, os.X_OK)
        or not _guard_content_is_trusted(hook)
        or not isinstance(settings, dict)
    ):
        return "absent"
    loaded_matchers = {
        matcher
        for entry in _guard_entries(settings)
        if (matcher := _loaded_guard_matcher(entry)) is not None
    }
    return "loaded" if _guard_matchers_are_complete(loaded_matchers) else "absent"


def _filesystem_boundary(runner: str, posture: str) -> str:
    """Return the filesystem boundary that the posture actually established."""
    if posture == "refused":
        return "not-applicable"
    boundary = {
        POSTURE_SANDBOXED: "container-mount",
        POSTURE_NATIVE: "runner-native-workspace-write",
        POSTURE_UNCONTAINED: "host",
        POSTURE_UNCONTAINABLE_FLOW: "host",
        "host": "host",
    }.get(posture)
    if boundary is not None:
        return boundary
    return "host" if runner_uncontainable(runner) is not None else "container-mount"


CONTAINER_PROXY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
"""Proxy names the inherit policy may pass by name, in argv order."""

ALWAYS_BLANK_PROXY_NAMES = ("FTP_PROXY", "ALL_PROXY", "ftp_proxy", "all_proxy")
"""Proxy names blanked at container launch under either policy: Docker client
config proxies (allProxy, ftpProxy) and Podman forwarding inject them."""

CONTROLLED_PROXY_NAMES = CONTAINER_PROXY_NAMES + ALWAYS_BLANK_PROXY_NAMES
"""Every proxy name the container run argv assigns explicitly, in argv order."""


def _proxy_environment_names(proxy_environment: str) -> list[str]:
    """Select names without reading or recording proxy values."""
    if proxy_environment == "inherit":
        return [name for name in CONTAINER_PROXY_NAMES if name in os.environ]
    return list(CONTAINER_PROXY_NAMES)


def _container_proxy_environment_args(
    proxy_environment: str, names: tuple[str, ...] = CONTROLLED_PROXY_NAMES
) -> list[str]:
    """Specify every proxy name so runtime defaults cannot inject values."""
    inherited = _proxy_variable_actions(proxy_environment)["inherited"]
    _require_parseable_inherited_proxies(inherited)
    return [
        argument
        for name in names
        for argument in ("-e", name if name in inherited else f"{name}=")
    ]


def _require_parseable_inherited_proxies(
    inherited: list[str], *, runtime: str | None = None, image: str | None = None
) -> None:
    """Refuse a launch or build that would forward an unparseable proxy URL.

    Posture resolution calls this first, with the runtime and image it saw, so
    dispatch attests the refusal on the ledger. The launch and build paths
    keep it as the fail-closed backstop.
    """
    for name in inherited:
        value = os.environ.get(name, "")
        if not value or name.lower() == "no_proxy":
            continue
        try:
            _split_proxy(value)
        except ValueError:
            # No "key = value" shape may precede the remediation: the
            # launch scope's sanitizer redacts the rest of such a line.
            raise SandboxProxyUnparseable(
                f"Sandbox: {name} does not parse as a proxy URL, so kew cannot "
                "redact its credentials while inheriting host proxies. "
                f'Set [sandbox] proxy_environment to "none", or fix {name}.',
                runtime=runtime,
                image=image,
            ) from None


def _proxy_variable_actions(
    proxy_environment: str, names: tuple[str, ...] = CONTROLLED_PROXY_NAMES
) -> dict[str, list[str]]:
    """Record inherited and blanked names without recording values."""
    inherited = (
        _proxy_environment_names(proxy_environment)
        if proxy_environment == "inherit"
        else []
    )
    return {
        "inherited": inherited,
        "blanked": [name for name in names if name not in inherited],
    }


def _attested_proxy_variables(
    posture: str, proxy_environment: str, runtime: str | None
) -> dict[str, list[str]]:
    """What this launch's proxy argv ACTUALLY did with the ten controlled names.

    Not what ``[sandbox] proxy_environment`` asks for — what happened — because
    only a launch that builds a container argv can act on that setting:
    :func:`_container_proxy_environment_args` passes an inherited name through
    and assigns every other one empty. Two launches build no such argv, and
    both record "nothing inherited, nothing blanked":

    * a posture that launched no container at all, and
    * the OpenShell runtime, whose agent environment is the exec allowlist
      :data:`kew.openshell.SANDBOX_EXEC_ENV_NAMES` and carries no proxy name
      under either policy. ``proxy_environment = "inherit"`` used to be
      recorded here as ten inherited variables — and in ``credential_names``
      as ten more exposed names — for an exec that forwarded none of them (PR
      #2401 review). The setting it asks for is a docker-argv setting; what the
      operator is told about it is
      :func:`openshell_ignored_argv_settings_warning`, not a ledger entry
      claiming it took effect.
    """
    if posture != POSTURE_SANDBOXED or runtime == OPENSHELL_RUNTIME:
        return {"inherited": [], "blanked": []}
    return _proxy_variable_actions(proxy_environment)


def _sandboxed_credential_names(
    runner: str,
    synthetic_credential_name: str | None,
    openshell_provider: str | None,
) -> list[str]:
    """The credential names ONE contained launch exposes, by boundary kind.

    Three boundaries, three different facts, and the field they share is a flat
    list of names where a bare name means "this value was injected". A brokered
    launch injects no value, so recording ``ANTHROPIC_API_KEY`` there would be
    a false claim — :func:`kew.openshell_broker.attested_credential_names`
    prefixes the provider that holds the credential and the variables that hold
    only a placeholder.

    Checked in order of narrowing ownership, the sidecar first: its worker
    credential is minted per attempt by the supervisor and no gateway provider
    holds it, so a brokered name cannot be the right answer for a launch that
    has one. A sidecar launch on the OpenShell runtime is handed no provider at
    all (:func:`kew.openshell_broker.broker_route`) and the order is belt to
    that brace — a provider name here would both claim the operator's
    long-lived credential authenticated the launch and drop the per-attempt
    credential that did.
    """
    from kew.openshell_broker import attested_credential_names

    declared = runner_credential_env_vars(runner)
    if synthetic_credential_name is not None:
        return [synthetic_credential_name]
    if openshell_provider is not None:
        return attested_credential_names(openshell_provider, declared)
    return list(_injected_credentials(declared))


def _posture_credential_names(
    runner: str,
    posture: str,
    synthetic_credential_name: str | None,
    inherited_proxies: list[str],
    openshell_provider: str | None = None,
) -> list[str]:
    """Return the names-only credential exposure bound for this launch.

    ``inherited_proxies`` is the ``inherited`` list this same launch attests in
    ``proxy_variables`` (:func:`_attested_proxy_variables`), handed in rather
    than re-derived from ``proxy_environment``: an inherited proxy URL can
    carry userinfo, so it belongs in the credential surface, and deriving "did
    they cross?" twice is how the two fields came to disagree. On the OpenShell
    runtime they did not cross and the list is empty, so the surface no longer
    names variables the exec never forwarded (PR #2401 review).
    """
    if posture == "refused":
        return []
    if posture != POSTURE_SANDBOXED:
        # Host agents inherit every non-KEW variable. Any variable can carry a
        # credential, so a declared-provider allowlist would understate this
        # boundary. Record every inherited name and never its value.
        return sorted(name for name in os.environ if not name.startswith("KEW_"))
    names = _sandboxed_credential_names(
        runner, synthetic_credential_name, openshell_provider
    )
    names.extend(inherited_proxies)
    return list(dict.fromkeys(names))


def _network_boundary(posture: str, egress_verification: str) -> str:
    """Return the network boundary that the posture established."""
    if posture in {POSTURE_SANDBOXED, "refused"}:
        return egress_verification
    return "not-applicable"


def posture_network_boundary(posture: str, egress_verification: str) -> str:
    """The network boundary a posture established, for a reader outside dispatch.

    The health page reports the same label the ledger records, rather than
    recomposing "what does egress look like for this lane?" from the config —
    two answers to that question is how a surface comes to claim a boundary the
    run row does not.
    """
    return _network_boundary(posture, egress_verification)


def _direct_route_network_evidence(
    posture: str, egress_verification: str, runtime: str | None, network: str
) -> dict[str, object]:
    """Record what kew can see of the operator's own attachment network."""
    if (
        posture != POSTURE_SANDBOXED
        or egress_verification != DIRECT_ROUTE_EGRESS_VERIFICATION
    ):
        return {}
    driver, internal = inspect_attachment_network(
        runtime, effective_network_name(network)
    )
    return {
        "egress_verification_basis": "operator-asserted",
        "network_driver": driver,
        "network_internal": internal,
    }


def posture_record_fields(
    *,
    runner: str,
    posture: str,
    egress_verification: str,
    checkout_dir: Path | None = None,
    synthetic_credential_name: str | None = None,
    proxy_environment: str = "none",
    prerequisite_gap: bool = False,
    runtime: str | None = None,
    network: str = "allowlist",
    openshell_provider: str | None = None,
) -> dict[str, object]:
    """Build the complete, names-only security boundary for one launch.

    ``openshell_provider`` names the gateway provider holding this runner's
    credential on the OpenShell runtime, and is ``None`` on every other
    boundary — so ``credential_names`` says "a provider holds it, these
    variables hold a placeholder" exactly where that is true.

    ``runtime`` is read for more than the attachment network: it is also which
    boundary this launch crossed, and so whether a docker-argv setting could
    have taken effect at all (:func:`_attested_proxy_variables`).
    """
    proxy_variables = _attested_proxy_variables(posture, proxy_environment, runtime)
    return {
        **_direct_route_network_evidence(
            posture, egress_verification, runtime, network
        ),
        "filesystem_boundary": _filesystem_boundary(runner, posture),
        "network_boundary": _network_boundary(posture, egress_verification),
        "credential_names": _posture_credential_names(
            runner,
            posture,
            synthetic_credential_name,
            proxy_variables["inherited"],
            openshell_provider,
        ),
        "proxy_environment": proxy_environment,
        "proxy_variables": proxy_variables,
        "command_guard": (
            "not-applicable"
            if posture == "refused"
            else command_guard_state(runner, checkout_dir)
        ),
        "admission": (
            "dangerously_skip_containment"
            if posture == POSTURE_UNCONTAINED
            else "prerequisite_gap"
            if prerequisite_gap
            else "policy"
        ),
    }


# Flows that run an agent on the host because kew has not built a contained
# path for them yet — NOT because anyone chose the host. Distinct from
# `dangerously_skip_containment`, which is an operator decision, and from
# `uncontainable_reason`, which is a property of a runner. This is a gap on
# kew's side, and it is named here so it appears in the ledger as itself
# (W1.1 / D2, node 3d).
POSTURE_UNCONTAINABLE_FLOW = "uncontained_flow"

# The two postures that mean "the agent is not running loose on this machine":
# a container boundary, or a runner that contains itself. Named because three
# modules now branch on them, and a set of bare strings duplicated per module is
# how one of them silently stops matching.
POSTURE_SANDBOXED = "sandboxed"
POSTURE_NATIVE = "native"
CONTAINED_POSTURES = frozenset({POSTURE_SANDBOXED, POSTURE_NATIVE})


def openshell_boundary_certified(
    config: SandboxConfig, *, posture: str, sidecar: bool
) -> bool:
    """Whether a contained flow's OpenShell boundary carries its own proof.

    Derived, not plumbed: a sandboxed OpenShell posture is only reachable
    through :func:`require_certified_boundary`, which refuses unless a fresh
    certification matching this exact boundary tuple was read off the ledger.
    Every flow that can attest one resolves through a module the launch-path
    gate in ``tests/test_openshell_certification.py`` holds to that.

    A sidecar route answers no whatever the runtime: its proof is the packet
    certification, which is a different observation on different images, and
    the egress gate says nothing about it.
    """
    return (
        not sidecar
        and posture == POSTURE_SANDBOXED
        and config.runtime == OPENSHELL_RUNTIME
    )


def attest_flow_posture(
    store: object,
    *,
    flow: str,
    runner: str,
    config: SandboxConfig,
    posture: str,
    image: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
    checkout_dir: Path | None = None,
    repo: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    inference_transport: ResolvedInferenceTransport | None = None,
    certified: bool = False,
    attempt_number: int = 1,
) -> str:
    """Record — and return — the containment posture of a non-dispatch flow.

    Review remediation and resolve once consulted no posture at all: an operator
    with ``[sandbox] enabled = true`` got fail-closed containment on dispatch and
    silent host execution here, with nothing on the ledger to say so. The config
    claimed one thing and two flows did another.

    The posture is now DECIDED by the caller and merely recorded here. It used to
    be derived, with the reason hardcoded to "has no contained path" — true when
    written, and a falsehood the moment those flows gained one. An attestation
    function that computes its own subject will eventually attest something that
    is no longer so.
    """
    from kew.inference_transport import direct_route

    if posture not in CONTAINED_POSTURES:
        _warn_uncontained_flow(flow, config, reason, posture=posture)
    sidecar = inference_transport is not None and inference_transport.sidecar
    runtime = config.runtime if posture == POSTURE_SANDBOXED else None
    # Derived here rather than at four call sites, because the fact is the
    # same one at all of them: the flow reached a sandboxed OpenShell posture,
    # which the egress gate is the only way past. Before #2389's review the
    # remediation flows genuinely could not say this — they resolved without a
    # ledger — so every such container was attested `openshell-uncertified`.
    certified = certified or openshell_boundary_certified(
        config, posture=posture, sidecar=sidecar
    )
    emit_resolved_posture(
        store,
        runner=runner,
        config=config,
        posture=posture,
        runtime=runtime,
        image=image,
        reason=reason,
        run_id=run_id,
        checkout_dir=checkout_dir,
        repo=repo,
        issue_num=issue_num,
        item_id=item_id,
        egress_route=contained_egress_route(
            posture=posture, runtime=runtime, sidecar=sidecar
        ),
        synthetic_credential_name=(
            inference_transport.worker_credential_env_name
            if sidecar and inference_transport is not None
            else None
        ),
        certified=certified,
        attempt_number=attempt_number,
        direct_transport=direct_route(inference_transport),
    )
    return posture


def contained_egress_route(
    *, posture: str, runtime: str | None, sidecar: bool
) -> Literal["direct", "sidecar", "openshell"]:
    """Identify a launch's egress route from the containment it resolved.

    One function because two writers answer this question — dispatch, from its
    :class:`SandboxDecision`, and :func:`attest_flow_posture`, from the config
    the flow resolved against — and a route derived two ways is how one of them
    records another route's verification value for the boundary it actually
    ran. That is exactly what happened before #2389's review: this derivation
    lived inline in :func:`attest_flow_posture` as "sidecar or direct", so a
    sandboxed OpenShell flow was attested with the direct route's permanent
    ``operator-managed-unverified``.

    The sidecar is a transport choice and wins when it is in play: the worker's
    packets leave through the proxy whatever the container runtime is. The
    OpenShell boundary is a runtime choice, so it is read off the runtime — and
    only for :data:`POSTURE_SANDBOXED`, because a posture with no container has
    no contained route to name and records ``direct`` like every other
    non-container posture.
    """
    if sidecar:
        return "sidecar"
    if posture == POSTURE_SANDBOXED and runtime == OPENSHELL_EGRESS_ROUTE:
        return OPENSHELL_EGRESS_ROUTE
    return "direct"


def _route_verification_basis(
    egress_route: str, *, posture: str, certified: bool
) -> dict[str, object]:
    """Name WHERE a contained route's egress claim came from, per route.

    Only the routes kew can produce evidence for record a basis here; the
    direct route's ``operator-asserted`` is written by
    :func:`_direct_route_network_evidence` beside the network shape it read, and
    a non-sandboxed posture has no contained route to describe.

    Neither OpenShell value is a guess. Dispatch admission refuses an
    uncertified OpenShell boundary before any posture is written, so a dispatch
    reaching here is always certified; the remediation flows resolve
    containment without a ledger, so a flow reaching here may not be, and
    :data:`OPENSHELL_UNCERTIFIED_VERIFICATION_BASIS` says which happened. This
    used to raise on the uncertified case instead, on the premise that it was
    unreachable — it is not, and raising out of an attestation writer would
    abort a flow that is already running rather than refuse it before it
    started.
    """
    if posture != POSTURE_SANDBOXED:
        return {}
    if egress_route == "sidecar":
        return {
            "egress_verification_basis": (
                "sidecar-admission" if certified else "pending-sidecar"
            )
        }
    if egress_route == OPENSHELL_EGRESS_ROUTE:
        return {
            "egress_verification_basis": (
                OPENSHELL_EGRESS_VERIFICATION_BASIS
                if certified
                else OPENSHELL_UNCERTIFIED_VERIFICATION_BASIS
            )
        }
    return {}


def _attested_openshell_provider(
    config: SandboxConfig,
    runner: str,
    *,
    posture: str,
    run_id: str | None,
    egress_route: str,
    direct: bool,
) -> str | None:
    """The gateway provider THIS posture record names, or ``None``.

    Only a container boundary routes through the gateway, so a ``native`` runner
    — contained by itself, on the host, whatever ``[sandbox] runtime`` says —
    names no provider, and neither does a refusal or an uncontained run.

    The provider named is the one the launch authenticates through
    (:func:`~kew.openshell_broker.attested_provider`), not the operator's
    mapping: a run whose minted key goes into ``kew-run-<run_id>`` would
    otherwise put two ledger facts about one launch on the chain, this one
    naming the long-lived provider and the realized route profile naming the
    per-run one.

    ``direct`` is the transport's own fact, not ``egress_route``'s: every
    launch without a sidecar has egress route ``direct``, switchyard included,
    and only a transport that talks straight to its upstream mints the key the
    per-run provider holds.
    """
    from kew.openshell_broker import attested_provider

    if posture != POSTURE_SANDBOXED:
        return None
    return attested_provider(
        config,
        runner,
        run_id=run_id,
        direct=direct,
        sidecar=egress_route == "sidecar",
    )


def _attested_caps(posture: str, config: SandboxConfig) -> dict[str, object]:
    """The EFFECTIVE per-container caps this dispatch ran under (#2360).

    So a run report answers "what did it run under?" from the ledger rather
    than from whatever the config says today. Both keys are present on every
    posture for the same reason ``reason`` is present-as-None on every
    posture: one payload schema, no consumer KeyError.

    Only :data:`POSTURE_SANDBOXED` launches a kew container, so only there is
    a cap a fact. On every other attested posture — ``uncontained``,
    ``uncontained_flow``, ``native``, ``host`` (the fallback token a tracked
    launch records when nothing resolved a posture) and ``refused`` — no cgroup
    enforced anything, and the value is ``None`` rather than a concrete ``8g``
    attesting a limit that did not exist. This is not the sentinel ``runtime``
    and ``image`` carry: ``refused`` deliberately records both, naming the
    runtime and image the launch tried and could not get.

    ``max_cpus`` is the ceiling the launch asked for — ``--cpus`` is
    additionally clamped to the daemon's own CPU count, which only the launch
    can know.

    The OpenShell runtime answers ``None`` for the same reason, one layer up:
    its container is created by :meth:`kew.openshell.OpenShellClient.
    sandbox_create`, whose verified v0.1.2 argv surface carries an image,
    providers, a policy and labels — and no memory or CPU flag. So kew sets no
    cgroup limit there, whatever ``[sandbox] max_memory`` says, and attesting a
    concrete ``8g`` would have claimed a cap that never existed on the only
    runtime where it is not kew that enforces one (PR #2401 review). An
    operator who configured a cap hears that it does not apply from
    :func:`openshell_ignored_argv_settings_warning`.
    """
    if posture != POSTURE_SANDBOXED or config.runtime == OPENSHELL_RUNTIME:
        return {"max_memory": None, "max_cpus": None}
    caps = resource_caps(config)
    return {"max_memory": caps.memory, "max_cpus": caps.cpus}


def emit_resolved_posture(
    store: object,
    *,
    runner: str,
    config: SandboxConfig,
    posture: str,
    runtime: str | None,
    image: str | None,
    reason: str | None,
    run_id: str | None,
    checkout_dir: Path | None,
    repo: str | None,
    issue_num: int | None,
    item_id: str | None,
    strict: bool = False,
    egress_route: Literal["direct", "sidecar", "openshell"] = "direct",
    synthetic_credential_name: str | None = None,
    certified: bool = False,
    egress_verification: str = "unavailable",
    attempt_number: int = 1,
    direct_transport: bool = True,
) -> None:
    """Bind posture to its attempt and require durable sidecar transitions.

    ``direct_transport`` is ``transport.mode == "direct"``
    (:func:`kew.inference_transport.direct_route`), and defaults to the answer
    for a caller that resolved no transport at all: no switchyard in the path.
    It is a separate fact from ``egress_route``, which is ``direct`` for every
    launch without a sidecar — and on the OpenShell runtime only a transport
    that talks straight to its upstream mints the bounded key whose per-run
    provider this record would otherwise name.
    """
    from kew import audit
    from kew.audit import resolve_actor

    if posture == POSTURE_SANDBOXED:
        egress_verification = classify_egress_verification(
            route=egress_route,
            certified=certified,
        )
    elif posture == "refused":
        egress_verification = "unavailable"
    openshell_provider = _attested_openshell_provider(
        config,
        runner,
        posture=posture,
        run_id=run_id,
        egress_route=egress_route,
        direct=direct_transport,
    )
    payload = {
        "attempt_number": attempt_number,
        "posture_id": str(uuid.uuid4()),
        "posture": posture,
        "runtime": runtime,
        "enabled": config.enabled,
        "runner": runner,
        "image": image,
        "reason": reason,
        "egress_verification": egress_verification,
        **_attested_caps(posture, config),
        **posture_record_fields(
            runner=runner,
            posture=posture,
            egress_verification=egress_verification,
            checkout_dir=checkout_dir,
            runtime=runtime,
            network=config.network,
            synthetic_credential_name=synthetic_credential_name,
            proxy_environment=config.proxy_environment,
            openshell_provider=openshell_provider,
            prerequisite_gap=(
                posture == POSTURE_UNCONTAINABLE_FLOW
                and config.enabled is not False
                and not config.uncontained_flows_allowed
            ),
        ),
        **_route_verification_basis(egress_route, posture=posture, certified=certified),
    }
    audit.emit(
        store,  # type: ignore[arg-type]
        "sandbox_resolved",
        actor=resolve_actor(),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload=payload,
        strict=strict,
    )
    if egress_route == "sidecar" and posture == POSTURE_SANDBOXED:
        _require_posture_record(store, run_id, payload, runtime=runtime, image=image)


def _require_posture_record(
    store: object,
    run_id: str | None,
    payload: dict[str, object],
    *,
    runtime: str | None,
    image: str | None,
) -> None:
    """Acknowledge this exact transition, never an earlier matching posture."""
    from kew import audit

    reason = "sandbox posture was not durably recorded"
    try:
        read = getattr(store, "list_audit_events", None)
        events = (
            read(event="sandbox_resolved", run_id=run_id, limit=None)
            if callable(read)
            else []
        )
        if any(
            event.run_id == run_id
            and isinstance(event.payload, dict)
            and all(event.payload.get(key) == value for key, value in payload.items())
            for event in events
        ):
            return
    except Exception as exc:
        # A failed readback cannot acknowledge admission; retain its cause.
        reason = f"{reason}: readback failed: {exc}"
    audit.emit(
        store,  # type: ignore[arg-type]
        "sandbox_posture_refused",
        actor=audit.resolve_actor(),
        run_id=run_id,
        payload={"attempt_number": payload["attempt_number"], "reason": reason},
    )
    raise SandboxRuntimeUnavailable(
        reason, reason="posture_not_recorded", runtime=runtime, image=image
    )


def resolve_flow_containment(
    store: object,
    *,
    flow: str,
    runner: str,
    config: SandboxConfig,
    gateway_launch: bool,
    run_id: str | None = None,
    checkout_dir: Path | None = None,
    repo: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    inference_transport: ResolvedInferenceTransport | None = None,
) -> SandboxDecision:
    """Resolve dispatch containment and attest it before a flow launch.

    ``gateway_launch`` is REQUIRED and has no default, unlike every other
    caller of :func:`resolve_transport_sandbox`. This function's whole purpose
    is to put a posture on the audit chain, so a flow that forgot to state its
    launch shape would attest ``sandboxed`` for the OpenShell runtime and only
    be refused afterwards, at the argv-build site — the admit-then-refuse
    ordering :func:`_require_launchable_boundary` exists to eliminate. Both of
    today's flows (``kew ci-fix`` and graph ``plan_run``) pass ``False``: they
    swap one execution step for a container argv
    (:func:`kew.dispatch_core._contained_execution_for`) rather than handing
    their launch to the gateway, exactly like the two remediation flows that
    resolve through :mod:`kew.remediation_containment`. Asking the question
    here means a flow added later has to answer it too.

    ``store`` is both the ledger this attests onto AND the one the OpenShell
    egress gate reads its certification from, so it is threaded into the
    resolution as well as the attestation: a flow that resolved without it
    refused every OpenShell launch on "no ledger to check" while the proof sat
    on that exact store (#2389 review).
    """
    try:
        decision = resolve_transport_sandbox(
            config,
            runner=runner,
            inference_transport=inference_transport,
            gateway_launch=gateway_launch,
            store=store,
        )
    except SandboxRuntimeUnavailable as exc:
        emit_resolved_posture(
            store,
            runner=runner,
            config=config,
            posture="refused",
            runtime=exc.runtime,
            image=exc.image,
            reason=exc.reason,
            run_id=run_id,
            checkout_dir=checkout_dir,
            repo=repo,
            issue_num=issue_num,
            item_id=item_id,
        )
        raise
    attest_flow_posture(
        store,
        flow=flow,
        runner=runner,
        config=config,
        posture=decision.posture,
        image=decision.image,
        run_id=run_id,
        checkout_dir=checkout_dir,
        repo=repo,
        issue_num=issue_num,
        item_id=item_id,
        inference_transport=inference_transport,
    )
    return decision


def _warn_uncontained_flow(
    flow: str, config: SandboxConfig, reason: str | None, *, posture: str
) -> None:
    """Name the host boundary and any explicit remediation policy exception.

    Names the SPECIFIC obstacle when the caller supplied one: "no runtime" is
    the operator's to fix and "no image for this runner" is kew's, and
    "uncontained" alone does not tell them apart.
    """
    from kew.progress import _warn

    detail = (
        " Host fallback admitted by [sandbox] allow_uncontained_flows = true."
        if posture == POSTURE_UNCONTAINABLE_FLOW and config.uncontained_flows_allowed
        else ""
    )
    because = f" — {reason}" if reason else ""
    _warn(
        f"Containment: {flow} runs the agent on this machine with no container "
        f"boundary{because}.{detail}"
    )


def _switchyard_endpoint_env_var(runner: str) -> str | None:
    return next(
        (
            name
            for runner_name, name in _SWITCHYARD_ENDPOINT_ENV_VARS
            if runner_name == runner
        ),
        None,
    )


def _container_environment_args(
    runner: str,
    inference_transport: ResolvedInferenceTransport | None,
) -> list[str]:
    """Return the exact environment surface exposed inside one container."""
    args = [arg for name in runner_credential_env_vars(runner) for arg in ("-e", name)]
    if inference_transport is None or inference_transport.mode == "direct":
        return args

    endpoint_name = _switchyard_endpoint_env_var(runner)
    if endpoint_name is None:
        return args
    if inference_transport.endpoint is None:
        raise ValueError("Switchyard container endpoint is unavailable")
    args.extend(["-e", f"{endpoint_name}={inference_transport.endpoint}"])
    return args


def _any_env_set(names: tuple[str, ...]) -> bool:
    """True if any of ``names`` is set (non-empty) in the environment.

    Presence only — the VALUE is never read past this truthiness check, so no
    credential can leak into a message, a run row, or the audit ledger (#1146).
    """
    return any(os.environ.get(name) for name in names)


def _present_credentials(names: tuple[str, ...]) -> tuple[str, ...]:
    """The subset of ``names`` actually set — the credentials a launch would
    hand this container. Names only, never values."""
    return tuple(name for name in names if os.environ.get(name))


def _injected_credentials(names: tuple[str, ...]) -> tuple[str, ...]:
    """The declared names this launch will hand the container, names only.

    A bounded OpenRouter key is minted for the run rather than inherited, so it
    is absent from the host environment and still reaches the container. Reading
    the environment alone recorded an empty credential exposure and silenced the
    open-egress warning on a provisioning-only host, which is the configuration
    a direct OpenRouter route now requires (governed review round 4,
    2026-09-21).
    """
    from kew.credential_issuer import has_provisioning_key

    present = set(_present_credentials(names))
    issuable = "OPENROUTER_API_KEY" in names and has_provisioning_key()
    return tuple(
        name
        for name in names
        if name in present or (issuable and name == "OPENROUTER_API_KEY")
    )


def credential_missing_message(runner: str, names: tuple[str, ...]) -> str:
    """Provider-specific "not logged in" guidance for ``runner`` (#1146).

    One sentence naming the env var(s) this runner authenticates with plus how
    to obtain one — claude points at ``claude setup-token``, an OpenRouter-backed
    runner at ``OPENROUTER_PROVISIONING_KEY``, from which kew issues each run's
    ``OPENROUTER_API_KEY``. Shared by the dispatch preflight refusal
    and ``kew sandbox smoke`` so an operator sees the same actionable text
    whichever surface tells them first.
    """
    joined = " or ".join(names)
    hints = [hint for name, hint in _CREDENTIAL_MINT_HINTS if name in names]
    suffix = f" ({'; '.join(hints)})" if hints else ""
    return f"Export {joined}{suffix}"


def open_egress_warning(network: str, *, runner: str) -> str | None:
    """Loud warning string when egress is fully open AND a credential is injected.

    ``network="open"`` omits ``--network`` (full Docker-bridge egress). With an
    agent credential forwarded into the container, a single outbound request from
    in-container code can exfiltrate the key — and it contradicts
    ``docs/threat-model.md``'s stated egress-restriction posture (egress
    restricted to inference + GitHub). The default ``"allowlist"`` avoids this;
    the warning fires only when an operator explicitly opts into ``"open"``.

    Keyed on ``runner`` (#1146): it names the credential THIS container will
    actually be handed — by name, never by value — so the operator can see which
    key is at risk instead of a generic "a credential". A runner whose own
    credentials are all absent has nothing to exfiltrate and does not warn; a
    bounded key this host can mint counts as handed over, because it is.

    Returns a one-line warning to surface at dispatch, or ``None`` when egress is
    restricted or no credential is present.
    """
    if network != "open":
        return None
    injected = _injected_credentials(runner_credential_env_vars(runner))
    if not injected:
        return None
    return (
        f"Sandbox: network='open' ships FULL container egress while {runner}'s "
        f"{' and '.join(injected)} is injected — one outbound request can "
        "exfiltrate it, contradicting the threat model's egress restriction. "
        "Set sandbox.network = 'allowlist' (the default) to restrict egress."
    )


def openshell_ignored_argv_settings_warning(config: SandboxConfig) -> str | None:
    """Loud warning naming the settings a gateway sandbox cannot deliver.

    Three ``[sandbox]`` settings reach a container as part of the docker argv
    and have no counterpart on the gateway: ``max_memory`` and ``max_cpus`` are
    ``--memory`` / ``--cpus`` flags, and ``proxy_environment = "inherit"``
    passes proxy names through as ``-e`` arguments
    (:func:`_container_proxy_environment_args`). An OpenShell sandbox is created
    from an image, a policy and its providers
    (:meth:`kew.openshell.OpenShellClient.sandbox_create`) and its agent
    environment is the exec allowlist, so none of the three has anywhere to go.

    The ledger already records that truthfully — no caps, no inherited proxy
    names (:func:`_attested_caps`, :func:`_attested_proxy_variables`) — and
    this is the other half of the same fix: a setting that does not apply has
    to be said out loud rather than left for an operator to discover from a run
    report. Returns ``None`` on every other runtime, and on this one when the
    operator configured none of the three.
    """
    if config.runtime != OPENSHELL_RUNTIME:
        return None
    ignored = [
        name
        for name, configured in (
            ('proxy_environment = "inherit"', config.proxy_environment == "inherit"),
            ("max_memory", config.max_memory is not None),
            ("max_cpus", config.max_cpus is not None),
        )
        if configured
    ]
    if not ignored:
        return None
    return (
        f"Sandbox: [sandbox] {' and '.join(ignored)} cannot take effect on "
        f'runtime "{OPENSHELL_RUNTIME}" — a docker run argument '
        "(--memory, --cpus, -e NAME) has nowhere to go on a gateway sandbox, "
        "which is created from an image, a policy and its providers. This run "
        "attests what actually crossed instead; set [sandbox] runtime = "
        '"docker" (or "podman") where these settings must apply.'
    )


# The prompt file the host writes into the mounted checkout dir and the path it
# is visible at inside the container. Both layers (build_run_argv/dispatch and
# docker/agent-entrypoint.sh) MUST agree on these names.
PROMPT_FILENAME = ".kew-prompt.txt"
PROMPT_MOUNT_PATH = "/work/.kew-prompt.txt"

# There is no reviewer verdict file: the code under review can write anything
# under /work, so the verdict travels in the reviewer's own typed result event
# (kew.review_stream, #2021).

#: Ledger-stable tokens naming WHY containment was refused, carried on the
#: exception and attested in the ``sandbox_resolved`` payload (#1144). The
#: message is for humans and free to be reworded; these are what the audit
#: ledger is queried on, so a refusal's cause survives a message edit and the
#: several distinct causes are never byte-identical on the chain.
REFUSAL_RUNTIME_UNAVAILABLE = "runtime_unavailable"
REFUSAL_CONTINUATION_HOST_ONLY = "continuation_host_only"
REFUSAL_IMAGE_UNMAPPED = "image_unmapped"
REFUSAL_CREDENTIAL_UNDECLARED = "credential_undeclared"
REFUSAL_CREDENTIAL_MISSING = "credential_missing"
REFUSAL_HOST_FREE_SPACE = "host_free_space"
REFUSAL_CHECKOUT_OVER_QUOTA = "checkout_over_quota"
REFUSAL_PROXY_UNPARSEABLE = "proxy_unparseable"
#: ``[sandbox] placement`` names a host this dispatch cannot be put on now
#: (#2526) — every instance is at ``max_runs``, or the run is one no instance
#: can carry (a sidecar transport, or a model credential this machine does not
#: hold). Its own token: the boundary is not missing and this host is not out
#: of disk — the run has nowhere to go *yet*, and the fix is to wait, free an
#: instance, or change the policy.
REFUSAL_PLACEMENT_UNAVAILABLE = "placement_unavailable"
#: A validated ``[sandbox] runtime`` name kew cannot launch into. Its own
#: token, not ``runtime_unavailable``: the host is not missing a runtime, kew
#: is missing the boundary it was told to use, and the two need different
#: fixes. ``openshell`` carried this token until epic node N6 gave it a launch
#: path (:mod:`kew.openshell_launch`); it remains the refusal for any name
#: ``kew.config`` starts accepting before this build can launch into it.
REFUSAL_OPENSHELL_UNAVAILABLE = "openshell-runtime-unavailable"
#: No fresh passing egress certification matches the OpenShell boundary this
#: dispatch would launch. Its own token, distinct from the two refusals it
#: would otherwise be confused with: the host is not missing a runtime
#: (``runtime_unavailable``) and kew is not missing the boundary
#: (``openshell-runtime-unavailable``) — the boundary is there and unproven,
#: and the fix is one shipped command (``kew sandbox certify``).
REFUSAL_OPENSHELL_UNCERTIFIED = "openshell-egress-uncertified"

#: kew ships no OpenShell policy for this runner, so there is no boundary for
#: ``kew sandbox certify`` to probe and nothing it could ever prove. Its own
#: token because the remedy is the opposite one: every other uncertified cause
#: is cleared by running that command, and this one is cleared only by kew
#: packaging a policy — ``pi`` and ``hermes`` used to be refused as
#: ``openshell-egress-uncertified`` and told to run a command that cannot
#: succeed for them (#2435).
REFUSAL_OPENSHELL_POLICY_UNPACKAGED = "openshell-policy-unpackaged"

#: On the OpenShell runtime a credential lives in a gateway PROVIDER, not in
#: the environment, so "export the variable" is the wrong remedy and
#: ``credential_missing`` the wrong cause. Its own token: a query for runs that
#: refused over a broker gap must not return the ones that refused over an
#: unexported token.
REFUSAL_BROKER_PROVIDER_MISSING = "broker_provider_missing"

#: Ledger posture for a run the operator deliberately took outside containment
#: (``dangerously_skip_containment``). Distinct from the old ``"guard-only"``
#: and ``"disabled"`` tokens ON PURPOSE: those recorded the same host execution
#: as a downgrade or a config state, so the chain could not distinguish a
#: machine that quietly lacked Docker from an operator who chose the risk. A
#: query for "which runs had no container boundary" now has one answer.
POSTURE_UNCONTAINED = "uncontained"

#: Banner for every uncontained dispatch. Deliberately not a one-line notice:
#: the previous downgrade printed one line among many and was routinely missed,
#: which is how operators ended up uncontained without knowing.
UNCONTAINED_BANNER = "\n".join(
    (
        "=" * 72,
        "DANGEROUS: containment skipped — this agent runs on your machine.",
        "",
        "There is no container boundary. The agent can read and write any file",
        "your user can, and prompt injection from an issue body or a fetched",
        "page reaches your host. The in-process guard is a second layer, not a",
        "boundary: it blocks known-destructive commands and nothing more.",
        "",
        "Chosen by [sandbox] dangerously_skip_containment = true.",
        "To contain: install Docker or OrbStack, then remove that line.",
        "=" * 72,
    )
)


class SandboxRuntimeUnavailable(RuntimeError):
    """Raised when a fail-closed sandbox posture cannot be satisfied — the
    runtime is missing, or no agent credential is in the environment (#732).

    Subclasses RuntimeError so callers with the old ``except RuntimeError``
    fail-closed handling keep working.

    Carries the machine-readable ``reason`` (one of the ``REFUSAL_*`` tokens)
    plus whatever containment facts WERE resolved before the refusal:
    ``runtime`` is the detected runtime (present for every cause except a
    missing runtime) and ``image`` the runner's mapped image (present once
    resolution got that far). Both default to ``None`` so the pre-#1144 raise
    sites keep working, and both exist so a refusal attests what it actually
    saw — a refusal that happened *because* a runtime was present must not be
    recorded as runtime-null (#1144 review).
    """

    #: Cause attested when a raise site does not name one. Each subclass has
    #: exactly one cause, so it sets this instead of every raise repeating it.
    _DEFAULT_REASON = REFUSAL_RUNTIME_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        runtime: str | None = None,
        image: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason or self._DEFAULT_REASON
        self.runtime = runtime
        self.image = image


class SandboxImageUnmapped(SandboxRuntimeUnavailable):
    """Raised when a dispatch must be contained but its resolved runner has no
    container image mapped (#1144).

    Subclasses :class:`SandboxRuntimeUnavailable` so every existing fail-closed
    handler covers it — an unmapped runner is a reason not to launch, exactly
    like a missing runtime. It is NEVER a reason to fall back to the claude
    image: that fallback is the #1104 incident (an opencode node handed an
    OpenRouter model string to the claude CLI, 404 in 1.3s).
    """

    _DEFAULT_REASON = REFUSAL_IMAGE_UNMAPPED


class SandboxCredentialUndeclared(SandboxRuntimeUnavailable):
    """Raised when a runner has a mapped image but declares no in-container
    credential (an empty ``required_credentials``), so nothing it can
    authenticate with would be forwarded into the container (#1144).

    Mapping an image is not enough to contain a runner: ``build_run_argv``
    forwards exactly the runner's OWN declared names (#1146), so a runner that
    declares none — lemonade, whose endpoint is host-local, or a runner kew does
    not know — would start with no provider credential and fail to authenticate.
    Refusing is the fail-closed answer; borrowing another runner's credential is
    never the alternative.
    """

    _DEFAULT_REASON = REFUSAL_CREDENTIAL_UNDECLARED


class SandboxProxyUnparseable(SandboxRuntimeUnavailable):
    """An inherited proxy URL does not parse, so its userinfo cannot be redacted.

    Only the complete value is recognized in agent output then, and a password
    the agent prints on its own survives redaction (re-audit F10, 2026-09-22).
    The launch is refused; the message names the variable, never its value.
    """

    _DEFAULT_REASON = REFUSAL_PROXY_UNPARSEABLE


#: The legacy default tag produced by the packaged Claude Code Dockerfile.
STOCK_AGENT_IMAGE = "kew-agent:latest"

_PACKAGED_IMAGE_DOCKERFILES = {
    DEFAULT_IMAGE_RUNNER: "agent.Dockerfile",
    "codex": "agent-codex.Dockerfile",
    "opencode": "agent-opencode.Dockerfile",
}
_PACKAGED_IMAGE_TAGS = {
    DEFAULT_IMAGE_RUNNER: STOCK_AGENT_IMAGE,
    "codex": "kew-agent-codex:latest",
    "opencode": "kew-agent-opencode:latest",
}


def missing_image_message(image: str, *, runner: str, runtime: str = "docker") -> str:
    """Return the actionable build instruction for an absent local ``image``.

    kew ships inputs for Claude Code, Codex and OpenCode. Custom tags can still name
    operator-owned images, so their guidance remains conditional.
    """
    current_pin = _packaged_pin_hint(runner)
    if image == current_pin:
        return (
            f"Sandbox: image {image!r} for runner {runner!r} is not present "
            f"in the local container runtime ({runtime}), and kew never pulls "
            f"it. Build the packaged inputs: kew sandbox build --runner {runner}."
        )
    if AGENT_IMAGE_PIN_TAG_RE.fullmatch(image):
        # Pin-shaped but not THIS install's pin for THIS runner: a stale pin
        # from an older release, or another runner's. The bare build would
        # produce the current pin and leave the configured tag absent.
        return (
            f"Sandbox: image {image!r} for runner {runner!r} is not present "
            f"in the local container runtime ({runtime}), and kew never pulls "
            f"it. It is not the pin this install produces for {runner!r}, "
            f"which is {current_pin}; point {_image_config_field(runner)} "
            f"at {current_pin} and run kew sandbox build --runner {runner}."
        )
    if image == _PACKAGED_IMAGE_TAGS.get(runner):
        # The legacy stock tag. `kew sandbox build` produces the digest pin
        # now, so the bare build instruction would leave THIS tag absent and
        # the operator following it still unable to dispatch.
        return (
            f"Sandbox: image {image!r} for runner {runner!r} is not present "
            f"in the local container runtime ({runtime}), and kew never pulls "
            f"it. kew sandbox build now produces the digest pin "
            f"{_packaged_pin_hint(runner)}; point {_image_config_field(runner)} "
            f"at that pin and run kew sandbox build --runner "
            f"{runner}. To keep this legacy tag instead, build it explicitly: "
            f"kew sandbox build --runner {runner} --tag {image} --unpinned."
        )
    return (
        f"Sandbox: image {image!r} for runner {runner!r} is not present in the "
        f"local container runtime ({runtime}), and kew never pulls it. This is "
        "not a standard kew tag, so how to rebuild it depends on where it came "
        f"from. Run kew sandbox build --runner {runner} --tag {image} --unpinned "
        f"only if it should contain kew's packaged {runner} image. If it is your own "
        "hardened or internal image, build or pull it normally. Do NOT run the "
        "kew build command because it would replace that image."
    )


def _image_config_field(runner: str) -> str:
    """Where THIS runner's image is configured: ``[sandbox] image`` is read for
    claude-code only; every other runner is mapped through ``images.<runner>``."""
    if runner == DEFAULT_IMAGE_RUNNER:
        return f"[sandbox] image (or images.{runner})"
    return f"[sandbox] images.{runner}"


def _packaged_pin_hint(runner: str) -> str:
    """The digest pin the packaged inputs produce, or its shape when unreadable."""
    try:
        return _expected_packaged_image_pin(packaged_agent_image_context(), runner)
    except (ValueError, OSError, KeyError):
        return "kew-agent:docker-<digest12>"


def resolve_runner_image(config: SandboxConfig, runner: str) -> str:
    """Return the container image that runs ``runner``, or fail closed.

    Resolution order:
    1. ``[sandbox] images.<runner>`` — the explicit per-runner mapping.
    2. ``[sandbox] image`` — but only for :data:`DEFAULT_IMAGE_RUNNER`, since
       the stock kew-agent image ships only the claude CLI. This is the
       back-compat path for the committed configs that predate the map.
    3. Otherwise raise :class:`SandboxImageUnmapped`.

    Shared by dispatch and the reviewer launcher so the two cannot diverge on
    which image a runner gets.
    """
    mapped = config.images.get(runner)
    if mapped:
        return mapped
    if runner == DEFAULT_IMAGE_RUNNER and config.image:
        return config.image
    raise SandboxImageUnmapped(
        f"Sandbox: runner {runner!r} has no container image mapped, so it "
        "cannot be contained (running it in the claude image would hand its "
        "model string to the claude CLI — the #1104 incident). Set "
        f'[sandbox] images.{runner} = "<image ref implementing the container '
        'entrypoint contract>" (docs/configuration.md), or route this '
        "dispatch to a runner that has one."
    )


@dataclass(frozen=True)
class SandboxDecision:
    """Effective sandbox posture for one dispatch.

    ``posture`` is the ledger token: ``"sandboxed"`` (container-contained),
    ``"native"`` (contained by the runner itself), or ``"uncontained"`` (host
    execution explicitly admitted with ``dangerously_skip_containment``; see
    :data:`POSTURE_UNCONTAINED` for why the old ``"guard-only"`` and
    ``"disabled"`` tokens are gone).

    ``image`` is the container image resolved for THIS dispatch's runner, and
    is the only image a caller may launch (``None`` when not contained). Both
    container callers take it from here rather than reading
    ``config.sandbox.image``, so dispatch and the reviewer cannot diverge.
    """

    sandboxed: bool
    runtime: str | None
    posture: str
    notice: str | None
    image: str | None = None
    egress_verification: str = "unavailable"


def detect_runtime(config: SandboxConfig) -> str | None:
    """Resolve the configured runtime binary path. Returns None if not found."""
    return shutil.which(config.runtime)


# Cache positive detections by runtime name until process exit or explicit reset.
# Missing runtimes must be probed again so installation repairs a running driver.
_RUNTIME_CACHE: dict[str, str] = {}


def detect_runtime_cached(config: SandboxConfig) -> str | None:
    """Reuse positive detections; retry missing runtimes on every lookup."""
    key = config.runtime
    if key in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[key]
    resolved = detect_runtime(config)
    if resolved is not None:
        _RUNTIME_CACHE[key] = resolved
    return resolved


def reset_runtime_cache() -> None:
    """Clear the per-process runtime-detection cache."""
    _RUNTIME_CACHE.clear()


def image_id_runtime(config: SandboxConfig, detected: str | None) -> str | None:
    """The runtime that can resolve an image TAG to its immutable ID.

    Normally ``detected`` — the configured runtime, as the caller already
    probed it. ``openshell`` is the exception and the reason this seam exists:
    it is a gateway over a container driver, and its v0.1.2 CLI has no image
    surface at all, so asking it to ``image inspect`` refuses every dispatch at
    admission, before the launch path it was supposed to reach. The ID is
    instead read from the local docker-compatible driver the gateway runs
    images under — the arrangement the 2026-10-03 feasibility spike proved
    (OpenShell's Docker driver on the dispatching host).

    That is a host-locality assumption, and it is deliberately NOT hidden: a
    gateway whose driver lives on another machine has no local image to read,
    and this answers with the absent runtime so ``require_image_id`` refuses
    saying the image is not here — rather than launching from an ID resolved
    against a different machine's image store.

    ``detected`` is passed in rather than probed here so this stays a pure
    choice BETWEEN runtimes: each caller keeps the detection seam it already
    had, and only the openshell answer is new.
    """
    if config.runtime != OPENSHELL_RUNTIME:
        return detected
    return next(
        (
            path
            for path in (shutil.which(name) for name in DOCKER_COMPATIBLE_RUNTIMES)
            if path is not None
        ),
        None,
    )


def _runtime_label(resolved: str, config: SandboxConfig) -> str:
    """Human label for the dispatch banner (``Sandbox: on (orbstack)``)."""
    low = resolved.lower()
    if "orbstack" in low:
        return "orbstack"
    if "podman" in low:
        return "podman"
    return config.runtime


def _require_containable_runner(
    config: SandboxConfig, runner: str, *, runtime: str | None
) -> str:
    """Return ``runner``'s image, or refuse to contain it (#1144).

    ``runtime`` is the runtime label already detected by the caller; it rides on
    whichever refusal is raised so the audit ledger attests that a runtime WAS
    present — a refusal recorded as runtime-null is indistinguishable from the
    no-runtime refusal it is the opposite of (#1144 review).

    Both refusals raise even under ``enabled="auto"``, and deliberately so. The
    runtime is present, so this host CAN contain, and ``auto`` means "contain
    when the host can" — not "contain the runners kew happens to know about".
    Downgrading a containment-capable host to an uncontained run because of a
    gap on *kew's* side — an unmapped image, an undeclared credential — is a
    posture drop no operator asked for, riding a warning line in a driver log
    (#1089: guard-only non-claude was 3-for-3 on root-checkout residue). The
    refusal is still per-dispatch: sibling claude nodes in the same epic keep
    dispatching contained, so this is not the old all-or-nothing posture.

    The credential refusal is asked only where the credential travels in the
    agent's ENVIRONMENT. On the brokered runtime it does not: the gateway holds
    it in a named provider and injects it at the endpoint its policy governs,
    and the sandbox is handed placeholders. An empty ``required_credentials``
    is therefore not a gap there but the accurate declaration — ``codex``
    authenticates through the ChatGPT-mode ``auth.json`` the in-sandbox
    launcher writes, with no env name to forward (epic node N10) — and whether
    anything can authenticate the launch is answered by
    :func:`~kew.openshell_broker.brokered_admission` a few lines above the
    caller's own credential check, which refuses as ``broker_provider_missing``
    when nothing can. Keeping the env question here would refuse every such
    runner on the one runtime kew built a boundary for it.
    """
    try:
        image = resolve_runner_image(config, runner)
    except SandboxImageUnmapped as exc:
        # Annotate rather than re-wrap: `resolve_runner_image` is the shared
        # resolver and has no runtime to report, but this call site does.
        exc.runtime = runtime
        raise
    if config.runtime != OPENSHELL_RUNTIME and not runner_credential_env_vars(runner):
        raise SandboxCredentialUndeclared(
            f"Sandbox: runner {runner!r} has image {image!r} mapped, but "
            "declares no in-container credential (an empty "
            "`required_credentials`), so the container would launch with no "
            "provider credential and authenticate as nothing. Route this "
            "dispatch to a runner that declares one, or set both "
            "sandbox.enabled = false and dangerously_skip_containment = true "
            "to run it on the host on purpose.",
            runtime=runtime,
            image=image,
        )
    return image


def _uncontained_decision(because: str) -> SandboxDecision:
    """The one shape every uncontained outcome takes.

    Single constructor on purpose. Uncontained execution used to be reachable
    from three places — ``enabled = false``, no runtime, no credential — each
    building its own decision with its own posture token and its own one-line
    notice. Three spellings of "the agent is on your host" meant no single
    place to attach the banner, and no single ledger token to query. Anything
    that skips the boundary comes through here or it does not happen.
    """
    return SandboxDecision(
        sandboxed=False,
        runtime=None,
        posture=POSTURE_UNCONTAINED,
        notice=f"{UNCONTAINED_BANNER}\nReason containment was unavailable: {because}",
    )


def _broker_provider_missing_outcome(
    config: SandboxConfig, runner: str, admission: ProviderAdmission, *, image: str
) -> SandboxDecision:
    """Refuse a brokered dispatch whose provider the gateway does not have.

    The remedy is never "export a variable": on this runtime the credential
    value never enters the agent's environment, so the gap is in
    ``[sandbox.openshell] providers`` or on the gateway, and ``admission`` —
    the SAME answer that refused, not a second question to the gateway —
    already resolved which. Re-asking is how a provider created between the two
    calls produced a refusal whose own text said the gateway holds the provider
    it was refusing over.
    """
    detail = admission.detail
    if not config.dangerously_skip_containment:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: {runner} cannot authenticate on the OpenShell runtime — "
            f"{detail}. Create the provider on the gateway and map it under "
            "[sandbox.openshell] providers to dispatch sandboxed, or set "
            "[sandbox] dangerously_skip_containment = true to run this agent "
            "on the host with no container boundary.",
            reason=REFUSAL_BROKER_PROVIDER_MISSING,
            runtime=config.runtime,
            image=image,
        )
    return _uncontained_decision(
        f"{runner} cannot authenticate on the OpenShell runtime ({detail})"
    )


def _credential_missing_outcome(
    config: SandboxConfig,
    runner: str,
    required: tuple[str, ...],
    *,
    image: str,
    brokered: ProviderAdmission | None = None,
) -> SandboxDecision:
    """Refuse — or, if explicitly opted out, go uncontained — when ``runner``'s
    own credential is absent (#1146).

    The runtime is present and the runner is containable, but none of the names
    it authenticates with is in the environment — so the container would come up
    "Not logged in" and burn the dispatch at $0 (#721). The message is
    provider-specific (:func:`credential_missing_message`): claude is told about
    ``claude setup-token``, an OpenRouter-backed runner about
    ``OPENROUTER_API_KEY``, rather than every runner being told about claude's.

    Raises before any container is launched, so this is a preflight refusal —
    no dispatch is spent. Names only ever appear here; the value is never read.
    """
    if brokered is not None:
        return _broker_provider_missing_outcome(config, runner, brokered, image=image)
    guidance = credential_missing_message(runner, required)
    if not config.dangerously_skip_containment:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: no {runner} credential in the environment — containers "
            f"cannot inherit CLI login. {guidance} to dispatch sandboxed, or "
            "set [sandbox] dangerously_skip_containment = true to run this "
            "agent on the host with no container boundary.",
            reason=REFUSAL_CREDENTIAL_MISSING,
            runtime=config.runtime,
            image=image,
        )
    return _uncontained_decision(
        f"no {runner} credential in the environment (containers cannot inherit "
        f"CLI login). {guidance} for containment."
    )


def resolve_effective_sandbox(
    config: SandboxConfig,
    *,
    runner: str,
    store: object | None = None,
    direct: bool = True,
    gateway_launch: bool = True,
    host_only: bool = False,
) -> SandboxDecision:
    """Resolve containment using only the runner's own host credentials.

    ``store`` is the ledger the OpenShell egress gate reads its certification
    from; without one an OpenShell resolution is refused (#2319).

    ``direct`` is ``transport.mode == "direct"``
    (:func:`kew.inference_transport.direct_route`), and defaults to the answer
    for a caller that resolved no transport. It reaches two branches, both on
    the OpenShell runtime: only a direct OpenRouter route mints its own bounded
    key, so only such a route may be admitted without a provider on the
    gateway; and only a direct route has a launch path at all, because the
    gateway exec carries no endpoint override and the packaged egress policies
    permit no switchyard destination
    (:data:`kew.openshell_launch.SWITCHYARD_LAUNCH_REFUSAL`).

    ``gateway_launch`` is whether the CALLER can launch through the OpenShell
    gateway, and defaults to the dispatch path's answer because that is the
    path this function models. The tracked remediation flows pass ``False``:
    they swap one execution step for a container argv
    (:func:`kew.dispatch_core._contained_execution_for`) and have no gateway
    launch at all, so admitting ``sandboxed`` for them on that runtime attested
    a boundary nothing would hold.

    ``host_only`` is the preserved-continuation shape
    (``kew.dispatch_core._resolve_and_apply_sandbox``): its worktree is already
    on the host, so no container is on offer for it whatever the runtime says.
    It only picks which posture question the runner is asked; the continuation
    refusal for a runner that needs kew's boundary is unchanged.
    """
    return _resolve_effective_sandbox(
        config,
        runner=runner,
        store=store,
        direct=direct,
        gateway_launch=gateway_launch,
        host_only=host_only,
    )


def resolve_transport_sandbox(
    config: SandboxConfig,
    *,
    runner: str,
    inference_transport: ResolvedInferenceTransport | None = None,
    gateway_launch: bool = True,
    store: object | None = None,
) -> SandboxDecision:
    """Resolve credentials for the selected worker route before attestation."""
    from kew.inference_transport import direct_route

    if inference_transport is not None and inference_transport.sidecar:
        return resolve_effective_sidecar_sandbox(
            config,
            runner=runner,
            synthetic_credential_env_name=inference_transport.worker_credential_env_name,
            store=store,
        )
    return resolve_effective_sandbox(
        config,
        runner=runner,
        store=store,
        direct=direct_route(inference_transport),
        gateway_launch=gateway_launch,
    )


def resolve_effective_sidecar_sandbox(
    config: SandboxConfig,
    *,
    runner: str,
    synthetic_credential_env_name: str | None,
    store: object | None = None,
    host_only: bool = False,
) -> SandboxDecision:
    """Resolve containment using the selected sidecar's worker credential.

    No ``direct`` flag: a sidecar route is never the direct one, and its worker
    credential is minted per attempt rather than brokered or minted as a
    bounded key, so where the route IS launchable it resolves the same way on
    every runtime (:func:`kew.openshell_broker.broker_route`).

    ``sidecar=True`` is the route fact rather than
    ``synthetic_credential_env_name is not None``, which only approximated it:
    a sidecar route that named no worker credential is still a sidecar route,
    and on the OpenShell runtime it is refused for its topology before its
    credential is ever considered.

    ``host_only`` carries the preserved-continuation shape for the same reason
    :func:`resolve_effective_sandbox` takes it: a continuation whose route is a
    sidecar is still a run against an existing host worktree, and the two entry
    points must not disagree about whether kew is offering a boundary.
    """
    return _resolve_effective_sandbox(
        config,
        runner=runner,
        synthetic_credential_env_name=synthetic_credential_env_name,
        sidecar=True,
        store=store,
        host_only=host_only,
    )


def require_certified_boundary(
    config: SandboxConfig,
    *,
    runner: str,
    store: object | None,
    flow: str = OPENSHELL_DISPATCH_FLOW,
) -> None:
    """Gate the runtimes whose boundary kew can prove, and only those.

    ``docker`` and ``podman`` have no certification contract: kew installs no
    destination rules there and
    :data:`DIRECT_ROUTE_EGRESS_VERIFICATION` says so permanently. OpenShell
    does, so it is checked.

    Public because the remediation flows resolve containment through their own
    decision module and have to apply the SAME rule: before the #2389 review
    they returned a sandboxed OpenShell posture with this gate never consulted.

    ``flow`` names which boundary on this runtime is being claimed. It defaults
    to the dispatch one because that is what every caller but the adversarial
    reviewer resolves; the reviewer passes
    :data:`~kew.openshell_egress.REVIEW_FLOW`, so the proof it demands is bound
    to the reviewer's own policy digest and a dispatch certification cannot
    satisfy it (epic node N12).
    """
    if config.runtime == OPENSHELL_RUNTIME:
        require_openshell_egress_certification(
            config, runner=runner, store=store, flow=flow
        )


def _require_packaged_openshell_policy(
    config: SandboxConfig, *, runner: str, flow: str = OPENSHELL_DISPATCH_FLOW
) -> None:
    """Refuse a runner this build ships no OpenShell policy for.

    Asked first, and before the ledger: whether kew packages a policy for a
    runner is a fact about the build, and no ledger, image mapping or
    certification run changes the answer. Reached through the egress gate
    because it is the same question one step earlier — ``pi`` and ``hermes``
    have images an operator can map and credentials kew declares, so they got
    all the way to the certification match, failed it on the absent policy
    file, and were refused as ``openshell-egress-uncertified`` with a
    ``kew sandbox certify`` that cannot succeed for them (#2435).

    The message names the missing policy and deliberately names no command:
    there is nothing the operator can run to produce one.

    ``flow`` is read for the same reason the gate above reads it: ``claude-code``
    is the only runner with a review policy, so a reviewer pinned to another
    runner is refused HERE, naming the flow — rather than at the certification
    match, which would prescribe a certify command that cannot succeed.
    """
    from kew.openshell_egress import packaged_openshell_policy_runners

    packaged = packaged_openshell_policy_runners(flow)
    if runner in packaged:
        return
    raise SandboxRuntimeUnavailable(
        f"Sandbox: no packaged OpenShell {flow} policy for runner {runner!r}, "
        f"so kew has no {OPENSHELL_RUNTIME} boundary to certify for it and no "
        f"command can prove one; this build ships {flow} policies for "
        f'{", ".join(packaged)}. Set [sandbox] runtime = "docker" (or '
        f'"podman") to dispatch {runner}, or dispatch a runner with a '
        "packaged policy.",
        reason=REFUSAL_OPENSHELL_POLICY_UNPACKAGED,
        runtime=config.runtime,
    )


def require_openshell_egress_certification(
    config: SandboxConfig,
    *,
    runner: str,
    store: object | None,
    flow: str = OPENSHELL_DISPATCH_FLOW,
) -> None:
    """Refuse an OpenShell dispatch whose boundary has no fresh passing proof.

    Admission must not claim a boundary it has not seen hold (#2319). The proof
    is the certification ``kew sandbox certify`` appends, matched on the exact
    tuple a dispatch would launch — this runner's immutable image ID, the byte
    digest of the policy it runs under, the gateway version that enforces it,
    and the digest of the in-sandbox launcher — and no older than the 24-hour
    evidence window the readiness probe already uses.

    Every way of not having that proof refuses identically, with one token and
    one remedy: absent, expired, bound to a different image or policy, signed
    by the wrong actor, self-contradictory, or unreadable because no ledger was
    supplied. "kew could not check" is not a pass.

    The one exception is a runner kew ships no policy for, which is refused
    first and under its own token by
    :func:`_require_packaged_openshell_policy` — that remedy is not the shipped
    command, so it must not be named with the causes the command clears.

    ``flow`` selects the policy whose digest the proof must be bound to, and so
    WHICH boundary is being proven. The remedy names the same flow, because a
    reviewer refused for want of a review certification must not be sent to run
    the dispatch certify it may already have (epic node N12).
    """
    from kew.audit import EgressCertificationUnavailable
    from kew.egress_certification import file_digest
    from kew.openshell import OpenShellClient
    from kew.openshell_egress import IMAGE_DRIVER_RUNTIME, openshell_policy_path

    _require_packaged_openshell_policy(config, runner=runner, flow=flow)
    remedy = (
        f"Run `kew sandbox certify --runtime {OPENSHELL_RUNTIME} "
        f"--runner {runner} --flow {flow}` on this host, then dispatch again."
    )
    if store is None:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: no ledger is available to read the {OPENSHELL_RUNTIME} "
            "egress certification, so kew cannot prove this boundary is "
            f"closed. {remedy}",
            reason=REFUSAL_OPENSHELL_UNCERTIFIED,
            runtime=config.runtime,
        )
    # Through the shared admission helper, not `resolve_runner_image`: an
    # unmapped image and an undeclared credential are kew-side gaps with their
    # OWN refusal reasons, and raising them reason-less from here would make
    # two fixable problems look like an unproven boundary.
    image = _require_containable_runner(config, runner, runtime=config.runtime)
    try:
        image_id = require_image_id(
            IMAGE_DRIVER_RUNTIME,
            image,
            runner=runner,
            refusal=(f"the {OPENSHELL_RUNTIME} egress certification cannot be matched"),
        )
        require_openshell_boundary_match(
            store,
            image_id=image_id,
            policy_sha256=file_digest(openshell_policy_path(runner, flow=flow)),
            client=OpenShellClient.from_settings(config.openshell),
        )
    except (
        EgressCertificationUnavailable,
        ContainedImageUnresolvedError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: {exception_text(exc)} — kew will not claim the "
            f"{OPENSHELL_RUNTIME} container boundary it cannot prove is "
            f"closed. {remedy}",
            reason=REFUSAL_OPENSHELL_UNCERTIFIED,
            runtime=config.runtime,
            image=image,
        ) from exc


def require_openshell_boundary_match(
    store: object,
    *,
    image_id: str,
    policy_sha256: str,
    client: OpenShellClient,
) -> None:
    """Require a passing certification for this boundary on ``client``'s gateway.

    The one place the bound tuple is assembled for a match, shared by admission
    and by the launch itself (:mod:`kew.openshell_launch`): the gateway's
    version and identity are read from the SAME client the caller is about to
    use, so a proof taken against one gateway cannot clear a sandbox created on
    another (#2624). Raises
    :class:`~kew.audit.EgressCertificationUnavailable` or the gateway's own
    error; the caller decides which refusal it becomes.
    """
    from kew.audit import matching_runtime_egress_certification
    from kew.openshell_egress import (
        openshell_gateway_identity,
        openshell_supervisor_digest,
    )

    matching_runtime_egress_certification(
        store,  # type: ignore[arg-type]
        runtime=OPENSHELL_RUNTIME,
        image_id=image_id,
        policy_sha256=policy_sha256,
        gateway_version=client.check_version(),
        gateway_identity=openshell_gateway_identity(client),
        supervisor_digest=openshell_supervisor_digest(),
    )


def _runner_credential_available(
    required: tuple[str, ...],
    synthetic_credential_env_name: str | None,
    brokered: ProviderAdmission | None,
) -> bool:
    """Report whether anything can authenticate the worker for this launch.

    Which question that is depends on the container boundary. On docker and
    podman the credential travels in the environment, so the environment (plus
    a mintable bounded key, plus a selected sidecar's synthetic name) is the
    whole answer. On the OpenShell runtime no credential VALUE enters the
    agent's environment at all, so the environment says nothing and
    ``brokered`` carries the gateway's own answer instead. Asking the
    environment there would admit a dispatch on the strength of a host token
    the sandbox is never handed, and the container would come up unauthenticated
    with the dispatch already paid for.

    ``brokered`` is ``None`` for every launch the gateway is not in the
    credential path of (:func:`~kew.openshell_broker.brokered_admission`).
    Those fall through to the env-and-synthetic answer below, the same one
    docker gives them; reading ``brokered.available`` for a sidecar launch
    refused it over a long-lived provider the launch never uses. A sidecar
    route is now refused on this runtime for its topology before it reaches
    here at all (:func:`_refuse_unlaunchable_openshell`) — the ``None`` arm
    still answers it on docker and podman, and stays the arm any future
    non-brokered route lands in.

    ``brokered`` is passed in rather than resolved here because the refusal
    below explains THIS answer: a second call to the gateway could disagree with
    the one that refused.
    """
    from kew.credential_issuer import has_provisioning_key

    if brokered is not None:
        return brokered.available
    return (
        _any_env_set(required)
        or ("OPENROUTER_API_KEY" in required and has_provisioning_key())
        or (
            synthetic_credential_env_name is not None
            and synthetic_credential_env_name in required
        )
    )


def _refuse_unlaunchable_openshell(
    runner: str, *, sidecar: bool, gateway_launch: bool, direct: bool
) -> None:
    """Refuse an OpenShell dispatch this build has no launch path for.

    Two kinds of gap, one refusal token, both asked before a posture is
    recorded. The dispatch SHAPE
    (:func:`kew.openshell_launch.gateway_launch_refusal`): only a direct,
    non-sidecar streaming dispatch hands its whole launch to the gateway, so a
    sidecar topology, a flow that runs its agent as a container argv and a
    Switchyard transport whose endpoint this runtime cannot deliver have
    nowhere to go. The RUNNER
    (:func:`kew.openshell_launch.require_launchable_runner`): a program for the
    in-sandbox launcher to exec, and a packaged egress policy to create the
    sandbox under.

    Shape first: a runner with no program on a flow with no launch path should
    hear about the flow, which is the fact that will still be true after the
    runner is added.
    """
    from kew.openshell_launch import (
        OpenShellLaunchRefused,
        gateway_launch_refusal,
        require_launchable_runner,
    )

    shape = gateway_launch_refusal(
        sidecar=sidecar, gateway_launch=gateway_launch, direct=direct
    )
    if shape is not None:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: {shape}", reason=REFUSAL_OPENSHELL_UNAVAILABLE
        )
    try:
        require_launchable_runner(runner)
    except OpenShellLaunchRefused as exc:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: {exception_text(exc)} kew cannot contain this agent "
            'with it. Set [sandbox] runtime = "docker" (or "podman") to '
            "dispatch this runner now.",
            reason=REFUSAL_OPENSHELL_UNAVAILABLE,
        ) from exc


def _require_launchable_boundary(
    config: SandboxConfig,
    runner: str,
    *,
    sidecar: bool = False,
    gateway_launch: bool = True,
    direct: bool = True,
) -> None:
    """Refuse a boundary this build cannot actually launch ``runner`` into.

    More than the runtime NAME, because the name is only the first of three
    questions. ``openshell`` admits the runtime through
    :func:`require_launchable_runtime`, but a launch there also needs a
    dispatch shape and a runner the gateway path can serve
    (:func:`_refuse_unlaunchable_openshell`) — and each of those used to
    resolve ``sandboxed`` here, record that posture on the chain, and only then
    be refused at the gateway seam. Asked here so the refusal arrives before
    any sandboxed posture is attested, with the runtime's own cause.

    The skip flag is not consulted, for the reason the runtime check does not
    consult it either: naming a runtime is asking for containment, and
    answering that with a host run is the silent downgrade
    :func:`_resolve_effective_sandbox` exists to prevent.
    """
    try:
        require_launchable_runtime(config.runtime)
    except RuntimeBackendUnavailable as exc:
        raise SandboxRuntimeUnavailable(
            f"Sandbox: {exc} kew cannot contain this agent with it. Set "
            '[sandbox] runtime = "docker" (or "podman") to dispatch now.',
            reason=REFUSAL_OPENSHELL_UNAVAILABLE,
        ) from exc
    if config.runtime == OPENSHELL_RUNTIME:
        _refuse_unlaunchable_openshell(
            runner, sidecar=sidecar, gateway_launch=gateway_launch, direct=direct
        )


def uncontainable_refusal(uncontainable: str) -> str:
    """Why a runner that can never be contained is refused without the skip."""
    return (
        f"Sandbox: {uncontainable}. kew will not run it while claiming "
        "containment it does not have. Set [sandbox] "
        "dangerously_skip_containment = true to run this agent on your "
        "machine with no container boundary, or route to a runner that "
        "can be contained."
    )


def _offered_runtime(
    config: SandboxConfig,
    runner: str,
    *,
    sidecar: bool,
    gateway_launch: bool,
    direct: bool,
    host_only: bool,
) -> str | None:
    """The runtime kew would actually put THIS dispatch inside, else ``None``.

    The question :meth:`~kew.runners.protocol.AgentRunner.containment_posture`
    is asked with, and it is NOT the configured runtime name. A runner may
    decline its own posture in favour of kew's boundary on a runtime
    (``codex`` under ``openshell``, epic node N10), and handing it a name kew
    is not going to launch into turns that deferral into a refusal of a run
    the runner contains perfectly well: the remediation flows
    (``kew review``, ``kew resolve``, ``kew ci-fix``, an epic's plan run) and
    every sidecar topology reach this resolver with ``openshell`` configured
    and no launch path on it (PR #2461 review round 2).

    ``None`` — kew is offering nothing — on each way that can be true:

    - ``enabled = false``: containment is off by request, which is the one
      setting that cannot be answered by holding a boundary anyway.
    - ``host_only``: a preserved continuation. Its worktree already exists on
      the host and the container path cannot take it without replacing the
      very checkout the operator asked to keep, which is why
      :func:`kew.dispatch_core._ensure_host_only_sandbox_posture` refuses a
      sandboxed decision there. Without this arm a ``codex`` continuation on
      this runtime resolved ``sandboxed`` and was then refused
      ``continuation_host_only``, where the same continuation on docker ran
      ``native`` (PR #2461 review round 3).
    - a runtime this build cannot launch an agent into at all.
    - on the gateway runtime, a dispatch SHAPE with no launch path
      (:func:`~kew.openshell_launch.gateway_launch_refusal`) or a runner with
      no program and no packaged policy on it
      (:func:`~kew.openshell_launch.launchable_runner_refusal`) — the same two
      questions :func:`_require_launchable_boundary` asks a few lines down, in
      their reason-returning spelling, so the two cannot disagree.
    - a runtime that is not on this HOST, but ONLY when the operator opted
      into the host path with ``dangerously_skip_containment``. Then there is
      nothing for the runner to defer TO, and a runner holding its own boundary
      keeps it (``native``) rather than running with none at all. Without that
      opt-in the configured name is answered as configured: the operator asked
      for containment by naming the runtime, so ``codex`` under ``openshell``
      on a host with no gateway CLI falls through to the refusal naming the
      missing runtime instead of quietly running ``native`` (#2623). Probed
      LAST, after the facts that cost nothing, so detection happens only on a
      dispatch kew was otherwise about to contain.

    An UNCERTIFIED boundary is deliberately not one of those arms. That
    boundary exists on this host and the operator can prove it with a shipped
    command, so a runner that declines its own posture there is refused by
    :func:`require_certified_boundary` rather than quietly answered with a host
    run — which is the whole point of naming the runtime.

    Answering ``None`` here drops nothing: a runner with no posture of its own
    falls through to that same gate unguarded and is refused there, with its
    own cause, exactly as before.
    """
    offered = declared_offered_runtime(
        config,
        runner,
        sidecar=sidecar,
        gateway_launch=gateway_launch,
        direct=direct,
        host_only=host_only,
    )
    if offered is None:
        return None
    if config.dangerously_skip_containment and detect_runtime_cached(config) is None:
        return None
    return offered


def declared_offered_runtime(
    config: SandboxConfig,
    runner: str,
    *,
    sidecar: bool,
    gateway_launch: bool,
    direct: bool,
    host_only: bool = False,
) -> str | None:
    """:func:`_offered_runtime` from code and config alone, probing nothing.

    Every arm but the last: the host-detection one, which only the launching
    process can answer. The prompt preview (:mod:`kew.prompt_previews`) asks
    this so it puts the runner's native-containment question the way a launch
    does without starting a process.
    """
    from kew.openshell_launch import (
        gateway_launch_refusal,
        launchable_runner_refusal,
    )

    if config.enabled is False or host_only:
        return None
    if config.runtime not in LAUNCHABLE_RUNTIMES:
        return None
    if config.runtime == OPENSHELL_RUNTIME and (
        gateway_launch_refusal(
            sidecar=sidecar, gateway_launch=gateway_launch, direct=direct
        )
        or launchable_runner_refusal(runner)
    ):
        return None
    return config.runtime


def _resolve_effective_sandbox(
    config: SandboxConfig,
    *,
    runner: str,
    synthetic_credential_env_name: str | None = None,
    store: object | None = None,
    direct: bool = True,
    sidecar: bool = False,
    gateway_launch: bool = True,
    host_only: bool = False,
) -> SandboxDecision:
    """Resolve the effective sandbox posture for ``runner``'s dispatch.

    ``runner`` is the ALREADY-RESOLVED runner name (``resolve_runner()`` runs
    first at the dispatch call site). It is required, not defaulted: the
    decision keying off runtime+credential alone is what let the #1104 opencode
    node be contained in the claude-only image because a sibling claude node's
    token happened to be in the driver env.

    Nothing here reaches host execution unless ``dangerously_skip_containment``
    is set. Every branch that once downgraded now raises instead, and the
    operator opts in by name or not at all.

    - runner declares native containment FOR THIS RUNTIME: use its hardened
      host path (posture ``"native"``), independently of image/credentials and
      independently of ``enabled`` — the runner's own boundary is not kew's to
      switch off, and this is checked first so a natively-contained runner is
      never asked for the skip flag. The declaration is asked with the runtime
      kew would actually launch THIS dispatch into (:func:`_offered_runtime`),
      so a runner that contains itself on one runtime and defers to kew's
      boundary on another (``codex`` under ``openshell``, epic node N10) falls
      through to the container branches below — but only where that boundary
      is really on offer for this dispatch.
    - ``enabled=False``: uncontained by request. Requires
      ``dangerously_skip_containment``; without it, raise. With it, posture
      ``"uncontained"`` and a banner.
    - runtime found + runner credential + a mapped image: sandbox (posture
      ``"sandboxed"``, loud banner), carrying that image on the decision.
    - runtime found but the runner has NO mapped image, or no declared
      in-container credential: raise (``SandboxImageUnmapped`` /
      ``SandboxCredentialUndeclared``) whatever ``enabled`` says, and whatever
      the skip flag says — see :func:`_require_containable_runner`. These are
      kew-side gaps the operator cannot fix by accepting risk. Never fall back
      to the claude image.
    - runtime found, runner declares a credential, but it is NOT in the
      environment and no selected sidecar supplies that credential: containers
      cannot inherit CLI login (#721). Raise, naming how to provision it — the
      machine *can* contain, so leaving containment over a fixable credential
      gap trades a solvable problem for an unsolvable one.
      ``dangerously_skip_containment`` still takes the host path if that is
      genuinely what the operator wants.
    - runtime missing: raise ``SandboxRuntimeUnavailable`` naming both ways
      forward, or — with ``dangerously_skip_containment`` — posture
      ``"uncontained"``.

    ``enabled=True`` combined with ``dangerously_skip_containment`` is rejected
    at config load as contradictory, so it cannot arrive here.

    ``host_only`` is the preserved-continuation shape, and it reaches exactly
    one branch: :func:`_offered_runtime`, so the runner posture question is
    asked with no boundary in it. Every other arm is unchanged, which is what
    keeps the continuation refusal itself intact — a runner holding no boundary
    of its own still resolves ``sandboxed`` here and is still refused
    ``continuation_host_only`` by the caller.
    """
    # Checked BEFORE `enabled`: a runner that contains itself is contained no
    # matter what the container settings say, and must not be dragged through
    # the uncontained gate. (This ordering also means `enabled = false` no
    # longer silently turns a native runner's own boundary off.)
    #
    # Asked with the runtime kew would actually put THIS dispatch inside
    # (`_offered_runtime`) rather than the configured name, so that a runner
    # whose own sandbox is weaker than kew's on this runtime can say so and be
    # contained — while a dispatch kew has no launch path for still gets the
    # no-runtime question, which is the only one with a true answer when no
    # boundary is on offer. The pure host-launch sites
    # (`kew.loops.delegate.resolve_loop_agent_posture`,
    # `kew.remediation_containment`'s fallback arm) ask that question directly.
    native_posture = runner_native_containment(
        runner,
        runtime=_offered_runtime(
            config,
            runner,
            sidecar=sidecar,
            gateway_launch=gateway_launch,
            direct=direct,
            host_only=host_only,
        ),
    )
    if native_posture is not None:
        return SandboxDecision(
            sandboxed=False,
            runtime=None,
            posture=native_posture,
            notice=None,
            image=None,
        )

    # Checked before runtime detection, because whether Docker happens to be
    # installed says nothing about a runner that cannot be contained anyway.
    # Without this branch such a runner hits the image/credential admission
    # gate, which fails closed EVEN WITH the skip flag — correct for a gap kew
    # can still close, wrong here, where there is nothing to wait for. That
    # combination made an uncontainable runner simply undispatchable on any
    # host with a runtime, and the error named a fix that would never arrive.
    uncontainable = runner_uncontainable(runner)
    if uncontainable is not None:
        if not config.dangerously_skip_containment:
            raise SandboxRuntimeUnavailable(
                uncontainable_refusal(uncontainable),
                reason=REFUSAL_RUNTIME_UNAVAILABLE,
            )
        return _uncontained_decision(uncontainable)

    if config.enabled is False:
        if not config.dangerously_skip_containment:
            raise SandboxRuntimeUnavailable(
                "Sandbox: [sandbox] enabled = false runs the agent on the host "
                "with no container boundary, which now has to be said out loud: "
                "also set dangerously_skip_containment = true. Removing "
                "`enabled = false` restores containment.",
                reason=REFUSAL_RUNTIME_UNAVAILABLE,
            )
        return _uncontained_decision("[sandbox] enabled = false")

    # Checked before the launch-path gate and before the probe this resolution
    # acts on, and deliberately in that order: the certification is a statement
    # about the BOUNDARY, which exists on this host whether or not this build
    # can launch THIS dispatch into it, and it is the OpenShell refusal an
    # operator can clear with a shipped command. A dispatch that got past this
    # gate and then hit a launch-path refusal (a runner, topology or flow the
    # gateway path cannot serve) has been told two true things in the order it
    # can act on them; the reverse order would hide the egress gate behind a
    # build limitation and leave it untested on those shapes (#2319).
    #
    # `_offered_runtime` above does consult `detect_runtime_cached` — a cached
    # `shutil.which`, which refuses nothing and attests nothing — and that is
    # not this ordering: it only does so under `dangerously_skip_containment`,
    # where a runner holding its own boundary keeps it rather than running
    # with none; without the opt-in an absent runtime reaches this gate and
    # the refusals below exactly as it does for a runner with no boundary.
    require_certified_boundary(config, runner=runner, store=store)

    # Checked where the container runtime starts to matter, and before it is
    # probed: a validated runtime kew has no LAUNCH path for could be installed
    # on this host and still not be usable, and sandboxing on it would claim a
    # boundary kew cannot actually hold. The skip flag is not consulted: the
    # operator asked for containment by naming a runtime, and answering that
    # with a host run is the silent downgrade this function exists to prevent.
    #
    # `require_launchable_runtime`, NOT `require_backed_runtime`: on
    # `openshell` kew owns the gateway client and the sandbox it creates for
    # the length of one dispatch (`kew.openshell_launch`, epic node N6) while
    # still being unable to answer, from a sweep holding only a run row,
    # whether that sandbox is running. Gating admission on the second fact
    # refused a boundary kew really can hold. The rest of the same question is
    # asked there too — this dispatch's SHAPE (a sidecar topology, a flow that
    # launches its agent as a container argv, or a Switchyard transport whose
    # endpoint the gateway exec cannot deliver) and this RUNNER's program and
    # policy — so nothing the gateway seam would refuse records a sandboxed
    # posture first.
    _require_launchable_boundary(
        config, runner, sidecar=sidecar, gateway_launch=gateway_launch, direct=direct
    )

    resolved = detect_runtime_cached(config)
    if resolved is not None:
        from kew.openshell_broker import brokered_admission

        # The BARE configured name, matching what a sandboxed decision carries
        # into the ledger's ``runtime`` field — a refusal and a success must
        # describe the same host in the same currency.
        image = _require_containable_runner(config, runner, runtime=config.runtime)
        required = runner_credential_env_vars(runner)
        # Asked ONCE, and only on the brokered runtime (``None`` elsewhere). The
        # refusal below explains this same answer rather than putting a second
        # question to the gateway, whose reply can disagree with the one that
        # refused — a provider created between the two calls used to produce a
        # refusal saying the gateway holds the provider it was refusing over.
        brokered = brokered_admission(
            config,
            runner,
            direct=direct,
            sidecar=sidecar,
        )
        if not _runner_credential_available(
            required, synthetic_credential_env_name, brokered
        ):
            return _credential_missing_outcome(
                config, runner, required, image=image, brokered=brokered
            )
        # Refuse here, before any sandboxed posture is recorded, so the
        # refusal is attested with its own cause (PR #1984 review round 4).
        _require_parseable_inherited_proxies(
            _proxy_variable_actions(config.proxy_environment)["inherited"],
            runtime=config.runtime,
            image=image,
        )
        label = _runtime_label(resolved, config)
        # Carry the BARE runtime name, not the shutil.which() path: argv[0]
        # goes through the subprocess allowlist, which matches names only
        # (the resolved path is used just for the orbstack/podman label).
        return SandboxDecision(
            sandboxed=True,
            runtime=config.runtime,
            posture="sandboxed",
            notice=f"Sandbox: on ({label})",
            image=image,
        )

    # Runtime missing. This is the branch that used to downgrade in silence:
    # `auto` on a machine without Docker ran the agent on the host and said so
    # in one line. Naming the two ways forward is the whole point — an operator
    # who wants the host path can have it, by asking for it.
    if not config.dangerously_skip_containment:
        install = (
            "Install the OpenShell CLI (`openshell` on PATH)"
            if config.runtime == OPENSHELL_RUNTIME
            else "Install Docker or OrbStack"
        )
        raise SandboxRuntimeUnavailable(
            f"Sandbox runtime {config.runtime!r} not found, so this agent "
            f"cannot be contained. {install} to contain it, or "
            "set [sandbox] dangerously_skip_containment = true to run it "
            "directly on your machine with no boundary."
        )

    return _uncontained_decision(f"runtime {config.runtime!r} not found")


def _daemon_cpu_count(runtime: str) -> int | None:
    """Ask the selected daemon for its CPU count; ``None`` when it cannot say.

    Asked once per launch, never cached: a daemon can be restarted, resized or
    switched (Docker context) between launches, and a cached answer from the
    old one is exactly the value the new one rejects. Only a positive integer
    counts as an answer; a non-zero exit, a timeout, a missing binary,
    malformed output, zero or a negative number are all "no answer". The
    template is per runtime because Podman nests the field under ``Host``.
    """
    from kew.progress import _warn

    template = _DAEMON_CPU_COUNT_TEMPLATES.get(
        runtime, _DAEMON_CPU_COUNT_TEMPLATES["docker"]
    )
    degraded = f"capping the container at {_CPU_LIMIT_UNKNOWN_DAEMON} CPU"
    try:
        result = subprocess_utils.run(
            [runtime, "info", "--format", template],
            check=False,
            timeout=_DAEMON_CPU_COUNT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # ValueError: the subprocess allowlist refused the runtime binary.
        _warn(
            f"Sandbox: could not ask {runtime} for its CPU count "
            f"({exception_text(exc)}); {degraded}"
        )
        return None
    if result.returncode != 0:
        _warn(f"Sandbox: {runtime} info exited {result.returncode}; {degraded}")
        return None
    raw = (result.stdout or "").strip()
    try:
        count = int(raw)
    except ValueError:
        _warn(
            f"Sandbox: {runtime} reported CPU count {raw!r}, not a number; {degraded}"
        )
        return None
    if count < 1:
        _warn(f"Sandbox: {runtime} reported CPU count {count}; {degraded}")
        return None
    return count


def resource_caps(config: SandboxConfig | None = None) -> ResourceCaps:
    """The effective per-container caps for *config*, ceilings when it is unset.

    ``None`` — and an unset ``max_memory`` — resolve to the module constants
    themselves rather than to a value derived from them. That is what makes
    "unset keeps today's caps exactly" true by construction instead of by
    arithmetic that happens to agree, and it leaves each constant
    independently authoritative: the live cgroup-OOM probe in
    ``tests/test_sandbox_containment.py`` lowers ``_MEMORY_LIMIT`` ALONE and
    needs the tmpfs mounts left above it, because a ``/tmp`` resized with the
    cap would answer its 32 MiB write with ``ENOSPC`` instead of the OOM kill
    it exists to prove. Derivation belongs to the configured branch, where the
    operator's cap is the only number there is. ``tests/test_sandbox.py`` pins
    the two branches to the same answer at the default, so they cannot drift
    apart.
    """
    requested_cpus = None if config is None else config.max_cpus
    cpus = int(_CPU_LIMIT) if requested_cpus is None else requested_cpus
    requested = None if config is None else config.max_memory
    if requested is None:
        return ResourceCaps(
            memory=_MEMORY_LIMIT,
            cpus=cpus,
            home_tmpfs=_HOME_TMPFS_SIZE,
            tmp_tmpfs=_TMP_TMPFS_SIZE,
        )
    return ResourceCaps(
        memory=requested,
        cpus=cpus,
        home_tmpfs=derived_tmpfs_size(
            requested, _HOME_TMPFS_SIZE, _HOME_TMPFS_MEMORY_SHARE
        ),
        tmp_tmpfs=derived_tmpfs_size(
            requested, _TMP_TMPFS_SIZE, _TMP_TMPFS_MEMORY_SHARE
        ),
    )


def resource_limit_args(
    runtime: str = "docker", caps: ResourceCaps | None = None
) -> list[str]:
    """The hard resource caps every kew container carries (S3, #1431).

    One list, shared by the agent and extraction argvs, because a cap that
    exists on only one of them is a cap an attacker picks the other container
    to avoid. Unlike the six hardening flags those two functions assert equal
    by test, these are new surface with no byte-stability history, so they are
    factored rather than duplicated — the drift they would otherwise develop is
    the whole failure mode.

    *caps* is the effective cap set (:func:`resource_caps`), which an operator
    may have LOWERED from the ceilings in this module; omitted, it is the
    ceilings. Lowering travels as an argument rather than as module state so
    two concurrent launches under different configs cannot read each other's.

    The CPU ceiling — lowered or not — is clamped to the daemon's reported CPU
    count, asked once per launch. The client's ``os.cpu_count()`` is never
    used: a Docker Desktop, OrbStack, remote or Podman daemon can be a smaller
    machine than the client, and a cap above the daemon's count is rejected
    outright, which means no container at all. When the daemon cannot be asked
    the cap is one CPU, the only value every daemon accepts; the launch is
    slower, never refused. The final value is never below one CPU.

    What is NOT here, and why:

    * the checkout quota, the transcript byte cap and the wall-clock kill —
      Docker has no flag for any of them (a bind mount has no quota, stdout has
      no size, ``docker run`` has no deadline), so :class:`ContainerHandle`
      enforces all three host-side while it streams;
    * ``--storage-opt size=`` — it needs an xfs/devicemapper backing store and
      is silently ignored on the overlay2 default, which is a cap that reports
      success while doing nothing (rule 4).
    """
    effective = resource_caps() if caps is None else caps
    daemon_cpu_count = _daemon_cpu_count(runtime)
    cpu_limit = (
        _CPU_LIMIT_UNKNOWN_DAEMON
        if daemon_cpu_count is None
        else str(max(1, min(effective.cpus, daemon_cpu_count)))
    )

    return [
        "--memory",
        effective.memory,
        # Equal to --memory: swap off. See the constant's note — a swap
        # allowance converts a memory bomb into host IO starvation.
        "--memory-swap",
        effective.memory_swap,
        "--cpus",
        cpu_limit,
        "--pids-limit",
        str(_PIDS_LIMIT),
        "--ulimit",
        f"nofile={_ULIMIT_NOFILE}",
        "--ulimit",
        f"fsize={_ULIMIT_FSIZE}",
        "--ulimit",
        f"core={_ULIMIT_CORE}",
    ]


def worker_pool_environment_args(limit_args: list[str]) -> list[str]:
    """``-e NAME=<n>`` for every test-runner pool size, *n* from ``--cpus``.

    Reads the ``--cpus`` value *limit_args* actually carries, so the pool can
    never be sized to a cap the container does not have — including the
    one-CPU fallback for a daemon that cannot be asked. No ``--cpus``, no
    sizing. Agent containers only: extraction runs no test suite.
    """
    if "--cpus" not in limit_args[:-1]:
        return []
    return _worker_pool_assignments(limit_args[limit_args.index("--cpus") + 1])


def _worker_pool_assignments(cpu_limit: str) -> list[str]:
    workers = str(max(1, int(Decimal(cpu_limit))))
    return [
        argument
        for name in TEST_WORKER_ENV_NAMES
        for argument in ("-e", f"{name}={workers}")
    ]


class SandboxHostCapacityError(SandboxRuntimeUnavailable):
    """The host cannot hold this run: refused before the container starts."""


def check_host_capacity(
    checkout_dir: Path | None,
    *,
    anchor: Path | None = None,
    free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
    quota_bytes: int = CHECKOUT_QUOTA_BYTES,
    quota_entries: int = CHECKOUT_QUOTA_ENTRIES,
    runtime: str | None = None,
    image: str | None = None,
) -> None:
    """Refuse a launch the watchdog would kill at its first tick.

    A host already under the free-space floor, or a checkout already over
    the quota, breaches at t=0 — before the container exists — and the run
    would then be reported as a cap kill it never had. Refusing here names
    the operator's condition instead of blaming the agent.
    """
    probe = checkout_dir if checkout_dir is not None else anchor
    if probe is not None:
        try:
            free = shutil.disk_usage(probe).free
        except OSError as exc:
            from kew.progress import _warn

            _warn(
                f"free-space preflight of {probe} failed: "
                f"{exception_text(exc)}; floor not checked"
            )
            free = None
        if free is not None and free < free_floor:
            raise SandboxHostCapacityError(
                f"Sandbox refused: host free space {free} bytes at {probe} is "
                f"below the {free_floor} byte floor (free it, or lower the cap in "
                "sandbox.py)",
                reason=REFUSAL_HOST_FREE_SPACE,
                runtime=runtime,
                image=image,
            )
    if checkout_dir is None or not checkout_dir.is_dir():
        return
    used, entries = checkout_usage(
        checkout_dir, stop_at_bytes=quota_bytes, stop_at_entries=quota_entries
    )
    if entries > quota_entries or used > quota_bytes:
        raise SandboxHostCapacityError(
            f"Sandbox refused: checkout {checkout_dir} already exceeds the quota "
            f"({used} bytes / {entries} entries vs {quota_bytes} / {quota_entries})",
            reason=REFUSAL_CHECKOUT_OVER_QUOTA,
            runtime=runtime,
            image=image,
        )


def check_runtime_memory_capacity(
    runtime: str, config: SandboxConfig | None = None
) -> None:
    """Warn when Docker's available memory is below the per-container cap.

    Measured against the EFFECTIVE cap, so an operator who lowered
    ``max_memory`` to fit the VM stops being warned about a cap they removed —
    and one who lowered it but not enough is still warned.

    Skipped entirely on the OpenShell runtime: there is no per-container cap to
    measure (:func:`_attested_caps`) and no daemon to ask for a total, so the
    preflight could only run ``openshell info`` — a subcommand the CLI does not
    have — and print a "docker info did not return MemTotal" line about a cap
    that was never going to be set (PR #2401 review).
    """
    if runtime == OPENSHELL_RUNTIME:
        return
    from kew.progress import _warn

    try:
        # By tool NAME: the allowlist refuses the which()-resolved absolute
        # path detect_runtime() hands out (same trap as ContainerHandle.kill).
        result = subprocess_utils.run(
            [Path(runtime).name, "info", "--format", "{{.MemTotal}}"],
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise ValueError("docker info failed")
        available = int((result.stdout or "").strip())
        if available <= 0:
            raise ValueError("invalid MemTotal")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        _warn(
            "Sandbox memory preflight skipped: docker info did not return "
            "MemTotal; continuing."
        )
        return
    limit = parse_memory_size(resource_caps(config).memory)
    if limit > available:
        _warn(
            f"Sandbox memory cap {limit / 1024**3:.2f} GiB exceeds runtime "
            f"memory {available / 1024**3:.2f} GiB; continuing."
        )


def _new_container_cidfile(slug: str) -> Path:
    """Return a collision-resistant host path for Docker's container ID.

    Call-local, and small: the file lives only while its container starts, so
    it stays in the OS temp directory rather than under the work root, which
    may sit under a home directory a contained child cannot write.
    """
    token = uuid.uuid4().hex
    return Path(tempfile.gettempdir()) / f"kew-{slug}-{os.getpid()}-{token}.cid"


def _container_network_args(network: str) -> list[str]:
    if network == "open":
        return []
    if network == "allowlist":
        return ["--network", ALLOWLIST_NETWORK]
    return ["--network", network]


def _container_identity_args(operation: str, slug: str) -> list[str]:
    if operation not in {"run", "create"}:
        raise ValueError("container operation must be run or create")
    if operation == "run":
        return ["--rm", "--cidfile", str(_new_container_cidfile(slug))]
    return []


def _container_launch_environment_args(
    runner: str,
    inference_transport: ResolvedInferenceTransport | None,
    credential_environment: Mapping[str, str] | None,
) -> list[str]:
    if credential_environment is None:
        return _container_environment_args(runner, inference_transport)
    return [
        argument
        for name, value in credential_environment.items()
        for argument in ("-e", f"{name}={value}")
    ]


def _read_only_mount_args(mounts: Sequence[tuple[Path, str]]) -> list[str]:
    """``-v host:container:ro`` for each extra mount, refused unless it is safe.

    The host side must be absolute, and the container side must sit strictly
    beneath the checkout mount: an extra bind is for dependencies the agent
    reads under ``/work``, never for shadowing the image. Neither side may
    carry ``:`` or ``,``, which the ``-v`` syntax would read as options.
    """
    args: list[str] = []
    for host, container in mounts:
        host_text = str(host)
        if not Path(host).is_absolute():
            raise ValueError(f"read-only mount source is not absolute: {host_text}")
        if not container.startswith(WORK_MOUNT_PATH + "/") or ".." in container.split(
            "/"
        ):
            raise ValueError(
                f"read-only mount target is not beneath {WORK_MOUNT_PATH}: {container}"
            )
        if any(ch in host_text + container for ch in ":,"):
            raise ValueError("read-only mount paths may not contain ':' or ','")
        args += ["-v", f"{host_text}:{container}:ro"]
    return args


def build_run_argv(
    *,
    runtime: str,
    image: str,
    checkout_dir: Path,
    slug: str,
    network: str,
    runner: str,
    inference_transport: ResolvedInferenceTransport | None = None,
    entrypoint: str | None = None,
    command: list[str] | None = None,
    operation: str = "run",
    container_name: str | None = None,
    owner: str | None = None,
    credential_environment: Mapping[str, str] | None = None,
    proxy_environment: str = "none",
    caps: ResourceCaps | None = None,
    read_only_mounts: Sequence[tuple[Path, str]] = (),
) -> list[str]:
    """Return the hardened docker-run argv for a sandboxed dispatch.

    Only mounts checkout_dir, plus any ``read_only_mounts`` the caller names
    (``(host_dir, container_path)``, bound ``:ro`` beneath the checkout — the
    reviewer's host-built frontend dependencies, #2559). No host credential
    FILES, ~/.claude, or dispatch.db. Agent credentials are passed by name
    only — values inherited from the caller's env; an unset name stays unset
    in the container.

    ``runner`` keys the credential env allowlist (#1146) and is REQUIRED, with
    no default: the injected names are exactly
    ``runner_credential_env_vars(runner)``, so a contained run sees its own
    provider's credentials and no other runner's. A default here would silently
    re-create the claude-only injection this replaced — an opencode container
    handed ``CLAUDE_CODE_OAUTH_TOKEN`` can spend the operator's Anthropic
    subscription, which is exactly the surface the allowlist exists to close.
    Widening a runner's declaration must not widen HOW credentials travel: the
    by-name-only invariant above holds for every runner.

    ``inference_transport`` adds only the selected runner's proven endpoint
    override. Endpoint origins are validated non-secrets and may therefore be
    assigned in argv; credentials remain names only.

    ``proxy_environment`` defaults to ``"none"`` and blanks all ten controlled
    proxy variables. This overrides Docker client configuration and Podman host
    forwarding. ``"inherit"`` forwards only the six inheritable names present in
    the host environment; the other four are always blanked.

    ``command`` (the args after the image) becomes the container command, which
    the image entrypoint receives as ``"$@"`` — the host uses it to pass claude
    flags. ``entrypoint`` overrides the image ENTRYPOINT (used by
    ``kew sandbox smoke`` to exercise launch/mount/prompt-file without invoking
    claude). See ``docs/sandbox-entrypoint-contract.md`` for the canonical text.

    ``slug`` is the branch-safe work-item id (:func:`kew.ids.branch_slug`) — it
    names the container ``kew-<slug>``. Lowercased Linear ids (``sfd-12``) and
    numeric GitHub ids (``42``) both stay inside the docker container-name
    charset.

    Resource caps come from :func:`resource_limit_args`, and the two tmpfs
    mounts carry explicit sizes (#1431). Containment without them stops the
    agent leaving the box but not taking the host down from inside it.

    ``caps`` is the effective cap set (:func:`resource_caps` of the operator's
    ``[sandbox]`` block, #2360), which may be LOWER than this module's
    ceilings and is never higher. Omitted, it is the ceilings — so a caller
    that forgets it gets today's argv rather than an uncapped container.

    ``openshell`` is refused here rather than argv-ified. This is THE
    argv-build site, so it is the one place that can state the invariant for
    every caller: a gateway sandbox is reached through
    :mod:`kew.openshell_launch`, and the ``openshell`` CLI has no ``run`` or
    ``create`` subcommand — ``runtime`` as ``argv[0]`` would hand that CLI a
    docker command line. Admission refuses every shape that gets here
    (:func:`_refuse_unlaunchable_openshell`) before a sandboxed posture is
    attested; this backstop is what makes "no docker argv on the gateway" true
    for a caller that resolved containment some other way.
    """
    if runtime == OPENSHELL_RUNTIME:
        from kew.openshell_launch import ARGV_LAUNCH_REFUSAL

        raise SandboxRuntimeUnavailable(
            f"Sandbox: {ARGV_LAUNCH_REFUSAL}",
            reason=REFUSAL_OPENSHELL_UNAVAILABLE,
            runtime=runtime,
            image=image,
        )
    effective = resource_caps() if caps is None else caps
    if credential_environment is not None and any(
        name in _PROXY_BUILD_NAMES for name in credential_environment
    ):
        raise ValueError("proxy environment assignments are not permitted")
    # "open" is the explicit full-egress opt-out: omit --network (Docker bridge).
    # "allowlist" (default) uses the pre-configured kew-allowlist Docker network;
    # Docker fails closed if that network does not exist.
    # Any other value is passed through as --network <value>.
    network_args = _container_network_args(network)
    entrypoint_args: list[str] = (
        ["--entrypoint", entrypoint] if entrypoint is not None else []
    )
    identity_args = _container_identity_args(operation, slug)
    label_args = (
        ["--label", f"{SWITCHYARD_ATTEMPT_LABEL}={_worker_filter_owner(owner)}"]
        if owner is not None
        else []
    )
    environment_args = _container_launch_environment_args(
        runner,
        inference_transport,
        credential_environment,
    )
    extra_mount_args = _read_only_mount_args(read_only_mounts)
    limit_args = resource_limit_args(runtime, effective)

    return [
        runtime,
        operation,
        *identity_args[:1],
        "--init",
        # Never resolve the image over the network at launch. kew's default
        # image tag is unqualified (``kew-agent:latest``) because nothing
        # publishes a kew agent image; an unqualified name is completed to
        # ``docker.io/library/<name>``, so without this flag a missing local
        # image is a PULL of whatever a stranger has registered under that
        # name on Docker Hub. The containment boundary comes from the image
        # this checkout built, or from nothing at all.
        "--pull=never",
        "--name",
        container_name or f"kew-{slug}",
        *identity_args[1:],
        *label_args,
        *network_args,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        # `size=` is the cap (#1431): a tmpfs is host RAM, so an unsized one is
        # an unmetered second route to the exhaustion --memory closes. No
        # `exec` option — /tmp keeps Docker's default noexec.
        "--tmpfs",
        f"/tmp:size={effective.tmp_tmpfs}",  # nosec B108 - in-container mount point, not a host temp path
        # Writable ephemeral HOME for the agent user (#742): the read-only
        # root made Claude Code's Bash tool fail shell-init (EROFS mkdir
        # ~/.claude) on every invocation — the agent could not run git or
        # tests. uid/gid/mode pin it to the image's `agent` user (uid 1001,
        # docker/agent.Dockerfile); contents die with the container.
        #
        # `exec` overrides Docker's default `noexec` for this tmpfs (#762): so
        # tooling an agent installs into HOME (uv-managed binaries, npm globals)
        # is runnable there, instead of being forced into the bind-mounted /work
        # where it litters the checkout. /tmp keeps the default noexec — only
        # HOME opts in. Still ephemeral, still uid-pinned; containment holds.
        "--tmpfs",
        (
            f"{AGENT_HOME}:exec,size={effective.home_tmpfs},"
            f"uid={_AGENT_UID},gid={_AGENT_UID},mode=0700"
        ),
        *limit_args,
        *entrypoint_args,
        "-v",
        # Anchored to an absolute path: the docker CLI expands a dot-prefixed
        # relative bind source (`./x`, `.wt/x`) against its own cwd before
        # the daemon sees it, and the launcher runs the CLI with
        # cwd=checkout_dir, so a relative checkout (the review clone at
        # `.worktrees/review/pr-N`) mounted a fresh empty directory nested
        # inside itself — the container saw no prompt file and the
        # entrypoint refused with exit 64 (#1663).
        f"{Path(checkout_dir).absolute()}:{WORK_MOUNT_PATH}",
        *extra_mount_args,
        "-w",
        WORK_MOUNT_PATH,
        # Not a credential: the guard's agent marker, as the container path
        # (#1691). Set for every runner, before the credential surface, so
        # the guard scopes its agent-only rules to this checkout in-container
        # exactly as it does on the host.
        "-e",
        f"{AGENT_MARKER_ENV}={WORK_MOUNT_PATH}",
        "-e",
        f"{STARTUP_NONCE_ENV}={uuid.uuid4().hex}",
        # Not a credential: test pools sized to --cpus, not the host (#2887).
        *worker_pool_environment_args(limit_args),
        # Reusable credential values never reach argv. Sidecar workers receive
        # one fresh synthetic client value and their verified endpoint here.
        *environment_args,
        *_container_proxy_environment_args(proxy_environment),
        image,
        *(command or []),
    ]


def build_worker_agent_exec_argv(
    *, runtime: str, worker_id: str, command: list[str]
) -> list[str]:
    """Attach agent code to one already-filtered worker bootstrap."""
    return [
        runtime,
        "exec",
        "-e",
        f"{STARTUP_NONCE_ENV}={uuid.uuid4().hex}",
        _worker_filter_container_id(worker_id, "worker_id"),
        "/usr/local/bin/kew-agent-entrypoint",
        *command,
    ]


def build_extract_argv(
    *,
    runtime: str,
    image: str,
    checkout_dir: Path,
    artifact_dir: Path,
    slug: str,
    base_sha: str,
    uid: int,
    gid: int,
) -> list[str]:
    """The container-run argv for ONE extraction (W1.1 D3, contract element 6).

    A second container over the finished checkout, not a change to the agent
    entrypoint: extraction is additive, can fail without touching the run, and
    stays identical whichever runner produced the work.

    It is deliberately *narrower* than :func:`build_run_argv`, and every
    difference is a reduction:

    * ``--network none``. The agent needs egress; extraction needs none at all,
      so it gets none rather than the allowlist.
    * **No runner credential environment.** Nothing here reads a model.
      Proxy variables follow the explicitly supplied dispatch policy, including
      proxy userinfo when the operator selects ``inherit``.
    * **The checkout is mounted read-only.** Extraction reads committed objects
      and writes only to ``/artifact``.
    * ``kew-extract-<slug>``, not ``kew-<slug>``: a name collision with the
      agent's own container would make ``docker kill`` ambiguous about which of
      the two it stops.

    The hardening flags it *shares* — ``--rm``, ``--cap-drop ALL``,
    ``no-new-privileges``, read-only root, a sized tmpfs ``/tmp`` — are asserted
    equal to ``build_run_argv``'s by test rather than factored into a common
    helper, because that function's argv is contractually byte-stable and a
    refactor to share five flags is not worth reordering it. The resource caps
    (#1431) are the exception: they come from :func:`resource_limit_args` in
    both, because a cap present on only one container is a cap an attacker
    avoids by choosing the other one.

    ``uid``/``gid`` are the HOST's. The artifact is written for the host to
    read, so writing it as anyone else means either a dir the host cannot read
    or one loosened until it can. Matching uid also settles git's dubious-
    ownership check on the mounted checkout instead of working around it.
    ``runtime`` stays the BARE name from config for the subprocess allowlist —
    the same #1017 invariant ``build_run_argv`` documents.
    """
    if not _FULL_OID.fullmatch(base_sha):
        # argv is a list, so there is no shell to inject into — but the producer
        # takes this as $1, and a value beginning with "-" becomes an OPTION to
        # the git commands it runs. A resolved oid is what every caller has.
        raise ValueError(f"base must be a full hex object id, got {base_sha!r}")
    return build_checkout_program_argv(
        runtime=runtime,
        image=image,
        checkout_dir=checkout_dir,
        artifact_dir=artifact_dir,
        slug=f"extract-{slug}",
        program=[EXTRACT_ENTRYPOINT, base_sha, EXTRACT_MOUNT_PATH],
        uid=uid,
        gid=gid,
    )


def build_checkout_program_argv(
    *,
    runtime: str,
    image: str,
    checkout_dir: Path,
    slug: str,
    program: list[str],
    uid: int,
    gid: int,
    checkout_writable: bool = False,
    artifact_dir: Path | None = None,
    scratch_paths: Sequence[str] = (),
) -> list[str]:
    """Run a program over a checkout without network or runner credentials.

    Extraction supplies its output mount. Verification needs only the checkout.
    Only operator configuration may request a writable checkout.
    *scratch_paths* are in-image paths mounted as small writable tmpfs over
    the read-only root (:data:`CHECK_IMAGE_SCRATCH_PATHS`).
    These launches take no proxy policy.
    Every proxy variable receives an empty assignment: they carry
    ``--network none``, so a forwarded proxy value can never be used and only
    puts its embedded credentials inside the container (governed review round
    4, 2026-09-21).
    """
    if not program:
        raise ValueError("checkout program must not be empty")
    artifact_mount = (
        ["-v", f"{artifact_dir.absolute()}:{EXTRACT_MOUNT_PATH}"]
        if artifact_dir is not None
        else []
    )
    mode = "rw" if checkout_writable else "ro"
    return [
        runtime,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        f"kew-{slug}",
        "--cidfile",
        str(_new_container_cidfile(slug)),
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        f"/tmp:size={_TMP_TMPFS_SIZE}",  # nosec B108 - in-container tmpfs
        *(
            arg
            for path in scratch_paths
            for arg in ("--tmpfs", f"{path}:size={_SCRATCH_TMPFS_SIZE},mode=1777")
        ),
        *resource_limit_args(runtime),
        "--user",
        f"{uid}:{gid}",
        "-e",
        "HOME=/tmp",  # nosec B108 - in-container tmpfs, not a host temp path
        *_container_proxy_environment_args("none", _PROXY_BUILD_NAMES),
        "--entrypoint",
        program[0],
        "-v",
        f"{checkout_dir.absolute()}:{WORK_MOUNT_PATH}:{mode}",
        *artifact_mount,
        "-w",
        WORK_MOUNT_PATH,
        image,
        *program[1:],
    ]


def checkout_usage_bytes(root: Path, *, stop_at: int | None = None) -> int:
    """Total size of the regular files under *root*, in bytes."""
    return checkout_usage(root, stop_at_bytes=stop_at)[0]


def checkout_usage(
    root: Path,
    *,
    stop_at_bytes: int | None = None,
    stop_at_entries: int | None = None,
) -> tuple[int, int]:
    """(bytes of regular files, entries of any kind) under *root*.

    ``lstat``, never ``stat``: the tree being measured is the one an agent
    writes, so following a symlink would let it point the measurement at
    something small and hide the fill underneath. Unreadable entries are skipped
    rather than raising — a quota probe that dies on one racing unlink stops
    enforcing the quota.

    The *stop_at_* bounds short-circuit as soon as a running total exceeds
    them. The caller only ever asks "is this over the quota", and a checkout
    that IS over is the most expensive possible tree to walk to the end.
    """
    total = 0
    entries = 0
    for path in root.rglob("*"):
        entries += 1
        if stop_at_entries is not None and entries > stop_at_entries:
            return total, entries
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
            if stop_at_bytes is not None and total > stop_at_bytes:
                return total, entries
    return total, entries


class _StreamGuard:
    """The three caps Docker has no flag for, measured against one live stream.

    Wall clock, transcript bytes and checkout bytes are all breached *while* a
    container runs, so they are enforced where kew is already looping over its
    output rather than by a separate watchdog that could itself die.
    :meth:`admit` is called once per line and returns the breach reason, or
    None to keep going.
    """

    def __init__(
        self,
        *,
        checkout_dir: Path | None,
        wall_clock_seconds: float,
        transcript_cap: int,
        checkout_quota: int,
        now: Callable[[], float],
        watchdog_interval: float = WATCHDOG_INTERVAL_SECONDS,
        checkout_entry_quota: int = CHECKOUT_QUOTA_ENTRIES,
        host_free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
    ) -> None:
        self._host_free_floor = host_free_floor
        self._checkout_dir = checkout_dir
        self._transcript_cap = transcript_cap
        self._checkout_quota = checkout_quota
        self._checkout_entry_quota = checkout_entry_quota
        self._now = now
        self._spent = 0
        self._watchdog_interval = watchdog_interval
        # Record the window after guard setup. Time spent constructing an
        # enforcement owner is not time that owner enforced.
        self.started_at = now()
        self._next_quota_check = self.started_at
        self._deadline = self.started_at + wall_clock_seconds

    def admit(self, line: str) -> ResourceCapObservation | None:
        self._spent += len(line.encode("utf-8"))
        if self._spent > self._transcript_cap:
            return ResourceCapObservation(
                ResourceCapCode.TRANSCRIPT,
                f"transcript exceeded {self._transcript_cap} bytes",
            )
        return None

    def timed_breach(self) -> ResourceCapObservation | None:
        """Return a time-driven breach without waiting for container output."""
        now = self._now()
        if now >= self._deadline:
            return ResourceCapObservation(ResourceCapCode.WALL_CLOCK, WALL_CLOCK_REASON)
        floor = self._free_space_breach()
        if floor is not None:
            return floor
        return self._quota_breach(now)

    def _free_space_breach(self) -> ResourceCapObservation | None:
        if self._checkout_dir is None:
            return None
        try:
            free = shutil.disk_usage(self._checkout_dir).free
        except OSError as exc:
            from kew.progress import _warn

            # No breach is reported because the probe failed, not because
            # space is fine; say so rather than default silently.
            _warn(
                f"free-space probe of {self._checkout_dir} failed: "
                f"{exception_text(exc)}; skipping"
            )
            return None
        if free >= self._host_free_floor:
            return None
        return ResourceCapObservation(
            ResourceCapCode.HOST_FREE_SPACE,
            f"host free space {free} bytes below the "
            f"{self._host_free_floor} byte floor",
        )

    def _quota_breach(self, now: float) -> ResourceCapObservation | None:
        if self._checkout_dir is None or now < self._next_quota_check:
            return None
        started = time.perf_counter()
        used, entries = checkout_usage(
            self._checkout_dir,
            stop_at_bytes=self._checkout_quota,
            stop_at_entries=self._checkout_entry_quota,
        )
        walk_seconds = time.perf_counter() - started
        self._next_quota_check = now + max(
            self._watchdog_interval,
            min(QUOTA_WALK_BACKOFF * walk_seconds, QUOTA_WALK_MAX_BACKOFF_SECONDS),
        )
        if entries > self._checkout_entry_quota:
            return ResourceCapObservation(
                ResourceCapCode.CHECKOUT_ENTRIES,
                f"checkout exceeded {self._checkout_entry_quota} entries",
            )
        if used <= self._checkout_quota:
            return None
        return ResourceCapObservation(
            ResourceCapCode.CHECKOUT_BYTES,
            f"checkout exceeded {self._checkout_quota} bytes",
        )


# ── cgroup OOM identification (#1767) ────────────────────────────────────────
# A container the kernel OOM-kills exits 137 — the same code as the wall-clock
# ``timeout -k``, as the watchdog's own ``docker kill``, and as an in-container
# SIGKILL. ``docker inspect`` would settle it (``State.OOMKilled``), but every
# sandboxed run carries ``--rm``: the container is gone before the client
# returns, and the inspect fails. The daemon's EVENT STREAM outlives the
# container, so an ``oom`` event in the run's own time window is the only
# evidence left that the memory cap, and not the clock, ended the run.
OOM_EVENT_ACTION = "oom"
KILL_EVENT_ACTION = "kill"

#: The cap reason a confirmed OOM event names. Says which limit was hit, so the
#: operator reads "raise the memory cap or shrink the workload", not "it timed
#: out" — the misdirection this probe exists to remove. Formatted at kill time
#: rather than at import, so the reason always names the limit the container
#: actually ran under.
OOM_CAP_REASON_TEMPLATE = "memory cap ({limit}) hit: OOM-killed"


#: The probe covers both sides of the daemon clock-skew range. A future
#: ``--until`` can make the CLI wait for that instant. The timeout therefore
#: exceeds the complete ten-second skew range and still bounds a wedged daemon.
OOM_PROBE_TIMEOUT_SECONDS = 12.0

#: Symmetric slack on both event-window bounds. The daemon stamps events with
#: its own clock, which can differ from the host on VM-backed runtimes.
OOM_PROBE_CLOCK_SLACK_SECONDS = 5.0

#: A daemon error is durable evidence, but stderr is untrusted and unbounded.
OOM_PROBE_DIAGNOSTIC_MAX_CHARS = 500


class OOMProbeStatus(StrEnum):
    """The three durable outcomes of asking the daemon about an OOM."""

    SEEN = "seen"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


class KillStatus(StrEnum):
    """The three outcomes of one bounded runtime kill request."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"


def _parse_container_state(stdout: object) -> dict[str, object] | None:
    """Parse one formatted daemon state response."""
    if not isinstance(stdout, str):
        return None
    state: object = None
    with contextlib.suppress(TypeError, ValueError):
        state = json.loads(stdout)
    if not isinstance(state, dict) or not isinstance(state.get("Running"), bool):
        return None
    return state


@dataclass(frozen=True)
class OOMProbeResult:
    """A bounded daemon-probe outcome."""

    status: OOMProbeStatus
    diagnostic: str = ""


def oom_cap_reason(limit: str | None = None) -> str:
    """The cap reason a confirmed OOM event names, under the current limit.

    *limit* is read off the launch argv by the caller, because the cap an
    operator lowered (#2360) is the cap the kernel enforced: naming this
    module's ceiling instead would tell the operator their 3 GiB container
    died at 8 GiB. Falls back to the ceiling when the argv is unavailable —
    the same value this reported before there was anything to lower.
    """
    return OOM_CAP_REASON_TEMPLATE.format(limit=limit or _MEMORY_LIMIT)


def _resource_cap_line(reason: str) -> str:
    """The terminal stdout line a capped run ends on.

    Shaped like the container's own stream events so the ordinary derivation
    folds it into the run's evidence as a named terminal type. A cap that killed
    a container and said nothing would read downstream as an agent that merely
    went quiet — the exact silent-clean-default this project keeps paying for.
    """
    return json.dumps({"type": RESOURCE_CAP_EVENT, "reason": reason}) + "\n"


def _readline_bytes(stream: IO[str]) -> str:
    """Read at most MAX_LINE_BYTES from a text pipe's binary buffer."""
    if isinstance(stream, io.TextIOWrapper):
        raw = stream.buffer.readline(MAX_LINE_BYTES)
        assert isinstance(raw, bytes)
        return raw.decode("utf-8", errors="replace")
    return stream.readline(MAX_LINE_BYTES)


def _read_stdout_on_demand(
    stdout: IO[str],
    read_requests: queue.Queue[None],
    items: queue.Queue[str | BaseException | None],
) -> None:
    """Perform one blocking read for each consumer request."""
    try:
        while True:
            read_requests.get()
            line = _readline_bytes(stdout)
            items.put(line or None)
            if not line:
                return
    except BaseException as exc:
        items.put(exc)


def _wait_for_stream_item(
    guard: _StreamGuard,
    items: queue.Queue[str | BaseException | None],
    interval: float,
) -> tuple[str | BaseException | None, ResourceCapObservation | None]:
    """Wait for one read result while continuing time-driven cap checks."""
    while True:
        reason = guard.timed_breach()
        if reason is not None:
            return None, reason
        try:
            return items.get(timeout=interval), None
        except queue.Empty:
            continue


def bounded_pipe_lines(
    stream: IO[str],
    *,
    wall_clock_seconds: float,
    end_workload: Callable[[str], None],
    transcript_cap: int = TRANSCRIPT_BYTE_CAP,
    now: Callable[[], float] = time.monotonic,
    watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
) -> Iterator[str]:
    """Stream a pipe no :class:`ContainerHandle` owns, under the caps that fit it.

    The handle enforces the wall clock and the transcript cap for a runtime
    client it launched. A plain ``openshell sandbox exec`` pipe has neither,
    and the two failures that leaves open are ones this project has already
    paid for on docker: a workload that stays ALIVE AND SILENT holds the reader
    forever, because a blocking ``readline`` has no deadline; and a workload
    that floods ends with the host buffering whatever the reader refused to
    take off the pipe.

    So the read is output-independent — the deadline is checked while a read
    is outstanding, on the watchdog interval, through the same guard the handle
    uses — and EITHER breach ends the workload through *end_workload* before
    this generator stops, which is what the docker path's kill does on the same
    breach. The breach reason is passed to that callback and yielded as one
    :data:`RESOURCE_CAP_EVENT` line, so the transcript the caller keeps says
    why it ends rather than merely ending.
    """
    from kew.progress import _warn

    guard = _StreamGuard(
        checkout_dir=None,
        wall_clock_seconds=wall_clock_seconds,
        transcript_cap=transcript_cap,
        checkout_quota=CHECKOUT_QUOTA_BYTES,
        now=now,
        watchdog_interval=watchdog_interval_seconds,
    )

    def ended(breach: ResourceCapObservation) -> str:
        _warn(f"the sandbox exec is ended by a host cap: {breach.reason}")
        end_workload(breach.reason)
        return _resource_cap_line(breach.reason)

    items: queue.Queue[str | BaseException | None] = queue.Queue()
    read_requests: queue.Queue[None] = queue.Queue()
    threading.Thread(
        target=_read_stdout_on_demand,
        args=(stream, read_requests, items),
        daemon=True,
    ).start()
    while True:
        read_requests.put(None)
        item, timed = _wait_for_stream_item(guard, items, watchdog_interval_seconds)
        if timed is not None:
            yield ended(timed)
            return
        if item is None:
            return
        if isinstance(item, BaseException):
            raise item
        breach = guard.admit(item)
        if breach is not None:
            yield ended(breach)
            return
        yield item


STARTUP_NONCE_ENV = "KEW_AGENT_START_NONCE"
STARTUP_ACK_PREFIX = "KEW_AGENT_START "


_STARTUP_ACK_LINE = re.compile(r"KEW_AGENT_START [A-Za-z0-9_-]*\r?\n?")


def is_startup_ack(line: str) -> bool:
    """Keep wrapper acknowledgements outside agent parsers and event files.

    The WHOLE line must be an acknowledgement: the prefix and one nonce-shaped
    token. A prefix match would also drop a line the reviewed code began with
    the prefix and left without a newline, together with whatever the agent
    wrote next, which could be the reviewer's result event (#2021).
    """
    return _STARTUP_ACK_LINE.fullmatch(line) is not None


def _startup_nonce(argv: list[str]) -> str:
    """Read the host-generated attempt identity from the container environment."""
    prefix = f"{STARTUP_NONCE_ENV}="
    return next(
        (arg.removeprefix(prefix) for arg in argv if arg.startswith(prefix)), ""
    )


def renew_startup_nonce(argv: list[str]) -> list[str]:
    """Give a CPU retry its own acknowledgement identity."""
    prefix = f"{STARTUP_NONCE_ENV}="
    return [
        f"{prefix}{uuid.uuid4().hex}" if arg.startswith(prefix) else arg for arg in argv
    ]


def capture_container_stream_failure(
    handle: object, result: DispatchResult, exc: Exception
) -> None:
    """Missing startup telemetry cannot authorize discarding possible work."""
    if handle is None:
        result.stop_reason = str(exc)
        return
    capture_container_launch(handle, result)
    if (
        isinstance(handle, ContainerHandle)
        and handle._proc.poll() not in (64, 78)
        and not handle.exec_failed_before_agent_event
        and result.error_category == "sandbox_startup_refused"
    ):
        result.error_category = ""
    result.exit_code = -1
    if (
        result.launched is not True
        and result.error_category != "sandbox_startup_refused"
    ):
        result.launched = None
        result.error_category = "sandbox_startup_unknown"
        result.stop_reason = (
            f"Missing startup acknowledgement; execution state unknown: {exc}"
        )
    elif not result.stop_reason:
        result.stop_reason = str(exc)
    result.error_category = result.error_category or "sandbox_stream_error"
    logging.getLogger(__name__).warning(
        "Container stream failed: %s", result.stop_reason
    )


class ContainerStream(Protocol):
    """Required handle contract for the container stream consumer.

    ContainerIdentity and WorkloadStartObserver are independent OPTIONAL
    capabilities. Python protocols cannot express optional members directly.
    Callers read these capabilities through getattr, with None as the default.
    Missing identity means no ID; identity alone never proves workload startup.
    Without an observer, output cannot confirm startup.
    Native ContainerHandle checks the attempt acknowledgement or direct exec event.
    """

    def __iter__(self) -> Iterator[str]: ...

    def wait(self) -> int: ...


class ContainerIdentity(Protocol):
    """Optional container identity; absence defaults to None."""

    @property
    def container_id(self) -> str | None: ...


class WorkloadStartObserver(Protocol):
    """Optional startup observer; absence never confirms execution."""

    def observe_workload_start(self) -> bool: ...


def capture_container_launch(handle: object, result: DispatchResult) -> None:
    """Retain startup evidence; tolerate handles without optional capabilities."""
    if isinstance(handle, ContainerHandle) and handle.exec_failed_before_agent_event:
        result.launched = False
    observer = getattr(handle, "observe_workload_start", None)
    started = observer() is True if callable(observer) else False
    if started:
        result.launched = True
        result.attempt_number = max(result.attempt_number, 1)
        if result.error_category == "sandbox_startup_refused":
            result.error_category = ""
            result.stop_reason = ""
    identity = getattr(handle, "container_id", None)
    if isinstance(identity, str):
        result.container_id = identity
    refusal = getattr(handle, "startup_refusal_reason", None)
    if result.launched is False and isinstance(refusal, str) and refusal:
        result.stop_reason = refusal
        result.error_category = "sandbox_startup_refused"


@dataclass
class LaunchObservedStream:
    """Observe workload bytes before parsing, logging, or waiting can fail.

    Keep the stream consumer's call signature unchanged. Lifecycle operations
    still use the original handle, including identity and hard-stop cleanup.
    """

    handle: ContainerStream
    result: DispatchResult

    def __iter__(self) -> Iterator[str]:
        for line in self.handle:
            capture_container_launch(self.handle, self.result)
            yield line

    def wait(self) -> int:
        return self.handle.wait()


STDERR_SETTLE_SECONDS = 5.0
"""How long a finished stdout stream waits for its stderr drain to end.

Bounded: both pipes close when the runtime client exits, so the drain is
normally already at EOF; a client that somehow keeps one pipe open must not
hold the stream consumer forever.
"""


class ContainerStderrDrain:
    """The container's stderr on its OWN pipe, off the verdict path (#2053).

    The runtime client merges the container's stderr into the pipe carrying its
    stdout unless it is given a second one. For a governed review that merge is
    a correctness bug: the verdict is the reviewer's typed terminal event as the
    host reads the stream (#2021), and any line that is not a typed event fails
    the round (#2047) — so one stray runtime warning from the reviewed code
    failed an otherwise genuine round. With a second pipe only the agent's own
    stdout reaches the review capture.

    A second pipe must be READ: an undrained one blocks the container as soon as
    the kernel buffer fills, so the lines are pulled off by a thread. Nothing
    here is verdict-bearing evidence — the text is counted and dropped, exactly
    as the sanitized operational log already dropped it, so a flood on stderr
    grows no host memory (it also no longer trips the transcript cap, which
    bounds what the host reads AS the agent's stream). The two RUNTIME facts
    the merged stream used to carry are handed to *observer* (the handle that
    already derives them): the daemon's CPU-cap refusal and the entrypoint's
    pre-acknowledgement diagnostic.
    """

    def __init__(self, observer: Callable[[str], None] | None = None) -> None:
        self._observer = observer
        self._lock = threading.Lock()
        self._lines = 0
        self._thread: threading.Thread | None = None

    @property
    def lines(self) -> int:
        """How many stderr lines this run kept off the event stream."""
        with self._lock:
            return self._lines

    def attach(self, stream: IO[str] | None) -> None:
        """Drain one launch attempt's stderr pipe (None: nothing to drain)."""
        if stream is None:
            return
        self.settle()
        thread = threading.Thread(
            target=self._pump,
            args=(stream,),
            name="kew-container-stderr",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def settle(self, timeout: float = STDERR_SETTLE_SECONDS) -> None:
        """Wait (bounded) for the drain to finish, so its facts are complete."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _pump(self, stream: IO[str]) -> None:
        try:
            while True:
                line = _readline_bytes(stream)
                # EOF, or anything that did not come back as decoded text:
                # either way this pipe has nothing left to drain, and a loop
                # that could never reach EOF would spin for the whole run.
                if not isinstance(line, str) or not line:
                    return
                self._observe(line)
        except Exception:  # noqa: BLE001 — a dead drain must not block the container
            logging.getLogger(__name__).debug(
                "Container stderr drain ended early", exc_info=True
            )
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _observe(self, line: str) -> None:
        with self._lock:
            self._lines += 1
        if self._observer is not None:
            self._observer(line)


class ContainerHandle:
    """Handle to a running sandboxed container.

    Exposes a line-streaming stdout iterable (same contract as popen's stdout),
    wait() for the exit code, and kill() that issues `docker kill <id>`.

    Iteration also ENFORCES the three resource caps that have no Docker flag
    (#1431) — wall clock, transcript bytes, and the bind-mounted checkout's
    quota. On a breach it kills the container, yields one
    :data:`RESOURCE_CAP_EVENT` line naming the reason, and stops. *checkout_dir*
    is normally derived from the launch argv's ``/work`` bind mount, so callers
    get the quota without opting in; None simply disables that one cap.
    """

    def __init__(
        self,
        proc: subprocess.Popen[str],
        name: str,
        runtime: str = "docker",
        *,
        checkout_dir: Path | None = None,
        wall_clock_seconds: float = WALL_CLOCK_LIMIT_SECONDS,
        transcript_byte_cap: int = TRANSCRIPT_BYTE_CAP,
        checkout_quota_bytes: int = CHECKOUT_QUOTA_BYTES,
        now: Callable[[], float] = time.monotonic,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
        checkout_quota_entries: int = CHECKOUT_QUOTA_ENTRIES,
        host_free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
        cidfile: Path | None = None,
        container_id: str | None = None,
        launch_argv: list[str] | None = None,
        startup_nonce: str = "",
        cpu_retry_launcher: Callable[[list[str]], subprocess.Popen[str]] | None = None,
        event_window_start: float | None = None,
        separate_stderr: bool = False,
    ) -> None:
        self._proc = proc
        self._checkout_quota_entries = checkout_quota_entries
        self._host_free_floor = host_free_floor
        self._name = name
        self._unrecorded_reported: set[str] = set()
        self._runtime = runtime
        self._checkout_dir = checkout_dir
        self._wall_clock_seconds = wall_clock_seconds
        self._transcript_byte_cap = transcript_byte_cap
        self._checkout_quota_bytes = checkout_quota_bytes
        self._now = now
        self._watchdog_interval_seconds = watchdog_interval_seconds
        self._watchdog_stop: threading.Event | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._resource_cap_reason: str | None = None
        self._resource_cap_code: ResourceCapCode | None = None
        self._resource_cap_observations: list[ResourceCapObservation] = []
        self._resource_cap_observations_truncated = False
        self._pending_breach: ResourceCapObservation | None = None
        self._oom_probe: OOMProbeResult | None = None
        self._cidfile = cidfile
        if (
            container_id is not None
            and _FULL_CONTAINER_ID.fullmatch(container_id) is None
        ):
            raise ValueError("container_id must be one full container identity")
        self._container_id = container_id
        self._admission: PendingAdmission | None = None
        self._launch_cancelled = False
        self._workload_started = False
        self._startup_acknowledged = False
        self._agent_event_observed = False
        self._startup_stream_complete = False
        self._startup_diagnostic = ""
        self._container_identity_diagnostic = ""
        self._launch_argv = list(launch_argv) if launch_argv is not None else None
        self._startup_nonce = startup_nonce or _startup_nonce(launch_argv or [])
        self._cpu_retry_launcher = cpu_retry_launcher
        self._cpu_cap_rejection_maximum: Decimal | None = None
        # WALL-CLOCK, not the injectable monotonic ``now``: it is the lower
        # bound of the `docker events` probe window, and the daemon stamps
        # events in epoch time.
        self._launched_at = (
            time.time() if event_window_start is None else event_window_start
        )
        self._breach_lock = threading.RLock()
        self._breach_changed = threading.Condition(self._breach_lock)
        self._active_breach_transitions = 0
        self._pending_breach_unknown = False
        self._enforcement_lock = threading.Lock()
        self._enforcement_windows: list[tuple[float, float]] = []
        self._kill_lock = threading.Lock()
        self._killed = False
        self._kill_failure_reported = False
        self._reaper_thread: threading.Thread | None = None
        self._diagnostic_tail = DiagnosticTail()
        # A separated stderr pipe is drained from the moment the client is
        # spawned; the iterable below yields the container's stdout alone
        # (#2053).
        self._stderr_drain = (
            ContainerStderrDrain(self.observe_client_diagnostic)
            if separate_stderr
            else None
        )
        self._attach_stderr_drain()

    def _attach_stderr_drain(self) -> None:
        """Drain the current client's stderr pipe, if this launch has one."""
        if self._stderr_drain is not None:
            self._stderr_drain.attach(self._proc.stderr)

    def _settle_stderr_drain(self) -> None:
        """Complete the drain's observations before they are read."""
        if self._stderr_drain is not None:
            self._stderr_drain.settle()

    @property
    def diagnostic_lines(self) -> list[str]:
        """The separated stderr pipe's last lines, redacted (#2887)."""
        self._settle_stderr_drain()
        return self._diagnostic_tail.lines()

    @property
    def stderr_lines(self) -> int:
        """Stderr lines kept off the event stream by a separated pipe."""
        return 0 if self._stderr_drain is None else self._stderr_drain.lines

    def observe_client_diagnostic(self, line: str) -> None:
        """Derive the runtime facts a separated stderr pipe still carries.

        Called on the drain's thread, NOT from the stream consumer, and it
        deliberately derives less than :meth:`observe_workload_output`: a
        startup acknowledgement and an agent event are the agent's own stdout,
        so stderr can neither start a workload nor be an agent event. What it
        keeps is what the merged pipe used to give the host — the daemon's
        CPU-cap maximum, which drives the one relaunch, and the entrypoint's
        pre-acknowledgement diagnostic.
        """
        self._diagnostic_tail.add(line)
        maximum = _cpu_cap_rejection_maximum(line)
        if maximum is not None:
            self._cpu_cap_rejection_maximum = maximum
            self._report_refused_attempt(line)
            return
        if not self._workload_started and line.startswith("ERROR:"):
            self._startup_diagnostic = line.strip()[:500]

    def _guard(self, wall_clock_seconds: float | None = None) -> _StreamGuard:
        seconds = (
            self._wall_clock_seconds
            if wall_clock_seconds is None
            else wall_clock_seconds
        )
        guard = _StreamGuard(
            checkout_dir=self._checkout_dir,
            wall_clock_seconds=seconds,
            transcript_cap=self._transcript_byte_cap,
            checkout_quota=self._checkout_quota_bytes,
            checkout_entry_quota=self._checkout_quota_entries,
            host_free_floor=self._host_free_floor,
            now=self._now,
            watchdog_interval=self._watchdog_interval_seconds,
        )
        with self._enforcement_lock:
            self._enforcement_windows.append((guard.started_at, seconds))
        return guard

    def enforced_duration_seconds(self, timeout_seconds: float) -> float | None:
        """Elapsed time from an owner of this timeout, or None."""
        with self._enforcement_lock:
            starts = [
                started_at
                for started_at, owner_timeout in self._enforcement_windows
                if owner_timeout == timeout_seconds
            ]
        if not starts:
            return None
        return max(0.0, self._now() - min(starts))

    def _breach(self, breach: ResourceCapObservation) -> None:
        with self._breach_changed:
            self._record_observation(breach)
            if self._pending_breach is None:
                self._pending_breach = breach
                self._pending_breach_unknown = False
            self._active_breach_transitions += 1
        try:
            should_reap = self._attempt_breach_kill(breach)
        finally:
            with self._breach_changed:
                self._active_breach_transitions -= 1
                self._breach_changed.notify_all()
        if should_reap:
            self._start_reaper(breach)

    def _attempt_breach_kill(self, breach: ResourceCapObservation) -> bool:
        """Try one kill and retain uncertainty until daemon evidence settles it."""
        status = self.kill()
        resolved_unknown = False
        if status is KillStatus.UNKNOWN:
            status = self._resolve_unknown_kill()
            resolved_unknown = status is not KillStatus.UNKNOWN
        return self._apply_kill_status(
            breach,
            status,
            resolved_unknown=resolved_unknown,
        )

    def _apply_kill_status(
        self,
        breach: ResourceCapObservation,
        status: KillStatus,
        *,
        resolved_unknown: bool,
    ) -> bool:
        """Apply one tri-state kill result and return whether reaping must continue."""
        with self._breach_changed:
            if status is KillStatus.ACCEPTED:
                self._killed = True
                self._latch_watchdog_cause(breach)
                if self._pending_breach == breach:
                    self._pending_breach = None
                    self._pending_breach_unknown = False
            elif status is KillStatus.UNKNOWN:
                if self._pending_breach == breach:
                    self._pending_breach_unknown = True
            elif self._pending_breach == breach and (
                resolved_unknown or not self._pending_breach_unknown
            ):
                self._pending_breach = None
                self._pending_breach_unknown = False
            self._breach_changed.notify_all()
            return status is not KillStatus.ACCEPTED and not self._killed

    def wait_for_breach_transition(self) -> None:
        """Wait boundedly for any runtime kill transition already in flight."""
        with self._breach_changed:
            self._breach_changed.wait_for(
                lambda: (
                    self._active_breach_transitions == 0
                    or self._resource_cap_code is not None
                ),
                timeout=3 * RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )

    def _record_observation(self, observation: ResourceCapObservation) -> None:
        with self._breach_lock:
            observations = self._resource_cap_observations
            if observation in observations:
                return
            if len(observations) < MAX_RESOURCE_CAP_OBSERVATIONS:
                observations.append(observation)
                return
            # Keep the first breach because it triggered the watchdog. Keep the
            # newest observations because they can include the final OOM probe.
            observations[1:] = [*observations[2:], observation]
            self._resource_cap_observations_truncated = True

    def _latch_watchdog_cause(self, breach: ResourceCapObservation) -> None:
        """Name a watchdog cause only after its runtime kill succeeded."""
        if self._resource_cap_code is not None:
            return
        self._resource_cap_code = breach.code
        self._resource_cap_reason = f"{self._name}: {breach.reason}"

    def _start_reaper(self, breach: ResourceCapObservation) -> None:
        """Keep issuing `docker kill` until the client process is gone.

        A breach can fire before the container exists (the first quota walk
        runs at t=0; `docker run` takes seconds to create it) — that kill
        returns "No such container" and, latched, would leave the container
        to run its whole budget unwatched and then be labelled a cap kill.
        """
        with self._kill_lock:
            if self._reaper_thread is not None:
                return
            self._reaper_thread = threading.Thread(
                target=self._reap,
                args=(breach,),
                daemon=True,
            )
        self._reaper_thread.start()

    def _reap(self, breach: ResourceCapObservation) -> None:
        """Re-issue the kill each interval until it lands or the client exits.

        A handle given neither a cidfile nor an id can never record one, so no
        kill could ever be sent: the reaper stops at once rather than polling a
        client forever (#3375). The interval is an :class:`threading.Event`
        wait, which no patched ``time.sleep`` can turn into a spin.
        """
        interval = threading.Event()
        while not self._killed:
            if self._cidfile is None and self._container_id is None:
                logging.getLogger(__name__).warning(
                    "Sandbox: %s can never record a container id; "
                    "the breach kill is not retried",
                    self._name,
                )
                return
            if self._proc.poll() is not None:
                return
            interval.wait(self._watchdog_interval_seconds)
            if self._proc.poll() is not None:
                return
            with self._breach_changed:
                if self._killed:
                    return
                if self._pending_breach is None:
                    self._pending_breach = breach
                    self._pending_breach_unknown = False
                self._active_breach_transitions += 1
            try:
                should_reap = self._attempt_breach_kill(breach)
            finally:
                with self._breach_changed:
                    self._active_breach_transitions -= 1
                    self._breach_changed.notify_all()
            if not should_reap:
                return

    def start_watchdog(self, *, wall_clock_seconds: float | None = None) -> None:
        """Start the output-independent wall-clock and checkout watchdog."""
        if self._watchdog_thread is not None:
            return
        guard = self._guard(wall_clock_seconds)
        stop = threading.Event()
        self._watchdog_stop = stop

        def watch() -> None:
            while not stop.is_set():
                reason = guard.timed_breach()
                if reason is not None:
                    self._breach(reason)  # re-issues the kill until it lands
                    if self._killed:
                        return
                stop.wait(self._watchdog_interval_seconds)

        self._watchdog_thread = threading.Thread(target=watch, daemon=True)
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        if self._watchdog_stop is not None:
            self._watchdog_stop.set()

    @property
    def resource_cap_reason(self) -> str | None:
        """The host cap that killed this container, or None."""
        with self._breach_lock:
            return self._resource_cap_reason

    @property
    def resource_cap_code(self) -> ResourceCapCode | None:
        """The stable code for the selected cap cause, or None."""
        with self._breach_lock:
            return self._resource_cap_code

    @property
    def resource_cap_observations(self) -> tuple[ResourceCapObservation, ...]:
        """The bounded cap observations retained in observed order."""
        with self._breach_lock:
            return tuple(self._resource_cap_observations)

    @property
    def resource_cap_observations_truncated(self) -> bool:
        """Whether older observations were removed from the bounded list."""
        with self._breach_lock:
            return self._resource_cap_observations_truncated

    @property
    def oom_probe(self) -> OOMProbeResult | None:
        """The daemon probe outcome, or None when no probe was relevant."""
        return self._oom_probe

    def resource_cap_line(self) -> str | None:
        if self._resource_cap_reason is None:
            return None
        return _resource_cap_line(self._resource_cap_reason)

    def _retry_cpu_cap_rejection(self) -> bool:
        """Retry this launch once when the daemon proves it created nothing."""
        argv = self._launch_argv
        launcher = self._cpu_retry_launcher
        self._cpu_retry_launcher = None
        if argv is None or launcher is None:
            return False
        try:
            returncode = self._proc.wait(timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            from kew.progress import _warn

            _warn("Sandbox: CPU-cap retry skipped because the runtime did not exit")
            return False
        maximum = self._cpu_cap_rejection_maximum
        if maximum is None:
            return False
        retry_argv = _cpu_cap_retry_argv(argv, returncode, maximum)
        if retry_argv is None:
            return False
        event_window_start = time.time()
        retry_argv = renew_startup_nonce(retry_argv)
        with DeferredSIGINT():
            self._proc = launcher(retry_argv)
            self._attach_stderr_drain()
            self._cpu_cap_rejection_maximum = None
            self._launch_argv = retry_argv
            self._startup_nonce = _startup_nonce(retry_argv)
            self._container_id = None
            self._container_identity_diagnostic = ""
            self._workload_started = False
            self._startup_acknowledged = False
            self._agent_event_observed = False
            self._startup_stream_complete = False
            self._startup_diagnostic = ""
            self._launched_at = event_window_start
            self._cache_container_id()
        return True

    def _breach_line(self, breach: ResourceCapObservation) -> str:
        """Enforce and report one stream cap with its retained cause."""
        self._breach(breach)
        reason = self._resource_cap_reason or f"{self._name}: {breach.reason}"
        return _resource_cap_line(reason)

    def _iter_launch_attempt(self, guard: _StreamGuard) -> Iterator[str]:
        """Stream one runtime client process and retain its CPU rejection."""
        stdout = self._proc.stdout
        assert stdout is not None
        items: queue.Queue[str | BaseException | None] = queue.Queue()
        read_requests: queue.Queue[None] = queue.Queue()

        threading.Thread(
            target=_read_stdout_on_demand,
            args=(stdout, read_requests, items),
            daemon=True,
        ).start()
        while True:
            read_requests.put(None)
            item, timed_reason = _wait_for_stream_item(
                guard, items, self._watchdog_interval_seconds
            )
            if timed_reason is not None:
                yield self._breach_line(timed_reason)
                return
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            line = item
            self.observe_workload_output(line)
            self.observe_workload_start()
            if is_startup_ack(line):
                continue
            breach = guard.admit(line)
            if breach is None:
                maximum = _cpu_cap_rejection_maximum(line)
                if maximum is not None:
                    self._cpu_cap_rejection_maximum = maximum
                yield line
                continue
            yield self._breach_line(breach)
            return

    def __iter__(self) -> Iterator[str]:
        """Yield the container's output, never a retried launch's refusal.

        The runtime client writes the daemon's refusal on the pipe that carries
        the container's stdout. A handle that can retry holds that line back
        until the attempt ends: when the retry launches, the line is the
        runtime's diagnostic for a launch that created nothing, so it goes to
        the operator's stderr and not into the agent-event stream, where a
        governed review counts any unparsed line against the round. When no
        retry launches, the line is yielded as the launch's failure.

        A launch that separated stderr (#2053) never carries the refusal in this
        stream at all: the drain observes it, reports it to the operator, and
        the retry decision below reads it from there.

        Only the attempt's latest refusal is held: it alone drives the retry,
        so a flood of refusal-shaped lines cannot grow a host-memory list. The
        earlier refusals of the same attempt are dropped — the launch's failure
        is still the one line the retry decision was made on.
        """
        guard = self._guard()
        while True:
            held: str | None = None
            for line in self._iter_launch_attempt(guard):
                if self._holds_back(line):
                    held = line
                else:
                    yield line
            # A separated stderr pipe carries the refusal instead of the stream
            # (#2053): its drain must be settled before the retry decision, and
            # it is cleared by the relaunch, not by this loop, so a refusal seen
            # before the first read is not discarded.
            self._settle_stderr_drain()
            if (
                self._cpu_cap_rejection_maximum is not None
                and self._retry_cpu_cap_rejection()
            ):
                self._report_refused_attempt(held)
                continue
            if held is not None:
                yield held
            return

    def _holds_back(self, line: str) -> bool:
        """Whether *line* is a CPU-cap refusal this handle may still retry."""
        return (
            self._cpu_retry_launcher is not None
            and _cpu_cap_rejection_maximum(line) is not None
        )

    @staticmethod
    def _report_refused_attempt(held: str | None) -> None:
        if held is None:
            return
        from kew.progress import _warn

        _warn(f"Sandbox: runtime refused the launch: {held.strip()}")

    def wait(
        self, timeout: float | None = None, *, cleanup_identity: bool = True
    ) -> int:
        """Wait for the runtime client to exit, then finish the identity cleanup.

        *timeout* bounds the wait (``subprocess.TimeoutExpired`` escapes and
        the cleanup does not run); the default keeps the unbounded wait.
        """
        exit_code = self._proc.wait(timeout=timeout)
        # Both pipes close with the client, so the drain is at EOF by now:
        # settle it before the startup facts below are read off it (#2053).
        self._settle_stderr_drain()
        self.finish_startup_observation()
        self._cache_container_id()
        self.observe_workload_start()
        if cleanup_identity:
            self.cleanup_identity()
        return exit_code

    def observe_workload_start(self) -> bool:
        """Latch agent evidence; a container start only proves wrapper startup."""
        if self.exec_failed_before_agent_event:
            self._record_admitted_startup(refused=True)
            return False
        if self._workload_started:
            self._record_admitted_startup(settled=self._agent_event_observed)
            return True
        # Every contained agent runs under /usr/local/bin/kew-agent-entrypoint,
        # which acknowledges this run's startup nonce on its own output; a
        # container or exec that merely started proves the wrapper ran, never
        # the agent. tests/test_entrypoint_contract.py pins that argv shape, so
        # an exec that skipped the entrypoint would fail there rather than run
        # without startup evidence (governed review round 2, 2026-09-21).
        self._cache_container_id()
        return False

    def _record_admitted_startup(
        self, *, refused: bool = False, settled: bool = False
    ) -> None:
        if self._admission is not None:
            self._admission.record_startup(refused=refused, settled=settled)

    def observe_workload_output(self, line: str) -> None:
        """Observe agent events before parsing or another stream read can fail."""
        nonce = self._startup_nonce
        if nonce and line.rstrip("\r\n") == f"{STARTUP_ACK_PREFIX}{nonce}":
            self._workload_started = True
            self._startup_acknowledged = True
            self._startup_diagnostic = ""
        elif self._startup_acknowledged:
            self._observe_agent_event(line)
        elif not self._workload_started and line.startswith("ERROR:"):
            self._startup_diagnostic = line.strip()[:500]

    def _observe_agent_event(self, line: str) -> None:
        """Retain typed stream events only after this attempt's acknowledgement."""
        try:
            event = json.loads(line)
        except ValueError as exc:
            logging.getLogger(__name__).debug("Non-event startup output: %s", exc)
            return
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            self._agent_event_observed |= bool(event["type"])

    def finish_startup_observation(self) -> None:
        """Settle exec failure only after consuming the stream and waiting."""
        self._startup_stream_complete = True
        started = self.observe_workload_start()
        if started or self.startup_refusal_reason:
            self._record_admitted_startup(refused=not started, settled=True)

    @property
    def exec_failed_before_agent_event(self) -> bool:
        """Shell exec failures revoke an acknowledgement with no agent event.

        Only the entrypoint acknowledges, so an acknowledgement followed by a
        shell exec failure is always the entrypoint failing to exec the agent.
        """
        return (
            self._startup_stream_complete
            and self._startup_acknowledged
            and not self._agent_event_observed
            and self._proc.poll() in (126, 127)
        )

    @property
    def startup_refusal_reason(self) -> str:
        """Keep the preparation diagnostic when the wrapper exits without work."""
        if self.exec_failed_before_agent_event:
            return "Container agent exec failed before any agent event (exit 126/127)"
        if self._workload_started:
            return ""
        code = self._proc.poll()
        if not isinstance(code, int):
            return ""
        detail = self._startup_diagnostic or {
            64: "agent prompt file missing or empty",
            78: "agent prerequisite unavailable",
        }.get(code, "no agent startup acknowledgement observed")
        return f"Container exited before agent startup (exit {code}): {detail}"

    @property
    def container_id(self) -> str | None:
        """The daemon's id for the created container, once the client wrote it.

        Read from the container id file when the client exits (``wait``); None
        means the client never recorded a creation, which a caller that needs
        an acknowledged launch treats as unconfirmed.
        """
        return self._container_id

    @property
    def name(self) -> str:
        """The container name this launch asked the runtime for."""
        return self._name

    def settle_client(self, timeout: float) -> bool:
        """Make sure the runtime client process has exited; kill it if needed.

        The container is not touched. A caller that must know the container
        is gone probes the daemon only after this returns True: a client with
        a launch request still in flight can create the container after such
        a probe.
        """
        if self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                from kew.progress import _warn

                _warn(
                    f"Sandbox: the runtime client for {self._name} did not exit "
                    f"within {timeout:g}s of being killed ({type(exc).__name__})"
                )
                return False
        return True

    def note_oom_kill(self) -> OOMProbeResult:
        """Probe a relevant 137 and retain a confirmed OOM observation.

        An accepted watchdog kill proves which action ended the container.
        A concurrent OOM can have killed only a child process, so it remains
        evidence without replacing that accepted watchdog cause.
        """
        probe = self._daemon_saw_oom()
        self._oom_probe = probe
        if probe.status is not OOMProbeStatus.SEEN:
            return probe
        observation = ResourceCapObservation(
            ResourceCapCode.OOM,
            oom_cap_reason(_argv_memory_limit(self._launch_argv)),
        )
        with self._breach_lock:
            self._record_observation(observation)
            if self._resource_cap_code is None:
                self._resource_cap_code = observation.code
                self._resource_cap_reason = f"{self._name}: {observation.reason}"
        return probe

    def _daemon_saw_oom(self) -> OOMProbeResult:
        """Return seen, absent, or unavailable daemon OOM evidence."""
        identity, identity_diagnostic = self._container_identity()
        if identity is None:
            from kew.progress import _warn

            diagnostic = self._bounded_probe_diagnostic(identity_diagnostic)
            _warn(
                f"OOM attribution unavailable for {self._name}: {diagnostic}; "
                "137 left unattributed"
            )
            return OOMProbeResult(OOMProbeStatus.UNAVAILABLE, diagnostic)
        now = time.time()
        argv = [
            # By tool NAME: the allowlist refuses an absolute runtime path,
            # which is how the same bug once made every kill a no-op.
            Path(self._runtime).name,
            "events",
            "--filter",
            f"container={identity}",
            "--filter",
            f"event={OOM_EVENT_ACTION}",
            "--since",
            f"{self._launched_at - OOM_PROBE_CLOCK_SLACK_SECONDS:.6f}",
            "--until",
            f"{now + OOM_PROBE_CLOCK_SLACK_SECONDS:.6f}",
        ]
        from kew.progress import _warn

        try:
            probe = subprocess_utils.run(
                argv, check=False, timeout=OOM_PROBE_TIMEOUT_SECONDS
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            diagnostic = self._bounded_probe_diagnostic(
                f"{type(exc).__name__}: {exception_text(exc)}"
            )
            _warn(
                f"OOM probe of {self._name} failed: {diagnostic}; 137 left unattributed"
            )
            return OOMProbeResult(OOMProbeStatus.UNAVAILABLE, diagnostic)
        if getattr(probe, "returncode", 1) != 0:
            stderr = (getattr(probe, "stderr", "") or "").strip()
            diagnostic = self._bounded_probe_diagnostic(
                f"exit {probe.returncode}: {stderr}"
            )
            _warn(
                f"OOM probe of {self._name} returned {probe.returncode}: "
                f"{diagnostic}; 137 left unattributed"
            )
            return OOMProbeResult(OOMProbeStatus.UNAVAILABLE, diagnostic)
        if (getattr(probe, "stdout", "") or "").strip():
            return OOMProbeResult(OOMProbeStatus.SEEN)
        return OOMProbeResult(OOMProbeStatus.ABSENT)

    @staticmethod
    def _bounded_probe_diagnostic(diagnostic: str) -> str:
        return redact_credentials(diagnostic)[:OOM_PROBE_DIAGNOSTIC_MAX_CHARS]

    def _container_identity(self) -> tuple[str | None, str]:
        """Return this launch's immutable identity or an attribution failure."""
        self._cache_container_id()
        if self._container_id is not None:
            return self._container_id, ""
        return None, self._container_identity_diagnostic

    def accept_admission(self, admission: PendingAdmission) -> None:
        """The runtime identity, never its client PID, transfers admission ownership."""
        with DeferredSIGINT():
            self._admission = admission
            admission.launch_cleanup = self.cancel_launch
            self._cache_container_id()

    def cancel_launch(self) -> None:
        """Stop and reap the runtime client and its identified container once."""
        if self._launch_cancelled:
            return
        with DeferredSIGINT():
            self._cache_container_id()
            self.stop_watchdog()
            try:
                self.kill()
                self.remove()
            finally:
                _reap_interrupted_client(self._proc)
            self.cleanup_identity()
            self._launch_cancelled = True

    def _accept_container_ownership(self) -> None:
        if self._admission is not None and self._container_id is not None:
            self._admission.take_ownership()

    def _cache_container_id(self) -> None:
        if self._container_id is not None:
            self._accept_container_ownership()
            return
        if self._cidfile is None:
            self._container_identity_diagnostic = "container id file was not supplied"
            return
        try:
            candidate = self._cidfile.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            if isinstance(exc, FileNotFoundError):
                self._container_identity_diagnostic = (
                    f"container id file {self._cidfile} is missing"
                )
            else:
                self._container_identity_diagnostic = (
                    f"container id file {self._cidfile} is unreadable: "
                    f"{exception_text(exc)}"
                )
        else:
            if re.fullmatch(r"[0-9a-fA-F]{12,64}", candidate):
                with DeferredSIGINT():
                    self._container_id = candidate
                    self._accept_container_ownership()
            elif not candidate:
                self._container_identity_diagnostic = (
                    f"container id file {self._cidfile} is empty"
                )
            else:
                self._container_identity_diagnostic = (
                    f"container id file {self._cidfile} has an invalid container id"
                )

    def cleanup_identity(self) -> None:
        """Remove the host cidfile after its identity has been cached."""
        if self._cidfile is None:
            return
        with contextlib.suppress(OSError):
            self._cidfile.unlink(missing_ok=True)

    def _recorded_target(self, action: str) -> str | None:
        """This launch's recorded container id, or None with the reason logged.

        A name is never a target: it is host-wide, and the container under it
        may be another launch's (#1963). Without a recorded id nothing is sent.
        """
        identity, diagnostic = self._container_identity()
        if identity is None and action not in self._unrecorded_reported:
            # Once per action: the bounded reaper asks again every interval.
            self._unrecorded_reported.add(action)
            logging.getLogger(__name__).warning(
                "Sandbox: no container id recorded for %s (%s); %s not sent",
                self._name,
                diagnostic or "none yet",
                action,
            )
        return identity

    @property
    def kill_accepted(self) -> bool:
        """Whether the runtime has accepted a kill of this container, by any
        path; :meth:`kill` refuses a second request once one has."""
        return self._killed

    def kill(self) -> KillStatus:
        """Try one bounded runtime kill and report accepted, refused, or unknown.

        Only the recorded id is killed: unknown, and nothing sent, while this
        launch has recorded none, never the name (#1963).
        """
        # Use the same runtime that launched the container (docker / OrbStack /
        # podman), not a hardcoded "docker".
        with self._kill_lock:
            if self._killed:
                return KillStatus.REFUSED
            # The allowlist resolves executables by tool NAME; the absolute
            # runtime path is refused ("Unknown executable"), which is how the
            # live kill test found this kill never firing. Latch ONLY on a kill
            # the runtime accepted: "No such container" (not created yet) must
            # leave the next attempt open (round-5 orphan).
            target = self._recorded_target("kill")
            if target is None:
                return KillStatus.UNKNOWN
            try:
                result = subprocess_utils.run(
                    [Path(self._runtime).name, "kill", target],
                    check=False,
                    timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if not self._kill_failure_reported:
                    from kew.progress import _warn

                    _warn(
                        f"Runtime kill of {self._name} failed; "
                        "the bounded reaper will retry"
                    )
                    self._kill_failure_reported = True
                if isinstance(exc, subprocess.TimeoutExpired):
                    return KillStatus.UNKNOWN
                return KillStatus.REFUSED
            if getattr(result, "returncode", 1) == 0:
                self._killed = True
                return KillStatus.ACCEPTED
            return KillStatus.REFUSED

    def _resolve_unknown_kill(self) -> KillStatus:
        """Resolve an uncertain kill through bounded daemon evidence."""
        event_status = self._daemon_saw_kill()
        if event_status is KillStatus.ACCEPTED:
            return event_status
        return self._read_container_kill_state()

    def _daemon_saw_kill(self) -> KillStatus:
        """Return accepted only when this container has a daemon kill event."""
        identity, _diagnostic = self._container_identity()
        if identity is None:
            return KillStatus.UNKNOWN
        now = time.time()
        argv = [
            Path(self._runtime).name,
            "events",
            "--filter",
            f"container={identity}",
            "--filter",
            f"event={KILL_EVENT_ACTION}",
            "--since",
            f"{self._launched_at - OOM_PROBE_CLOCK_SLACK_SECONDS:.6f}",
            "--until",
            f"{now + OOM_PROBE_CLOCK_SLACK_SECONDS:.6f}",
        ]
        try:
            result = subprocess_utils.run(
                argv,
                check=False,
                timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return KillStatus.UNKNOWN
        if getattr(result, "returncode", 1) != 0:
            return KillStatus.UNKNOWN
        stdout = getattr(result, "stdout", "")
        if isinstance(stdout, str) and stdout.strip():
            return KillStatus.ACCEPTED
        return KillStatus.UNKNOWN

    def _read_container_kill_state(self) -> KillStatus:
        """Resolve an uncertain kill from the container's bounded state read."""
        identity, _diagnostic = self._container_identity()
        if identity is None:
            return KillStatus.UNKNOWN
        try:
            result = subprocess_utils.run(
                [
                    Path(self._runtime).name,
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    identity,
                ],
                check=False,
                timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return KillStatus.UNKNOWN
        if getattr(result, "returncode", 1) != 0:
            return KillStatus.UNKNOWN
        state = _parse_container_state(getattr(result, "stdout", ""))
        if state is None:
            return KillStatus.UNKNOWN
        if state["Running"] or state.get("OOMKilled") is True:
            return KillStatus.REFUSED
        if state.get("ExitCode") == 137:
            return KillStatus.ACCEPTED
        return KillStatus.REFUSED

    def remove_if_never_started(self) -> bool:
        """Remove this launch's container when its start never happened (#2973).

        A refused ``start`` leaves the container ``created`` under the item's
        name. Only this handle's own full id is read and removed, never a name,
        and only once the client has exited: a live client may yet start it.
        """
        if self._proc.poll() is None:
            return False
        return remove_unstarted_start(
            self._runtime, self._container_id, self._launch_argv
        )

    def remove(self) -> bool:
        """Remove the owned container even when it never reached Running.

        Only the id this launch recorded is removed, never the name: under
        load another launch's container can hold ``kew-<item>`` by then (#1963).
        """
        target = self._recorded_target("removal")
        if target is None:
            return False
        try:
            result = subprocess_utils.run(
                [Path(self._runtime).name, "rm", "-f", target],
                check=False,
                timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            from kew.progress import _warn

            _warn(f"Runtime removal of {self._name} failed: {type(exc).__name__}")
            return False
        return getattr(result, "returncode", 1) == 0


def _reached_recorded_deadline(
    result: DispatchResult,
    timeout_seconds: float,
) -> bool:
    """Return whether the matching enforcement owner reached its deadline."""
    return (
        result.enforced_duration_seconds is not None
        and result.enforced_timeout_seconds == timeout_seconds
        and result.enforced_duration_seconds >= timeout_seconds
    )


def classify_hard_stop(result: DispatchResult, *, timeout_seconds: float) -> str:
    """Name a 124/137 exit as wall_clock, resource_cap, or hard_kill."""
    if result.paused or result.budget_exceeded or result.breaker_stopped:
        return ""
    if result.resource_cap_reason:
        return (
            "wall_clock"
            if result.resource_cap_code == ResourceCapCode.WALL_CLOCK
            else "resource_cap"
        )
    if result.exit_code in (124, 137) and _reached_recorded_deadline(
        result, timeout_seconds
    ):
        return "wall_clock"
    if result.exit_code == 137:
        return "hard_kill"
    return ""


def _should_probe_oom(result: DispatchResult, returncode: int | None) -> bool:
    """Return whether a 137 was not an intentional kew stop."""
    intentional = result.paused or result.budget_exceeded or result.breaker_stopped
    return not intentional and 137 in (result.exit_code, returncode)


def _copy_container_cap_evidence(
    handle: ContainerHandle, result: DispatchResult
) -> None:
    """Copy bounded, typed handle evidence onto a dispatch result."""
    reason = getattr(handle, "resource_cap_reason", None)
    code = getattr(handle, "resource_cap_code", None)
    result.resource_cap_reason = reason if isinstance(reason, str) else ""
    result.resource_cap_code = code.value if isinstance(code, ResourceCapCode) else ""
    observed = getattr(handle, "resource_cap_observations", ())
    observations = tuple(
        observation
        for observation in observed
        if isinstance(observation, ResourceCapObservation)
    )
    result.resource_cap_observations = [
        {
            "code": observation.code.value,
            "reason": f"{handle._name}: {observation.reason}",
        }
        for observation in observations
    ] or None
    truncated = getattr(handle, "resource_cap_observations_truncated", False)
    result.resource_cap_observations_truncated = truncated is True


def collect_container_hard_stop_evidence(
    handle: ContainerHandle,
    result: DispatchResult,
    *,
    returncode: int | None,
    timeout_seconds: float,
) -> OOMProbeResult | None:
    """Probe a relevant 137 and copy its bounded evidence onto a result."""
    # Read the enforcing owner's window before a future-bound daemon query.
    # No guard or watchdog means None, which forbids inferred wall-clock blame.
    duration_reader = getattr(handle, "enforced_duration_seconds", None)
    enforced_duration = (
        duration_reader(timeout_seconds) if callable(duration_reader) else None
    )
    result.enforced_duration_seconds = (
        float(enforced_duration)
        if isinstance(enforced_duration, (int, float))
        else None
    )
    result.enforced_timeout_seconds = (
        timeout_seconds if result.enforced_duration_seconds is not None else None
    )
    transition_waiter = getattr(handle, "wait_for_breach_transition", None)
    if callable(transition_waiter):
        transition_waiter()
    probe: OOMProbeResult | None = None
    if _should_probe_oom(result, returncode):
        observed_probe = handle.note_oom_kill()
        probe = observed_probe if isinstance(observed_probe, OOMProbeResult) else None
    _copy_container_cap_evidence(handle, result)
    if probe is not None:
        result.oom_probe_status = probe.status.value
        result.oom_probe_diagnostic = probe.diagnostic
    return probe


def finalize_container_hard_stop(
    handle: ContainerHandle,
    result: DispatchResult,
    *,
    returncode: int | None,
    timeout_seconds: float,
) -> OOMProbeResult | None:
    """Probe a relevant 137 and copy one shared classification onto a result."""
    probe = collect_container_hard_stop_evidence(
        handle,
        result,
        returncode=returncode,
        timeout_seconds=timeout_seconds,
    )
    result.hard_stop = classify_hard_stop(result, timeout_seconds=timeout_seconds)
    return probe


def write_prompt_handoff(checkout_dir: Path, prompt: str) -> Path:
    """Place the prompt handoff file in an AGENT-WRITABLE checkout, safely.

    Every caller writes into a directory an agent can modify — mounted rw into
    the container, and on the reuse paths an earlier agent has already run
    there. A plain ``write_text`` FOLLOWS a symlink, so an agent that leaves
    ``.kew-prompt.txt`` pointing at an absolute host path gets the next run's
    prompt written to that path instead. Unlinking afterwards removes the link,
    not the damage — the victim keeps the content.

    The prompt is not inert, either: a review prompt embeds PR comment bodies,
    which anyone with access to the pull request can author. That makes this a
    write primitive with partially chosen content, aimed anywhere the operator
    can write.

    So: unlink whatever is at the path (that removes a symlink, never its
    target), then create exclusively with ``O_NOFOLLOW`` so a link planted in
    the gap is refused rather than followed. A non-file squatting the name — a
    directory, say — raises here instead of being cleared, because silently
    removing whatever an agent left is its own hazard.
    """
    path = checkout_dir / PROMPT_FILENAME
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    return path


def remove_prompt_handoff(checkout_dir: Path) -> None:
    """Remove the prompt handoff file (#725).

    Safe by construction where the write is not: ``unlink`` operates on the link
    itself, so a planted symlink is removed and its target left alone.
    """
    (checkout_dir / PROMPT_FILENAME).unlink(missing_ok=True)


def _argv_runtime_and_name(argv: list[str]) -> tuple[str, str]:
    """The runtime and container name carried by a container-run *argv*.

    One derivation site, used by both launch paths, so the container a watchdog
    kills is always the container that was launched.
    """
    return argv[0], argv[argv.index("--name") + 1]


def _argv_checkout_dir(argv: list[str]) -> Path | None:
    """The HOST side of the argv's ``/work`` bind mount, if it has one.

    Read off the argv rather than threaded through every caller, for the same
    reason :func:`_argv_runtime_and_name` is: the directory a quota is enforced
    over must be the directory that was actually mounted, and a second parameter
    is a second thing that can disagree with the argv.
    """
    for flag, spec in zip(argv, argv[1:], strict=False):
        if flag != "-v":
            continue
        host, _, rest = spec.partition(":")
        if host and rest.split(":")[0] == WORK_MOUNT_PATH:
            return Path(host)
    return None


def _argv_memory_limit(argv: list[str] | None) -> str | None:
    """The ``--memory`` value this launch actually carried, if it is readable.

    Read off the argv for the reason :func:`_argv_checkout_dir` is: the cap a
    report names must be the cap the container ran under, and a second
    parameter threaded through the handle is a second thing that can disagree
    with the argv.
    """
    launched = argv or []
    for flag, value in zip(launched, launched[1:], strict=False):
        if flag == "--memory":
            return value
    return None


def _argv_cidfile(argv: list[str]) -> Path | None:
    """Return the host cidfile named by a container-run argv, if present."""
    try:
        return Path(argv[argv.index("--cidfile") + 1])
    except (ValueError, IndexError):
        return None


def _launched_container_id(argv: list[str]) -> str | None:
    """Read only a validated container ID from this launch's host cidfile."""
    from kew.progress import _warn

    cidfile = _argv_cidfile(argv)
    if cidfile is None or not cidfile.exists():
        return None
    try:
        container_id = cidfile.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        _warn("Sandbox: cannot read launch container id")
        return None
    if not container_id:
        return None
    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        _warn("Sandbox: invalid container id in launch cidfile; refusing identity")
        return None
    return container_id


def _cpu_cap_rejection_maximum(line: str) -> Decimal | None:
    """Return the validated maximum from a daemon CPU-range rejection."""
    match = _CPU_CAP_REJECTION.fullmatch(line.rstrip("\r\n"))
    if match is None:
        return None
    minimum = Decimal(match.group("minimum"))
    maximum = Decimal(match.group("maximum"))
    available = Decimal(match.group("available"))
    if minimum != Decimal("0.01") or maximum != available or available < 1:
        return None
    return maximum


def _is_cpu_cap_rejection(line: str) -> bool:
    """Return whether one runtime line is the daemon CPU-range rejection."""
    return _cpu_cap_rejection_maximum(line) is not None


def _container_absence_confirmed(argv: list[str]) -> bool:
    """Confirm absence of this launch's ID, without claiming its name is free.

    No recorded ID means this launch has no known container to remove.
    Another launch may still hold the configured name.
    """
    runtime, name = _argv_runtime_and_name(argv)
    container_id = _launched_container_id(argv)
    if container_id is None:
        return True
    try:
        result = subprocess_utils.run(
            [
                Path(runtime).name,
                "ps",
                "-a",
                "--filter",
                f"id={container_id}",
                "--format",
                "{{.ID}}",
            ],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from kew.progress import _warn

        _warn(
            f"Sandbox: cannot confirm container {name!r} is absent: "
            f"{exception_text(exc)}"
        )
        return False
    stdout = getattr(result, "stdout", None)
    return (
        getattr(result, "returncode", 1) == 0
        and isinstance(stdout, str)
        and not stdout.strip()
    )


def _cpu_cap_retry_identity_cleared(argv: list[str]) -> bool:
    """Retry only before creation, clearing an empty cidfile after client exit."""
    if _launched_container_id(argv) is not None:
        return False
    cidfile = _argv_cidfile(argv)
    if cidfile is not None:
        try:
            cidfile.unlink(missing_ok=True)
        except OSError:
            from kew.progress import _warn

            _warn("Sandbox: cannot clear cidfile for CPU-cap retry")
            return False
    return True


def _resize_test_workers(argv: list[str], cpu_limit: str) -> list[str]:
    """Re-point every test-pool ``-e`` value at the retry's ``--cpus``."""
    sized = {
        assignment.split("=", 1)[0]: assignment
        for assignment in _worker_pool_assignments(cpu_limit)[1::2]
    }
    return [
        sized.get(arg.split("=", 1)[0], arg)
        if index > 0 and argv[index - 1] == "-e"
        else arg
        for index, arg in enumerate(argv)
    ]


def _cpu_cap_retry_argv(
    argv: list[str], returncode: int | None, advertised_maximum: Decimal
) -> list[str] | None:
    """Return one-CPU retry argv after a confirmed pre-creation rejection."""
    if returncode != _DOCKER_LAUNCH_REFUSAL_EXIT_STATUS:
        return None
    try:
        cpu_value_index = argv.index("--cpus") + 1
        current_limit = argv[cpu_value_index]
    except (ValueError, IndexError) as exc:
        from kew.progress import _warn

        _warn(
            f"Sandbox: cannot build CPU-cap retry from launch arguments: "
            f"{exception_text(exc)}"
        )
        return None
    try:
        attempted_limit = Decimal(current_limit)
    except InvalidOperation:
        from kew.progress import _warn

        _warn("Sandbox: cannot build CPU-cap retry from nonnumeric --cpus value")
        return None
    if not attempted_limit.is_finite() or attempted_limit <= advertised_maximum:
        return None
    if not _cpu_cap_retry_identity_cleared(argv):
        return None

    runtime, name = _argv_runtime_and_name(argv)
    _daemon_cpu_count(Path(runtime).name)
    retry = _resize_test_workers(list(argv), _CPU_LIMIT_UNKNOWN_DAEMON)
    retry[cpu_value_index] = _CPU_LIMIT_UNKNOWN_DAEMON

    from kew.progress import _warn

    _warn(
        f"Sandbox: {runtime} rejected the CPU cap before creating {name}; "
        "retrying once at 1 CPU"
    )
    return retry


def _completed_cpu_cap_rejection_maximum(
    result: subprocess.CompletedProcess[str],
) -> Decimal | None:
    """Return the latest validated CPU maximum from captured process output."""
    latest: Decimal | None = None
    for output in (result.stdout, result.stderr):
        if not isinstance(output, str):
            continue
        for line in output.splitlines():
            maximum = _cpu_cap_rejection_maximum(line)
            if maximum is not None:
                latest = maximum
    return latest


class ContainerCleanupFailed(subprocess.TimeoutExpired):
    """A launch timed out AND its container could not be proven gone.

    Still a timeout to every caller, but the cidfile is kept: it is the only
    durable identity of a container that may be running under a cap nobody is
    watching, and deleting it would make that container untraceable.
    """


def _remove_launched_container(argv: list[str]) -> bool:
    """Kill and remove only this launch's recorded ID; confirm removal.

    ``subprocess.run`` on timeout kills only the local ``docker run`` client;
    the container keeps running under its cap, holding the name that the next
    launch needs and the CPUs it was given. A ``rm -f`` that fails is followed
    by an absence check, because "no such container" is also a stable end.
    """
    runtime, name = _argv_runtime_and_name(argv)
    container_id = _launched_container_id(argv)
    if container_id is None:
        from kew.progress import _warn

        _warn(f"Sandbox: launch of {name} recorded no container id; nothing removed")
        return False
    try:
        removed = subprocess_utils.run(
            [Path(runtime).name, "rm", "-f", container_id],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from kew.progress import _warn

        _warn(f"Sandbox: could not remove container {name!r}: {exception_text(exc)}")
        return False
    if removed.returncode == 0:
        return True
    return _container_absence_confirmed(argv)


class CheckoutQuotaExceeded(RuntimeError):
    """A synchronous container breached the shared checkout resource guard."""

    def __init__(self, observation: ResourceCapObservation) -> None:
        super().__init__(f"checkout-quota refusal: {observation.reason}")
        self.observation = observation
        self.cleanup_failed = False


def _checkout_quota_monitor(argv: list[str]) -> Callable[[], None]:
    """Use the dispatch guard against the checkout actually mounted at /work."""
    from functools import partial

    guard = _StreamGuard(
        checkout_dir=_argv_checkout_dir(argv),
        wall_clock_seconds=float("inf"),
        transcript_cap=TRANSCRIPT_BYTE_CAP,
        checkout_quota=CHECKOUT_QUOTA_BYTES,
        checkout_entry_quota=CHECKOUT_QUOTA_ENTRIES,
        host_free_floor=HOST_FREE_SPACE_FLOOR_BYTES,
        watchdog_interval=WATCHDOG_INTERVAL_SECONDS,
        now=time.monotonic,
    )

    return partial(_check_checkout_quota, guard)


def _check_checkout_quota(guard: _StreamGuard) -> None:
    """Raise when the mounted checkout exceeds the shared resource guard."""
    breach = guard.timed_breach()
    if breach is not None:
        raise CheckoutQuotaExceeded(breach)


def _run_container_attempt(
    argv: list[str], deadline: float, output_limit: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run under *deadline*, retaining state for inspection before removal."""
    from kew.run_credentials import launch_environment

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(argv, 0)
    # Retain runtime state until completion is inspected. These internal run
    # builders place --rm immediately after the operation, before project args.
    launch_argv = argv[:2] + argv[3:] if argv[2:3] == ["--rm"] else argv
    launch_env = launch_environment(os.environ, None, contained=True)
    try:
        if output_limit is not None:
            return subprocess_utils.run_bounded(
                launch_argv,
                deadline=deadline,
                output_limit=output_limit,
                monitor=_checkout_quota_monitor(argv),
                poll_interval=WATCHDOG_INTERVAL_SECONDS,
                env=launch_env,
            )
        return subprocess_utils.run(
            launch_argv, check=False, timeout=remaining, env=launch_env
        )
    except (subprocess_utils.OutputBudgetExceeded, CheckoutQuotaExceeded) as exc:
        exc.cleanup_failed = not _remove_launched_container(argv)
        if exc.cleanup_failed:
            from kew.progress import _warn

            _warn(f"Sandbox: {exc}; container removal could not be confirmed")
        raise
    except subprocess.TimeoutExpired as exc:
        if _remove_launched_container(argv):
            raise
        from kew.progress import _warn

        _, name = _argv_runtime_and_name(argv)
        _warn(
            f"Sandbox: container {name!r} timed out and could not be proven "
            "removed; its cidfile is kept so it can still be traced."
        )
        raise ContainerCleanupFailed(argv, remaining) from exc


class _UnconfirmedContainerCompletion(subprocess.CompletedProcess[str]):
    """Preserve client output and status when runtime completion is unconfirmed."""


def _container_completion_confirmed(argv: list[str], returncode: int) -> bool:
    """Confirm completion through runtime state, independently of project output."""
    from kew.progress import _warn

    runtime, name = _argv_runtime_and_name(argv)
    container_id = _launched_container_id(argv)
    if container_id is None:
        return False
    try:
        inspected = subprocess_utils.run(
            [
                Path(runtime).name,
                "inspect",
                "--type",
                "container",
                "--format",
                '{"Name":{{json .Name}},"State":{{json .State}}}',
                container_id,
            ],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        data = json.loads(inspected.stdout) if inspected.returncode == 0 else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        _warn("Sandbox: container completion inspection failed")
        return False
    if not isinstance(data, dict):
        return False
    # Docker reports the name as "/<name>"; Podman reports "<name>". Accept exactly
    # those two encodings of the configured name and nothing else.
    if data.get("Name") not in {name, "/" + name}:
        return False
    state = data.get("State")
    return (
        isinstance(state, dict)
        and state.get("Status") == "exited"
        and type(state.get("ExitCode")) is int
        and state["ExitCode"] == returncode
    )


def _container_name_conflict(result: subprocess.CompletedProcess[str]) -> bool:
    """Recognize Docker and Podman name refusals without exposing stderr."""
    if result.returncode != _DOCKER_LAUNCH_REFUSAL_EXIT_STATUS:
        return False
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    return (
        "is already in use by container" in stderr
        or re.search(r"container name[^\r\n]*is already in use", stderr) is not None
    )


def container_client_failure(result: subprocess.CompletedProcess[str]) -> str | None:
    """Explain when a runtime exit cannot establish container completion.

    Docker and Podman reserve 125 for runtime failures. Other nonnegative
    statuses require confirmation from runtime state. Project output never
    establishes runtime health. Keep failure diagnostics static.
    """
    if isinstance(result, _UnconfirmedContainerCompletion):
        return "container completion could not be confirmed"
    if result.returncode is None:
        return "runtime client did not report an exit status"
    if result.returncode < 0:
        return f"runtime client terminated by signal {-result.returncode}"
    if result.returncode == _DOCKER_LAUNCH_REFUSAL_EXIT_STATUS:
        if _container_name_conflict(result):
            return "container name already in use"
        return "runtime client failed with exit 125"
    return None


def _remove_completed_container(argv: list[str]) -> bool:
    """Replace deferred auto-removal after a confirmed exit, without force."""
    from kew.progress import _warn

    runtime, name = _argv_runtime_and_name(argv)
    container_id = _launched_container_id(argv)
    if container_id is None:
        _warn(f"Sandbox: launch of {name} recorded no container id; nothing removed")
        return False
    try:
        removed = subprocess_utils.run(
            [Path(runtime).name, "rm", container_id],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        _warn(
            f"Sandbox: completed container {name!r} removal failed; its cidfile is kept"
        )
        return False
    if removed.returncode == 0:
        return True
    _warn(
        f"Sandbox: completed container {name!r} removal unconfirmed; "
        "its cidfile is kept"
    )
    return False


def _confirm_container_result(
    argv: list[str], result: subprocess.CompletedProcess[str]
) -> subprocess.CompletedProcess[str]:
    """Carry one completion observation through cleanup and verification."""
    if container_client_failure(result) is not None:
        return result
    if _container_completion_confirmed(argv, result.returncode):
        return result
    from kew.progress import _warn

    _warn("Sandbox: container completion could not be confirmed")
    return _UnconfirmedContainerCompletion(
        result.args, result.returncode, result.stdout, result.stderr
    )


def _cleanup_incomplete_container_exit(
    argv: list[str], outcome: subprocess.CompletedProcess[str] | BaseException
) -> bool:
    """Allow cidfile deletion only after completion or confirmed removal.

    Exceptions and abnormal client results share this rule. Preserve the
    original outcome if cleanup fails or is interrupted. A validated CPU
    refusal with confirmed absence needs no removal; it never made a container.
    """
    from kew.progress import _warn

    reason: str | None
    if isinstance(outcome, BaseException):
        reason = f"{type(outcome).__name__}: {outcome}"
    else:
        reason = container_client_failure(outcome)
        if outcome.returncode is not None and _launched_container_id(argv) is None:
            # The client exited without recording an identity. There is nothing
            # to trace; this says nothing about another launch with the same name.
            return True
        if reason is None:
            return _remove_completed_container(argv)
        if (
            outcome.returncode == _DOCKER_LAUNCH_REFUSAL_EXIT_STATUS
            and _completed_cpu_cap_rejection_maximum(outcome) is not None
            and _container_absence_confirmed(argv)
        ):
            return True
    _, name = _argv_runtime_and_name(argv)
    removed = False
    try:
        removed = _remove_launched_container(argv)
    except BaseException as cleanup_exc:
        _warn(
            f"Sandbox: cleanup of container {name!r} failed: "
            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        )
    if not removed:
        _warn(
            f"Sandbox: container {name!r} failed with {reason}; "
            "removal could not be confirmed; "
            "its cidfile is kept so it can still be traced."
        )
    return removed


def run_container_with_cpu_cap_retry(
    argv: list[str], *, timeout: float, output_limit: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one container, with one guarded retry after a CPU-cap refusal.

    Both attempts share ONE deadline of *timeout* seconds from entry, so a slow
    first refusal cannot double the wall-clock budget. Exceptions and abnormal
    runtime-client exits require confirmed removal before the cidfile goes.
    Other statuses need matching exited runtime state. Defer auto-removal until
    inspection, then remove normally. Keep the cidfile if removal is unconfirmed.
    """
    cidfile = _argv_cidfile(argv)
    deadline = time.monotonic() + timeout
    keep_cidfile = False
    try:
        result = _run_container_attempt(argv, deadline, output_limit)
        maximum = _completed_cpu_cap_rejection_maximum(result)
        if maximum is not None and not _container_name_conflict(result):
            retry_argv = _cpu_cap_retry_argv(argv, result.returncode, maximum)
            if retry_argv is not None:
                result = _run_container_attempt(retry_argv, deadline, output_limit)
        keep_cidfile = True
        result = _confirm_container_result(argv, result)
        keep_cidfile = not _cleanup_incomplete_container_exit(argv, result)
        return result
    except (subprocess_utils.OutputBudgetExceeded, CheckoutQuotaExceeded) as exc:
        keep_cidfile = exc.cleanup_failed
        raise
    except ContainerCleanupFailed:
        keep_cidfile = True
        raise
    except subprocess.TimeoutExpired:
        # The attempt already confirmed removal, or never launched.
        raise
    except BaseException as exc:
        # This boundary owns both attempts and their identity file. Preserve
        # that identity until cleanup succeeds, even if cleanup is interrupted.
        keep_cidfile = True
        keep_cidfile = not _cleanup_incomplete_container_exit(argv, exc)
        raise
    finally:
        if cidfile is not None and not keep_cidfile:
            with contextlib.suppress(OSError):
                cidfile.unlink(missing_ok=True)


CONTAINER_START_FAILED = stop_recovery.CONTAINER_START_FAILED


class ContainerStartFailed(RuntimeError):
    """kew could not create or start the container it named (#2973).

    The name is the operator's lead: a refusal here costs no tokens, so read
    as a bare exception it looked like a rate limit rather than a container.
    """

    category = CONTAINER_START_FAILED

    def __init__(self, name: str, detail: str, *, name_conflict: bool = False) -> None:
        super().__init__(f"container {name!r} could not be started: {detail}")
        self.container_name = name
        self.name_conflict = name_conflict


#: A ``create`` the runtime did not answer in time, on every attempt (#3103).
#: The host was too slow, not the run wrong: no agent ever started.
RUNTIME_TIMEOUT = "runtime_timeout"


class ContainerCreateTimedOut(ContainerStartFailed):
    """Every ``create`` of the container timed out before the runtime answered.

    Still ambiguous like any failed create: the daemon may have made the
    container after its client was killed, so nothing is stopped by name.
    """

    category = RUNTIME_TIMEOUT

    def __init__(self, name: str, attempts: int, timeout: float) -> None:
        super().__init__(
            name,
            "the host was too slow to create the container (create timed out "
            f"after {timeout:g} seconds, {attempts} attempt"
            f"{'' if attempts == 1 else 's'})",
        )
        self.attempts = attempts


# Every registered container carries the ledger and run that created it, so a
# leftover holding a host-wide ``kew-<item>`` name can be traced to its owner
# before anything removes it (#2973). The store's reaper reads them too.
KEW_LEDGER_LABEL = stop_recovery.KEW_LEDGER_LABEL
KEW_RUN_LABEL = stop_recovery.KEW_RUN_LABEL

#: How much of a failed ``create``'s stderr reaches the run's reason (#2913).
CREATE_STDERR_MAX_CHARS = 300


def runtime_stderr_detail(stderr: str | None) -> str:
    """The runtime's own words on a failure: redacted, one line, bounded."""
    # Headers are redacted before the lines are joined; the joined text is then
    # rechecked for host credentials (the order redact_credential_values asks).
    text = redact_credential_values(" ".join(redact_credentials(stderr or "").split()))
    if len(text) > CREATE_STDERR_MAX_CHARS:
        text = text[: CREATE_STDERR_MAX_CHARS - 1] + "…"
    return text


@dataclass(frozen=True)
class NeverStartedContainer:
    """A container the runtime reported ``created`` with no process."""

    container_id: str
    labels: Mapping[str, str]


def _inspect_container(runtime: str, target: str, fmt: str) -> object | None:
    try:
        return subprocess_utils.run(
            [Path(runtime).name, "inspect", "--type", "container", "--format", fmt]
            + [target],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from kew.progress import _warn

        _warn(f"Sandbox: cannot read container {target!r}: {exception_text(exc)}")
        return None


def read_never_started_container(
    runtime: str, target: str
) -> NeverStartedContainer | None:
    """Read *target* only when it was created and never started.

    Any unreadable answer is None, so a caller only ever removes a container
    the runtime itself reported as ``created`` with no process.
    """
    result = _inspect_container(
        runtime,
        target,
        "{{.Id}} {{.State.Status}} {{.State.Pid}} {{json .Config.Labels}}",
    )
    stdout = getattr(result, "stdout", None)
    if getattr(result, "returncode", 1) != 0 or not isinstance(stdout, str):
        return None
    fields = stdout.strip().split(" ", 3)
    if len(fields) != 4 or fields[1:3] != ["created", "0"]:
        return None
    if not _FULL_CONTAINER_ID.fullmatch(fields[0]):
        return None
    return NeverStartedContainer(fields[0], _container_labels(fields[3]))


def _container_labels(text: str) -> Mapping[str, str]:
    """Labels as the runtime printed them; unreadable ones read as no owner."""
    try:
        labels = json.loads(text)
    except ValueError as exc:
        from kew.progress import _warn

        _warn(f"Sandbox: container labels unreadable: {exception_text(exc)}")
        labels = None
    if not isinstance(labels, dict):
        return {}
    return {k: v for k, v in labels.items() if isinstance(v, str)}


def never_started_container(runtime: str, target: str) -> str | None:
    """Return the full id of *target* when it was created and never started."""
    container = read_never_started_container(runtime, target)
    return None if container is None else container.container_id


def container_absent(runtime: str, container_id: str) -> bool:
    """The runtime itself answered that *container_id* no longer exists.

    Docker says "No such container", Podman "no such object"; either counts
    only when it names this exact id. An unreachable daemon or any other
    refusal is not absence.
    """
    from kew.stop_recovery import container_missing

    result = _inspect_container(runtime, container_id, "{{.Id}}")
    stderr = getattr(result, "stderr", None)
    return (
        result is not None
        and getattr(result, "returncode", 0) != 0
        and isinstance(stderr, str)
        and container_missing(stderr, container_id, "")
    )


def runtime_daemon_identity(runtime: str) -> str | None:
    """Name the daemon *runtime* talks to now; ``None`` when it cannot say.

    A container id only means something on the daemon that holds it, so a
    fact about one (its absence) is only read back from that same daemon.
    The template and the answer's shape are the store layer's
    (:func:`kew.stop_recovery.daemon_identity_answer`), so a daemon recorded
    here compares equal to the one recovery names later.
    """
    name = Path(runtime).name
    template = stop_recovery.DAEMON_IDENTITY_TEMPLATES.get(name)
    if template is None:
        return None
    try:
        result = subprocess_utils.run(
            [name, "info", "--format", template],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from kew.progress import _warn

        _warn(f"Sandbox: cannot name the {name} daemon: {exception_text(exc)}")
        return None
    return stop_recovery.daemon_identity_answer(name, result)


def remove_never_started_container(runtime: str, container_id: str) -> bool:
    """Remove one never-started container by id, without force.

    ``rm`` without ``-f`` refuses a running container, so a start that lands
    between the state read and the removal keeps its container. A refused
    ``rm`` still succeeds when the runtime then answers that this exact id is
    gone: a concurrent remover got there first, and the name is free.
    """
    try:
        result = subprocess_utils.run(
            [Path(runtime).name, "rm", container_id],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from kew.progress import _warn

        _warn(
            f"Sandbox: cannot remove never-started container {container_id}: "
            f"{exception_text(exc)}"
        )
        return False
    return getattr(result, "returncode", 1) == 0 or container_absent(
        runtime, container_id
    )


def remove_unstarted_start(
    runtime: str, container_id: str | None, argv: Sequence[str] | None
) -> bool:
    """Remove the container a ``start`` launch named, if it never started.

    Only a ``start`` of a container kew created itself qualifies: an ``exec``
    into a sidecar worker belongs to that worker's supervisor. The container
    is read and removed by its full id, never by name.
    """
    if container_id is None or argv is None or list(argv[1:2]) != ["start"]:
        return False
    if never_started_container(runtime, container_id) is None:
        return False
    return remove_never_started_container(runtime, container_id)


def settle_never_started_start(handle: object, result: DispatchResult) -> None:
    """A start that never ran its container fails as a container start (#2973).

    The client can exit (``docker start`` refusing with 1) while the container
    it was to start stays ``created`` under the item's name. Removing it is
    what proves it never ran, so only then is the refusal renamed.
    """
    remove_unstarted = getattr(handle, "remove_if_never_started", None)
    if result.launched is True or not callable(remove_unstarted):
        return
    try:
        removed = remove_unstarted() is True
    except Exception as exc:
        from kew.progress import _warn

        _warn(
            "Sandbox: never-started container removal unconfirmed: "
            f"{exception_text(exc)}"
        )
        return
    if removed and result.error_category in {"", "sandbox_startup_refused"}:
        detail = result.stop_reason or "start exited before the container ran"
        result.launched = False
        result.error_category = CONTAINER_START_FAILED
        result.stop_reason = str(
            ContainerStartFailed(str(getattr(handle, "name", "")), detail)
        )


def create_registered_container(
    argv: list[str], *, credential: BoundedRunKey | None = None
) -> str:
    """Create without executing the agent, retaining its cidfile for recovery."""
    create = list(argv)
    if create[1] != "run":
        raise ValueError("registered container creation requires a run command")
    create[1] = "create"
    result = subprocess_utils.run(
        create,
        check=False,
        timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        credential=credential,
    )
    maximum = _completed_cpu_cap_rejection_maximum(result)
    refusal_code = 125 if result.returncode == 1 else result.returncode
    retry = (
        None if maximum is None else _cpu_cap_retry_argv(create, refusal_code, maximum)
    )
    if retry is not None:
        result = subprocess_utils.run(
            retry,
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            credential=credential,
        )
    if result.returncode != 0:
        raise _create_refused(argv, result)
    cidfile = _argv_cidfile(argv)
    if cidfile is None:
        raise RuntimeError("registered container cidfile missing")
    identity = cidfile.read_text(encoding="utf-8").strip()
    if _FULL_CONTAINER_ID.fullmatch(identity) is None:
        raise RuntimeError("registered container identity malformed")
    return identity


def _create_refused(
    argv: list[str], result: subprocess.CompletedProcess[str]
) -> ContainerStartFailed:
    """Name the container a ``create`` failed to make, in the runtime's words."""
    _, name = _argv_runtime_and_name(argv)
    # `create` reports a daemon refusal as 1 where `run` reports 125.
    refusal = subprocess.CompletedProcess(
        result.args,
        125 if result.returncode == 1 else result.returncode,
        result.stdout,
        result.stderr,
    )
    if _container_name_conflict(refusal):
        return ContainerStartFailed(name, "name already in use", name_conflict=True)
    detail = runtime_stderr_detail(result.stderr)
    return ContainerStartFailed(
        name,
        f"create exited with status {result.returncode}"
        + (f": {detail}" if detail else ""),
    )


def _launch_container_client(
    argv: list[str],
    on_spawn: Callable[[subprocess.Popen[str]], None] | None,
    *,
    env: dict[str, str],
    credential: BoundedRunKey | None,
    stderr: int = subprocess.STDOUT,
) -> subprocess.Popen[str]:
    """Keep every client, including CPU retries, behind its registration gate.

    *stderr* is the client's stderr disposition, which every retry inherits with
    the launcher: merged into the stdout pipe by default, or ``PIPE`` for a run
    whose stream must carry the container's stdout alone (#2053).
    """
    if on_spawn is None:
        return subprocess_utils.popen(
            argv, env=env, credential=credential, stderr=stderr
        )
    proc = subprocess_utils.popen(
        argv,
        env=env,
        credential=credential,
        start_new_session=True,
        gate=True,
        stderr=stderr,
    )
    try:
        on_spawn(proc)
    except BaseException as exc:
        from kew.progress import _warn

        _warn(f"Sandbox client registration failed for {proc.pid}: {exc}")
        subprocess_utils.release_spawn(proc, registered=False)
        proc.wait(timeout=10)
        raise
    return proc


def launch_container(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    credential: BoundedRunKey | None = None,
    container_name: str | None = None,
    container_id: str | None = None,
    checkout_dir: Path | None = None,
    admission: PendingAdmission | None = None,
    on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
    prepare: Callable[[list[str]], ContainedExecution] | None = None,
    separate_stderr: bool = False,
) -> ContainerHandle:
    """Launch a sandboxed container and return a handle for streaming its stdout.

    argv must be the full docker-run argv (as produced by build_run_argv).
    The runtime is argv[0]; the container name follows the --name argument, and
    the checkout the quota applies to is the argv's ``/work`` bind source.

    With *separate_stderr* the client gets a SECOND pipe and the returned
    handle's iterable is the container's stdout alone (#2053). The stderr pipe
    is drained by the handle, because an unread one blocks the container; the
    caller that needs the merged operator view keeps the default.
    """
    startup_nonce = _startup_nonce(argv)
    if prepare is not None:
        execution = prepare(argv)
        startup_nonce = execution.startup_nonce or startup_nonce
        argv = list(execution.argv)
        container_name = execution.name
        container_id = execution.container_id
        checkout_dir = execution.checkout_dir
    if container_name is None:
        runtime, name = _argv_runtime_and_name(argv)
    else:
        runtime, name = argv[0], container_name
    from kew.run_credentials import launch_environment

    event_window_start = time.time()
    launch_env = launch_environment(
        os.environ if env is None else env, None, contained=True
    )
    launcher = partial(
        _launch_container_client,
        on_spawn=on_spawn,
        env=launch_env,
        credential=credential,
        stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
    )
    proc = None
    handle = None
    with execution_cancellation(admission):
        try:
            with DeferredSIGINT():
                proc = launcher(argv)
                handle = ContainerHandle(
                    proc,
                    name,
                    runtime,
                    checkout_dir=(
                        _argv_checkout_dir(argv)
                        if checkout_dir is None
                        else checkout_dir
                    ),
                    cidfile=None if container_id is not None else _argv_cidfile(argv),
                    container_id=container_id,
                    launch_argv=argv,
                    startup_nonce=startup_nonce,
                    cpu_retry_launcher=launcher,
                    event_window_start=event_window_start,
                    separate_stderr=separate_stderr,
                )
                if admission is not None:
                    handle.accept_admission(admission)
            return handle
        except BaseException:
            with DeferredSIGINT():
                _cleanup_interrupted_container_start(proc, handle)
            raise


def _cleanup_interrupted_container_start(
    proc: subprocess.Popen[str] | None, handle: ContainerHandle | None
) -> None:
    if handle is not None:
        handle.cancel_launch()
    else:
        _reap_interrupted_client(proc)


def _reap_interrupted_client(proc: subprocess.Popen[str] | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@dataclass(frozen=True)
class ContainedExecution:
    """A run that executes inside a container instead of on the operator's host.

    Carries the launch argv plus the two facts that killing it needs. The kill
    is why this is a type and not a bare argv: terminating the local
    ``docker run`` client does not stop the container, so a budget, breaker or
    pause kill has to reach it by name (``docker kill kew-<slug>``). A caller
    that passed only the argv would launch a contained run the watchdogs could
    not stop — which is the state #1462 describes.

    Handed to :func:`kew.orchestrator.run_agent` in place of building a host
    command. Everything around execution — retry, budget, breaker, pause,
    cost accounting — is then the same code the host path runs, rather than a
    second implementation of it.
    """

    argv: tuple[str, ...]
    name: str
    runtime: str
    checkout_dir: Path | None = None
    container_id: str | None = None
    prepared: bool = False
    startup_nonce: str = ""

    @classmethod
    def from_argv(cls, argv: list[str]) -> ContainedExecution:
        """Build from a full container-run argv (as :func:`build_run_argv` returns)."""
        runtime, name = _argv_runtime_and_name(argv)
        return cls(
            argv=tuple(argv),
            startup_nonce=_startup_nonce(argv),
            name=name,
            runtime=runtime,
            checkout_dir=_argv_checkout_dir(argv),
        )

    def remove_if_never_started(self) -> bool:
        """Remove the prepared container a start that never spawned left behind."""
        return self.prepared and remove_unstarted_start(
            self.runtime, self.container_id, self.argv
        )

    def handle_for(
        self, proc: subprocess.Popen[str], *, event_window_start: float | None = None
    ) -> ContainerHandle:
        """The kill handle for an already-launched *proc* running :attr:`argv`.

        The launching caller owns the process (it needs the pipe for streaming),
        so the handle is built around it afterwards rather than by
        :func:`launch_container`.
        """
        return ContainerHandle(
            proc,
            self.name,
            self.runtime,
            checkout_dir=self.checkout_dir,
            cidfile=(None if self.prepared else _argv_cidfile(list(self.argv))),
            container_id=self.container_id,
            launch_argv=list(self.argv),
            startup_nonce=self.startup_nonce,
            event_window_start=event_window_start,
        )


# ── kew sandbox smoke (#691) ─────────────────────────────────────────────────
# A one-shot preflight that launches ONE real container through kew's own
# build_run_argv/launch_container and names the failure layer. It proves the
# live path the mock suite cannot: runtime resolution, image presence (building
# locally if absent), container launch, and the prompt-file handoff — WITHOUT
# needing an API key (the container just echoes the mounted prompt file back).
# A final env-only "auth" layer then checks the credential a real dispatch
# needs (#721) — no API call is made.

_SMOKE_TOKEN = "kew-sandbox-smoke-ok"  # nosec B105 - marker echoed by the smoke container, not a secret

# Emitted only after the container proves the agent HOME is writable — the
# layer the original smoke was blind to (#742: read-only root broke Claude
# Code's Bash tool at shell-init, EROFS on ~/.claude).
_SMOKE_HOME_TOKEN = "kew-sandbox-home-writable"  # nosec B105 - marker, not a secret

# The smoke's in-container script: echo the mounted prompt file (mount/handoff
# proof), then probe that $HOME accepts the exact write Claude Code's shell
# init performs (mkdir ~/.claude).
_SMOKE_SCRIPT = (
    f"cat {PROMPT_MOUNT_PATH}"
    ' && mkdir -p "$HOME/.claude"'
    ' && touch "$HOME/.claude/probe"'
    f" && echo {_SMOKE_HOME_TOKEN}"
)

# Ordered failure layers, most-upstream first (used for docs + tests). "auth"
# is last: the container plumbing is proven credential-free (the smoke echoes
# the prompt file, never invokes claude), then the env credential a REAL
# dispatch needs is checked — the layer the original smoke was blind to (#721).
SMOKE_LAYERS = ("runtime", "image", "launch", "execution", "auth")


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of ``kew sandbox smoke``.

    ``layer`` is ``"ok"`` on success, else the first failing layer (one of
    :data:`SMOKE_LAYERS`). ``detail`` is a one-line human explanation.
    """

    ok: bool
    layer: str
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", redact_credentials(self.detail))


def _repo_root() -> Path | None:
    """Return the source root used by the git-based staleness check."""
    # sandbox.py = <repo>/kew/src/kew/sandbox.py → parents[3] = <repo>
    root = Path(__file__).resolve().parents[3]
    if (root / "docker" / "agent.Dockerfile").is_file():
        return root
    return None


def _image_exists_locally(runtime: str, image: str) -> bool:
    """True if ``image`` is present in the local runtime (no registry pull)."""
    result = subprocess_utils.run([runtime, "image", "inspect", image], check=False)
    return result.returncode == 0


def packaged_agent_image_context() -> Path:
    """Return the installed agent image context after manifest validation."""
    root = Path(__file__).resolve().parent / "_docker"
    docker_root = root / "docker"
    if root.is_symlink() or docker_root.is_symlink():
        raise ValueError("symbolic link in packaged agent image context")
    if not docker_root.is_dir():
        raise FileNotFoundError(
            "packaged agent image inputs are missing; reinstall kew-cli"
        )
    _validate_packaged_agent_image_manifest(root)
    agent_image_inputs_digest(root)
    return root


def _expected_packaged_image_pin(context: Path, runner: str) -> str:
    try:
        stock_tag = _PACKAGED_IMAGE_TAGS[runner]
    except KeyError:
        supported = ", ".join(_PACKAGED_IMAGE_DOCKERFILES)
        raise ValueError(
            f"runner {runner!r} has no packaged image; choose {supported}"
        ) from None
    image_name = stock_tag.partition(":")[0]
    digest = agent_image_inputs_digest(context)[:12]
    return f"{image_name}:docker-{digest}"


def _validate_packaged_image_tag(
    context: Path, runner: str, image: str, *, unpinned: bool = False
) -> bool:
    expected = _expected_packaged_image_pin(context, runner)
    if image == expected:
        return True
    if unpinned:
        return False
    raise ValueError(
        f"image tag {image!r} does not match the packaged inputs; "
        f"expected {expected!r}. Pass --unpinned to build another tag"
    )


def packaged_image_dockerfile(runner: str) -> Path:
    """The packaged Dockerfile *runner*'s image is built from."""
    try:
        dockerfile = _PACKAGED_IMAGE_DOCKERFILES[runner]
    except KeyError:
        supported = ", ".join(_PACKAGED_IMAGE_DOCKERFILES)
        raise ValueError(
            f"runner {runner!r} has no packaged image; choose {supported}"
        ) from None
    return packaged_agent_image_context() / "docker" / dockerfile


def packaged_image_entrypoint(runner: str) -> tuple[str, ...]:
    """The ENTRYPOINT *runner*'s packaged image declares, read off its Dockerfile.

    Read from the same file the image is built from, so a launch that has to
    start the entrypoint itself names the one the image runs. The LAST
    ``ENTRYPOINT`` wins, as it does for the builder, and only the exec form
    (a JSON array) is accepted: the shell form is not one kew writes.
    """
    declared = [
        line.removeprefix("ENTRYPOINT").strip()
        for line in packaged_image_dockerfile(runner)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("ENTRYPOINT ")
    ]
    try:
        entrypoint = json.loads(declared[-1]) if declared else None
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"the packaged {runner} image's ENTRYPOINT is not an exec array: {exc}"
        ) from exc
    if not (
        isinstance(entrypoint, list)
        and entrypoint
        and all(isinstance(word, str) and word for word in entrypoint)
    ):
        raise ValueError(f"the packaged {runner} image declares no exec ENTRYPOINT")
    return tuple(entrypoint)


def build_agent_image(
    runtime: str,
    image: str,
    *,
    runner: str = DEFAULT_IMAGE_RUNNER,
    unpinned: bool = False,
    proxy_environment: str = "none",
) -> subprocess.CompletedProcess[str]:
    """Build one runner image from the inputs installed with kew."""
    try:
        dockerfile = _PACKAGED_IMAGE_DOCKERFILES[runner]
    except KeyError:
        supported = ", ".join(_PACKAGED_IMAGE_DOCKERFILES)
        raise ValueError(
            f"runner {runner!r} has no packaged image; choose {supported}"
        ) from None
    context = packaged_agent_image_context()
    _validate_packaged_image_tag(context, runner, image, unpinned=unpinned)
    env = _build_env(proxy_environment)
    actions = _proxy_variable_actions(proxy_environment, _PROXY_BUILD_NAMES)
    logging.getLogger(__name__).info(
        "Sandbox image build proxy_environment=%s proxy_variables=%s",
        proxy_environment,
        actions,
        extra={"proxy_environment": proxy_environment, "proxy_variables": actions},
    )
    with proxy_redaction_scope(proxy_environment, environment=env):
        result = subprocess_utils.run(
            [
                runtime,
                "build",
                *_proxy_build_args(proxy_environment),
                "-f",
                str(context / "docker" / dockerfile),
                "-t",
                image,
                # What the image was built from, carried by the image itself: a
                # launch reads it back before trusting a docker-<digest> tag.
                "--label",
                inputs_label(context),
                str(context),
            ],
            check=False,
            env=env,
        )
        result.stdout = redact_credentials(result.stdout or "")
        result.stderr = redact_credentials(result.stderr or "")
        return result


#: Host variables a packaged image build may see. The runtime's own settings
#: (DOCKER_HOST, BUILDX_*, ...) and what a process needs to run at all; never a
#: provider key or a GitHub token, which a build has no use for and which a
#: Dockerfile step or a failure tail could otherwise echo into a terminal log.
_BUILD_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TERM",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "ftp_proxy",
        "all_proxy",
        "no_proxy",
    }
)
_BUILD_ENV_PREFIXES = ("DOCKER_", "BUILDX_", "BUILDKIT_", "CONTAINERD_", "ORBSTACK_")


@contextlib.contextmanager
def proxy_redaction_scope(
    proxy_environment: str, *, environment: Mapping[str, str] | None = None
) -> Iterator[None]:
    """Retain launch proxy values only until its diagnostics are sanitized.

    Outside a launch scope, scan credential-shaped host proxies only.
    Host proxies retain full URL and userinfo protection under either policy.
    Only inherited launch values contribute path, query and fragment forms.
    """
    source = os.environ if environment is None else environment
    received = {
        name: source[name]
        for name in CONTAINER_PROXY_NAMES
        if proxy_environment == "inherit" and source.get(name)
    }
    token = _LAUNCH_PROXY_VALUES.set(received)
    marker_token = _CREDENTIAL_MARKERS.set(None)
    try:
        _CREDENTIAL_MARKERS.set(_select_credential_markers(_credential_forms()))
        yield
    except Exception as exc:
        from kew.runner_fields import sanitize_exception_args

        sanitize_exception_args(exc)
        raise
    finally:
        _CREDENTIAL_MARKERS.reset(marker_token)
        _LAUNCH_PROXY_VALUES.reset(token)


def _proxy_password(value: str) -> str | None:
    """The password inside a ``[scheme://]user:password@host`` proxy, if any."""
    try:
        parts, _ = _split_proxy(value)
    except ValueError as exc:
        from kew.progress import _warn

        _warn(
            f"Sandbox: proxy URL could not be parsed for redaction: "
            f"{exception_text(exc)}"
        )
        return None
    return parts.password or None


def _strip_proxy_userinfo(value: str) -> str:
    """A proxy value without its ``user:password@`` — the build gets the host.

    Raises ``ValueError`` for a value that cannot be parsed: a proxy the
    sanitizer cannot read is one it cannot prove credential-free, and it must
    never reach Docker unchanged.
    """
    from urllib.parse import urlunsplit

    parts, has_scheme = _split_proxy(value)
    if parts.username is None and parts.password is None:
        return value
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal: hostname drops the brackets, netloc needs them
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if not has_scheme:
        return host + parts.path
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _build_env(proxy_environment: str = "none") -> dict[str, str]:
    """Limit build credentials and apply the selected host proxy policy."""
    env = {
        name: value
        for name, value in os.environ.items()
        if (name in _BUILD_ENV_NAMES or name.startswith(_BUILD_ENV_PREFIXES))
        and _CREDENTIAL_NAME_RE.search(name) is None
    }
    inherited = _proxy_variable_actions(proxy_environment)["inherited"]
    _require_parseable_inherited_proxies(inherited)
    for name in _PROXY_BUILD_NAMES:
        env[name] = os.environ[name] if name in inherited else ""
    return env


_CONFIG_PROXY_KEYS = {
    "httpProxy": "HTTP_PROXY",
    "httpsProxy": "HTTPS_PROXY",
    "ftpProxy": "FTP_PROXY",
    "allProxy": "ALL_PROXY",
    "noProxy": "NO_PROXY",
}


def _docker_client_config() -> dict[str, object]:
    """The operator's Docker client ``config.json`` as an object, else empty.

    An unreadable file or one that is not a JSON object is said, not
    silently treated as empty.
    """
    from kew.progress import _warn

    source = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")
    source = source / "config.json"
    if not source.is_file():
        return {}
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(
            f"Sandbox: Docker client config unreadable; proxies ignored: "
            f"{exception_text(exc)}"
        )
        return {}
    if not isinstance(loaded, dict):
        _warn("Sandbox: Docker client config is not a JSON object; proxies ignored")
        return {}
    return loaded


def _config_json_proxies() -> dict[str, str]:
    """``proxies.default`` from the Docker client config, userinfo stripped.

    ``noProxy`` is a host list, not a URL, and passes through as it is.
    """
    proxies = _docker_client_config().get("proxies")
    default = proxies.get("default") if isinstance(proxies, dict) else None
    if not isinstance(default, dict):
        return {}
    found: dict[str, str] = {}
    for key, value in default.items():
        if key not in _CONFIG_PROXY_KEYS or not isinstance(value, str):
            continue
        name = _CONFIG_PROXY_KEYS[key]
        found[name] = value if key == "noProxy" else _strip_proxy_userinfo(value)
    return found


def _proxy_build_args(proxy_environment: str) -> list[str]:
    """Inherit selected host names by name and block all Docker proxy defaults.

    FTP_PROXY and ALL_PROXY remain blank under both policies, in both cases.
    The policy permits inheritance only for the six container proxy names.
    """
    inherited = _proxy_variable_actions(proxy_environment)["inherited"]
    _require_parseable_inherited_proxies(inherited)
    return [
        argument
        for name in _PROXY_BUILD_NAMES
        for argument in ("--build-arg", name if name in inherited else f"{name}=")
    ]


# Compatibility for callers of the original authorization-only classifier.
is_authorization_key = is_opaque_credential_key


def contains_short_credential_value(text: str) -> bool:
    """Check short forms with the shared positional engine."""
    return credential_redaction.contains(
        text,
        [item for item in _credential_forms() if item[1].short],
    )


def redact_container_observations(
    observations: list[dict[str, str]] | None,
) -> list[dict[str, str]] | None:
    """Redact container diagnostics while preserving absent evidence."""
    from kew.runner_fields import reported_identity

    if observations is None:
        return None
    return [
        {
            key: reported_identity(value, "resource_cap_code")
            if key == "code"
            else redact_credentials(value)
            for key, value in observation.items()
        }
        for observation in observations
    ]


def effective_network_name(network: str) -> str | None:
    """The Docker network a dispatch under *network* attaches, or None for open."""
    if network == "open":
        return None
    if network == "allowlist":
        return ALLOWLIST_NETWORK
    return network


def classify_egress_verification(
    *,
    route: Literal["direct", "sidecar", "openshell"],
    certified: bool = False,
) -> str:
    """Classify contained egress with permanent direct-route acceptance (#1945).

    Direct egress is operator-asserted and unverifiable by kew.
    Docker exposes no destination rules through its network API.
    Docker Desktop keeps the host firewall inside its VM.
    Network existence, operator configuration, and sidecar evidence cannot
    upgrade the direct route's frozen value.

    ``certified`` is the route's OWN certification verdict, read off the audit
    chain by the route's own admission gate — never a caller's opinion. The
    route decides which proven value it may carry, which is what keeps one
    boundary's evidence from promoting another's.
    """
    if route == "direct":
        return DIRECT_ROUTE_EGRESS_VERIFICATION
    if route == "sidecar":
        return "sidecar-certified" if certified else "unavailable"
    if route == OPENSHELL_EGRESS_ROUTE:
        return OPENSHELL_ROUTE_EGRESS_VERIFICATION if certified else "unavailable"
    raise ValueError(f"Unknown contained egress route: {route!r}")


SWITCHYARD_ATTEMPT_LABEL = "io.kew.switchyard.attempt"

_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_IMMUTABLE_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SWITCHYARD_OWNER = re.compile(r"[0-9a-f]{32}")
_FILTER_HELPER_LIFETIME_SECONDS = 30
_FIREWALL_READ_TIMEOUT_SECONDS = 10


class GlobalFirewallSource(StrEnum):
    """One fixed host-network firewall readback command."""

    NFT = "nft"
    IPTABLES_LEGACY = "iptables-legacy"
    IP6TABLES_LEGACY = "ip6tables-legacy"


@dataclass(frozen=True)
class GlobalFirewallSnapshot:
    """Normalized nftables and legacy iptables host-network rules."""

    nft: str
    iptables_legacy: str
    ip6tables_legacy: str


class WorkerNetnsFilterError(RuntimeError):
    """The worker-only filter could not be installed and proven."""


def _docker_resource_argument(value: str, field: str) -> str:
    """Validate one exact Docker resource argument."""
    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be one exact Docker resource identity")
    return value


def _worker_filter_owner(owner: str) -> str:
    owner = _docker_resource_argument(owner, "owner")
    if _SWITCHYARD_OWNER.fullmatch(owner) is None:
        raise ValueError("owner must be 32 lowercase hexadecimal digits")
    return owner


def _worker_filter_image_id(image_id: str) -> str:
    if _IMMUTABLE_IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError("filter helper image must be an immutable SHA-256 ID")
    return image_id


def _worker_filter_container_id(container_id: str, field: str) -> str:
    if _FULL_CONTAINER_ID.fullmatch(container_id) is None:
        raise ValueError(f"{field} must be a full container ID")
    return container_id


def _worker_filter_proxy(proxy_address: str, proxy_port: int) -> tuple[str, int]:
    if not isinstance(proxy_address, str):
        raise ValueError("proxy_address must be one IPv4 address")
    try:
        address = ipaddress.ip_address(proxy_address)
    except ValueError as exc:
        raise ValueError("proxy_address must be one IPv4 address") from exc
    if address.version != 4 or str(address) != proxy_address:
        raise ValueError("proxy_address must be one IPv4 address")
    if (
        not isinstance(proxy_port, int)
        or isinstance(proxy_port, bool)
        or not 1 <= proxy_port <= 65535
    ):
        raise ValueError("proxy_port must be between 1 and 65535")
    return str(address), proxy_port


def worker_netns_filter_table(owner: str) -> str:
    """Return the nftables table owned by one Switchyard attempt."""
    return f"kew_sc0_{_worker_filter_owner(owner)}"


def build_worker_netns_filter_helper_argv(
    *,
    runtime: str,
    image_id: str,
    worker_id: str,
    owner: str,
    proxy_environment: str = "none",
) -> list[str]:
    """Create one bounded NET_ADMIN helper in the exact worker netns."""
    owner = _worker_filter_owner(owner)
    worker_id = _worker_filter_container_id(worker_id, "worker_id")
    image_id = _worker_filter_image_id(image_id)
    return [
        runtime,
        "create",
        "--name",
        f"kew-sc0-{owner[:12]}-netns-filter",
        "--label",
        f"{SWITCHYARD_ATTEMPT_LABEL}={owner}",
        "--pull=never",
        "--network",
        f"container:{worker_id}",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/run:size=1m,mode=0700",  # nosec B108 - private in-container tmpfs
        "--pids-limit",
        "16",
        "--memory",
        "32m",
        "--memory-swap",
        "32m",
        "--cpus",
        "0.10",
        "--stop-timeout",
        "1",
        "--user",
        "0:0",
        "--entrypoint",
        "sleep",
        *_container_proxy_environment_args(proxy_environment),
        image_id,
        str(_FILTER_HELPER_LIFETIME_SECONDS),
    ]


def build_worker_netns_filter_install_argv(
    *, runtime: str, helper_id: str
) -> list[str]:
    """Build the fixed nftables batch-install command for one helper."""
    return [
        runtime,
        "exec",
        "-i",
        _worker_filter_container_id(helper_id, "helper_id"),
        "nft",
        "-f",
        "-",
    ]


def build_worker_netns_filter_readback_argv(
    *, runtime: str, helper_id: str, owner: str
) -> list[str]:
    """Build a complete JSON readback of one attempt-owned filter table."""
    return [
        runtime,
        "exec",
        _worker_filter_container_id(helper_id, "helper_id"),
        "nft",
        "--json",
        "list",
        "table",
        "inet",
        worker_netns_filter_table(owner),
    ]


def worker_netns_filter_ruleset(
    *, owner: str, proxy_address: str, proxy_port: int
) -> bytes:
    """Return the fixed default-drop OUTPUT table for one worker namespace."""
    table = worker_netns_filter_table(owner)
    address, port = _worker_filter_proxy(proxy_address, proxy_port)
    return (
        f"table inet {table} {{\n"
        " chain output {\n"
        "  type filter hook output priority 0; policy drop;\n"
        "  ct state established accept\n"
        f"  ip daddr {address} tcp dport {port} accept\n"
        " }\n"
        "}\n"
    ).encode("ascii")


def _filter_rule_matches(
    rule: object, *, table: str, expressions: list[dict[str, object]]
) -> bool:
    if not isinstance(rule, dict):
        return False
    observed = dict(rule)
    observed.pop("handle", None)
    return observed == {
        "family": "inet",
        "table": table,
        "chain": "output",
        "expr": expressions,
    }


def _worker_filter_readback_objects(readback: str) -> list[dict[str, object]]:
    try:
        document = json.loads(readback)
        objects = document["nftables"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkerNetnsFilterError("worker filter readback was malformed") from exc
    if not isinstance(objects, list):
        raise WorkerNetnsFilterError("worker filter readback was malformed")
    ruleset: list[dict[str, object]] = []
    for item in objects:
        if isinstance(item, dict) and set(item) == {"metainfo"}:
            continue
        if not isinstance(item, dict) or len(item) != 1:
            raise WorkerNetnsFilterError("worker filter readback did not match")
        ruleset.append(item)
    if len(ruleset) != 4:
        raise WorkerNetnsFilterError("worker filter readback did not match")
    return ruleset


def _validate_worker_filter_structure(
    ruleset: list[dict[str, object]], table: str
) -> None:
    raw_table = ruleset[0].get("table")
    raw_chain = ruleset[1].get("chain")
    if not isinstance(raw_table, dict) or not isinstance(raw_chain, dict):
        raise WorkerNetnsFilterError("worker filter readback did not match")
    table_object = dict(raw_table)
    table_object.pop("handle", None)
    chain_object = dict(raw_chain)
    chain_object.pop("handle", None)
    if table_object != {"family": "inet", "name": table}:
        raise WorkerNetnsFilterError("worker filter table readback did not match")
    if chain_object != {
        "family": "inet",
        "table": table,
        "name": "output",
        "type": "filter",
        "hook": "output",
        "prio": 0,
        "policy": "drop",
    }:
        raise WorkerNetnsFilterError("worker filter chain readback did not match")


def _worker_filter_expected_expressions(
    address: str, port: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    established_match: dict[str, object] = {
        "match": {
            "op": "in",
            "left": {"ct": {"key": "state"}},
            "right": "established",
        }
    }
    established: list[dict[str, object]] = [established_match, {"accept": None}]
    proxy: list[dict[str, object]] = [
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                "right": address,
            }
        },
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                "right": port,
            }
        },
        {"accept": None},
    ]
    return established, proxy


def validate_worker_netns_filter_readback(
    readback: str, *, owner: str, proxy_address: str, proxy_port: int
) -> None:
    """Require the complete readback to contain only the two approved rules."""
    table = worker_netns_filter_table(owner)
    address, port = _worker_filter_proxy(proxy_address, proxy_port)
    ruleset = _worker_filter_readback_objects(readback)
    _validate_worker_filter_structure(ruleset, table)
    established, proxy = _worker_filter_expected_expressions(address, port)
    if not _filter_rule_matches(
        ruleset[2].get("rule"), table=table, expressions=established
    ) or not _filter_rule_matches(
        ruleset[3].get("rule"), table=table, expressions=proxy
    ):
        raise WorkerNetnsFilterError("worker filter rules readback did not match")


_GLOBAL_FIREWALL_COMMANDS: dict[GlobalFirewallSource, tuple[str, ...]] = {
    GlobalFirewallSource.NFT: ("nft", "--json", "list", "ruleset"),
    GlobalFirewallSource.IPTABLES_LEGACY: ("iptables-legacy-save",),
    GlobalFirewallSource.IP6TABLES_LEGACY: ("ip6tables-legacy-save",),
}


def build_global_firewall_observer_argv(
    *,
    runtime: str,
    image_id: str,
    owner: str,
    source: GlobalFirewallSource,
    proxy_environment: str = "none",
) -> list[str]:
    """Build one fixed, read-only host-network firewall observation."""
    if not isinstance(source, GlobalFirewallSource):
        raise ValueError("source must be a GlobalFirewallSource")
    owner = _worker_filter_owner(owner)
    image_id = _worker_filter_image_id(image_id)
    command = _GLOBAL_FIREWALL_COMMANDS[source]
    return [
        runtime,
        "run",
        "--rm",
        "--name",
        f"kew-sc0-{owner[:12]}-observe-{source.value}",
        "--label",
        f"{SWITCHYARD_ATTEMPT_LABEL}={owner}",
        "--pull=never",
        "--network",
        "host",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/run:size=1m,mode=0700",  # nosec B108 - private in-container tmpfs
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
        "--entrypoint",
        command[0],
        *_container_proxy_environment_args(proxy_environment),
        image_id,
        *command[1:],
    ]


def _normalize_nft_snapshot(raw: str) -> str:
    try:
        document = json.loads(raw)
        objects = document["nftables"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkerNetnsFilterError("global nftables readback was malformed") from exc
    if not isinstance(objects, list):
        raise WorkerNetnsFilterError("global nftables readback was malformed")

    def normalize(value: object) -> object:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if key == "counter" and isinstance(item, dict):
                normalized[key] = {
                    counter_key: 0 if counter_key in {"packets", "bytes"} else counter
                    for counter_key, counter in item.items()
                }
            else:
                normalized[key] = normalize(item)
        return normalized

    ruleset = [
        normalize(item)
        for item in objects
        if not (isinstance(item, dict) and set(item) == {"metainfo"})
    ]
    return json.dumps({"nftables": ruleset}, separators=(",", ":"), sort_keys=True)


_IPTABLES_TIMESTAMP = re.compile(r"^# (?:Generated by .+|Completed on .+)$")
_IPTABLES_CHAIN_COUNTERS = re.compile(
    r"^(?P<prefix>:\S+[ \t]+\S+[ \t]+\[)\d+:\d+(?P<suffix>\])$"
)


def _normalize_legacy_snapshot(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        if _IPTABLES_TIMESTAMP.fullmatch(line):
            continue
        line = _IPTABLES_CHAIN_COUNTERS.sub(
            lambda match: f"{match['prefix']}0:0{match['suffix']}", line
        )
        lines.append(line)
    return "\n".join(lines) + "\n"


def capture_global_firewall_snapshot(
    *, runtime: str, image_id: str, owner: str
) -> GlobalFirewallSnapshot:
    """Read nftables plus IPv4 and IPv6 legacy iptables from the host netns."""
    observations: dict[GlobalFirewallSource, str] = {}
    for source in GlobalFirewallSource:
        argv = build_global_firewall_observer_argv(
            runtime=runtime,
            image_id=image_id,
            owner=owner,
            source=source,
        )
        try:
            result = subprocess_utils.run(
                argv,
                check=False,
                timeout=_FIREWALL_READ_TIMEOUT_SECONDS,
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerNetnsFilterError(
                f"global {source.value} readback was unavailable"
            ) from exc
        if result.returncode != 0:
            raise WorkerNetnsFilterError(
                f"global {source.value} readback failed with exit {result.returncode}"
            )
        observations[source] = result.stdout
    return GlobalFirewallSnapshot(
        nft=_normalize_nft_snapshot(observations[GlobalFirewallSource.NFT]),
        iptables_legacy=_normalize_legacy_snapshot(
            observations[GlobalFirewallSource.IPTABLES_LEGACY]
        ),
        ip6tables_legacy=_normalize_legacy_snapshot(
            observations[GlobalFirewallSource.IP6TABLES_LEGACY]
        ),
    )


def build_per_run_network_argv(*, runtime: str, network: str, owner: str) -> list[str]:
    """Build an internal, IPv6-enabled bridge for one sidecar attempt."""
    network = _docker_resource_argument(network, "network")
    owner = _docker_resource_argument(owner, "owner")
    if len(owner) < 14 or any(
        character not in "0123456789abcdef" for character in owner
    ):
        raise ValueError("owner must contain at least 14 lowercase hexadecimal digits")
    ipv6_subnet = f"fd{owner[:2]}:{owner[2:6]}:{owner[6:10]}:{owner[10:14]}::/64"
    return [
        runtime,
        "network",
        "create",
        "--driver",
        "bridge",
        "--internal",
        "--ipv6",
        "--subnet",
        ipv6_subnet,
        "--label",
        f"{SWITCHYARD_ATTEMPT_LABEL}={owner}",
        network,
    ]


def build_network_attachment_argv(
    *, runtime: str, network: str, container_id: str
) -> list[str]:
    """Build one explicit network attachment for one exact container."""
    return [
        runtime,
        "network",
        "connect",
        _docker_resource_argument(network, "network"),
        _docker_resource_argument(container_id, "container_id"),
    ]


def build_resource_inspect_argv(
    *, runtime: str, resource_kind: str, resource_id: str
) -> list[str]:
    """Build an inspection for one container or network identity."""
    if resource_kind not in {"container", "network"}:
        raise ValueError("resource_kind must be 'container' or 'network'")
    return [
        runtime,
        resource_kind,
        "inspect",
        _docker_resource_argument(resource_id, "resource_id"),
    ]


def build_container_cleanup_argv(
    *, runtime: str, container_ids: tuple[str, ...]
) -> list[str]:
    """Build forced cleanup for exact containers owned by one attempt."""
    if not container_ids:
        raise ValueError("container_ids must not be empty")
    identities = [
        _docker_resource_argument(container_id, "container_id")
        for container_id in container_ids
    ]
    return [runtime, "container", "rm", "--force", *identities]


def build_network_cleanup_argv(*, runtime: str, network_id: str) -> list[str]:
    """Build cleanup for one exact per-attempt network identity."""
    return [
        runtime,
        "network",
        "rm",
        _docker_resource_argument(network_id, "network_id"),
    ]


#: What the posture records about an attachment network kew could not read:
#: an unresponsive daemon, a missing network, or output it cannot parse. An
#: unseen attribute is never guessed — "no Internal flag" is not "internal".
NETWORK_ATTRIBUTE_UNKNOWN: Final = "unknown"


def _network_attribute(network: object, *keys: str) -> object:
    """Read one attribute under Docker's or Podman's spelling of its key."""
    if not isinstance(network, dict):
        raise TypeError("inspect document entry is not an object")
    for key in keys:
        if key in network:
            return network[key]
    raise LookupError(keys)


def network_attributes(inspected: str) -> tuple[str, bool | str]:
    """Pull the driver and ``Internal`` flag out of one inspect document.

    Docker capitalizes both keys; Podman's native libpod endpoint names them
    in lowercase. Podman is a supported runtime, so its own output has to read
    as evidence rather than as an unreadable daemon (#2313).
    """
    try:
        [network, *_] = json.loads(inspected)
        driver = _network_attribute(network, "Driver", "driver")
        internal = _network_attribute(network, "Internal", "internal")
    except (ValueError, LookupError, TypeError):
        return NETWORK_ATTRIBUTE_UNKNOWN, NETWORK_ATTRIBUTE_UNKNOWN
    return (
        driver if isinstance(driver, str) else NETWORK_ATTRIBUTE_UNKNOWN,
        internal if isinstance(internal, bool) else NETWORK_ATTRIBUTE_UNKNOWN,
    )


def inspect_attachment_network(
    runtime: str | None, name: str | None = ALLOWLIST_NETWORK
) -> tuple[str, bool | str]:
    """Report *name*'s driver and ``Internal`` flag as kew can observe them.

    The direct route attaches containers to a network kew never creates and
    whose destination rules it cannot read (#1945), so the label stays
    ``operator-managed-unverified``. The network's *shape* is readable, and a
    plain bridge with ``Internal=false`` has open egress, so the posture
    records that much rather than nothing (#2313).

    ``name=None`` is ``network = "open"``: no kew-named network is attached, so
    there is nothing to read. Reading some *other* network's shape there would
    be exactly the false record this evidence exists to replace.
    """
    if runtime is None or name is None:
        return NETWORK_ATTRIBUTE_UNKNOWN, NETWORK_ATTRIBUTE_UNKNOWN
    try:
        inspected = subprocess_utils.run(
            [runtime, "network", "inspect", name],
            check=False,
            timeout=_NETWORK_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # This is evidence collection on the admission path: a daemon that
        # cannot answer must leave the record honest, not fail the dispatch.
        return NETWORK_ATTRIBUTE_UNKNOWN, NETWORK_ATTRIBUTE_UNKNOWN
    if inspected.returncode != 0:
        return NETWORK_ATTRIBUTE_UNKNOWN, NETWORK_ATTRIBUTE_UNKNOWN
    return network_attributes(inspected.stdout)


def allowlist_network_exists(runtime: str, name: str = ALLOWLIST_NETWORK) -> bool:
    """Prove existence only; this check verifies no destination rules (#1945)."""
    try:
        inspected = subprocess_utils.run(
            [runtime, "network", "inspect", name],
            check=False,
            timeout=_NETWORK_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # This sits on every contained dispatch's admission path: an
        # unresponsive daemon must neither hang the fleet nor read as
        # "network absent" without saying why.
        from kew.progress import _warn

        _warn(f"Sandbox: could not inspect network {name!r}: {exception_text(exc)}")
        return False
    return inspected.returncode == 0


def allowlist_network_create_command(runtime: str) -> list[str]:
    """The one network shape kew creates: a bridge named ``kew-allowlist``.

    It is what the doctor's ``allowlist-network`` check looks for, and no
    more: kew installs no firewall rules on it, so egress stays open until
    the operator restricts it (#1945).
    """
    return [runtime, "network", "create", "--driver", "bridge", ALLOWLIST_NETWORK]


@dataclass(frozen=True)
class AllowlistNetworkResult:
    """What ``kew sandbox network`` found or did, and the network's shape."""

    #: ``created`` by this call, already ``present``, or ``absent`` (check only).
    outcome: Literal["created", "present", "absent"]
    runtime: str
    driver: str = NETWORK_ATTRIBUTE_UNKNOWN
    internal: bool | str = NETWORK_ATTRIBUTE_UNKNOWN


def _inspect_allowlist_network(runtime: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess_utils.run(
            [runtime, "network", "inspect", ALLOWLIST_NETWORK],
            check=False,
            timeout=_NETWORK_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not inspect network {ALLOWLIST_NETWORK!r}: {exception_text(exc)}"
        ) from None


def _present_network(
    runtime: str, outcome: Literal["created", "present"]
) -> AllowlistNetworkResult | None:
    inspected = _inspect_allowlist_network(runtime)
    if inspected.returncode != 0:
        return None
    driver, internal = network_attributes(inspected.stdout)
    return AllowlistNetworkResult(outcome, runtime, driver, internal)


def ensure_allowlist_network(
    config: SandboxConfig, *, create: bool = True
) -> AllowlistNetworkResult:
    """Create ``kew-allowlist`` on the configured runtime unless it exists.

    Idempotent: a present network is reported and never modified, whatever
    its shape. A create that loses a race to a concurrent creator reads the
    network back and reports it present rather than failing. Raises
    ``RuntimeError`` when the runtime is missing, cannot answer, or refuses
    the create; OpenShell owns its own egress and is refused here too.
    """
    if config.runtime == OPENSHELL_RUNTIME:
        raise RuntimeError("OpenShell owns its own egress; it attaches no network")
    runtime = _build_runtime(config)
    present = _present_network(runtime, "present")
    if present is not None:
        return present
    if not create:
        return AllowlistNetworkResult("absent", runtime)
    try:
        created = subprocess_utils.run(
            allowlist_network_create_command(runtime),
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not create network {ALLOWLIST_NETWORK!r}: {exception_text(exc)}"
        ) from None
    outcome: Literal["created", "present"] = (
        "created" if created.returncode == 0 else "present"
    )
    result = _present_network(runtime, outcome)
    if result is None:
        detail = redact_credentials((created.stderr or "").strip())
        raise RuntimeError(
            f"{shlex.join(allowlist_network_create_command(runtime))} "
            f"exited {created.returncode} and {ALLOWLIST_NETWORK!r} is still "
            f"absent{': ' + detail if detail else ''}"
        )
    return result


@dataclass(frozen=True)
class SandboxBuildResult:
    """Built tag, immutable image identity, and allowlist network status."""

    tag: str
    image_id: str
    allowlist_network_exists: bool
    pinned: bool = True
    #: The network dispatch attaches under the configuration that built this
    #: image: ``kew-allowlist``, a custom name, or ``None`` for ``"open"``.
    network_name: str | None = ALLOWLIST_NETWORK


_BUILD_OUTPUT_TAIL_CHARS = 2000


class SandboxBuildFailed(RuntimeError):
    """The build ran and failed, including its terminal image verification."""


def _require_build_daemon(runtime: str) -> None:
    """Refuse before building when the runtime cannot reach its daemon."""
    reply = subprocess_utils.run(
        [runtime, "info"],
        check=False,
        timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        env=_build_env(),
    )
    if reply.returncode != 0:
        raise RuntimeError(
            "Docker daemon unreachable: " + redact_credentials(reply.stderr or "")
        )


def _build_failure_message(image: str, built: subprocess.CompletedProcess[str]) -> str:
    """Return a bounded Docker failure with both diagnostic streams."""
    details = []
    for label, value in (("stdout", built.stdout), ("stderr", built.stderr)):
        output = redact_credentials((value or "").strip())
        if output:
            details.append(f"{label} tail:\n{output[-_BUILD_OUTPUT_TAIL_CHARS:]}")
    suffix = "\n" + "\n".join(details) if details else ""
    return f"build of {image!r} failed (rc={built.returncode}){suffix}"


def _build_runtime(config: SandboxConfig) -> str:
    resolved = detect_runtime(config)
    if resolved is None:
        raise RuntimeError(
            f"runtime {config.runtime!r} not found on PATH; install Docker/OrbStack"
        )
    subprocess_utils.TOOL_PATHS.setdefault(config.runtime, resolved)
    return config.runtime


def _effective_build_tag(
    context: Path,
    runner: str,
    requested_tag: str | None,
    *,
    unpinned: bool,
) -> tuple[str, bool]:
    expected = _expected_packaged_image_pin(context, runner)
    tag = requested_tag or expected
    pinned = _validate_packaged_image_tag(context, runner, tag, unpinned=unpinned)
    return tag, pinned


def build_sandbox_image(
    config: SandboxConfig,
    *,
    runner: str = DEFAULT_IMAGE_RUNNER,
    tag: str | None = None,
    unpinned: bool = False,
) -> SandboxBuildResult:
    """Build the effective runner image and resolve its immutable identity."""
    context = packaged_agent_image_context()
    image, pinned = _effective_build_tag(context, runner, tag, unpinned=unpinned)
    runtime = _build_runtime(config)
    _require_build_daemon(runtime)
    network_name = effective_network_name(config.network)
    network_exists = (
        allowlist_network_exists(runtime, network_name)
        if network_name is not None
        else False
    )
    if pinned:
        built = build_agent_image(
            runtime, image, runner=runner, proxy_environment=config.proxy_environment
        )
    else:
        built = build_agent_image(
            runtime,
            image,
            runner=runner,
            unpinned=True,
            proxy_environment=config.proxy_environment,
        )
    if built.returncode != 0:
        raise SandboxBuildFailed(_build_failure_message(image, built))
    resolution = inspect_image_id(runtime, image)
    if resolution.status is not ImageIdStatus.RESOLVED or not resolution.image_id:
        detail = f": {resolution.detail}" if resolution.detail else ""
        raise SandboxBuildFailed(
            f"built {image!r}, but its immutable image ID was unavailable{detail}"
        )
    return SandboxBuildResult(
        image, resolution.image_id, network_exists, pinned, network_name=network_name
    )


def _ensure_smoke_image(
    runtime: str,
    image: str,
    *,
    runner: str,
    build_if_missing: bool,
    proxy_environment: str = "none",
) -> SmokeResult | None:
    """Image layer: ``None`` when ``image`` is present locally, else the failing
    ``SmokeResult``.

    Only the current packaged pin is buildable for each of the three runners.
    Other tags may name operator-owned images, so smoke never replaces them.

    That is gated on the RUNNER **and the IMAGE REFERENCE**. Guarding on the
    runner alone was not enough: a ``claude-code`` route pointed at an
    operator's hardened image took this branch and silently rebuilt stock bytes.
    ``--pull=never`` then launched the replacement forever.
    """
    if _image_exists_locally(runtime, image):
        return None
    try:
        expected_pin = _expected_packaged_image_pin(
            packaged_agent_image_context(), runner
        )
    except (FileNotFoundError, ValueError) as exc:
        return SmokeResult(ok=False, layer="image", detail=exception_text(exc))
    if image != expected_pin:
        return SmokeResult(
            ok=False,
            layer="image",
            detail=missing_image_message(image, runner=runner, runtime=runtime),
        )
    if not build_if_missing:
        return SmokeResult(
            ok=False,
            layer="image",
            detail=f"image {image!r} absent and build disabled",
        )
    try:
        built = build_agent_image(
            runtime, image, runner=runner, proxy_environment=proxy_environment
        )
    except (FileNotFoundError, ValueError, SandboxProxyUnparseable) as exc:
        return SmokeResult(ok=False, layer="image", detail=exception_text(exc))
    if built.returncode != 0:
        return SmokeResult(
            ok=False,
            layer="image",
            detail=f"local build of {image!r} failed (rc={built.returncode})",
        )
    return None


def run_smoke(
    config: SandboxConfig,
    *,
    runner: str = DEFAULT_IMAGE_RUNNER,
    build_if_missing: bool = True,
) -> SmokeResult:
    """Launch one container end-to-end and report the first failing layer.

    Exercises the LIVE path unit tests can't: runtime resolution, image presence
    (built locally if absent), container launch through ``build_run_argv`` /
    ``launch_container``, and the prompt-file handoff. The container overrides
    the entrypoint to ``cat`` the mounted prompt file, so no API key is needed
    for those layers; a final env-only ``auth`` layer then checks that a real
    dispatch credential is present.

    ``runner`` names WHICH containment is being proven. The image comes from
    :func:`resolve_runner_image` — the same resolver dispatch uses — so the
    operator's only pre-dispatch containment proof is against the image
    dispatch will actually launch, and a non-claude runner can be smoked at all
    (#1144). Reading ``config.image`` here instead would build and prove the
    claude image while dispatch runs a mapped one, or refuses.
    """
    with proxy_redaction_scope(config.proxy_environment):
        # Layer: runtime.
        resolved = detect_runtime(config)
        if resolved is None:
            return SmokeResult(
                ok=False,
                layer="runtime",
                detail=(
                    f"runtime {config.runtime!r} not found on PATH — "
                    "install Docker/OrbStack"
                ),
            )
        runtime = config.runtime
        # `kew sandbox smoke` runs outside dispatch preflight, so the subprocess
        # allowlist is empty. Register the runtime we JUST resolved via shutil.which
        # (same mechanism preflight_tools uses for optional tools) — this does not
        # broaden the allowlist, it binds the configured runtime to its known path.
        subprocess_utils.TOOL_PATHS.setdefault(runtime, resolved)

        # Layer: image — runner-derived (never config.image directly), built
        # locally only when it is the stock image kew knows how to build.
        try:
            image = resolve_runner_image(config, runner)
        except SandboxImageUnmapped as exc:
            return SmokeResult(ok=False, layer="image", detail=exception_text(exc))
        image_failure = _ensure_smoke_image(
            runtime,
            image,
            runner=runner,
            build_if_missing=build_if_missing,
            proxy_environment=config.proxy_environment,
        )
        if image_failure is not None:
            return image_failure

        # Layers: launch + execution.
        with work_dirs.work_tempdir("kew-sandbox-smoke-") as tmp:
            checkout = Path(tmp)
            (checkout / PROMPT_FILENAME).write_text(
                f"{_SMOKE_TOKEN}\n", encoding="utf-8"
            )
            try:
                argv = build_run_argv(
                    runtime=runtime,
                    image=image,
                    checkout_dir=checkout,
                    slug="smoke",
                    network=config.network,
                    proxy_environment=config.proxy_environment,
                    runner=runner,
                    entrypoint="sh",
                    command=["-c", _SMOKE_SCRIPT],
                    caps=resource_caps(config),
                )
            # Every refusal the build can raise — an unparseable inherited
            # proxy, and a runtime with no container command line at all — is
            # the launch layer failing, which is what `kew sandbox smoke`
            # exists to report. The base class, not just the proxy subclass:
            # the smoke run is a diagnostic, so a new refusal must reach the
            # operator as a failed layer rather than a traceback.
            except SandboxRuntimeUnavailable as exc:
                return SmokeResult(ok=False, layer="launch", detail=exception_text(exc))
            try:
                handle = launch_container(argv)
                output = "".join(handle)
                exit_code = handle.wait()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return SmokeResult(
                    ok=False,
                    layer="launch",
                    detail=f"container launch failed: {exception_text(exc)}",
                )

        return _evaluate_smoke_output(exit_code, output, runner=runner)


class ContainedImageUnresolvedError(RuntimeError):
    """A contained attempt's image tag would not resolve to an immutable ID.

    Fail-closed by contract (#1550 review F1): an attempt whose containment
    boundary cannot be identified is refused before any provider charge —
    a mutable tag alone is never evidence of what ran.
    """


class AgentImageInputsMismatchError(ContainedImageUnresolvedError):
    """A ``docker-<digest>`` pin tag names an image built from other inputs.

    The tag is only a name (``docker tag`` points it anywhere); the inputs
    label is what kew wrote into the image when it built it. A pinned tag
    whose label is absent or names other inputs is refused before a container
    is created (#3160). This catches a STALE image — one built before the
    label existed, from other inputs, or retagged by mistake. It is not a
    defence against tampering: anyone who can run ``docker`` can build an
    image with any label.
    """


class ImageIdStatus(StrEnum):
    """Why an image reference did or did not resolve to an immutable ID.

    ``ABSENT`` and ``UNAVAILABLE`` are kept apart for the same reason
    :class:`OOMProbeStatus` splits ``absent`` from ``unavailable``: "the image
    is not here" and "I could not tell" have different remedies, and reporting
    the second as the first hands the operator a build command for a problem a
    build will not fix.
    """

    RESOLVED = "resolved"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ImageResolution:
    """Outcome of resolving one image reference to its immutable ID."""

    status: ImageIdStatus
    image_id: str | None = None
    #: Free text explaining an ``UNAVAILABLE``; empty otherwise.
    detail: str = ""
    #: The image's :data:`AGENT_IMAGE_INPUTS_LABEL`, read from the same inspect
    #: payload as the ID, or ``None`` when it carries none.
    inputs_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", redact_credentials(self.detail))


#: A full content-addressed image ID. Docker and OrbStack report
#: ``sha256:<64 hex>``; Podman reports the BARE 64 hex digits for the same
#: image. Both are accepted and normalised to the prefixed form, because a
#: refusal here blocks the dispatch entirely — rejecting Podman's spelling
#: would be a false refusal, which is as bad as the false pass this replaced.
#:
#: What is NOT accepted is anything that is not a full digest: a tag, a prefix,
#: uppercase or non-hex text. Those are not evidence of what ran.
_FULL_IMAGE_ID = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


#: What a runtime says when the reference simply is not in its local store.
#: Docker says "No such image"; Podman says "image not known" on current
#: releases and "no such object" on older ones (round-1 review, Minor 7).
#:
#: This is a substring match on an error string, which is the weakest part of
#: the classification: a runtime kew has not seen, or a non-English locale,
#: falls through to UNAVAILABLE. That direction is the safe one — it withholds
#: the build instruction rather than giving a wrong one — but it is a judgement,
#: not a proof, so any new phrasing belongs here rather than in a caller.
_ABSENT_IMAGE_PHRASES = (
    "no such image",
    "image not known",
    "no such object",
    "manifest unknown",
)


def _classify_inspect_failure(
    runtime: str, result: subprocess.CompletedProcess[str]
) -> ImageResolution:
    """Absent, or unreadable? The two have different remedies."""
    stderr = (result.stderr or "").lower()
    if any(phrase in stderr for phrase in _ABSENT_IMAGE_PHRASES):
        return ImageResolution(ImageIdStatus.ABSENT)
    return ImageResolution(
        ImageIdStatus.UNAVAILABLE,
        detail=redact_credentials(result.stderr or "").strip()[:200]
        or f"{runtime} image inspect exited {result.returncode} with no output",
    )


def _read_inspected_id(stdout: str) -> ImageResolution:
    """The ``Id`` out of an ``image inspect`` payload, or why it was not there."""
    try:
        data = json.loads(stdout)
        image_id = data[0].get("Id", "") if data else ""
        labels = (data[0].get("Config") or {}).get("Labels") if data else None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        return ImageResolution(
            ImageIdStatus.UNAVAILABLE, detail="malformed image inspect output"
        )
    if not isinstance(image_id, str) or not image_id.strip():
        return ImageResolution(
            ImageIdStatus.UNAVAILABLE, detail="image inspect returned no Id"
        )
    # An "immutable ID" that is a tag, a prefix, or a non-hex string is not one.
    # Every non-empty string used to be accepted here, so a runtime returning
    # `kew-agent:latest` or `sha256:a` had that value recorded on the route
    # profile and launched as though it were content-addressed. The same
    # validation is applied to `docker build --iidfile` in
    # scripts/scan-agent-image.sh; it belongs on both sides of the boundary.
    match = _FULL_IMAGE_ID.fullmatch(image_id)
    if match is None:
        return ImageResolution(
            ImageIdStatus.UNAVAILABLE,
            detail=(
                f"image inspect returned {redact_credentials(image_id)[:80]!r}, "
                "which is not a full "
                "content-addressed image ID — refusing to record a mutable or "
                "partial reference as immutable evidence"
            ),
        )
    # One spelling downstream, whichever runtime answered: the run row, the
    # route-profile fingerprint and the launch argv must all name the same
    # bytes the same way, or the same image compares unequal across runtimes.
    label = labels.get(AGENT_IMAGE_INPUTS_LABEL) if isinstance(labels, dict) else None
    return ImageResolution(
        ImageIdStatus.RESOLVED,
        image_id=f"sha256:{match.group(1)}",
        inputs_digest=label if isinstance(label, str) else None,
    )


def inspect_image_id(runtime: str, image: str) -> ImageResolution:
    """Resolve ``image`` to its immutable ID, saying WHY when it does not.

    A tag is a pointer: ``kew-agent:latest`` names different bytes before and
    after a rebuild, so retaining it as a run's containment boundary retains
    nothing. This reads the content-addressed ``Id`` the runtime resolved the
    tag to, which is what the attempt actually ran. Never pulls — ``image
    inspect`` is local-only, matching the ``--pull=never`` launch.
    """
    # ``detect_runtime`` returns a which()-resolved ABSOLUTE path, which is not
    # a TOOL_PATHS key (keys are bare names registered by preflight) — so this
    # call refused for the entire life of the feature and every caller
    # silently recorded an unresolved boundary. An absolute path that came
    # from shutil.which is already the resolution the allowlist exists to
    # provide, so register it under itself rather than depending on preflight
    # ordering in whichever process this runs in (#1513 incident).
    if os.path.isabs(runtime):
        subprocess_utils.TOOL_PATHS.setdefault(runtime, runtime)
    try:
        result = subprocess_utils.run(
            [runtime, "image", "inspect", image],
            check=False,
            timeout=10,
            env={
                name: value
                for name, value in os.environ.items()
                if name != "OPENROUTER_API_KEY"
            },
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # ValueError: tool not allowlisted; OSError: exec failure;
        # TimeoutExpired: a wedged daemon must never hang an admission.
        return ImageResolution(
            ImageIdStatus.UNAVAILABLE,
            detail=f"{type(exc).__name__}: {exception_text(exc)}",
        )
    if result.returncode != 0 or not result.stdout.strip():
        return _classify_inspect_failure(runtime, result)
    return _read_inspected_id(result.stdout)


def resolve_image_id(runtime: str, image: str) -> str | None:
    """The immutable image ID behind a (mutable) image reference, or ``None``.

    Thin wrapper over :func:`inspect_image_id` for callers that only need the
    id-or-nothing answer. Callers that REFUSE on ``None`` should use
    :func:`require_image_id` instead, so the refusal can name which of the two
    causes it was.
    """
    return inspect_image_id(runtime, image).image_id


def require_image_id(
    runtime: str | None, image: str, *, runner: str, refusal: str
) -> str:
    """Resolve ``image`` to its immutable ID, or raise with the right remedy.

    The single seam behind every :class:`ContainedImageUnresolvedError`. Before
    this existed each raise site wrote its own message, and all of them said
    "check the container runtime and the [sandbox.images] pin" — which is the
    wrong instruction for by far the most common cause, an image that has
    simply never been built on this machine. Three causes, three remedies:

    * no container runtime — install one, or turn the sandbox off;
    * runtime present, image not in its store — build it (the command is in
      the message);
    * runtime present but it could not answer — a daemon problem, and the
      detail it gave is quoted rather than guessed at.

    ``refusal`` is the caller's own clause naming what is being refused, so a
    dispatch, a launch and a readiness probe still read as themselves.
    """
    if runtime is None:
        raise ContainedImageUnresolvedError(
            f"cannot resolve image {image!r} to an immutable image ID: no "
            f"container runtime is available — {refusal}. Install "
            "Docker/OrbStack, or set [sandbox] enabled = false with "
            "dangerously_skip_containment = true to run uncontained."
        )
    outcome = inspect_image_id(runtime, image)
    if outcome.status is ImageIdStatus.RESOLVED and outcome.image_id:
        require_pinned_inputs_label(
            image, outcome.inputs_digest, runner=runner, refusal=refusal
        )
        return outcome.image_id
    if outcome.status is ImageIdStatus.ABSENT:
        raise ContainedImageUnresolvedError(
            f"{missing_image_message(image, runner=runner, runtime=runtime)} "
            f"Until then, {refusal}."
        )
    detail = f" ({outcome.detail})" if outcome.detail else ""
    raise ContainedImageUnresolvedError(
        f"cannot resolve image {image!r} to an immutable image ID: the "
        f"container runtime {runtime!r} did not answer{detail} — {refusal}. "
        "This is a runtime problem, not a missing image: the image may well "
        "be there."
    )


_FULL_INPUTS_DIGEST = re.compile(r"[0-9a-f]{64}")


def require_pinned_inputs_label(
    image: str, inputs_digest: str | None, *, runner: str, refusal: str
) -> None:
    """Refuse a kew pin tag whose image was not built from the inputs it names.

    ``kew-agent:docker-<12 hex>`` says the image was built from inputs whose
    digest starts with those 12 hex; *inputs_digest* is the image's own
    :data:`AGENT_IMAGE_INPUTS_LABEL`. A label that is absent, malformed or
    names other inputs means the tag points at an image kew did not build
    from those inputs — an old build retagged, or anything else — so the
    launch is refused with the rebuild command. Any image that is not a kew
    pin tag (an operator's own image, an ID) is left alone.
    """
    problem = pinned_inputs_label_problem(image, inputs_digest)
    if problem is None:
        return
    current = _packaged_pin_hint(runner)
    rebuild = (
        f"Rebuild it from kew's packaged inputs: kew sandbox build --runner {runner}."
    )
    if image != current:
        # Building the current inputs under this tag would only make the next
        # mismatch: the remedy is the pin this install produces.
        rebuild = (
            f"This install's inputs build {current}: point "
            f"{_image_config_field(runner)} at it and run kew sandbox build "
            f"--runner {runner}."
        )
    raise AgentImageInputsMismatchError(
        f"image {image!r} {problem} — {refusal}. {rebuild}"
    )


def pinned_inputs_label_problem(image: str, inputs_digest: str | None) -> str | None:
    """Why *image*'s inputs label does not match its pin tag, or ``None``.

    ``None`` when *image* is not a kew pin tag, or when *inputs_digest* is a
    full digest starting with the tag's 12 hex. Otherwise the reason, worded
    to follow the image's name. The one rule a local launch
    (:func:`require_pinned_inputs_label`) and a placed one
    (:func:`kew.hosts.remote_dispatch.ensure_agent_image`) both apply.
    """
    match = AGENT_IMAGE_PIN_TAG_RE.fullmatch(image)
    if match is None:
        return None
    pinned = match.group(2)
    if (
        inputs_digest is not None
        and _FULL_INPUTS_DIGEST.fullmatch(inputs_digest)
        and inputs_digest.startswith(pinned)
    ):
        return None
    found = (
        f"carries no {AGENT_IMAGE_INPUTS_LABEL} label"
        if not inputs_digest
        else f"has {AGENT_IMAGE_INPUTS_LABEL}="
        f"{redact_credentials(inputs_digest)[:80]!r}"
    )
    return f"{found}, so it was not built from the inputs its docker-{pinned} tag names"


def _parse_docker_created(raw: str) -> datetime | None:
    """Parse a Docker RFC3339 Created timestamp to a UTC-aware datetime.

    Docker returns nanosecond precision (e.g. ``2024-01-15T10:30:00.123456789Z``).
    ``datetime.fromisoformat`` handles microseconds; excess digits are truncated.
    """
    ts = re.sub(r"(\.\d{6})\d+", r"\1", raw.strip())
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    # Normalize to UTC so a non-UTC offset renders correctly with a ``Z`` suffix.
    return dt.astimezone(UTC)


def _get_image_created_ts(runtime: str, image: str) -> datetime | None:
    """Return the ``Created`` timestamp for a local image, or ``None`` on failure."""
    try:
        result = subprocess_utils.run(
            [runtime, "image", "inspect", image],
            check=False,
            timeout=10,
            env={
                name: value
                for name, value in os.environ.items()
                if name != "OPENROUTER_API_KEY"
            },
        )
    except (ValueError, OSError, subprocess.TimeoutExpired):
        # ValueError: tool not allowlisted; OSError: exec failure;
        # TimeoutExpired: a wedged daemon must never hang a dispatch preflight.
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
        raw = data[0].get("Created", "") if data else ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        # A non-array top-level object (``data[0]`` → KeyError/TypeError) or any
        # other malformed inspect output is swallowed — never blocks a dispatch.
        return None
    return _parse_docker_created(raw)


# Packaged build-input manifest. The values record each executable bit.
# Package validation requires this exact file set before Docker receives it.
_PACKAGED_AGENT_IMAGE_MANIFEST = {
    # Every file COPY'd into ANY agent image. A commit touching any of them
    # changes what an image contains, and a staleness check blind to one calls
    # a stale image fresh — which is the whole failure this exists to prevent.
    #
    # The list was long incomplete: it named the claude Dockerfile and its
    # entrypoint only, so a change to `extract-artifact.sh` — which BOTH images
    # copy, and whose last change is exactly why the images were rebuilt in
    # 2026-08 — marked neither image stale. Nor did anything in the opencode
    # image (#1793 review).
    #
    # Deliberately the UNION across every image rather than a per-image list.
    # An image TAG carries no record of which Dockerfile produced it, so there
    # is nothing to key a per-image lookup on; and this check only ever warns,
    # so over-warning on an unrelated input is the safe direction. The images
    # are pinned and rebuilt together in kew.toml anyway.
    "docker": None,
    "docker/agent.Dockerfile": False,
    "docker/agent-entrypoint.sh": True,
    "docker/agent-opencode.Dockerfile": False,
    "docker/agent-opencode-entrypoint.sh": False,
    # The Codex image is OpenShell-only and so has no entrypoint of its own:
    # `openshell sandbox exec` replaces one, and the docker lane for codex does
    # not exist (CodexRunner declares native containment and no in-container
    # credential). docker/agent-codex-entrypoint.sh was deleted with that
    # image's arrival rather than carried as an unreachable reference.
    "docker/agent-codex.Dockerfile": False,
    # The OpenShell containment runtime replaces the image ENTRYPOINT, so this
    # launcher does the pre-runner work the entrypoints do and EVERY image
    # installs it. Its policies are host-side inputs to `openshell sandbox
    # exec`, never COPY'd into a layer, but they live under docker/ and so are
    # covered by the digest the pins name — re-pinning on a policy change is
    # the point: what a dispatch is allowed to reach is part of the image's
    # identity to kew.
    "docker/kew-openshell-launch": True,
    "docker/openshell": None,
    "docker/openshell/policy-claude-code.yaml": False,
    "docker/openshell/policy-codex.yaml": False,
    "docker/openshell/policy-opencode.yaml": False,
    # The adversarial reviewer and its environment provisioning are separate
    # boundaries from a dispatch on the same image (epic node N12): the
    # reviewer executes the reviewed PR's tests, so its egress is certified on
    # its own digest and the provisioning install runs under a package-index
    # policy in a sandbox of its own.
    "docker/openshell/policy-review-claude-code.yaml": False,
    "docker/openshell/policy-review-provision-claude-code.yaml": False,
    "docker/opencode.json": False,
    "docker/extract-artifact.sh": False,
    "docker/switchyard.Dockerfile": False,
    "docker/verify-npm-overlay.js": False,
}

# These paths also drive the git-based staleness check.
_AGENT_IMAGE_BUILD_INPUTS = tuple(
    path
    for path, executable in _PACKAGED_AGENT_IMAGE_MANIFEST.items()
    if executable is not None
)


AGENT_IMAGE_PIN_TAG_RE = re.compile(
    r"^(kew-agent|kew-agent-codex|kew-agent-opencode):docker-([0-9a-f]{12})$"
)


def _packaged_image_manifest_entry(
    repo_root: Path, path: Path
) -> tuple[str, bool | None]:
    relative = unicodedata.normalize("NFC", path.relative_to(repo_root).as_posix())
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError(f"symbolic link in packaged image context: {relative}")
    if stat.S_ISREG(mode):
        return relative, bool(mode & 0o111)
    if stat.S_ISDIR(mode):
        return relative, None
    raise ValueError(f"unsupported entry type in packaged image context: {relative}")


def _validate_packaged_agent_image_manifest(repo_root: Path) -> None:
    """Require the packaged context to match its file and mode manifest."""
    entries = [
        _packaged_image_manifest_entry(repo_root, path) for path in repo_root.rglob("*")
    ]
    actual = dict(entries)
    if len(actual) != len(entries):
        raise ValueError("duplicate normalized path in packaged image manifest")

    expected_paths = set(_PACKAGED_AGENT_IMAGE_MANIFEST)
    missing = sorted(expected_paths - actual.keys())
    unexpected = sorted(actual.keys() - expected_paths)
    if missing or unexpected:
        raise ValueError(
            "packaged agent image manifest mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )

    mode_mismatches = sorted(
        relative
        for relative, executable in actual.items()
        if executable != _PACKAGED_AGENT_IMAGE_MANIFEST[relative]
    )
    if mode_mismatches:
        raise ValueError(
            "packaged agent image entry kind or executable bit mismatch: "
            f"{mode_mismatches}"
        )


#: A hung ``git log`` (a lock, a slow filesystem) reads as unknown history.
_GIT_HISTORY_TIMEOUT_SECONDS = 10


def _get_dockerfile_ts(root: Path) -> datetime | None:
    """Return the last committer timestamp touching any agent-image build input."""
    try:
        result = subprocess_utils.run(
            ["git", "log", "-1", "--format=%cI", "--", *_AGENT_IMAGE_BUILD_INPUTS],
            check=False,
            cwd=root,
            timeout=_GIT_HISTORY_TIMEOUT_SECONDS,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        ts = datetime.fromisoformat(result.stdout.strip())
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    # Normalize to UTC so a non-UTC git offset renders correctly with ``Z``.
    return ts.astimezone(UTC)


@dataclass(frozen=True)
class ImageStalenessResult:
    """Outcome of an agent-image staleness check.

    ``status`` is one of ``"fresh"``, ``"stale"``, or ``"unknown"``. ``unknown``
    means the check could not run (image missing, docker/git unavailable, no
    source checkout, or malformed inspect output) and must NOT be reported as a
    passing check. ``warning`` carries the rebuild advisory only when stale;
    ``detail`` explains an ``unknown`` outcome.
    """

    status: str
    warning: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.detail is not None:
            object.__setattr__(self, "detail", redact_credentials(self.detail))


def _stale_image_remediation(runtime: str, image: str, runner: str) -> str:
    """Return a rebuild command only when the image tag proves its source."""
    known_builds = {
        DEFAULT_IMAGE_RUNNER: ("kew-agent", "docker/agent.Dockerfile"),
        "codex": ("kew-agent-codex", "docker/agent-codex.Dockerfile"),
        "opencode": (
            "kew-agent-opencode",
            "docker/agent-opencode.Dockerfile",
        ),
    }
    known_build = known_builds.get(runner)
    if known_build is not None:
        image_repository, dockerfile = known_build
        pin_match = AGENT_IMAGE_PIN_TAG_RE.fullmatch(image)
        is_runner_pin = pin_match is not None and pin_match.group(1) == image_repository
        if image == f"{image_repository}:latest" or is_runner_pin:
            command = Path(runtime).name
            return f"Rebuild: {command} build -f {dockerfile} -t {image} ."
    return (
        "The image is stale, but kew cannot rebuild it because its tag does not "
        "identify a kew-owned image. Build or pull it from its trusted source."
    )


def evaluate_agent_image_staleness(
    runtime: str, image: str, *, runner: str = DEFAULT_IMAGE_RUNNER
) -> ImageStalenessResult:
    """Compare the local image's created-at timestamp against the last git commit
    touching any agent-image build input (Dockerfile + copied entrypoint).

    Distinguishes ``fresh``, ``stale``, and ``unknown`` so callers can tell a
    passing check from one that could not run. Best-effort: any failure yields
    ``unknown`` (never raises) so a missing docker daemon or git repo never
    blocks a dispatch.
    """
    image_created = _get_image_created_ts(runtime, image)
    if image_created is None:
        return ImageStalenessResult(
            "unknown",
            detail="could not read the local image timestamp "
            "(image missing, or docker unavailable)",
        )
    root = _repo_root()
    if root is None:
        return ImageStalenessResult(
            "unknown", detail="not a kew source checkout (no repository root)"
        )
    dockerfile_ts = _get_dockerfile_ts(root)
    if dockerfile_ts is None:
        return ImageStalenessResult(
            "unknown", detail="could not read the Dockerfile git history"
        )
    if dockerfile_ts <= image_created:
        return ImageStalenessResult("fresh")
    warning = (
        f"Agent image {image!r} predates its build inputs "
        f"({', '.join(_AGENT_IMAGE_BUILD_INPUTS)}) "
        f"(image built {image_created.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
        f"inputs last changed {dockerfile_ts.strftime('%Y-%m-%dT%H:%M:%SZ')}). "
        f"{_stale_image_remediation(runtime, image, runner)}"
    )
    return ImageStalenessResult("stale", warning=warning)


def check_agent_image_staleness(
    runtime: str, image: str, *, runner: str = DEFAULT_IMAGE_RUNNER
) -> str | None:
    """Return a one-line rebuild warning when the agent image is stale, else ``None``.

    Thin wrapper over :func:`evaluate_agent_image_staleness` for the dispatch
    preflight, which only needs the warn-or-not signal (never blocks).
    """
    return evaluate_agent_image_staleness(runtime, image, runner=runner).warning


# Smoke's auth layer reads the environment only; kew mints a direct OpenRouter
# route's run key at dispatch, after this check (#1985).
_SMOKE_MINTED_KEY_NOTE = (
    " With OPENROUTER_PROVISIONING_KEY set, a direct OpenRouter route receives "
    "its OPENROUTER_API_KEY at dispatch, which this environment-only check "
    "cannot see."
)


def _smoke_auth_layer(runner: str) -> SmokeResult | None:
    """Auth layer: ``None`` when a REAL dispatch of ``runner`` could
    authenticate in-container, else the failing ``SmokeResult``.

    Keyed on the runner's own declared vars rather than claude's, so the smoke
    answers the question dispatch will ask of the same runner (#1144), with the
    same provider-specific guidance the dispatch refusal gives (#1146). A runner
    that declares no credential is an auth failure here for the same reason it
    is a dispatch refusal: nothing it can authenticate with is forwarded.
    """
    declared = runner_credential_env_vars(runner)
    if not declared:
        return SmokeResult(
            ok=False,
            layer="auth",
            detail=(
                f"container plumbing OK, but runner {runner!r} declares no "
                "in-container credential — a real dispatch is refused rather "
                "than launched with nothing to authenticate with"
            ),
        )
    if not _any_env_set(declared):
        return SmokeResult(
            ok=False,
            layer="auth",
            detail=(
                f"container plumbing OK, but no {runner} credential in the "
                "environment — a real dispatch would authenticate as nothing "
                f"($0 failure). {credential_missing_message(runner, declared)}."
                + (_SMOKE_MINTED_KEY_NOTE if "OPENROUTER_API_KEY" in declared else "")
            ),
        )
    return None


def _evaluate_smoke_output(
    exit_code: int, output: str, *, runner: str = DEFAULT_IMAGE_RUNNER
) -> SmokeResult:
    """Judge the execution + auth layers from the smoke container's output."""
    if exit_code != 0:
        detail = redact_credentials(output).strip()[:200]
        return SmokeResult(
            ok=False,
            layer="execution",
            detail=f"container exited {exit_code}: {detail}",
        )
    if _SMOKE_TOKEN not in output:
        return SmokeResult(
            ok=False,
            layer="execution",
            detail="prompt file not echoed from /work — mount/handoff broken",
        )
    if _SMOKE_HOME_TOKEN not in output:
        return SmokeResult(
            ok=False,
            layer="execution",
            detail=(
                "agent HOME not writable (EROFS?) — Claude Code's Bash tool "
                "and git would fail in-container (#742)"
            ),
        )
    auth_failure = _smoke_auth_layer(runner)
    if auth_failure is not None:
        return auth_failure
    return SmokeResult(
        ok=True,
        layer="ok",
        detail=(
            "runtime, image, launch, prompt-file handoff, writable HOME, "
            "and auth all OK"
        ),
    )
