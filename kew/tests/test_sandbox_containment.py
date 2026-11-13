"""Bypass-containment regression suite (Node S7).

Two tests:
- test_bypass_payloads_cannot_escape_mount: skip-marked _REQUIRES_DOCKER; proves each
  #502 bypass payload cannot modify a host marker file outside the container mount.
- test_containment_argv_excludes_sensitive_paths: always runs (no daemon); locks the
  argv/env invariant — no sensitive host paths or credentials in the constructed argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from kew.config import DispatchConfig, SandboxConfig
from kew.orchestrator import _is_timeout_exit, run_agent
from kew.runners.claude_code import ClaudeCodeRunner
from kew.sandbox import (
    AGENT_MARKER_ENV,
    TEST_WORKER_ENV_NAMES,
    ContainedExecution,
    build_run_argv,
    open_egress_warning,
    run_container_with_cpu_cap_retry,
)
from kew.subprocess_utils import find_timeout_cmd

_REQUIRES_DOCKER = pytest.mark.skipif(
    not shutil.which("docker"), reason="docker not available"
)
# Opted in without docker names docker, so KEW_REQUIRE_TOOLS=1 can tell an
# opt-in left off (intended) from a missing tool (tests/support/required_tools.py).
_LIVE_DOCKER_OPTED_IN = os.environ.get("KEW_RUN_LIVE_DOCKER") == "1"
_REQUIRES_LIVE_DOCKER = pytest.mark.skipif(
    not _LIVE_DOCKER_OPTED_IN or not shutil.which("docker"),
    reason=(
        "docker not available"
        if _LIVE_DOCKER_OPTED_IN
        else "live Docker containment tests require KEW_RUN_LIVE_DOCKER=1"
    ),
)


@_REQUIRES_DOCKER
def test_bypass_payloads_cannot_escape_mount(tmp_path: Path) -> None:
    """Each #502 bypass payload is contained by the mount boundary.

    A host marker file is created *outside* the container's /work mount.
    The container runs each payload that attempts to delete or overwrite it.
    The host marker must be untouched after every run.
    """
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    host_marker = tmp_path / f"kew-containment-{uuid.uuid4().hex}.txt"
    host_marker.write_text("intact")
    marker_path = str(host_marker)

    # #502 bypass payloads, adapted to target the host marker by absolute path.
    # Inside the container only /work is mounted; the marker's path does not exist.
    payloads = [
        # Background-process escape: fork a subshell to rm the host marker
        f"true & rm -f {marker_path}",
        # Glob-based write: expand * hoping to match the host marker path
        f"printf 'pwned' > {tmp_path}/*.txt 2>/dev/null || true",
        # Variable-concatenation write: reassemble the path from fragments
        (
            f'd="{tmp_path}/kew-containment-";'
            f'e="{host_marker.name}";'
            f'echo pwned > "$d$e" 2>/dev/null || true'
        ),
        # Direct echo to absolute marker path (baseline containment check)
        f"echo pwned > {marker_path} 2>/dev/null || true",
    ]

    docker = shutil.which("docker")
    assert docker is not None
    # The production launch path resolves docker through the subprocess
    # allowlist; register the tools exactly as a dispatch does.
    from kew.subprocess_utils import preflight_tools, run

    preflight_tools()
    # build_run_argv launches with --pull=never, so the probe image must be in
    # the local store. Provision it here rather than trusting the runner: the
    # same module runs in the hosted containment job, the self-hosted test
    # job, and verify.sh. A daemon that cannot provide the image fails loudly.
    present = run(["docker", "image", "inspect", "alpine:latest"], check=False)
    if present.returncode != 0:
        pulled = run(["docker", "pull", "alpine:latest"], check=False, timeout=300)
        assert pulled.returncode == 0, (
            "containment probe image alpine:latest is absent and could not be "
            f"pulled: exit {pulled.returncode}, stderr {pulled.stderr[-300:]!r}"
        )

    for index, payload in enumerate(payloads):
        # One argv per payload: build_run_argv binds a --cidfile, and reusing
        # it makes docker refuse every run after the first (exit 125), which
        # the old test never noticed because it ignored the result.
        argv_base = build_run_argv(
            # The allowlist matches executables by name; a resolved path is
            # rejected at launch on purpose (see _build_sandbox_argv).
            runtime="docker",
            image="alpine:latest",
            checkout_dir=checkout_dir,
            slug=f"9997-{index}",
            network="open",
            runner="claude-code",
        )
        # The probe must prove it executed inside a container. build_run_argv
        # launches with --pull=never, so a runner whose image store lacks
        # alpine:latest fails before any container starts; an untouched host
        # marker would then pass this test while proving nothing.
        full_argv = [*argv_base, "sh", "-c", f"{payload}; echo KEW_PROBE_RAN"]
        # The production launch path: a daemon that advertises fewer CPUs than
        # the default cap (hosted runners have two) refuses the argv before
        # creating a container, and production retries once at one CPU.
        result = run_container_with_cpu_cap_retry(full_argv, timeout=60)
        assert result.returncode == 0 and "KEW_PROBE_RAN" in result.stdout, (
            "containment probe did not run inside a container (provision "
            f"alpine:latest before this test): exit {result.returncode}, "
            f"stderr {result.stderr[-300:]!r}"
        )

        assert host_marker.exists(), (
            f"Bypass payload deleted host marker (mount boundary failed): {payload!r}"
        )
        assert host_marker.read_text() == "intact", (
            f"Bypass payload modified host marker (mount boundary failed): {payload!r}"
        )


@_REQUIRES_LIVE_DOCKER
def test_ignored_sigterm_container_is_gone_after_hard_timeout(
    tmp_path: Path,
) -> None:
    """A TERM-ignoring agent is killed and removed within the hard grace."""
    docker = shutil.which("docker")
    assert docker is not None
    # The production path resolves `docker` and `timeout` through the
    # executable allowlist; fill it the way a real dispatch does (the conftest
    # fixture restores it after the test, #1631).
    from kew.subprocess_utils import preflight_tools

    preflight_tools()
    slug = f"term-{uuid.uuid4().hex[:12]}"
    name = f"kew-{slug}"
    argv = build_run_argv(
        runtime=docker,
        image="alpine:latest",
        checkout_dir=tmp_path,
        slug=slug,
        network="open",
        runner="claude-code",
        entrypoint="sh",
        command=["-c", 'trap "" TERM; sleep 3600'],
    )
    if find_timeout_cmd() is None:
        pytest.skip("timeout wrapper not available")

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    started = time.monotonic()
    try:
        result = run_agent(
            runner=ClaudeCodeRunner(),
            prompt="live timeout containment probe",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1431,
            timeout_override_minutes=5 / 60,  # type: ignore[arg-type]
            contained=ContainedExecution.from_argv(argv),
        )
        elapsed = time.monotonic() - started
        inspect = subprocess.run(  # nosec B603 - fixed runtime operation
            [docker, "inspect", name], capture_output=True, text=True, timeout=5
        )
        assert elapsed < 40
        assert _is_timeout_exit(result.exit_code)
        # `--rm` removes the container after its process dies; the wrapper
        # returns as soon as the docker CLI exits, so give the daemon a moment
        # to finish the teardown before declaring the kill missed.
        deadline = time.monotonic() + 20
        while inspect.returncode == 0 and time.monotonic() < deadline:
            time.sleep(1)
            inspect = subprocess.run(  # nosec B603 - fixed runtime operation
                [docker, "inspect", name], capture_output=True, text=True, timeout=5
            )
        assert inspect.returncode != 0, "timed-out container still exists after 20 s"
    finally:
        subprocess.run(  # nosec B603 - best-effort live-test cleanup
            [docker, "rm", "-f", name], capture_output=True, text=True, timeout=5
        )


@_REQUIRES_LIVE_DOCKER
def test_a_cgroup_oom_is_named_as_an_oom_not_as_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container the kernel OOM-kills reports the memory cap, not a timeout.

    The live half of #1767. A cgroup OOM exits 137 exactly like the watchdog's
    kill and the wrapper's ``-k``, and ``--rm`` removes the container before
    ``docker inspect`` could read ``State.OOMKilled`` — so this can only be
    proven against a real daemon, whose event stream outlives the container.

    ``[sandbox] max_memory`` is lower-only and floors at 1 GiB, so a cap this
    small can only come from the module constant: 32 MiB written into the
    container's ``/tmp`` tmpfs (host RAM, charged to the memory cgroup) under
    a 16 MiB cap.
    """
    docker = shutil.which("docker")
    assert docker is not None
    from kew import sandbox
    from kew.subprocess_utils import preflight_tools

    preflight_tools()
    # --memory-swap follows --memory structurally (ResourceCaps.memory_swap),
    # so lowering the one constant lowers both: with a swap allowance the
    # writer would thrash instead of being OOM-killed and prove nothing.
    monkeypatch.setattr(sandbox, "_MEMORY_LIMIT", "16m")
    slug = f"oom-{uuid.uuid4().hex[:12]}"
    name = f"kew-{slug}"
    argv = build_run_argv(
        runtime=docker,
        image="alpine:latest",
        checkout_dir=tmp_path,
        slug=slug,
        network="open",
        runner="claude-code",
        entrypoint="sh",
        command=["-c", "exec dd if=/dev/zero of=/tmp/blob bs=1M count=32"],
    )
    assert "16m" in argv, "the test must launch under its own lowered cap"

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    try:
        result = run_agent(
            runner=ClaudeCodeRunner(),
            prompt="live cgroup OOM probe",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1767,
            timeout_override_minutes=2,
            contained=ContainedExecution.from_argv(argv),
        )
        assert result.exit_code == 137
        assert "OOM-killed" in (result.resource_cap_reason or ""), (
            f"a cgroup OOM was not named: {result.resource_cap_reason!r}"
        )
        assert "memory cap (16m)" in result.resource_cap_reason
        # Named as the cap it is, so the breaker event and the retry ladder
        # both read it as resource_cap rather than as a wall-clock timeout.
        assert result.hard_stop == "resource_cap"
    finally:
        subprocess.run(  # nosec B603 - best-effort live-test cleanup
            [docker, "rm", "-f", name], capture_output=True, text=True, timeout=5
        )


def test_containment_argv_excludes_sensitive_paths(tmp_path: Path) -> None:
    """Regression lock: the sandbox argv/env must not expose sensitive host paths.

    No daemon required — this test exercises build_run_argv directly and asserts
    the constructed invocation omits ~/.claude, dispatch.db, repo .git, and any
    GitHub token environment variable.
    """
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    cfg = SandboxConfig()
    argv = build_run_argv(
        runtime=cfg.runtime,
        image=cfg.image,
        checkout_dir=checkout_dir,
        slug="42",
        network=cfg.network,
        runner="claude-code",
    )

    argv_flat = " ".join(str(a) for a in argv)

    # No sensitive host paths in any argument
    assert ".claude" not in argv_flat, "argv exposes ~/.claude"
    assert "dispatch.db" not in argv_flat, "argv exposes dispatch.db"
    assert "GH_TOKEN" not in argv_flat, "argv exposes GH_TOKEN"
    assert "GITHUB_TOKEN" not in argv_flat, "argv exposes GITHUB_TOKEN"

    # The only volume mount must be checkout_dir -> /work
    v_indices = [i for i, a in enumerate(argv) if a == "-v"]
    assert len(v_indices) == 1, f"Expected exactly one -v mount, got {len(v_indices)}"
    assert argv[v_indices[0] + 1] == f"{checkout_dir}:/work"

    # ANTHROPIC_API_KEY passed by name (value inherited from env), nothing else secret
    e_indices = [i for i, a in enumerate(argv) if a == "-e"]
    e_values = [argv[i + 1] for i in e_indices]
    assert any("ANTHROPIC_API_KEY" in v for v in e_values), (
        "argv must pass ANTHROPIC_API_KEY to the container"
    )
    for v in e_values:
        assert "GH_TOKEN" not in v, f"argv passes GH_TOKEN via -e: {v!r}"
        assert "GITHUB_TOKEN" not in v, f"argv passes GITHUB_TOKEN via -e: {v!r}"

    # .git must not appear in any non-mount argument (the checkout has its own .git
    # which is acceptable as part of the /work path, but the host repo .git must not
    # be a separate mount or direct path argument)
    non_mount_args = [
        a
        for i, a in enumerate(argv)
        if a not in ("-v", "--mount")
        and (i == 0 or argv[i - 1] not in ("-v", "--mount"))
    ]
    for a in non_mount_args:
        assert "/.git" not in str(a), f"argv argument exposes host .git path: {a!r}"


def test_sandbox_network_default_is_not_open() -> None:
    """Egress default must be restricted, never full-open (#935 Task 8).

    The sandbox forwards ANTHROPIC_API_KEY into the container; shipping
    ``network="open"`` by default would let one outbound request exfiltrate the
    key and contradicts docs/threat-model.md's egress-restriction posture. Lock
    the default to a restricted value so a regression can't silently reopen it.
    """
    assert SandboxConfig().network != "open"
    assert SandboxConfig().network == "allowlist"


def test_open_egress_with_credential_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator opting into open egress with a key present gets a loud warning."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abc")
    warning = open_egress_warning("open", runner="claude-code")
    assert warning is not None
    assert "exfiltrate" in warning
    # Restricted egress (the default) never warns, even with a credential set.
    assert open_egress_warning("allowlist", runner="claude-code") is None


def test_open_egress_without_credential_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential in the environment → nothing to exfiltrate → no warning."""
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert open_egress_warning("open", runner="claude-code") is None


@pytest.mark.parametrize("network", ["allowlist", "operator-network", "open"])
def test_direct_egress_configuration_cannot_upgrade_attestation(
    tmp_path: Path, network: str
) -> None:
    """Existing networks and operator settings cannot upgrade direct egress."""
    from kew import sandbox
    from kew.dispatch_core import _attest_direct_sandbox_resolution, _DispatchContext
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        sandbox=SandboxConfig(
            enabled="auto",
            network=network,
            images={"claude-code": "operator-image"},
            dangerously_skip_containment=True,
        )
    )
    # Arbitrary extra settings fail validation before dispatch admission.
    for extra in ("egress_verification", "sidecar_certified", "operator_rules"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            SandboxConfig.model_validate(
                {**config.sandbox.model_dump(), extra: "operator-verified"}
            )
    name = sandbox.effective_network_name(network)
    if name is not None:
        with patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess([], 0, "[]", ""),
        ) as inspect_network:
            assert sandbox.allowlist_network_exists("docker", name)
        assert inspect_network.call_args.args[0] == [
            "docker",
            "network",
            "inspect",
            name,
        ]

    ctx = MagicMock(spec=_DispatchContext)
    ctx.config = config
    ctx.run_id = "direct-1945"
    ctx.issue_num = 1945
    ctx.item_id = "1945"
    # Reuse a certified sidecar decision to expose route cross-contamination.
    decision = sandbox.SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="operator-image",
        egress_verification="sidecar-certified",
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        ctx.store = store
        _attest_direct_sandbox_resolution(
            ctx, "claude-code", decision, MagicMock(sidecar=False), tmp_path
        )
        [event] = store.list_audit_events(event="sandbox_resolved")

    assert event.payload.get("egress_verification_basis") == "operator-asserted"
    assert (
        event.payload["egress_verification"] == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    )
    assert event.payload["network_boundary"] == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION


@pytest.mark.parametrize("flow", ["review", "resolve", "ci-fix", "plan-run"])
def test_direct_flow_egress_uses_frozen_value(tmp_path: Path, flow: str) -> None:
    """The second posture writer shares the direct-route audit invariant."""
    from kew import sandbox
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "flow.db")) as store:
        sandbox.attest_flow_posture(
            store,
            flow=flow,
            runner="claude-code",
            config=SandboxConfig(),
            posture="sandboxed",
            run_id=f"{flow}-1945",
        )
        [event] = store.list_audit_events(event="sandbox_resolved")
    assert event.payload.get("egress_verification_basis") == "operator-asserted"
    assert (
        event.payload["egress_verification"] == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    )
    assert event.payload["network_boundary"] == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION


# ── Per-runner env allowlist (#1146 / #779 N5) ────────────────────────────────
#
# The containment property, stated directly: the env allowlist is keyed by the
# RESOLVED runner, so a contained run only ever sees the credentials its own
# runner authenticates with. Cross-runner leakage here is a real exfiltration
# path — an opencode container that can read CLAUDE_CODE_OAUTH_TOKEN can spend
# the operator's Anthropic subscription, and vice versa.


def _is_worker_pool_size(entry: str) -> bool:
    """``entry`` is a test-runner pool size: a #2887 name with a positive integer.

    Only the names in ``TEST_WORKER_ENV_NAMES`` qualify, and only with a value
    of plain digits — ``PYTEST_XDIST_AUTO_NUM_WORKERS=abc`` or any other name
    with a value is still a forwarded VALUE.
    """
    name, sep, value = entry.partition("=")
    return (
        bool(sep)
        and name in TEST_WORKER_ENV_NAMES
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    )


def _env_names(argv: list[str]) -> list[str]:
    """The credential names forwarded via ``-e`` (by name only — never ``NAME=value``).

    Three non-credential ``-e`` shapes are filtered out here and pinned
    elsewhere: the guard marker ``AGENT_ASSIGNED_WORKTREE=/work`` (#1691,
    ``test_sandbox.py``), the ten proxy variables kew blanks explicitly
    (``HTTP_PROXY=`` and friends, #1933, ``test_sandbox.py``) so the runtime
    cannot inject a proxy URL with embedded credentials, and the test-runner
    pool sizes (``PYTEST_XDIST_AUTO_NUM_WORKERS=3`` and friends, #2887,
    ``test_sandbox_test_worker_pool.py``), which carry only a positive integer.
    A blank carries no value; ``test_credentials_travel_by_name_only_for_every_runner``
    pins that those are the only ``=`` entries.
    """
    from kew.sandbox import CONTROLLED_PROXY_NAMES

    proxy_blanks = {f"{name}=" for name in CONTROLLED_PROXY_NAMES}
    return [
        argv[i + 1]
        for i, a in enumerate(argv)
        if a == "-e"
        and not argv[i + 1].startswith(
            (f"{AGENT_MARKER_ENV}=", "KEW_AGENT_START_NONCE=")
        )
        and argv[i + 1] not in proxy_blanks
        and not _is_worker_pool_size(argv[i + 1])
    ]


def test_only_a_positive_integer_worker_pool_size_is_exempt_from_name_only() -> None:
    """The #2887 exemption is by name and integer value, nothing wider.

    A pool-size name with a non-integer value, or any other name with a value,
    still surfaces as a forwarded VALUE.
    """
    argv = [
        "-e",
        "PYTEST_XDIST_AUTO_NUM_WORKERS=3",
        "-e",
        "VITEST_MAX_WORKERS=12",
        "-e",
        "PYTEST_XDIST_AUTO_NUM_WORKERS=abc",
        "-e",
        "VITEST_MAX_FORKS=0",
        "-e",
        "VITEST_MAX_THREADS=",
        "-e",
        "ANTHROPIC_API_KEY=3",
        "-e",
        "ANTHROPIC_API_KEY",
    ]

    assert _env_names(argv) == [
        "PYTEST_XDIST_AUTO_NUM_WORKERS=abc",
        "VITEST_MAX_FORKS=0",
        "VITEST_MAX_THREADS=",
        "ANTHROPIC_API_KEY=3",
        "ANTHROPIC_API_KEY",
    ]


def test_contained_opencode_run_does_not_carry_claude_credentials(
    tmp_path: Path,
) -> None:
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    argv = build_run_argv(
        runtime="docker",
        image="kew-opencode:latest",
        checkout_dir=checkout_dir,
        slug="1146",
        network="allowlist",
        runner="opencode",
    )

    names = _env_names(argv)
    assert "OPENROUTER_API_KEY" in names, (
        "an opencode container must carry its own provider credential"
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in names
    assert "ANTHROPIC_API_KEY" not in names


def test_contained_claude_run_does_not_carry_openrouter_credentials(
    tmp_path: Path,
) -> None:
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    argv = build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout_dir,
        slug="1146",
        network="allowlist",
        runner="claude-code",
    )

    names = _env_names(argv)
    assert set(names) == {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert "OPENROUTER_API_KEY" not in names


def test_credentials_travel_by_name_only_for_every_runner(tmp_path: Path) -> None:
    """Widening the allowlist must not widen HOW credentials travel (#1146).

    Every declared runner's injection is ``-e NAME`` — never ``-e NAME=value``,
    never a mounted credential file, never a host path.
    """
    from kew.runners.registry import KNOWN_RUNNER_NAMES

    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()

    from kew.sandbox import CONTROLLED_PROXY_NAMES

    proxy_blanks = {f"{name}=" for name in CONTROLLED_PROXY_NAMES}

    for runner in sorted(KNOWN_RUNNER_NAMES):
        argv = build_run_argv(
            runtime="docker",
            image="example:latest",
            checkout_dir=checkout_dir,
            slug="1146",
            network="allowlist",
            runner=runner,
        )
        for name in _env_names(argv):
            assert "=" not in name, f"{runner} forwards a VALUE, not a name: {name!r}"
            assert "/" not in name, f"{runner} forwards a path, not a name: {name!r}"
        # The only ``=`` entries besides the guard marker, main's per-launch
        # startup nonce (a fresh random acknowledgement token, not a
        # credential) and the positive-integer test-runner pool sizes (#2887)
        # are the ten proxy blanks (#1933), and a blank carries no value:
        # exactly ``NAME=``.
        raw = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assignments = {
            entry
            for entry in raw
            if "=" in entry
            and not entry.startswith((AGENT_MARKER_ENV, "KEW_AGENT_START_NONCE="))
            and not _is_worker_pool_size(entry)
        }
        assert assignments == proxy_blanks, (
            f"{runner} forwards an assignment beyond the ten proxy blanks: "
            f"{sorted(assignments - proxy_blanks)!r}"
        )
        assert len([a for a in argv if a == "-v"]) == 1, (
            f"{runner} mounts something beyond the checkout"
        )


def test_open_egress_warning_names_the_injected_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #935 warning still fires and now names the credential at risk (#1146)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    warning = open_egress_warning("open", runner="opencode")
    assert warning is not None
    assert "OPENROUTER_API_KEY" in warning
    # The VALUE never appears — only the name (#1146).
    assert "sk-or-test" not in warning

    # claude's credential is absent, so claude's contained run has nothing to
    # exfiltrate: the warning is keyed on the runner's OWN credentials.
    assert open_egress_warning("open", runner="claude-code") is None


def test_bounded_container_launch_environment_is_private(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from kew.sandbox import build_run_argv, launch_container

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-secret-1944")
    popen = Mock()
    monkeypatch.setattr("kew.sandbox.subprocess_utils.popen", popen)
    argv = build_run_argv(
        runtime="docker",
        image="test",
        checkout_dir=tmp_path,
        slug="1944",
        runner="opencode",
        network="allowlist",
    )
    launch_container(
        argv,
        env={
            "OPENROUTER_API_KEY": "run-secret-1944",
            "OPENROUTER_PROVISIONING_KEY": "management-secret-1944",
        },
    )
    assert popen.call_args.kwargs["env"] == {}
    assert "secret-1944" not in str(argv)


def test_bounded_container_preflight_accepts_host_provisioner(monkeypatch):
    """`brokered=None` is the docker boundary: the environment is the answer."""
    from kew.sandbox import _runner_credential_available

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    assert _runner_credential_available(("OPENROUTER_API_KEY",), None, None)
    assert not _runner_credential_available(("UNRELATED_API_KEY",), None, None)


def test_verification_and_extraction_share_checkout_program_boundary(
    tmp_path, monkeypatch
):
    from kew import sandbox

    builder = getattr(sandbox, "build_checkout_program_argv", None)
    assert callable(builder), "extraction and verification need one argv builder"
    monkeypatch.setattr(
        sandbox, "_new_container_cidfile", lambda slug: tmp_path / "cid"
    )
    common = dict(
        runtime="docker",
        image="approved:tag",
        checkout_dir=tmp_path / "checkout",
        uid=501,
        gid=20,
    )
    extraction = sandbox.build_extract_argv(
        **common,
        artifact_dir=tmp_path / "artifact",
        slug="same",
        base_sha="a" * 40,
    )
    shared = builder(
        **common,
        artifact_dir=tmp_path / "artifact",
        slug="extract-same",
        program=[sandbox.EXTRACT_ENTRYPOINT, "a" * 40, sandbox.EXTRACT_MOUNT_PATH],
    )
    assert extraction == shared
    verification = builder(
        **common, slug="verify-same", program=["/bin/sh", "-c", "./verify.sh"]
    )
    # Every isolation flag before the entrypoint is identical except the name.
    boundary = extraction[: extraction.index("--entrypoint")]
    boundary[boundary.index("--name") + 1] = "kew-verify-same"
    assert verification[: verification.index("--entrypoint")] == boundary
    assert f"{tmp_path / 'checkout'}:/work:ro" in verification
